; ─────────────────────────────────────────────────────────────
;  Portfolio Tracker — Inno Setup installer script
;  Produces: Output\PortfolioTracker-Setup.exe
;  Installs without admin rights (per-user, like Chrome)
; ─────────────────────────────────────────────────────────────

[Setup]
AppName=Portfolio Tracker
AppVersion=1.0
AppPublisher=Portfolio Tracker
AppPublisherURL=https://github.com/OsmanDeol/portfolio-tracker
AppSupportURL=https://github.com/OsmanDeol/portfolio-tracker
AppUpdatesURL=https://github.com/OsmanDeol/portfolio-tracker

; Install to user's AppData — no admin needed (same as Chrome)
DefaultDirName={localappdata}\PortfolioTracker
DefaultGroupName=Portfolio Tracker
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Output
OutputDir=Output
OutputBaseFilename=PortfolioTracker-Setup
SetupIconFile=icon.ico

; Compression
Compression=lzma2/ultra64
SolidCompression=yes

; Appearance
WizardStyle=modern
WizardSmallImageFile=
DisableWelcomePage=no
DisableDirPage=yes
DisableProgramGroupPage=yes

; Version info embedded in the setup exe
VersionInfoVersion=1.0.0.0
VersionInfoCompany=Portfolio Tracker
VersionInfoDescription=Portfolio Tracker Installer
VersionInfoProductName=Portfolio Tracker

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
; Copy everything PyInstaller built
Source: "dist\PortfolioTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu
Name: "{group}\Portfolio Tracker";                      Filename: "{app}\PortfolioTracker.exe"; IconFilename: "{app}\PortfolioTracker.exe"
Name: "{group}\Uninstall Portfolio Tracker";            Filename: "{uninstallexe}"

; Desktop (if task selected)
Name: "{autodesktop}\Portfolio Tracker"; Filename: "{app}\PortfolioTracker.exe"; IconFilename: "{app}\PortfolioTracker.exe"; Tasks: desktopicon

[Registry]
; Register in Add/Remove Programs properly
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\PortfolioTracker"; ValueType: string; ValueName: "DisplayName";          ValueData: "Portfolio Tracker"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\PortfolioTracker"; ValueType: string; ValueName: "DisplayVersion";       ValueData: "1.0"
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\PortfolioTracker"; ValueType: string; ValueName: "Publisher";            ValueData: "Portfolio Tracker"
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\PortfolioTracker"; ValueType: string; ValueName: "DisplayIcon";          ValueData: "{app}\PortfolioTracker.exe,0"
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\PortfolioTracker"; ValueType: string; ValueName: "InstallLocation";      ValueData: "{app}"
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\PortfolioTracker"; ValueType: string; ValueName: "UninstallString";      ValueData: "{uninstallexe}"
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\PortfolioTracker"; ValueType: string; ValueName: "URLInfoAbout";         ValueData: "https://github.com/OsmanDeol/portfolio-tracker"

[Run]
; Offer to launch the app right after installing
Filename: "{app}\PortfolioTracker.exe"; Description: "Launch Portfolio Tracker now"; Flags: nowait postinstall skipifsilent
