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
        python court_doc_downloader.py "【某市某区人民法院】某某保险...查阅：https://zxfw.court.gov.cn/..."
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
# 参数值的合法字符集：服务器给的是 Base64 类串（字母数字 + `-` `_` `=`），
# 但用户粘贴整段短信时结尾常跟中文标点（`&sdsin=XXX，请及时查阅。`）。
# 不把标点切掉就会把「，请及时查阅。」当成参数值发出去 → 接口校验失败。
# 所以这里显式定义「允许集」，用白名单而不是逐个排除（漏一个就又是一次线上事故）。
_P_VAL = r"[A-Za-z0-9\-_=+%*~]"      # 参数值允许的字符
_TAIL_STRIP = ".,;:!?)]}>\"'，。；：！？、）】》」』〉〞…—～·　 \t\r\n"


def _clean_val(m):
    """把匹配到的参数值右侧的标点/空白切掉。"""
    return m.group(1).rstrip(_TAIL_STRIP)


def parse_params(text):
    """从链接或整段短信里提取 qdbh / sdbh / sdsin，以及案号。"""
    qdbh = re.search(r"[?&]qdbh=(%s+)" % _P_VAL, text)
    sdbh = re.search(r"[?&]sdbh=(%s+)" % _P_VAL, text)
    sdsin = re.search(r"[?&]sdsin=(%s+)" % _P_VAL, text)
    if not (qdbh and sdbh and sdsin):
        return None
    # 顺便尝试从短信正文里抠出标准案号，例如 (2025)苏0000民初1234号
    # 结构：[年度] + 法院代字(汉字+可选数字) + 案件类型 + 程序 + 序号 + 号
    # ⚠️ 与 GUI 版保持一致：真实案号里汉字与「民」之间会夹法院代码数字
    #    （如「苏0000民初1234号」），各段都必须允许可选数字，否则永远匹配不到。
    m = re.search(
        r"[\(（](\d{4})[\)）]\s*[\u4e00-\u9fa5]{1,8}\d{0,6}\s*"
        r"(?:民|刑|行|执|商|赔|认)\d{0,4}"
        r"[初终再申保特监破执异复撤销核催督催告]{0,2}\s*\d{1,8}\s*号",
        text,
    )
    caseno = m.group(0) if m else ""
    return {
        "qdbh": _clean_val(qdbh),
        "sdbh": _clean_val(sdbh),
        "sdsin": _clean_val(sdsin),
        "caseno": caseno,
    }


# ---------- 2. 调接口拿文书清单 ----------
class LinkRejected(RuntimeError):
    """法院接口明确拒绝了这条链接（多半是链接已过期）—— 重试无意义。"""


def fetch_doc_list(params, ctx):
    """带重试地取文书清单：网络抖动重试 3 次；「链接过期」不重试，直接报原因。"""
    last = None
    for attempt in range(1, 4):
        try:
            return _fetch_doc_list_once(params, ctx)
        except LinkRejected:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 3:
                print("  ⚠ 取清单失败（第 %d 次）：%s —— 稍后重试…" % (attempt, e))
                time.sleep(1.2 * attempt)
    raise last


def _fetch_doc_list_once(params, ctx):
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
        # 与图形版一致：把服务端错误翻成人话，最常见的是链接过期导致「校验失败」
        if code == 401 or "校验失败" in str(msg):
            raise LinkRejected(
                "接口拒绝了该链接（%s）—— 链接多半已过期，请重新获取送达短信。" % msg)
        raise RuntimeError("接口返回非成功状态（code=%s）：%s" % (code, msg))
    docs = obj.get("data") or []
    if not docs:
        raise LinkRejected("接口返回文书清单为空（可能链接已失效或参数有误）")
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
                # 与 GUI 版一致：读 Content-Length，下完比对，提早发现半截包
                expect = resp.headers.get("Content-Length")
                expect = int(expect) if expect and expect.isdigit() else None
                chunk = 65536
                with open(path, "wb") as f:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
            # 校验：0 字节 / 完整性 / 拿到的是错误页（OSS 出错会返回 XML/HTML）
            got = os.path.getsize(path)
            if got == 0:
                raise IOError("下载到 0 字节")
            if expect is not None and got != expect:
                os.remove(path)
                raise IOError("下载不完整（收到 %d / 应为 %d 字节）" % (got, expect))
            with open(path, "rb") as f:
                head = f.read(32)
            low = head.lstrip(b"\xef\xbb\xbf \r\n\t").lower()
            if low.startswith((b"<?xml", b"<html", b"<!doctype", b"<error")):
                os.remove(path)
                raise IOError("服务端返回的是错误页而非文件（链接可能已过期）")
            # 文书不一定是 PDF，只在 .pdf 上强制魔数校验
            if path.lower().endswith(".pdf") and not head.startswith(b"%PDF-"):
                os.remove(path)
                raise IOError("PDF 文件头异常，疑似下载失败")
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
# Windows 保留设备名：以此为文件名（或带扩展名的同名）无法创建，需加前缀规避
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} \
    | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def sanitize_filename(name):
    name = str(name or "").strip()
    # 去掉 Windows / 各类文件系统不允许的字符
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    # 去掉首尾空格与句点
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
    """
    ext = ""
    if wjgs:
        raw = str(wjgs).strip().lower().lstrip(".")
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
            if 1 <= len(cand) <= 5:
                ext = "." + cand.lower()
    if not ext:
        m = re.search(r"\.([A-Za-z0-9]{2,5})(?:[?#]|$)", url or "")
        ext = ("." + m.group(1).lower()) if m else ".pdf"
    return ext

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
    # ⚠️ 键存在但值为 None 时 get 的默认值不生效 → 必须再 or 一次，
    #    否则下一行 court + "_" 会抛 TypeError。
    court = docs[0].get("c_fymc") or "未知法院"
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
