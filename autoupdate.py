#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autoupdate.py — Tkinter 应用通用自动更新模块（零第三方依赖，仅标准库）
=====================================================================

功能（2026-09-13 改为「就地更新」，供所有带更新功能的软件复用）：
    1. 启动后台检查 GitHub Latest Release（静默，失败不打扰）
    2. 发现新版本 → 弹窗显示「更新内容」（取 Release body，自动清理 markdown 符号）
       三个选择（统一扁平按钮：主按钮蓝底白字，次按钮灰底深字）：
         立即更新     → 下载进度对话框（进度条 / 速度 / 已下载大小 / 随时取消）
         本次忽略     → 本次关闭，下次启动继续检查
         以后不再提醒 → 配置文件写 auto_update=false，启动不再自动检查
    3. 下载完成 → 新版直接放进程序所在目录并接管原文件名，旧版由新版启动后删除
       （不生成 bat、不做「等进程退出」的轮询，见 windows_replace_and_restart）
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

import hashlib
import json
import os
import re
import shutil
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

# ---------------- 自有下载站（2026-09-22 新增） ----------------
# 起因：用户反馈从 GitHub 下载又慢又容易超时。本机搭了国内下载站
# （阿里云 47.116.64.26:8888），更新元数据与 exe 都同步过去，
# 检查更新与下载都优先走它，GitHub 只作兜底。
SITE_URL = "http://47.116.64.26:8888"

# GitHub 仓库 → 服务器上的 update.json 文件名（与下载服务器 REPOS 配置一致）
_REPO_TO_APP = {
    "jianry/dianzisongda-xiazaiqi": "court",
    "jianry/invoice-ocr-tool": "ocr",
    "jianry/invoice-qr-tool": "qr",
}


def _server_meta_url(api_url):
    """从 GitHub API 地址推出自有服务器上的 update.json 地址；认不出则返回 None。

    https://api.github.com/repos/<owner>/<repo>/releases/latest
        → http://47.116.64.26:8888/updates/<app>.json
    """
    m = re.search(r"repos/([^/]+/[^/]+)/releases", str(api_url or ""))
    if not m:
        return None
    key = _REPO_TO_APP.get(m.group(1).lower())
    return "%s/updates/%s.json" % (SITE_URL, key) if key else None


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
# 安装包（Setup / Installer / 安装版）绝不能当作自动更新的下载源：
# 就地更新会把它搬进程序目录并改名成主程序名，等于用安装器覆盖程序本体。
# 发布 Release 时会同时上传「绿色版 exe」和「安装版 exe」，必须显式区分。
_INSTALLER_HINTS = ("setup", "installer", "install", "安装")


def _looks_like_installer(name):
    n = str(name or "").lower()
    return any(h in n for h in _INSTALLER_HINTS)


def fetch_latest_release(api_url, timeout=15):
    """返回 dict(tag, notes, download_url)；无可用 exe 资产时 download_url 为 None。

    download_url 只会指向「绿色单文件版」exe：名字含 setup/installer/安装 的资产一律跳过
    （这类是安装包，交给用户手动下载安装，不能被自动更新消费）。
    若 Release 里只有安装包，则返回 None，让界面提示「未找到可下载的更新文件」，
    而不是把安装器当成新版程序下载下来。
    """
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    dl = None
    for a in data.get("assets", []):
        name = str(a.get("name", ""))
        if not name.lower().endswith(".exe"):
            continue
        if _looks_like_installer(name):
            continue
        dl = a.get("browser_download_url")
        break
    return {
        "tag": data.get("tag_name", ""),
        "notes": data.get("body", "") or "",
        "download_url": dl,
        "download_urls": [dl] if dl else [],
        "sha256": "",
        "html_url": data.get("html_url", ""),
        "source": "GitHub API",
    }


def _http_json(url, timeout=8):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _file_sha256(path):
    """算文件 SHA256，用于校验下载到的更新包完整（update.json 里带 sha256）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_update_info(latest_api_url, timeout=8, log_fn=None):
    """依次尝试「自有服务器 → GitHub API」，返回统一的更新信息 dict 或 None。

    自有服务器上的 update.json 由各仓库的发版脚本生成、随 Release 上传、
    再由服务器定时脚本抄到 /updates/<app>.json。它同时给出
        url          自有服务器直链（首选，国内快）
        fallback_url GitHub Release 直链（服务器没同步到 / 不可达时兜底）
    两级都失败返回 None —— 静默失败，绝不因为检查更新把软件卡住。
    """
    def _log(m):
        try:
            if log_fn:
                log_fn(m)
        except Exception:  # noqa: BLE001
            pass

    meta_url = _server_meta_url(latest_api_url)
    if meta_url:
        try:
            d = _http_json(meta_url, timeout)
            if isinstance(d, dict) and d.get("version") and d.get("url"):
                urls = [u for u in (d.get("url"), d.get("fallback_url")) if u]
                _log("已从自有服务器获取版本信息：v%s" % d["version"])
                return {
                    "tag": str(d["version"]).strip(),
                    "notes": d.get("notes") or "",
                    "download_url": urls[0],
                    "download_urls": urls,
                    "sha256": (d.get("sha256") or "").strip().lower(),
                    "html_url": d.get("release_url", ""),
                    "source": "自有服务器",
                }
        except Exception:  # noqa: BLE001
            pass

    return fetch_latest_release(latest_api_url, timeout=timeout)


# ---------------- 就地更新（Windows，不借助外部脚本） ----------------
# 事实依据（2026-09-13 实测，Windows 10/11）：程序运行期间，它自己的 exe 文件
#   · 不能删除        → PermissionError WinError 5
#   · 不能被覆盖/替换 → PermissionError WinError 5
#   · 但**可以被重命名**（同目录改名不受锁限制）
# 因此「删掉旧版本」这件事必须推迟到进程退出之后，由接管的新版本去完成。
# 这也是旧实现（生成 updater.bat + 轮询等待进程退出）失败的根源：轮询条件写错，
# 永远等不到，最后走超时分支什么都不做。


def _is_frozen():
    """是否以打包后的 exe 运行。开发态（python 跑 .py）绝不能碰 sys.executable 所在目录。"""
    return bool(getattr(sys, "frozen", False))


def _base_stem(stem):
    """去掉 _旧版[_数字] / _更新中[_数字] 后缀，还原出本程序的基准文件名。"""
    return re.sub(r"_(?:旧版(?:_\d+)?|更新中(?:_\d+)?)$", "", stem)


def _old_version_pattern(base_stem, ext):
    """只匹配本程序自己产生的旧版/中间文件，避免误删同目录其他文件。"""
    return re.compile(
        r"^%s_(?:旧版(?:_\d+)?|更新中(?:_\d+)?)%s$" % (re.escape(base_stem), re.escape(ext)),
        re.IGNORECASE,
    )


def cleanup_old_versions(log_fn=None, max_wait=8.0):
    """删除程序目录里遗留的「旧版 / 更新中」文件。

    只在打包运行时生效；开发态直接返回，不做任何事。
    命中规则严格限定为本程序自己的命名格式，且排除当前正在运行的自己。
    刚启动时旧进程可能还没完全退出（文件仍被锁），所以带重试。
    """
    if not _is_frozen():
        return []
    try:
        current = os.path.abspath(sys.executable)
    except Exception:  # noqa: BLE001
        return []
    return _purge(directory=os.path.dirname(current), current=current,
                  log_fn=log_fn, max_wait=max_wait)


def _purge(directory, current, log_fn=None, max_wait=8.0):
    """删除 directory 下所有「本程序旧版/中间」文件（排除 current）。"""
    stem, ext = os.path.splitext(os.path.basename(current))
    pat = _old_version_pattern(_base_stem(stem), ext)

    def _scan():
        found = []
        try:
            for name in os.listdir(directory):
                full = os.path.join(directory, name)
                if pat.match(name) and os.path.abspath(full).lower() != current.lower():
                    found.append(full)
        except OSError:
            pass
        return found

    removed, deadline = [], time.time() + max_wait
    while True:
        left = []
        for p in _scan():
            try:
                os.remove(p)
                removed.append(os.path.basename(p))
            except OSError:
                left.append(p)
        if not left or time.time() >= deadline:
            break
        time.sleep(0.6)

    if removed and log_fn:
        try:
            log_fn("已清理旧版本文件：%s" % "、".join(removed))
        except Exception:  # noqa: BLE001
            pass
    return removed


def settle_after_update(log_fn=None):
    """程序启动时调用：接管原文件名，并清掉更新过程留下的旧版文件。

    只在打包运行时生效。所谓「接管」：如果自己是以中间名（xxx_更新中.exe）
    启动的，就把旧的 xxx.exe 挪开、把自己改名为 xxx.exe，让用户看到的文件名始终不变。

    为什么新版要先用中间名启动，而不是直接顶替原文件名：
        实测（Windows 10/11 + PyInstaller onefile）**无法在「当前运行进程自己的
        映像路径」上启动新进程** —— 引导器进程起得来，但真实程序起不来，
        表现为「程序关了却没有新窗口」。换个名字或换个目录都能正常启动。
    """
    if not _is_frozen():
        return []
    try:
        current = os.path.abspath(sys.executable)
    except Exception:  # noqa: BLE001
        return []

    def _log(m):
        try:
            if log_fn:
                log_fn(m)
        except Exception:  # noqa: BLE001
            pass

    directory = os.path.dirname(current)
    stem, ext = os.path.splitext(os.path.basename(current))
    base = _base_stem(stem)
    final = os.path.join(directory, base + ext)

    # ① 接管文件名：自己叫中间名时，把旧版挪开（运行中的 exe 允许改名），自己顶上
    if os.path.abspath(current).lower() != os.path.abspath(final).lower():
        # 旧版名带时间戳：若沿用固定名（xxx_旧版.exe），一旦上次更新留下同名残留，
        # Windows 的 os.rename 会因「目标已存在」直接失败 → 接管失败 → 用户继续启动
        # 旧版本（症状就是「更新了却没变」）。带时间戳 + 冲突兜底可彻底避免。
        parked = os.path.join(
            directory, "%s_旧版_%s%s" % (base, time.strftime("%Y%m%d%H%M%S"), ext))
        if os.path.exists(parked):
            try:
                os.remove(parked)          # 自己上次的残留，能删就复用这个名字
            except OSError:
                parked = os.path.join(
                    directory, "%s_旧版_%d%s" % (base, int(time.time() * 1000), ext))
        try:
            if os.path.exists(final):
                os.rename(final, parked)
            os.rename(current, final)
            _log("已接管程序文件名：%s" % os.path.basename(final))
        except OSError as e:  # noqa: BLE001
            _log("暂未能接管文件名（旧版本可能仍在运行）：%s" % e)

    # ② 清掉所有旧版/中间残留（含刚被挪开的那份，旧进程退出后即可删除）
    return _purge(directory, current, log_fn=log_fn, max_wait=12.0)


def windows_replace_and_restart(new_exe_path, log_fn=None):
    """就地更新：把下载好的新 exe 放进程序目录，启动它，本进程退出。

    步骤（全在本进程内完成，不生成任何外部脚本、不做进程退出轮询）：
        ① 新 exe 搬进程序目录，暂用中间名 xxx_更新中.exe
        ② 以中间名启动它（**不能**用原文件名，见 settle_after_update 里的说明）
        ③ 本进程退出；新版启动后由 settle_after_update() 接管原文件名并删掉旧版

    new_exe_path: 下载好的新版 exe 路径（通常位于临时目录）。
    失败时抛 RuntimeError，调用方负责提示。下载失败时文件原封不动。
    """
    if not _is_frozen():
        raise RuntimeError(
            "当前是开发态运行（未打包），无法执行就地更新。请在打包后的 exe 中测试。")

    current = os.path.abspath(sys.executable)
    directory = os.path.dirname(current)
    stem, ext = os.path.splitext(os.path.basename(current))
    base = _base_stem(stem)
    staging = os.path.join(directory, "%s_更新中%s" % (base, ext))
    # 自己就是以中间名在运行（上次接管尚未完成）时，必须换个名字：
    # 否则会试图覆盖正在运行的自身文件，必然失败。
    if os.path.abspath(staging).lower() == current.lower():
        staging = os.path.join(
            directory, "%s_更新中_%d%s" % (base, int(time.time()) % 1000000, ext))

    def _log(msg):
        try:
            if log_fn:
                log_fn(msg)
        except Exception:  # noqa: BLE001
            pass

    # ① 新 exe 搬进程序目录（同分区是瞬时改名，跨分区则复制）
    if os.path.abspath(new_exe_path).lower() != os.path.abspath(staging).lower():
        try:
            if os.path.exists(staging):
                os.remove(staging)
        except OSError:
            pass
        try:
            shutil.move(new_exe_path, staging)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("无法把新版本写入程序目录（%s）：%s" % (directory, e))
    _log("新版本已放入程序目录：%s" % os.path.basename(staging))

    # ② 启动新版。用中间名（而非原文件名）启动：Windows 不允许在「当前进程自己的
    #    映像路径」上启动新进程，用原名会静默失败（引导器起来、真实程序起不来）。
    started = False
    if hasattr(os, "startfile"):
        try:
            try:
                os.startfile(staging, cwd=directory)
            except TypeError:          # 旧版 Python 没有 cwd 参数
                os.startfile(staging)
            started = True
        except Exception:  # noqa: BLE001
            started = False
    if not started:                     # 兜底：cmd start
        try:
            subprocess.Popen(["cmd", "/c", "start", "", staging], cwd=directory,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started = True
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "新版本已放入程序目录，但自动启动失败，请手动双击 %s：%s" % (staging, e))
    _log("已启动新版本（%s），本程序即将退出。" % os.path.basename(staging))


# ---------------- 窗口居中工具 ----------------
def _center_on(master, w, h):
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


# ---------------- 主入口 ----------------
def run_update_check(parent, app_name, current_version, latest_api_url, config_file,
                     install_helper=None, log_fn=None, manual=False,
                     on_before_install=None):
    """启动检查（后台线程）。manual=True 时失败/无更新也弹提示，且忽略 auto_update 开关。

    on_before_install: 可选回调。新版本已就位、程序即将退出前调用，
                       宿主可在此停掉后台任务 / 保存状态 / 销毁窗口，确保进程真正退出。
    """
    if install_helper is None:
        install_helper = windows_replace_and_restart

    if not manual and not _load_auto_update(config_file):
        return

    def _worker():
        try:
            info = fetch_update_info(latest_api_url, log_fn=log_fn)
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
        urls = info.get("download_urls") or ([info["download_url"]] if info.get("download_url") else [])
        if not urls:
            if manual:
                parent.after(0, lambda: messagebox.showinfo(
                    "检查更新", "发现新版本 %s，但未找到可下载的更新文件。" % tag))
            return

        # 有新版 → 主线程弹窗
        parent.after(0, lambda: UpdateDialog(
            parent, app_name=app_name, current_version=current_version,
            tag=tag, notes=info.get("notes", ""),
            download_urls=urls, sha256=info.get("sha256", ""),
            html_url=info.get("html_url", ""),
            config_file=config_file, install_helper=install_helper, log_fn=log_fn,
            on_before_install=on_before_install,
        ))

    threading.Thread(target=_worker, daemon=True).start()


# ---------------- 更新确认弹窗（三选项） ----------------
class UpdateDialog(tk.Toplevel):
    def __init__(self, master, app_name, current_version, tag, notes,
                 download_url=None, config_file=None, install_helper=None,
                 log_fn=None, on_before_install=None,
                 download_urls=None, sha256="", html_url=""):
        super().__init__(master)
        self.title("发现新版本 · %s" % app_name)
        self.configure(bg="#f4f6f8")
        self.resizable(False, True)
        try:
            self.transient(master)
            self.grab_set()  # 模态
        except Exception:
            pass
        self._download_urls = download_urls or ([download_url] if download_url else [])
        self._sha256 = sha256
        self._html_url = html_url
        self._download_url = self._download_urls[0] if self._download_urls else None
        self._app_name = app_name
        self._config_file = config_file
        self._install_helper = install_helper
        self._log_fn = log_fn
        self._on_before_install = on_before_install

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
        # 统一按钮样式：主按钮蓝色实底 + 白字加粗，次按钮浅灰 + 深字常规，
        # 三者字号 / 内边距 / 圆角观感一致（tk.Button 扁平化，ttk 在 vista 主题下改不了底色）
        def _mkbtn(parent, text, cmd, primary=False):
            return tk.Button(
                parent, text=text, command=cmd,
                font=("Microsoft YaHei", 10, "bold") if primary else ("Microsoft YaHei", 10),
                bg="#2563eb" if primary else "#e5e7eb",
                fg="#ffffff" if primary else "#1f2937",
                activebackground="#1d4ed8" if primary else "#d1d5db",
                activeforeground="#ffffff" if primary else "#1f2937",
                relief="flat", bd=0, cursor="hand2",
                padx=18 if primary else 14, pady=6,
            )

        _mkbtn(btns, "以后不再提醒", self._on_never).pack(side="left")
        _mkbtn(btns, "本次忽略", self._on_skip).pack(side="left", padx=(8, 0))
        _mkbtn(btns, "立即更新", self._on_update, primary=True).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_skip)
        self.update_idletasks()
        w, h = 560, max(340, self.winfo_reqheight())
        x, y = _center_on(master, w, h)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))

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
            download_urls=self._download_urls, sha256=self._sha256,
            install_helper=self._install_helper, log_fn=self._log_fn,
            on_before_install=self._on_before_install,
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

    def __init__(self, master, app_name, download_url=None, install_helper=None,
                 log_fn=None, on_before_install=None,
                 download_urls=None, sha256=""):
        super().__init__(master)
        self.title("正在下载更新 · %s" % app_name)
        self.configure(bg="#f4f6f8")
        self.resizable(False, False)
        try:
            self.transient(master)
            self.grab_set()
        except Exception:
            pass
        self._urls = [u for u in (download_urls or ([download_url] if download_url else [])) if u]
        self._url = self._urls[0] if self._urls else None
        self._sha256 = (sha256 or "").strip().lower()
        self._app_name = app_name
        self._install_helper = install_helper
        self._log_fn = log_fn
        self._on_before_install = on_before_install

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

        tk.Button(frm, text="取消更新", command=self._on_cancel,
                  font=("Microsoft YaHei", 10),
                  bg="#e5e7eb", fg="#1f2937",
                  activebackground="#d1d5db", activeforeground="#1f2937",
                  relief="flat", bd=0, cursor="hand2", padx=14, pady=5,
                  ).pack(anchor="e")

        self.update_idletasks()
        w = 460
        x, y = _center_on(master, w, 190)
        self.geometry("%dx%d+%d+%d" % (w, 190, x, y))

        threading.Thread(target=self._worker, daemon=True).start()
        self.after(self.POLL_MS, self._poll)

    # ---- 下载线程 ----
    def _worker(self):
        st = self._state
        tmp = os.path.join(tempfile.gettempdir(), "%s_更新.exe" % re.sub(r"\W+", "_", self._app_name))
        last_err = None
        # 多源依次尝试：自有服务器直链 → GitHub 直链（见 fetch_update_info）
        for idx, url in enumerate(self._urls, 1):
            if self._cancel_evt.is_set():
                break
            try:
                st["done"] = 0
                st["total"] = 0
                if idx > 1:
                    st["note"] = "源 %d/%d" % (idx, len(self._urls))
                req = urllib.request.Request(
                    url, headers={"User-Agent": UA, "Accept": "*/*"}, method="GET")
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
                if self._sha256 and not _file_sha256(tmp) == self._sha256:
                    raise IOError("文件校验失败（SHA256 不一致），已丢弃")
                st["result"] = tmp
                st["running"] = False
                return
            except Exception as e:  # noqa: BLE001
                if self._cancel_evt.is_set():
                    break
                last_err = e
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
        # 成功 → 交给宿主安装（默认：就地落位 + 启动新版）
        try:
            self._install_helper(done_path, self._log_fn)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("更新失败", "更新未完成：%s" % e)
            return
        # 新版已就位并启动。本进程必须真正退出：既避免两个实例同时存在，
        # 也为了释放旧版文件的锁，新版才能把它删掉。
        try:
            if self._on_before_install:
                self._on_before_install()
        except Exception:  # noqa: BLE001
            pass
        self._exit_app()

    def _exit_app(self):
        """关闭主窗口并结束进程。

        新版此刻已接管原文件名并启动，本进程必须真正退出：一是让新版成为唯一实例，
        二是释放对「旧版_<时间戳>.exe」的文件锁，新版才能删掉它。
        这里不弹模态框——模态框要等用户点击，会让本进程迟迟不退出。
        """
        master = self.master
        if self._log_fn:
            try:
                self._log_fn("新版本已就位，程序即将自动重启…")
            except Exception:  # noqa: BLE001
                pass
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
