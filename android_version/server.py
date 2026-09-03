#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法院文书下载器 · 手机网页版后端（零第三方依赖，仅用 Python 标准库）
=====================================================================

职责：
    1. 托管手机自适应前端 index.html
    2. 代理法院接口（浏览器有跨域限制，必须由服务端 POST 取清单）
    3. 把每份 PDF 下载到本机，再按稳定 URL 分发，规避 OSS 签名链接极短有效期问题
    4. 自动打包 ZIP 方便手机一次性保存

运行（同一台电脑 / 局域网 / 服务器均可）：
    python server.py                      # 默认 127.0.0.1:8000
    python server.py --host 0.0.0.0 --port 8000   # 让手机通过局域网 IP 访问
    python server.py --no-verify          # 内网证书异常时关闭 SSL 校验

手机使用：
    - 电脑与手机连同一 WiFi，手机浏览器打开 http://<电脑局域网IP>:8000
    - 长期远程使用：把本服务放到你已有的 HTTPS 反向代理（frp / workbuddy.link）后，手机用 https 域名访问

接口：
    GET  /                         -> 前端页面
    POST /api/process  {"text": "...", "no_verify": false}
                                -> {"ok": true, "results":[...]} 或 {"ok": false, "error": "..."}
    GET  /files/<案件目录>/<文件名>   -> 预览/下载对应文件（PDF 内联预览，ZIP 附件下载）
"""

import argparse
import json
import mimetypes
import os
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import court_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.join(HERE, "downloads")          # 下载落盘目录
INDEX_PATH = os.path.join(HERE, "index.html")
os.makedirs(DOWNLOADS, exist_ok=True)


# ---------------- 业务处理 ----------------
def process(text, no_verify=False):
    """解析短信 -> 逐案取清单 -> 下载 PDF -> 打包 ZIP，返回结构化结果。"""
    tasks = core.extract_tasks(text)
    if not tasks:
        raise ValueError("未能从输入中识别到送达链接（需含 qdbh/sdbh/sdsin 参数）。")
    ctx = core.make_ctx(no_verify)
    ts = time.strftime("%Y%m%d%H%M%S")
    results = []
    for params in tasks:
        # 案件目录：法院名_案号_时间戳（防止同名法院不同链接合并）
        court = "未知法院"
        caseno = params.get("caseno", "") or ""
        # 先占位，拿到清单后补法院名
        case_dir = os.path.join(DOWNLOADS, "case_%s" % ts)
        info = core.download_case(params, case_dir, ctx)
        court = info["court"]
        folder = core.sanitize_filename(
            (court + ("_" + caseno if caseno else "") + "_" + ts).strip("_ ")
        )
        final_dir = os.path.join(DOWNLOADS, folder)
        if os.path.abspath(case_dir) != os.path.abspath(final_dir):
            os.makedirs(final_dir, exist_ok=True)
            for f in os.listdir(case_dir):
                src = os.path.join(case_dir, f)
                dst = os.path.join(final_dir, f)
                if os.path.isfile(src):
                    os.replace(src, dst)
            try:
                os.rmdir(case_dir)
            except OSError:
                pass

        # 打包 ZIP
        zip_name = "全部文书_%s.zip" % folder
        zip_path = os.path.join(final_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for d in info["docs"]:
                fp = os.path.join(final_dir, d["file"])
                if os.path.isfile(fp):
                    z.write(fp, d["file"])
        results.append({
            "court": court,
            "caseno": caseno,
            "dir": folder,
            "zip": zip_name,
            "zip_size": os.path.getsize(zip_path),
            "docs": info["docs"],
        })
    return results


# ---------------- HTTP 处理 ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CourtDocMobile/1.0"

    # 让日志安静一点
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, disk_path, as_attachment=False, disp_name=None, content_type_override=None):
        if not os.path.isfile(disk_path):
            self._send(404, json.dumps({"ok": False, "error": "文件不存在"}, ensure_ascii=False))
            return
        if content_type_override:
            mime = content_type_override
        else:
            mime, _ = mimetypes.guess_type(disk_path)
            mime = mime or "application/octet-stream"
        size = os.path.getsize(disk_path)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if not content_type_override:  # 静态资源（HTML）不需要下载头
            name = disp_name or os.path.basename(disk_path)
            enc = urllib.parse.quote(name)
            disp = "attachment" if as_attachment else "inline"
            self.send_header(
                "Content-Disposition",
                "%s; filename=\"%s\"; filename*=UTF-8''%s" % (disp, enc, enc),
            )
        self.end_headers()
        with open(disk_path, "rb") as f:
            while True:
                buf = f.read(65536)
                if not buf:
                    break
                self.wfile.write(buf)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path in ("/", "/index.html"):
            if os.path.isfile(INDEX_PATH):
                self._send_file(INDEX_PATH, content_type_override="text/html; charset=utf-8")
            else:
                self._send(500, "前端 index.html 缺失")
            return
        if path == "/health":
            self._send(200, json.dumps({"ok": True, "version": core.VERSION}, ensure_ascii=False))
            return
        if path.startswith("/files/"):
            # /files/<案件目录>/<文件名>
            rel = path[len("/files/"):]
            # 防穿越：规范化后必须仍在 DOWNLOADS 内
            target = os.path.normpath(os.path.join(DOWNLOADS, rel))
            if not target.startswith(os.path.normpath(DOWNLOADS) + os.sep) and target != os.path.normpath(DOWNLOADS):
                self._send(403, json.dumps({"ok": False, "error": "非法路径"}, ensure_ascii=False))
                return
            as_attachment = rel.lower().endswith(".zip")
            self._send_file(target, as_attachment=as_attachment)
            return
        self._send(404, json.dumps({"ok": False, "error": "未找到资源"}, ensure_ascii=False))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/process":
            self._send(404, json.dumps({"ok": False, "error": "未知接口"}, ensure_ascii=False))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            text = (payload.get("text") or "").strip()
            no_verify = bool(payload.get("no_verify", os.environ.get("COURT_NO_VERIFY") == "1"))
            if not text:
                self._send(400, json.dumps({"ok": False, "error": "请输入送达短信或链接"}, ensure_ascii=False))
                return
            results = process(text, no_verify)
            self._send(200, json.dumps(
                {"ok": True, "results": results, "version": core.VERSION}, ensure_ascii=False))
        except ValueError as e:
            self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"ok": False, "error": "处理失败：%s" % e}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="法院文书下载器 · 手机网页版后端")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（局域网用 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8000, help="监听端口")
    ap.add_argument("--no-verify", action="store_true", help="关闭 SSL 证书校验")
    args = ap.parse_args()

    # 把 --no-verify 透传给 process 的全局默认：借助环境变量
    if args.no_verify:
        os.environ["COURT_NO_VERIFY"] = "1"

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d" % (args.host, args.port)
    print("法院文书下载器（手机网页版）已启动：")
    print("  -> 本机访问： %s" % url)
    print("  -> 手机访问： http://<本机局域网IP>:%d  （手机与电脑需同一 WiFi）" % args.port)
    print("  -> 下载文件保存在： %s" % DOWNLOADS)
    print("  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
