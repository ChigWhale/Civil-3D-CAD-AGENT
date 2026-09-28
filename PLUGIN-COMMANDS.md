# CADAgent.dll command-line commands

Load the plugin once per session with `NETLOAD` and pick
`bin\Debug\net10.0-windows\CADAgent.dll`. Loading it also starts the HTTP
bridge on `http://127.0.0.1:5199`, which is how Claude Desktop and the
dashboard drive the drawing, and adds a **CAD-AGENT** tab to the ribbon. The
commands below are the only ones you type at the Civil 3D command line;
everything else in the plugin (drafting intents, grading labels, label
spacing, sheets, pressure runs) has no command name and runs through the
bridge.

## The CAD-AGENT ribbon tab

One panel, one button: **Start Dashboard**. It does what `CADAGENT` does
(below), so the dashboard can be brought up from Civil 3D alone, with no
Claude Desktop, ChatGPT or terminal involved. The tab is re-added if a
workspace switch rebuilds the ribbon.

| Command | What it does | Writes to the drawing? |
|---|---|---|
| `HARVEST` | Adds the open drawing to the standards catalog | No |
| `HARVESTOPEN` | Opens `standards\global.md` | No |
| `GETCADOBJECTS` | Refreshes the model-space snapshot and prints its size | No |
| `DRAWSPATIALHASH` | Draws the spatial hash grid for inspection | Yes, on `SPATIALHASH_DEBUG` |
| `GRAPHOBJECT` | Builds an object graph around one picked entity | No |
| `GraphNeuralNetworkCreator` | Exports graph JSON for the GNN label experiments | No (writes files) |
| `CADAGENT` | Starts the dashboard (and model server) if down, then opens the dashboard in the browser | No |
| `ASKAGENT` | Prints a reminder that commands go through Claude Desktop | No |

## HARVEST

Reads the drawing that is open and current: model space, the layer table,
block definitions and drawing settings. Hands them to `harvest.py`, which
adds one observation for the drawing and rebuilds `standards\global.json` and
`standards\global.md`. Prints the entity, layer, block and part counts and the
two output paths.

Needs `python` on PATH, or the `CAD_PYTHON` environment variable pointing at
the interpreter, and the repo folder somewhere above the DLL, or
`CAD_AGENT_ROOT` set to it. Running it twice on the same drawing changes
nothing. See `standards\USING-HARVEST.md` for the full loop.

## HARVESTOPEN

Opens `standards\global.md` in whatever handles `.md` files. Reports if the
file does not exist yet.

## GETCADOBJECTS

Runs the same collector the bridge uses for `GET /modelspace` and prints how
large the JSON came out. Useful to confirm the plugin is loaded and the
drawing can be read. The JSON stays in memory; nothing is saved.

## DRAWSPATIALHASH

Builds the 50 ft spatial hash over every line, arc and polyline in model
space and draws each occupied cell as a red polyline, with circles at cell
centres, on a layer named `SPATIALHASH_DEBUG`. Debug only. Erase or freeze
that layer afterwards; the command does not clean up after itself.

## GRAPHOBJECT

Prompts for one entity, then collects everything inside a 50 by 50 ft box
around it into a node and edge graph with normalised coordinates. Prints node
and edge counts and a few normalisation debug lines. Nothing is drawn or
saved. This fed the label GNN work and is not used by the agent.

## GraphNeuralNetworkCreator

Interactive. Asks whether to build a label graph or a feedforward graph
(`L`/`F`) and for a project name, then walks the drawing and writes one JSON
file per graph to `Downloads\JSON GNN Representation\`. Part of the older
neural-network labelling experiment (`GNN_*.py`, `LabelGNN*.py`), kept for
reference.

## CADAGENT

The command behind the ribbon's Start Dashboard button. Runs
`launcher.py open` without a window: that starts the model server and the
dashboard when they are not already answering, then opens
`http://localhost:8765` in the default browser. The Civil 3D command line
gets one line saying it started; the browser tab appears once the dashboard
answers, which can take up to a minute the first time.

Where things come from: in a packaged install, `install.json` beside the DLL
names the bundled Python and launcher. In a developer checkout, `launcher.py`
is found by walking up from the DLL (or `CAD_AGENT_ROOT`) and Python comes
from `CAD_PYTHON` or `python` on PATH. With neither, the command just opens
the address in case a dashboard is already running and says so.

## ASKAGENT

Prints one line saying the agent is running and to use Claude Desktop. It
does nothing else.

## Rebuilding after a code change

Civil 3D holds the loaded DLL, so a build will compile but fail to copy into
`bin` while it is open. Close Civil 3D, run `dotnet build CADAgent.csproj`
from the repo root, then reopen and `NETLOAD` again. Confirm the loaded build
with `GET http://127.0.0.1:5199/intents`, which reports the build stamp.

The Civil 3D 2025 update of 2026-09-28 moved its managed assemblies to
.NET 10 (acad.exe now runs on the 10.0 runtime), so `CADAgent.csproj` and
`PipeInvertSetter.csproj` target `net10.0-windows`; a net8 target no longer
compiles against them. The output folder is `bin\Debug\net10.0-windows`
accordingly.

## Loading automatically (no NETLOAD)

`package\install-dev-bundle.ps1` installs this checkout's build as an
Autodesk bundle in `%APPDATA%\Autodesk\ApplicationPlugins\CAD-Agent.bundle`.
Civil 3D loads it at startup, the `CAD-AGENT` ribbon tab appears, and with
`autostart` in the bundle's `install.json` the dashboard and model server are
started too. From then on every build of `CADAgent.csproj` copies its output
into the bundle (the `SyncDevBundle` target), so a code change only needs a
build and a Civil 3D restart. Stop NETLOADing from `bin\` once the bundle is
installed; loading the assembly twice errors. `-Uninstall` removes it. The
installer built by `package\build.ps1` lays down the same bundle for users.
