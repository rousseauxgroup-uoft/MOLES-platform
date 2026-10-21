; Inno Setup script for the Windows installer, MOLES-Setup-<version>.exe.
;
; Built by .github/workflows/build-installers.yml:
;     iscc /DAppVersion=0.2.0 /DStageDir=<folder with uv.exe and the moles wheel> moles.iss
;
; The installer itself is small: it carries uv (a Python installer tool), the
; MOLES wheel and the pinned package list, and install_env.cmd uses them to
; download Python and the packages into the install folder. Installs per user
; (no administrator password) into %LOCALAPPDATA%\Programs\MOLES.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "stage"
#endif

[Setup]
; Never change AppId: it is how a new version finds and replaces the old one.
AppId={{6F1D3C52-8B7A-4E0F-9C4B-2D5E8A1F7B93}
AppName=MOLES
AppVersion={#AppVersion}
AppVerName=MOLES {#AppVersion}
AppPublisher=MOLES
AppPublisherURL=https://github.com/rousseauxgroup-uoft/MOLES-platform
AppSupportURL=https://github.com/rousseauxgroup-uoft/MOLES-platform/issues
DefaultDirName={autopf}\MOLES
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\..\LICENSE
SetupIconFile=..\assets\MOLES.ico
UninstallDisplayIcon={app}\setup\MOLES.ico
UninstallDisplayName=MOLES
OutputDir=..\..\dist
OutputBaseFilename=MOLES-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Messages]
ReadyLabel2a=Before you click Install, close every MOLES window (the launcher and any app it opened). If an experiment is running, let it finish first.%n%nClick Install to continue, or Back to change anything.
ReadyLabel2b=Before you click Install, close every MOLES window (the launcher and any app it opened). If an experiment is running, let it finish first.%n%nClick Install to continue.

[Tasks]
Name: "desktopicon"; Description: "Put a MOLES icon on the desktop"

[InstallDelete]
; An older version's wheel would otherwise sit next to the new one.
Type: files; Name: "{app}\setup\moles-*.whl"

[Files]
Source: "{#StageDir}\uv.exe"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "{#StageDir}\moles-*.whl"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "..\python-version.txt"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "install_env.cmd"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "..\assets\MOLES.ico"; DestDir: "{app}\setup"; Flags: ignoreversion

[Icons]
; pythonw.exe runs MOLES without a console window.
Name: "{autoprograms}\MOLES"; Filename: "{app}\runtime\pythonw.exe"; \
    Parameters: "-m moles"; WorkingDir: "{userdocs}"; IconFilename: "{app}\setup\MOLES.ico"; \
    Comment: "Open the MOLES launcher"
Name: "{autodesktop}\MOLES"; Filename: "{app}\runtime\pythonw.exe"; \
    Parameters: "-m moles"; WorkingDir: "{userdocs}"; IconFilename: "{app}\setup\MOLES.ico"; \
    Comment: "Open the MOLES launcher"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m moles"; WorkingDir: "{userdocs}"; \
    Description: "Open MOLES now"; Flags: postinstall nowait skipifsilent; Check: EnvironmentReady

[UninstallDelete]
; Built after installation, so the uninstaller doesn't know about them by itself.
; Settings, boards and logs in %USERPROFILE%\.moles are deliberately kept.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\python"
Type: files; Name: "{app}\install-log.txt"

[Code]
var
  EnvReady: Boolean;

function EnvironmentReady: Boolean;
begin
  Result := EnvReady;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  WizardForm.StatusLabel.Caption :=
    'Downloading Python and the packages MOLES needs. This can take several minutes...';
  WizardForm.FilenameLabel.Caption := '';
  WizardForm.ProgressGauge.Style := npbstMarquee;
  Params := '/C ""' + ExpandConstant('{app}\setup\install_env.cmd') + '" "' +
    ExpandConstant('{app}') + '" "{#AppVersion}""';
  EnvReady := Exec(ExpandConstant('{cmd}'), Params, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  WizardForm.ProgressGauge.Style := npbstNormal;
  if not EnvReady then
    SuppressibleMsgBox(
      'MOLES could not finish setting up. This is usually a missing or ' +
      'blocked internet connection, or a MOLES window that was still open.' + #13#10#13#10 +
      'Close every MOLES window, check the connection, and run this ' +
      'installer again. If it keeps failing, send this file to whoever ' +
      'maintains MOLES:' + #13#10 +
      ExpandConstant('{app}\install-log.txt'),
      mbError, MB_OK, IDOK);
end;
