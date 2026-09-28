# Packaging CAD-Agent

One installer, `CAD-Agent-Setup-<version>.exe`, that carries its own Python,
the plugin, the model runtime and (optionally) the models. The user needs
Civil 3D and nothing else.

## What the installer lays down

```
%LOCALAPPDATA%\Programs\CAD-Agent\        program folder, read-only after install
  cad-agent.json                          written by `launcher.py install` from cad-agent.template.json
  version.txt
  python\                                 embeddable Python + site-packages (requirements-runtime.txt)
  app\                                    the repo's Python code and local data (playbooks, standards, layer files)
  models\                                 llama-swap.exe, llama-server.exe, CUDA runtime, *.gguf
%APPDATA%\Autodesk\ApplicationPlugins\CAD-Agent.bundle\
  PackageContents.xml                     Civil 3D autoloads Contents\CADAgent.dll
  Contents\CADAgent.dll ... install.json  install.json tells the plugin where python\ and app\ are
%LOCALAPPDATA%\CAD-Agent\                 everything written at run time
  dashboard\  reviews\  exemplars\  learned\  playbooks\  standards\  logs\  run\  layer_tags.learned.json
```

How the pieces find each other:

- `cadagent_config.py` reads `cad-agent.json` (next to `app\`), with environment
  variables as overrides. Every process uses it for the plugin and dashboard
  addresses, the model endpoint and the writable data dir. In a developer
  checkout there is no file and every module keeps its old repo-relative default.
- `launcher.py` (in `app\`) starts llama-swap and the dashboard, opens the
  browser, stops them, fetches models, and writes the two JSON files at install.
- The plugin reads `install.json`; with `autostart` it runs the launcher when
  Civil 3D loads, and the `CADAGENT` command opens the dashboard. HARVEST uses
  the bundled Python instead of PATH.

## .NET version

The plugin targets `net10.0-windows` (both `CADAgent.csproj` and
`PipeInvertSetter.csproj`), because Autodesk's managed assemblies moved to
.NET 10 with the AutoCAD 2025 update of 2026-09-28. Consequences:

- Build output is `bin\<Config>\net10.0-windows\`; the build script copies the
  bundle from `bin\Release\net10.0-windows`.
- The installer ships no .NET runtime. The plugin runs inside Civil 3D's
  process, which brings the .NET 10 runtime it was updated with.
- The bundle's `SeriesMin` is `R25.0`: a Civil 3D 2025 without that update
  (or anything older) cannot load a .NET 10 assembly, and the manifest keeps
  Civil 3D from trying. Raise `SeriesMax` when a newer release is tested.
- The build machine needs the .NET 10 SDK.

## Building

Prerequisites on the build machine only: .NET 10 SDK (the plugin), Civil 3D
with the .NET 10 update (reference assemblies), internet access (Python
embeddable zip, PyPI, llama.cpp and llama-swap releases), and Inno Setup 6
for the `.exe`.

```
.\package\build.ps1 -Version 1.0.0 `
    -LlamaSwapUrl   https://github.com/mostlygeek/llama-swap/releases/download/<tag>/llama-swap_<tag>_windows_amd64.zip `
    -LlamaServerUrl https://github.com/ggml-org/llama.cpp/releases/download/<build>/llama-<build>-bin-win-cuda-12.4-x64.zip `
    -CudartUrl      https://github.com/ggml-org/llama.cpp/releases/download/<build>/cudart-llama-bin-win-cuda-12.4-x64.zip `
    -ModelsDir      D:\models `      # optional: .gguf files to include; otherwise fetched on first run
    -SeriesMin R25.0 -SeriesMax R25.1 `   # the Civil 3D releases the plugin was built against
    -Installer
```

`-SkipPlugin` reuses `bin\Release`; `-SkipPython` reuses a staged `python\`.
Downloads are cached in `package\downloads`. For a CPU-only build use the
`bin-win-cpu-x64` llama.cpp archive and drop `-CudartUrl`.

The script checks itself: the staged Python must import every runtime
dependency, and the staged `app\` must import the MCP server, the dashboard,
the redline pipeline and the launcher under that Python.

## Model files

`cad-agent.template.json` names the two models, their files and download
URLs. `launcher.py fetch-models` downloads what is missing (resumable). You
can point the URLs at a network share instead of huggingface.co. Verify
the URLs and file names against the current releases before a build; the
GGUF repositories rename files between quantisation updates.

llama-swap serves both models on one OpenAI-compatible port and loads
whichever is asked for, unloading the other after `ttl` seconds, which is
what an 8 GB card needs. The pipeline asks for `vlm` first for every page,
then `llm` for every comment, so the swap happens once per review.

## Testing a build without installing

```
.\package\build.ps1 -Version 0.0.1 -SkipPlugin
.\package\stage\python\python.exe .\package\stage\app\launcher.py install --data-dir %TEMP%\cad-agent-data
.\package\stage\python\python.exe .\package\stage\app\launcher.py doctor
.\package\stage\python\python.exe .\package\stage\app\launcher.py start --no-models
```

`install` without `--bundle` writes only `cad-agent.json`; `doctor` prints
what every process would resolve.

## Licences in the box

llama.cpp and llama-swap: MIT. Qwen weights: Apache-2.0. Python: PSF.
FastAPI, uvicorn, requests, numpy, Pillow, openpyxl, mcp, anthropic: MIT or
BSD. pypdfium2: BSD-3 / Apache-2.0. PyMuPDF (AGPL) is not shipped; it is
only a development fallback in `redline/pdf.py` and a test dependency.
