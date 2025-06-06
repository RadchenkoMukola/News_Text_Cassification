import gc
import os
import pickle
import pandas as pd
import torch
import numpy as np
from torch.utils.data import Dataset
from transformers import LongformerTokenizer, LongformerForSequenceClassification, Trainer, TrainingArguments, EarlyStoppingCallback
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, f1_score
from sklearn.utils.class_weight import compute_class_weight

# ========== Clear Memory ==========
torch.cuda.empty_cache()
gc.collect()
os.chdir("..")

# ========== Load Data ==========
df = pd.read_csv("allsides_news_marked.csv")
labels = df["label"].tolist()
texts = df["text"].astype(str).tolist()

# ========== Train-Test Split ==========
train_texts, val_texts, train_labels, val_labels = train_test_split(
    texts, labels, test_size=0.2, random_state=42, stratify=labels
)

# ========== Tokenizer ==========
tokenizer = LongformerTokenizer.from_pretrained("allenai/longformer-base-4096")
max_length = 1024

def tokenize_with_cache(texts, tokenizer, max_length=1024, cache_path="cache.pkl"):
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    input_ids, attention_mask = [], []
    for i, text in enumerate(texts):
        encoding = tokenizer(
            text,
            truncation=True,
            padding='max_length',
            max_length=max_length
        )
        input_ids.append(encoding['input_ids'])
        attention_mask.append(encoding['attention_mask'])

        if i % 500 == 0:
            print(f"[{i}/{len(texts)}] Tokenized...")
            torch.cuda.empty_cache()
            gc.collect()

    encodings = {'input_ids': input_ids, 'attention_mask': attention_mask}
    with open(cache_path, "wb") as f:
        pickle.dump(encodings, f)
    return encodings

train_encodings = tokenize_with_cache(train_texts, tokenizer, max_length, "train_longformer.pkl")
val_encodings = tokenize_with_cache(val_texts, tokenizer, max_length, "val_longformer.pkl")

# ========== Dataset ==========
class NewsDataset(Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return {
            'input_ids': torch.tensor(self.encodings['input_ids'][idx]),
            'attention_mask': torch.tensor(self.encodings['attention_mask'][idx]),
            'labels': torch.tensor(self.labels[idx] + 1),
        }

train_dataset = NewsDataset(train_encodings, train_labels)
val_dataset = NewsDataset(val_encodings, val_labels)

# ========== Class Weights ==========
class_weights = compute_class_weight(class_weight='balanced', classes=np.array([-1, 0, 1]), y=np.array(train_labels))
class_weights_tensor = torch.tensor(class_weights, dtype=torch.float)

# ========== Weighted Trainer ==========
class WeightedTrainer(Trainer):
    def __init__(self, *args, class_weights=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):  # <-- додано **kwargs
        labels = inputs.get("labels")
        outputs = model(**inputs)
        logits = outputs.get("logits")
        loss_fct = torch.nn.CrossEntropyLoss(weight=self.class_weights.to(model.device))
        loss = loss_fct(logits, labels)
        return (loss, outputs) if return_outputs else loss

# ========== Metrics ==========
def compute_metrics(pred):
    labels = pred.label_ids - 1
    preds = np.argmax(pred.predictions, axis=1) - 1
    acc = accuracy_score(labels, preds)
    f1 = f1_score(labels, preds, average='macro')
    return {"accuracy": acc, "f1": f1}

# ========== Model ==========
checkpoint_path = "./results_phase2/checkpoint-9580"  # або None, якщо з останнього

if checkpoint_path and os.path.exists(checkpoint_path):
    model = LongformerForSequenceClassification.from_pretrained(checkpoint_path)
    rng_file = os.path.join(checkpoint_path, "rng_state.pth")
    if os.path.exists(rng_file):
        os.remove(rng_file)
else:
    model = LongformerForSequenceClassification.from_pretrained("allenai/longformer-base-4096", num_labels=3)
model.gradient_checkpointing_enable()
model = model.to("cuda")

# ========== Training Args ==========
training_args = TrainingArguments(
    output_dir='./results_LF_weighted',
    eval_strategy="epoch",
    save_strategy="epoch",
    num_train_epochs=5,
    per_device_train_batch_size=4,
    per_device_eval_batch_size=4,
    warmup_steps=50,
    weight_decay=0.01,
    logging_dir='./logs_lonformer_weighted',
    logging_steps=100,
    load_best_model_at_end=True,
    metric_for_best_model="accuracy",
    learning_rate=2e-6,
    fp16=False,
    max_grad_norm=5.0
)

# ========== 10. Metrics ==========
def compute_metrics(pred):
    labels = pred.label_ids - 1
    preds = np.argmax(pred.predictions, axis=1) - 1
    acc = accuracy_score(labels, preds)
    f1 = f1_score(labels, preds, average='macro')
    return {"accuracy": acc, "f1": f1}

# ========== 11. Train ==========
trainer = WeightedTrainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=val_dataset,
    compute_metrics=compute_metrics,
    class_weights=class_weights_tensor,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=2)]
)

torch.cuda.empty_cache()
gc.collect()
trainer.train()