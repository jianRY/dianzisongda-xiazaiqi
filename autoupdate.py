#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autoupdate.py — Tkinter 应用通用自动更新模块（零第三方依赖，仅标准库）
=====================================================================

功能（2026-09-05 定版，供所有带更新功能的软件复用）：
    1. 启动后台检查 GitHub Latest Release（静默，失败不打扰）
    2. 发现新版本 → 弹窗显示「更新内容」（取 Release body，自动清理 markdown 符号）
       三个选择：
         ✅ 立即更新   → 下载进度对话框（进度条 / 速度 / 已下载大小 / 随时取消）
         ⏭ 本次忽略   → 本次关闭，下次启动继续检查
         🚫 以后不再提醒 → 配置文件写 auto_update=false，启动不再自动检查
    3. 下载完成 → 生成 updater 脚本自替换并重启（Windows bat 实现）
    4. 菜单/按钮手动「检查更新」不受 auto_update 开关影响（manual=True）

接入方法（三行代码）：
    import autoupdate
    # 程序启动后（mainloop 之前或 mainloop 内均可）：
    autoupdate.run_update_check(
        root,                       # Tk 主窗口
        app_name="法院文书下载器",    # 应用名（弹窗/临时文件用）
        current_version="1.4",       # 当前版本号（纯数字串，如 "1.4"）
        latest_api_url="https://api.github.com/repos/<owner>/<repo>/releases/latest",
        config_file=CONFIG_PATH,     # json 配置文件路径（新增/复用 auto_update 字段）
        install_helper=autoupdate.windows_replace_and_restart,  # 安装回调（一般用默认）
        log_fn=self.log_msg,         # 可选：日志回调
    )
    # 手动检查（菜单）：
    autoupdate.run_update_check(..., manual=True)

配置文件约定：
    {"auto_update": true}  缺省视为 true；用户选「以后不再提醒」后写 false。

Release 要求：
    - latest Release 的 body 写清楚本次更新内容（markdown 可读即可，弹窗会清理符号）；
    - assets 里放一个 .exe（自动取第一个 .exe 作为下载地址）。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------- 版本比较 ----------------
def parse_version(v):
    """'v1.2.3' / '1.4' → 可比较的数字元组。"""
    v = str(v).strip().lstrip("vV")
    out = []
    for p in re.split(r"[.\-]", v):
        m = re.match(r"\d+", p)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def version_greater(remote, local):
    try:
        return parse_version(remote) > parse_version(local)
    except Exception:
        return False


# ---------------- 极简 markdown 清理（弹窗显示用） ----------------
def _strip_md(text, limit=4000):
    t = text or ""
    t = re.sub(r"```.*?```", "…", t, flags=re.S)      # 代码块
    t = re.sub(r"`([^`]*)`", r"\1", t)                # 行内代码
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)        # 图片
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)    # 链接→文字
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)      # 标题井号
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)          # 粗体
    t = re.sub(r"^\s*[-*]\s+", "· ", t, flags=re.M)   # 列表符
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.strip()
    if len(t) > limit:
        t = t[:limit] + "\n…（更多见 Release 页）"
    return t


# ---------------- 配置（auto_update 开关） ----------------
def _load_auto_update(config_file):
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("auto_update", True))
    except Exception:
        return True


def _save_auto_update(config_file, enabled):
    try:
        data = {}
        if os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data["auto_update"] = bool(enabled)
        os.makedirs(os.path.dirname(config_file) or ".", exist_ok=True)
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- 取 Release 信息 ----------------
def fetch_latest_release(api_url, timeout=15):
    """返回 dict(tag, notes, download_url)；无 exe 资产时 download_url 为 None。"""
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    dl = None
    for a in data.get("assets", []):
        if str(a.get("name", "")).lower().endswith(".exe"):
            dl = a.get("browser_download_url")
            break
    return {
        "tag": data.get("tag_name", ""),
        "notes": data.get("body", "") or "",
        "download_url": dl,
        "html_url": data.get("html_url", ""),
    }


# ---------------- Windows 自替换重启 ----------------
def windows_replace_and_restart(new_exe_path, log_fn=None):
    """生成 updater.bat：等当前进程退出 → 替换 → 重启 → 自删。"""
    current = os.path.abspath(sys.executable)
    bat = os.path.join(tempfile.gettempdir(), "%s_updater.bat" % re.sub(r"\W+", "_", new_exe_path)[:40])
    cur = current.replace("/", "\\")
    tmp = new_exe_path.replace("/", "\\")
    with open(bat, "w", encoding="gbk") as f:
        f.write("@echo off\n")
        f.write(":wait\n")
        f.write("timeout /t 1 /nobreak >nul\n")
        f.write('del /f /q "%s"\n' % cur)
        f.write('if exist "%s" goto wait\n' % cur)
        f.write('move /y "%s" "%s"\n' % (tmp, cur))
        f.write('start "" "%s"\n' % cur)
        f.write('del /f /q "%~f0"\n')
    subprocess.Popen(["cmd", "/c", bat], shell=False)
    try:
        if log_fn:
            log_fn("✓ 更新脚本已启动，程序即将退出并替换…")
    except Exception:
        pass


# ---------------- 主入口 ----------------
def run_update_check(parent, app_name, current_version, latest_api_url, config_file,
                     install_helper=None, log_fn=None, manual=False):
    """启动检查（后台线程）。manual=True 时失败/无更新也弹提示，且忽略 auto_update 开关。"""
    if install_helper is None:
        install_helper = windows_replace_and_restart

    if not manual and not _load_auto_update(config_file):
        return

    def _worker():
        try:
            info = fetch_latest_release(latest_api_url)
        except urllib.error.HTTPError as e:
            if manual:
                if e.code in (401, 403, 404):
                    msg = "无法检查更新：仓库/Release 不可见（可能为私有），可前往 GitHub 手动下载。"
                else:
                    msg = "检查更新失败（HTTP %d）。" % e.code
                parent.after(0, lambda m=msg: messagebox.showwarning("检查更新", m))
            return
        except Exception as e:  # noqa: BLE001
            if manual:
                parent.after(0, lambda err=e: messagebox.showwarning(
                    "检查更新", "检查更新失败：%s" % err))
            return

        tag = info.get("tag", "")
        if not tag or not version_greater(tag, current_version):
            if manual:
                parent.after(0, lambda: messagebox.showinfo(
                    "检查更新", "已是最新版本 v%s。" % current_version))
            return
        if not info.get("download_url"):
            if manual:
                parent.after(0, lambda: messagebox.showinfo(
                    "检查更新", "发现新版本 %s，但未找到可下载的更新文件。" % tag))
            return

        # 有新版 → 主线程弹窗
        parent.after(0, lambda: UpdateDialog(
            parent, app_name=app_name, current_version=current_version,
            tag=tag, notes=info.get("notes", ""),
            download_url=info["download_url"],
            config_file=config_file, install_helper=install_helper, log_fn=log_fn,
        ))

    threading.Thread(target=_worker, daemon=True).start()


# ---------------- 更新确认弹窗（三选项） ----------------
class UpdateDialog(tk.Toplevel):
    def __init__(self, master, app_name, current_version, tag, notes,
                 download_url, config_file, install_helper, log_fn=None):
        super().__init__(master)
        self.title("发现新版本 · %s" % app_name)
        self.configure(bg="#f4f6f8")
        self.resizable(False, True)
        try:
            self.transient(master)
            self.grab_set()  # 模态
        except Exception:
            pass
        self._download_url = download_url
        self._app_name = app_name
        self._config_file = config_file
        self._install_helper = install_helper
        self._log_fn = log_fn

        frm = ttk.Frame(self, padding=(16, 14))
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm, text="发现新版本 %s（当前 v%s）" % (tag, current_version),
            font=("Microsoft YaHei", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        if notes.strip():
            ttk.Label(frm, text="本次更新内容：", font=("Microsoft YaHei", 10)).pack(anchor="w")
            txt = scrolledtext.ScrolledText(
                frm, height=12, wrap="word", font=("Microsoft YaHei", 10),
                bg="white", relief="flat",
            )
            txt.pack(fill="both", expand=True, pady=(4, 10))
            txt.insert("1.0", _strip_md(notes))
            txt.configure(state="disabled")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(4, 0))
        style = ttk.Style()
        try:
            style.configure("Primary.TButton", font=("Microsoft YaHei", 10, "bold"), padding=6)
        except Exception:
            pass
        ttk.Button(btns, text="✅ 立即更新", style="Primary.TButton",
                   command=self._on_update).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="⏭ 本次忽略",
                   command=self._on_skip).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="🚫 以后不再提醒",
                   command=self._on_never).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_skip)
        self.update_idletasks()
        w, h = 560, max(340, self.winfo_reqheight())
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry("%dx%d+%d+%d" % (w, h, max(x, 0), max(y, 0)))

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_update(self):
        self._close()
        DownloadProgressDialog(
            self.master, app_name=self._app_name,
            download_url=self._download_url,
            install_helper=self._install_helper, log_fn=self._log_fn,
        )

    def _on_skip(self):
        if self._log_fn:
            try:
                self._log_fn("⏭ 已忽略本次更新。")
            except Exception:
                pass
        self._close()

    def _on_never(self):
        _save_auto_update(self._config_file, False)
        if self._log_fn:
            try:
                self._log_fn("🚫 已关闭自动检查更新（可在帮助菜单手动检查）。")
            except Exception:
                pass
        self._close()


# ---------------- 下载进度对话框（速度 / 进度 / 取消） ----------------
class DownloadProgressDialog(tk.Toplevel):
    """共享状态 + UI 轮询模式：下载线程只写 _state，UI 每 150ms 刷新，不积压。"""

    POLL_MS = 150

    def __init__(self, master, app_name, download_url, install_helper, log_fn=None):
        super().__init__(master)
        self.title("正在下载更新 · %s" % app_name)
        self.configure(bg="#f4f6f8")
        self.resizable(False, False)
        try:
            self.transient(master)
            self.grab_set()
        except Exception:
            pass
        self._url = download_url
        self._app_name = app_name
        self._install_helper = install_helper
        self._log_fn = log_fn

        self._cancel_evt = threading.Event()
        self._state = {"done": 0, "total": 0, "running": True,
                       "error": None, "cancelled": False, "result": None}

        frm = ttk.Frame(self, padding=(18, 16))
        frm.pack(fill="both", expand=True)

        self.lbl_title = ttk.Label(frm, text="正在下载新版本…", font=("Microsoft YaHei", 11, "bold"))
        self.lbl_title.pack(anchor="w", pady=(0, 8))

        self.bar = ttk.Progressbar(frm, orient="horizontal", mode="determinate", length=100)
        self.bar.pack(fill="x")
        self.bar["maximum"] = 1
        self.bar["value"] = 0

        self.lbl_info = ttk.Label(frm, text="准备中…", font=("Microsoft YaHei", 10))
        self.lbl_info.pack(anchor="w", pady=(8, 0))
        self.lbl_speed = ttk.Label(frm, text="速度：—", font=("Microsoft YaHei", 10), foreground="#555")
        self.lbl_speed.pack(anchor="w", pady=(2, 10))

        ttk.Button(frm, text="✖ 取消更新", command=self._on_cancel).pack(anchor="e")

        self.update_idletasks()
        w = 460
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - 190) // 3
        self.geometry("%dx%d+%d+%d" % (w, 190, max(x, 0), max(y, 0)))

        threading.Thread(target=self._worker, daemon=True).start()
        self.after(self.POLL_MS, self._poll)

    # ---- 下载线程 ----
    def _worker(self):
        st = self._state
        tmp = os.path.join(tempfile.gettempdir(), "%s_更新.exe" % re.sub(r"\W+", "_", self._app_name))
        try:
            req = urllib.request.Request(
                self._url, headers={"User-Agent": UA, "Accept": "*/*"}, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                try:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                except (TypeError, ValueError):
                    total = 0
                st["total"] = total
                done = 0
                with open(tmp, "wb") as f:
                    while True:
                        if self._cancel_evt.is_set():
                            raise IOError("cancelled")
                        buf = resp.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        st["done"] = done
            if self._cancel_evt.is_set():
                raise IOError("cancelled")
            if total and done != total:
                raise IOError("下载不完整（%d / %d 字节）" % (done, total))
            if os.path.getsize(tmp) < 100000:
                raise IOError("下载文件过小，疑似失败")
            st["result"] = tmp
            st["running"] = False
        except Exception as e:  # noqa: BLE001
            st["error"] = e
            st["running"] = False
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    # ---- UI 轮询 ----
    def _fmt(self, n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return "%.1f %s" % (n, unit)
            n /= 1024.0

    def _poll(self):
        st = self._state
        done, total = st["done"], st["total"]

        # 速度：本次采样与上次采样的 Δbytes/Δt，EMA 平滑
        now = time.monotonic()
        prev = getattr(self, "_last", None)
        if prev:
            dt = now - prev[0]
            if dt > 0.05:
                speed = max(0, done - prev[1]) / dt
                self._ema = speed if not hasattr(self, "_ema") else (self._ema * 0.7 + speed * 0.3)
        self._last = (now, done)

        if total > 0:
            self.bar["maximum"] = total
            self.bar["value"] = min(done, total)
            pct = int(done * 100 / total)
            self.lbl_info.configure(text="%s / %s (%d%%)" % (self._fmt(done), self._fmt(total), pct))
        else:
            self.lbl_info.configure(text="已下载 %s" % self._fmt(done))
        if hasattr(self, "_ema"):
            self.lbl_speed.configure(text="速度：%s/s" % self._fmt(self._ema))

        if st["cancelled"]:
            self._finish(cancelled=True)
            return
        if not st["running"]:
            if st["error"] is not None:
                self._finish(error=st["error"])
                return
            if st["result"]:
                self._finish(done_path=st["result"])
                return
        self.after(self.POLL_MS, self._poll)

    def _on_cancel(self):
        self._state["cancelled"] = True
        self._cancel_evt.set()
        self.lbl_speed.configure(text="正在取消…")

    def _finish(self, cancelled=False, error=None, done_path=None):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        if cancelled:
            if self._log_fn:
                try:
                    self._log_fn("✖ 已取消更新下载。")
                except Exception:
                    pass
            return
        if error is not None:
            messagebox.showerror("更新失败", "下载新版本失败：%s" % error)
            if self._log_fn:
                try:
                    self._log_fn("✗ 更新下载失败：%s" % error)
                except Exception:
                    pass
            return
        # 成功 → 交给宿主安装（默认：自替换 + 重启）
        try:
            self._install_helper(done_path, self._log_fn)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("更新失败", "启动更新程序失败：%s" % e)
