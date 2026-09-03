[app]

# (str) 应用标题
title = 法院文书下载器

# (str) 包名（必须小写、仅字母数字点，建议改成你自己的反向域名）
package.name = fayuansongda
package.domain = org.example

# (str) 源码目录与入口
source.dir = .
source.include_exts = py,png,jpg,jpeg,txt,json
# 入口必须是 main.py（python-for-android 只认 main.py 作为入口，main.filename 选项对它无效）
source.exclude_patterns = server.py, index.html, README_安卓版.md, buildozer.spec, *.pyc, .buildozer
version = 1.0

# (list) 依赖：仅 Python + Kivy（court_core.py 为零依赖标准库模块）
requirements = python3,kivy

# (str) 应用方向
orientation = portrait

# (bool) 全屏
fullscreen = 0

# (str) 图标（可选，留空则用默认；放入 icon.png 后取消注释）
# icon.filename = %(source.dir)s/icon.png

# (str) 启动图（可选）
# presplash.filename = %(source.dir)s/presplash.png

#
# Android 专属（注意：以下 android.* 必须放在 [app] 段，放 [buildozer] 段会被忽略）
#
# (int) 目标 Android API（按需调整，需 >= minapi）
android.api = 33
android.minapi = 21
# (str) 钉死稳定版 build-tools，避免拉到超新的 37 导致许可证/兼容问题
android.build_tools = 33.0.2
# (str) NDK 版本：python-for-android 要求 >= 25（23b 会报 minimum supported NDK version is 25）
android.ndk = 25b
android.arch = arm64-v8a

# (bool) 自动接受 Android SDK 许可证（CI 必开，否则 build-tools 装不上）
android.accept_sdk_license = True

# 权限：联网取文书 + 写存储保存 PDF
android.permissions = INTERNET, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE

# 启用 FileProvider（main.py 用其打开 PDF 预览）
android.fileprovider = True

# (str) Android 日志标签
android.logcat_filters = *:S python:D

# ---- 正式签名（release 构建）----
# 用 `buildozer android release` 出正式签名包。release.keystore 仅存在于 CI 构建期
# （由 secret ANDROID_KEYSTORE_B64 解码生成；首次无 secret 时 CI 用 keytool 生成并上传 artifact），
# 不入库（见仓库 .gitignore）。keystore 口令固定公开（keystore 文件本身保密即可）。
# 注意：一旦分发过某把 key 签名的包，就必须一直用它签后续版本，否则无法覆盖安装。
android.release_artifact = fayuansongda
android.keystore = release.keystore
android.keystore_alias = fayuansongda
android.keystore_password = fayuansongda2026
android.keystore_alias_password = fayuansongda2026

[buildozer]

# (int) Log level (0 = minimal, 1 = verbose, 2 = very verbose)
log_level = 2

# (bool) 跳过确认提示
warn_on_root = 0
