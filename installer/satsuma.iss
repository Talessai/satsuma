; Inno Setup script: builds dist\Satsuma-Setup.exe from dist\Satsuma\.
;   ISCC.exe /DAppVersion=0.1.0 installer\satsuma.iss
; Per-user install (no admin rights) into a short path, which avoids the
; Windows 260-character path limit that breaks the bundled Python/Qt DLLs.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppName "Satsuma"
#define AppPublisher "Talal Alawi"
#define AppExe "Satsuma.exe"

[Setup]
AppId={{6F3E2A9C-58B1-4C7E-9A0D-2B7C1E5F4A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppCopyright=© 2026 {#AppPublisher}. AGPL-3.0-or-later
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} installer
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Satsuma-Setup
SetupIconFile=..\satsuma\resources\satsuma.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ar"; MessagesFile: "compiler:Languages\Arabic.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
en.StartWithWindows=Start {#AppName} when I sign in to Windows
ar.StartWithWindows=تشغيل {#AppName} عند تسجيل الدخول إلى Windows
es.StartWithWindows=Iniciar {#AppName} al iniciar sesión en Windows
en.AddSendTo=Add {#AppName} to the "Send to" menu
ar.AddSendTo=إضافة {#AppName} إلى قائمة "إرسال إلى"
es.AddSendTo=Agregar {#AppName} al menú "Enviar a"
en.LaunchNow=Start {#AppName}
ar.LaunchNow=تشغيل {#AppName}
es.LaunchNow=Iniciar {#AppName}

[Tasks]
Name: "autostart"; Description: "{cm:StartWithWindows}"
Name: "sendto"; Description: "{cm:AddSendTo}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\dist\Satsuma\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{usersendto}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: sendto

[Registry]
; Same value the app's own "Start with Windows" setting uses, so either can turn it off.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[Code]
procedure StopRunningApp();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopRunningApp();
    { Entries the app may have created itself from Settings. Settings and the log in
      %APPDATA%\Satsuma are kept, as Windows apps usually do. }
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Classes\*\shell\{#AppName}');
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Classes\Directory\shell\{#AppName}');
    DeleteFile(ExpandConstant('{usersendto}\{#AppName}.lnk'));
  end;
end;
