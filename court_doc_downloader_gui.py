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
    - 下载中按钮变为“⏸ 取消下载”，点击可中途取消未完成的任务
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
import re
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

import urllib.error
import urllib.request

import autoupdate

API_URL = "https://zxfw.court.gov.cn/yzw/yzw-zxfw-sdfw/api/v1/sdfw/getWsListBySdbhNew"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://zxfw.court.gov.cn/zxfw/"
MAX_RETRY = 3
RETRY_BACKOFF = 2.0
VERSION = "1.8"
# 并发下载线程数：过小无提速、过大可能触发法院平台限流；4 是实测稳妥值
MAX_WORKERS = 4
# 自动更新：GitHub 上最新 Release 信息（私有仓库需设为公开才能免密访问）
GITHUB_API_LATEST = "https://api.github.com/repos/jianRY/dianzisongda-xiazaiqi/releases/latest"

# ---------------- 应用图标 ----------------
# 图标资源相对路径（打包进 exe 的 _MEIPASS 内同样位于 assets/ 下）
APP_ICON_REL = os.path.join("assets", "app.ico")
# Windows 任务栏身份标识：不设置的话任务栏会显示 python 默认图标
APP_ID = "jianRY.CourtDocDownloader"


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
# 标准案号，如 (2025)苏0505民初7780号 / （2025）苏05民终1234号
# 结构：[年度] + 法院代字(汉字+可选数字) + 案件类型(民/刑/行/执/商) + 程序(初/终/再/申/保/特/监/破…) + 序号 + 号
_CASE_RE = re.compile(
    r"[\(（](\d{4})[\)）]\s*[一-龥]{1,6}\d{0,6}\s*"
    r"(?:民|刑|行|执|商|赔|认)[初终再申保特监破执异复撤销核催督催告]?\s*\d+\s*号"
)


def _extract_one(url, text, pos, case_spans=None):
    """从单个 url 提取参数。

    case_spans: 预先算好的 [(start, end, caseno), ...]，按案号在文本中的位置排序。
    优先取「紧跟在该链接之前」的最近案号；没有则回退到全局第一个案号。
    这样批量粘贴多案件时才不会把 B 案的案号安到 A 案头上。
    """
    q = re.search(r"[?&]qdbh=([^&\s]+)", url)
    s1 = re.search(r"[?&]sdbh=([^&\s]+)", url)
    s2 = re.search(r"[?&]sdsin=([^&\s]+)", url)
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
    url_re = re.compile(r"https?://[^\s\"'<>）) ]+")
    for m in url_re.finditer(text):
        url = m.group(0).rstrip(".,;:)%）) ")
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


def fetch_doc_list(params, ctx):
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
        raise RuntimeError("接口返回非成功状态：%s" % obj.get("msg"))
    docs = obj.get("data") or []
    if not docs:
        raise RuntimeError("接口返回文书清单为空（链接可能已失效或参数有误）")
    return docs


def download_file(url, path, ctx, cancel_check=None):
    """下载单个文书文件。

    Args:
        cancel_check: 可选的零参数回调，返回 True 表示已取消 → 抛 CancelledError 提前退出。
    """
    last_err = None

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
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                # 记录服务端声明的长度，下载完比对，防止网络中断留下「文件头正常但内容被截断」的坏 PDF
                try:
                    expect = int(resp.headers.get("Content-Length", 0) or 0)
                except (TypeError, ValueError):
                    expect = 0
                written = 0
                with open(path, "wb") as f:
                    while True:
                        if cancel_check and cancel_check():
                            raise _Cancelled()
                        buf = resp.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        written += len(buf)
            if os.path.getsize(path) == 0:
                raise IOError("下载到 0 字节")
            if expect and written != expect:
                raise IOError("下载不完整（%d / %d 字节），将重试" % (written, expect))
            # 内容校验：
            #  · 文书不一定是 PDF（法院也可能发 jpg / docx），所以只在 .pdf 上强制 %PDF- 魔数，
            #    否则所有非 PDF 文书都会被误判失败并重试 3 次；
            #  · 但对任何类型都拦一下「错误页」——OSS 出错会返回 XML / HTML，文件头能认出来。
            with open(path, "rb") as f:
                head = f.read(32)
            low = head.lstrip(b"\xef\xbb\xbf \r\n\t").lower()
            if low.startswith((b"<?xml", b"<html", b"<!doctype", b"<error")):
                os.remove(path)
                raise IOError("服务端返回的是错误页而非文件（链接可能已过期）")
            if path.lower().endswith(".pdf") and not head.startswith(b"%PDF-"):
                os.remove(path)
                raise IOError("PDF 文件头异常，疑似下载失败")
            return True
        except _Cancelled:
            # 取消：删半成品文件后向上抛
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
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


def safe_ext(wjgs, url):
    """返回带点的扩展名。

    c_wjgs 可能是 'pdf' / '.PDF' / 'application/pdf' 等形态，统一清洗成小写扩展名；
    清洗不出合法扩展名时再从 URL 猜，最后兜底 .pdf。
    旧实现直接拼 '.'+c_wjgs，遇到 'application/pdf' 会得到非法文件名。
    """
    ext = ""
    if wjgs:
        cand = re.sub(r"[^A-Za-z0-9]", "", str(wjgs).strip().lstrip("."))
        if 1 <= len(cand) <= 5:
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
            for name in os.listdir(base):
                if name != prefix and not name.startswith(prefix + "_"):
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


def _jpg_output_exists(pdf_path, case_dir, mode):
    """该 PDF 对应的 JPG 是否已生成过（复用案件文件夹时用来决定要不要补转）。

    命名规则见 convert_pdf_to_jpg：单页 stem.jpg，多页 stem_1.jpg、stem_2.jpg…
    """
    stem = sanitize_filename(os.path.splitext(os.path.basename(pdf_path))[0])
    img_dir = (os.path.join(case_dir, "图片", stem) if mode == JPG_MODE_PERPDF
               else os.path.join(case_dir, "图片"))
    pat = re.compile(re.escape(stem) + r"(?:_\d+)?\.jpg$", re.IGNORECASE)
    try:
        return any(pat.match(n) for n in os.listdir(img_dir))
    except OSError:
        return False


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
                try:
                    pix.save(out_path, "jpg", jpeg_quality=95)
                except TypeError:
                    pix.save(out_path, "jpg")
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


def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "法院文书下载器", "config.json")


def load_config():
    cfg = {"out_dir": "", "auto_open_mode": 0, "convert_jpg": False, "jpg_mode": 0,
           "auto_update": True, "skip_existing": True}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("out_dir"), str):
            cfg["out_dir"] = data["out_dir"]
        mode = data.get("auto_open_mode", None)
        if mode in (0, 1, 2):
            cfg["auto_open_mode"] = mode
        elif data.get("auto_open"):  # 兼容旧版布尔配置
            cfg["auto_open_mode"] = 1
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


def save_config(out_dir, auto_open_mode, convert_jpg, jpg_mode, auto_update=None,
                skip_existing=True):
    """保存配置。

    auto_update 传 None = 「沿用配置文件里已有的值」，这是必须有别于 True 的语义：
    该字段由更新模块（autoupdate._save_auto_update）在用户选「以后不再提醒」时写 false，
    本函数若以默认 True 落盘，用户随后只要改动任意一个选项就会被悄悄重置回 true
    ——「以后不再提醒」形同虚设。
    """
    try:
        p = config_path()
        if auto_update is None:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    auto_update = bool(json.load(f).get("auto_update", True))
            except Exception:
                auto_update = True
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({
                "out_dir": out_dir,
                "auto_open_mode": int(auto_open_mode),
                "convert_jpg": bool(convert_jpg),
                "jpg_mode": int(jpg_mode),
                "auto_update": bool(auto_update),
                "skip_existing": bool(skip_existing),
            }, f, ensure_ascii=False, indent=2)
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
        self.auto_open_mode = cfg.get("auto_open_mode", 0)
        self.convert_jpg = bool(cfg.get("convert_jpg", False))
        self.jpg_mode = cfg.get("jpg_mode", 0)
        self.skip_existing = bool(cfg.get("skip_existing", True))
        self.last_case_dir = None
        self.running = False
        self.stop_event = threading.Event()  # 取消下载信号
        self._cancel_lock = threading.Lock()  # 保护 last_case_dir / 计数等共享状态

        root.title("法院文书下载器 v" + VERSION)
        root.geometry("640x690")
        root.resizable(True, True)
        apply_window_icon(root)
        # 窗口关闭保护：下载中先确认并置取消信号，避免半途强杀留下半成品文件
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 顶部菜单栏
        self._build_menubar()

        # ① 粘贴区
        frm1 = ttk.Frame(root)
        frm1.pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(frm1, text="① 粘贴法院送达短信或链接（支持批量：多个链接自动依次下载）", font=("Microsoft YaHei", 10)).pack(side="left")
        ttk.Button(frm1, text="🗑 清空", command=self.clear_text).pack(side="right")
        ttk.Button(frm1, text="📋 粘贴", command=self.paste_text).pack(side="right", padx=(0, 4))
        self.text_in = scrolledtext.ScrolledText(
            root, height=6, wrap="word", font=("Microsoft YaHei", 10)
        )
        self.text_in.pack(fill="x", padx=12)
        self._init_placeholder()
        self._build_ctx_menu()

        # ② 保存路径
        frm = ttk.Frame(root)
        frm.pack(fill="x", padx=12, pady=(8, 2))
        ttk.Label(frm, text="② 保存路径：", font=("Microsoft YaHei", 10)).pack(side="left")
        self.path_var = tk.StringVar(value=self.out_dir)
        self.path_var.trace_add("write", self._on_path_var_changed)
        self.entry_path = ttk.Entry(frm, textvariable=self.path_var)
        self.entry_path.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(frm, text="选择路径…", command=self.choose_path).pack(side="left")
        ttk.Button(frm, text="恢复默认", command=self.reset_path).pack(side="left", padx=(4, 0))

        # ③ 选项：下载完成后打开文件夹
        opt_box = ttk.LabelFrame(root, text="下载完成后打开文件夹")
        opt_box.pack(fill="x", padx=12, pady=(2, 4))
        self.auto_open_var = tk.IntVar(value=self.auto_open_mode)
        self.auto_open_var.trace_add("write", self._on_auto_open_changed)
        ttk.Radiobutton(opt_box, text="不打开", variable=self.auto_open_var,
                        value=AUTO_OPEN_OFF).pack(side="left", padx=8)
        ttk.Radiobutton(opt_box, text="打开保存根目录", variable=self.auto_open_var,
                        value=AUTO_OPEN_ROOT).pack(side="left", padx=8)
        ttk.Radiobutton(opt_box, text="打开每个案件文件夹", variable=self.auto_open_var,
                        value=AUTO_OPEN_EACH).pack(side="left", padx=8)

        # ④ 选项：下载后自动转换为 JPG
        jpg_box = ttk.LabelFrame(root, text="下载后转换为图片 (JPG)")
        jpg_box.pack(fill="x", padx=12, pady=(2, 4))
        self.convert_var = tk.BooleanVar(value=self.convert_jpg)
        self.convert_var.trace_add("write", self._on_convert_changed)
        ttk.Checkbutton(
            jpg_box, text="自动将 PDF 转换为 JPG 图片（长边 2000px，高质量）",
            variable=self.convert_var, command=self._sync_jpg_state,
        ).pack(anchor="w", padx=8, pady=(2, 2))
        self.jpg_mode_var = tk.IntVar(value=self.jpg_mode)
        self.jpg_mode_var.trace_add("write", self._on_jpg_mode_changed)
        self.jpg_sub = ttk.Frame(jpg_box)
        self.jpg_sub.pack(anchor="w", padx=(18, 0), pady=(0, 4))
        ttk.Radiobutton(self.jpg_sub, text="所有图片放在一个文件夹内",
                        variable=self.jpg_mode_var, value=JPG_MODE_SINGLE).pack(side="left", padx=8)
        ttk.Radiobutton(self.jpg_sub, text="按 PDF 文件名分别建文件夹",
                        variable=self.jpg_mode_var, value=JPG_MODE_PERPDF).pack(side="left", padx=8)
        self._sync_jpg_state()

        # ⑤ 选项：跳过已存在的文件
        skip_box = ttk.LabelFrame(root, text="重复下载处理")
        skip_box.pack(fill="x", padx=12, pady=(2, 4))
        self.skip_var = tk.BooleanVar(value=self.skip_existing)
        self.skip_var.trace_add("write", self._on_skip_existing_changed)
        ttk.Checkbutton(
            skip_box, text="跳过已存在的同名文书（同一案件重复下载时省时省流量）",
            variable=self.skip_var,
        ).pack(anchor="w", padx=8, pady=(2, 4))

        # 开始按钮
        self.btn_start = ttk.Button(
            root, text="⬇ 开始下载", command=self.on_start, style="Big.TButton"
        )
        self.btn_start.pack(padx=12, pady=(8, 4), fill="x")

        # 进度条
        frm_prog = ttk.Frame(root)
        frm_prog.pack(fill="x", padx=12, pady=(0, 4))
        self.progress = ttk.Progressbar(frm_prog, orient="horizontal", mode="determinate", length=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.progress_label = ttk.Label(frm_prog, text="进度：0 / 0 份", font=("Microsoft YaHei", 9), width=14)
        self.progress_label.pack(side="left")

        # 日志
        ttk.Label(root, text="下载日志：", font=("Microsoft YaHei", 10)).pack(
            anchor="w", padx=12, pady=(4, 2)
        )
        self.log = scrolledtext.ScrolledText(
            root, height=12, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # 底部按钮
        frm2 = ttk.Frame(root)
        frm2.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(frm2, text="检查更新", command=self.check_update_manual).pack(side="left")
        ttk.Button(frm2, text="打开保存文件夹", command=self.open_folder).pack(side="right")
        ttk.Button(frm2, text="清空日志", command=self.clear_log).pack(side="right", padx=6)

        # 让“开始下载”按钮醒目
        style = ttk.Style()
        try:
            style.configure("Big.TButton", font=("Microsoft YaHei", 11, "bold"), padding=6)
        except Exception:
            pass

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

    def _settle_after_update(self):
        """更新后首次启动：接管程序文件名 + 清理旧版文件；失败不影响使用。"""
        try:
            autoupdate.settle_after_update(log_fn=self.log_msg)
        except Exception:
            pass

    # ---- 线程安全的日志（自动加 [HH:MM:SS] 时间戳）----
    def log_msg(self, msg):
        # 窗口可能已被销毁（用户关窗 / 更新重启），此时 after() 会抛 TclError；
        # 下载线程仍在收尾，不能让它带着异常退出。
        try:
            self.root.after(0, self._log_msg, msg)
        except Exception:
            pass

    def _log_msg(self, msg):
        ts = datetime.now().strftime("[%H:%M:%S] ")
        self.log.configure(state="normal")
        self.log.insert("end", ts + msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

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
        if val:
            self.out_dir = val
            self._save_cfg()

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
        # 未勾选“转 JPG”时，禁用目录模式单选
        state = "normal" if self.convert_var.get() else "disabled"
        for child in self.jpg_sub.winfo_children():
            child.configure(state=state)

    # ---- 进度条（线程安全）----
    def reset_bar(self):
        self.root.after(0, self._reset_bar)

    def _reset_bar(self):
        self.progress["maximum"] = 1
        self.progress["value"] = 0
        self.progress_label.configure(text="进度：0 / 0 份 (0%)")

    def set_bar(self, total, done):
        self.root.after(0, self._set_bar, total, done)

    def _set_bar(self, total, done):
        if total > 0:
            self.progress["maximum"] = total
        self.progress["value"] = done
        pct = int(done * 100 / total) if total > 0 else 0
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
            "例：【苏州市虎丘区人民法院】…点击链接查阅：https://zxfw.court.gov.cn/...?qdbh=...&sdbh=...&sdsin=...\n"
            "可一次粘贴多个链接（每行一个，或混在多条短信里均可）。"
        )
        self.is_placeholder = True
        self.text_in.insert("1.0", self.placeholder_text)
        self.text_in.configure(fg="#9a9a9a")
        self.text_in.bind("<FocusIn>", self._on_focus_in)
        self.text_in.bind("<FocusOut>", self._on_focus_out)
        self.text_in.bind("<<Paste>>", self._on_native_paste)

    def _clear_placeholder(self):
        if self.is_placeholder:
            self.text_in.delete("1.0", "end")
            self.text_in.configure(fg="black")
            self.is_placeholder = False

    def _restore_placeholder(self):
        if not self.is_placeholder and self.text_in.get("1.0", "end").strip() == "":
            self.text_in.delete("1.0", "end")
            self.text_in.insert("1.0", self.placeholder_text)
            self.text_in.configure(fg="#9a9a9a")
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
        self.text_in.configure(fg="#9a9a9a")
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

    # ---- 顶部菜单栏 ----
    def _build_menubar(self):
        menubar = tk.Menu(self.root)
        # 更新菜单
        m_update = tk.Menu(menubar, tearoff=0)
        m_update.add_command(label="检查更新", command=self.check_update_manual)
        menubar.add_cascade(label="更新", menu=m_update)
        # 帮助菜单
        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="使用说明", command=self.show_help)
        m_help.add_separator()
        m_help.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=m_help)
        self.root.config(menu=menubar)

    def show_help(self):
        """使用说明窗口。"""
        win = tk.Toplevel(self.root)
        win.title("使用说明 · 法院文书下载器 v" + VERSION)
        win.geometry("660x560")
        win.resizable(True, True)
        apply_window_icon(win)
        try:
            win.transient(self.root)
        except Exception:
            pass
        txt = scrolledtext.ScrolledText(win, wrap="word", font=("Microsoft YaHei", 10))
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        content = (
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
            "4. 开始：点「⬇ 开始下载」，进度条实时显示「已下载 / 总份数 (百分比)」。\n"
            "   · 同一案件的多份文书会用 %d 个线程并发下载，速度更快。\n"
            "   · 下载过程中按钮变为「⏸ 取消下载」，点击可中途取消未完成任务。\n"
            "   · 下载中直接关闭窗口会先询问，未完成文件会被清理。\n"
            "   · 每个案件会自动建子文件夹：法院名_案号_<启动时间戳>，\n"
            "     防止同名法院不同链接的文件被合并。\n\n"
            "————————— 自动更新 —————————\n"
            "· 程序启动时会自动检查更新，发现新版会弹窗显示本次更新内容。\n"
            "· 弹窗三选：✅ 立即更新 / ⏭ 本次忽略 / 🚫 以后不再提醒。\n"
            "  选「以后不再提醒」后启动不再自动检查（菜单里仍可手动检查）。\n"
            "· 确认更新后显示下载进度与速度，可随时取消。\n"
            "· 下载完成后新版本直接放进本程序所在文件夹，自动重启，\n"
            "  并删掉旧版本文件；程序文件名保持不变，桌面快捷方式继续可用。\n"
            "  请把程序放在有写入权限的位置（如桌面），放在只读目录里无法自动更新。\n"
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
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 10))

    def show_about(self):
        messagebox.showinfo(
            "关于",
            "法院文书下载器  v%s\n\n"
            "从全国法院统一送达平台自动下载电子送达文书。\n"
            "数据来源：zxfw.court.gov.cn\n\n"
            "仅供本人依法处理诉讼事务使用。" % VERSION,
        )

    # ---- 自动检查更新（通用模块 autoupdate 承担 UI 与下载） ----
    def check_update_manual(self):
        """菜单/按钮手动检查：不受「以后不再提醒」开关影响。"""
        autoupdate.run_update_check(
            self.root,
            app_name="法院文书下载器",
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
            app_name="法院文书下载器",
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
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            test_file = os.path.join(self.out_dir, ".wb_write_test")
            with open(test_file, "w") as f:
                f.write("0")
            os.remove(test_file)
        except OSError as e:
            messagebox.showerror(
                "保存路径不可用",
                "无法写入所选路径：\n%s\n\n请改选一个有写入权限的文件夹。" % e,
            )
            return
        tasks = extract_tasks(text)
        if not tasks:
            messagebox.showwarning("无法识别", "未能从文本中提取到送达链接（需含 qdbh/sdbh/sdsin）。")
            return
        # 重置取消标志
        self.stop_event.clear()
        self.running = True
        self.btn_start.configure(state="normal", text="⏸ 取消下载", command=self.on_cancel)
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
            self.btn_start.configure(state="disabled", text="正在取消…")

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
            self._closing = True
            self.root.after(1500, self._force_close)
            return
        self.root.destroy()

    def _force_close(self):
        self.root.destroy()

    def worker(self, tasks):
        ctx = ssl.create_default_context()
        total_ok = 0
        total_all = 0
        done_count = 0
        total_docs = 0
        cancelled = False
        self.reset_bar()
        try:
            for idx, params in enumerate(tasks, 1):
                # 进入下一个案件前先看取消
                if self.stop_event.is_set():
                    self.log_msg("⏹ 已取消，跳过剩余 %d 个案件。" % (len(tasks) - idx + 1))
                    cancelled = True
                    break
                self.log_msg("")
                self.log_msg("==== 案件 %d / %d ====" % (idx, len(tasks)))
                self.log_msg("链接：%s" % params.get("url", ""))
                try:
                    docs = fetch_doc_list(params, ctx)
                except Exception as e:  # noqa: BLE001
                    self.log_msg("✗ 获取清单失败：%s" % e)
                    continue
                self.log_msg("✓ 找到 %d 份文书" % len(docs))
                total_docs += len(docs)
                self.set_bar(total_docs, done_count)

                court = docs[0].get("c_fymc", "未知法院")
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
                    if self.skip_existing and os.path.exists(full_) and os.path.getsize(full_) > 0:
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
                    self.log_msg("[%d/%d] 下载：%s" % (i_, len(docs), raw_))
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
                            success += 1  # 已存在视为成功，不计入失败
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
                total_all += len(docs)
                self.log_msg("--- 本案完成：%d / %d 份 ---" % (success, len(docs)))

                # 模式：打开每个案件文件夹
                if self.auto_open_mode == AUTO_OPEN_EACH and not cancelled:
                    _open_in_explorer(case_dir, self.log_msg)

                if cancelled:
                    break

            self.set_bar(total_docs, done_count)
            if cancelled:
                self.log_msg("")
                self.log_msg("=== 已取消：成功 %d / %d 份 ===" % (total_ok, max(total_all, done_count)))
                self.root.after(0, lambda: messagebox.showinfo(
                    "已取消", "下载已取消。成功 %d / %d 份，详见日志。" % (total_ok, total_all)))
            else:
                self.log_msg("")
                self.log_msg("=== 全部完成：成功 %d / %d 份（%d 个案件）===" % (total_ok, total_all, len(tasks)))
                if total_ok < total_all:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "部分失败", "成功 %d / %d 份，详见日志。" % (total_ok, total_all)))
                else:
                    self.root.after(0, lambda: messagebox.showinfo(
                        "下载完成", "全部 %d 个案件、%d 份文书已下载。" % (len(tasks), total_ok)))
                # 模式：打开保存根目录（批量时可见所有案件子文件夹）
                if self.auto_open_mode == AUTO_OPEN_ROOT and self.out_dir and os.path.isdir(self.out_dir):
                    self.root.after(50, self.open_base_folder)
        except Exception as e:  # noqa: BLE001
            self.log_msg("✗ 出错了：%s" % e)
            self.root.after(0, lambda: messagebox.showerror("错误", str(e)))
        finally:
            self.running = False
            self.stop_event.clear()
            self.root.after(0, lambda: self.btn_start.configure(
                state="normal", text="⬇ 开始下载", command=self.on_start))


def main():
    setup_app_id()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
