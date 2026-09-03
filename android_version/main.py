#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法院文书下载器 · 安卓 APK 版（Kivy 源码）
=====================================================================

说明：
    - 本文件是「原生安卓 App」的源码，复用 court_core.py 的核心逻辑。
    - 在电脑上可直接 `python main.py` 运行测试（需 pip install kivy）。
    - 打包成 APK 见同目录 buildozer.spec 与 README_安卓版.md。
    - 文书会下载到手机「Download/法院文书/<案件>/」目录，可用文件管理器打开。

注意：安卓端网络/存储权限已在 buildozer.spec 申请；首次运行会自动请求存储权限。
"""

import os
import threading
import time

import court_core as core
from kivy.app import App
from kivy.clock import mainthread
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.progressbar import ProgressBar
from kivy.utils import platform

# ---------------- 存储路径 ----------------
def get_base_dir():
    """返回文书保存根目录。

    按「Download/法院文书 → app 私有外部目录」顺序尝试，确保一定能存：
    - 安卓优先用 Download/法院文书（直观好找）；
    - Android 11+ 作用域存储下若无写入权限，则回退到
      /sdcard/Android/data/<包名>/files/法院文书（无需任何权限，一定能写）。
    """
    # 1) 优先 Download/法院文书，并实测是否可写
    try:
        from android.storage import primary_external_storage_path
        base = os.path.join(primary_external_storage_path(), "Download", "法院文书")
        os.makedirs(base, exist_ok=True)
        probe = os.path.join(base, ".write_test.tmp")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return base
    except Exception:
        pass
    # 2) 回退：app 私有外部目录（不需要 WRITE_EXTERNAL_STORAGE 权限）
    try:
        from android.storage import app_storage_path
        base = os.path.join(app_storage_path(), "法院文书")
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        pass
    # 3) 桌面兜底
    base = os.path.join(os.path.expanduser("~"), "Downloads", "法院文书")
    os.makedirs(base, exist_ok=True)
    return base


def request_storage_permission():
    """安卓端运行时申请存储权限（桌面忽略）。"""
    try:
        from android.permissions import request_permissions, Permission
        request_permissions([
            Permission.READ_EXTERNAL_STORAGE,
            Permission.WRITE_EXTERNAL_STORAGE,
        ])
    except Exception:
        pass


def open_in_viewer(path):
    """尽量用系统组件打开文件（安卓用 Intent，桌面用 startfile）。失败仅记录。"""
    try:
        if platform == "android":
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            File = autoclass("java.io.File")
            FileProvider = autoclass("androidx.core.content.FileProvider")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            f = File(path)
            uri = FileProvider.getUriForFile(
                activity, activity.getPackageName() + ".fileprovider", f)
            intent = Intent(Intent.ACTION_VIEW)
            intent.setDataAndType(uri, "application/pdf")
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            activity.startActivity(intent)
        else:
            os.startfile(path)
    except Exception as e:
        print("打开文件失败（可手动在文件管理器中打开）：%s" % e)


# ---------------- UI ----------------
class CourtApp(App):
    def build(self):
        self.running = False
        root = BoxLayout(orientation="vertical", padding=12, spacing=8)

        root.add_widget(Label(
            text="法院文书下载器", font_size=22, size_hint_y=None, height=34,
            bold=True, color=(0.12, 0.43, 0.92, 1)))
        root.add_widget(Label(
            text="粘贴法院电子送达短信 / 链接，自动下载全部文书",
            font_size=13, size_hint_y=None, height=20, color=(0.4, 0.46, 0.55, 1)))

        self.txt = TextInput(
            hint_text="【苏州市虎丘区人民法院】…查阅：https://zxfw.court.gov.cn/...?qdbh=...&sdbh=...&sdsin=...",
            multiline=True, size_hint_y=None, height=120, font_size=15)
        root.add_widget(self.txt)

        self.btn_start = Button(text="⬇ 开始下载", size_hint_y=None, height=50,
                                font_size=18, background_color=(0.12, 0.43, 0.92, 1))
        self.btn_start.bind(on_press=self.on_start_press)
        root.add_widget(self.btn_start)

        self.bar = ProgressBar(max=1, value=0, size_hint_y=None, height=12)
        root.add_widget(self.bar)

        self.status = Label(text="", size_hint_y=None, height=22, font_size=13,
                            color=(0.3, 0.35, 0.45, 1))
        root.add_widget(self.status)

        # 日志（可滚动）
        self.log = Label(text="", font_size=13, size_hint_y=None, markup=False,
                         text_size=(None, None), halign="left", valign="top")
        self.log.bind(width=lambda *x: self.log.setter("text_size")(self.log, (self.log.width, None)))
        self.log.bind(texture_size=lambda inst, sz: inst.setter("height")(inst, sz[1]))
        sv = ScrollView(size_hint_y=1)
        sv.add_widget(self.log)
        root.add_widget(sv)

        self.btn_open = Button(text="📂 打开保存文件夹", size_hint_y=None, height=42,
                               disabled=True)
        self.btn_open.bind(on_press=self.on_open)
        root.add_widget(self.btn_open)

        return root

    def on_start(self):
        if platform == "android":
            request_storage_permission()

    # ---- 线程安全的 UI 更新 ----
    @mainthread
    def _set_status(self, msg):
        self.status.text = msg

    @mainthread
    def _log(self, msg):
        self.log.text += msg + "\n"

    @mainthread
    def _set_bar(self, total, done):
        if total > 0:
            self.bar.max = total
        self.bar.value = done

    @mainthread
    def _finish(self, ok, total, last_dir):
        self.running = False
        self.btn_start.disabled = False
        self.btn_start.text = "⬇ 开始下载"
        if last_dir:
            self.last_dir = last_dir
            self.btn_open.disabled = False
        if ok:
            self._set_status("✅ 完成：成功 %d 份" % total)
        else:
            self._set_status("⚠ 部分/未完成，详见日志")

    # ---- 交互 ----
    def on_start_press(self, *_):
        if self.running:
            return
        text = self.txt.text.strip()
        if not text:
            self._set_status("请先粘贴送达短信或链接")
            return
        self.running = True
        self.btn_start.disabled = True
        self.btn_start.text = "下载中…"
        self.log.text = ""
        self.last_dir = None
        self.btn_open.disabled = True
        t = threading.Thread(target=self.worker, args=(text,), daemon=True)
        t.start()

    def worker(self, text):
        ctx = core.make_ctx()
        try:
            tasks = core.extract_tasks(text)
            if not tasks:
                self._log("✗ 未能识别到送达链接（需含 qdbh/sdbh/sdsin）。")
                self._finish(False, 0, None)
                return
            self._log("识别到 %d 个送达链接，开始依次下载…" % len(tasks))
            ts = time.strftime("%Y%m%d%H%M%S")
            total_ok = 0
            last_dir = None
            done = 0
            total = 0
            for idx, params in enumerate(tasks, 1):
                self._log("==== 案件 %d / %d ====" % (idx, len(tasks)))
                try:
                    docs = core.fetch_doc_list(params, ctx)
                except Exception as e:
                    self._log("✗ 获取清单失败：%s" % e)
                    continue
                self._log("✓ 找到 %d 份文书" % len(docs))
                total += len(docs)
                court = docs[0].get("c_fymc", "未知法院")
                caseno = params.get("caseno", "") or ""
                folder = core.sanitize_filename(
                    (court + ("_" + caseno if caseno else "") + "_" + ts).strip("_ "))
                case_dir = os.path.join(get_base_dir(), folder)
                os.makedirs(case_dir, exist_ok=True)
                last_dir = case_dir
                for i, d in enumerate(docs, 1):
                    raw = d.get("c_wsmc") or ("文书%d" % i)
                    ext = core.safe_ext(d.get("c_wjgs"), d.get("wjlj", ""))
                    base = core.sanitize_filename(raw)
                    filename = base + ext
                    full = os.path.join(case_dir, filename)
                    dup = 1
                    while os.path.exists(full):
                        filename = "%s(%d)%s" % (base, dup, ext)
                        full = os.path.join(case_dir, filename)
                        dup += 1
                    self._log("[%d/%d] 下载：%s" % (i, len(docs), raw))
                    try:
                        core.download_file(d.get("wjlj"), full, ctx)
                        sz = os.path.getsize(full)
                        self._log("    ✓ 已保存：%s (%d 字节)" % (filename, sz))
                        total_ok += 1
                    except Exception as e:
                        self._log("    ✗ 失败：%s" % e)
                    done += 1
                    self._set_bar(total, done)
                self._log("--- 本案完成，目录：%s ---" % case_dir)
            self._log("")
            self._log("=== 全部完成：成功 %d / %d 份 ===" % (total_ok, total))
            self._finish(total_ok >= total and total > 0, total_ok, last_dir)
        except Exception as e:
            self._log("✗ 出错了：%s" % e)
            self._finish(False, 0, getattr(self, "last_dir", None))

    def on_open(self, *_):
        d = getattr(self, "last_dir", None)
        if d and os.path.isdir(d):
            open_in_viewer(d)


if __name__ == "__main__":
    CourtApp().run()
