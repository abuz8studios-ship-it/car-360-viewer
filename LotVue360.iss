; Inno Setup script — builds a distributable LotVue360-Setup.exe.
; Compile with: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" LotVue360.iss
; (Install Inno Setup 6 first: winget install JRSoftware.InnoSetup)
;
; This packages the PyInstaller output (dist\LotVue360) into a one-click installer
; with Start-Menu + Desktop shortcuts, Add/Remove Programs entry, and optional
; launch-on-startup. Per-user install — no admin required.

#define MyAppName "LotVue360"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "ABUZ8 LLC"
#define MyAppExeName "LotVue360.exe"

[Setup]
AppId={{B7A3F2E1-360C-4A55-9E2F-LOTVUE360CAR}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\LotVue360
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=LotVue360-Setup-{#MyAppVersion}
SetupIconFile=dist\LotVue360\static\desktop\lotvue.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Launch LotVue360 when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\LotVue360\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\LotVue360"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\LotVue360"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "LotVue360"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: startupicon; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch LotVue360 now"; Flags: nowait postinstall skipifsilent
