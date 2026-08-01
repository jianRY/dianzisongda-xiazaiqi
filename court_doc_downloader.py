#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法院电子送达文书下载工具（全国法院统一送达平台 zxfw.court.gov.cn）
=====================================================================

功能：
    把法院短信里的“电子送达”链接（或整段短信）丢进来，自动把该案件下
    的每一份文书（判决书 / 起诉状 / 传票 / 证据等）下载到本地文件夹。

原理（已实测）：
    1. 送达链接里带 3 个参数：qdbh / sdbh / sdsin
    2. 用这 3 个参数 POST 到文书列表接口，拿回文书清单（含 OSS 签名下载链接 wjlj）
    3. 接口【免登录】，但返回的 OSS 签名链接【有效期极短】
       → 所以必须“每次运行重新取链接，并立即下载”，绝不能缓存复用旧链接
    4. OSS 签名与 HTTP 方法绑定，只能 GET，HEAD 会 403，所以本工具只用 GET

特点：
    - 零第三方依赖，只用 Python 标准库（Windows / macOS / Linux 均自带 python3 即可）
    - 自动建文件夹：法院名_案号/
    - 生成 manifest.json（机器可读）和 送达清单.txt（人读）
    - 失败自动重试，文件名非法字符自动清洗

用法：
    方式一（命令行传链接或整段短信）：
        python court_doc_downloader.py "https://zxfw.court.gov.cn/...?qdbh=...&sdbh=...&sdsin=..."
        python court_doc_downloader.py "【苏州市虎丘区人民法院】安盛天平...查阅：https://zxfw.court.gov.cn/..."
    方式二（不带参数，交互式粘贴）：
        python court_doc_downloader.py
    可选参数：
        --out DIR      指定下载根目录（默认 ./法院文书）
        --no-verify    关闭 SSL 证书校验（某些内网环境需要，默认开启）

进阶：若未来接口改版导致本工具失效，可用 Playwright 无头浏览器打开链接，
      拦截 getWsListBySdbhNew 的响应或直接在页面点“下载”，参见文末说明。
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

# ---- 配置 ----
API_URL = "https://zxfw.court.gov.cn/yzw/yzw-zxfw-sdfw/api/v1/sdfw/getWsListBySdbhNew"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://zxfw.court.gov.cn/zxfw/"
MAX_RETRY = 3
RETRY_BACKOFF = 2.0  # 秒


def log(msg):
    print(msg, flush=True)


# ---------- 1. 解析输入 ----------
def parse_params(text):
    """从链接或整段短信里提取 qdbh / sdbh / sdsin，以及案号。"""
    qdbh = re.search(r"[?&]qdbh=([^&\s]+)", text)
    sdbh = re.search(r"[?&]sdbh=([^&\s]+)", text)
    sdsin = re.search(r"[?&]sdsin=([^&\s]+)", text)
    if not (qdbh and sdbh and sdsin):
        return None
    # 顺便尝试从短信正文里抠出标准案号，例如 (2025)苏0505民初7780号
    m = re.search(r"[\(（](\d{4})[\)）][一-龥A-Za-z]+?(?:民|刑|行|执|商)[初终再申保]?\w*?\d+号", text)
    caseno = m.group(0) if m else ""
    return {
        "qdbh": qdbh.group(1),
        "sdbh": sdbh.group(1),
        "sdsin": sdsin.group(1),
        "caseno": caseno,
    }


# ---------- 2. 调接口拿文书清单 ----------
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
        raise RuntimeError("接口返回文书清单为空（可能链接已失效或参数有误）")
    return docs


# ---------- 3. 下载单个文件（GET，带重试） ----------
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
            log("    ⚠ 第 %d 次下载失败：%s" % (attempt, e))
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError("下载失败（已重试 %d 次）：%s" % (MAX_RETRY, last_err))


# ---------- 工具：文件名清洗 ----------
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


# ---------- 主流程 ----------
def main():
    parser = argparse.ArgumentParser(description="法院电子送达文书下载工具")
    parser.add_argument("text", nargs="*", help="送达链接或整段短信（可省略进入交互模式）")
    parser.add_argument("--out", default="./法院文书", help="下载根目录（默认 ./法院文书）")
    parser.add_argument("--no-verify", action="store_true", help="关闭 SSL 证书校验")
    args = parser.parse_args()

    # 取输入文本
    if args.text:
        text = " ".join(args.text)
    else:
        try:
            text = input("请粘贴法院送达链接或整段短信，回车确认：\n").strip()
        except EOFError:
            text = ""
    if not text:
        log("未提供任何输入，退出。")
        sys.exit(1)

    params = parse_params(text)
    if not params:
        log("✗ 未能从输入中提取到 qdbh/sdbh/sdsin 参数，请确认链接完整。")
        sys.exit(1)
    log("✓ 已解析参数：qdbh=%s  sdbh=%s  sdsin=%s" % (params["qdbh"], params["sdbh"], params["sdsin"]))

    ctx = ssl.create_default_context()
    if args.no_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    # 取清单
    log("→ 正在向送达平台请求文书清单…")
    try:
        docs = fetch_doc_list(params, ctx)
    except Exception as e:  # noqa: BLE001
        log("✗ 获取文书清单失败：%s" % e)
        sys.exit(1)
    log("✓ 共找到 %d 份文书" % len(docs))

    # 决定文件夹名
    court = docs[0].get("c_fymc", "未知法院")
    caseno = params["caseno"] or ""
    folder_name = sanitize_filename((court + ("_" + caseno if caseno else "")).strip("_ "))
    out_dir = os.path.join(args.out, folder_name)
    os.makedirs(out_dir, exist_ok=True)
    log("→ 下载目录：%s" % os.path.abspath(out_dir))

    # 逐个下载
    manifest = {
        "platform": "zxfw.court.gov.cn",
        "court": court,
        "case_no": caseno,
        "params": params,
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "documents": [],
    }
    success = 0
    for i, d in enumerate(docs, 1):
        raw_name = d.get("c_wsmc") or ("文书%d" % i)
        ext = safe_ext(d.get("c_wjgs"), d.get("wjlj", ""))
        base = sanitize_filename(raw_name)
        filename = base + ext
        # 防重名
        full = os.path.join(out_dir, filename)
        dup = 1
        while os.path.exists(full):
            filename = "%s(%d)%s" % (base, dup, ext)
            full = os.path.join(out_dir, filename)
            dup += 1

        log("[%d/%d] 下载：%s" % (i, len(docs), raw_name))
        try:
            # 关键：用接口刚返回的签名链接立即 GET，不缓存
            download_file(d.get("wjlj"), full, ctx)
            size = os.path.getsize(full)
            log("    ✓ 已保存：%s  (%d 字节)" % (filename, size))
            manifest["documents"].append(
                {
                    "name": raw_name,
                    "file": filename,
                    "size": size,
                    "type": d.get("c_wjgs"),
                    "sent_at": d.get("dt_cjsj"),
                    "status": "ok",
                }
            )
            success += 1
        except Exception as e:  # noqa: BLE001
            log("    ✗ 失败：%s" % e)
            manifest["documents"].append(
                {"name": raw_name, "file": None, "status": "failed", "error": str(e)}
            )

    # 写清单
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    lines = [
        "法院电子送达文书下载清单",
        "平台：zxfw.court.gov.cn",
        "法院：%s" % court,
        ("案号：%s" % caseno) if caseno else "",
        "下载时间：%s" % manifest["downloaded_at"],
        "成功：%d / 共 %d 份" % (success, len(docs)),
        "----------------------------------------",
    ]
    for d in manifest["documents"]:
        if d["status"] == "ok":
            lines.append("[✓] %s  (%s, %d 字节)" % (d["name"], d.get("type"), d.get("size", 0)))
        else:
            lines.append("[✗] %s  (失败：%s)" % (d["name"], d.get("error")))
    with open(os.path.join(out_dir, "送达清单.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join([x for x in lines if x != ""]) + "\n")

    log("")
    log("=== 完成：成功 %d / %d 份 ===" % (success, len(docs)))
    log("文件夹：%s" % os.path.abspath(out_dir))
    if success < len(docs):
        sys.exit(2)


if __name__ == "__main__":
    main()
