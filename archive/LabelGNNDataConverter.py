import os
import json
import torch
from torch_geometric.data import Data
import tkinter as tk
from tkinter import filedialog

#Allowing user to pick specific file
root = tk.Tk()
root.withdraw()
folder = filedialog.askdirectory(title="Select GNN JSON Folder")

files = [f for f in os.listdir(folder) if f.endswith(".json")]
total = len(files)

#First pass: load every JSON once and collect raw labels so we can compute a
#dataset-wide mean/std to standardize against (can't standardize per-file, since
#the mean/std has to come from the whole dataset, not a single graph).
raw_graphs = []
raw_labels = []

for i, filename in enumerate(files):
    print(f"Reading {i+1}/{total}: {filename}")

    with open(os.path.join(folder, filename), "r") as f:
        raw = json.load(f)

    raw_graphs.append(raw)
    if "Label" in raw:
        raw_labels.append(raw["Label"])

label_mean = [0.0, 0.0]
label_std = [1.0, 1.0]
if raw_labels:
    labels_tensor = torch.tensor(raw_labels, dtype=torch.float)
    label_mean = labels_tensor.mean(dim=0).tolist()
    label_std = [s if s > 1e-8 else 1.0 for s in labels_tensor.std(dim=0).tolist()]

#Second pass: build the actual graphs, standardizing each label against the
#dataset-wide mean/std computed above.
dataset = []
for i, raw in enumerate(raw_graphs):
    print(f"Building {i+1}/{total}")

    features = sorted(raw["Features"], key=lambda n: n["Item1"])
    index_map = {n["Item1"]: i for i, n in enumerate(features)}
    x = torch.tensor([n["Item2"] for n in features], dtype=torch.float)

    from_local = [index_map[i] for i in raw["From"]]
    to_local   = [index_map[i] for i in raw["To"]]
    edge_index = torch.tensor([from_local, to_local], dtype=torch.long)

    data = Data(x=x, edge_index=edge_index)

    if "Label" in raw:
        standardized = [(raw["Label"][d] - label_mean[d]) / label_std[d] for d in range(2)]
        data.y = torch.tensor(standardized, dtype=torch.float)

    dataset.append(data)

save_folder = os.path.join(os.path.expanduser("~"), "Downloads", "GNN Dataset")
os.makedirs(save_folder, exist_ok=True)

torch.save(dataset, os.path.join(save_folder, "dataset.pt"))
torch.save({"mean": label_mean, "std": label_std}, os.path.join(save_folder, "label_stats.pt"))