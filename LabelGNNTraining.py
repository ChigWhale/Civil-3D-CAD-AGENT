from GNN_NeuralNetwork import CADGNN
import torch
import torch.nn.functional as F
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import matplotlib.widgets as widgets
import matplotlib.animation as animation
import tkinter as tk
from tkinter import filedialog
from torch_geometric.loader import DataLoader
from torch_geometric.nn import global_mean_pool
import threading
import time

model = CADGNN()
optimizer = torch.optim.Adam(model.parameters(), lr=0.01)

root = tk.Tk()
root.withdraw()
file_path = filedialog.askopenfilename(
    title = "Select dataset file",
    filetypes = [("PyTorch files", "*.pt")]
)

dataset = torch.load(file_path, weights_only=False)
loader = DataLoader(dataset, batch_size = 32, shuffle = True)
num_epochs = 8

show_graph = input("Show training visualization? (y/n): ").strip().lower() == "y"

plot_data = {"xs": [], "ys": [], "zs": [], "colors": [], "batch_nums": [], "paused": False}
batch_num = 1

def epoch_color(epoch):
    third = num_epochs / 3
    if epoch < third:
        return (epoch / third, 0, 0)
    elif epoch < 2 * third:
        return (1, (epoch - third) / third, 0)
    else:
        return (1, 1, (epoch - 2 * third) / third)

def run_plot():
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection="3d")

    btn_ax = plt.axes([0.8, 0.01, 0.1, 0.05])
    pause_btn = widgets.Button(btn_ax, "Pause")

    def toggle_pause(event):
        plot_data["paused"] = not plot_data["paused"]
        pause_btn.label.set_text("Resume" if plot_data["paused"] else "Pause")

    pause_btn.on_clicked(toggle_pause)

    window_size = 5

    def update(frame):
        ax.clear()
        xs = plot_data["xs"][-window_size:]
        ys = plot_data["ys"][-window_size:]
        zs = plot_data["zs"][-window_size:]
        colors = plot_data["colors"][-window_size:]
        batch_nums = plot_data["batch_nums"][-window_size:]
        if xs:
            ax.scatter(xs, ys, zs, c=colors)
            for x, y, z, n in zip(xs, ys, zs, batch_nums):
                ax.text(x, y, z, str(n))
        ax.set_xlabel("X Dist. Error")
        ax.set_ylabel("Y Dist. Error")
        ax.set_zlabel("Tot. Error")
        ax.text2D(0.02, 0.95, f"Batch {len(plot_data['xs'])}", transform=ax.transAxes)

    ani = animation.FuncAnimation(fig, update, interval=500)
    plt.show()

if show_graph:
    plot_thread = threading.Thread(target=run_plot, daemon=True)
    plot_thread.start()

for epoch in range(num_epochs):
    for batch_in_epoch, batch in enumerate(loader, start=1):
        optimizer.zero_grad()
        out = model(batch)
        out_pooled = global_mean_pool(out, batch.batch)
        batch_y = batch.y.view(-1, 2)
        loss = F.mse_loss(out_pooled, batch_y)
        loss.backward()
        optimizer.step()

        per_dim = (out_pooled - batch_y).abs().mean(dim=0)
        x_loss = per_dim[0].item()
        y_loss = per_dim[1].item()
        tot_loss = (x_loss + y_loss) / 2

        print(f"epoch {epoch + 1}: batch {batch_in_epoch}: Training error: {tot_loss}")

        if show_graph:
            plot_data["xs"].append(x_loss)
            plot_data["ys"].append(y_loss)
            plot_data["zs"].append(tot_loss)
            plot_data["colors"].append(epoch_color(epoch))
            plot_data["batch_nums"].append(batch_num)
            while plot_data["paused"]:
                time.sleep(0.1)

        batch_num += 1

torch.save(model.state_dict(), "gnn_weights.pt")