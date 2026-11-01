#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autoupdate.py — 法院文书下载器 · 自动更新（共享内核适配层）

=====================================================================
⚠️ 本文件是**适配层**，不是实现。真正的逻辑在共享内核里：

    updater_core.py   纯逻辑（版本比较 / 源排序 / 元数据 / 校验 / 落位 / 清理）
    updater_gui.py    界面（更新弹窗 / 下载进度框）

这两个文件由 `_shared/updater/sync_updater.py` 从唯一权威源分发而来，
**逐字节相同**。改逻辑请改权威源再同步，不要直接改这里的副本。

本文件只做三件事：
    ① 声明本项目的专属配置（应用名、仓库、SSL 信任库…）
    ② 把共享内核的接口**原样转发**出去，保持旧调用方一行不改
    ③ 保留旧版独有的公开名（向后兼容）

对外接口（全部与旧版一致，调用方无需改动）：
    run_update_check / UpdateDialog / DownloadProgressDialog / _SwitchedSource
    fetch_update_info / fetch_latest_release / cleanup_old_versions
    settle_after_update / windows_replace_and_restart / apply_pending_rename
    write_cleanup_flag / read_and_consume_cleanup_flag / desired_base_name
    build_download_sources / rank_sources / probe_speed / parse_version …

接入方法：
    import autoupdate
    autoupdate.run_update_check(
        root, app_name="法院文书下载器", current_version="2.11.2",
        latest_api_url=GITHUB_API_LATEST, config_file=config_path(),
        install_helper=autoupdate.windows_replace_and_restart, log_fn=self.log_msg,
    )
"""

import os
import sys
import threading
import urllib.error
from tkinter import messagebox

import updater_core as _core
import updater_gui as _gui

# ================================================================ 本项目配置

APP_NAME = "法院文书下载器"
APP_KEY = "court"

# 信任库构造：系统库 + 随包内置公共 CA（叠加，不替换）。
# ⚠️ 本模块是**跨项目复用**的，别的项目可能没有 ssl_trust.py，
#    所以做成可选导入：缺失时退化为仅系统库（与旧行为一致），不影响使用。
# 为什么必须改：新装 Windows 常缺公共根证书（如 CFCA），
#    直接用 urlopen 不带 context 只能读系统库 → 检查更新/下载更新会报
#    CERTIFICATE_VERIFY_FAILED（与主程序访问法院站点是同一个根因）。
try:
    import ssl_trust
except ImportError:  # pragma: no cover
    ssl_trust = None

_SSL_CTX = None


def _ssl_context():
    """惰性构造并缓存 SSLContext（避免每次请求都重读证书库）。"""
    global _SSL_CTX
    if _SSL_CTX is None:
        if ssl_trust is not None:
            _SSL_CTX = ssl_trust.build_ssl_context()[0]
        else:
            import ssl
            _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


# 自有站地址不写死在公开仓库里。本机放 _endpoints.py；缺失则该源自动禁用。
try:
    from _endpoints import SITE_URL          # 本机私有，.gitignore 已排除
    SITE_URL = (SITE_URL or "").rstrip("/")
except Exception:
    SITE_URL = ""

# GitHub 仓库 → 服务器上的 update.json 文件名（与更新源 REPOS 配置一致）
_REPO_TO_APP = {
    "jianry/dianzisongda-xiazaiqi": "court",
    "jianry/invoice-ocr-tool": "ocr",
    "jianry/invoice-qr-tool": "qr",
}

GITHUB_API_LATEST = (
    "https://api.github.com/repos/jianRY/dianzisongda-xiazaiqi/releases/latest"
)

# 共享内核配置对象。四个项目的差异全部收敛在这一处。
CFG = _core.UpdaterConfig(
    site_url=SITE_URL,
    site_meta_path="updates/%s.json" % APP_KEY,
    site_file_path="files",
    repo="jianRY/dianzisongda-xiazaiqi",
    repo_to_app=_REPO_TO_APP,
    app_name=APP_NAME,
    asset_prefix="CourtDocDownloader",
    site_label="备用源",          # 本项目历史文案（界面/日志里显示自有站）
    trust_site_meta=True,
    require_sha256=True,
    # settle_after_update 的清理是否丢后台线程（各项目历史行为不同，见内核说明）
    settle_background=False,
    ssl_context=_ssl_context,
)

# ================================================================ 常量转发

UA = _core.UA
MIRROR_PREFIXES = _core.MIRROR_PREFIXES
API_MIRROR_PREFIXES = _core.API_MIRROR_PREFIXES
PROBE_BYTES = _core.PROBE_BYTES
PROBE_TIMEOUT = _core.PROBE_TIMEOUT
MIN_USEFUL_SPEED = _core.MIN_USEFUL_SPEED
SWITCH_SPEED = _core.SWITCH_SPEED
TRUSTED_META_HOSTS = _core.TRUSTED_META_HOSTS
INSTALLER_HINTS = _core.INSTALLER_HINTS
# 旧版模块级常量名（带下划线前缀），保留以兼容外部引用。
_INSTALLER_HINTS = INSTALLER_HINTS
VERSION_PARTS = _core.VERSION_PARTS
_CLEANUP_FLAG = _core.CLEANUP_FLAG
CORE_VERSION = _core.CORE_VERSION


# ================================================================ 函数转发
# 注意：这些是**薄包装**而不是直接赋值。旧版的调用方与测试会 monkeypatch
# 本模块的 _purge / _is_frozen / write_cleanup_flag，包装层保证补丁对
# 共享内核内的调用同样生效（直接 `from updater_core import _purge` 则不会）。

def parse_version(v=None):
    return _core.parse_version(v)


def version_greater(remote=None, local=None):
    return _core.version_greater(remote, local)


def _strip_md(text=None, limit=4000):
    return _core.strip_md(text, limit)


def _is_github_url(url=None):
    return _core.is_github_url(url)


def _mirror_variants(url=None):
    return _core.mirror_variants(url, CFG.mirrors)


def _source_label(url=None):
    return _core.source_label(url, CFG)


def _src_label(url=None):
    return _core.source_label(url, CFG)


def _is_mirror(url=None):
    return _core.is_mirror(url, CFG.mirrors)


def _src_rank(url=None):
    return _core.src_rank(url, CFG)


def _probe_speed(url=None, nbytes=PROBE_BYTES, timeout=PROBE_TIMEOUT):
    return _core.probe_speed(url, nbytes=nbytes, timeout=timeout, cfg=CFG)


def probe_speed(url=None, nbytes=PROBE_BYTES, timeout=PROBE_TIMEOUT):
    return _core.probe_speed(url, nbytes=nbytes, timeout=timeout, cfg=CFG)


def build_download_sources(urls=None):
    return _core.build_download_sources(urls, CFG)


def rank_sources(urls=None, timeout=PROBE_TIMEOUT, on_probe_done=None):
    return _core.rank_sources(urls, timeout=timeout, on_probe_done=on_probe_done, cfg=CFG)


def order_download_urls(urls=None, timeout=PROBE_TIMEOUT, log_fn=None):
    """把候选下载地址展开成「实测最快优先」的有序列表（旧接口名，保留）。"""
    def _log_speeds(speeds):
        if not log_fn:
            return
        try:
            log_fn("测速结果：" + "、".join(
                "%s %.0fKB/s" % (_src_label(u), speeds.get(u, 0.0)) for u in speeds))
        except Exception:  # noqa: BLE001
            pass

    return _core.rank_sources(
        _core.build_download_sources(urls, CFG), timeout=timeout,
        on_probe_done=_log_speeds, cfg=CFG)


def _site_base():
    return CFG.site_base


def _is_trusted_meta_url(url=None):
    return _core.is_trusted_meta_url(url, CFG)


def _is_downloadable(url=None):
    return _core.is_downloadable(url)


def _looks_like_exe(path=None):
    return _core.looks_like_exe(path)


def _http_json(url=None, timeout=8):
    return _core.http_json(url, timeout, CFG)


def _get_json(url=None, timeout=8):
    return _core.http_json(url, timeout, CFG)


def _looks_like_installer(name=None):
    return _core._looks_like_installer(name)


def _file_sha256(path=None):
    return _core.file_sha256(path)


def _sha256_from_notes(notes=None):
    return _core.sha256_from_notes(notes)


def _release_meta_url(api_url=None):
    return CFG.release_meta_url


def _server_meta_url(api_url=None):
    return CFG.site_meta_url


def fetch_latest_release(api_url=None, timeout=15):
    """走 GitHub API 取最新 Release（旧接口名，保留）。"""
    info = _core.fetch_latest_release(CFG, timeout=timeout)
    return info.to_dict() if info else None


def fetch_update_info(latest_api_url=None, timeout=5, log_fn=None):
    """取更新信息（旧接口名，保留）。返回 dict 或 None。

    ⚠️ 这里**必须走本模块的 _http_json**，不能直接调内核的 http_json：
       宿主与测试会 monkeypatch `autoupdate._http_json` 来打桩，
       直接调内核那份会把补丁绕过去（打桩失效 → 打到真实网络）。
    """
    def _log(m):
        try:
            if log_fn:
                log_fn(m)
        except Exception:  # noqa: BLE001
            pass

    for url, source in _meta_candidates():
        try:
            d = _http_json(url, timeout)
        except Exception:  # noqa: BLE001
            continue
        info = _core._parse_update_json(CFG, d, source)
        if info:
            _log("已从 %s 获取版本信息：v%s" % (source, info.version))
            return info.to_dict()

    try:
        info = _core.fetch_latest_release(CFG, timeout=10)
        if info:
            _log("已从 GitHub API 获取版本信息：v%s" % info.version)
            return info.to_dict()
    except Exception:  # noqa: BLE001
        pass
    return None


def _meta_candidates():
    return _core.meta_candidates(CFG)


# ---------------- 配置（auto_update / cleanup_old 开关） ----------------

def _load_auto_update(config_file=None):
    return _core.load_auto_update(config_file)


def _save_auto_update(config_file=None, enabled=None):
    return _core.save_auto_update(config_file, enabled)


def _load_cleanup_old(config_file=None):
    return _core.load_cleanup_old(config_file)


def _save_cleanup_old(config_file=None, enabled=None):
    return _core.save_cleanup_old(config_file, enabled)


# ---------------- 就地更新 ----------------

def _is_frozen():
    return _core.is_frozen()


def _base_stem(stem=None):
    return _core.base_stem(stem)


def _old_version_pattern(base_stem=None, ext=None):
    return _core.old_version_pattern(base_stem, ext)


def _old_version_pattern_multi(prefixes=None, ext=None):
    return _core.old_version_pattern_multi(prefixes, ext)


def desired_base_name(app_name=None, version=None, fallback=None):
    return _core.desired_base_name(app_name, version, fallback)


def _purge(directory=None, current=None, log_fn=None, max_wait=8.0,
           prefixes=None, cleanup_old=True):
    return _core.purge(directory=directory, current=current, log_fn=log_fn,
                       max_wait=max_wait, prefixes=prefixes,
                       cleanup_old=cleanup_old)


def cleanup_old_versions(log_fn=None, max_wait=8.0, app_name=None, cleanup_old=None):
    # ⚠️ 把本模块的 _purge / _is_frozen 原样传进内核：宿主与测试会 monkeypatch
    #    这两个名字，内核若用自己那份，补丁就被绕过了。
    return _core.cleanup_old_versions(log_fn=log_fn, max_wait=max_wait,
                                       app_name=app_name, cleanup_old=cleanup_old,
                                       purge_fn=_purge, is_frozen_fn=_is_frozen)


def write_cleanup_flag(directory=None, cleanup_old=None):
    return _core.write_cleanup_flag(directory, cleanup_old)


def read_and_consume_cleanup_flag(directory=None):
    return _core.read_and_consume_cleanup_flag(directory)


def _park_file(directory=None, path=None, ext=None):
    return _core.park_file(directory, path, ext)


def apply_pending_rename(log_fn=None):
    return _core.apply_pending_rename(log_fn)


def settle_after_update(log_fn=None, app_name=None, version=None, cleanup_old=None):
    """程序启动时调用：登记「退出时要改的文件名」，并清掉更新留下的旧版文件。"""
    # ⚠️ 钩子注入同 cleanup_old_versions：让宿主的 monkeypatch 能穿透到内核。
    return _core.settle_after_update(log_fn=log_fn, app_name=app_name,
                                     version=version, cleanup_old=cleanup_old,
                                     background=CFG.settle_background,
                                     purge_fn=_purge, is_frozen_fn=_is_frozen)


def windows_replace_and_restart(new_exe_path=None, log_fn=None, cleanup_old=True):
    return _core.windows_replace_and_restart(new_exe_path, log_fn=log_fn,
                                             cleanup_old=cleanup_old,
                                             app_name=APP_NAME)


def send_to_recycle_bin(path=None):
    return _core.send_to_recycle_bin(path)


# 待改名状态：旧版是模块级变量，外部（含测试）会读写它。
# ⚠️ 重构后状态住在共享内核里 ⇒ 用**状态代理**把本模块的读写直通过去，
#    不能在这里留静态副本（那样外部读到的永远是 None，写复位也清不掉内核真状态）。
_self = sys.modules.get(__name__)
if _self is not None:
    _core.install_state_proxy(_self)
else:
    # 少数自定义 loader 不把模块登记进 sys.modules ⇒ 退化成「只读转发」
    def __getattr__(name):  # noqa: N807  (PEP 562)
        if name in _core.STATE_PROXY_NAMES:
            return getattr(_core, name)
        raise AttributeError(name)


# ================================================================ GUI 转发
# ⚠️ 用**子类**而不是直接赋值：GUI 层写跨进程标记时要调用**本模块**的
#    write_cleanup_flag，这样宿主/测试 monkeypatch 本模块的名字才会生效。

class UpdateDialog(_gui.UpdateDialog):
    """更新确认弹窗（旧接口名保留）。"""

    def _default_write_flag_fn(directory, cleanup_old):
        return write_cleanup_flag(directory, cleanup_old)

    def __init__(self, master=None, app_name=None, current_version=None,
                 tag=None, notes=None, download_url=None, config_file=None,
                 install_helper=None, log_fn=None, on_before_install=None,
                 download_urls=None, sha256="", html_url="",
                 cleanup_old_default=True):
        super().__init__(
            master, app_name=app_name, current_version=current_version,
            tag=tag, notes=notes, download_url=download_url,
            config_file=config_file, install_helper=install_helper,
            log_fn=log_fn, on_before_install=on_before_install,
            download_urls=download_urls, sha256=sha256, html_url=html_url,
            cleanup_old_default=cleanup_old_default,
            cfg=CFG, write_flag_fn=None)


class DownloadProgressDialog(_gui.DownloadProgressDialog):
    """下载进度框（旧接口名保留）。"""

    def _default_write_flag_fn(directory, cleanup_old):
        return write_cleanup_flag(directory, cleanup_old)

    def __init__(self, master=None, app_name=None, download_url=None,
                 install_helper=None, log_fn=None, on_before_install=None,
                 download_urls=None, sha256="", cleanup_old=True):
        super().__init__(
            master, app_name=app_name, download_url=download_url,
            install_helper=install_helper, log_fn=log_fn,
            on_before_install=on_before_install, download_urls=download_urls,
            sha256=sha256, cleanup_old=cleanup_old,
            cfg=CFG, write_flag_fn=None)


_SwitchedSource = _gui._SwitchedSource


def _u(master=None, v=None):
    return _gui.scale_px(master, v)


def _center_on(master=None, w=None, h=None):
    return _gui.center_on(master, w, h)


# ================================================================ 主入口


def run_update_check(parent=None, app_name=None, current_version=None,
                     latest_api_url=None, config_file=None, install_helper=None,
                     log_fn=None, manual=False, on_before_install=None,
                     cleanup_old=None):
    """启动检查（后台线程）。manual=True 时失败/无更新也弹提示，且忽略 auto_update 开关。

    on_before_install: 可选回调。新版本已就位、程序即将退出前调用，
                       宿主可在此停掉后台任务 / 保存状态 / 销毁窗口，确保进程真正退出。

    cleanup_old: 「更新后是否删除旧版本文件」复选框的**默认勾选状态**。
                 None（默认）→ 读上次的记录；没有记录则默认勾选（保持原行为）。
                 用户在弹窗里改了选择后，新选择会被记住并用于下次。
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

        if not info:
            if manual:
                parent.after(0, lambda: messagebox.showinfo(
                    "检查更新", "检查更新失败，请稍后重试或前往 GitHub 手动下载。"))
            return

        tag = info.get("tag", "")
        if not tag or not version_greater(tag, current_version):
            if manual:
                parent.after(0, lambda: messagebox.showinfo(
                    "检查更新", "已是最新版本 v%s。" % current_version))
            return

        urls = info.get("download_urls") or (
            [info["download_url"]] if info.get("download_url") else [])
        if not urls:
            if manual:
                parent.after(0, lambda: messagebox.showinfo(
                    "检查更新", "发现新版本 %s，但未找到可下载的更新文件。" % tag))
            return
        # 没有 sha256 → 元数据不可信（多半是只有 GitHub API 兜底成功）。
        # 自动安装这条路上，sha256 是唯一的完整性依据，缺了就不装。
        if not (info.get("sha256") or "").strip():
            if log_fn:
                try:
                    log_fn("⚠ 更新元数据缺少 sha256，已拒绝自动更新（防投毒）")
                except Exception:  # noqa: BLE001
                    pass
            if manual:
                msg = ("发现新版本 %s，但本次更新信息缺少完整性校验值，"
                       "出于安全考虑不会自动安装。\n\n"
                       "请前往 GitHub Releases 手动下载。" % tag)
                parent.after(0, lambda m=msg: messagebox.showwarning("检查更新", m))
            return

        # 有新版 → 主线程弹窗
        # cleanup_old 默认值：宿主显式传 > 配置文件里的上次选择 > 默认勾选
        default_cleanup = (_load_cleanup_old(config_file) if cleanup_old is None
                           else bool(cleanup_old))
        parent.after(0, lambda: UpdateDialog(
            parent, app_name=app_name, current_version=current_version,
            tag=tag, notes=info.get("notes", ""),
            download_urls=urls, sha256=info.get("sha256", ""),
            html_url=info.get("html_url", ""),
            config_file=config_file, install_helper=install_helper, log_fn=log_fn,
            on_before_install=on_before_install,
            cleanup_old_default=default_cleanup,
        ))

    threading.Thread(target=_worker, daemon=True).start()
