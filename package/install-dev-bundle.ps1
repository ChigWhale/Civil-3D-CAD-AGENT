<#
.SYNOPSIS
  Installs the plugin from this checkout as an Autodesk bundle, so Civil 3D
  loads it (and the CAD-AGENT ribbon tab) at startup with no NETLOAD.

.DESCRIPTION
  Creates %APPDATA%\Autodesk\ApplicationPlugins\CAD-Agent.bundle with:
    PackageContents.xml       from package\bundle\PackageContents.xml (version "dev")
    Contents\*.dll            copied from bin\<Config>\net10.0-windows
    Contents\install.json     program_root = this repo, python = the python on PATH,
                              launcher = launcher.py, autostart as requested
  After this, every build of CADAgent.csproj copies its output into the bundle
  (the SyncDevBundle target), so the next Civil 3D start runs the newest DLL.
  The same layout is what the installer ships; only the paths differ.

  Civil 3D must be restarted to pick up a new DLL, and the copy fails (harmlessly)
  while a Civil 3D that already loaded the bundle is open. Stop NETLOADing from
  bin\ once this is installed: loading the assembly twice errors.

.EXAMPLE
  .\package\install-dev-bundle.ps1                 # Debug build, autostart on
  .\package\install-dev-bundle.ps1 -Config Release -NoAutostart
  .\package\install-dev-bundle.ps1 -Uninstall
#>
param(
    [ValidateSet("Debug", "Release")] [string]$Config = "Debug",
    [switch]$NoAutostart,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Bundle = Join-Path $env:APPDATA "Autodesk\ApplicationPlugins\CAD-Agent.bundle"
$Contents = Join-Path $Bundle "Contents"

if ($Uninstall) {
    if (Test-Path $Bundle) { Remove-Item -Recurse -Force $Bundle; Write-Host "removed $Bundle" } else { Write-Host "no bundle at $Bundle" }
    exit 0
}

$Bin = Join-Path $Root "bin\$Config\net10.0-windows"
if (-not (Test-Path (Join-Path $Bin "CADAgent.dll"))) { throw "no $Config build at $Bin; build CADAgent.csproj first" }

New-Item -ItemType Directory -Force $Contents | Out-Null
$manifest = Get-Content (Join-Path $PSScriptRoot "bundle\PackageContents.xml") -Raw
$manifest = $manifest.Replace("{{VERSION}}", "dev").Replace("{{SERIESMIN}}", "R25.0").Replace("{{SERIESMAX}}", "R26.0")
Set-Content -Path (Join-Path $Bundle "PackageContents.xml") -Value $manifest -Encoding utf8

$copied = 0
foreach ($f in Get-ChildItem $Bin -Include "*.dll", "*.deps.json", "*.pdb" -Recurse) {
    try { Copy-Item $f.FullName $Contents -Force; $copied++ }
    catch { Write-Warning "could not copy $($f.Name): $($_.Exception.Message) (Civil 3D holding the old one? restart it and rerun)" }
}

$python = $null
try { $python = (Get-Command python -ErrorAction Stop).Source } catch { }
if (-not $python) { Write-Warning "no python on PATH; install.json will not name one (HARVEST and the ribbon's Start Dashboard need it)" }
$pythonw = $null
if ($python) { $pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"; if (-not (Test-Path $pythonw)) { $pythonw = $python } }
$install = [ordered]@{
    program_root = $Root
    python       = $python
    pythonw      = $pythonw
    launcher     = (Join-Path $Root "launcher.py")
    autostart    = (-not $NoAutostart)
    version      = "dev"
}
($install | ConvertTo-Json) | Set-Content -Path (Join-Path $Contents "install.json") -Encoding utf8

Write-Host "installed $Bundle ($copied files from $Bin)"
Write-Host "install.json: python=$python autostart=$(-not $NoAutostart)"
Write-Host "Restart Civil 3D: the CAD-AGENT ribbon tab loads on startup. Do not NETLOAD from bin\ any more."
