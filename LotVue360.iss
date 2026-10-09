; Native per-user, one-click Windows installer compiled by the Desktop releases workflow.
; The installer payload is the verified frozen package under build\\desktop\\dist\\LotVue360.
#define MyAppName "LotVue360"
#define MyAppVersion "2.1.0"
#define MyAppPublisher "LotVue360"
#define MyAppExeName "LotVue360.exe"

[Setup]
; Keep this stable between updates so Windows recognizes the same application.
AppId={{A4B981C2-2A8C-4E63-9E3B-360054D9AD80}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\LotVue360
DefaultGroupName=LotVue360
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=dist\desktop-release
OutputBaseFilename=LotVue360-OneClick-windows-x64
SetupIconFile=build\desktop\dist\LotVue360\app\static\desktop\lotvue.ico
UninstallDisplayIcon={app}\LotVue360.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=LotVue360.exe
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "build\desktop\dist\LotVue360\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\LotVue360"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\LotVue360"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch LotVue360 now"; Flags: nowait postinstall skipifsilent
