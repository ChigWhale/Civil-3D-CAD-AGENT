<#
.SYNOPSIS
  Assembles the CAD-Agent package in package\stage and, when Inno Setup is
  installed, compiles the installer into package\dist.

.DESCRIPTION
  Layout produced (see package\README.md):
    stage\bundle\CAD-Agent.bundle\   PackageContents.xml + Contents\*.dll   (Release build of the plugin)
    stage\python\                    embeddable Python + site-packages       (requirements-runtime.txt)
    stage\app\                       the repo's Python code and local data
    stage\models\                    llama-swap.exe, llama-server.exe (+ CUDA runtime), optional .gguf files
    stage\version.txt, stage\cad-agent.template.json

  Downloads are cached in package\downloads so a rebuild is quick.

.EXAMPLE
  .\package\build.ps1 -Version 1.0.0 -LlamaSwapUrl <zip url> -LlamaServerUrl <zip url> -CudartUrl <zip url> -Installer
#>
param(
    [string]$Version = "0.1.0",
    [string]$PythonVersion = "3.12.10",
    [string]$Stage = "",
    [string]$LlamaSwapUrl = "",
    [string]$LlamaServerUrl = "",
    [string]$CudartUrl = "",
    [string]$ModelsDir = "",
    # The plugin targets net10.0-windows because Autodesk's managed assemblies moved to
    # .NET 10 with the 2025 update of 2026-09-28; only releases from that update on
    # (R25.0) can load it. Civil 3D hosts the runtime, so nothing extra is installed.
    [string]$SeriesMin = "R25.0",
    [string]$SeriesMax = "R26.0",
    [switch]$SkipPlugin,
    [switch]$SkipPython,
    [switch]$Installer,
    # dashboard + plugin + drafting scripts only: no models, no model runtime, no GPU
    # needed, one small Setup.exe (the redline comment automation is not included)
    [switch]$Lite
)
if ($Lite) { $LlamaSwapUrl = ""; $LlamaServerUrl = ""; $CudartUrl = ""; $ModelsDir = "" }

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Pkg = $PSScriptRoot
if (-not $Stage) { $Stage = Join-Path $Pkg "stage" }
$Downloads = Join-Path $Pkg "downloads"
New-Item -ItemType Directory -Force $Downloads | Out-Null

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Fetch($url, $name) {
    $dest = Join-Path $Downloads $name
    if (-not (Test-Path $dest)) {
        Step "downloading $name"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    }
    return $dest
}

# ── clean stage ──────────────────────────────────────────────────────────────
Step "staging into $Stage"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
foreach ($d in "bundle\CAD-Agent.bundle\Contents", "python", "app", "models") {
    New-Item -ItemType Directory -Force (Join-Path $Stage $d) | Out-Null
}
Set-Content -Path (Join-Path $Stage "version.txt") -Value $Version -Encoding ascii
Copy-Item (Join-Path $Pkg "cad-agent.template.json") (Join-Path $Stage "cad-agent.template.json")

# ── 1. plugin: Release build -> bundle ───────────────────────────────────────
$BundleContents = Join-Path $Stage "bundle\CAD-Agent.bundle\Contents"
if (-not $SkipPlugin) {
    Step "building the plugin (Release)"
    & dotnet build (Join-Path $Root "CADAgent.csproj") -c Release --nologo -v quiet
    if ($LASTEXITCODE -ne 0) { throw "plugin build failed" }
}
$BinRelease = Join-Path $Root "bin\Release\net10.0-windows"
if (-not (Test-Path (Join-Path $BinRelease "CADAgent.dll"))) { throw "no Release build at $BinRelease" }
Copy-Item (Join-Path $BinRelease "*.dll") $BundleContents
Copy-Item (Join-Path $BinRelease "*.deps.json") $BundleContents -ErrorAction SilentlyContinue
$manifest = Get-Content (Join-Path $Pkg "bundle\PackageContents.xml") -Raw
if ($SeriesMin -ne "R25.0") {
    # SeriesMin is a floor, not "the release I built against": a bundle stamped R25.1
    # is skipped by Civil 3D 2025 (R25.0) without any message. One package loads on
    # 2025 and 2026 only when the floor is the oldest release it should run in.
    Write-Warning "SeriesMin is $SeriesMin: Civil 3D releases below it will silently NOT load the plugin (2025 = R25.0, 2026 = R25.1). Use R25.0 unless excluding 2025 is intended."
}
$manifest = $manifest.Replace("{{VERSION}}", $Version).Replace("{{SERIESMIN}}", $SeriesMin).Replace("{{SERIESMAX}}", $SeriesMax)
Set-Content -Path (Join-Path $Stage "bundle\CAD-Agent.bundle\PackageContents.xml") -Value $manifest -Encoding utf8

# ── 2. python: embeddable + pip + requirements ──────────────────────────────
$PyDir = Join-Path $Stage "python"
if (-not $SkipPython) {
    $short = ($PythonVersion -split "\.")[0] + ($PythonVersion -split "\.")[1]      # 3.12.10 -> 312
    $zip = Fetch "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" "python-$PythonVersion-embed-amd64.zip"
    Step "unpacking Python $PythonVersion"
    Expand-Archive -Path $zip -DestinationPath $PyDir -Force
    # the ._pth file is the embeddable Python's whole sys.path: add site-packages and app\
    $pth = Join-Path $PyDir "python$short._pth"
    Set-Content -Path $pth -Encoding ascii -Value @(
        "python$short.zip", ".", "Lib\site-packages", "..\app", "import site")
    $getpip = Fetch "https://bootstrap.pypa.io/get-pip.py" "get-pip.py"
    Step "installing pip and the runtime requirements"
    & (Join-Path $PyDir "python.exe") $getpip --no-warn-script-location -q
    if ($LASTEXITCODE -ne 0) { throw "get-pip failed" }
    & (Join-Path $PyDir "python.exe") -m pip install -q --no-cache-dir --no-warn-script-location -r (Join-Path $Pkg "requirements-runtime.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    # nothing here should pull torch or a compiler
    & (Join-Path $PyDir "python.exe") -c "import fastapi, uvicorn, mcp, numpy, PIL, pypdfium2, openpyxl, requests; print('runtime imports ok')"
    if ($LASTEXITCODE -ne 0) { throw "runtime import check failed" }
}

# ── 3. app: the repo's Python and local data ─────────────────────────────────
Step "copying the app"
$AppDir = Join-Path $Stage "app"
$exclude = @(".git", ".vs", ".claude", "bin", "obj", "tests", "package", "finetune", ".venv-finetune", "__pycache__", "MathDrill", "Gambol",
             "uploads", "uploads-test", "downloads", "stage", "dist", "PipeInvertSetter", "PipeInvertSetter.Tests",
             "ReeseOclock", "Commands", ".run", "observations", "FeatureLineSync", "FeatureLineSync.Tests")
if ($Lite) { $exclude += "redline" }      # the comment addresser stays out of the lite package
$excludeFiles = @("*.cs", "*.csproj", "*.sln", "*.slnx", "*.pyc", "*.user", "*.xaml", "*.pt", "layer_tags.learned.json",
                  "ollama_agent.py", "GNN_*.py", "LabelGNN*.py", "Testing.py", "*.bak", "redline.json", "cad-agent.json",
                  "build-test.log")
$rc = & robocopy $Root $AppDir /E /NFL /NDL /NJH /NJS /XD $exclude /XF $excludeFiles
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
$global:LASTEXITCODE = 0
$mods = if ($Lite) { "cad_mcp_server, dashboard.server, launcher" } else { "cad_mcp_server, dashboard.server, redline.pipeline, launcher" }
& (Join-Path $PyDir "python.exe") -c "import sys; sys.path.insert(0, r'$AppDir'); import $mods; print('app imports ok')"
if ($LASTEXITCODE -ne 0) { throw "app import check failed" }
Get-ChildItem $AppDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force    # left by the check

# ── 4. models: runtime binaries and optional weights ────────────────────────
$ModelsStage = Join-Path $Stage "models"
if ($LlamaSwapUrl) {
    $z = Fetch $LlamaSwapUrl "llama-swap.zip"
    Expand-Archive -Path $z -DestinationPath (Join-Path $Downloads "llama-swap") -Force
    Get-ChildItem (Join-Path $Downloads "llama-swap") -Recurse -Filter "llama-swap*.exe" | Select-Object -First 1 |
        ForEach-Object { Copy-Item $_.FullName (Join-Path $ModelsStage "llama-swap.exe") }
}
if ($LlamaServerUrl) {
    $z = Fetch $LlamaServerUrl "llama-server.zip"
    Expand-Archive -Path $z -DestinationPath (Join-Path $Downloads "llama-server") -Force
    Get-ChildItem (Join-Path $Downloads "llama-server") -Recurse -Include "llama-server.exe", "*.dll" |
        ForEach-Object { Copy-Item $_.FullName $ModelsStage -Force }
}
if ($CudartUrl) {
    $z = Fetch $CudartUrl "cudart.zip"
    Expand-Archive -Path $z -DestinationPath (Join-Path $Downloads "cudart") -Force
    Get-ChildItem (Join-Path $Downloads "cudart") -Recurse -Filter "*.dll" | ForEach-Object { Copy-Item $_.FullName $ModelsStage -Force }
}
if ($ModelsDir -and (Test-Path $ModelsDir)) {
    Step "linking model files from $ModelsDir"
    # gigabytes each: hard-link when the stage is on the same volume, copy otherwise
    Get-ChildItem $ModelsDir -Filter "*.gguf" | ForEach-Object {
        $dest = Join-Path $ModelsStage $_.Name
        try { New-Item -ItemType HardLink -Path $dest -Target $_.FullName -ErrorAction Stop | Out-Null }
        catch { Copy-Item $_.FullName $dest }
    }
}

# ── 5. installer ────────────────────────────────────────────────────────────
$size = (Get-ChildItem $Stage -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
Step ("staged {0:N0} MB" -f $size)
if ($Installer) {
    $iscc = @("$env:ProgramFiles\Inno Setup 7\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
              "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 or 7 not found; install it from https://jrsoftware.org/isinfo.php or drop -Installer" }
    New-Item -ItemType Directory -Force (Join-Path $Pkg "dist") | Out-Null
    Step "compiling the installer"
    # ISCC prints warnings on stderr; under $ErrorActionPreference = "Stop" PowerShell
    # would abort on the first one, so judge the compile by its exit code alone.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $defs = @("/DVersion=$Version", "/DStage=$Stage", "/DDist=$(Join-Path $Pkg 'dist')")
    if ($Lite) { $defs += "/DLite" }
    & $iscc @defs (Join-Path $Pkg "CAD-Agent.iss") 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "ISCC failed ($code)" }
    Get-ChildItem (Join-Path $Pkg "dist") -Filter "*.exe" | ForEach-Object { Step ("installer: {0} ({1:N0} MB)" -f $_.FullName, ($_.Length / 1MB)) }
}
Step "done"
