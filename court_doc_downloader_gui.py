#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法院文书下载器（图形界面版，可打包为单个 exe）
=====================================================================

功能：
    - 粘贴法院电子送达短信（或仅链接）到文本框
    - 点击“开始下载”，自动从文本中提取 zxfw.court.gov.cn 的送达链接
    - 调接口拿到该案件全部文书，逐一下载 PDF 到本地
    - 保存路径可“选择”，也可直接在框里手填；不选则默认：桌面\文书（不存在自动新建）
    - 上次选择的路径与“完成后打开文件夹”方式会被记住（写入 %APPDATA%）
    - “下载完成后打开文件夹”三档可选：不打开 / 打开保存根目录 / 打开每个案件文件夹
    - 可勾选“下载后自动转为 JPG 图片”（长边 2000px、高质量）；两种目录模式可选：
        ① 所有图片统一放进案件下的“图片”文件夹
        ② 按 PDF 文件名各自建子文件夹存放对应图片（多页自动加页码）
    - 带进度条，实时显示“已下载 / 总份数”
    - 每个案件自动建子文件夹：法院名_案号_启动时间戳\（时间戳为年月日时分秒纯数字，避免同名法院不同链接合并）

原理（与命令行版一致，已实测）：
    - 文书列表接口免登录：POST .../getWsListBySdbhNew，body 含 qdbh/sdbh/sdsin
    - OSS 签名链接有效期极短 → 每次运行现取现下
    - OSS 签名绑定 HTTP 方法 → 只用 GET

依赖：仅 Python 标准库（tkinter / urllib / ssl 等）。
打包：pyinstaller --onefile --windowed court_doc_downloader_gui.py
"""

import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import urllib.error
import urllib.request

API_URL = "https://zxfw.court.gov.cn/yzw/yzw-zxfw-sdfw/api/v1/sdfw/getWsListBySdbhNew"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://zxfw.court.gov.cn/zxfw/"
MAX_RETRY = 3
RETRY_BACKOFF = 2.0
VERSION = "1.2"
# 自动更新：GitHub 上最新 Release 信息（私有仓库需设为公开才能免密访问）
GITHUB_API_LATEST = "https://api.github.com/repos/jianRY/dianzisongda-xiazaiqi/releases/latest"


# ---------------- 核心下载逻辑（与命令行版一致） ----------------
_CASE_RE = re.compile(
    r"[\(（](\d{4})[\)）][一-龥A-Za-z]+?(?:民|刑|行|执|商)[初终再申保]?\w*?\d+号"
)


def _extract_one(url, text, pos):
    q = re.search(r"[?&]qdbh=([^&\s]+)", url)
    s1 = re.search(r"[?&]sdbh=([^&\s]+)", url)
    s2 = re.search(r"[?&]sdsin=([^&\s]+)", url)
    if not (q and s1 and s2):
        return None
    caseno = ""
    if pos is not None:
        seg = text[max(0, pos - 140): pos + 40]
        cm = _CASE_RE.search(seg)
        caseno = cm.group(0) if cm else ""
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
    url_re = re.compile(r"https?://[^\s\"'<>）) ]+")
    for m in url_re.finditer(text):
        url = m.group(0).rstrip(".,;:)%）) ")
        t = _extract_one(url, text, m.start())
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


def download_file(url, path, ctx):
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": BROWSER_UA, "Referer": REFERER, "Accept": "*/*"},
                method="GET",
            )
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                with open(path, "wb") as f:
                    while True:
                        buf = resp.read(4096)
                        if not buf:
                            break
                        f.write(buf)
            if os.path.getsize(path) == 0:
                raise IOError("下载到 0 字节")
            with open(path, "rb") as f:
                if f.read(5) != b"%PDF-":
                    os.remove(path)
                    raise IOError("文件头不是 %PDF，疑似下载失败")
            return True
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


def _parse_version(v):
    """把 'v1.2.3' / '1.2' 解析成可按元组比较的数字序列。"""
    v = v.strip().lstrip("vV")
    parts = re.split(r"[.\-]", v)
    out = []
    for p in parts:
        m = re.match(r"\d+", p)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def _version_greater(remote, local):
    try:
        return _parse_version(remote) > _parse_version(local)
    except Exception:
        return False


def download_generic(url, path, timeout=120):
    """通用下载（更新用），不做文件类型校验。自动继承系统代理环境变量。"""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        with open(path, "wb") as f:
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                f.write(buf)
    if os.path.getsize(path) == 0:
        raise IOError("下载到 0 字节")


def sanitize_filename(name):
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = name.strip(". ").strip()
    return name or "未命名文书"


def safe_ext(wjgs, url):
    if wjgs:
        return "." + wjgs.strip().lstrip(".")
    m = re.search(r"\.([a-zA-Z0-9]{2,4})(?:\?|$)", url)
    return "." + m.group(1) if m else ".pdf"


# ---------------- PDF -> JPG 转换（PyMuPDF / fitz） ----------------
# 长边固定 2000px，短边按比例自适应；高画质 JPEG（quality=95）。
# 单页：图片名 = PDF文件名.jpg；多页：PDF文件名_页码.jpg
def convert_pdf_to_jpg(pdf_path, case_dir, mode):
    """把单个 PDF 转为 JPG，返回生成的图片数量。

    mode=0：所有图片放在 case_dir/图片/ 下
    mode=1：按 PDF 文件名在 case_dir/图片/<文件名>/ 下分别建文件夹
    """
    import fitz  # PyMuPDF，懒加载（仅转换时需要）

    base_img_dir = os.path.join(case_dir, "图片")
    stem = sanitize_filename(os.path.splitext(os.path.basename(pdf_path))[0])
    img_dir = os.path.join(base_img_dir, stem) if mode == JPG_MODE_PERPDF else base_img_dir
    os.makedirs(img_dir, exist_ok=True)

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
    cfg = {"out_dir": "", "auto_open_mode": 0, "convert_jpg": False, "jpg_mode": 0}
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
    except Exception:
        pass
    if not cfg["out_dir"]:
        cfg["out_dir"] = default_out_dir()
    return cfg


def save_config(out_dir, auto_open_mode, convert_jpg, jpg_mode):
    try:
        p = config_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({
                "out_dir": out_dir,
                "auto_open_mode": int(auto_open_mode),
                "convert_jpg": bool(convert_jpg),
                "jpg_mode": int(jpg_mode),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- GUI ----------------
class App:
    def __init__(self, root):
        self.root = root
        cfg = load_config()
        self.out_dir = cfg["out_dir"] or default_out_dir()
        self.auto_open_mode = cfg.get("auto_open_mode", 0)
        self.convert_jpg = bool(cfg.get("convert_jpg", False))
        self.jpg_mode = cfg.get("jpg_mode", 0)
        self.last_case_dir = None
        self.running = False

        root.title("法院文书下载器 v" + VERSION)
        root.geometry("640x620")
        root.resizable(True, True)
        try:
            root.iconbitmap()  # 无图标则忽略
        except Exception:
            pass

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

        # 启动时自动检查更新（静默，无更新不打扰）
        try:
            threading.Thread(target=self.check_update, kwargs={"silent": True}, daemon=True).start()
        except Exception:
            pass

    # ---- 线程安全的日志 ----
    def log_msg(self, msg):
        self.root.after(0, self._log_msg, msg)

    def _log_msg(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
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

    def _on_path_var_changed(self, *_):
        val = self.path_var.get().strip()
        if val:
            self.out_dir = val
            save_config(self.out_dir, self.auto_open_mode, self.convert_jpg, self.jpg_mode)

    def _on_auto_open_changed(self, *_):
        self.auto_open_mode = int(self.auto_open_var.get())
        save_config(self.out_dir, self.auto_open_mode, self.convert_jpg, self.jpg_mode)

    def _on_convert_changed(self, *_):
        self.convert_jpg = bool(self.convert_var.get())
        save_config(self.out_dir, self.auto_open_mode, self.convert_jpg, self.jpg_mode)

    def _on_jpg_mode_changed(self, *_):
        self.jpg_mode = int(self.jpg_mode_var.get())
        save_config(self.out_dir, self.auto_open_mode, self.convert_jpg, self.jpg_mode)

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
        self.progress_label.configure(text="进度：0 / 0 份")

    def set_bar(self, total, done):
        self.root.after(0, self._set_bar, total, done)

    def _set_bar(self, total, done):
        if total > 0:
            self.progress["maximum"] = total
        self.progress["value"] = done
        self.progress_label.configure(text="进度：%d / %d 份" % (done, total))

    def open_folder(self):
        target = self.last_case_dir or self.out_dir
        if target and os.path.isdir(target):
            try:
                os.startfile(target)
            except Exception as e:  # noqa: BLE001
                self.log_msg("无法打开文件夹：%s" % e)
        else:
            messagebox.showinfo("提示", "还没有可打开的文件夹。")

    def open_base_folder(self):
        if self.out_dir and os.path.isdir(self.out_dir):
            try:
                os.startfile(self.out_dir)
            except Exception as e:  # noqa: BLE001
                self.log_msg("无法打开文件夹：%s" % e)

    def open_case_folder(self, case_dir):
        if case_dir and os.path.isdir(case_dir):
            try:
                os.startfile(case_dir)
            except Exception as e:  # noqa: BLE001
                self.log_msg("无法打开文件夹：%s" % e)

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
        try:
            win.transient(self.root)
        except Exception:
            pass
        txt = scrolledtext.ScrolledText(win, wrap="word", font=("Microsoft YaHei", 10))
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        content = (
            "【法院文书下载器 · 使用说明】\n\n"
            "本工具用于从「全国法院统一送达平台」发来的电子送达短信/链接中，\n"
            "自动下载对应的裁判文书（PDF）到本地文件夹。\n\n"
            "————————— 使用步骤 —————————\n"
            "1. 粘贴：把法院送达短信整段（或其中的链接）粘贴到顶部文本框。\n"
            "   · 支持批量：一次粘贴多个链接，会自动依次下载。\n"
            "   · 文本框右键有「粘贴 / 复制 / 剪切 / 全选」菜单。\n"
            "   · 「🗑 清空」按钮可一键清掉内容并恢复占位提示。\n"
            "2. 保存路径：默认「桌面\\文书」，可点「选择」改为任意文件夹；\n"
            "   程序会记住上次的选择。\n"
            "3. 选项：\n"
            "   · 下载完成后打开：不打开 / 打开根目录 / 打开每个案件文件夹。\n"
            "   · PDF 转 JPG：勾选后自动把 PDF 转成图片。\n"
            "     - 模式一：所有图片放到一个「图片」文件夹；\n"
            "     - 模式二：按 PDF 文件名各自建子文件夹存放。\n"
            "     - 图片长边 2000 像素、高质量、短边自适应；多页自动加页码。\n"
            "4. 开始：点「开始下载」，进度条实时显示「已下载 / 总份数」。\n"
            "   每个案件会自动建子文件夹：法院名_案号_<启动时间戳>，\n"
            "   防止同名法院不同链接的文件被合并。\n\n"
            "————————— 自动更新 —————————\n"
            "· 程序启动时会自动静默检查更新，有新版会弹窗提示。\n"
            "· 也可点菜单「更新 → 检查更新」手动检查。\n"
            "· 确认更新后会自动下载新程序、替换旧文件并重启。\n\n"
            "————————— 常见问题 —————————\n"
            "Q：提示下载失败 / 链接无效？\n"
            "A：电子送达链接有时效，请重新从法院短信复制最新链接再试。\n\n"
            "Q：下载到的文件只有几 KB？\n"
            "A：多为链接已过期或需重新获取，请换用最新短信链接。\n\n"
            "Q：转 JPG 后图片打不开？\n"
            "A：请确认已勾选「PDF 转 JPG」且磁盘有足够空间。\n\n"
            "————————— 备注 —————————\n"
            "本工具仅供本人依法处理诉讼事务使用，请遵守相关平台与法律规定。\n"
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

    # ---- 自动检查更新 ----
    def check_update(self, silent=False):
        """检查 GitHub 最新 Release。silent=True 用于启动静默检查（无更新不打扰）。"""
        try:
            req = urllib.request.Request(
                GITHUB_API_LATEST,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/vnd.github+json"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            dl = None
            for a in data.get("assets", []):
                if a.get("name", "").lower().endswith(".exe"):
                    dl = a.get("browser_download_url")
                    break
            notes = data.get("body", "") or ""
            if not dl:
                if not silent:
                    self.root.after(0, lambda: messagebox.showinfo("检查更新", "未找到可用的更新文件。"))
                return
            if _version_greater(tag, VERSION):
                self.root.after(0, lambda: self._prompt_update(tag, dl, notes))
            elif not silent:
                self.root.after(0, lambda: messagebox.showinfo("检查更新", "已是最新版本 v%s。" % VERSION))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                msg = "无法自动检查更新：该仓库 / Release 为私有或不可见，需将其设为公开后才能免密自动下载。可前往 GitHub 手动下载。"
            else:
                msg = "检查更新失败（HTTP %d）。" % e.code
            if not silent:
                self.root.after(0, lambda m=msg: messagebox.showwarning("检查更新", m))
        except Exception as e:  # noqa: BLE001
            if not silent:
                self.root.after(0, lambda: messagebox.showwarning("检查更新", "检查更新失败：%s" % e))

    def check_update_manual(self):
        self.check_update(silent=False)

    def _prompt_update(self, tag, dl_url, notes):
        info = "发现新版本 %s（当前 v%s）。\n\n是否立即下载并更新？" % (tag, VERSION)
        if notes.strip():
            info += "\n\n更新内容：\n" + notes.strip()[:600]
        if messagebox.askyesno("发现新版本", info):
            self._do_update(dl_url)

    def _do_update(self, dl_url):
        self.log_msg("开始下载更新…")
        tmp = os.path.join(tempfile.gettempdir(), "法院文书下载器_更新.exe")
        try:
            download_generic(dl_url, tmp, timeout=180)
        except Exception as e:  # noqa: BLE001
            self.log_msg("✗ 更新下载失败：%s" % e)
            self.root.after(0, lambda: messagebox.showerror("更新失败", "下载新版本失败：%s" % e))
            return
        if os.path.getsize(tmp) < 100000:
            self.log_msg("✗ 下载文件过小，疑似失败")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return
        current = os.path.abspath(sys.executable)
        self.log_msg("下载完成，准备重启并替换旧程序…")
        bat = os.path.join(tempfile.gettempdir(), "法院文书下载器_updater.bat")
        cur = current.replace("/", "\\")
        tmpn = tmp.replace("/", "\\")
        try:
            with open(bat, "w", encoding="gbk") as f:
                f.write("@echo off\n")
                f.write(":wait\n")
                f.write("timeout /t 1 /nobreak >nul\n")
                f.write('del /f /q "%s"\n' % cur)
                f.write('if exist "%s" goto wait\n' % cur)
                f.write('move /y "%s" "%s"\n' % (tmpn, cur))
                f.write('start "" "%s"\n' % cur)
                f.write('del /f /q "%~f0"\n')
        except Exception as e:  # noqa: BLE001
            self.log_msg("✗ 生成更新脚本失败：%s" % e)
            return
        try:
            subprocess.Popen(["cmd", "/c", bat], shell=False)
        except Exception as e:  # noqa: BLE001
            self.log_msg("✗ 启动更新失败：%s" % e)
            return
        self.root.destroy()

    def on_start(self):
        if self.running:
            return
        text = self.text_in.get("1.0", "end")
        # 再次同步路径（保险）：以输入框当前内容为准
        self.out_dir = self.path_var.get().strip() or self.out_dir
        tasks = extract_tasks(text)
        if not tasks:
            messagebox.showwarning("无法识别", "未能从文本中提取到送达链接（需含 qdbh/sdbh/sdsin）。")
            return
        self.running = True
        self.btn_start.configure(state="disabled", text="下载中…")
        self._log_msg("法院文书下载器 v%s" % VERSION)
        self._log_msg("✓ 识别到 %d 个送达链接，将依次下载。" % len(tasks))
        t = threading.Thread(target=self.worker, args=(tasks,), daemon=True)
        t.start()

    def worker(self, tasks):
        ctx = ssl.create_default_context()
        total_ok = 0
        total_all = 0
        done_count = 0
        total_docs = 0
        self.reset_bar()
        try:
            for idx, params in enumerate(tasks, 1):
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
                # 任务启动时的时间戳：年月日时分秒（纯数字），避免同名法院不同链接合并到一个文件夹
                ts = time.strftime("%Y%m%d%H%M%S")
                folder = sanitize_filename(
                    (court + ("_" + caseno if caseno else "") + "_" + ts).strip("_ ")
                )
                base = self.out_dir or default_out_dir()
                os.makedirs(base, exist_ok=True)
                case_dir = os.path.join(base, folder)
                os.makedirs(case_dir, exist_ok=True)
                self.last_case_dir = case_dir
                self.log_msg("→ 保存目录：%s" % case_dir)

                success = 0
                for i, d in enumerate(docs, 1):
                    raw = d.get("c_wsmc") or ("文书%d" % i)
                    ext = safe_ext(d.get("c_wjgs"), d.get("wjlj", ""))
                    base_name = sanitize_filename(raw)
                    filename = base_name + ext
                    full = os.path.join(case_dir, filename)
                    dup = 1
                    while os.path.exists(full):
                        filename = "%s(%d)%s" % (base_name, dup, ext)
                        full = os.path.join(case_dir, filename)
                        dup += 1
                    self.log_msg("[%d/%d] 下载：%s" % (i, len(docs), raw))
                    try:
                        download_file(d.get("wjlj"), full, ctx)
                        size = os.path.getsize(full)
                        self.log_msg("    ✓ 已保存：%s (%d 字节)" % (filename, size))
                        success += 1
                        if self.convert_jpg and filename.lower().endswith(".pdf"):
                            try:
                                cnt = convert_pdf_to_jpg(full, case_dir, self.jpg_mode)
                                self.log_msg("    ✓ 已转 JPG：%d 页" % cnt)
                            except Exception as e2:  # noqa: BLE001
                                self.log_msg("    ✗ 转 JPG 失败：%s" % e2)
                    except Exception as e:  # noqa: BLE001
                        self.log_msg("    ✗ 失败：%s" % e)
                    done_count += 1
                    self.set_bar(total_docs, done_count)

                total_ok += success
                total_all += len(docs)
                self.log_msg("--- 本案完成：%d / %d 份 ---" % (success, len(docs)))

                # 模式：打开每个案件文件夹
                if self.auto_open_mode == AUTO_OPEN_EACH:
                    self.open_case_folder(case_dir)

            self.set_bar(total_docs, done_count)
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
            self.root.after(0, lambda: self.btn_start.configure(state="normal", text="⬇ 开始下载"))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
