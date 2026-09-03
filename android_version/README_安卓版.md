# 法院文书下载器 · 安卓版

把原来 Windows 上的「法院电子送达文书下载工具」搬到了安卓手机上，提供 **两种形态**，二选一或都留着都行：

| 形态 | 是什么 | 怎么用 | 是否需要打包 |
|------|--------|--------|--------------|
| ① 手机网页版 | 一个小后端 + 手机自适应网页 | 手机浏览器打开即用，可「添加到主屏幕」像 App | 否，直接跑 |
| ② 原生 APK | Kivy 写的安卓 App | 安装 apk，离线也能用，文件存到手机 Download | 是，需在自己电脑上用 buildozer 打包 |

核心逻辑（解析短信 → POST 法院接口取清单 → 立即 GET 下载 OSS 签名 PDF）与 Windows 版完全一致，只是**去掉了 PyMuPDF 转 JPG**（手机直接看/存 PDF 即可）。

---

## 目录结构

```
android_version/
├── court_core.py        # 共享核心逻辑（解析/取清单/下载，零依赖）
├── server.py            # ① 网页版后端（Python 标准库，零第三方依赖）
├── index.html           # ① 手机前端（移动优先、无外部依赖）
├── main.py             # ② 安卓 APK 源码（Kivy，入口必须为 main.py）
├── buildozer.spec       # ② 打包配置
├── downloads/           # ① 网页版下载落盘目录（自动创建）
└── README_安卓版.md
```

> ⚠️ 打包 APK 时建议把整个 `android_version/` 拷到一个**纯英文路径**（如 `D:\build\court-android\`），
> 避免中文/空格路径导致 buildozer 报错。

---

## 形态一：手机网页版（推荐先试这个）

### 1. 运行后端
在电脑（或你的家庭服务器）上：

```bash
cd android_version
python server.py                      # 默认 127.0.0.1:8000
python server.py --host 0.0.0.0 --port 8000   # 让同 WiFi 下的手机能访问
python server.py --no-verify          # 内网证书异常时关闭 SSL 校验
```

需要 Python 3.8+，无需安装任何第三方库。

### 2. 手机访问
- **同一 WiFi（最简单）**：手机浏览器打开 `http://<电脑局域网IP>:8000`
  （电脑 IP 用 `ipconfig` 查，例如 `192.168.1.20`）。
- **远程/长期用**：把 `server.py` 放到你已有的 HTTPS 反向代理后面
  （frp / workbuddy.link / Nginx），手机用 `https://你的域名` 访问即可，
  跨网也能用、且不依赖同一 WiFi。

### 3. 像 App 一样用
安卓 Chrome 打开页面后 → 点右上角 `⋮` → **「添加到主屏幕」**，
以后就像普通 App 一样从桌面点开。

### 4. 使用
粘贴法院送达短信 → 点「开始下载」→ 等处理完 → 每份文书有
「查看」（浏览器内联预览）和「下载」（存到手机）两个按钮，
底部还有「📦 打包下载全部（ZIP）」一次性保存整案。

---

## 形态二：原生 APK（Kivy）

### 0. 先在电脑上验证逻辑（可选）
```bash
pip install kivy
python main.py     # 弹窗界面，粘贴短信即可测试下载逻辑
```
下载会保存到 `~/Downloads/法院文书/<案件>/`。

### 1. 用 buildozer 打包（在自己电脑上执行）
buildozer 需要 Linux 环境（Ubuntu / WSL2 / 官方 Docker 镜像均可）。

**方式 A：Ubuntu / WSL2**
```bash
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip
pip install buildozer
# 首次还需按提示装 Cython 等
buildozer android debug      # 生成 bin/*.apk
```

**方式 B：官方 Docker 镜像（免装环境）**
```bash
docker run --rm -v "$PWD":/home/user/host -w /home/user/host kivy/buildozer android debug
```

打包产物在 `bin/法院文书下载器-1.0-debug.apk`（或类似名），拷到手机安装即可。
要上架 Google Play 可改 `android.release = true` 后 `buildozer android release` 出 AAB。

### 2. 权限与存储
- 应用会请求 **联网** 与 **存储** 权限（buildozer.spec 已配置）。
- 文书保存到手机 `Download/法院文书/<案件>/`，用系统「文件管理」即可找到。
- 安卓 11+ 对存储有分区限制：写入 `Download` 目录一般可用；若个别机型受限，
  文件会落到应用的私有目录，可用「文件管理 → 内部存储 → Android → data → org.example.fayuansongda」找到。
  （如确需放宽，可自行调整 buildozer.spec 的 `android.permissions` 与适配逻辑。）

### 3. 预览 PDF
点「📂 打开保存文件夹」会尝试用系统组件打开；部分机型因分区存储限制无法直接拉起预览，
此时用文件管理器手动打开 PDF 即可（核心下载功能不受影响）。

---

## 与原 Windows 版的差异
- **去掉了「PDF 转 JPG」**：手机直接看/存 PDF 更方便，且避免引入 PyMuPDF 依赖。
- 网页版由**服务端**代理法院接口（浏览器有跨域限制，无法直接调 zxfw.court.gov.cn）；
  APK 是原生网络请求，无此限制。
- 自动更新/菜单栏等桌面特性未移植（手机版用「刷新页面 / 重装 apk」代替）。

---

## 安全 / 隐私 / 法律
- 文书经你的服务器/手机下载，**不经过任何第三方**，数据来源仅为法院官方平台 zxfw.court.gov.cn。
- 网页版后端请仅在你信任的网络/服务器上运行；如需公网访问，务必放在 **HTTPS** 后。
- 仅供本人依法处理诉讼事务使用，请遵守相关平台与法律规定。

## 已知限制
- 电子送达链接有时效，过期会提示「清单为空 / 下载失败」，重新从法院短信复制最新链接即可。
- 网页版后端需保持运行；关掉终端服务即停止。
- APK 打包需自备 Android 编译环境（本沙箱无法生成 apk，仅提供工程与步骤）。

## 已验证
- `court_core.extract_tasks` 解析样例短信正确提取 qdbh/sdbh/sdsin 与案号。
- `server.py` 正常托管前端、接口错误能优雅返回 JSON。
- 全部 Python 文件通过语法检查。
- 真实取文书需联网环境，请在能访问 zxfw.court.gov.cn 的机器上运行。
