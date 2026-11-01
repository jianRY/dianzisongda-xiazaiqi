#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""updater_core.py — 跨项目共享的自动更新内核（唯一权威源）

=====================================================================
本文件是**四个软件共用**的更新内核的**唯一真源**：

    · 发票二维码识别下载工具（发票处理/iqr_update.py）
    · 发票识别汇总（InvoiceOcrTool/autoupdate.py）
    · 法院文书下载器（诉讼案件网站/autoupdate.py）
    · 图片表格转 Excel 助手（杂项/img22excel/autoupdate.py）

⚠️⚠️ **改这个文件之前先读完这段** ——————————————————————
四个项目是**四个独立的 Git 仓库**，各自打包、各自发版，没有共同的
Python 包。因此共享内核采用「**单一权威源 + 逐字节副本**」的落地方式：

    权威源   <共享目录>/updater/updater_core.py             ← 只改这里
    副本     <各项目>/updater_core.py                        ← 由同步脚本写入

副本与权威源必须**逐字节相同**（sha256 一致）。改动流程：

    1. 只改权威源：_shared/updater/updater_core.py
    2. 跑同步：  python _shared/updater/sync_updater.py
    3. 跑校验：  python _shared/updater/check_updater_sync.py
    4. 各项目各自回归 + 各自发版（版本号互不影响）

绝不要直接改某个项目里的副本 —— 下次同步会被覆盖回去。

——————————————————————————————————————————————

设计边界（为什么内核里没有 GUI、也没有 requests）：

    · **零第三方依赖**：只用标准库。发票工具用 requests，但那是它自己的
      下载器（iqr_net）的事；本内核不引入 requests，否则另外三个项目
      的打包体积会平白变大。
    · **不 import tkinter**：内核是纯逻辑。GUI 那一层在 updater_gui.py。
    · **配置外置**：站点地址、仓库、镜像列表、UA 全部走 UpdaterConfig，
      不在核心里写死 —— 四个项目的这些值本来就不同。

安全基线（以「法院文书下载器」版为准，那是四者中最严格的一版）：

    · 元数据只从**可信 HTTPS 源**取（GitHub 原站 / api.github.com /
      自建 https 站）；加速镜像只用于下载 exe 字节，不作元数据源。
      原因：镜像同时给出 exe 与 sha256，被控就能两者一起换，校验等于没有。
    · sha256 **强制**：自动安装路径上拿不到校验值就拒绝更新。
    · 下载物必须以 MZ 开头（防止把 HTML 错误页当 exe 装上）。
    · 下载地址 scheme 白名单（挡掉 file:// 这类「读本地任意文件」）。

接口约定：

    check_update(cfg, ...)          -> UpdateInfo | None
    download_update(cfg, urls, ...) -> 本地临时文件路径（失败抛异常）
    install_update(cfg, exe, ...)   -> 落位 + 启动新版 + 旧版回收

三个入口（core / gui / 项目适配层）都只调用上面这三个，
保证四个软件的更新行为**逐字节一致**。
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

__all__ = [
    "UpdaterConfig", "UpdateInfo",
    "parse_version", "version_greater", "strip_md",
    "is_github_url", "mirror_variants", "source_label", "src_rank",
    "is_mirror", "build_download_sources", "probe_speed", "rank_sources",
    "is_trusted_meta_url", "is_downloadable", "looks_like_exe",
    "file_sha256", "sha256_from_notes",
    "http_json", "fetch_latest_release", "fetch_update_info",
    "api_url_candidates", "resolve_ssl_context",
    "install_state_proxy", "STATE_PROXY_NAMES",
    "is_frozen", "base_stem", "old_version_pattern", "old_version_pattern_multi",
    "desired_base_name", "cleanup_old_versions", "purge",
    "write_cleanup_flag", "read_and_consume_cleanup_flag",
    "park_file", "apply_pending_rename", "settle_after_update",
    "windows_replace_and_restart", "launch_replaced_version",
    "send_to_recycle_bin",
    "load_auto_update", "save_auto_update", "load_cleanup_old", "save_cleanup_old",
    "load_auto_update_cfg", "save_auto_update_cfg",
]

# 内核版本号：改动内核后 +1，同步脚本据此判断副本是否过期。
CORE_VERSION = "1.0.0"

# ---------------- 常量 ----------------
UA = (
    "Mozilla/5.0 (Windows NT 10.11; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 公共 GitHub 加速镜像。⚠️ 第三方服务，随时可能失效，且列表**硬编码在客户端**
# （靠发版换源），不写进 update.json。
MIRROR_PREFIXES = (
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
)

# 能代理 **API 查询** 的镜像（与下载用的不是同一批，别混用）。
# 实测：只有 gh-proxy.com 能代理 api.github.com，另两家一律 403。
API_MIRROR_PREFIXES = ("https://gh-proxy.com/",)

PROBE_BYTES = 256 * 1024        # 探测读取的字节数（探活，不是精确测速）
PROBE_TIMEOUT = 3               # 单源探测最长等待秒数
MIN_USEFUL_SPEED = 100 * 1024   # 探测门限：低于此值视为「病源」，降级到队尾
SWITCH_SPEED = 50 * 1024        # 下载看门狗门限：连续 8 秒低于此值就换源续传

# 更新链的信任边界：元数据只允许来自这些主机。
TRUSTED_META_HOSTS = ("github.com", "www.github.com", "api.github.com",
                      "objects.githubusercontent.com")

# 安装包（Setup / Installer / 安装版）绝不能当自动更新的下载源：
# 就地更新会把它搬进程序目录并改名成主程序名，等于用安装器覆盖程序本体。
INSTALLER_HINTS = ("setup", "installer", "install", "安装")

VERSION_PARTS = 3

# 「更新后是否删除旧版」的跨进程标记文件名（落在程序目录）。
CLEANUP_FLAG = "_更新保留旧版.flag"


# ============================================================== 配置


class UpdaterConfig(object):
    """一次更新所需的全部项目特定配置。

    四个项目的差异全部收敛到这里，内核本身不含任何项目专属常量。

    site_url        自有下载站地址（空串 = 未配置，该源自动禁用）。
                    末尾斜杠可有可无，内部统一 rstrip("/")。
    site_meta_path  自有站上的 update.json 相对路径，如 "updates/qr.json"。
    site_file_path  自有站上的 exe 相对路径前缀，如 "files"（可空）。
    repo            GitHub 仓库 "owner/name"，用于推导 API / 附件地址。
    repo_to_app     GitHub 仓库 → 自有站 update.json 文件名（不含 .json）。
                    例：{"jianry/invoice-qr-tool": "qr"}
    app_name        应用名（临时文件命名、界面显示）。
    asset_prefix    绿色版 exe 的文件名前缀（小写、去符号后比较）。
                    例："InvoiceQRDownloader"；空 = 只按 .exe 匹配。
    user_agent      HTTP UA。
    mirrors         加速镜像前缀列表（默认用上面的 MIRROR_PREFIXES）。
    trust_site_meta 自有站是否可作**元数据**源。
                    True  = 自有站上了 TLS，可信（大多数项目）。
                    False = 自有站只作下载兜底，不给 sha256 作保。
    require_sha256  自动安装路径是否强制校验 sha256。
                    True  = 缺校验值就拒绝自动更新（安全基线，推荐）。
                    False = 老项目的老行为（缺值时跳过校验）。
    extra_meta_urls 额外的元数据地址（url, 标签），排在推导出来的候选之前。
    """

    def __init__(self, **kw):
        self.site_url = (kw.get("site_url") or "").rstrip("/")
        # 自有站在界面/日志里的显示名。⚠️ 各项目历史文案不同：
        #   法院/发票汇总 = 「备用源」；图片转 Excel = 「自有服务器」；
        #   发票二维码/工具箱 = 空（显示域名本身）。
        # 留空 ⇒ 与旧版一致（回落到域名）。
        self.site_label = kw.get("site_label") or ""
        self.site_meta_path = kw.get("site_meta_path") or ""
        self.site_file_path = (kw.get("site_file_path") or "").strip("/")
        self.repo = (kw.get("repo") or "").strip("/")
        self.repo_to_app = dict(kw.get("repo_to_app") or {})
        self.app_name = kw.get("app_name") or ""
        self.asset_prefix = kw.get("asset_prefix") or ""
        self.user_agent = kw.get("user_agent") or UA
        self.mirrors = tuple(kw.get("mirrors") or MIRROR_PREFIXES)
        self.trust_site_meta = bool(kw.get("trust_site_meta", True))
        self.require_sha256 = bool(kw.get("require_sha256", True))
        # settle_after_update 的清理是否丢后台线程。
        # ⚠️ 各项目历史不同：发票识别汇总 = True（P0 回归：清理最多重试 12 秒，
        #    放主线程会把界面冻住）；法院/图片转Excel/工具箱 = False（旧行为，
        #    同步清理，调用方返回时旧版文件已清干净）。
        self.settle_background = bool(kw.get("settle_background", False))
        self.extra_meta_urls = list(kw.get("extra_meta_urls") or [])
        # ssl_context 由调用方注入（法院项目有 ssl_trust.py，其余项目没有）。
        # ⚠️ 可以传 **ssl.SSLContext 对象**，也可以传 **惰性构造的工厂函数**
        #    （法院项目传函数：_ssl_context()，避免每次请求都重读证书库）。
        self.ssl_context = kw.get("ssl_context")
        # config_file：auto_update / cleanup_old 两个开关的持久化位置。
        self.config_file = kw.get("config_file") or ""

    # ---- 派生地址 ----
    @property
    def site_base(self):
        """自有站地址（无尾斜杠）。"""
        return self.site_url

    @property
    def site_meta_url(self):
        if not (self.site_url and self.site_meta_path):
            return ""
        return "%s/%s" % (self.site_url, self.site_meta_path.lstrip("/"))

    @property
    def site_files_url(self):
        if not (self.site_url and self.site_file_path):
            return ""
        return "%s/%s" % (self.site_url, self.site_file_path.lstrip("/"))

    def site_file_url(self, asset):
        """自有站上的文件直链：<SITE_URL>/<site_file_path>/<资产名>。

        asset 缺失（或本项目未配置 site_file_path）返回 None。
        为什么需要它：update.json 是**随 Release 公开的资产**，不能把站内地址
        写进去，所以线上那份文件里没有 fallback_url。只看 update.json 的 url，
        一旦 GitHub 与所有加速镜像同时不通，就会出现「拿到了版本号却下不到
        文件」—— 自有站白搭。这里用 asset 名自己拼一条兜底直链。
        """
        a = str(asset or "").strip()
        if not (a and self.site_files_url):
            return None
        import urllib.parse
        return "%s/%s" % (self.site_files_url, urllib.parse.quote(a))

    @property
    def api_latest_url(self):
        if not self.repo:
            return ""
        return "https://api.github.com/repos/%s/releases/latest" % self.repo

    @property
    def release_meta_url(self):
        """Release 附件里的 update.json（带 sha256/size，与版本严格同步）。"""
        if not self.repo:
            return ""
        return "https://github.com/%s/releases/latest/download/update.json" % self.repo

    def repo_app_key(self):
        """本仓库在自有站上对应的 update.json 文件名（不含 .json）。"""
        if not self.repo:
            return ""
        return self.repo_to_app.get(self.repo.lower(), "")


# ============================================================== 数据


class UpdateInfo(object):
    """一次检查更新的结果。四个项目的对外字典接口也由此派生。"""

    __slots__ = ("version", "notes", "download_urls", "sha256", "html_url", "source")

    def __init__(self, version="", notes="", download_urls=None,
                 sha256="", html_url="", source=""):
        self.version = str(version or "").strip()
        self.notes = notes or ""
        self.download_urls = [u for u in (download_urls or []) if u]
        self.sha256 = (sha256 or "").strip().lower()
        self.html_url = html_url or ""
        self.source = source or ""

    def to_dict(self):
        """转成旧版 dict 形状（tag / download_url / download_urls …）。"""
        return {
            "tag": self.version,
            "version": self.version,
            "notes": self.notes,
            "download_url": self.download_urls[0] if self.download_urls else None,
            "download_urls": list(self.download_urls),
            "sha256": self.sha256,
            "html_url": self.html_url,
            "source": self.source,
        }

    @property
    def parsed_version(self):
        return parse_version(self.version)


# ============================================================== 版本比较


def parse_version(v):
    """'v2.9.0' / '2.7' → 可比较的数字元组（不足 3 段补 0）。

    · **只取前 3 段数字**，缺失段补 0；非数字字符全部跳过。
    · 数字段从任意位置抽取（`re.findall(r"\\d+")`），因此**附件文件名**
      （InvoiceQRDownloader_5.9.0.exe）也能解析出版本号 ——
      发版脚本按「文件名含版本号」匹配 Release 资产，全靠这一点。
      ⚠️ 早期实现用 `split(".")` 逐段取首数字，对文件名会得到 (0, 9, 0)，
      导致资产版本永远匹配不上 → 走 GitHub API 兜底时找不到 exe。
    · **必须补零**：早期 tag 是两段式（v2.7），新标准是三段式（v2.9.0）。
      不补零的话 Python 的元组比较会认为 (2,7) < (2,7,0) —— 把「同一个版本」
      判成「有新版本」，弹出一个永远升不掉的更新提示。
    """
    digits = re.findall(r"\d+", str(v or ""))
    nums = [int(x) for x in digits[:VERSION_PARTS]]
    while len(nums) < VERSION_PARTS:
        nums.append(0)
    return tuple(nums)


def version_greater(remote, local):
    """远端版本是否比本地新。"""
    try:
        return parse_version(remote) > parse_version(local)
    except Exception:
        return False


def strip_md(text, limit=4000):
    """极简 markdown 清理（弹窗显示用）。"""
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


# ============================================================== 下载源


def is_github_url(url):
    """是否为 GitHub 上的地址（只有这类才值得拼加速镜像前缀）。"""
    u = str(url or "")
    return "github.com/" in u or "githubusercontent.com/" in u


def is_mirror(url, mirrors=None):
    """该地址是否为加速镜像。"""
    for p in (mirrors or MIRROR_PREFIXES):
        if str(url or "").startswith(p):
            return True
    return False


def mirror_variants(url, mirrors=None):
    """给一个 GitHub 直链生成全部镜像版本；非 GitHub 链接返回空列表。"""
    if not is_github_url(url):
        return []
    return [p + url for p in (mirrors or MIRROR_PREFIXES)]


def src_rank(url, cfg=None):
    """源的优先级别（仅在实测速度都达标时作并列排序的次要依据）：
    0 = 加速镜像 → 1 = GitHub 直连 → 2 = 自有站（永远兜底）。"""
    u = str(url or "")
    mirrors = (cfg.mirrors if cfg is not None else MIRROR_PREFIXES)
    if is_mirror(u, mirrors):
        return 0
    if "github.com/" in u:
        return 1
    return 2


def source_label(url, cfg=None):
    """给人看的源名，用于进度框显示。"""
    u = str(url or "")
    mirrors = (cfg.mirrors if cfg is not None else MIRROR_PREFIXES)
    for p in mirrors:
        if u.startswith(p):
            return "加速镜像 %s" % p.split("//")[1].strip("/")
    site = (cfg.site_base if cfg is not None else "")
    if site and u.startswith(site):
        # ⚠️ 自有站的显示名由各项目配置（site_label），
        #    留空则沿用旧版行为：直接显示域名。
        lab = (getattr(cfg, "site_label", "") or "") if cfg is not None else ""
        if lab:
            return lab
        try:
            return u.split("//")[1].split("/")[0]
        except IndexError:
            return u[:30]
    if is_github_url(u):
        return "GitHub 原站"
    try:
        return u.split("//")[1].split("/")[0]
    except IndexError:
        return u[:30]


def build_download_sources(urls, cfg=None):
    """把候选下载地址展开成有序列表：加速镜像 → GitHub 直链 → 自有站兜底。

    GitHub 直链会额外派生出镜像版本（同一文件，走 CDN），原链保留在最后：
    国内实测直连 4 KB/s，只能当万不得已的兜底。
    """
    mirrors_cfg = (cfg.mirrors if cfg is not None else MIRROR_PREFIXES)
    mirrors, others, gh = [], [], []
    for u in (urls or []):
        u = (u or "").strip()
        if not u:
            continue
        # scheme 白名单：挡掉 file:// / ftp:// 这类「让远端指定读本地哪个文件」的地址
        if not is_downloadable(u):
            continue
        if is_github_url(u):
            for m in mirror_variants(u, mirrors_cfg):
                if m not in mirrors:
                    mirrors.append(m)
            if u not in gh:
                gh.append(u)
        elif u not in others:
            others.append(u)
    return mirrors + gh + others


def resolve_ssl_context(cfg=None):
    """取 cfg.ssl_context，得到可直接传给 urlopen(context=) 的 SSLContext。

    ⚠️ 支持两种传法：
      ① 直接给 ssl.SSLContext 对象；
      ② 给**惰性构造的工厂函数**（法院项目传 `ssl_trust` 的惰性构造函数，
         避免每次请求都重读证书库）。

    工厂函数调用失败（证书文件缺失等）时返回 None ⇒ 退回系统默认信任库，
    绝不因为证书问题让整个更新链路崩掉。
    """
    if cfg is None:
        return None
    ctx = getattr(cfg, "ssl_context", None)
    if ctx is None:
        return None
    if callable(ctx):
        try:
            ctx = ctx()
        except Exception:  # noqa: BLE001
            return None
    return ctx


def probe_speed(url, nbytes=PROBE_BYTES, timeout=PROBE_TIMEOUT, cfg=None):
    """拉一小段（Range）探活测速，返回 KB/s；失败返回 0.0。绝不抛异常。

    只读 256KB 就断开，几十 MB 的包不会因为测速被白下。
    """
    import urllib.request

    ua = (cfg.user_agent if cfg is not None else UA)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ua, "Accept": "*/*", "Range": "bytes=0-%d" % (nbytes - 1)},
        method="GET",
    )
    try:
        t0 = time.monotonic()
        n = 0
        ctx = resolve_ssl_context(cfg)
        if ctx is not None:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout)
        with resp:
            while n < nbytes:
                buf = resp.read(65536)
                if not buf:
                    break
                n += len(buf)
                if time.monotonic() - t0 > timeout:
                    break
        dt = time.monotonic() - t0
        if n <= 0 or dt <= 0:
            return 0.0
        return n / 1024.0 / dt
    except Exception:  # noqa: BLE001
        return 0.0


def rank_sources(urls, timeout=PROBE_TIMEOUT, on_probe_done=None, cfg=None):
    """并发探活后排出尝试顺序。

    排序规则（刻意不做「全局按速度排序」—— 小样本测不出 CDN 镜像的真实能力，
    而且会让自有站在偶尔测速偏高时插到镜像前面，违背「镜像优先、本站兜底」）：
      组优先：0 = 加速镜像 → 1 = GitHub 直连 → 2 = 自有站（**永远兜底**）
      组内：① 健康的（达到 MIN_USEFUL_SPEED）在前；
            ② 再按探测速度降序；
            ③ 最后按原顺序，保证结果稳定可复现。
    探测失败 / 过慢的源都**不丢弃**，只是排到本组末尾，最后仍会试一次。
    """
    urls = [u for u in (urls or []) if u]
    if len(urls) <= 1:
        return list(urls)

    speeds = {}
    lock = threading.Lock()

    def _one(u):
        s = probe_speed(u, timeout=timeout, cfg=cfg)
        with lock:
            speeds[u] = s

    threads = [threading.Thread(target=_one, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 2)

    if on_probe_done:
        try:
            on_probe_done(speeds)
        except Exception:  # noqa: BLE001
            pass

    def _key(item):
        idx, u = item
        s = speeds.get(u, 0.0)
        healthy = s >= MIN_USEFUL_SPEED / 1024.0
        return (
            src_rank(u, cfg),         # 组：0 加速镜像 → 1 GitHub 原站 → 2 自有站
            0 if healthy else 1,      # 组内：健康的在前，探测失败的排后（但不丢弃）
            -s,                       # 组内再按实测速度降序
            idx,                      # 最后按原顺序，保证结果稳定可复现
        )

    return [u for _, u in sorted(enumerate(urls), key=_key)]


# ============================================================== 安全闸门


def is_trusted_meta_url(url, cfg=None):
    """这个地址能否作为 update.json 的可信来源（决定要不要自动更新）。"""
    import urllib.parse
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except Exception:  # noqa: BLE001
        return False
    if p.scheme.lower() != "https":
        return False
    host = (p.hostname or "").lower()
    if host in TRUSTED_META_HOSTS:
        return True
    # 自建站：只有上了 TLS 且项目允许时才配当元数据源。
    site = (cfg.site_base if cfg is not None else "")
    if site and cfg is not None and cfg.trust_site_meta:
        try:
            sp = urllib.parse.urlsplit(site)
            if host == (sp.hostname or "").lower() and sp.scheme.lower() == "https":
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def is_downloadable(url):
    """下载地址的 scheme 白名单。

    防的是 update.json 被篡改成 file:// / ftp:// 之类的地址 —— urllib 对 file://
    是照单全收的，那等于让远端指定「读本地哪个文件当程序装」。
    http 允许：完整性由可信源给的 sha256 兜底。
    """
    import urllib.parse
    try:
        return urllib.parse.urlsplit(str(url or "")).scheme.lower() in ("https", "http")
    except Exception:  # noqa: BLE001
        return False


def looks_like_exe(path):
    """粗校验：Windows 可执行文件必须以 MZ 开头。

    防的是「拿到一个 html 错误页 / 压缩包，却当成新版本装进程序目录」。
    """
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def file_sha256(path):
    """算文件 SHA256（小写十六进制），用于校验下载到的更新包完整。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Release 正文里写 sha256 的约定格式，由发版脚本自动追加，例如：
#     SHA256: 90f1ed78078ddbd1a1b2c3...
_SHA256_IN_BODY_RE = re.compile(r"SHA256[:\s]+([0-9a-fA-F]{64})")


def sha256_from_notes(notes):
    """从 Release 正文里抠出 SHA256（小写）；没有则返回空串。"""
    m = _SHA256_IN_BODY_RE.search(notes or "")
    return m.group(1).lower() if m else ""


# ============================================================== 元数据


def http_json(url, timeout=8, cfg=None):
    """读一份 JSON。只允许可信 HTTPS 源。

    兜底闸门放在这一层：将来谁再往候选列表里塞镜像地址也会被直接拒掉。
    """
    import urllib.request
    if not is_trusted_meta_url(url, cfg):
        raise ValueError("拒绝从不信任的来源读取更新元数据：%s"
                         % source_label(url, cfg))
    ua = (cfg.user_agent if cfg is not None else UA)
    req = urllib.request.Request(
        url, headers={"User-Agent": ua, "Accept": "application/json"}, method="GET")
    ctx = resolve_ssl_context(cfg)
    if ctx is not None:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    else:
        resp = urllib.request.urlopen(req, timeout=timeout)
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def _looks_like_installer(name):
    n = str(name or "").lower()
    return any(h in n for h in INSTALLER_HINTS)


def _match_asset(cfg, asset, version, strict=True):
    """按项目规则挑出「绿色版 exe」资产（跳过安装包）。

    asset_prefix 非空时要求文件名（去符号后）以它开头；
    strict=True 时还要求**版本号一致**，避免抓到别的版本。

    ⚠️ strict 只用于「第一轮挑选」：万一某个项目的资产名里不带版本号，
    严格匹配会一个都挑不出来 ⇒ 整个更新通道断掉（用户永远看不到更新）。
    ⇒ 挑不到时还有一轮「放宽到只看前缀」的兜底（见 fetch_latest_release）。
    """
    name = str(asset.get("name", ""))
    if not name.lower().endswith(".exe"):
        return False
    if _looks_like_installer(name):
        return False
    prefix = (cfg.asset_prefix if cfg is not None else "")
    if prefix:
        flat = re.sub(r"\W", "", name).lower()
        if not flat.startswith(prefix.lower()):
            return False
        if strict and version and parse_version(name) != parse_version(version):
            return False
    return True


def api_url_candidates(cfg):
    """GitHub API 的查询候选：直连优先，再试能代理 API 的加速镜像。

    实测（2026-09-24）：api.github.com 直连在国内本来就能通（约 0.8s），所以直连
    排第一；镜像只作兜底 —— 多数 gh-proxy 系镜像**不代理 API**
    （实测 ghfast.top / ghproxy.net 对 api.github.com 一律 403）。
    """
    base = (cfg.api_latest_url if cfg is not None else "")
    out, seen = [], set()
    for u in [base] + [p + base for p in API_MIRROR_PREFIXES]:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_latest_release(cfg, timeout=15):
    """走 GitHub API 取最新 Release。

    返回 UpdateInfo 或 None。sha256 取自 asset 的 digest 字段
    （GitHub 服务端算的内容摘要，形如 "sha256:..."）。
    为什么重要：这条路径走 api.github.com，比 github.com 稳定得多；有了 digest，
    「取不到 update.json 就没法安全更新」的死路就通了 —— 两条可信路径都能给校验值。

    依次尝试 api_url_candidates()：直连优先，能代理 API 的镜像兜底。
    """
    import urllib.request
    data = None
    last_err = None
    for u in api_url_candidates(cfg):
        req = urllib.request.Request(
            u,
            headers={"User-Agent": cfg.user_agent, "Accept": "application/vnd.github+json"},
            method="GET",
        )
        try:
            ctx = resolve_ssl_context(cfg)
            if ctx is not None:
                resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            else:
                resp = urllib.request.urlopen(req, timeout=timeout)
            with resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            data = None
    if data is None:
        if last_err is not None:
            raise last_err
        return None

    version = str(data.get("tag_name", ""))
    notes = data.get("body", "") or ""
    dl, dl_sha = None, ""
    assets = data.get("assets", [])
    # 两轮挑选：① 严格要求「前缀 + 版本号一致」；② 一个都没挑到再放宽到「只看前缀」。
    # ⚠️ 放宽是兜底：资产名里不带版本号时，严格匹配会挑不出来 ⇒ 更新通道整个断掉。
    picked = None
    for strict in (True, False):
        for a in assets:
            if _match_asset(cfg, a, version, strict=strict):
                picked = a
                break
        if picked is not None:
            break
    if picked is not None:
        dl = picked.get("browser_download_url")
        digest = str(picked.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            dl_sha = digest.split(":", 1)[1].strip().lower()
    if not dl:
        # ⚠️ Release 存在、但里面只有安装包（没有可自动更新的绿色版 exe）——
        #    旧版这里返回的是「有 tag、download_url 为 None」的 dict，
        #    调用方 `r["download_url"] is None` 的判断不能崩 ⇒ 保持同样形状。
        return UpdateInfo(version=version, notes=notes, download_urls=[],
                          sha256=dl_sha, html_url=data.get("html_url", ""),
                          source="GitHub API")
    if not dl_sha:
        dl_sha = sha256_from_notes(notes)
    return UpdateInfo(version=version, notes=notes, download_urls=[dl],
                      sha256=dl_sha, html_url=data.get("html_url", ""),
                      source="GitHub API")


def _parse_update_json(cfg, data, source):
    """把 update.json 归一化成 UpdateInfo。

    ⚠️ 必须是**模块级**函数：check_update() 直接调用它。
       （曾经被误缩进到 fetch_latest_release() 里、且落在 return 之后，
         成了永远不执行的死代码 → check_update 必抛 NameError。）
    """
    if not isinstance(data, dict):
        return None
    ver = str(data.get("version") or "").strip()
    urls = [u for u in (data.get("url"), data.get("fallback_url")) if u]
    if not ver or not urls:
        return None
    # 自有站文件兜底：update.json 是**随 Release 公开的资产**，不能把站内地址
    # 写进去，所以线上那份文件里没有 fallback_url。只看 url，一旦 GitHub 与
    # 所有加速镜像同时不通，就会「拿到了版本号却下不到文件」—— 自有站白搭。
    # 这里用 asset 名自己拼一条兜底直链（仅当项目配置了 site_file_path）。
    extra = cfg.site_file_url(data.get("asset")) if cfg is not None else None
    if extra and extra not in urls:
        urls.append(extra)
    return UpdateInfo(version=ver, notes=data.get("notes") or "",
                      download_urls=urls,
                      sha256=(data.get("sha256") or "").strip().lower(),
                      html_url=data.get("release_url") or "",
                      source=source)


def meta_candidates(cfg):
    """检查更新的元数据候选源，按「可信源优先」排序。

    顺序：
      ① 项目自带的额外候选（extra_meta_urls）
      ② Release 附件里的 update.json（与版本严格同步，自带 sha256）
      ③ 自有站上的 update.json（仅当该项目允许它当可信元数据源）
    加速镜像**不在此列**：镜像同时给出 exe 与 sha256，被控就能两者一起换，
    校验等于没有。镜像只用于下载 exe 字节。
    """
    out, seen = [], set()

    def _add(u, label):
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append((u, label))

    for u, label in cfg.extra_meta_urls:
        _add(u, label)

    rel = cfg.release_meta_url
    if rel and is_trusted_meta_url(rel, cfg):
        _add(rel, source_label(rel, cfg))

    site = cfg.site_meta_url
    if site and is_trusted_meta_url(site, cfg):
        # ⚠️ 标签必须走 source_label()，让各项目的 site_label 生效：
        #    法院/发票汇总 = 「备用源」，图片转 Excel = 「自有服务器」，
        #    发票二维码/工具箱 = 空（显示域名本身）。
        #    这里曾硬编码「自有服务器」，导致四个项目在日志/界面里
        #    都显示成「自有服务器」，与各自历史文案不符。
        _add(site, source_label(site, cfg))

    return out


def check_update(cfg, timeout=5, log_fn=None, allow_api_fallback=True):
    """取更新信息。返回 UpdateInfo 或 None。

    取不到时**静默返回 None** —— 绝不因为检查更新把软件卡住。
    """
    def _log(m):
        try:
            if log_fn:
                log_fn(m)
        except Exception:  # noqa: BLE001
            pass

    for url, source in meta_candidates(cfg):
        try:
            d = http_json(url, timeout, cfg)
        except Exception:  # noqa: BLE001
            continue
        info = _parse_update_json(cfg, d, source)
        if info:
            _log("已从 %s 获取版本信息：v%s" % (source, info.version))
            return info

    if allow_api_fallback:
        try:
            info = fetch_latest_release(cfg, timeout=10)
            if info:
                _log("已从 GitHub API 获取版本信息：v%s" % info.version)
                return info
        except Exception:  # noqa: BLE001
            pass
    return None


# fetch_update_info 是 check_update 的别名（旧名保留，向后兼容）。
def fetch_update_info(cfg, timeout=5, log_fn=None, allow_api_fallback=True):
    return check_update(cfg, timeout=timeout, log_fn=log_fn,
                        allow_api_fallback=allow_api_fallback)


# ============================================================== 配置文件


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path, data):
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def load_auto_update(config_file):
    """读「是否自动检查更新」；缺省视为 True。"""
    try:
        return bool(_read_json(config_file).get("auto_update", True))
    except Exception:
        return True


def save_auto_update(config_file, enabled):
    """记住「是否自动检查更新」。失败静默 —— 只是下次弹窗的默认值。"""
    data = _read_json(config_file)
    data["auto_update"] = bool(enabled)
    _write_json(config_file, data)


def load_cleanup_old(config_file):
    """读上次的选择；没有记录 / 读不出来 → True（默认删旧版，与最初行为一致）。"""
    try:
        return bool(_read_json(config_file).get("cleanup_old", True))
    except Exception:
        return True


def save_cleanup_old(config_file, enabled):
    """记住勾选状态。失败静默 —— 只是下次弹窗的默认值，不值得因此打断更新。"""
    data = _read_json(config_file)
    data["cleanup_old"] = bool(enabled)
    _write_json(config_file, data)


# 别名：语义更明确的名字（与 load_auto_update 等价）。
def load_auto_update_cfg(config_file):
    return load_auto_update(config_file)


def save_auto_update_cfg(config_file, enabled):
    return save_auto_update(config_file, enabled)


# ============================================================== 就地更新
# 事实依据（Windows 10/11 实测）：程序运行期间，它自己的 exe 文件
#   · 不能删除        → PermissionError WinError 5
#   · 不能被覆盖/替换 → PermissionError WinError 5
#   · 但**可以被重命名**（同目录改名不受锁限制）
# 因此「删掉旧版本」这件事必须推迟到进程退出之后，由接管的新版本去完成。
# 这也是旧实现（生成 updater.bat + 轮询等待进程退出）失败的根源：轮询条件写错，
# 永远等不到，最后走超时分支什么都不做。


def is_frozen():
    """是否以打包后的 exe 运行。开发态（python 跑 .py）绝不能碰 sys.executable 所在目录。"""
    return bool(getattr(sys, "frozen", False))


def base_stem(stem):
    """去掉 _旧版[_数字] / _更新中[_数字] 后缀，还原出本程序的基准文件名。"""
    return re.sub(r"_(?:旧版(?:_\d+)?|更新中(?:_\d+)?)$", "", stem)


def old_version_pattern(base_stem_, ext):
    """只匹配本程序自己产生的旧版/中间文件，避免误删同目录其他文件。"""
    return re.compile(
        r"^%s_(?:旧版(?:_\d+)?|更新中(?:_\d+)?)%s$" % (re.escape(base_stem_), re.escape(ext)),
        re.IGNORECASE,
    )


def old_version_pattern_multi(prefixes, ext):
    """按「多个可能的前缀」生成旧版/中间文件匹配规则。

    为什么需要多个前缀：更新后文件名会带上实际版本号（法院文书下载器_v2.5.exe
    → 法院文书下载器_v2.7.exe），所以「自己产生的残留」不再只有一个基准名。
    把 app_name、更新前的基准名、更新后的基准名都列进来，既能覆盖全部残留，
    又因为要求 `前缀 + 可选_v版本 + _旧版/_更新中 + 扩展名` 精确结构，
    不会误删同目录其他文件。

    为什么前缀后面允许再跟一段 `_v数字.数字`：老版本留下的中间名是
    `法院文书下载器_v2.8_更新中.exe`（前缀只认到 `法院文书下载器`），
    不放宽的话这种历史残留永远清不掉。
    """
    uniq = []
    for p in prefixes:
        if p and p not in uniq:
            uniq.append(p)
    if not uniq:
        return re.compile(r"(?!x)x")        # 永不匹配
    alts = "|".join(re.escape(p) for p in uniq)
    return re.compile(
        r"^(?:%s)(?:_v[\d.]+)?_(?:旧版(?:_\d+)?|更新中(?:_\d+)?)%s$" % (alts, re.escape(ext)),
        re.IGNORECASE,
    )


def desired_base_name(app_name, version, fallback):
    """决定「更新后本程序该叫什么名字」（不含扩展名）。

    项目组 2026-09-27 要求：自动更新后文件名要带上**实际**版本号
    （`法院文书下载器_v2.7.exe`），别再出现「名字写着 v2.1、跑的其实是 v2.7」
    这种串版。

    ⚠️ 但**安装版必须保持固定名不变**：开始菜单/桌面的快捷方式、卸载程序、
    以及后续每一次自动更新，全都认死这个名字。给安装版改名等于把这三者一起打断。

    判据 = 「当前文件名是不是就叫 app_name 这个光名字」：
      · 安装版落地名就是 `法院文书下载器.exe` → fallback == app_name → 保持固定名
      · 绿色版名字带版本号（`法院文书下载器_v2.5.exe`）→ fallback != app_name → 换成新版本号
    这比探 `unins*.exe` 或查注册表都稳，且绝不会误伤安装版。
    """
    if not app_name or not version:
        return fallback
    if fallback == app_name:            # 光名字 → 安装版（或旧版绿色版）→ 不动
        return fallback
    return "%s_v%s" % (app_name, version)


def purge(directory, current, log_fn=None, max_wait=8.0, prefixes=None,
          cleanup_old=True):
    """删除 directory 下所有「本程序旧版/中间」文件（排除 current）。

    cleanup_old=False（用户在更新弹窗里取消了「更新后自动删除旧版本文件」）：
        **完全不动这些文件**，旧版本原样保留在程序目录里。
        注意这里只是「不删」，settle_after_update 里的改名逻辑照旧执行
        （旧版会被挪成 `xxx_旧版_<时间戳>.exe`，仍占着磁盘，但用户明确要求保留）。
    """
    stem, ext = os.path.splitext(os.path.basename(current))
    if prefixes:
        pat = old_version_pattern_multi(prefixes, ext)
    else:
        pat = old_version_pattern(base_stem(stem), ext)

    def _log(msg):
        try:
            if log_fn:
                log_fn(msg)
        except Exception:  # noqa: BLE001
            pass

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

    if not cleanup_old:
        # 用户明确要求保留旧版本 → 一个字节都不删，只如实告知留下了什么
        try:
            kept = [os.path.basename(p) for p in _scan()]
        except Exception:  # noqa: BLE001
            kept = []
        _log("已保留旧版本文件（未勾选自动删除）：%s"
             % ("、".join(kept) if kept else "当前目录无旧版残留"))
        return []

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

    if removed:
        _log("已清理旧版本文件：%s" % "、".join(removed))
    return removed


def cleanup_old_versions(log_fn=None, max_wait=8.0, app_name=None, cleanup_old=None,
                         purge_fn=None, is_frozen_fn=None):
    """删除程序目录里遗留的「旧版 / 更新中」文件。

    只在打包运行时生效；开发态直接返回，不做任何事。
    命中规则严格限定为本程序自己的命名格式，且排除当前正在运行的自己。
    刚启动时旧进程可能还没完全退出（文件仍被锁），所以带重试。

    app_name 传了就用「多前缀」规则（见 old_version_pattern_multi），
    以覆盖改名后基准名变化的情况；不传则沿用旧的单前缀规则。

    cleanup_old: 与 settle_after_update 同义 —— None 时自动读旧版进程留下的
    标记文件（用户在更新弹窗里勾了什么），True/False 则显式指定。
    这个函数是**对外公开接口**，不接这个参数会造成「同一份配置、不同函数
    行为不一致」的坑，所以必须透传。

    purge_fn / is_frozen_fn: 可注入的依赖点。传 None 用本模块的实现。
    ⚠️ 这两个参数存在的唯一理由是**向后兼容**：各项目的旧测试会 monkeypatch
    适配层模块里的 _purge / _is_frozen 并期望补丁对调用链生效。适配层必须把
    自己的模块级函数原样传进来（而不是让内核直接用内核里的那份），
    否则补丁会被绕过，测试与依赖它的宿主行为都会失真。
    """
    frozen = (is_frozen_fn or is_frozen)()
    if not frozen:
        return []
    try:
        current = os.path.abspath(sys.executable)
    except Exception:  # noqa: BLE001
        return []
    if cleanup_old is None:
        cleanup_old = read_and_consume_cleanup_flag(os.path.dirname(current))
    prefixes = None
    if app_name:
        prefixes = [app_name,
                    base_stem(os.path.splitext(os.path.basename(current))[0])]
    _purge = purge_fn or purge
    return _purge(directory=os.path.dirname(current), current=current,
                  log_fn=log_fn, max_wait=max_wait, prefixes=prefixes,
                  cleanup_old=cleanup_old)


# ------------------------------------------------- 「更新后删除旧版」跨进程传递
# 为什么需要它：复选框是在**旧版进程**的更新弹窗里勾的，而删旧版这个动作发生在
# **新版进程**的 settle_after_update() 里 —— 两个不同的进程，没有共享内存，
# 只能靠一个文件把用户的选择传过去。
#
# 落盘位置选「程序所在目录」而不是配置目录：更新期间程序目录一定可写
# （就地更新本来就要往那儿写新 exe），而配置目录在别的模块里、不一定传得进来。


def write_cleanup_flag(directory, cleanup_old):
    """旧版进程：把用户的选择写进程序目录，供新版启动时读取。"""
    if not directory:
        return False
    path = os.path.join(directory, CLEANUP_FLAG)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("cleanup_old=%s" % ("1" if cleanup_old else "0"))
        return True
    except OSError:
        return False


def read_and_consume_cleanup_flag(directory):
    """新版进程：读取旧版留下的选择并删掉标记文件。

    返回 True/False 表示要不要删旧版；**文件不存在时返回 True**（默认保持原行为，
    不因为机制故障就悄悄改成「保留一堆旧版文件」）。

    必须「读完就删」：这个文件表达的是「本次更新」的意图，若留着会被下一次启动
    当成新决策反复生效，用户后来改了勾选状态也不生效。
    """
    path = os.path.join(directory or "", CLEANUP_FLAG)
    try:
        if not os.path.exists(path):
            return True
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return "cleanup_old=0" not in txt.replace(" ", "")


# ---------------------------------------------------------------- 待改名登记
# ⚠️⚠️ 2026-09-27 实测铁律（复现脚本 probe_matrix.py）：
#   **把正在运行的 exe 改名，会让本进程的出网永久失效。**
#     直接启动 + 正常文件名        → 4.0 秒拿到版本信息
#     直接启动 + `_更新中` 文件名  → 75 秒仍然一个字节都发不出去
#     父进程 startfile + 正常文件名 → 4.3 秒
#     父进程 startfile + `_更新中`（真实更新路径）→ 75 秒仍然出不去
#   结论：与「谁启动的」无关，只要进程改了自己的名字，网络就废掉且不会自行恢复。
#   用户看到的症状：更新完自动打开的新版，一点下载就莫名卡死，必须关掉重开。
# 所以：**绝不在运行期间改名**，只登记待办，等进程退出时（已不需要网络）再改。
_PENDING_RENAME = None
_RENAME_HOOKED = False
# atexit 里能拿到的日志回调（注册时的那个）
_PENDING_LOG_FN = None


# ------------------------------------------------- 状态直通（适配层兼容用）
# ⚠️ 待改名状态是**模块级可变状态**，重构后它住在 updater_core 里。
#    但旧代码（以及各项目的回归测试）会直接读写
#    `autoupdate._PENDING_RENAME` / `iqr_update._PENDING_RENAME`。
#    若适配层只留一份静态副本，外部读到的永远是 None（假值），
#    外部写的复位也清不掉内核里的真状态。
# ⇒ 让适配层把这几个名字的**读写直通到内核**，语义与旧版完全一致。
STATE_PROXY_NAMES = ("_PENDING_RENAME", "_RENAME_HOOKED", "_PENDING_LOG_FN")


def install_state_proxy(module_obj, state_module=None):
    """把 module_obj 的「待改名状态」读写直通到共享内核。

    用法（放在适配层模块末尾）：
        _core.install_state_proxy(sys.modules[__name__])

    ⚠️ 调用后**不要**再在本模块里定义 _PENDING_RENAME 等同名变量 ——
       那样会遮蔽代理，读到的仍是静态副本。
    """
    import types

    state = state_module if state_module is not None else sys.modules[__name__]

    class _StateProxyModule(types.ModuleType):
        """把状态名读写直通到共享内核的模块类型。"""

        def __getattr__(self, name):
            # 只有 __dict__ 里没有该名字时才会走到这里（PEP 562）
            if name in STATE_PROXY_NAMES:
                return getattr(state, name)
            raise AttributeError(
                "module %r has no attribute %r" % (self.__name__, name))

        def __setattr__(self, name, value):
            if name in STATE_PROXY_NAMES:
                setattr(state, name, value)
                return
            object.__setattr__(self, name, value)

    module_obj.__class__ = _StateProxyModule
    return module_obj


def park_file(directory, path, ext):
    """把 path 挪成 `xxx_旧版_<时间戳>.exe`，返回是否成功。

    两条 Windows 硬事实决定了必须用「挪开」而不是「删除」：
      · 运行中的 exe **不能删、不能覆盖**，但**可以改名**（同目录改名不受锁限制）
      · 若沿用固定名（xxx_旧版.exe），一旦上次更新留下同名残留，os.rename
        会因「目标已存在」直接失败 → 接管失败 → 用户继续启动旧版本
        （症状就是「更新了却没变」）。带时间戳 + 冲突兜底可彻底避免。
    """
    try:
        pstem = os.path.splitext(os.path.basename(path))[0]
        parked = os.path.join(
            directory, "%s_旧版_%s%s" % (pstem, time.strftime("%Y%m%d%H%M%S"), ext))
        if os.path.exists(parked):
            try:
                os.remove(parked)          # 自己上次的残留，能删就复用这个名字
            except OSError:
                parked = os.path.join(
                    directory, "%s_旧版_%d%s" % (pstem, int(time.time() * 1000), ext))
        os.rename(path, parked)
        return True
    except OSError:
        return False


def apply_pending_rename(log_fn=None):
    """进程退出时把程序文件名改成「软件名_v实际版本号」（由 atexit 自动调用）。

    放在退出时做，是为了绕开上面那条铁律：此时网络已经不需要了，改名无副作用。
    幂等且绝不抛异常 —— 它跑在解释器关闭阶段，出错也不能影响退出。
    """
    p = _PENDING_RENAME
    if not p:
        return False
    log_fn = log_fn or _PENDING_LOG_FN

    def _log(m):
        try:
            if log_fn:
                log_fn(m)
        except Exception:  # noqa: BLE001
            pass

    try:
        current, final = p["current"], p["final"]
        if not os.path.exists(current):
            return False                       # 已经被改过 / 文件没了
        if os.path.abspath(current).lower() == os.path.abspath(final).lower():
            return False
        if os.path.exists(final):
            # 目标名已被占用（又更新过一次、或用户自己放了个同名文件）→ 不动手，绝不覆盖
            _log("退出时未改名：%s 已存在，留给下次启动处理"
                 % os.path.basename(final))
            return False
        os.rename(current, final)
        _log("已把程序文件名改为：%s" % os.path.basename(final))
        return True
    except Exception:  # noqa: BLE001
        return False


def settle_after_update(log_fn=None, app_name=None, version=None, cleanup_old=None,
                        background=False, purge_fn=None, is_frozen_fn=None):
    """程序启动时调用：登记「退出时要改的文件名」，并清掉更新留下的旧版文件。

    ⚠️ **本函数不在启动时改名**（原因见上方 `_PENDING_RENAME` 段的实测铁律）。
    它只做两件事：① 把该腾的位置腾开、把上一版文件挪走；② 登记待改名，退出时执行。

    更新后该叫什么，由 desired_base_name() 决定：
      · 安装版（光名字 `法院文书下载器.exe`）→ 保持固定名，快捷方式与卸载才不会断
      · 绿色版 → `法院文书下载器_v<当前版本>.exe`，名字与实际版本永远一致
    规则幂等：名字已经对了就什么都不做，所以每次启动调用都安全。

    cleanup_old: 是否删除旧版本文件。
      · True  → 清理（旧行为）
      · False → **保留**旧版本文件（用户在更新弹窗里取消了勾选）
      · None（默认）→ 自动读旧版进程留下的标记文件（read_and_consume_cleanup_flag），
        所以**调用方不做任何改动**也能拿到用户在弹窗里的选择；标记文件不存在时按 True 处理。
    注意：cleanup_old=False 只是「不删」，上面的挪名/登记待改名照旧执行。

    background: True 时把清理丢到后台线程，不阻塞 Tk 主线程。
        ⚠️ 这一条是「发票识别汇总」项目 P0 回归修出来的：清理要重试最多 12 秒，
        放在主线程会把界面冻住（用户看到「程序卡死」）。
        ⚠️ 默认 False（同步）＝ 法院/图片转Excel/工具箱三个项目的历史行为；
        发票识别汇总的适配层显式传 True。

    purge_fn / is_frozen_fn: 可注入的依赖点（理由同 cleanup_old_versions）。
        适配层必须把自己的模块级 _purge / _is_frozen 传进来，
        否则宿主的 monkeypatch 会被绕过。

    为什么新版要先用中间名启动，而不是直接顶替原文件名：
        实测（Windows 10/11 + PyInstaller onefile）**无法在「当前运行进程自己的
        映像路径」上启动新进程** —— 引导器进程起得来，但真实程序起不来，
        表现为「程序关了却没有新窗口」。换个名字或换个目录都能正常启动。
    """
    global _PENDING_RENAME, _RENAME_HOOKED, _PENDING_LOG_FN
    frozen = (is_frozen_fn or is_frozen)()
    if not frozen:
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
    base = base_stem(stem)                          # 更新前的基准名
    target = desired_base_name(app_name, version, base)   # 更新后该有的名字
    final = os.path.join(directory, target + ext)

    # 清理策略：调用方显式给了就用，没给就读旧版进程留下的标记文件（用户勾了什么）
    if cleanup_old is None:
        cleanup_old = read_and_consume_cleanup_flag(directory)
    _log("旧版本文件处理方式：%s" % ("更新完成后自动删除"
                                   if cleanup_old else "保留（用户取消了勾选）"))

    # ① 名字不对时：腾位置 + 登记待改名（**不在这里动手**）
    if os.path.abspath(current).lower() != os.path.abspath(final).lower():
        # (a) 目标名字已被占（比如上次更新留下的残留）→ 先挪开腾位置
        if os.path.exists(final):
            park_file(directory, final, ext)
        # (b) 上一版实际在跑的那个文件（base.exe）也挪开。
        #     ⚠️ 改名之后「新名字」不再等于「旧名字」，所以不能再指望 (a) 顺带把它处理掉；
        #     必须显式把 base.exe 挪走，否则新旧两个 exe 会并排留在目录里，用户不知道该点哪个。
        old_final = os.path.join(directory, base + ext)
        if (os.path.abspath(old_final).lower() != os.path.abspath(current).lower()
                and os.path.exists(old_final)):
            park_file(directory, old_final, ext)
        # (c) 登记：退出时把 current 改成 final
        _PENDING_RENAME = {"current": current, "final": final}
        _PENDING_LOG_FN = log_fn
        if not _RENAME_HOOKED:
            try:
                import atexit
                atexit.register(apply_pending_rename)
                _RENAME_HOOKED = True
            except Exception:  # noqa: BLE001
                pass
        _log("程序文件名将在退出时改为：%s（本次运行不受影响）"
             % os.path.basename(final))

    # ② 清掉所有旧版/中间残留（含刚被挪开的那份，旧进程退出后即可删除）
    prefixes = [base, target]
    if app_name:
        prefixes.insert(0, app_name)
    _purge = purge_fn or purge
    if background:
        # 丢后台线程：清理要重试最多 12 秒，放在主线程会把界面冻住。
        # ⚠️ prefixes 必须一起传：漏了会退化成「单前缀」规则，
        #    改名后产生的旧版残留（xxx_旧版_*.exe）永远清不掉。
        try:
            threading.Thread(
                target=_purge,
                kwargs={"directory": directory, "current": current,
                        "log_fn": log_fn, "max_wait": 12.0,
                        "prefixes": prefixes, "cleanup_old": cleanup_old},
                daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        return []
    return _purge(directory=directory, current=current, log_fn=log_fn,
                  max_wait=12.0, prefixes=prefixes, cleanup_old=cleanup_old)


def windows_replace_and_restart(new_exe_path, log_fn=None, cleanup_old=True,
                                 app_name=None, version=None):
    """就地更新：把下载好的新 exe 放进程序目录，启动它，本进程退出。

    步骤（全在本进程内完成，不生成任何外部脚本、不做进程退出轮询）：
        ① 新 exe 搬进程序目录，暂用中间名 xxx_更新中.exe
        ② 把「是否删除旧版本」写进标记文件（跨进程传给新版）
        ③ 以中间名启动它（**不能**用原文件名，见 settle_after_update 里的说明）
        ④ 本进程退出；新版启动后由 settle_after_update() 接管原文件名并按标记决定删不删旧版

    cleanup_old: 用户在更新弹窗里的选择（默认 True = 删，与旧行为一致）。
                 这里只负责传下去，真正删除发生在新进程里。

    new_exe_path: 下载好的新版 exe 路径（通常位于临时目录）。
    失败时抛 RuntimeError，调用方负责提示。下载失败时文件原封不动。
    """
    if not is_frozen():
        raise RuntimeError(
            "当前是开发态运行（未打包），无法执行就地更新。请在打包后的 exe 中测试。")

    current = os.path.abspath(sys.executable)
    directory = os.path.dirname(current)
    stem, ext = os.path.splitext(os.path.basename(current))
    base = base_stem(stem)
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

    # ② 把「删不删旧版本」写进标记文件 —— 新版进程读不到本进程的变量，
    #    只能靠文件跨进程传递。必须在启动新版**之前**写好。
    if write_cleanup_flag(directory, cleanup_old):
        _log("旧版本文件处理方式已传给新版本：%s"
             % ("更新完成后自动删除" if cleanup_old else "保留旧版本文件"))

    # ③ 启动新版。用中间名（而非原文件名）启动：Windows 不允许在「当前进程自己的
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


def launch_replaced_version(new_exe_path, current_exe=None, cleanup_old=True,
                            log_fn=None):
    """协议 B（库族 / 发票二维码工具）：启动新版本，旧 exe 交给**新进程**回收。

    与 windows_replace_and_restart（协议 A，GUI 族用）是**同一目的的两种落地**：
        协议 A：新版以「xxx_更新中.exe」启动 + 标记文件 → 新版启动后接管原文件名
        协议 B：新版以「xxx_v5.9.0.exe」启动 + --recycle-old 参数 → 新版窗口出来后
               在后台线程回收旧 exe
    协议 B 是发票工具的历史形态：它的主程序用 `--recycle-old "<旧exe>"` 接收任务，
    改动它会牵动主程序启动流程，收益不抵风险，故保留并**收进内核**统一维护。

    ⚠️ PyInstaller 6.9+ 的坑（三个项目共通）：
         · 必须设 PYINSTALLER_RESET_ENVIRONMENT=1，否则子进程会复用本进程的
           %TEMP%\\_MEIxxxx 解包目录，本进程退出时把它删掉 → 新版「看着正常，
           一用懒加载的 cv2 / zxing-cpp 就点了没反应」。
         · 必须清掉 _PYI_APPLICATION_HOME_DIR / _PYI_ARCHIVE_FILE /
           _PYI_PARENT_PROCESS_LEVEL / _MEIPASS / _MEIPASS2。
         · cwd 固定为 exe 所在目录，与用户双击启动完全一致。
         · 不能在「当前进程自己的映像路径」上启动新进程 —— 所以启动的是
           **另一个文件名**的新 exe（{base}_v{ver}.exe），不是就地覆盖。

    cleanup_old=False 时**不带** --recycle-old：新进程据此什么也不回收，
    旧 exe 就留在原目录里。

    成功返回 True；失败抛 RuntimeError（调用方负责给用户提示）。
    """
    def _log(msg):
        try:
            if log_fn:
                log_fn(msg)
        except Exception:  # noqa: BLE001
            pass

    new_exe_path = os.path.abspath(new_exe_path)
    directory = os.path.dirname(new_exe_path)
    current = os.path.abspath(current_exe) if current_exe else os.path.abspath(sys.executable)

    child_env = dict(os.environ)
    child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k in ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
              "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS", "_MEIPASS2"):
        child_env.pop(k, None)

    launch_args = [new_exe_path, "--recycle-old", current] if cleanup_old else [new_exe_path]
    try:
        subprocess.Popen(
            launch_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=directory,
            env=child_env,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("启动新版本失败：%s" % e)
    _log("已启动新版本（%s）。" % os.path.basename(new_exe_path))
    return True


def send_to_recycle_bin(path):
    """把文件移动到回收站（而非彻底删除），便于误删后恢复。返回是否成功。

    使用 Windows Shell 的 SHFileOperation 并带 FOF_ALLOWUNDO 标志，即「删除到回收站」。
    非 Windows 平台或操作失败均返回 False（调用方据此决定是否保留旧文件）。
    """
    if not os.path.exists(path):
        return True
    if sys.platform != "win32":
        try:
            os.remove(path)
            return True
        except Exception:
            return False
    _co_ok = False
    try:
        import ctypes
        from ctypes import wintypes

        # SHFileOperation 走 Shell / COM：在**非主线程**里调用（新版启动后由后台线程回收
        # 旧 exe）必须先初始化 COM 单元，否则会直接失败返回非 0，文件根本不会进回收站。
        # 只在该线程首次初始化成功（S_OK=0）时才配对 CoUninitialize。
        try:
            _co_ok = (ctypes.windll.ole32.CoInitialize(None) == 0)
        except Exception:
            _co_ok = False

        FO_DELETE = 0x0003
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT = 0x0004
        FOF_NOERRORUI = 0x0400

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.UINT),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        from_buf = ctypes.create_unicode_buffer(path + "\0")
        op = SHFILEOPSTRUCTW()
        op.hwnd = 0
        op.wFunc = FO_DELETE
        op.pFrom = ctypes.cast(from_buf, wintypes.LPCWSTR)
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        op.fAnyOperationsAborted = 0
        op.hNameMappings = None
        op.lpszProgressTitle = None
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        # 某些环境下 SHFileOperation 会返回非 0（如 fAnyOperationsAborted），
        # 但文件实际已被移入回收站 / 删除。以「原路径是否已不存在」作为最终成功判据。
        return res == 0 or not os.path.exists(path)
    except Exception:
        return False
    finally:
        # 与上面的 CoInitialize 配对，避免后台线程反复回收时 COM 引用计数失衡。
        if _co_ok:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def make_temp_exe_path(app_name):
    """生成一个**独占**的临时 exe 路径。

    必须随机且独占：固定名会被同机其它进程预置/覆盖，多开两个实例同时更新时
    也会互相踩（都往同一个 _更新.exe 里写）。
    """
    safe = re.sub(r"\W+", "_", app_name or "app")
    try:
        import tempfile as _tf
        fd, tmp = _tf.mkstemp(prefix="%s_" % safe, suffix="_更新.exe")
        os.close(fd)
        return tmp
    except Exception:  # noqa: BLE001
        return os.path.join(tempfile.gettempdir(), "%s_更新.exe" % safe)
