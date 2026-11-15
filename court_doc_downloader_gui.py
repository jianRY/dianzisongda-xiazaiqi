#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法院文书下载器（图形界面版，可打包为单个 exe）
=====================================================================

功能：
    - 粘贴法院电子送达短信（或仅链接）到文本框
    - 点击“开始下载”，自动从文本中提取 zxfw.court.gov.cn 的送达链接
    - 调接口拿到该案件全部文书，**多线程并发下载** PDF 到本地
    - 保存路径可“选择”，也可直接在框里手填；不选则默认：桌面\文书（不存在自动新建）
    - 上次选择的路径与“完成后打开文件夹”方式会被记住（写入 %APPDATA%）
    - “下载完成后打开文件夹”三档可选：不打开 / 打开保存根目录 / 打开每个案件文件夹
    - 可勾选“下载后自动转为 JPG 图片”（长边 2000px、高质量）；两种目录模式可选：
        ① 所有图片统一放进案件下的“图片”文件夹
        ② 按 PDF 文件名各自建子文件夹存放对应图片（多页自动加页码）
    - 带进度条（份数 + 百分比），实时显示“已下载 / 总份数 (xx%)”
    - 下载中按钮变为“取消下载”，点击可中途取消未完成的任务
    - 每个案件自动建子文件夹：法院名_案号_启动时间戳\（时间戳为年月日时分秒纯数字，避免同名法院不同链接合并）

原理（与命令行版一致，已实测）：
    - 文书列表接口免登录：POST .../getWsListBySdbhNew，body 含 qdbh/sdbh/sdsin
    - OSS 签名链接有效期极短 → 每次运行现取现下
    - OSS 签名绑定 HTTP 方法 → 只用 GET
    - 同一案件的文书清单一次拿到 N 份 OSS 签名 URL，文件之间互相独立 → 用线程池并发下载

依赖：仅 Python 标准库（tkinter / urllib / ssl / concurrent.futures 等）。
打包：pyinstaller --onefile --windowed court_doc_downloader_gui.py
"""

import json
import os
import queue
import re
import ssl
import glob
import hashlib
import io
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import ui_kit as K  # 必须最先导入：模块导入时即启用 DPI 感知（先于 tkinter）
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

import urllib.error
import urllib.parse
import urllib.request

import autoupdate
import ssl_trust

API_URL = "https://zxfw.court.gov.cn/yzw/yzw-zxfw-sdfw/api/v1/sdfw/getWsListBySdbhNew"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://zxfw.court.gov.cn/zxfw/"
MAX_RETRY = 3
RETRY_BACKOFF = 2.0
# 版本号标准：三段式 X.Y.Z（项目组 2026-09-27 定）。发版脚本 发版脚本 会校验这个格式。
# ⚠️ 这个常量必须和发版参数严格一致。v2.10.1 就因为只改了产物名和 CHANGELOG、
#    忘了改这里，导致用户每次启动都弹「发现新版本 v2.10.1」→ 点了更新装回
#    v2.10.1 → 重启又弹 —— 永远升不掉。发版脚本 release_all.py 现在会强制校验它。
VERSION = "2.16.0"
# 软件名（唯一来源）：用于窗口标题、更新弹窗、以及**更新后的文件名**
# （绿色版会被改名为「法院文书下载器_v2.7.exe」；安装版保持固定名「法院文书下载器.exe」）
APP_NAME = "法院文书下载器"
# 并发下载线程数：过小无提速、过大可能触发法院平台限流；4 是实测稳妥值
MAX_WORKERS = 4
# 顶栏高度（逻辑像素）。v2.16.0 由 60 收到 52：白底扁平后太高会显得空。
HEADER_H = 52
# 自动更新：GitHub 上最新 Release 信息（私有仓库需设为公开才能免密访问）
GITHUB_API_LATEST = "https://api.github.com/repos/jianRY/dianzisongda-xiazaiqi/releases/latest"

# ---------------- 应用图标 ----------------
# 图标资源相对路径（打包进 exe 的 _MEIPASS 内同样位于 assets/ 下）
APP_ICON_REL = os.path.join("assets", "app.ico")
# Windows 任务栏身份标识：不设置的话任务栏会显示 python 默认图标
APP_ID = "jianRY.CourtDocDownloader"


# ---------------- 运行日志落盘 ----------------
# 为什么必须落盘：日志只画在界面上时，一旦界面刷新出问题（或进程被强杀），
# 用户能看到的就只有「点了没反应」——曾经因此排查了两天。落盘后任何异常都留痕。
_LOG_LOCK = threading.Lock()
# 当前日志文件的「缓存状态」：key=(目录, 日期) / path / 已写字节数（-1=未知）。
# 见 write_log_file 的说明：避免每行都去 stat 一次磁盘。
_LOG_STATE = {"key": None, "path": "", "size": -1}
_LOG_MAX_BYTES = 3 * 1024 * 1024
_LOG_KEEP_BYTES = 1024 * 1024

# 日志里绝不能出现送达链接的查询串：qdbh/sdbh/sdsin 这三元组是**免登录**访问
# 当事人全部文书的凭证。日志落到 %LOCALAPPDATA% 后会被用户整份发给技术支持，
# 等于把案件材料一并交出去（实测日志里确实出现过完整 sdsin）。
# 原则：日志只需要能区分「是不是同一条链接」，不需要能复原它。
_REDACT_KEYS = ("sdsin", "sdbh", "qdbh", "sid", "token", "access_token",
                "access_key", "key", "sign", "signature")
_REDACT_RE = re.compile(r"(?i)\b(%s)=([^&\s#\"'<>]+)" % "|".join(_REDACT_KEYS))


def redact_text(s):
    """把文本里的送达凭证打码成「参数名 + 短指纹」。

    ⚠️ 原来是「保留前 3 位，便于比对是否同一条链接」。但 qdbh/sdbh/sdsin 三个参数
    就是**免登录**访问当事人全部文书的凭证，前 3 位同样是凭证的一部分（少几位
    只是增加了爆破成本，并没有改变性质）。现在改为对值取 SHA256 前 8 位：
    依旧能判断「两次是不是同一条链接」，但一个原始字符都拿不到。
    指纹形如 `sdsin=#6fe10705`，二次脱敏幂等（`#` 不在值字符集里）。
    """
    try:
        t = str(s)
        # 快路径：日志每行都要过这里，没有「key=值」形态就完全不必跑正则
        if "=" not in t:
            return t

        def _sub(m):
            key, val = m.group(1), m.group(2)
            fp = hashlib.sha256(val.encode("utf-8", "replace")).hexdigest()[:8]
            return "%s=#%s" % (key, fp)

        return _REDACT_RE.sub(_sub, t)
    except Exception:
        return str(s)


def url_fingerprint(url):
    """链接 → 不可复原的指纹「域名 (#摘要前8位)」。

    排查「链接是否过期 / 两次是不是同一条」够用，但拿着它访问不了任何文书。
    """
    u = str(url or "").strip()
    if not u:
        return "（空）"
    try:
        host = urllib.parse.urlsplit(u).netloc or "链接"
    except Exception:  # noqa: BLE001
        host = "链接"
    return "%s (#%s)" % (host, hashlib.sha256(u.encode("utf-8", "replace")).hexdigest()[:8])


def log_dir():
    """日志目录：%LOCALAPPDATA%\\法院文书下载器\\日志。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "法院文书下载器", "日志")


def prune_logs(keep_days=30):
    """删除超过保留期的运行日志。

    日志里可能残留案件访问凭证与文书信息，不能无限期堆在磁盘上。
    只删本程序自己的 运行日志_YYYYMMDD.log，任何失败都静默。
    """
    try:
        cutoff = time.time() - keep_days * 86400
        d = log_dir()
        for name in os.listdir(d):
            if not (name.startswith("运行日志_") and name.endswith(".log")):
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def _safe_text(s):
    """把任意对象转成 Tk 能安全显示的文本。

    Tk 遇到孤立代理字符（surrogate）或 NUL 会直接抛异常 —— 一旦异常发生在
    「记录错误」这一步，真正的错误信息就会被吞掉（正是 v2.6 以前的坑）。
    这里统一做清洗，保证日志永远不会因为内容而写不出去。

    最后再做一次凭证脱敏：本函数是所有日志（界面 / 落盘 / 退出改名 / 异常兜底）
    的公共入口，在这里做等于全路径覆盖，不用逐个调用点去改。
    """
    try:
        t = str(s).replace("\x00", "")
        t = t.encode("utf-8", "replace").decode("utf-8", "replace")
        return redact_text(t)
    except Exception:
        return repr(s)


def write_log_file(line):
    """把一行日志追加到当天的日志文件；任何失败都静默（绝不影响主流程）。

    ⚠️ 性能：原来每写一行都 os.path.getsize() 一次（一次额外系统调用），
    而日志是每秒几十条地写、全天累计上万行 —— 纯属浪费。
    现在把「当天文件路径 + 已写字节数」缓存在内存里，只在跨天（或路径变了）
    时 stat 一次，之后靠累加计数判断要不要轮转。
    """
    try:
        d = log_dir()
        with _LOG_LOCK:
            day = time.strftime("%Y%m%d")
            # key 里带上目录：测试/宿主会 monkeypatch log_dir，缓存必须跟着走
            key = (d, day)
            if _LOG_STATE.get("key") != key:
                _LOG_STATE["key"] = key
                _LOG_STATE["path"] = os.path.join(d, "运行日志_%s.log" % day)
                _LOG_STATE["size"] = -1          # -1 = 未知，首次写入时 stat
            p = _LOG_STATE["path"]
            os.makedirs(d, exist_ok=True)
            if _LOG_STATE["size"] < 0:
                try:
                    _LOG_STATE["size"] = os.path.getsize(p)
                except OSError:
                    _LOG_STATE["size"] = 0
            # 简单轮转：超过 3MB 就留最后 1MB，避免无限增长
            if _LOG_STATE["size"] > _LOG_MAX_BYTES:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        tail = f.read()[-_LOG_KEEP_BYTES:]
                    blob = ("…（日志过大，仅保留最近部分）…\n" + tail).encode("utf-8")
                    # ⚠️ 用二进制写：文本模式下 Windows 会把每个 \n 翻成 \r\n，
                    #    我们缓存的「已写字节数」就会比真实文件小，轮转阈值随之失真。
                    with open(p, "wb") as f:
                        f.write(blob)
                    _LOG_STATE["size"] = len(blob)
                except OSError:
                    pass
            # 二道保险：直接调 write_log_file 的地方（traceback / 界面异常）
            # 不经过 _safe_text，在这里统一脱敏，保证落盘内容永远不含送达凭证。
            data = (redact_text(line) + "\n").encode("utf-8")
            with open(p, "ab") as f:
                f.write(data)
            _LOG_STATE["size"] += len(data)
    except Exception:
        pass


def resource_path(rel):
    """定位打包进 exe 的资源；PyInstaller 单文件运行时资源解包在 sys._MEIPASS。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def setup_app_id():
    """设置 Windows AppUserModelID，让任务栏用本程序的图标与身份。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def apply_window_icon(win):
    """给窗口设置图标（标题栏 + 任务栏）。缺图标时静默跳过，不影响主功能。

    注意：Tk 的 wm iconbitmap 在 Windows 上会取 .ico 里最小的一档（16px）再放大，
    标题栏和任务栏都会发虚。这里在其后用 Win32 LoadImage 显式取 32px 档覆盖，
    清晰度差别肉眼可见。
    """
    ico = resource_path(APP_ICON_REL)
    if not os.path.isfile(ico):
        return
    # 1) Tk 常规方式（非 Windows 平台靠它；同时把图标属性挂上）
    try:
        win.iconbitmap(ico)
    except Exception:
        pass
    # 2) Windows：显式指定尺寸设置 WM_SETICON，取高分辨率档位
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        ctypes.c_void_p, ctypes.c_void_p]
        user32.SendMessageW.restype = ctypes.c_void_p
        win.update_idletasks()
        hwnd = user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
        if not hwnd:
            return
        big = user32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        small = user32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ctypes.c_void_p(ICON_BIG), ctypes.c_void_p(big))
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ctypes.c_void_p(ICON_SMALL), ctypes.c_void_p(small))
    except Exception:
        pass


# ---------------- 核心下载逻辑（与命令行版一致） ----------------
# 标准案号，如 (2025)苏0000民初1234号 / （2025）苏05民终1234号
# 结构：[年度] + 法院代字(汉字+可选数字) + 案件类型(民/刑/行/执/商) + 程序(初/终/再/申/保/特/监/破…) + 序号 + 号
_CASE_RE = re.compile(
    r"[\(（](\d{4})[\)）]\s*[\u4e00-\u9fa5]{1,8}\d{0,6}\s*"
    r"(?:民|刑|行|执|商|赔|认)\d{0,4}"
    r"[初终再申保特监破执异复撤销核催督催告]{0,2}\s*\d{1,8}\s*号"
)


# 链接末尾可能紧跟的**任何**标点。必须中英文都列全：
#   法院短信的标准句式就是「…&sdsin=XXX，请及时查阅。」—— 只 strip 英文标点的话，
#   用户整段粘贴时中文标点会被吞进 sdsin，接口必然校验失败，
#   弹「链接已过期」让用户以为链接真失效了，白跑一趟。
# 这一串被同时用在「正则字符类排除」和「rstrip 兜底」两处，形成双重保险。
_URL_TAIL_CHARS = ".,;:!?)]}>\"'，。；：！？、）】》」』〉〞…—～·　 \t\r\n"
# 正则里要排除的字符（不能含 `&`、`=`、`#`、`?`，那些是 URL 的合法组成）
_URL_STOP_CHARS = "\\s\"'<>，。；：！？、（）【】《》「」『』〔〕…—～·"


def _extract_one(url, text, pos, case_spans=None):
    """从单个 url 提取参数。

    case_spans: 预先算好的 [(start, end, caseno), ...]，按案号在文本中的位置排序。
    优先取「紧跟在该链接之前」的最近案号；没有则回退到全局第一个案号。
    这样批量粘贴多案件时才不会把 B 案的案号安到 A 案头上。
    """
    # 参数值用**白名单**取（服务器给的是 Base64 类串），而不是 `[^&\s]+` 黑名单：
    # 黑名单会漏掉中文标点（链接后常跟「，请及时查阅。」），把正文当成参数值发出去。
    # 两处保险：外层 extract_tasks 已 strip 过尾部标点，这里再按白名单取一次。
    p = r"[A-Za-z0-9\-_=+%*~]+"
    q = re.search(r"[?&]qdbh=(%s)" % p, url)
    s1 = re.search(r"[?&]sdbh=(%s)" % p, url)
    s2 = re.search(r"[?&]sdsin=(%s)" % p, url)
    if not (q and s1 and s2):
        return None
    caseno = ""
    if case_spans:
        # 取链接位置之前最近的一个案号（同一条短信通常是「案号…链接」的顺序）
        for st, en, cn in case_spans:
            if en <= pos:
                caseno = cn
            elif st > pos:
                break
        if not caseno:
            # 链接在前、案号在后（少数短信格式）：取之后最近的一个
            for st, en, cn in case_spans:
                if st >= pos:
                    caseno = cn
                    break
    if not caseno:
        cm = _CASE_RE.search(text)
        caseno = cm.group(0) if cm else ""
    return {
        "qdbh": q.group(1),
        "sdbh": s1.group(1),
        "sdsin": s2.group(1),
        "caseno": caseno,
        "url": url,
    }


def extract_tasks(text):
    """从文本中提取所有送达链接（支持批量、自动去重）。"""
    tasks = []
    seen = set()
    # 先扫描全文所有案号及其位置，供 _extract_one 按就近原则配对
    case_spans = [(m.start(), m.end(), m.group(0)) for m in _CASE_RE.finditer(text)]
    case_spans.sort()
    url_re = re.compile(r"https?://[^%s]+" % _URL_STOP_CHARS)
    for m in url_re.finditer(text):
        url = m.group(0).rstrip(_URL_TAIL_CHARS)
        t = _extract_one(url, text, m.start(), case_spans)
        if not t:
            continue
        key = (t["qdbh"], t["sdbh"], t["sdsin"])
        if key in seen:
            continue
        seen.add(key)
        tasks.append(t)
    return tasks


def parse_params(text):
    """兼容单链接场景：返回第一个任务或 None。"""
    ts = extract_tasks(text)
    return ts[0] if ts else None


class LinkRejected(RuntimeError):
    """法院接口明确拒绝了这条链接（多半是链接已过期）—— 重试无意义。"""


def fetch_doc_list_once(params, ctx):
    """向法院接口要一次文书清单。网络层异常原样抛出，供上层决定是否重试。"""
    data = json.dumps(
        {"qdbh": params["qdbh"], "sdbh": params["sdbh"], "sdsin": params["sdsin"]}
    ).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": BROWSER_UA,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    obj = json.loads(body)
    if obj.get("code") != 200:
        code = obj.get("code")
        msg = obj.get("msg") or "未知错误"
        # 把服务端错误翻成人话：最常见的就是链接过期导致的「校验失败」，
        # 直接写清楚原因，用户才知道该去重新获取送达短信，而不是以为程序坏了。
        if code == 401 or "校验失败" in str(msg):
            raise LinkRejected(
                "接口拒绝了该链接（%s）—— 链接多半已过期，请重新获取送达短信。" % msg)
        raise RuntimeError("接口返回非成功状态（code=%s）：%s" % (code, msg))
    docs = obj.get("data") or []
    if not docs:
        raise LinkRejected("接口返回文书清单为空（链接可能已失效或参数有误）")
    return docs


def fetch_doc_list(params, ctx, attempts=3, on_retry=None):
    """带重试地取文书清单。

    ⚠️ 原来这里只请求一次 —— 网络抖一下（DNS 慢、TLS 握手超时、接口偶发 5xx）
    整条链接就被判死、直接跳过，用户看到的是「点了下载什么都没发生」。
    现在：网络类错误自动重试 3 次（1.2s / 2.4s 退避）；而接口明确说「链接过期」
    的（LinkRejected）不重试，立刻把原因报给用户。
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fetch_doc_list_once(params, ctx)
        except LinkRejected:
            raise  # 链接本身的问题，重试没用
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < attempts:
                if on_retry:
                    try:
                        on_retry(attempt, e)
                    except Exception:
                        pass
                time.sleep(1.2 * attempt)
    raise last


def _discard(path):
    """删掉下载用的临时文件。

    删不掉**不能**让下载流程崩（文件可能被杀软/预览器占用），但也**不能**像以前
    那样静默 pass —— 那正是「半截文件留在磁盘上被下次当成已下载」的成因之一，
    出问题时日志里至少要看得出是哪个文件没删掉。
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        try:
            write_log_file("[下载] 无法删除临时文件 %s：%s"
                           % (os.path.basename(path), e))
        except Exception:  # noqa: BLE001
            pass


def download_file(url, path, ctx, cancel_check=None):
    """下载单个文书文件。

    Args:
        cancel_check: 可选的零参数回调，返回 True 表示已取消 → 抛 CancelledError 提前退出。

    ⚠️ 先写 `<目标名>.<pid>.part`，全部校验通过后再 os.replace 成正式文件名。
    为什么：直接写目标文件时，进程被强杀 / 断电 / 文件被占用会留下半截文件，
    而「跳过已存在」的判定只看 getsize>0 —— 下次运行会把这份坏文件当成已下载
    跳过，用户永远拿到一个打不开的 PDF，界面却显示「✓ 已保存」（而且删除失败
    时原来那几处 os.remove 是静默 pass，日志里连痕迹都没有）。
    """
    last_err = None
    # 带上 pid：两个实例同时跑同一案件时不至于互相写坏对方的临时文件。
    tmp = "%s.%d.part" % (path, os.getpid())

    class _Cancelled(Exception):
        pass

    for attempt in range(1, MAX_RETRY + 1):
        if cancel_check and cancel_check():
            raise _Cancelled()
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": BROWSER_UA, "Referer": REFERER, "Accept": "*/*"},
                method="GET",
            )
            # 超时 30s 而不是 120s：这个 urlopen 是**不可中断**的阻塞调用，
            # 120s 意味着点「取消」后最长要干等 2 分钟才响应（按钮锁死像程序死了）。
            # 30s 足以区分「网络慢」与「网络断」，失败会自动重试 3 次。
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                # 记录服务端声明的长度，下载完比对，防止网络中断留下「文件头正常但内容被截断」的坏 PDF
                try:
                    expect = int(resp.headers.get("Content-Length", 0) or 0)
                except (TypeError, ValueError):
                    expect = 0
                written = 0
                with open(tmp, "wb") as f:
                    while True:
                        if cancel_check and cancel_check():
                            raise _Cancelled()
                        buf = resp.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        written += len(buf)
            if os.path.getsize(tmp) == 0:
                raise IOError("下载到 0 字节")
            if expect and written != expect:
                raise IOError("下载不完整（%d / %d 字节），将重试" % (written, expect))
            # 内容校验：
            #  · 文书不一定是 PDF（法院也可能发 jpg / docx），所以只在 .pdf 上强制 %PDF- 魔数，
            #    否则所有非 PDF 文书都会被误判失败并重试 3 次；
            #  · 但对任何类型都拦一下「错误页」——OSS 出错会返回 XML / HTML，文件头能认出来。
            with open(tmp, "rb") as f:
                head = f.read(32)
            low = head.lstrip(b"\xef\xbb\xbf \r\n\t").lower()
            if low.startswith((b"<?xml", b"<html", b"<!doctype", b"<error")):
                _discard(tmp)
                raise IOError("服务端返回的是错误页而非文件（链接可能已过期）")
            if path.lower().endswith(".pdf") and not head.startswith(b"%PDF-"):
                _discard(tmp)
                raise IOError("PDF 文件头异常，疑似下载失败")
            # 全部校验通过才落地：os.replace 在同一分区上是原子的，
            # 正式文件名一旦出现，内容就一定是完整的。
            os.replace(tmp, path)
            return True
        except _Cancelled:
            # 取消：删临时文件后向上抛
            _discard(tmp)
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            _discard(tmp)
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError("下载失败（已重试 %d 次）：%s" % (MAX_RETRY, last_err))


# Windows 保留设备名：以此为文件名（或带扩展名的同名）无法创建，需加前缀规避
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} \
    | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def sanitize_filename(name):
    name = str(name or "").strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = name.strip(". ").strip()
    if not name:
        return "未命名文书"
    # 路径长度保护：Windows 整条路径上限 260，过长的文书名要截断（重名由 (N) 机制兜底）
    if len(name) > 100:
        name = name[:100].strip(". ").strip() or "未命名文书"
    if name.upper() in _RESERVED_NAMES or name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    return name


# MIME 类型 → 扩展名。接口的 c_wjgs 字段给的是完整 MIME（如 application/msword），
# 不查表的话会被压成 "applicationmsword"（超长→判非法）→ 回落 .pdf，
# 导致 Word/Excel 文书被命名成 .pdf，双击打不开。查不到时再走原来的清洗逻辑。
_MIME_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/rtf": ".rtf",
    "application/vnd.oasis.opendocument.text": ".odt",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/json": ".json",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/ofd": ".ofd",
    "application/vnd.ofd": ".ofd",
    "application/zip": ".zip",
    "application/octet-stream": "",
}

def safe_ext(wjgs, url):
    """返回带点的扩展名。

    c_wjgs 可能是 'pdf' / '.PDF' / 'application/pdf' 等形态，统一清洗成小写扩展名；
    清洗不出合法扩展名时再从 URL 猜，最后兜底 .pdf。
    旧实现直接拼 '.'+c_wjgs，遇到 'application/pdf' 会得到非法文件名。

    ⚠️ 类型安全：c_wjgs 来自**外部 HTTP 接口的 JSON**，不能假定它一定是字符串。
    实测过 wjgs=123 会走 `cand="123"` 判定为合法扩展名 → 产出 `.123`，
    用户双击「判决书.123」根本打不开。数字/布尔/容器一律视为「无效声明」。
    """
    ext = ""
    if wjgs:
        # 只接受真正的字符串；数字、布尔、list、dict 等一律忽略（走 URL 推断 → 兜底 .pdf）
        raw = wjgs.strip().lower().lstrip(".") if isinstance(wjgs, str) else ""
        # ① 先查 MIME 映射表（application/msword → .doc 这类必须走表，
        #    压成单词后再判断长度会被误判成非法 → 回落 .pdf，Word 文档就废了）
        if raw in _MIME_EXT:
            ext = _MIME_EXT[raw]
        else:
            cand = re.sub(r"[^A-Za-z0-9]", "", raw)
            # ② 形如 "msword"/"openxmlformats-officedocument..." 的长串：取最后一段
            if len(cand) > 5:
                cand = cand.split()[-1] if " " in cand else cand
                # MIME 主类型+子类型压缩后仍过长 → 取尾部有意义的片段
                for sep in ("officedocument", "formats", "spreadsheetml",
                            "wordprocessingml", "opendocument"):
                    if sep in cand:
                        cand = cand.split(sep)[-1]
                        break
            # 扩展名必须是**纯字母**且 1~5 位：这样 ".123"（数字）、".true"（布尔）
            # 这类由非字符串值拼出来的东西会被自然挡掉
            if 1 <= len(cand) <= 5 and cand.isalpha():
                ext = "." + cand.lower()
    if not ext:
        m = re.search(r"\.([A-Za-z0-9]{2,5})(?:[?#]|$)", url or "")
        ext = ("." + m.group(1).lower()) if m else ".pdf"
    return ext

def _find_existing_case_dir(base, prefix):
    """在 base 下找「同一案件」已有的文件夹（名 == prefix 或 prefix_时间戳），取最近修改的。

    用途：勾选「跳过已存在的同名文书」时复用旧文件夹，否则每次运行都新建带时间戳的
    文件夹，文件永远不存在 → 跳过逻辑形同虚设。
    """
    best, best_mt = None, -1.0
    try:
        if prefix and os.path.isdir(base):
            # 只认两种名字：`prefix` 本身，或 `prefix_<数字>`（我们创建的
            # 「法院_案号_年月日时分秒」）。原来用 startswith(prefix + "_")，
            # 会把「案号是本案号前缀」的另一个案件也当成同一案（123 匹配 1234_…），
            # 于是两份不同案子的文书被合并进同一个文件夹。
            pat = re.compile(r"^%s_\d{1,20}$" % re.escape(prefix))
            for name in os.listdir(base):
                if name != prefix and not pat.match(name):
                    continue
                full = os.path.join(base, name)
                if not os.path.isdir(full):
                    continue
                mt = os.path.getmtime(full)
                if mt > best_mt:
                    best, best_mt = full, mt
    except OSError:
        pass
    return best


def _looks_complete(path):
    """磁盘上已有文件能否当作「已下载完成」（跳过判定用）。

    光看「大小 > 0」不够：旧版本是直接往目标文件写的，中断/被强杀会留下
    半截文件，一旦被判定为「已存在」跳过，这一份就永远缺了。
    这里对 .pdf 额外验一下魔数；不像 PDF 就重新下载（重下会覆盖它）。
    """
    try:
        if os.path.getsize(path) <= 0:
            return False
        if str(path).lower().endswith(".pdf"):
            with open(path, "rb") as f:
                return f.read(5) == b"%PDF-"
        return True
    except OSError:
        return False


def _jpg_output_exists(pdf_path, case_dir, mode):
    """该 PDF 对应的 JPG 是否**完整**生成过（复用案件文件夹时用来决定要不要补转）。

    命名规则见 convert_pdf_to_jpg：单页 stem.jpg，多页 stem_1.jpg、stem_2.jpg…

    ⚠️ 原来只判断「目录里存在任意一张 stem_数字.jpg」就算转好了。多页 PDF 中途
    失败（或用户中途取消）会留下前几页，此后每次重下都被判「已存在，跳过」，
    缺掉的页永远补不回来，界面也不给任何提示 —— 用户只会发现图片少了几张。
    现在按页数逐张核对：一张不缺才算转完；缺任何一张就整体重转（覆盖写）。
    """
    stem = sanitize_filename(os.path.splitext(os.path.basename(pdf_path))[0])
    img_dir = (os.path.join(case_dir, "图片", stem) if mode == JPG_MODE_PERPDF
               else os.path.join(case_dir, "图片"))
    try:
        have = {n.lower() for n in os.listdir(img_dir)}
    except OSError:
        return False

    pages = _pdf_page_count(pdf_path)
    if pages <= 0:
        # 页数读不出来（PDF 损坏 / 没装 PyMuPDF）→ 退回旧判定：
        # 有任意一张就当作已转，至少不会把「已经转好」的重复转一遍。
        pat = re.compile(re.escape(stem) + r"(?:_\d+)?\.jpg$", re.IGNORECASE)
        return any(pat.match(nm) for nm in have)
    if pages == 1:
        return (stem + ".jpg").lower() in have
    return all((stem + "_%d.jpg" % i).lower() in have for i in range(1, pages + 1))


def _pdf_page_count(pdf_path):
    """PDF 页数；读不出来返回 0（调用方按「未知」处理）。"""
    try:
        import fitz  # PyMuPDF，懒加载（仅转换时需要）
    except Exception:  # noqa: BLE001
        return 0
    try:
        # fitz 不保证线程安全，而本函数会被多个下载线程并发调用 ——
        # 必须与 convert_pdf_to_jpg 共用同一把全局锁。
        with _FITZ_LOCK:
            doc = fitz.open(pdf_path)
            try:
                return int(doc.page_count)
            finally:
                doc.close()
    except Exception:  # noqa: BLE001
        return 0


# ---------------- PDF -> JPG 转换（PyMuPDF / fitz） ----------------
# 长边固定 2000px，短边按比例自适应；高画质 JPEG（quality=95）。
# 单页：图片名 = PDF文件名.jpg；多页：PDF文件名_页码.jpg
#
# 注意：PyMuPDF 不保证线程安全，多线程并发下载后若同时转图可能崩溃。
# 调用方应传入一个全局 threading.Lock，串行化所有 fitz 操作。
_FITZ_LOCK = threading.Lock()


def convert_pdf_to_jpg(pdf_path, case_dir, mode, lock=None):
    """把单个 PDF 转为 JPG，返回生成的图片数量。

    mode=0：所有图片放在 case_dir/图片/ 下
    mode=1：按 PDF 文件名在 case_dir/图片/<文件名>/ 下分别建文件夹
    lock：可选 threading.Lock，用于串行化 fitz 调用（多线程场景必传）。
    """
    import fitz  # PyMuPDF，懒加载（仅转换时需要）

    base_img_dir = os.path.join(case_dir, "图片")
    stem = sanitize_filename(os.path.splitext(os.path.basename(pdf_path))[0])
    img_dir = os.path.join(base_img_dir, stem) if mode == JPG_MODE_PERPDF else base_img_dir
    os.makedirs(img_dir, exist_ok=True)

    lk = lock or _FITZ_LOCK
    with lk:
        doc = fitz.open(pdf_path)
        try:
            n = doc.page_count
            count = 0
            target_long = 2000.0
            for p in range(n):
                page = doc[p]
                long_edge = max(page.rect.width, page.rect.height)
                zoom = target_long / long_edge if long_edge > 0 else 1.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                suffix = ("_%d" % (p + 1)) if n > 1 else ""
                out_path = os.path.join(img_dir, stem + suffix + ".jpg")
                # ⚠️ 参数名是 jpg_quality，不是 jpeg_quality（PyMuPDF 1.x）。
                #    写成 jpeg_quality 会抛 TypeError；这里原来还套着一层
                #    「捕获 TypeError 就用默认参数再存一次」的兜底，结果**每一页**
                #    都静默走兜底分支 —— 画质参数从未生效，真正的编码错误也被一起吞掉。
                #    现在参数写对，且不再兜底：真出错就让调用方记「✗ 转 JPG 失败」。
                pix.save(out_path, "jpg", jpg_quality=JPG_QUALITY)
                count += 1
            return count
        finally:
            doc.close()


# ---------------- 路径工具 ----------------
def get_desktop():
    home = os.path.expanduser("~")
    for name in ("Desktop", "桌面", "OneDrive/Desktop"):
        c = os.path.join(home, name)
        if os.path.isdir(c):
            return c
    return os.path.join(home, "Desktop")


def default_out_dir():
    return os.path.join(get_desktop(), "文书")


# ---------------- 配置持久化（记住上次路径 / 自动打开方式 / 转 JPG） ----------------
AUTO_OPEN_OFF = 0      # 不自动打开
AUTO_OPEN_ROOT = 1     # 打开保存根目录
AUTO_OPEN_EACH = 2     # 打开每个案件各自的文件夹

JPG_MODE_SINGLE = 0    # 所有图片放在一个“图片”文件夹内
JPG_MODE_PERPDF = 1    # 按 PDF 文件名分别建子文件夹存放图片

# 导出 JPEG 的质量（PyMuPDF 的 Pixmap.save 默认也是 95，这里显式写出来便于调整）。
JPG_QUALITY = 95


def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "法院文书下载器", "config.json")


def load_config():
    # auto_open_mode 默认 = 打开保存根目录（v2.6 起）。旧默认是「不自动打开」，
    # 下载完看不到任何反应（尤其下载失败时），用户会以为「点了没下载」。
    cfg = {"out_dir": "", "auto_open_mode": AUTO_OPEN_ROOT, "convert_jpg": False,
           "jpg_mode": 0, "auto_update": True, "skip_existing": True,
           "migrated_open": False}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("out_dir"), str):
            cfg["out_dir"] = data["out_dir"]
        mode = data.get("auto_open_mode", None)
        if mode in (0, 1, 2):
            cfg["auto_open_mode"] = mode
        elif data.get("auto_open"):  # 兼容旧版布尔配置
            cfg["auto_open_mode"] = AUTO_OPEN_ROOT
        # 一次性迁移（只做一次）：老配置里的「不自动打开」基本都是出厂默认、并非用户主动选择，
        # v2.6 起统一升级为「打开保存根目录」。迁移后写 migrated_open 标记，此后用户手动改回
        # 「不打开」会被永久尊重，不会再被覆盖。
        if not data.get("migrated_open"):
            if cfg["auto_open_mode"] == AUTO_OPEN_OFF:
                cfg["auto_open_mode"] = AUTO_OPEN_ROOT
            cfg["migrated_open"] = True   # 告诉调用方：需要把标记落盘
        cfg["convert_jpg"] = bool(data.get("convert_jpg", False))
        jm = data.get("jpg_mode", None)
        if jm in (0, 1):
            cfg["jpg_mode"] = jm
        cfg["auto_update"] = bool(data.get("auto_update", True))
        cfg["skip_existing"] = bool(data.get("skip_existing", True))
    except Exception:
        pass
    if not cfg["out_dir"]:
        cfg["out_dir"] = default_out_dir()
    return cfg


def _atomic_write_json(path, data):
    """原子写 JSON：先写同目录临时文件，再 os.replace 顶替。

    ⚠️ 直接 open(path, "w") 是先截断再写：写到一半断电 / 被占用 / 被强杀，
    磁盘上就只剩半截 JSON。而本程序读配置的每个地方都是 `except: 用默认值`，
    于是用户会「莫名其妙地被重置成出厂设置」，且日志里什么都没有。
    os.replace 在同分区上是原子的，要么旧内容、要么新内容，不会出现半截文件。
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_config(out_dir, auto_open_mode, convert_jpg, jpg_mode, auto_update=None,
                skip_existing=True):
    """保存配置。

    auto_update 传 None = 「沿用配置文件里已有的值」，这是必须有别于 True 的语义：
    该字段由更新模块（autoupdate._save_auto_update）在用户选「以后不再提醒」时写 false，
    本函数若以默认 True 落盘，用户随后只要改动任意一个选项就会被悄悄重置回 true
    ——「以后不再提醒」形同虚设。

    ⚠️ 实现上必须「先读原文件、只覆盖自己负责的键」：
    config.json 是**多个模块共用**的一份文件，更新模块还会往里写 cleanup_old
    （「更新后是否删除旧版本」的勾选状态）。原来这里用一个固定的 6 字段字典
    整份覆盖，等于把同文件里其他键全删了 —— 用户取消勾选后只要再动一下设置
    （比如换个保存目录），下次更新又变成「自动删除旧版」，且毫无提示。
    """
    try:
        p = config_path()
        # 原文件读不出来（不存在 / 损坏）就当成空字典：反正接下来要整份重写。
        data = {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                data = old
        except Exception:  # noqa: BLE001
            data = {}

        if auto_update is None:
            auto_update = bool(data.get("auto_update", True))

        # 只更新本函数负责的键，其余键（cleanup_old 等）原样保留。
        data.update({
            "out_dir": out_dir,
            "auto_open_mode": int(auto_open_mode),
            "convert_jpg": bool(convert_jpg),
            "jpg_mode": int(jpg_mode),
            "auto_update": bool(auto_update),
            "skip_existing": bool(skip_existing),
            # 标记：auto_open_mode 的一次性迁移已执行过，之后用户手选「不打开」不再被覆盖
            "migrated_open": True,
        })
        _atomic_write_json(p, data)
    except Exception:
        pass


# ---------------- GUI ----------------


def _open_in_explorer(path, log_fn, empty_msg=None):
    """跨平台打开文件夹。empty_msg 在路径不存在时弹出提示。"""
    if path and os.path.isdir(path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:  # noqa: BLE001
            log_fn("无法打开文件夹：%s" % e)
    elif empty_msg:
        messagebox.showinfo("提示", empty_msg)


class App:
    def __init__(self, root):
        self.root = root
        cfg = load_config()
        self.out_dir = cfg["out_dir"] or default_out_dir()
        self.auto_open_mode = cfg.get("auto_open_mode", AUTO_OPEN_ROOT)
        self.convert_jpg = bool(cfg.get("convert_jpg", False))
        self.jpg_mode = cfg.get("jpg_mode", 0)
        self.skip_existing = bool(cfg.get("skip_existing", True))
        if cfg.get("migrated_open"):
            # 老配置的「不自动打开」已一次性升级为「打开保存根目录」，把迁移标记落盘，
            # 保证只迁移这一次（此后用户手选「不打开」会被永久尊重）。
            save_config(self.out_dir, self.auto_open_mode, self.convert_jpg,
                        self.jpg_mode, skip_existing=self.skip_existing)
        self.last_case_dir = None
        self.running = False
        self.stop_event = threading.Event()  # 取消下载信号
        self._cancel_lock = threading.Lock()  # 保护 last_case_dir / 计数等共享状态
        self._path_save_job = None           # 路径框防抖的待执行 after 句柄
        # ⚠️ 界面更新一律走这条队列，worker 线程绝不直接碰 Tk —— 见 _post / _pump 注释
        self._ui_queue = queue.Queue()
        self._pump_job = None
        self._closing = False
        self._dpi_checked = 0.0              # 上次检查「显示器缩放是否变了」的时间
        self._win_logical = (860, 780)       # 主窗口的逻辑尺寸（DPI 变化后按新比例重设）

        root.title("法院文书下载器 v" + VERSION)
        root.resizable(True, True)
        apply_window_icon(root)
        # DPI 感知已在 import ui_kit 时生效；这里取缩放比并统一 ttk 扁平样式
        K.setup_scale(root)
        K.style_ttk()
        # 窗口关闭保护：下载中先确认并置取消信号，避免半途强杀留下半成品文件
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # 界面回调异常统一落盘：打包成 exe（--windowed）后 sys.stderr 是 None，
        # Tk 默认的异常打印会二次失败、异常彻底消失 —— 曾因此查了两天的「点了没反应」。
        root.report_callback_exception = self._report_callback_exception

        sk = K.SKIN
        root.configure(bg=sk.bg)
        pad_x = 14
        win_w, win_h = 860, 780
        avail_w = int(root.winfo_screenwidth() / K.SCALE) - 40
        avail_h = int(root.winfo_screenheight() / K.SCALE) - 90
        win_w, win_h = min(win_w, avail_w), min(win_h, avail_h)
        self._win_logical = (win_w, win_h)
        root.geometry("%dx%d" % (K.u(win_w), K.u(win_h)))
        # 换显示器（DPI 变了）时给一次提示：Tk 无法重排已建控件的像素尺寸，
        # 硬改缩放比只会让「新画的 Canvas」与「老控件」错位，所以这里只按新比例
        # 重设窗口大小，并明确告诉用户「重启后最准」，别让他对着畸大的窗口发懵。
        root.bind("<Configure>", self._on_root_configure, add="+")

        # ── 顶栏：白底扁平 + 图标 + 标题 + 版本徽章 + 右上文字入口 ──
        # v2.16.0 起不再用 Windows 原生菜单栏（它会压在顶栏上面，视觉上像多出一条
        # 浅灰色带）；「使用说明 / 检查更新」提到顶栏，「导出诊断日志 / 关于」收进
        # 使用说明窗口底部。功能一个没少，入口位置变了。
        self._header = tk.Canvas(root, height=K.u(HEADER_H), highlightthickness=0,
                                 bd=0, bg=sk.bg)
        self._header.pack(fill="x")
        self._hover_link = None          # 当前悬停的顶栏入口（重绘后据此恢复颜色）
        # 鼠标移出顶栏时清掉手型光标：否则从按钮直接划出窗口，光标会一直停在 hand2
        self._header.bind("<Leave>", lambda _e: self._set_header_cursor(False))

        # 真实软件图标（assets/app_icon_288.png，圆角透明底，随包发货）。
        # 引用必须挂在实例上，否则被 GC 回收后 Canvas 显示空白（缩放档同理，按 n 缓存）。
        self._logo_photo = None
        self._logo_cache = {}
        try:
            self._logo_photo = tk.PhotoImage(file=resource_path(
                os.path.join("assets", "app_icon_288.png")))
        except Exception:
            self._logo_photo = None  # 资源缺失时回退到自绘方块

        # ⚠️ bind 必须放在图标初始化**之后**：_paint_header 要读 _logo_photo，
        # 绑定在前会让首次 Configure 有概率撞上「属性还不存在」。
        self._header.bind("<Configure>", self._paint_header)

        # ── 卡1：① 粘贴区 ──
        c1 = K.Card(root, pad=12)
        c1.pack(fill="x", padx=K.u(pad_x), pady=(K.u(12), 0))
        row1 = tk.Frame(c1.body, bg=sk.card)
        row1.pack(fill="x")
        tk.Label(row1, text="① 送达短信 / 链接", font=K.f(10, True),
                 bg=sk.card, fg=sk.text).pack(side="left")
        tk.Label(row1, text="支持批量：多条链接自动依次下载", font=K.f(8),
                 bg=sk.card, fg=sk.faint).pack(side="left", padx=(K.u(8), 0))
        K.RoundButton(row1, text="清空", command=self.clear_text, kind="ghost",
                      height=30, font_size=9, width=64).pack(side="right")
        K.RoundButton(row1, text="粘贴", command=self.paste_text, kind="ghost",
                      height=30, font_size=9, width=64).pack(side="right", padx=(0, K.u(6)))
        wrap_in = tk.Frame(c1.body, bg=sk.card)
        wrap_in.pack(fill="x", pady=(K.u(6), 0))
        self.text_in = tk.Text(wrap_in, height=5, wrap="word", font=K.f(9),
                               bg=sk.card, fg=sk.text, bd=0, highlightthickness=0,
                               padx=K.u(6), pady=K.u(4))
        sb_in = ttk.Scrollbar(wrap_in, orient="vertical", style="P.Vertical.TScrollbar",
                              command=self.text_in.yview)
        self.text_in.configure(yscrollcommand=sb_in.set)
        sb_in.pack(side="right", fill="y")
        self.text_in.pack(side="left", fill="both", expand=True)
        self._init_placeholder()
        self._build_ctx_menu()

        # ── 卡2：② 保存路径 ──
        c2 = K.Card(root, pad=12)
        c2.pack(fill="x", padx=K.u(pad_x), pady=(K.u(10), 0))
        row2 = tk.Frame(c2.body, bg=sk.card)
        row2.pack(fill="x")
        tk.Label(row2, text="② 保存路径", font=K.f(10, True),
                 bg=sk.card, fg=sk.text).pack(side="left")
        self.path_var = tk.StringVar(value=self.out_dir)
        self.path_var.trace_add("write", self._on_path_var_changed)
        self.entry_path = ttk.Entry(row2, textvariable=self.path_var, style="P.TEntry")
        self.entry_path.pack(side="left", fill="x", expand=True, padx=(K.u(10), K.u(8)))
        self.entry_path.update_idletasks()
        eh = self.entry_path.winfo_reqheight() / K.SCALE
        K.RoundButton(row2, text="恢复默认", command=self.reset_path, kind="ghost",
                      height=eh, font_size=9, width=76).pack(side="right")
        K.RoundButton(row2, text="选择…", command=self.choose_path, kind="ghost",
                      height=eh, font_size=9, width=64).pack(side="right", padx=(0, K.u(6)))

        # ── 卡3：③ 选项（原三个 LabelFrame 合并为一张卡） ──
        c3 = K.Card(root, pad=12)
        c3.pack(fill="x", padx=K.u(pad_x), pady=(K.u(10), 0))
        # ③-a 下载完成后打开文件夹（原 Radiobutton 组 → 分段选择器）
        r3a = tk.Frame(c3.body, bg=sk.card)
        r3a.pack(fill="x")
        tk.Label(r3a, text="③ 下载完成后", font=K.f(10, True),
                 bg=sk.card, fg=sk.text).pack(side="left")
        self.auto_open_var = tk.IntVar(value=self.auto_open_mode)
        self.auto_open_var.trace_add("write", self._on_auto_open_changed)
        self.auto_open_seg = K.SegmentedControl(
            r3a,
            [("off", "不打开"), ("root", "打开根目录"), ("each", "打开案件夹")],
            self._auto_open_key(self.auto_open_mode),
            command=self._on_auto_open_seg, width=260)
        self.auto_open_seg.pack(side="left", padx=(K.u(10), 0))
        # ③-b 下载后转换为 JPG
        r3b = tk.Frame(c3.body, bg=sk.card)
        r3b.pack(fill="x", pady=(K.u(9), 0))
        self.convert_var = tk.BooleanVar(value=self.convert_jpg)
        self.convert_var.trace_add("write", self._on_convert_changed)
        self.jpg_mode_var = tk.IntVar(value=self.jpg_mode)
        self.jpg_mode_var.trace_add("write", self._on_jpg_mode_changed)
        self.jpg_seg = K.SegmentedControl(
            r3b, [("single", "统一文件夹"), ("per", "按 PDF 分夹")],
            "single" if self.jpg_mode == JPG_MODE_SINGLE else "per",
            command=self._on_jpg_mode_seg, width=200)
        self.jpg_seg.pack(side="right")
        self.convert_check = K.RoundCheck(
            r3b, text="转换为 JPG 图片（长边 2000px 高质量）",
            variable=self.convert_var, command=self._sync_jpg_state)
        self.convert_check.pack(side="left")
        # ③-c 重复下载处理
        r3c = tk.Frame(c3.body, bg=sk.card)
        r3c.pack(fill="x", pady=(K.u(9), 0))
        self.skip_var = tk.BooleanVar(value=self.skip_existing)
        self.skip_var.trace_add("write", self._on_skip_existing_changed)
        K.RoundCheck(r3c, text="跳过已存在的同名文书（重复下载省时省流量）",
                     variable=self.skip_var).pack(side="left")

        # ── 开始按钮 ──
        self.btn_start = K.RoundButton(root, text="开始下载", command=self.on_start,
                                       kind="primary", height=sk.btn_h, font_size=12)
        self.btn_start.pack(fill="x", padx=K.u(pad_x), pady=(K.u(12), 0))

        # ── 进度条 ──
        prow = tk.Frame(root, bg=sk.bg)
        prow.pack(fill="x", padx=K.u(pad_x), pady=(K.u(10), 0))
        self.progress = K.RoundProgress(prow, height=9)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(prow, text="进度：0 / 0 份 (0%)",
                                        font=K.f(9), background=sk.bg,
                                        foreground=sk.muted)
        self.progress_label.pack(side="left", padx=(K.u(10), 0))

        # ── 日志（分级着色） ──
        c4 = K.Card(root, pad=10, auto=False, fill=True)
        c4.pack(fill="both", expand=True, padx=K.u(pad_x), pady=(K.u(10), 0))
        self.logview = K.LogView(c4.body)
        self.logview.pack(fill="both", expand=True)
        self.log = self.logview.txt  # 兼容既有 log_msg / 配置读写

        # ── 底部按钮 ──
        brow = tk.Frame(root, bg=sk.bg)
        brow.pack(fill="x", padx=K.u(pad_x), pady=(K.u(8), K.u(12)))
        K.RoundButton(brow, text="打开保存文件夹", command=self.open_folder,
                      kind="ghost", height=32, font_size=9,
                      width=110).pack(side="right")
        K.RoundButton(brow, text="清空日志", command=self.clear_log, kind="ghost",
                      height=32, font_size=9, width=76).pack(side="right", padx=(0, K.u(6)))

        # 若本程序是由更新流程启动的，接管原文件名并清掉旧版文件。
        # 旧进程刚退出时文件可能还被锁着，模块内部会重试；放后台线程，不拖慢启动。
        try:
            threading.Thread(target=self._settle_after_update, daemon=True).start()
        except Exception:
            pass

        # 启动时自动检查更新（静默；有新版才弹「更新内容 + 三选项」对话框）
        try:
            self.startup_update_check()
        except Exception:
            pass

        # 界面已就绪：启动主线程 UI 泵（worker 的日志/进度都靠它落地）
        self._pump()

    def _report_callback_exception(self, exc, val, tb):
        """Tk 回调里的任何未捕获异常都写进日志文件（界面照常继续）。"""
        try:
            write_log_file("[界面异常] %s: %s\n%s" % (
                getattr(exc, "__name__", exc), val,
                "".join(traceback.format_exception(exc, val, tb))))
        except Exception:  # noqa: BLE001
            pass

    # ---- worker 线程与主线程之间的唯一通道 ----
    def _post(self, fn):
        """把界面更新排进队列，由主线程的 _pump 执行。

        为什么不能像旧版那样直接 self.root.after(...)：
          Tkinter 不是线程安全的。worker 线程直接调 root.after 时，只要主线程
          此刻不在 mainloop 的事件派发状态里，_tkinter 就会抛
          RuntimeError("main thread is not in main loop")。而旧代码对这个异常的
          处理是不一致的：log_msg 里 except: pass 把日志静默吞掉；set_bar 里没保护
          于是直接杀死 worker 线程；连外层 except 里写日志的也是 log_msg（同样被吞）。
          最终现象就是：点了下载 → 界面卡住不动 → 日志停在半路 → 没有任何报错。
          改用队列后，worker 只做纯 Python 的 put，永远不可能失败。
        """
        try:
            self._ui_queue.put(fn)
        except Exception:  # noqa: BLE001
            pass

    def _pump(self):
        """主线程 UI 泵：把 worker 投递的闭包在主线程里执行。"""
        self._pump_job = None
        if self._closing:
            return
        try:
            # 每轮最多执行 200 个回调，而不是「抽干为止」：
            # 回调里往往会继续投递（开始下载 → 一串日志 → 可能的收尾动作），
            # 队列若一直不空，while True 会霸占主线程，界面（含「取消」按钮）
            # 直到这次下载结束都画不出来。留一点余量给 Tk 重绘，剩下的下一轮再说。
            for _ in range(200):
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    # 单个回调出错不能拖垮整个泵（否则后续状态复位全丢）——
                    # 这个「不中断」的行为是对的，但**不能什么都不留**：
                    # 否则用户看到的就是「点了没反应 + 日志空白 + 零报错」，
                    # 正是 v2.6 那次故障的形态，事后无从查起。
                    # 只落盘、不回灌队列：写日志本身在主线程做且极轻，
                    # 若它自己再抛，外层还有一层兜底。
                    try:
                        write_log_file("[UI-CALLBACK] " + traceback.format_exc()
                                       .replace("\n", "\n    "))
                    except Exception:  # noqa: BLE001
                        pass
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            pass
        try:
            self._pump_job = self.root.after(40, self._pump)
        except tk.TclError:
            self._pump_job = None  # 窗口已销毁

    def _on_root_configure(self, _evt=None):
        """换显示器（DPI 变化）检测 —— 节流 2 秒，<Configure> 触发非常频繁。"""
        now = time.time()
        if now - self._dpi_checked < 2.0:
            return
        self._dpi_checked = now
        try:
            changed = K.refresh_scale(self.root)
        except Exception:  # noqa: BLE001
            return
        if not changed:
            return
        # 只按新比例重设窗口尺寸：已建控件的像素尺寸 Tk 不会跟着变，
        # 硬重排整套界面属于「重建 UI」，代价与风险都不值得为这个场景承担。
        try:
            w, h = self._win_logical
            self.root.geometry("%dx%d" % (K.u(w), K.u(h)))
        except Exception:  # noqa: BLE001
            pass
        self.log_msg("⚠ 检测到显示器缩放变化（当前 %d%%）—— 已按新比例调整窗口；"
                     "重启程序后界面尺寸最准确" % round(K.SCALE * 100))

    def _settle_after_update(self):
        """更新后首次启动：登记「退出时把文件改成软件名_v实际版本号」+ 清理旧版文件。

        ⚠️ **启动时故意不改名**：实测把正在运行的 exe 改名会让本进程**出网永久失效**
        （连 DNS 都出不去，之前 v2.8/v2.9.0 的「更新完自动打开的新版一点下载就卡死、
        必须关掉重开」就是这个原因）。改名推迟到退出时由 autoupdate 的 atexit 钩子执行。

        传入 app_name / version 后，绿色版退出时会把旧名字（如 v2.9.0）改成当前版本
        （v2.10.0）；安装版因为文件名就是光名字，会被识别出来并保持固定名不变
        （快捷方式/卸载都认它，不能改）。名字已正确时该函数什么都不做。
        """
        try:
            autoupdate.settle_after_update(
                log_fn=self._settle_log, app_name=APP_NAME, version=VERSION)
        except Exception:
            pass

    # ---- 日志（线程安全：队列投递 + 自动加 [HH:MM:SS] + 落盘留痕）----
    def log_msg(self, msg):
        """任何线程都能安全调用：只做队列投递，永不抛异常。"""
        self._post(lambda m=msg: self._log_msg(m))

    def _log_msg(self, msg):
        line = datetime.now().strftime("[%H:%M:%S] ") + _safe_text(msg)
        write_log_file(line)  # 先落盘：即使界面刷新出问题，日志也不丢
        self._append_log(line)

    def _append_log(self, line):
        try:
            self.logview.append(line)  # LogView 按内容自动分级着色
        except Exception:  # noqa: BLE001
            pass

    def _settle_log(self, msg):
        """更新/改名相关日志：**先直接落盘**再投界面。

        为什么不用 log_msg：改名推迟到进程退出时执行（autoupdate 的 atexit 钩子），
        那一刻主线程的界面泵已经停了 —— 走 log_msg 的话这条记录只会进队列、
        永远不会被执行，日志里就看不到「文件名改成了什么」。落盘才是可靠留痕。
        """
        line = datetime.now().strftime("[%H:%M:%S] ") + _safe_text(msg)
        write_log_file(line)
        self._post(lambda l=line: self._append_log(l))

    def clear_log(self):
        self.logview.clear()

    def choose_path(self):
        d = filedialog.askdirectory(initialdir=self.out_dir or get_desktop())
        if d:
            self.path_var.set(d)  # trace 会自动同步 out_dir 并保存配置

    def reset_path(self):
        self.path_var.set(default_out_dir())

    def _save_cfg(self):
        """统一保存：一次写全所有配置项，避免各调用点漏参数。"""
        save_config(self.out_dir, self.auto_open_mode, self.convert_jpg, self.jpg_mode,
                    skip_existing=self.skip_existing)

    def _on_path_var_changed(self, *_):
        val = self.path_var.get().strip()
        if not val:
            return
        self.out_dir = val
        # ⚠️ 防抖：trace 是每次击键都触发，用户粘贴一段长路径会连着写几十次
        #    磁盘（open + json.dump）。慢盘/网络盘上会明显卡输入。
        #    这里只重排一次延迟保存，停止输入 400ms 后才真正落盘。
        try:
            if self._path_save_job:
                self.root.after_cancel(self._path_save_job)
        except Exception:  # noqa: BLE001
            pass
        self._path_save_job = self.root.after(400, self._save_cfg)

    def _on_auto_open_changed(self, *_):
        self.auto_open_mode = int(self.auto_open_var.get())
        self._save_cfg()

    def _on_convert_changed(self, *_):
        self.convert_jpg = bool(self.convert_var.get())
        self._save_cfg()

    def _on_jpg_mode_changed(self, *_):
        self.jpg_mode = int(self.jpg_mode_var.get())
        self._save_cfg()

    def _on_skip_existing_changed(self, *_):
        self.skip_existing = bool(self.skip_var.get())
        self._save_cfg()

    def _sync_jpg_state(self):
        # 未勾选“转 JPG”时，禁用目录模式分段选择器
        self.jpg_seg.set_enabled(bool(self.convert_var.get()))

    # ---- 分段选择器 ↔ 配置变量（沿用原 trace 保存配置） ----
    @staticmethod
    def _auto_open_key(mode):
        return {AUTO_OPEN_OFF: "off", AUTO_OPEN_ROOT: "root",
                AUTO_OPEN_EACH: "each"}.get(mode, "root")

    def _on_auto_open_seg(self, key):
        self.auto_open_var.set({"off": AUTO_OPEN_OFF, "root": AUTO_OPEN_ROOT,
                                "each": AUTO_OPEN_EACH}[key])

    def _on_jpg_mode_seg(self, key):
        self.jpg_mode_var.set(JPG_MODE_SINGLE if key == "single" else JPG_MODE_PERPDF)

    # ---- 进度条（线程安全：一律走 UI 队列）----
    def reset_bar(self):
        self._post(self._reset_bar)

    def _reset_bar(self):
        self.progress.set_value(0)
        self.progress_label.configure(text="进度：0 / 0 份 (0%)")

    def set_bar(self, total, done):
        self._post(lambda t=total, d=done: self._set_bar(t, d))

    def _set_bar(self, total, done):
        pct = int(done * 100 / total) if total > 0 else 0
        self.progress.set_value(pct, stopped=self.stop_event.is_set())
        self.progress_label.configure(text="进度：%d / %d 份 (%d%%)" % (done, total, pct))

    def open_folder(self):
        target = self.last_case_dir or self.out_dir
        _open_in_explorer(target, self.log_msg, "还没有可打开的文件夹。")

    def open_base_folder(self):
        _open_in_explorer(self.out_dir, self.log_msg)

    def open_case_folder(self, case_dir):
        _open_in_explorer(case_dir, self.log_msg)

    # ---- 文本框占位符（置灰）+ 粘贴按钮 + 右键菜单 ----
    def _init_placeholder(self):
        self.placeholder_text = (
            "例：【某市某区人民法院】…点击链接查阅：https://zxfw.court.gov.cn/...?qdbh=...&sdbh=...&sdsin=...\n"
            "可一次粘贴多个链接（每行一个，或混在多条短信里均可）。"
        )
        self.is_placeholder = True
        self.text_in.insert("1.0", self.placeholder_text)
        self.text_in.configure(fg=K.SKIN.faint)
        self.text_in.bind("<FocusIn>", self._on_focus_in)
        self.text_in.bind("<FocusOut>", self._on_focus_out)
        self.text_in.bind("<<Paste>>", self._on_native_paste)

    def _clear_placeholder(self):
        if self.is_placeholder:
            self.text_in.delete("1.0", "end")
            self.text_in.configure(fg=K.SKIN.text)
            self.is_placeholder = False

    def _restore_placeholder(self):
        if not self.is_placeholder and self.text_in.get("1.0", "end").strip() == "":
            self.text_in.delete("1.0", "end")
            self.text_in.insert("1.0", self.placeholder_text)
            self.text_in.configure(fg=K.SKIN.faint)
            self.is_placeholder = True

    def _on_focus_in(self, *_):
        self._clear_placeholder()

    def _on_focus_out(self, *_):
        self._restore_placeholder()

    def _on_native_paste(self, *_):
        # Ctrl+V 等原生粘贴：先清占位符，再交给默认行为插入
        self._clear_placeholder()
        return  # 不拦截，默认粘贴照常执行

    def clear_text(self):
        """清空按钮：删除全部内容并恢复置灰占位符。"""
        self.text_in.delete("1.0", "end")
        self.text_in.insert("1.0", self.placeholder_text)
        self.text_in.configure(fg=K.SKIN.faint)
        self.is_placeholder = True
        self.text_in.focus_set()

    def paste_text(self):
        """粘贴按钮：清空占位符后，把剪贴板内容插入光标处。"""
        self._clear_placeholder()
        try:
            clip = self.root.clipboard_get()
        except Exception:
            clip = ""
        if clip:
            self.text_in.insert("insert", clip)
        self.text_in.focus_set()

    def _build_ctx_menu(self):
        self.ctx_menu = tk.Menu(self.text_in, tearoff=0)
        self.ctx_menu.add_command(label="粘贴", command=self._ctx_paste)
        self.ctx_menu.add_command(label="复制", command=lambda: self.text_in.event_generate("<<Copy>>"))
        self.ctx_menu.add_command(label="剪切", command=lambda: self.text_in.event_generate("<<Cut>>"))
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="全选", command=lambda: self.text_in.tag_add("sel", "1.0", "end"))
        self.text_in.bind("<Button-3>", self._show_ctx_menu)

    def _show_ctx_menu(self, event):
        try:
            self.ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx_menu.grab_release()

    def _ctx_paste(self):
        self._clear_placeholder()
        try:
            clip = self.root.clipboard_get()
            if clip:
                self.text_in.insert("insert", clip)
        except Exception:
            pass

    # ---- 顶栏（自绘，替代原生菜单栏）----
    def _set_header_cursor(self, on):
        try:
            self._header.configure(cursor=("hand2" if on else ""))
        except Exception:  # noqa: BLE001
            pass

    def _hover_header_link(self, item, key, on):
        """顶栏文字入口的悬停态：变色 + 手型光标。"""
        sk = K.SKIN
        self._hover_link = key if on else None
        try:
            self._header.itemconfigure(
                item, fill=(sk.accent if on else sk.header_link))
        except Exception:  # noqa: BLE001
            return
        self._set_header_cursor(on)

    def _add_header_link(self, cv, label, right, cy, command):
        """在顶栏右侧画一个纯文字入口（无背景、无边框），返回它的左边界。

        热区用一层**透明矩形**实现：Canvas 的 create_text 命中范围只有字形本身，
        「使」「用」之间的空隙鼠标移过去就触发 <Leave> → 悬停色一闪一闪。
        矩形与文字绑同一组回调，所以点哪里都算点中。

        ⚠️ 返回左边界是为了让调用方「从右往左」排版：先画最右的「检查更新」，
        再用它实测的左边界减间距排「使用说明」。宽度不写死，换字号/换 DPI 都不错位。
        """
        sk = K.SKIN
        tid = cv.create_text(right, cy, text=label, font=K.f(10),
                             fill=(sk.accent if self._hover_link == label
                                   else sk.header_link), anchor="e")
        bb = cv.bbox(tid)
        left = bb[0] if bb else right - K.u(40)
        pad_x, pad_y = K.u(6), K.u(5)
        hid = cv.create_rectangle(left - pad_x, bb[1] - pad_y,
                                  right + pad_x, bb[3] + pad_y,
                                  fill="", outline="")
        for iid in (tid, hid):
            cv.tag_bind(iid, "<Enter>",
                        lambda _e, t=tid, k=label: self._hover_header_link(t, k, True))
            cv.tag_bind(iid, "<Leave>",
                        lambda _e, t=tid, k=label: self._hover_header_link(t, k, False))
            cv.tag_bind(iid, "<Button-1>", lambda _e, c=command: c())
        return left

    def _paint_header(self, _e=None):
        """重画顶栏：白底 + 1px 分割线 + 图标 + 标题 + 版本徽章 + 右上文字入口。

        ⚠️ 每次 <Configure> 都会全量重画，这里的 item 都是「画完即弃」。
        唯一要跨重绘保留的状态是 self._hover_link —— 否则鼠标停在按钮上时
        窗口一缩放，按钮颜色就跳回默认灰。
        """
        cv = getattr(self, "_header", None)
        if cv is None or not cv.winfo_exists():
            return
        sk = K.SKIN
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 10:
            return
        # 底：纯白 + 底部 1 **物理**像素分割线。
        # 这里故意写 1 而不是 K.u(1)：150% 缩放下 K.u(1) 会取整成 2、250% 下成 3，
        # 比参考图那根细线粗一倍，视觉上变成「一条灰带」。
        cv.create_rectangle(0, 0, w, h, fill=sk.header_bg, outline=sk.header_bg)
        cv.create_rectangle(0, h - 1, w, h, fill=sk.header_line,
                            outline=sk.header_line)

        # 图标
        gx, gs = K.u(20), K.u(28)
        gy = (h - gs) / 2.0
        if self._logo_photo is not None:
            # 288px 源图按 DPI 抽样到最接近 gs 的档位（整数倍 subsample）
            n = max(1, min(12, round(288.0 / max(gs, 1))))
            photo = self._logo_cache.get(n)
            if photo is None:
                try:
                    photo = self._logo_photo.subsample(n, n)
                except Exception:  # noqa: BLE001
                    photo = self._logo_photo
                self._logo_cache[n] = photo   # 必须挂引用，否则下一帧就被 GC 掉
            cv.create_image(gx + gs / 2.0, gy + gs / 2.0, image=photo)
        else:
            K.rr(cv, gx, gy, gx + gs, gy + gs, K.u(7), sk.gold)
            for i, t in enumerate((0.36, 0.50, 0.64)):
                cv.create_line(gx + gs * 0.26, gy + gs * t,
                               gx + gs * (0.74 if i < 2 else 0.58), gy + gs * t,
                               fill=sk.header_bg, width=K.u(2), capstyle="round")

        # 标题
        tx = gx + gs + K.u(15)
        title_id = cv.create_text(tx, h / 2.0, text="法院文书下载器",
                                  font=K.f(11, True), fill=sk.header_title,
                                  anchor="w")

        # 版本徽章：紧跟标题右侧 14px。宽度按文字实测而不是写死 58，
        # 版本号从 v2.9.9 变 v2.10.10 时不会溢出。
        badge_id = cv.create_text(0, 0, text="v" + VERSION, font=K.f(9),
                                  fill=sk.badge_fg, anchor="w")
        bb = cv.bbox(badge_id)
        tw = (bb[2] - bb[0]) if bb else K.u(40)
        bx = cv.bbox(title_id)[2] + K.u(14)
        bh, by = K.u(22), (h - K.u(22)) / 2.0
        bw = tw + 2 * K.u(10)
        K.rr(cv, bx, by, bx + bw, by + bh, K.u(6), sk.badge_bg)
        cv.coords(badge_id, bx + bw / 2.0, h / 2.0)
        cv.itemconfigure(badge_id, anchor="center")
        # ⚠️ 徽章底板是后画的，会盖在先画的文字上面 → 必须把文字提到最上层，
        #    否则看到的是一个空白的浅蓝圆角块（这个坑当场踩过）。
        cv.tag_raise(badge_id)

        # 右上文字入口：右距 17、组距 16（从右往左依次排）
        right = w - K.u(17)
        for label, action in (("检查更新", self.check_update_manual),
                              ("使用说明", self.show_help)):
            right = self._add_header_link(cv, label, right, h / 2.0, action) - K.u(16)

    def show_help(self):
        """使用说明窗口。"""
        sk = K.SKIN
        win = tk.Toplevel(self.root)
        win.title("使用说明 · 法院文书下载器 v" + VERSION)
        win.configure(bg=sk.bg)
        win.geometry("%dx%d" % (K.u(680), K.u(580)))
        win.resizable(True, True)
        apply_window_icon(win)
        try:
            win.transient(self.root)
        except Exception:
            pass
        body = tk.Frame(win, bg=sk.card,
                        highlightbackground=sk.border, highlightthickness=1)
        body.pack(fill="both", expand=True, padx=K.u(14), pady=(K.u(14), 0))
        txt = scrolledtext.ScrolledText(
            body, wrap="word", font=K.f(10),
            bg=sk.card, fg=sk.text, bd=0, highlightthickness=0,
            padx=K.u(12), pady=K.u(10),
        )
        txt.pack(fill="both", expand=True)
        content = (
            "★ 项目仓库：https://github.com/jianRY/dianzisongda-xiazaiqi\n"
            "   （新版发布、历史版本下载都在这里，欢迎 Star）\n\n"
            "【法院文书下载器 · 使用说明 v%s】\n\n"
            "本工具用于从「全国法院统一送达平台」发来的电子送达短信/链接中，\n"
            "自动下载对应的裁判文书（PDF）到本地文件夹。\n\n"
            "————————— 使用步骤 —————————\n"
            "1. 粘贴：把法院送达短信整段（或其中的链接）粘贴到顶部文本框。\n"
            "   · 支持批量：一次粘贴多个链接，会自动依次下载。\n"
            "   · 文本框右键有「粘贴 / 复制 / 剪切 / 全选」菜单。\n"
            "   · 「🗑 清空」按钮可一键清掉内容并恢复占位提示。\n"
            "2. 保存路径：默认「桌面\\文书」，可点「选择」改为任意文件夹；\n"
            "   程序会记住上次的选择；开始下载前会校验路径是否可写。\n"
            "3. 选项：\n"
            "   · 下载完成后打开：不打开 / 打开根目录 / 打开每个案件文件夹。\n"
            "   · PDF 转 JPG：勾选后自动把 PDF 转成图片。\n"
            "     - 模式一：所有图片放到一个「图片」文件夹；\n"
            "     - 模式二：按 PDF 文件名各自建子文件夹存放。\n"
            "     - 图片长边 2000 像素、高质量、短边自适应；多页自动加页码。\n"
            "   · 重复下载处理：默认勾选「跳过已存在的同名文书」，\n"
            "     识别到案号时会复用上次的案件文件夹，已下过的文书直接跳过，\n"
            "     省时省流量；需要整案重新下载就先取消勾选。\n"
            "4. 开始：点「开始下载」，进度条实时显示「已下载 / 总份数 (百分比)」。\n"
            "   · 同一案件的多份文书会用 %d 个线程并发下载，速度更快。\n"
            "   · 下载过程中按钮变为「取消下载」，点击可中途取消未完成任务。\n"
            "   · 下载中直接关闭窗口会先询问，未完成文件会被清理。\n"
            "   · 每个案件会自动建子文件夹：法院名_案号_<启动时间戳>，\n"
            "     防止同名法院不同链接的文件被合并。\n\n"
            "————————— 自动更新 —————————\n"
            "· 程序启动时会自动检查更新，发现新版会弹窗显示本次更新内容。\n"
            "· 弹窗三选：立即更新 / 本次忽略 / 以后不再提醒。\n"
            "  选「以后不再提醒」后启动不再自动检查（顶栏「检查更新」仍可手动检查）。\n"
            "· 确认更新后显示下载进度与速度，可随时取消。\n"
            "· 下载完成后新版本直接放到本程序所在位置，提示「更新完成」后本程序退出；\n"
            "  重新打开程序即为新版本（不会自动帮你打开新窗口）。\n"
            "  旧版本文件会在下次启动时清理，程序文件名保持不变，桌面快捷方式继续可用。\n"
            "  请把程序放在有写入权限的位置（如桌面），放在只读目录里无法自动更新。\n"
            "· 更新失败时弹窗会直接给出官网与 Release 下载地址，可手动下载安装。\n"
            "· 本程序有两种版本，功能完全一致：绿色单文件版（免安装，放哪都能跑）\n"
            "  和安装版（会创建开始菜单/桌面快捷方式，可随时在系统「已安装的应用」里卸载）。\n\n"
            "————————— 常见问题 —————————\n"
            "Q：提示下载失败 / 链接无效？\n"
            "A：电子送达链接有时效，请重新从法院短信复制最新链接再试。\n\n"
            "Q：下载到的文件只有几 KB？\n"
            "A：多为链接已过期或需重新获取，请换用最新短信链接。\n\n"
            "Q：转 JPG 后图片打不开？\n"
            "A：请确认已勾选「PDF 转 JPG」且磁盘有足够空间。\n\n"
            "————————— 备注 —————————\n"
            "本工具仅供本人依法处理诉讼事务使用，请遵守相关平台与法律规定。\n"
            % (VERSION, MAX_WORKERS)
        )
        txt.insert("1.0", content)
        txt.configure(state="disabled")
        # 底部按钮行：v2.16.0 去掉原生菜单栏后，「导出诊断日志 / 关于」原本的两个
        # 菜单位置没有了，收到这里 —— 比在顶栏再挤两个入口更省地方，也不打断主流程。
        bar = tk.Frame(win, bg=sk.bg)
        bar.pack(fill="x", padx=K.u(14), pady=(K.u(10), K.u(12)))
        K.RoundButton(bar, text="关闭", command=win.destroy, kind="ghost",
                      height=32, font_size=9, width=72).pack(side="right")
        K.RoundButton(bar, text="关于", command=self.show_about, kind="ghost",
                      height=32, font_size=9, width=72).pack(side="right",
                                                             padx=(0, K.u(8)))
        K.RoundButton(bar, text="导出诊断日志", command=self.export_diag_log,
                      kind="ghost", height=32, font_size=9,
                      width=112).pack(side="left")

    def export_diag_log(self):
        """把最近几天的日志脱敏后另存，供用户发给技术支持。

        为什么需要这个：用户报障时通常直接把整个日志目录打包发出去，而日志里有
        案件信息。这里产出的副本会去掉送达凭证与文书文件名，只剩时间线与错误原因 ——
        够定位问题，但拿到它也开不了别人的案子。
        """
        try:
            d = log_dir()
            files = sorted(glob.glob(os.path.join(d, "运行日志_*.log")))[-3:]
            if not files:
                messagebox.showinfo("导出诊断日志", "还没有日志可以导出。")
                return
            chunks = []
            for p in files:
                try:
                    chunks.append(io.open(p, "r", encoding="utf-8", errors="replace").read())
                except OSError:
                    pass
            if not chunks:
                messagebox.showinfo("导出诊断日志", "日志文件读不出来，可能已被清理。")
                return
            text = redact_text("\n".join(chunks))
            # 文书文件名也隐去：有些文书名直接含当事人姓名
            text = re.sub(r"(已保存：)[^\s(]+", r"\1（文件名已隐藏）", text)
            text = re.sub(r"(已存在，跳过：)[^\s]+", r"\1（文件名已隐藏）", text)
            text = re.sub(r"(下载失败：)[^\s]+\.(pdf|docx?|xlsx?|ofd|jpe?g|png)",
                          r"\1（文件名已隐藏）.\2", text, flags=re.I)
            out = os.path.join(get_desktop(), "诊断日志_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
            with io.open(out, "w", encoding="utf-8") as f:
                f.write(text)
            messagebox.showinfo(
                "导出诊断日志",
                "已导出脱敏后的日志：\n%s\n\n"
                "文件里已去除链接访问凭证与文书文件名，可以直接发给技术支持。" % out)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导出诊断日志", "导出失败：%s" % e)

    def show_about(self):
        messagebox.showinfo(
            "关于",
            "法院文书下载器  v%s\n\n"
            "从全国法院统一送达平台自动下载电子送达文书。\n"
            "数据来源：zxfw.court.gov.cn\n\n"
            "项目仓库：https://github.com/jianRY/dianzisongda-xiazaiqi\n\n"
            "仅供本人依法处理诉讼事务使用。" % VERSION,
        )

    # ---- 自动检查更新（通用模块 autoupdate 承担 UI 与下载） ----
    def check_update_manual(self):
        """顶栏「检查更新」手动检查：不受「以后不再提醒」开关影响。"""
        autoupdate.run_update_check(
            self.root,
            app_name=APP_NAME,
            current_version=VERSION,
            latest_api_url=GITHUB_API_LATEST,
            config_file=config_path(),
            install_helper=autoupdate.windows_replace_and_restart,
            log_fn=self.log_msg,
            manual=True,
            on_before_install=self._before_install,
        )

    def startup_update_check(self):
        """启动静默检查：发现新版弹「更新内容 + 三选项」对话框。"""
        autoupdate.run_update_check(
            self.root,
            app_name=APP_NAME,
            current_version=VERSION,
            latest_api_url=GITHUB_API_LATEST,
            config_file=config_path(),
            install_helper=autoupdate.windows_replace_and_restart,
            log_fn=self.log_msg,
            manual=False,
            on_before_install=self._before_install,
        )

    def _before_install(self):
        """新版本已就位、程序即将退出前的收尾：停掉下载并保存配置。"""
        try:
            self.stop_event.set()
        except Exception:  # noqa: BLE001
            pass
        self.root.update()

    def on_start(self):
        if self.running:
            return
        # ① 若文本框仍是占位符或空内容，提示并返回
        if getattr(self, "is_placeholder", False):
            messagebox.showwarning("请先粘贴", "请先粘贴法院送达短信或链接再开始下载。")
            self.text_in.focus_set()
            return
        text = self.text_in.get("1.0", "end")
        if not text.strip():
            messagebox.showwarning("请先粘贴", "文本框为空，请先粘贴法院送达短信或链接。")
            return
        # ② 同步路径（保险）：以输入框当前内容为准
        self.out_dir = self.path_var.get().strip() or self.out_dir
        if not self.out_dir:
            messagebox.showwarning("路径为空", "请填写或选择保存路径。")
            return
        # ③ 路径预校验：父目录必须可写、能创建
        test_file = os.path.join(self.out_dir, ".wb_write_test")
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            with open(test_file, "w") as f:
                f.write("0")
        except OSError as e:
            messagebox.showerror(
                "保存路径不可用",
                "无法写入所选路径：\n%s\n\n请改选一个有写入权限的文件夹。" % e,
            )
            return
        finally:
            # 无论成功失败都尝试清掉探针文件，避免在用户目录留垃圾
            try:
                if os.path.exists(test_file):
                    os.remove(test_file)
            except OSError:
                pass
        tasks = extract_tasks(text)
        if not tasks:
            messagebox.showwarning("无法识别", "未能从文本中提取到送达链接（需含 qdbh/sdbh/sdsin）。")
            return
        # 重置取消标志（加锁：与 worker 收尾时的 clear() 互斥，
        # 否则上一轮的延迟清理可能把本轮的取消标志清掉）
        with self._cancel_lock:
            self.stop_event.clear()
        self.btn_start.configure_state(state="normal", text="取消下载", command=self.on_cancel)
        # ⚠️ running 必须在按钮切换「成功之后」才置 True。反过来写的话，一旦界面操作
        #    抛异常，running 已经为 True，用户再点按钮会被 `if self.running: return` 挡掉，
        #    按钮永久卡在「取消下载」——整个程序看起来就是死的。
        self.running = True
        self._log_msg("法院文书下载器 v%s" % VERSION)
        self._log_msg("✓ 识别到 %d 个送达链接，将依次下载。" % len(tasks))
        t = threading.Thread(target=self.worker, args=(tasks,), daemon=True)
        t.start()

    def on_cancel(self):
        """取消下载：设置 stop_event，未完成的任务会尽快退出。"""
        if not self.running:
            return
        if messagebox.askyesno(
            "确认取消",
            "确定要取消当前下载吗？\n\n已完成的部分文件会保留，未完成的任务将停止。",
        ):
            self.stop_event.set()
            self.log_msg("⏸ 收到取消请求，正在停止剩余任务…")
            # 明确告知最坏等待时间：urlopen 是阻塞调用，取消最多要等一个超时周期
            # （已降到 30s）才生效。说清楚用户才不会以为程序死了而强杀（强杀会留半截 PDF）。
            self.btn_start.configure_state(state="disabled", text="正在取消…（最多等 30 秒）")

    def on_close(self):
        """关闭窗口：下载中先确认，并让 worker 有机会清理半成品文件。"""
        if self.running:
            if not messagebox.askyesno(
                "确认退出",
                "正在下载中，确定要退出吗？\n\n未完成的文件将被丢弃，已下载的部分会保留。",
            ):
                return
            self.stop_event.set()
            self.log_msg("⏸ 正在退出，等待当前下载任务收尾…")
            # 给 worker 一点时间清理（最多约 1.5 秒），再关闭窗口
            self.root.after(1500, self._force_close)
            return
        self._force_close()

    def _cancel_pump(self):
        if self._pump_job is not None:
            try:
                self.root.after_cancel(self._pump_job)
            except Exception:  # noqa: BLE001
                pass
            self._pump_job = None

    def _force_close(self):
        """真正关窗：先停 UI 泵再销毁窗口，避免销毁后回调乱跑。"""
        self._closing = True
        self._cancel_pump()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _ui(self, fn):
        """把界面回调排到主线程执行。

        worker 线程调用它弹提示 / 复位按钮 / 打开文件夹。所有跨线程界面操作统一
        走 UI 队列 —— 旧版直接 root.after 会遇到「主线程不在 mainloop」而抛
        RuntimeError，且异常发生在非主线程会打断 worker 收尾。
        """
        self._post(fn)

    def worker(self, tasks):
        # 信任库 = 系统库 + 随包内置的公共 CA（叠加，不替换）。
        # ⚠️ 不能再用裸 ssl.create_default_context()：它只读系统库，
        #    新装 Windows 常缺法院站点所需的根证书（CFCA EV ROOT），
        #    请求清单时会直接报 CERTIFICATE_VERIFY_FAILED（详见 ssl_trust.py）。
        ctx, _ssl_info = ssl_trust.build_ssl_context(log_fn=self.log_msg)
        total_ok = 0
        total_all = 0
        total_skip = 0   # 已存在而跳过的份数（不计入成功）
        done_count = 0
        total_docs = 0
        cancelled = False
        failed_cases = 0        # 连文书清单都没取到的链接数（链接过期 / 接口报错）
        last_error = ""         # 最后一次取清单失败的真实原因（报给用户，别再让他猜）
        opened_any_case = False  # each 模式下是否已打开过案件文件夹（失败兜底用）
        # ⚠️ reset_bar 与建目录都必须挡在这层 try 里：reset_bar 是从 worker 线程调
        #    root.after，主线程若不在 mainloop（或窗口已销毁）会抛 RuntimeError/TclError。
        #    若放在下面那个主 try 之外，异常会让 worker 线程**静默死亡**、running 永远
        #    停在 True、按钮卡在「取消下载」——用户看到的就是「点了下载没反应」。
        try:
            self.reset_bar()
            # 先把保存根目录建出来：即使所有链接都失效，点下载后也能看到文件夹（并按设置打开），
            # 不会出现「点了没反应、桌面什么都没有」的困惑。
            os.makedirs(self.out_dir or default_out_dir(), exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            for idx, params in enumerate(tasks, 1):
                # 进入下一个案件前先看取消
                if self.stop_event.is_set():
                    self.log_msg("⏹ 已取消，跳过剩余 %d 个案件。" % (len(tasks) - idx + 1))
                    cancelled = True
                    break
                self.log_msg("")
                self.log_msg("==== 案件 %d / %d ====" % (idx, len(tasks)))
                # 只落指纹，不落原链接：qdbh/sdbh/sdsin 是免登录下载全部文书的凭证。
                # 指纹足以判断「是不是同一条链接、会不会过期」，但拿着它打不开任何文书。
                self.log_msg("链接：%s" % url_fingerprint(params.get("url", "")))
                # 取清单要联网，慢的时候界面会有几秒没动静；先给一行可见反馈，
                # 免得用户以为卡死了（v2.10.0 起）。正常情况这条几乎瞬间就被下一行顶掉。
                self.log_msg("⏳ 正在连接法院平台获取文书清单…")
                try:
                    # 网络类失败自动重试 3 次；「链接过期」不重试，直接报原因
                    docs = fetch_doc_list(
                        params, ctx,
                        on_retry=lambda n, err: self.log_msg(
                            "⚠ 取清单失败（第 %d 次尝试）：%s —— 稍后自动重试…"
                            % (n, _safe_text(err).strip() or type(err).__name__)))
                except Exception as e:  # noqa: BLE001
                    failed_cases += 1
                    # 证书类错误单独翻成人话：新电脑缺根证书时报的原始英文错误
                    # 用户完全看不懂，会误以为「程序坏了」或「链接过期」。
                    ssl_hint = ssl_trust.describe_cert_error(e)
                    if ssl_hint:
                        last_error = ssl_hint
                        self.log_msg("✗ 安全连接校验失败（不是链接过期，别去重新取短信）")
                        for line in ssl_hint.splitlines():
                            self.log_msg("   " + line)
                    else:
                        last_error = _safe_text(e).strip() or type(e).__name__
                        self.log_msg("✗ 获取清单失败：%s" % last_error)
                        self.log_msg("   该链接未能取到文书清单，已跳过（链接多半已过期）。")
                    continue
                self.log_msg("✓ 找到 %d 份文书" % len(docs))
                total_docs += len(docs)
                self.set_bar(total_docs, done_count)

                # ⚠️ 键存在但值为 None 时 get 的默认值不生效 → 必须再 or 一次，
                #    否则下面 court + "_" 会抛 TypeError（接口换版本时踩过）。
                court = docs[0].get("c_fymc") or "未知法院"
                caseno = params["caseno"] or ""
                base = self.out_dir or default_out_dir()
                os.makedirs(base, exist_ok=True)
                # 案件文件夹前缀 = 法院名[_案号]。只有识别到案号才有「同一案件」的判定依据，
                # 所以仅在 caseno 非空时才允许复用旧文件夹 —— 否则同一法院的不同链接会被合并。
                prefix = sanitize_filename((court + ("_" + caseno if caseno else "")).strip("_ "))
                case_dir = None
                if self.skip_existing and caseno:
                    case_dir = _find_existing_case_dir(base, prefix)
                if case_dir:
                    self.log_msg("→ 复用已有案件文件夹（同案重下，跳过已下载的文书）")
                else:
                    # 新建文件夹带启动时间戳（年月日时分秒），避免同名法院不同链接合并
                    folder = sanitize_filename(
                        (court + ("_" + caseno if caseno else "") + "_"
                         + time.strftime("%Y%m%d%H%M%S")).strip("_ "))
                    case_dir = os.path.join(base, folder)
                os.makedirs(case_dir, exist_ok=True)
                with self._cancel_lock:
                    self.last_case_dir = case_dir
                self.log_msg("→ 保存目录：%s" % case_dir)
                self.log_msg("→ 启用 %d 线程并发下载…" % MAX_WORKERS)

                # 准备每份文书的下载任务（先确定本地路径，避免并发下重名竞态）
                jobs = []  # [(i, d, full_path, raw, ext, base_name), ...]
                used_names = set()
                for i, d in enumerate(docs, 1):
                    raw = d.get("c_wsmc") or ("文书%d" % i)
                    ext = safe_ext(d.get("c_wjgs"), d.get("wjlj", ""))
                    base_name = sanitize_filename(raw)
                    filename = base_name + ext
                    full = os.path.join(case_dir, filename)
                    dup = 1
                    while full in used_names or os.path.exists(full):
                        filename = "%s(%d)%s" % (base_name, dup, ext)
                        full = os.path.join(case_dir, filename)
                        dup += 1
                    used_names.add(full)
                    jobs.append((i, d, full, raw, ext, base_name))

                def _do_one(job):
                    """单个文书下载 + 转换（在工作线程内执行）。"""
                    i_, d_, full_, raw_, ext_, base_name_ = job
                    if self.stop_event.is_set():
                        return (i_, raw_, "cancelled", None, full_)
                    # 已存在同名文件跳过：重复下载同一案件时省时省流量
                    # （复用了旧案件文件夹才可能命中；需要重下时取消勾选即可）
                    if self.skip_existing and _looks_complete(full_):
                        msg = "    ⏭ 已存在，跳过：%s" % os.path.basename(full_)
                        # 上次可能没勾转 JPG，本次勾了 → 补转一次，否则用户会以为转换丢了
                        if (self.convert_jpg
                                and os.path.basename(full_).lower().endswith(".pdf")):
                            try:
                                if not _jpg_output_exists(full_, case_dir, self.jpg_mode):
                                    cnt = convert_pdf_to_jpg(full_, case_dir, self.jpg_mode)
                                    msg += "  ✓ 补转 JPG：%d 页" % cnt
                            except Exception as e2:  # noqa: BLE001
                                msg += "  ✗ 转 JPG 失败：%s" % e2
                        return (i_, raw_, "skip", msg, full_)
                    # 流水行不再打印文书名：日志可能被整份外发，而文书名有时直接
                    # 含当事人姓名（「XX 与 YY 民间借贷纠纷判决书」）。序号 + 进度条
                    # 已经说明进度，具体是哪份，看下载完的文件夹即可。
                    self.log_msg("[%d/%d] 下载中" % (i_, len(docs)))
                    try:
                        download_file(
                            d_.get("wjlj"), full_, ctx,
                            cancel_check=lambda: self.stop_event.is_set(),
                        )
                        size = os.path.getsize(full_)
                        msg = "    ✓ 已保存：%s (%d 字节)" % (os.path.basename(full_), size)
                        # 同步转 JPG（fitz 用全局 lock 串行化，避免崩溃）
                        if (
                            self.convert_jpg
                            and os.path.basename(full_).lower().endswith(".pdf")
                            and not self.stop_event.is_set()
                        ):
                            try:
                                cnt = convert_pdf_to_jpg(full_, case_dir, self.jpg_mode)
                                msg += "  ✓ 转 JPG：%d 页" % cnt
                            except Exception as e2:  # noqa: BLE001
                                msg += "  ✗ 转 JPG 失败：%s" % e2
                        return (i_, raw_, "ok", msg, full_)
                    except Exception as e:  # noqa: BLE001
                        return (i_, raw_, "fail", "    ✗ 失败：%s" % e, full_)

                success = 0
                skipped = 0
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    futures = {ex.submit(_do_one, j): j for j in jobs}
                    for fut in as_completed(futures):
                        try:
                            i, raw, status, msg, full = fut.result()
                        except Exception as e:  # noqa: BLE001
                            self.log_msg("    ✗ 任务异常：%s" % e)
                            done_count += 1
                            self.set_bar(total_docs, done_count)
                            continue
                        self.log_msg(msg)
                        if status == "ok":
                            success += 1
                        elif status == "skip":
                            # ⚠️ 跳过**不等于下载成功**：单独计数，否则「勾了跳过已存在
                            #    + 文件全都已下过」时，会弹出「108 份文书已下载」而实际一份
                            #    都没下 —— 这正是 v2.6「下载失败却提示成功」那类坑的变种。
                            skipped += 1
                        elif status == "cancelled":
                            # 已取消：剩余 future 还在跑，等它们自己看到 stop_event 退出
                            pass
                        done_count += 1
                        self.set_bar(total_docs, done_count)
                        # 用户中途取消：尽早取消未启动的 future
                        if self.stop_event.is_set():
                            for f2 in futures:
                                if not f2.running() and not f2.done():
                                    f2.cancel()
                            cancelled = True

                total_ok += success
                total_skip += skipped
                total_all += len(docs)
                if skipped and not success:
                    self.log_msg("--- 本案完成：全部 %d 份均已存在，无需下载 ---" % skipped)
                else:
                    self.log_msg("--- 本案完成：%d / %d 份 ---" % (success, len(docs)))

                # 模式：打开每个案件文件夹
                if self.auto_open_mode == AUTO_OPEN_EACH and not cancelled:
                    _open_in_explorer(case_dir, self.log_msg)
                    opened_any_case = True

                if cancelled:
                    break

            self.set_bar(total_docs, done_count)
            if cancelled:
                self.log_msg("")
                self.log_msg("=== 已取消：成功 %d / %d 份 ===" % (total_ok, max(total_all, done_count)))
                # 闭包用默认参数绑定数值：_ui 是延迟执行，直接引用外层变量会读到后续变化
                self._ui(lambda ok=total_ok, al=total_all: messagebox.showinfo(
                    "已取消", "下载已取消。成功 %d / %d 份，详见日志。" % (ok, al)))
            else:
                self.log_msg("")
                self.log_msg("=== 全部完成：成功 %d / %d 份（跳过 %d 份已存在，共 %d 个链接，%d 个未取到清单）==="
                             % (total_ok, total_all, total_skip, len(tasks), failed_cases))
                if total_all == 0:
                    # ⚠️ 一份都没拿到时必须报错，绝不能提示「下载完成」——
                    #    旧版正是在这里骗了用户：链接全过期 → 弹「下载完成」→ 桌面空空。
                    #    并且把**真实原因**写进弹窗：光说「详见日志」等于让用户自己猜。
                    self._ui(lambda n=failed_cases, le=last_error, ld=log_dir():
                             messagebox.showerror(
                        "未能下载任何文书",
                        "%d 个链接都没有取到文书清单，未下载任何文件。\n\n"
                        "失败原因：\n%s\n\n"
                        "· 若提示「链接过期 / 校验失败」→ 请重新获取送达短信后再试\n"
                        "· 若是网络类错误 → 程序已自动重试 3 次仍失败，请检查网络\n\n"
                        "完整日志已保存到：\n%s\n\n"
                        "（日志已自动去除链接中的访问凭证；如仍需发送，"
                        "建议先看一眼内容）" % (n, le or "（未捕获到具体原因，见日志）", ld)))
                elif failed_cases or total_ok + total_skip < total_all:
                    self._ui(lambda ok=total_ok, al=total_all, fc=failed_cases,
                                    sk=total_skip, le=last_error: messagebox.showwarning(
                                 "部分完成",
                                 "成功 %d / %d 份文书%s；另有 %d 个链接未能获取清单。\n\n"
                                 "失败原因：%s\n\n详见日志。"
                                 % (ok, al,
                                    ("，%d 份已存在被跳过" % sk) if sk else "",
                                    fc, le or "见日志")))
                elif total_ok == 0 and total_skip > 0:
                    # ⚠️ 全是「已存在跳过」时不能说「已下载」—— 一份都没下。
                    #    这是 v2.6「假成功提示」的同类问题，弹错会让用户以为下过了。
                    self._ui(lambda n=len(tasks), sk=total_skip: messagebox.showinfo(
                        "无需下载", "全部 %d 份文书均已存在，本次未重复下载。\n\n"
                        "%d 个案件文件夹，内容完好。" % (sk, n)))
                else:
                    extra = ("，%d 份已存在被跳过" % total_skip) if total_skip else ""
                    self._ui(lambda n=len(tasks), ok=total_ok, sk=total_skip:
                             messagebox.showinfo(
                        "下载完成", "全部 %d 个案件、%d 份文书已下载%s。" % (n, ok, extra)))
                # 自动打开保存位置：一个案件文件夹都没建起来时（链接全失效），
                # 也要按设置打开根目录，否则用户看到的是「点了下载毫无反应」。
                if self.auto_open_mode == AUTO_OPEN_ROOT:
                    self._ui(self.open_base_folder)
                elif self.auto_open_mode == AUTO_OPEN_EACH and not opened_any_case:
                    self._ui(self.open_base_folder)
        except Exception as e:  # noqa: BLE001
            # 完整堆栈落盘：worker 出意外时，界面只写一行，但文件里要留全证据
            try:
                write_log_file("[TRACEBACK] " + traceback.format_exc().replace("\n", "\n    "))
            except Exception:  # noqa: BLE001
                pass
            self.log_msg("✗ 出错了：%s：%s" % (type(e).__name__, e))
            self._ui(lambda msg="%s：%s" % (type(e).__name__, e): messagebox.showerror(
                "错误", msg))
        finally:
            # ⚠️ 收尾动作全部走 _ui()：窗口可能已被用户关掉（_force_close 已 destroy），
            #    此时直接调 self.root.after 会抛 TclError，把 worker 线程弄崩。
            with self._cancel_lock:
                self.running = False
                # stop_event 是全局唯一的取消标志。这里的 clear() 与按钮复位之间
                # 隔着一次主线程队列投递（按钮复位要等队列被泵执行），期间用户可能
                # 看着「正在取消…」又点了按钮 → on_start 发现 running 已是 False
                # → 启动新下载。若这之后再有人 clear 一次，新下载的取消标志就被清掉了。
                # 所以先取锁、并把「已收尾」状态与清标志绑在同一个临界区里。
                self.stop_event.clear()
            self._ui(lambda: self.btn_start.configure_state(
                state="normal", text="开始下载", command=self.on_start))


def main():
    setup_app_id()
    # 启动时清掉过期日志：日志里可能留着历史案件信息，不能无限期堆着
    prune_logs(30)
    # 每次运行在日志里留下抬头，出问题时一眼能看出是哪次、什么版本、什么环境
    write_log_file("")
    write_log_file("=" * 56)
    write_log_file("启动 法院文书下载器 v%s | Python %s | frozen=%s | 日志目录 %s"
                   % (VERSION, sys.version.split()[0], bool(getattr(sys, "frozen", False)),
                      log_dir()))
    # 内置 CA 的加载结果写进日志：这是「证书类故障」的第一手线索。
    # 用户报 SSL 错误时，让日志直接说明内置证书到底有没有加载上，
    # 不用再去猜是打包漏了文件、还是环境真的缺根证书。
    try:
        _ctx, _info = ssl_trust.build_ssl_context()
        write_log_file("内置CA 目录=%s | 文件=%s | 已加载=%s | 失败=%s | 信任库 %s→%s 张"
                       % (os.path.join(ssl_trust.base_dir(), ssl_trust.CA_SUBDIR),
                          _info["files"], _info["loaded"],
                          _info["failed"] or "无", _info.get("total"), _info.get("total_after")))
    except Exception as e:  # noqa: BLE001
        write_log_file("内置CA 加载异常：%s" % _safe_text(e))
    # 更新源（只写域名）：备用源为空 = _endpoints.py 没打进包。
    # 用户反馈「检查更新慢/失败」时，这条能一眼区分是网络问题还是打包漏了配置。
    try:
        _site = getattr(autoupdate, "_site_base", lambda: "")()
        write_log_file("更新源：GitHub 原站 / 备用源 %s" % (_site or "（未配置）"))
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
