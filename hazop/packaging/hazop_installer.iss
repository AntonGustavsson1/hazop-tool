; Inno Setup script for the HAZOP Tool (2026-08-21, see NOTES.md "Paketera
; HAZOP-appen som en installationsfil"). Wraps the PyInstaller onedir
; output (dist/HazopTool/) into a single HazopSetup.exe.
;
; Build with (after `pyinstaller hazop.spec` has produced dist/HazopTool/):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\hazop_installer.iss
; Output: packaging\output\HazopSetup.exe
;
; Installs PER-USER to {localappdata}\ProSa\HAZOP Tool -- deliberately NOT
; Program Files. The app writes its own data (hazop_project.db, crashes/,
; hazop_backups/, hazop_crash.log) next to the installed exe (see
; constants.py's _app_dir()) -- Program Files is normally read-only for
; non-admin users, which would silently break that. Per-user install needs
; no UAC elevation and gives each Windows account its own private project
; data, which matches how this exact app already behaves unpackaged today.

#define MyAppName "HAZOP Tool"
#define MyAppPublisher "ProSa Process Safety Consulting AB"
#define MyAppExeName "HazopTool.exe"
#define MyAppAssocExt ".hzp"
#define MyAppAssocKey "ProSa.HAZOPTool.Project"

[Setup]
; Fixed, never-reused GUID -- lets a future installer build upgrade this
; install in place (same install dir, shortcuts, file association)
; instead of registering as a separate side-by-side app.
AppId={{5E6B4F6E-6C6E-4A0B-9B77-2B0F6B0C6D5E}
AppName={#MyAppName}
AppVersion=1.0.0
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\ProSa\HAZOP Tool
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=HazopSetup
SetupIconFile=app_icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ChangesAssociations=yes

[Languages]
Name: "swedish"; MessagesFile: "compiler:Languages\Swedish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "fileassoc"; Description: "Öppna .hzp-projektfiler med {#MyAppName}"; GroupDescription: "Filkoppling:"

; The PyInstaller onedir bundle (HazopTool.exe + its _internal/ folder of
; DLLs/data) -- built separately via `pyinstaller hazop.spec` before this
; script runs. Path is relative to this .iss file, one level up.
[Files]
Source: "..\dist\HazopTool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; .hzp file association, scoped to the current user (HKCU, not HKLM --
; matches the per-user, no-admin install above). Only written if the user
; kept the "fileassoc" task checked.
Root: HKCU; Subkey: "Software\Classes\{#MyAppAssocExt}"; ValueType: string; ValueName: ""; ValueData: "{#MyAppAssocKey}"; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKCU; Subkey: "Software\Classes\{#MyAppAssocKey}"; ValueType: string; ValueName: ""; ValueData: "HAZOP-projekt"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKCU; Subkey: "Software\Classes\{#MyAppAssocKey}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: fileassoc
Root: HKCU; Subkey: "Software\Classes\{#MyAppAssocKey}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: fileassoc

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
