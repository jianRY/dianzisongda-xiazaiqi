[app]

# (str) 应用标题
title = 法院文书下载器

# (str) 包名（必须小写、仅字母数字点，建议改成你自己的反向域名）
package.name = fayuansongda
package.domain = org.example

# (str) 源码目录与入口
source.dir = .
source.include_exts = py,png,jpg,jpeg,txt,json
# 入口脚本
main.filename = kivy_main.py
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
# Android 专属
#
[buildozer]

# (int) 目标 Android API（按需调整，需 >= minapi）
android.api = 33
android.minapi = 21
android.ndk = 23b
android.arch = arm64-v8a
android.accept_sdk_license = True

# 权限：联网取文书 + 写存储保存 PDF
android.permissions = INTERNET, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE

# 启用 FileProvider（kivy_main.py 用其打开 PDF 预览）
android.fileprovider = True

# (str) Android 日志标签
android.logcat_filters = *:S python:D

# (bool) 使用 AAB（Google Play 上架需要）；调试/侧载用 APK 设为 false
android.release = false
