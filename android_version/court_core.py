#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法院电子送达文书下载 · 核心逻辑（零第三方依赖）
=====================================================================

供「手机网页版后端」与「安卓 APK（Kivy）」共用的核心代码：
    - 从短信/链接中解析 qdbh / sdbh / sdsin
    - POST 法院接口取文书清单（免登录）
    - 用清单里返回的 OSS 签名链接立即 GET 下载（OSS 链接极短有效期，必须现取现下）

逻辑与原 Windows 版（court_doc_downloader_gui.py）完全一致，仅去掉了 PyMuPDF 转 JPG
（手机端直接预览/保存 PDF 即可，无需转图）。

用法（仅被其它模块 import，不直接运行）：
    from court_core import extract_tasks, fetch_doc_list, download_file, ...
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

# ---- 配置（与 Windows 版一致，已实测）----
API_URL = "https://zxfw.court.gov.cn/yzw/yzw-zxfw-sdfw/api/v1/sdfw/getWsListBySdbhNew"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://zxfw.court.gov.cn/zxfw/"
MAX_RETRY = 3
RETRY_BACKOFF = 2.0
VERSION = "1.0"  # 安卓版版本号

# 案号正则：例如 (2025)苏0505民初7780号
# 注意：法院地名与“民/刑…”之间常夹数字（如“苏0505”），故字符类包含 0-9
_CASE_RE = re.compile(
    r"[\(（](\d{4})[\)）][一-龥A-Za-z0-9]+?(?:民|刑|行|执|商)[初终再申保]?\w*?\d+号"
)


def make_ctx(no_verify=False):
    """构造 SSL 上下文；内网环境可 no_verify 关闭证书校验。"""
    ctx = ssl.create_default_context()
    if no_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------- 1. 解析输入 ----------------
def _extract_one(url, text, pos):
    """从单个 URL 提取 qdbh/sdbh/sdsin 与附近案号。"""
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


# ---------------- 2. 调接口拿文书清单 ----------------
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


# ---------------- 3. 下载单个文件（GET，带重试） ----------------
def download_file(url, path, ctx):
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": BROWSER_UA,
                    "Referer": REFERER,
                    "Accept": "*/*",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                chunk = 4096
                with open(path, "wb") as f:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
            # 校验：必须是真实文件且以 %PDF 开头（OSS 出错会返回 XML）
            if os.path.getsize(path) == 0:
                raise IOError("下载到 0 字节")
            with open(path, "rb") as f:
                head = f.read(5)
            if head != b"%PDF-":
                os.remove(path)
                raise IOError("文件头不是 %PDF，疑似下载失败（拿到的是错误页）")
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


# ---------------- 工具：文件名清洗 ----------------
def sanitize_filename(name):
    name = name.strip()
    # 去掉 Windows / 各类文件系统不允许的字符
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    # 去掉首尾空格与句点
    name = name.strip(". ").strip()
    if not name:
        name = "未命名文书"
    return name


def safe_ext(wjgs, url):
    ext = ""
    if wjgs:
        ext = "." + wjgs.strip().lstrip(".")
    if not ext:
        m = re.search(r"\.([a-zA-Z0-9]{2,4})(?:\?|$)", url)
        ext = "." + m.group(1) if m else ".pdf"
    return ext


# ---------------- 批量下载到一个目录，返回清单 ----------------
def download_case(params, case_dir, ctx):
    """下载单个案件全部文书到 case_dir，返回 {court, caseno, docs:[...]}。"""
    docs = fetch_doc_list(params, ctx)
    court = docs[0].get("c_fymc", "未知法院")
    caseno = params.get("caseno", "") or ""
    os.makedirs(case_dir, exist_ok=True)
    out = []
    for i, d in enumerate(docs, 1):
        raw = d.get("c_wsmc") or ("文书%d" % i)
        ext = safe_ext(d.get("c_wjgs"), d.get("wjlj", ""))
        base = sanitize_filename(raw)
        filename = base + ext
        full = os.path.join(case_dir, filename)
        dup = 1
        while os.path.exists(full):
            filename = "%s(%d)%s" % (base, dup, ext)
            full = os.path.join(case_dir, filename)
            dup += 1
        download_file(d.get("wjlj"), full, ctx)
        size = os.path.getsize(full)
        out.append({
            "name": raw,
            "file": filename,
            "size": size,
            "type": d.get("c_wjgs"),
            "sent_at": d.get("dt_cjsj"),
        })
    return {"court": court, "caseno": caseno, "docs": out}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        sample = " ".join(sys.argv[1:])
    else:
        sample = "【苏州市虎丘区人民法院】请查阅：https://zxfw.court.gov.cn/x?qdbh=A&sdbh=B&sdsin=C (2025)苏0505民初7780号"
    print("解析结果：", extract_tasks(sample))
