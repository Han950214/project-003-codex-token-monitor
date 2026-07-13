#ifndef AppVersion
  #error AppVersion must be provided by scripts/build_installer.ps1
#endif
#ifndef PortableSourceDir
  #error PortableSourceDir must be provided by scripts/build_installer.ps1
#endif

#define AppName "Codex Token Monitor"
#define AppExeName "CodexTokenMonitor.exe"
#define ProjectUrl "https://github.com/Han950214/project-003-codex-token-monitor"

[Setup]
AppId={{C01E34C2-5C0B-4E19-A52B-84D2B3FF2E6E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Codex Token Monitor Project
AppPublisherURL={#ProjectUrl}
AppSupportURL={#ProjectUrl}
AppUpdatesURL={#ProjectUrl}
DefaultDirName={localappdata}\Programs\CodexTokenMonitor
DefaultGroupName=Codex Token Monitor
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=CodexTokenMonitor-Setup-{#AppVersion}
SetupIconFile=..\resources\app-icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
AppMutex=Local\CodexTokenMonitor.SingleInstance
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式 / Create a desktop shortcut"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PortableSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Codex Token Monitor"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Codex Token Monitor"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,Codex Token Monitor}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU64; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "CodexTokenMonitor"; Flags: uninsdeletevalue
