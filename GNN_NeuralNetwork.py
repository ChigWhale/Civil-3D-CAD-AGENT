import torch
import torch.nn.functional as F
from torch_geometric.nn import GCNConv

class CADGNN(torch.nn.Module):
    def __init__(self):
        super().__init__()
        