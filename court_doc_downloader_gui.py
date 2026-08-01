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
VERSION = "1.0"


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

        # ① 粘贴区
        frm1 = ttk.Frame(root)
        frm1.pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(frm1, text="① 粘贴法院送达短信或链接（支持批量：多个链接自动依次下载）", font=("Microsoft YaHei", 10)).pack(side="left")
        ttk.Button(frm1, text="📋 粘贴", command=self.paste_text).pack(side="right")
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
        ttk.Button(frm2, text="打开保存文件夹", command=self.open_folder).pack(side="right")
        ttk.Button(frm2, text="清空日志", command=self.clear_log).pack(side="right", padx=6)

        # 让“开始下载”按钮醒目
        style = ttk.Style()
        try:
            style.configure("Big.TButton", font=("Microsoft YaHei", 11, "bold"), padding=6)
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
