# Archive

Earlier experiments kept for reference. Nothing in this folder is imported by
the running system, and the packager (`package/build.ps1`) leaves it out.

- `ollama_agent.py`: the first agent, May 2026. A LangGraph planner on a local
  Llama model that emitted JSON steps for the plugin. Superseded by the MCP
  server (`cad_mcp_server.py`), where Claude Desktop or the dashboard's local
  model chooses tools directly.
- `GNN_NeuralNetwork.py`, `LabelGNNTraining.py`, `LabelGNNDataConverter.py`,
  `GNN_Label_Verification.py`, `gnn_weights.pt`: a July 2026 experiment in
  learning label placement with a graph neural network (PyTorch Geometric).
  The graph exporter still lives in the plugin as the
  `GraphNeuralNetworkCreator` command (`CreateGraphs.cs`).
- `Testing.py`: scratch file.
