#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef BundleDir
  #define BundleDir "..\output\release-build\dist\GeoSorter"
#endif
[Setup]
AppId={{F55D1D49-832E-4F3B-93A8-E12E149C8168}
AppName=GeoSorter
AppVersion={#AppVersion}
AppPublisher=GeoSorter
AppPublisherURL=https://github.com/hmallen/geosorter
DefaultDirName={localappdata}\Programs\GeoSorter
DefaultGroupName=GeoSorter
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\output\releases
OutputBaseFilename=GeoSorter-{#AppVersion}-windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\GeoSorter.exe
CloseApplications=no
RestartApplications=no
LicenseFile=..\LICENSE

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\GeoSorter"; Filename: "{app}\GeoSorter.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\GeoSorter"; Filename: "{app}\GeoSorter.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\GeoSorter.exe"; Description: "Open GeoSorter"; Flags: nowait postinstall skipifsilent

[Code]
function ApplicationRunning: Boolean;
var Locator, Services, Processes, Process: Variant;
    I: Integer;
    ExecutablePath: String;
begin
  Result := False;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Services.ExecQuery('SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE Name = ''GeoSorter.exe''');
    for I := 0 to Processes.Count - 1 do
    begin
      Process := Processes.ItemIndex(I);
      { A pip-installed CLI also uses geosorter.exe. Only folder-bundle desktop
        processes hold these binaries and share the desktop lifecycle. }
      if not VarIsNull(Process.ExecutablePath) then
      begin
        ExecutablePath := Process.ExecutablePath;
        if DirExists(ExtractFileDir(ExecutablePath) + '\_internal') then
          Result := True;
      end;
    end;
  except
    { Fail closed if process inspection fails; do not replace running binaries. }
    Log('GeoSorter process inspection failed: ' + GetExceptionMessage);
    Result := True;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if ApplicationRunning then
    Result := 'Close GeoSorter using Settings & Help or its tray icon, wait for work to finish, then retry.';
end;

function InitializeUninstall: Boolean;
begin
  Result := not ApplicationRunning;
  if not Result then
    MsgBox('Close GeoSorter using Settings & Help or its tray icon before uninstalling. Your library and settings will be kept.', mbInformation, MB_OK);
end;
