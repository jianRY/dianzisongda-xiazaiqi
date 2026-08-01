# 电子送达下载器（法院文书下载器）

全国法院统一送达平台电子送达文书自动下载工具。粘贴送达短信或链接，自动提取该案件全部文书并下载为 PDF，支持批量下载、转 JPG、防合并命名。

## 功能特性（v1.0）

- 自动识别 `zxfw.court.gov.cn` 送达链接（支持整段短信 / 一次粘贴多个链接批量下载）
- 免登录调用文书清单接口，逐一下载该案件全部 PDF
- 图形界面（Tkinter）：粘贴 / 开始下载 / 实时进度条 / 下载日志
- 保存路径可选、可手填，并记住上次选择
- 下载完成后打开文件夹（三档：不打开 / 打开根目录 / 打开每个案件文件夹）
- 粘贴按钮 + 文本框右键菜单 + 置灰占位符（无需先删举例即可粘贴）
- 可勾选「下载后自动转为 JPG」（长边 2000px，高质量；两种目录模式：统一图夹 / 按文件名分夹）
- 案件文件夹命名：`法院名_案号_启动时间戳`，避免同名法院不同链接合并

## 使用方法

### 直接用（推荐）
下载仓库 Release 中的 `法院文书下载器.exe`，双击运行，把送达短信粘贴进窗口即可。

### 源码运行
依赖：Python 3、tkinter（系统 Python 自带）、PyMuPDF（仅转 JPG 时需要）

```bash
pip install pymupdf
python court_doc_downloader_gui.py
```

### 自行打包为单文件 exe
```bash
pip install pyinstaller pymupdf
pyinstaller --onefile --windowed --noupx --hidden-import=fitz --name CourtDocDownloader court_doc_downloader_gui.py
```
产物 `dist/CourtDocDownloader.exe` 重命名为 `法院文书下载器.exe` 即可。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `court_doc_downloader_gui.py` | 图形界面主程序（v1.0） |
| `court_doc_downloader.py` | 命令行版（零依赖，仅标准库 urllib） |
| `CourtDocDownloader.spec` | PyInstaller 打包配置 |
| `CHANGELOG.md` | 版本更新记录 |

## 技术说明

- 平台：全国法院统一送达平台 `zxfw.court.gov.cn`
- 文书清单接口免登录：`POST .../getWsListBySdbhNew`（参数 qdbh/sdbh/sdsin）
- OSS 签名链接有效期极短，运行时现取现下；签名绑定 HTTP 方法，只用 GET
- 配置持久化：`%APPDATA%\法院文书下载器\config.json`

## 免责声明

本工具仅用于合法、经授权的文书接收与存档。请遵守法院平台的使用规定，勿用于任何违规用途。
