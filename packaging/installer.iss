; Inno Setup 安装包脚本。先用 PyInstaller 生成 dist\AI盯播助手\，再在仓库根目录编译：
;   iscc /DAppVersion=<版本号> packaging\installer.iss
; 输出 dist\VoiceDetect-<版本>-windows-setup.exe（文件名不用中文：GitHub 发布时会把中文去掉）。
; 按当前用户安装，不需要管理员权限，装在 %LOCALAPPDATA%\Programs 下。
; 卸载只删程序本身；场次记录、录音、识别模型和设置在 %LOCALAPPDATA%\voicedetect，卸载后还在。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "AI 盯播助手"
#define AppExe "VoiceDetect.exe"

[Setup]
AppId={{6F0B1C7E-5A51-4B7B-9C2E-1D3F7A9B2C41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\AI盯播助手
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=VoiceDetect-{#AppVersion}-windows-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
; Inno Setup 6.7.1（GitHub 的 Windows 机器上装的版本）还没有自带简体中文，这是同一版本源码里
; Files/Languages/Unofficial/ChineseSimplified.isl 的原样副本
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面上创建快捷方式"; GroupDescription: "快捷方式："

[InstallDelete]
; 升级时先删掉旧版本的程序文件，免得留下新版本已经不用的库
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\AI盯播助手\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "现在打开 {#AppName}"; Flags: nowait postinstall skipifsilent
