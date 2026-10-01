#define AppName "AI Device Bridge"
#define AppVersion "0.5.0rc1"
#define AppPublisher "AI Device Bridge"
#define AppExeName "AI Device Bridge.exe"
#define ProjectRoot AddBackslash(SourcePath) + ".."

[Setup]
AppId={{D86C3A76-4BF8-4E2F-A021-CEAA9FA5A570}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
SourceDir={#ProjectRoot}
OutputDir={#ProjectRoot}\release
DefaultDirName={localappdata}\Programs\AI Device Bridge
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputBaseFilename=AI-Device-Bridge-Setup
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "build\release-dist\AI Device Bridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: postinstall nowait skipifsilent
