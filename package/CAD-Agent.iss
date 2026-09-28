; Inno Setup 6 script for CAD-Agent. Compiled by package\build.ps1 -Installer with
;   ISCC /DVersion=1.0.0 /DStage=<stage folder> /DDist=<output folder> CAD-Agent.iss
; Per-user install, no admin: the program goes under %LOCALAPPDATA%\Programs, the
; plugin bundle under %APPDATA%\Autodesk\ApplicationPlugins (Civil 3D autoloads it),
; and everything written at run time under %LOCALAPPDATA%\CAD-Agent.

#ifndef Version
  #define Version "0.1.0"
#endif
#ifndef Stage
  #define Stage "stage"
#endif
#ifndef Dist
  #define Dist "dist"
#endif

[Setup]
AppId={{B7E1C4A2-9D3F-4C6B-8E5A-CADA6E2026C3}
AppName=CAD-Agent
AppVersion={#Version}
AppPublisher=Nick Chiglo
DefaultDirName={localappdata}\Programs\CAD-Agent
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#Dist}
OutputBaseFilename=CAD-Agent-Setup-{#Version}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
DiskSpanning=no
UninstallDisplayName=CAD-Agent {#Version}
WizardStyle=modern
CloseApplications=no

[Tasks]
Name: "fetchmodels"; Description: "Download the language and vision models now (about 9 GB; can be done later from the dashboard)"; Flags: unchecked

[Files]
Source: "{#Stage}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs ignoreversion
Source: "{#Stage}\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs ignoreversion
Source: "{#Stage}\models\*"; DestDir: "{app}\models"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist
Source: "{#Stage}\version.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\cad-agent.template.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\bundle\CAD-Agent.bundle\*"; DestDir: "{userappdata}\Autodesk\ApplicationPlugins\CAD-Agent.bundle"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\CAD-Agent Dashboard"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"" open"; WorkingDir: "{app}\app"; Comment: "Open the CAD-Agent dashboard"
Name: "{autoprograms}\CAD-Agent Stop Services"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"" stop"; WorkingDir: "{app}\app"

[Run]
; writes cad-agent.json and the bundle's install.json with this machine's paths
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\launcher.py"" install --bundle ""{userappdata}\Autodesk\ApplicationPlugins\CAD-Agent.bundle"""; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; StatusMsg: "Writing the configuration..."
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\launcher.py"" fetch-models"; WorkingDir: "{app}\app"; Flags: nowait postinstall skipifsilent; Tasks: fetchmodels; Description: "Download the models"
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"" open"; WorkingDir: "{app}\app"; Flags: nowait postinstall skipifsilent unchecked; Description: "Open the dashboard now"

[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\launcher.py"" stop"; WorkingDir: "{app}\app"; Flags: runhidden; RunOnceId: "stopservices"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"
Type: files; Name: "{app}\cad-agent.json"
; user data under {localappdata}\CAD-Agent is kept on purpose (reviews, exemplars, learned files)

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
  if not DirExists(ExpandConstant('{userappdata}\Autodesk')) then
    if MsgBox('No Autodesk profile was found for this user. Civil 3D must be installed and started once before the plugin can load. Continue anyway?', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
end;
