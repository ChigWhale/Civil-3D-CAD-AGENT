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
# SeriesMin is the LOWEST release that may load the bundle. A value above the
# installed release makes Civil 3D skip the bundle without a word, so it is
# read from the acad.exe on this machine (2025 = R25.0, 2026 = R25.1) and
# never set higher than the oldest one found.
$seriesMin = "R25.0"
$found = @()
foreach ($dir in Get-ChildItem "$env:ProgramFiles\Autodesk" -Directory -Filter "AutoCAD 20*" -ErrorAction SilentlyContinue) {
    $exe = Join-Path $dir.FullName "acad.exe"
    if (Test-Path $exe) {
        $v = (Get-Item $exe).VersionInfo.ProductVersion      # e.g. R25.0.189.0.0
        if ($v -match "^R(\d+)\.(\d+)") { $found += [pscustomobject]@{ Name = $dir.Name; Series = "R$($Matches[1]).$($Matches[2])"; Key = [int]$Matches[1] * 100 + [int]$Matches[2] } }
    }
}
if ($found) {
    $lowest = $found | Sort-Object Key | Select-Object -First 1
    if ($lowest.Key -lt 2500) { Write-Warning "$($lowest.Name) ($($lowest.Series)) predates the .NET 10 update; the plugin will not load in it" }
    else { $seriesMin = $lowest.Series }
    Write-Host ("installed: " + (($found | ForEach-Object { "$($_.Name) = $($_.Series)" }) -join ", ") + "; SeriesMin = $seriesMin")
}
$manifest = Get-Content (Join-Path $PSScriptRoot "bundle\PackageContents.xml") -Raw
$manifest = $manifest.Replace("{{VERSION}}", "dev").Replace("{{SERIESMIN}}", $seriesMin).Replace("{{SERIESMAX}}", "R26.0")
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
