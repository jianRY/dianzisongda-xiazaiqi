# 电子送达下载器（法院文书下载器）

> **项目仓库：https://github.com/jianRY/dianzisongda-xiazaiqi**
> （新版发布、历史版本下载、问题反馈都在这里；两种版本：绿色单文件版 + 系统安装版，功能完全一致）
>
> **国内直连下载（推荐，速度快）：http://47.116.64.26:8888/**
> 两个版本都同步在国内服务器上，不用访问 GitHub；软件自带的自动更新也优先走这台服务器。

全国法院统一送达平台电子送达文书自动下载工具。粘贴送达短信或链接，自动提取该案件全部文书并下载为 PDF，支持**多线程并发下载**、批量、转 JPG、防合并命名、自动更新（含更新内容展示 / 三选项 / 进度速度 / 随时取消）。

## 功能特性（v1.8）

- 自动识别 `zxfw.court.gov.cn` 送达链接（支持整段短信 / 一次粘贴多个链接批量下载）
- **准确提取标准案号**（如 `(2025)苏0505民初7780号`），批量多案件时按位置就近配对，不会张冠李戴
- 免登录调用文书清单接口，**同一案件多份文书用 4 线程并发下载**，速度更快
- 图形界面（Tkinter）：粘贴 / 开始下载 / 实时进度条（份数+百分比） / 下载日志
- **专属应用图标**（深蓝底金天平）：exe 文件图标、窗口标题栏、任务栏图标统一，不再显示 Python 默认图标
- 下载中按钮自动切换为「取消下载」，可中途取消未完成任务（已完成文件保留）
- **下载完整性校验**：比对 Content-Length，截断的坏文件自动重试；并拦截服务端返回的错误页
- **支持非 PDF 文书**：jpg / docx 等格式不再被文件头校验误判为失败
- **跳过已存在的同名文书**（默认开启）：识别到案号时复用同一案件上次的文件夹，已下过的文书真正跳过
- **自动更新**（`autoupdate.py` 通用模块）：启动检查更新 → 弹窗显示本次更新内容 → 立即更新 / 本次忽略 / 以后不再提醒 → 下载显示进度与速度、可随时取消 → **新版就地放进程序目录、自动重启并清理旧版文件**（程序文件名保持不变，快捷方式继续有效）
- 保存路径可选、可手填，开始下载前自动校验可写性，并记住上次选择
- 下载完成后打开文件夹（三档：不打开 / 打开根目录 / 打开每个案件文件夹）
- 下载中关闭窗口会先确认，未完成文件自动清理
- 粘贴按钮 + 文本框右键菜单 + 置灰占位符（无需先删举例即可粘贴）
- 可勾选「下载后自动转为 JPG」（长边 2000px，高质量；两种目录模式：统一图夹 / 按文件名分夹）
- 案件文件夹命名：`法院名_案号_启动时间戳`，避免同名法院不同链接合并
- 日志带时间戳，便于回溯

## 使用方法

### 两种版本，按喜好任选

| 版本 | 文件名 | 特点 |
| --- | --- | --- |
| **安装版** | `法院文书下载器_安装版_vX.Y.exe` | 双击安装，自动创建开始菜单 + 桌面快捷方式，可在系统「已安装的应用」里卸载。**仅为当前用户安装，不需要管理员权限** |
| **绿色版** | `法院文书下载器.exe` | 免安装单文件，放哪都能跑，拷到 U 盘也行 |

两种版本功能完全一致，且都支持内置自动更新。程序会自动记住你的所有设置，换版本也不会丢。

> ⚠️ 绿色版请放在**有写入权限**的目录（桌面、文档等）。放在 `C:\Program Files` 这类只读目录里，自动更新会因无写入权限而失败。

### 源码运行
依赖：Python 3、tkinter（系统 Python 自带）、PyMuPDF（仅转 JPG 时需要）

```bash
pip install pymupdf
python court_doc_downloader_gui.py
```

### 自行打包

**单文件 exe**：
```bash
pip install pyinstaller pymupdf
pyinstaller --onefile --windowed --noupx --hidden-import=fitz \
  --icon assets/app.ico --add-data "assets/app.ico;assets" \
  --name CourtDocDownloader court_doc_downloader_gui.py
```
产物 `dist/CourtDocDownloader.exe` 重命名为 `法院文书下载器.exe` 即可。

**安装包**（需先装 [Inno Setup 6](https://jrsoftware.org/isdl.php)）：
```bash
python _project_upload/make_installer_assets.py     # 生成中文语言包与向导配图
ISCC.exe installer.iss /DAppVersion=1.8
```

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `court_doc_downloader_gui.py` | 图形界面主程序（v1.8） |
| `autoupdate.py` | 通用自动更新模块（三行接入，可复用到其他 Tkinter 软件） |
| `court_doc_downloader.py` | 命令行版（零依赖，仅标准库 urllib） |
| `installer.iss` | Inno Setup 安装包脚本（生成安装版） |
| `installer/` | 安装包资源：简体中文语言包 + 向导配图 |
| `CourtDocDownloader.spec` | PyInstaller 打包配置 |
| `assets/app.ico` | 应用图标（16~256 共 7 种尺寸） |
| `CHANGELOG.md` | 版本更新记录 |

## 技术说明

- 平台：全国法院统一送达平台 `zxfw.court.gov.cn`
- 文书清单接口免登录：`POST .../getWsListBySdbhNew`（参数 qdbh/sdbh/sdsin）
- OSS 签名链接有效期极短，运行时现取现下；签名绑定 HTTP 方法，只用 GET
- 同一案件清单接口一次拿到 N 份 OSS 签名 URL，文件之间互相独立，用 `concurrent.futures.ThreadPoolExecutor` 并发下载；案件之间串行，避免跨案件 OSS 链接过期
- 配置持久化：`%APPDATA%\法院文书下载器\config.json`

## 免责声明

本工具仅用于合法、经授权的文书接收与存档。请遵守法院平台的使用规定，勿用于任何违规用途。
