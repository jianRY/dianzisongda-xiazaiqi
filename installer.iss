; ============================================================================
;  法院文书下载器 · 安装版脚本（Inno Setup 6）
; ============================================================================
;  用途：把 dist\法院文书下载器.exe（绿色单文件版）打成一个可安装到系统的
;        安装包（开始菜单 / 桌面快捷方式 / 系统「已安装的应用」里可卸载）。
;
;  编译（由 .pybuild_cache\release_all.py 自动调用，也可手动跑）：
;      ISCC.exe installer.iss /DAppVersion=1.8
;
;  ⚠️ 为什么固定「仅为当前用户安装」（PrivilegesRequired=lowest）：
;     程序内置就地自动更新 —— 下载新版后要往「程序所在目录」写文件。
;     若装到 C:\Program Files（需管理员，目录只读），自动更新必然失败。
;     装到 %LOCALAPPDATA%\Programs 则始终可写、且全程不需要 UAC 提权。
;     如需全机安装，把 PrivilegesRequired 改为 admin 并提醒用户更新需以管理员运行。
;
;  注意：本文件必须保存为「带 BOM 的 UTF-8」，否则 Inno 按 ANSI 解析会乱码。
; ============================================================================

#ifndef AppVersion
  #define AppVersion "1.8"
#endif
#ifndef SrcExe
  #define SrcExe "dist\法院文书下载器.exe"
#endif

#define AppName "法院文书下载器"
#define AppPublisher "jianRY"
#define AppURL "https://github.com/jianRY/dianzisongda-xiazaiqi"

[Setup]
; AppId 一旦发布就不能改，否则新版本会被当成另一个软件、无法覆盖升级
AppId={{8F3A1C42-7B6E-4D9A-9E21-5C4B7A2D6F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序 v{#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes
UsePreviousAppDir=yes

; 仅当前用户安装：不需要管理员，程序目录可写，自动更新才能工作
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=

OutputDir=dist
OutputBaseFilename={#AppName}_安装版_v{#AppVersion}
SetupIconFile=assets\app.ico
WizardStyle=modern
WizardImageFile=installer\wizard_image.bmp
WizardSmallImageFile=installer\wizard_small.bmp

Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4

UninstallDisplayIcon={app}\{#AppName}.exe
UninstallDisplayName={#AppName} v{#AppVersion}

MinVersion=6.1sp1
CloseApplications=yes
RestartApplications=no
AllowUNCPath=no
SetupLogging=yes
ShowLanguageDialog=no

[Languages]
Name: "chs"; MessagesFile: "installer\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#SrcExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\app.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; WorkingDir: "{app}"
Name: "{group}\使用说明与更新记录"; Filename: "{app}\README.md"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 就地自动更新可能留下 旧版_时间戳.exe / 更新中.exe 之类的中间文件，卸载时一并清掉
Type: files; Name: "{app}\*_旧版.exe"
Type: files; Name: "{app}\*_旧版_*.exe"
Type: files; Name: "{app}\*_更新中.exe"
Type: files; Name: "{app}\*_更新中_*.exe"
