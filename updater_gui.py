#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""updater_gui.py — 跨项目共享的更新界面层（唯一权威源）

=====================================================================
配套 updater_core.py，同样是**唯一真源 + 逐字节副本**的落地方式：

    权威源   <共享目录>/updater/updater_gui.py              ← 只改这里
    副本     <各项目>/updater_gui.py                        ← 由同步脚本写入

⚠️ 改副本会被同步脚本覆盖回去。只改权威源。

——————————————————————————————————————————————

本层提供三个类（四个项目共用，行为逐字节一致）：

    UpdateDialog            更新确认弹窗（三选项 + 清理勾选框）
    DownloadProgressDialog  下载进度框（进度条 / 速度 / 换源 / 取消）
    _SwitchedSource         掉速换源信号（异常类）

与 updater_core.py 的分工：

    core  = 纯逻辑（版本比较、源排序、元数据、校验、落位、清理）
    gui   = 界面（弹窗、进度条、按钮）
    项目自己的 autoupdate.py = 薄适配层（保留旧接口名 + 本项目配置）

安全基线与 core 一致，且比旧版更严：

    · sha256 **必须**存在且匹配 —— 缺值直接拒绝安装（旧版法院项目才这么严，
      其它三个项目旧版是「有值才校验」，现在统一为强制）。
    · 下载物必须以 MZ 开头。
    · 临时文件用 mkstemp 独占（多开实例不互相踩）。
    · 取消/失败时按 worker 实际用的路径删临时文件（不再猜固定名）。
"""

import os
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk

# 引用共享内核。
# ⚠️ **包内优先**：聚合器（工具软件组合）里本文件位于 shell/ 包中，
#    必须 import 的是 `shell.updater_core` —— 若误用顶层 `updater_core`，
#    会拿到加载器预置的「某个内嵌模块的那一份」，两个应用共用同一实例，
#    updater_core 的待改名状态（_PENDING_RENAME）就会互相串。
try:
    from . import updater_core as core      # 作为包模块时（工具软件组合 / shell.updater_gui）
except ImportError:
    import updater_core as core             # 作为顶层模块时（四个独立项目）

__all__ = ["UpdateDialog", "DownloadProgressDialog", "_SwitchedSource",
           "center_on", "scale_px"]


class _SwitchedSource(Exception):
    """下载速度过低、主动换源续传 —— 属正常调度，不是失败（已下载字节会被保留）。"""


def scale_px(master, v):
    """子窗口尺寸过 DPI 缩放。

    主窗口开了 PER_MONITOR_DPI_AWARE 后，geometry() 收的是物理像素，
    而字号按 DPI 自动放大 → 不缩放会把子窗口压成窄条。
    """
    try:
        return int(round(v * master.winfo_fpixels("1i") / 96.0))
    except Exception:  # noqa: BLE001
        return int(v)


def center_on(master, w, h):
    """相对 master 居中；master 尺寸还没算出来时退化为屏幕居中，避免弹窗跑到屏幕角落。"""
    try:
        master.update_idletasks()
        mw, mh = master.winfo_width(), master.winfo_height()
        if mw > 1 and mh > 1:
            return (max(master.winfo_rootx() + (mw - w) // 2, 0),
                    max(master.winfo_rooty() + (mh - h) // 3, 0))
        sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        return (max((sw - w) // 2, 0), max((sh - h) // 3, 0))
    except Exception:  # noqa: BLE001
        return (60, 60)


def _mkbtn(parent, text, cmd, primary=False):
    """统一按钮样式：主按钮蓝底白字，次按钮浅灰深字。

    tk.Button 扁平化 —— ttk 在 vista 主题下改不了底色。
    """
    return tk.Button(
        parent, text=text, command=cmd,
        font=("Microsoft YaHei UI", 10, "bold") if primary else ("Microsoft YaHei UI", 10),
        bg="#2E5DA8" if primary else "#e5e7eb",
        fg="#ffffff" if primary else "#1f2937",
        activebackground="#254C8C" if primary else "#d1d5db",
        activeforeground="#ffffff" if primary else "#1f2937",
        relief="flat", bd=0, cursor="hand2",
        padx=18 if primary else 14, pady=6,
    )


# ============================================================== 更新确认弹窗


class UpdateDialog(tk.Toplevel):
    """发现新版本 → 三个选择：立即更新 / 本次忽略 / 以后不再提醒。"""

    # 更新完成后是否自动启动新版本。
    #   True （默认，历史行为）= 装完就拉起新版，用户无感；
    #   False = 只落位、不拉起，装完弹一句「请重新运行程序」后退出。
    # 适配层直接写 `UpdateDialog.AUTO_RESTART = False` 即可，不用改内核。
    AUTO_RESTART = True

    def __init__(self, master, app_name, current_version, tag, notes,
                 download_url=None, config_file=None, install_helper=None,
                 log_fn=None, on_before_install=None,
                 download_urls=None, sha256="", html_url="",
                 cleanup_old_default=True, cfg=None, write_flag_fn=None,
                 site_url=""):
        super().__init__(master)
        self.title("发现新版本 · %s" % app_name)
        self.configure(bg="#F2F4F8")
        self.resizable(False, True)
        try:
            self.transient(master)
            self.grab_set()  # 模态
        except Exception:  # noqa: BLE001
            pass
        self._download_urls = list(download_urls or ([download_url] if download_url else []))
        self._sha256 = sha256
        self._html_url = html_url
        self._site_url = site_url
        self._download_url = self._download_urls[0] if self._download_urls else None
        self._app_name = app_name
        self._config_file = config_file
        self._install_helper = install_helper
        self._log_fn = log_fn
        self._on_before_install = on_before_install
        self._cfg = cfg
        self._write_flag_fn = write_flag_fn

        frm = ttk.Frame(self, padding=(16, 14))
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm, text="发现新版本 %s（当前 v%s）" % (tag, current_version),
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        if (notes or "").strip():
            ttk.Label(frm, text="本次更新内容：", font=("Microsoft YaHei UI", 10)).pack(anchor="w")
            txt = scrolledtext.ScrolledText(
                frm, height=12, wrap="word", font=("Microsoft YaHei UI", 10),
                bg="white", relief="flat",
            )
            txt.pack(fill="both", expand=True, pady=(4, 10))
            txt.insert("1.0", core.strip_md(notes))
            txt.configure(state="disabled")

        # 「手动下载」入口：自动更新随时可能被墙 / 代理 / 杀毒软件拦掉，那时用户
        # 只会看到一句「更新失败」；窗口里若没有可点的地址，等于「知道有新版却
        # 拿不到」。所以**发现新版时**就把地址摆出来，点一下用系统浏览器打开，
        # 完全不依赖自动更新是否成功。
        # ⚠️ 文案用「自动更新失败？」，是在事前给出退路，不是咒它失败。
        links = core.manual_download_links(site_url, html_url, self._download_urls)
        if links:
            lf = ttk.Frame(frm)
            lf.pack(fill="x", pady=(0, 6))
            ttk.Label(lf, text="自动更新失败？手动下载：",
                      font=("Microsoft YaHei UI", 9)).pack(side="left")
            for i, (label, url) in enumerate(links[:2]):
                lb = ttk.Label(lf, text=label, cursor="hand2",
                               foreground="#1a56db",
                               font=("Microsoft YaHei UI", 9))
                lb.pack(side="left", padx=(6 if i else 4, 0))
                lb.bind("<Button-1>", lambda e, u=url: self._open_url(u))

        # 「更新后是否删除旧版本文件」：默认勾选（= 保持旧行为）。
        # 勾选＝更新完成后把旧版 exe **移入系统回收站**（不是彻底删除，可还原）；
        # 取消勾选时旧版本 exe 会保留在程序目录里，但**会被改名**成
        # `xxx_旧版_<时间戳>.exe`（就地更新必须把正在运行的 exe 挪开腾位置），
        # 所以文案里要写清楚「改名保留」，否则用户找不到「我明明选了保留」的那个文件。
        # 放在按钮**上方**：它是本次更新的前置选择，与「立即更新」同属一个决策区；
        # 放到按钮下面会紧贴窗口底边，看起来像按钮的副标题。
        self._keep_old = tk.BooleanVar(value=cleanup_old_default)
        ttk.Checkbutton(
            frm, text="更新完成后把旧版本文件移入系统回收站"
                      "（取消勾选则改名保留在程序目录，可双击回退）",
            variable=self._keep_old,
        ).pack(anchor="w", pady=(0, 8))

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(4, 0))

        _mkbtn(btns, "以后不再提醒", self._on_never).pack(side="left")
        _mkbtn(btns, "本次忽略", self._on_skip).pack(side="left", padx=(8, 0))
        _mkbtn(btns, "立即更新", self._on_update, primary=True).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_skip)
        self.update_idletasks()
        w, h = scale_px(master, 560), max(scale_px(master, 340), self.winfo_reqheight())
        x, y = center_on(master, w, h)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _close(self):
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    def _open_url(self, url):
        """用系统默认浏览器打开地址。失败也不弹错 —— 这只是辅助入口，
        绝不能因为「浏览器打不开」把更新主流程的提示盖掉。"""
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001
            opened = False
        if self._log_fn:
            try:
                self._log_fn(("↪ 已在浏览器打开：%s" if opened else "手动下载地址：%s") % url)
            except Exception:  # noqa: BLE001
                pass

    def _on_update(self):
        cleanup_old = bool(self._keep_old.get())
        # 记住这次选择，下次弹窗用同样的默认值（配置写失败不影响更新本身）
        core.save_cleanup_old(self._config_file, cleanup_old)
        if self._log_fn:
            try:
                self._log_fn("旧版本文件处理方式：%s"
                             % ("移入系统回收站" if cleanup_old
                                else "保留 —— 旧版本会被改名为「<软件名>_旧版_<时间戳>.exe」"
                                     "留在程序目录，双击它即可回退"))
            except Exception:  # noqa: BLE001
                pass
        self._close()
        DownloadProgressDialog(
            self.master, app_name=self._app_name,
            download_url=self._download_url,
            download_urls=self._download_urls, sha256=self._sha256,
            html_url=self._html_url, site_url=self._site_url,
            install_helper=self._install_helper, log_fn=self._log_fn,
            on_before_install=self._on_before_install,
            cleanup_old=cleanup_old,
            auto_restart=self.AUTO_RESTART,
            cfg=self._cfg,
            # 传类级钩子（而不是实例属性）：子类可以覆盖它，
            # 适配层则用它来指向自己模块的 write_cleanup_flag。
            write_flag_fn=getattr(self.__class__, "_default_write_flag_fn", None),
        )

    def _on_skip(self):
        if self._log_fn:
            try:
                self._log_fn("⏭ 已忽略本次更新。")
            except Exception:  # noqa: BLE001
                pass
        self._close()

    def _on_never(self):
        core.save_auto_update(self._config_file, False)
        if self._log_fn:
            try:
                self._log_fn("🚫 已关闭自动检查更新（可在帮助菜单手动检查）。")
            except Exception:  # noqa: BLE001
                pass
        self._close()


# ============================================================== 下载进度框


class DownloadProgressDialog(tk.Toplevel):
    """共享状态 + UI 轮询模式：下载线程只写 _state，UI 每 150ms 刷新，不积压。"""

    POLL_MS = 150

    def __init__(self, master, app_name, download_url=None, install_helper=None,
                 log_fn=None, on_before_install=None,
                 download_urls=None, sha256="", cleanup_old=True, cfg=None,
                 write_flag_fn=None, html_url="", site_url="", auto_restart=True):
        super().__init__(master)
        self.title("正在下载更新 · %s" % app_name)
        self.configure(bg="#F2F4F8")
        self.resizable(False, False)
        try:
            self.transient(master)
            self.grab_set()  # 模态
        except Exception:  # noqa: BLE001
            pass
        self._urls = [u for u in (download_urls or ([download_url] if download_url else [])) if u]
        self._url = self._urls[0] if self._urls else None
        self._sha256 = (sha256 or "").strip().lower()
        # 失败时要把「手动下载去哪下」告诉用户，所以这两个地址一路带下来。
        self._html_url = html_url or ""
        self._site_url = site_url or ""
        self._app_name = app_name
        self._install_helper = install_helper
        self._log_fn = log_fn
        self._on_before_install = on_before_install
        self._cfg = cfg
        # 「是否删除旧版本」：由更新弹窗的复选框决定，透传给 install_helper。
        # install_helper 可能被宿主换成自定义函数，所以**用关键字传**并在调用处
        # 兼容旧签名（只接受 2 个位置参数的老函数）——别把宿主的老回调弄崩。
        self._cleanup_old = bool(cleanup_old)
        # 更新完是否自动拉起新版本（见 UpdateDialog.AUTO_RESTART 的说明）
        self._auto_restart = bool(auto_restart)
        self._cancel_evt = threading.Event()
        self._has_spare = False
        self._dl_thread = None
        # 写跨进程标记的实现（可注入）：适配层传自己的 write_cleanup_flag 进来，
        # 这样宿主的 monkeypatch 能对这里的调用生效。
        self._write_flag_fn = write_flag_fn
        self._state = {"done": 0, "total": 0, "running": True,
                       "error": None, "cancelled": False, "result": None,
                       "phase": "准备中…", "src": "", "idx": 0, "cnt": 0,
                       "tmp": None}

        frm = ttk.Frame(self, padding=(18, 16))
        frm.pack(fill="both", expand=True)

        self.lbl_title = ttk.Label(frm, text="正在下载新版本…",
                                    font=("Microsoft YaHei UI", 11, "bold"))
        self.lbl_title.pack(anchor="w", pady=(0, 8))

        self.bar = ttk.Progressbar(frm, orient="horizontal", mode="determinate", length=100)
        self.bar.pack(fill="x")
        self.bar["maximum"] = 1
        self.bar["value"] = 0

        self.lbl_info = ttk.Label(frm, text="准备中…", font=("Microsoft YaHei UI", 10))
        self.lbl_info.pack(anchor="w", pady=(8, 0))
        self.lbl_speed = ttk.Label(frm, text="速度：—", font=("Microsoft YaHei UI", 10),
                                    foreground="#555")
        self.lbl_speed.pack(anchor="w", pady=(2, 0))
        self.lbl_src = ttk.Label(frm, text="下载源：正在选择…", font=("Microsoft YaHei UI", 9),
                                 foreground="#8a94a6")
        self.lbl_src.pack(anchor="w", pady=(2, 10))

        tk.Button(frm, text="取消更新", command=self._on_cancel,
                  font=("Microsoft YaHei UI", 10),
                  bg="#e5e7eb", fg="#1f2937",
                  activebackground="#d1d5db", activeforeground="#1f2937",
                  relief="flat", bd=0, cursor="hand2", padx=14, pady=5,
                  ).pack(anchor="e")

        self.update_idletasks()
        w = scale_px(master, 460)
        x, y = center_on(master, w, scale_px(master, 210))
        self.geometry("%dx%d+%d+%d" % (w, scale_px(master, 210), x, y))

        self._dl_thread = threading.Thread(target=self._worker, daemon=True)
        self._dl_thread.start()
        self.after(self.POLL_MS, self._poll)

    # ---- 内部工具 ----
    def _log(self, msg):
        if not self._log_fn:
            return
        try:
            self._log_fn(msg)
        except Exception:  # noqa: BLE001
            pass

    def _label(self, url):
        return core.source_label(url, self._cfg)

    # ---- 下载线程 ----
    def _log_speeds(self, speeds):
        if not self._log_fn:
            return
        try:
            items = sorted(speeds.items(), key=lambda kv: -kv[1])
            txt = "、".join("%s %.0fKB/s" % (self._label(u), s) for u, s in items)
            self._log_fn("测速结果：%s" % txt)
        except Exception:  # noqa: BLE001
            pass

    def _download_one(self, url, tmp, done, st):
        """从单个源下载（done>0 时用 Range 续传）。返回累计已下载字节数。

        速度长期过低且还有备选源时抛 _SwitchedSource，已下载字节保留给下个源续传。
        """
        import urllib.request
        headers = {"User-Agent": (self._cfg.user_agent if self._cfg else core.UA),
                   "Accept": "*/*"}
        if done > 0:
            headers["Range"] = "bytes=%d-" % done
        req = urllib.request.Request(url, headers=headers, method="GET")
        # ⚠️⚠️ 必须走 core.resolve_ssl_context()，不能直接取 cfg.ssl_context：
        #   各项目允许把 ssl_context 传成「惰性构造函数」（法院项目就是这样，
        #   避免每次请求都重读证书库）。直接把函数塞给 urlopen(context=)，
        #   建立 TLS 时 http.client 会调 context.wrap_socket(...) →
        #   AttributeError: 'function' object has no attribute 'wrap_socket'。
        #   这个坑在 v2.12.0 的共享内核重构里引入：取元数据那几处都走了 resolve，
        #   唯独下载这条路漏了 ⇒ 表现是「能检查到新版本，一点更新就失败」，
        #   而且新旧版本都卡死（坏代码在用户机器上），只能手动下载绕过。
        ctx = core.resolve_ssl_context(self._cfg)
        if ctx is not None:
            resp = urllib.request.urlopen(req, timeout=30, context=ctx)
        else:
            resp = urllib.request.urlopen(req, timeout=30)
        with resp:
            status = getattr(resp, "status", 200)
            if done > 0 and status != 206:
                # 该源不支持断点续传（或忽略了 Range）→ 只能从头下
                done = 0
                st["done"] = 0
            try:
                total = int(resp.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                total = 0
            if total:
                st["total"] = total + done if status == 206 else total
                total = st["total"]
            mode = "ab" if (done > 0 and status == 206) else "wb"
            win_t0, win_bytes = time.monotonic(), 0
            with open(tmp, mode) as f:
                while True:
                    if self._cancel_evt.is_set():
                        raise IOError("cancelled")
                    buf = resp.read(65536)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    win_bytes += len(buf)
                    st["done"] = done
                    # 换源看门狗：连续 8 秒平均速度仍低于门限 → 换源续传
                    elapsed = time.monotonic() - win_t0
                    if elapsed >= 8.0:
                        if win_bytes / elapsed < core.SWITCH_SPEED and self._has_spare:
                            raise _SwitchedSource(
                                "%s 速度仅 %.0fKB/s，换源续传"
                                % (self._label(url), win_bytes / 1024 / elapsed))
                        win_t0, win_bytes = time.monotonic(), 0
        return done

    def _worker(self):
        st = self._state
        # 临时文件名必须随机且独占：固定名会被同机其它进程预置/覆盖，
        # 多开两个实例同时更新时也会互相踩（都往同一个 _更新.exe 里写）。
        tmp = core.make_temp_exe_path(self._app_name)
        # 把实际路径告诉收尾逻辑：_finish 取消/失败时要删的就是这个文件，
        # 不能再用固定名去猜（名字是随机生成的，猜不到就删不掉，会在 %TEMP% 堆垃圾）。
        st["tmp"] = tmp

        sources = core.build_download_sources(self._urls, self._cfg)
        if not sources:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            st["error"] = IOError("没有可用的下载地址")
            st["running"] = False
            return

        # 阶段一：并发探活，把死源/病源挪到队尾（镜像优先，其次自有站）
        if len(sources) > 1:
            st["phase"] = "正在选择最快的下载源…"
            sources = core.rank_sources(sources, on_probe_done=self._log_speeds,
                                        cfg=self._cfg)
        st["phase"] = "下载中"

        done = 0
        last_err = None
        for idx, url in enumerate(sources, 1):
            if self._cancel_evt.is_set():
                break
            self._has_spare = idx < len(sources)
            st["src"] = self._label(url)
            st["idx"] = idx
            st["cnt"] = len(sources)
            try:
                done = self._download_one(url, tmp, done, st)
                if self._cancel_evt.is_set():
                    raise IOError("cancelled")
                if done <= 0:
                    raise IOError("未收到数据")
                want = st.get("total") or 0
                if want and done < want:
                    raise IOError("下载不完整（%d / %d 字节）" % (done, want))
                if os.path.getsize(tmp) < 100000:
                    raise IOError("下载文件过小，疑似失败")
                # sha256 必须非空：下载源（镜像 / 明文自有站）本身不可信，
                # 这个值来自可信 HTTPS 源，是唯一的完整性依据。缺了就不装 ——
                # 宁可让用户去 Releases 手动下载，也不执行来路不明的 exe。
                if not self._sha256:
                    raise IOError("更新包缺少 sha256 校验值，已拒绝安装。"
                                  "请到 GitHub Releases 手动下载。")
                if core.file_sha256(tmp) != self._sha256:
                    raise IOError("文件校验失败（SHA256 不一致），已丢弃")
                if not core.looks_like_exe(tmp):
                    raise IOError("下载到的不是可执行文件（缺少 MZ 头），已丢弃")
                st["result"] = tmp
                st["running"] = False
                return
            except _SwitchedSource as e:
                # 主动换源：保留已下载字节，下个源用 Range 续传
                last_err = e
                self._log("下载换源：%s" % e)
                continue
            except Exception as e:  # noqa: BLE001
                if self._cancel_evt.is_set():
                    break
                last_err = e
                # 数据可能已损坏/不完整 → 清掉重来，不做跨源续传
                done = 0
                st["done"] = 0
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                continue

        if self._cancel_evt.is_set():
            st["error"] = IOError("cancelled")
        else:
            st["error"] = last_err or IOError("所有下载源均不可用")
        st["running"] = False

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
            self.lbl_info.configure(text=st.get("phase") or ("已下载 %s" % self._fmt(done)))
        if hasattr(self, "_ema"):
            self.lbl_speed.configure(text="速度：%s/s" % self._fmt(self._ema))
        src = st.get("src") or ""
        if src:
            self.lbl_src.configure(text="下载源：%s（第 %d/%d 个）"
                                   % (src, st.get("idx", 1), st.get("cnt", 1)))
        else:
            self.lbl_src.configure(text="下载源：%s" % (st.get("phase") or "正在选择…"))

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
        except Exception:  # noqa: BLE001
            pass
        self.destroy()
        # 记下临时文件路径：取消/失败时要用它清掉半截的 exe。
        # （成功时不能删 —— 那一路要交给 install_helper 搬进程序目录）
        # 优先用 worker 实际创建的那个路径（mkstemp 是随机名），只在拿不到时回退到旧固定名。
        tmp = (self._state or {}).get("tmp") or os.path.join(
            tempfile.gettempdir(),
            "%s_更新.exe" % re.sub(r"\W+", "_", self._app_name))
        if cancelled:
            self._discard_partial(tmp)
            self._log("✖ 已取消更新下载。")
            return
        if error is not None:
            self._discard_partial(tmp)
            # 失败时**必须**给出可手动下载的地址：用户此刻最需要的就是「去哪下」，
            # 而更新链挂掉的原因五花八门（被墙 / 代理 / 杀毒 / 临时 502），
            # 光报一句「下载失败」等于把人晾在原地。
            hint = core.manual_download_text(
                getattr(self, "_site_url", ""), getattr(self, "_html_url", ""),
                getattr(self, "_urls", None))
            msg = "下载新版本失败：%s" % error
            if hint:
                msg += "\n\n" + hint + "\n\n装好新版后自动更新即恢复正常；" \
                                       "也可以稍后在软件里点「检查更新」重试。"
            messagebox.showerror("更新失败", msg)
            self._log("✗ 更新下载失败：%s" % error)
            if hint:
                try:
                    self._log("↪ 手动下载地址：" + "；".join(
                        "%s %s" % (k, v) for k, v in core.manual_download_links(
                            getattr(self, "_site_url", ""),
                            getattr(self, "_html_url", ""),
                            getattr(self, "_urls", None))))
                except Exception:  # noqa: BLE001
                    pass
            return
        # 成功 → 交给宿主安装（默认：就地落位；AUTO_RESTART 决定是否顺手拉起新版）
        try:
            self._call_install_helper(done_path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("更新失败", "更新未完成：%s" % e)
            return
        # 先让宿主收尾（停后台任务、销毁窗口等），再决定「自动重启」还是「提示手动重开」。
        # ⚠️ 提示必须在这里弹：restart=False 时新版本已经顶掉原文件名，本进程必须退出放锁，
        #    所以要趁窗口还活着把话说完。
        auto_restart = bool(getattr(self, "_auto_restart", True))
        if not auto_restart:
            # 不自动拉起时，必须**明确告诉用户「已更新、请重新打开」**：
            # 否则窗口里还是旧版界面，用户会以为更新根本没成功。
            #
            # ① 旧版文件就在**这里**送进回收站，不再等用户下次启动（2026-11-15 建哥要求）：
            #    顺序固定为「先处理旧版、再弹提示」。反过来的话，提示里写着
            #    「旧版已移入回收站」，而用户切到资源管理器一看两个 exe 还并排躺着——
            #    界面说的和实际做的对不上，比不提示更糟。
            disposal = self._dispose_old_versions()
            self._log("✓ 更新完成：新版本已就位（未自动启动，重新运行程序后生效）")
            try:
                messagebox.showinfo("更新完成", self._done_text(disposal))
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._on_before_install:
                self._on_before_install()
        except Exception:  # noqa: BLE001
            pass
        # 本进程必须真正退出：既避免两个实例同时存在，也为了释放旧版文件的锁。
        self._exit_app(restart=auto_restart)

    # ---- 更新完成时：旧版去哪了 ----
    def _dispose_old_versions(self):
        """把程序目录里的旧版文件送进系统回收站，返回内核给的处置结果。

        ⚠️ 必须在**弹「更新完成」之前**调用 —— 提示要如实交代旧版的下落，
           而「提示已弹出、旧版还躺在程序目录里」就是这个函数要消灭的窗口期。
        ⚠️ 正在运行的 exe 删不掉自己，内核会把这一份登记给后台助手，
           本进程退出后 0.3 秒内完成 —— 用户看到的结果和提示说的一致。
        ⚠️ 只处理「本程序自己的旧版文件」（命名结构由内核把关），
           同目录里别人的文件一律不碰。
        """
        info = {"cleanup_old": bool(getattr(self, "_cleanup_old", True)),
                "found": [], "recycled": [], "scheduled": [], "failed": []}
        try:
            current = os.path.abspath(sys.executable)
            directory = os.path.dirname(current)
        except Exception:  # noqa: BLE001
            return info
        try:
            res = core.recycle_parked_versions(
                directory, current=current,
                app_name=getattr(self, "_app_name", "") or None,
                log_fn=self._log, cleanup_old=info["cleanup_old"])
            if isinstance(res, dict):
                info.update(res)
        except Exception as e:  # noqa: BLE001
            # 走到这里说明更新本身已经成功了，收尾出岔子绝不能把它说成「更新失败」
            self._log("旧版本文件处理异常（不影响更新结果）：%s" % e)
        return info

    def _done_text(self, info=None):
        """「更新完成」提示的正文。

        三种情形都要说清楚旧版去哪了，不能让用户自己猜：
          · 删除旧版 + 已进回收站 → 明确告知已移入回收站，并给出回退办法
          · 删除旧版 + 暂时删不掉   → 说明「关闭后 / 下次启动」会清理，绝不说成已删
          · 保留旧版（用户取消勾选）→ 写明保留成什么名字，双击即可回退
        """
        info = info or {}
        app = getattr(self, "_app_name", "") or "本程序"
        recycled = list(info.get("recycled") or [])
        scheduled = list(info.get("scheduled") or [])
        failed = list(info.get("failed") or [])
        found = list(info.get("found") or [])
        kept = found if not info.get("cleanup_old", True) else []

        lines = ["更新已完成，新版本已安装到原位置。"]
        if recycled or scheduled:
            lines.append("")
            lines.append("旧版本已移入系统回收站：")
            lines += ["· %s" % n for n in (recycled + scheduled)]
            lines.append("如需回退到旧版本，可在回收站中还原该文件后双击运行。")
        elif kept:
            lines.append("")
            lines.append("按你的选择，旧版本已保留在程序目录：")
            lines += ["· %s" % n for n in kept]
            lines.append("需要回退时，双击上面的文件即可运行旧版本。")
        if failed:
            lines.append("")
            lines.append("以下旧版本文件暂时没能删除（仍被占用），"
                         "会在下次启动时自动清理：")
            lines += ["· %s" % n for n in failed]
        lines.append("")
        lines.append("点「确定」后本程序会关闭，请重新打开「%s」即可使用新版本。" % app)
        return "\n".join(lines)

    def _discard_partial(self, path):
        """删掉没下完的临时 exe。

        ⚠️ 必须先让下载线程收干净再删：线程还在往这个文件里写的时候删掉，
        会留下「文件被占用删不掉」或「删完又被写出一个残缺文件」的竞态。
        旧实现没做这一步，是 P0 回归（发票识别汇总项目的测试 [8] 守着）。
        """
        # ① 先置取消标志，让下载循环尽快退出
        try:
            self._cancel_evt.set()
        except Exception:  # noqa: BLE001
            pass
        # ② 等下载线程收尾（最多等 3 秒；线程卡在网络读时靠 timeout 自然结束）
        try:
            th = getattr(self, "_dl_thread", None)
            if th is not None and th.is_alive():
                th.join(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        # ③ 再删文件
        try:
            if path and os.path.exists(path):
                os.remove(path)
                # ⚠️ 日志必须是「尽力而为」：这一步跑在失败收尾路径上，
                #    绝不能因为日志回调出问题（甚至对象没被完整初始化）而把
                #    原始异常盖掉 —— 那会让用户看到完全无关的报错。
                try:
                    self._log("已清理未完成的临时更新文件：%s"
                              % os.path.basename(path))
                except Exception:  # noqa: BLE001
                    pass
        except OSError:
            pass

    def _call_install_helper(self, done_path):
        """调用宿主给的安装函数，并带上 cleanup_old / restart。

        为什么要这么啰嗦：`install_helper` 是开放给宿主的回调，**可能有项目还在用
        只接受 2 个位置参数的老签名**（不接 cleanup_old），也可能有不接 restart 的。
        直接多传关键字会让它抛 TypeError → 用户看到「更新未完成」，但其实文件已经
        下好了，比不做还糟。所以：参数从多到少逐级回退。

        ⚠️「写标记文件」必须放在调用 helper **之前**的公共路径上：
        曾写在「helper 拒绝新签名」的异常分支里，于是宿主自带一个**支持 cleanup_old
        的 helper** 时压根不会写 → 新版读不到用户的选择 → 回落 True 照删旧版，
        用户勾掉「删除旧版」的意图被静默忽略。默认实现自己也会写一次（同值覆盖，幂等）。

        write_flag_fn: 可注入的依赖点。各项目的适配层必须传自己的
            write_cleanup_flag 进来 —— 宿主的 monkeypatch（替换适配层模块里的
            write_cleanup_flag）才能对这里的调用生效。
        """
        # ① 先写跨进程标记（新旧签名都一样）
        #    ⚠️ 用 getattr 而不是 self._write_flag_fn：测试会用 object.__new__(cls)
        #    绕过 __init__ 造对象再调本方法，此时实例属性一个都没有。
        #    回落顺序：实例注入 → 类级钩子 → 内核默认。
        #    适配层把 write_flag_fn 作为**类属性**设好（指向自己模块里的函数），
        #    于是宿主 monkeypatch 适配层模块的 write_cleanup_flag 时，
        #    类级钩子会重新解析到补丁 —— 这是向后兼容的关键。
        writer = (getattr(self, "_write_flag_fn", None)
                  or getattr(self.__class__, "_default_write_flag_fn", None)
                  or core.write_cleanup_flag)
        try:
            writer(os.path.dirname(os.path.abspath(sys.executable)),
                   self._cleanup_old)
        except Exception:  # noqa: BLE001
            pass
        # ② 再调宿主安装函数：能少传就少传，退回时**必须记日志**，
        #    因为退回后 restart 约定可能不成立（helper 会自作主张拉起新版）。
        want_restart = bool(getattr(self, "_auto_restart", True))
        try:
            self._install_helper(done_path, self._log_fn,
                                 cleanup_old=self._cleanup_old,
                                 restart=want_restart)
            return
        except TypeError as e:
            # 只在「参数不匹配」时退回；安装逻辑内部的 TypeError 必须暴露出来
            if "restart" not in str(e) and "cleanup_old" not in str(e):
                raise
            self._log("安装函数未适配新参数，改用兼容签名调用。")
        if not want_restart:
            # helper 不认识 restart ⇒ 它一定会自动拉起新版。此时不能静默：
            # 用户在界面上看到的是「请重新运行」，实际却会自己弹一个新窗口。
            self._log("⚠ 安装函数不支持「不自动启动」，本次仍会自动打开新版本。")
        try:
            self._install_helper(done_path, self._log_fn,
                                 cleanup_old=self._cleanup_old)
            return
        except TypeError as e:
            if "cleanup_old" not in str(e):
                raise
            self._log("安装函数使用旧签名，改为默认保留旧版本的清理策略。")
        self._install_helper(done_path, self._log_fn)

    def _exit_app(self, restart=True):
        """关闭主窗口并结束进程。

        不论是「自动重启」还是「提示用户手动重开」，本进程都必须真正退出：
        一是让新版成为唯一实例，二是释放对旧版文件的文件锁（改名/清理都要它）。
        这里不弹模态框——模态框要等用户点击，会让本进程迟迟不退出。
        """
        master = self.master
        self._log("新版本已就位，程序即将自动重启…" if restart
                  else "新版本已就位，本程序退出后请重新运行。")
        try:
            master.destroy()
        except Exception:  # noqa: BLE001
            pass
        # 兜底：销毁窗口后 mainloop 通常已退出；若仍有残留线程/阻塞，强制退出
        def _force():
            try:
                master.quit()
            except Exception:  # noqa: BLE001
                pass
            os._exit(0)
        try:
            root = master.winfo_toplevel()
            root.after(400, _force)
        except Exception:  # noqa: BLE001
            _force()
