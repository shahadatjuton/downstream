; Inno Setup 6 script for IDM Clone.
; Build with scripts\build-installer.ps1 (or ISCC.exe /DAppVersion=x.y.z packaging\installer.iss).
; Installs per-user (no admin prompt), registers the browser native messaging host,
; and shows up in Apps & Features with a working uninstaller.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "IDM Clone"
#define AppExe "IDM Clone.exe"
#define HostName "com.idmclone.host"
#define SourceDir "..\dist\IDM Clone"

[Setup]
AppId={{6C1F2E0A-4B7D-4E4B-9C3A-1D2E3F4A5B6C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=IDM Clone open-source project
AppPublisherURL=https://github.com/shahadatjuton/idm-clone
AppSupportURL=https://github.com/shahadatjuton/idm-clone/issues
AppUpdatesURL=https://github.com/shahadatjuton/idm-clone/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\release
OutputBaseFilename=IDM-Clone-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start {#AppName} when I sign in to Windows (minimised to the tray)"; GroupDescription: "Startup:"; Flags: unchecked
Name: "browser"; Description: "Register the browser extension host for Chrome, Edge and Brave"; GroupDescription: "Browser integration:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\idm-host.exe"; DestDir: "{app}\host"; Flags: ignoreversion
Source: "..\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Start with Windows (minimised)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --minimized"; Flags: uninsdeletevalue; Tasks: startup
; Native messaging host registration (per browser)
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\{#HostName}"; ValueType: string; ValueData: "{app}\host\{#HostName}.json"; Flags: uninsdeletekey; Tasks: browser
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\{#HostName}"; ValueType: string; ValueData: "{app}\host\{#HostName}.json"; Flags: uninsdeletekey; Tasks: browser
Root: HKCU; Subkey: "Software\Chromium\NativeMessagingHosts\{#HostName}"; ValueType: string; ValueData: "{app}\host\{#HostName}.json"; Flags: uninsdeletekey; Tasks: browser
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{#HostName}"; ValueType: string; ValueData: "{app}\host\{#HostName}.json"; Flags: uninsdeletekey; Tasks: browser

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\host"
Type: filesandordirs; Name: "{app}\extension"

[Code]
function ExtensionId(): String;
var
  Json, Marker: String;
  P: Integer;
begin
  // The pinned extension id lives in extension\.extension-key.json as "id": "...."
  Result := '';
  if LoadStringFromFile(ExpandConstant('{app}\extension\.extension-key.json'), Json) then
  begin
    Marker := '"id": "';
    P := Pos(Marker, Json);
    if P > 0 then
    begin
      Json := Copy(Json, P + Length(Marker), 64);
      P := Pos('"', Json);
      if P > 0 then
        Result := Copy(Json, 1, P - 1);
    end;
  end;
end;

procedure WriteHostManifest();
var
  Manifest, HostExe: String;
begin
  HostExe := ExpandConstant('{app}\host\idm-host.exe');
  StringChangeEx(HostExe, '\', '\\', True);
  Manifest :=
    '{' + #13#10 +
    '  "name": "{#HostName}",' + #13#10 +
    '  "description": "IDM Clone native messaging host",' + #13#10 +
    '  "path": "' + HostExe + '",' + #13#10 +
    '  "type": "stdio",' + #13#10 +
    '  "allowed_origins": ["chrome-extension://' + ExtensionId() + '/"]' + #13#10 +
    '}' + #13#10;
  SaveStringToFile(ExpandConstant('{app}\host\{#HostName}.json'), Manifest, False);
end;

procedure WriteAppPath();
var
  Dir: String;
begin
  // Lets the browser host start the app on demand.
  Dir := ExpandConstant('{userappdata}\idm-clone');
  ForceDirectories(Dir);
  SaveStringToFile(Dir + '\app_path.txt', '"' + ExpandConstant('{app}\{#AppExe}') + '"', False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WriteAppPath();
    if WizardIsTaskSelected('browser') then
      WriteHostManifest();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := ExpandConstant('{userappdata}\idm-clone');
    if DirExists(Dir) then
      if MsgBox('Also delete your IDM Clone settings and download history?' + #13#10 + Dir,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Dir, True, True, True);
  end;
end;
