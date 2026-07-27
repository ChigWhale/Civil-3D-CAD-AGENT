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

dataset = []
files = [f for f in os.listdir(folder) if f.endswith(".json")]
total = len(files)

for i, filename in enumerate(files):
    print(f"Processing {i+1}/{total}: {filename}")

    with open(os.path.join(folder, filename), "r") as f:
        raw = json.load(f)

    features = sorted(raw["Features"], key=lambda n: n["Item1"])
    index_map = {n["Item1"]: i for i, n in enumerate(features)}
    x = torch.tensor([n["Item2"] for n in features], dtype=torch.float)

    from_local = [index_map[i] for i in raw["From"]]
    to_local   = [index_map[i] for i in raw["To"]]
    edge_index = torch.tensor([from_local, to_local], dtype=torch.long)

    data = Data(x=x, edge_index=edge_index)

    if "Label" in raw:
        data.y = torch.tensor(raw["Label"], dtype=torch.float)

    dataset.append(data)

save_folder = os.path.join(os.path.expanduser("~"), "Downloads", "GNN Dataset")
os.makedirs(save_folder, exist_ok=True)

torch.save(dataset, os.path.join(save_folder, "dataset.pt"))