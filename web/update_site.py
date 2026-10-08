#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法院文书下载器 · 自有服务器自动同步脚本（放在网站根目录运行）

作用
    从 GitHub Releases 拉取最新版本，把三样东西同步到本站：
        ① /updates/court.json   —— 更新元数据（客户端与官网都读它）
        ② /files/<资产名>.exe    —— 绿色版（国内直链）
        ③ /files/<资产名>_setup.exe —— 安装版

为什么需要它
    以前 exe 与元数据是**手工上传**的，结果出现过「GitHub 已发 v2.10.1、
    服务器还停在旧版 → 官网点下载拿到旧版」的事故（2026-10-08 发现）。
    把同步交给脚本 + 计划任务，发完 Release 服务器自动跟上，不会再漏。

特点
    * 只用 Python 3 标准库（宝塔自带 python3 直接能跑，不装任何包）
    * 多源容灾：GitHub 慢/挂了就自动切镜像
    * 原子写入 + 自动备份，元数据内容非法时拒绝覆盖（站点不会被搞坏）
    * 幂等：内容没变就跳过，可安全地频繁执行
    * 旧版 exe 自动清理，只保留最近 N 个版本
    * 退出码 0 = 成功（含无更新）；非 0 = 失败，便于计划任务报警

用法（宝塔面板 → 计划任务 → Shell 脚本，填一条命令）
    python3 /www/wwwroot/你的站点目录/update_site.py

    · 想每天自动跟一次：执行周期选「N 天」或「每小时」
    · 想立刻同步一次：点任务的「执行」按钮

命令行参数
    --check          只检查线上最新版本，不写任何文件（安全探测）
    --keep 2         files/ 里保留最近几个版本的 exe（默认 2）
    --proxy URL      HTTP 代理，如 http://127.0.0.1:7890（默认读环境变量）
    --token TOKEN    GitHub Token（可选，仅用于提高 API 速率限制）
    --dest DIR       网站根目录（默认 = 本脚本所在目录）

⚠️ 目录要求（本站约定）
    <网站根>/index.html            官网（由 update_web.sh 另行同步）
    <网站根>/updates/court.json    本脚本写入
    <网站根>/files/*.exe           本脚本写入

作者：法院文书下载器 build 流程配套
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

# ============================ 配置 ============================
OWNER_REPO = "jianRY/dianzisongda-xiazaiqi"
APP_KEY = "court"                       # → 元数据写 updates/court.json
APP_NAME = "法院文书下载器"
ASSET_RE = re.compile(r"^CourtDocDownloader_v(\d+(?:\.\d+)+)\.exe$")
SETUP_RE = re.compile(r"^CourtDocDownloader_v(\d+(?:\.\d+)+)_setup\.exe$")

META_SUBDIR = "updates"
FILES_SUBDIR = "files"

# exe 下载加速前缀（按顺序尝试）。
# ⚠️ 顺序刻意把「GitHub 直连」放**最后**：国内服务器直连 GitHub 经常中途断流，
#    实测会白等 3 分钟才拿到一个残包。镜像站反而快且稳（实测 30.6MB 约 20 秒下完）。
EXE_PREFIXES = ["https://ghfast.top/", "https://ghproxy.net/",
                "https://gh-proxy.com/", ""]

DEFAULT_KEEP_VERSIONS = 2
BACKUP_DIR_NAME = ".update_bak"
BACKUP_KEEP = 5
UA = "Mozilla/5.0 (compatible; CourtDocUpdater/1.0)"
# ============================================================

DEFAULT_DEST = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0


def opener(proxy=None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    op = urllib.request.build_opener(*handlers)
    op.addheaders = [("User-Agent", UA)]
    return op


def http_get(url, timeout=45, proxy=None, stream_to=None, expect_size=0):
    """取回 url 内容。给了 stream_to 就边下边写盘（大 exe 用，避免占内存）。"""
    op = opener(proxy)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with op.open(req, timeout=timeout) as r:
        if stream_to is None:
            return r.read()
        total = int(r.headers.get("Content-Length") or 0)
        got, last = 0, -25
        with open(stream_to, "wb") as f:
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                pct = int(got * 100 / (total or expect_size or got))
                if pct >= last + 25 or pct >= 100:
                    last = pct
                    log("      %d%%（%s / %s）" % (pct, human(got), human(total or expect_size)))
        return got


def http_get_json(url, timeout=45, proxy=None, token=None):
    op = opener(proxy)
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = "Bearer " + token
    with op.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_latest(proxy=None, token=None):
    """查最新 Release → (version, {资产名: {url,size}}, published_at, notes)"""
    d = http_get_json("https://api.github.com/repos/%s/releases/latest" % OWNER_REPO,
                      proxy=proxy, token=token)
    ver = (d.get("tag_name") or "").lstrip("vV")
    assets = {}
    for a in d.get("assets") or []:
        assets[a["name"]] = {"url": a["browser_download_url"], "size": a["size"]}
    return ver, assets, d.get("published_at", ""), (d.get("body") or "").strip()


def pick(assets, pattern):
    """按正则挑一个资产；同版本优先精确匹配。"""
    for name in sorted(assets):
        if pattern.match(name):
            return name
    return None


def download_exe(name, info, dest_dir, proxy=None, prefixes=None):
    """把一个 exe 镜像到本站，返回 (是否成功, 说明)。"""
    out = os.path.join(dest_dir, name)
    want = info.get("size") or 0
    if os.path.exists(out) and want and os.path.getsize(out) == want:
        return True, "已存在且大小相符，跳过"
    if os.path.exists(out) and want and os.path.getsize(out) != want:
        try:
            os.remove(out)            # 残包，删掉重下
        except OSError:
            pass
    os.makedirs(dest_dir, exist_ok=True)
    tmp = out + ".part"
    last = ""
    for prefix in (prefixes if prefixes is not None else EXE_PREFIXES):
        host = prefix or "github.com(直连)"
        try:
            http_get(prefix + info["url"], timeout=180, proxy=proxy,
                     stream_to=tmp, expect_size=want)
            size = os.path.getsize(tmp)
            # 双重校验：大小必须与 GitHub 声明一致，且必须是个像样的 exe。
            # 实测踩过：GitHub 直连在国内经常中途断流，只下到 604450 字节就"成功"返回
            # （Content-Length 声明 32MB），若只判 HTTP 200 就会把**残包**当成功镜像上去，
            # 用户点下载拿到的是打不开的 exe。
            if want and size != want:
                os.remove(tmp)
                last = "%s 大小不符（%d != %d）" % (host, size, want)
                log("      !! " + last)
                continue
            with open(tmp, "rb") as f:
                if f.read(2) != b"MZ":
                    os.remove(tmp)
                    last = "%s 返回的不是 exe（缺少 MZ 头）" % host
                    log("      !! " + last)
                    continue
            os.replace(tmp, out)      # 原子落地
            return True, "下载成功（%s，%s）" % (human(size), host)
        except Exception as e:  # noqa: BLE001
            last = "%s 失败：%s" % (host, e)
            log("      !! " + last)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            continue
    return False, last or "全部镜像源失败"


def prune_old_exes(files_dir, keep_names, keep_versions=DEFAULT_KEEP_VERSIONS):
    """files/ 里只保留最近 keep_versions 个版本的 exe。"""
    if not os.path.isdir(files_dir):
        return []
    vers = set()
    for f in os.listdir(files_dir):
        m = ASSET_RE.match(f) or SETUP_RE.match(f)
        if m:
            vers.add(m.group(1))

    def vkey(v):
        return [int(x) for x in v.split(".") if x.isdigit()]

    keep_vers = set(sorted(vers, key=vkey, reverse=True)[:max(1, keep_versions)])
    removed = []
    for f in os.listdir(files_dir):
        m = ASSET_RE.match(f) or SETUP_RE.match(f)
        if not m:
            continue
        if m.group(1) in keep_vers or f in keep_names:
            continue
        try:
            os.remove(os.path.join(files_dir, f))
            removed.append(f)
        except OSError:
            pass
    return removed


def sha256_of(path):
    """算文件 sha256。读不到就返回空串（宁可没有，也不能填错）。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except OSError:
        return ""


def write_atomic(path, data, keep_backup=True, tag=""):
    """原子写入：先写 .tmp 再 os.replace；替换前备份旧文件。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if keep_backup and os.path.exists(path):
        bdir = os.path.join(d, BACKUP_DIR_NAME)
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, os.path.join(
            bdir, "%s%s.json" % (stamp, ("_" + tag) if tag else "")))
        olds = sorted(x for x in os.listdir(bdir) if x.endswith(".json"))
        for f in olds[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(bdir, f))
            except OSError:
                pass
    os.replace(tmp, path)


def read_current_version(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return str(json.load(f).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def main():
    ap = argparse.ArgumentParser(description="法院文书下载器 · 自有服务器自动同步")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="网站根目录（默认=脚本所在目录）")
    ap.add_argument("--check", action="store_true", help="只检查，不写任何文件")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP_VERSIONS,
                    help="files/ 保留最近几个版本的 exe（默认 %d）" % DEFAULT_KEEP_VERSIONS)
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY")
                    or os.environ.get("https_proxy") or "",
                    help="HTTP 代理，如 http://127.0.0.1:7890（默认读环境变量）")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                    help="GitHub Token（可选，仅提高 API 速率限制）")
    ap.add_argument("--exe-prefix", action="append", default=[],
                    help="追加/优先使用的 exe 加速前缀（可多次传）")
    args = ap.parse_args()

    dest = os.path.abspath(args.dest)
    proxy = args.proxy or None
    meta_path = os.path.join(dest, META_SUBDIR, "%s.json" % APP_KEY)
    files_dir = os.path.join(dest, FILES_SUBDIR)
    log("网站根目录：%s" % dest)

    # ① 查最新 Release
    try:
        ver, assets, published, notes = fetch_latest(proxy=proxy,
                                                      token=args.token or None)
    except Exception as e:  # noqa: BLE001
        log("!! 查询 GitHub Release 失败：%s" % e)
        return 1
    if not ver or not assets:
        log("!! 未取到有效版本信息，中止（不改动现有文件）")
        return 1
    log("GitHub 最新：v%s（发布于 %s，资产 %d 个）" % (ver, published[:10] or "-", len(assets)))

    cur = read_current_version(meta_path)
    log("本站当前：v%s" % (cur or "（无）"))

    main_asset = pick(assets, ASSET_RE)
    setup_asset = pick(assets, SETUP_RE)
    if not main_asset:
        log("!! Release 里没有找到绿色版资产（%s_vX.Y.Z.exe），中止" % APP_KEY)
        return 1
    log("绿色版资产：%s    安装版：%s" % (main_asset, setup_asset or "（无）"))

    if args.check:
        log("--check：仅检查，未写入任何文件")
        log("将写入：%s" % meta_path)
        log("将下载：%s" % os.path.join(files_dir, main_asset))
        return 0

    # ② 镜像 exe（先下 exe，再写元数据 —— 顺序反了会让用户下到还不存在的文件）
    prefixes = list(args.exe_prefix) + [p for p in EXE_PREFIXES
                                        if p not in args.exe_prefix]
    mirrored = {}
    for name in [n for n in (main_asset, setup_asset) if n]:
        ok, info = download_exe(name, assets[name], files_dir, proxy=proxy,
                                prefixes=prefixes)
        log("  %-40s %s" % (name, info))
        if ok:
            mirrored[name] = os.path.getsize(os.path.join(files_dir, name))

    if main_asset not in mirrored:
        log("!! 绿色版未能镜像成功，**不更新元数据**（否则用户会点到下载失败的链接）")
        return 1

    removed = prune_old_exes(files_dir, set(mirrored), args.keep)
    if removed:
        log("清理旧版 exe：%s" % "、".join(removed))

    # ③ 写元数据。字段与既有 court.json 保持一致（客户端/官网都按这些键读）
    meta = {
        "app": APP_KEY,
        "name": APP_NAME,
        "version": ver,
        "asset": main_asset,
        "notes": notes,
        "url": assets[main_asset]["url"],
        "release_url": "https://github.com/%s/releases/tag/v%s" % (OWNER_REPO, ver),
        "size": mirrored.get(main_asset, assets[main_asset]["size"]),
        # ⚠️ sha256 必须自己算：GitHub 的「API」不返回资产摘要（digest 字段常年是 null），
        #    但客户端 update 会拿它校验下载产物 —— 填错会让所有用户更新失败。
        "sha256": sha256_of(os.path.join(files_dir, main_asset)),
        "published": published[:19].replace("T", " ") if published else "",
    }
    if setup_asset:
        meta["setup_url"] = assets[setup_asset]["url"]
        meta["setup_size"] = mirrored.get(setup_asset, assets[setup_asset]["size"])

    data = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    if os.path.exists(meta_path) and open(meta_path, "rb").read() == data:
        log("元数据无变化，跳过写入")
    else:
        write_atomic(meta_path, data, keep_backup=True, tag="v" + ver)
        log("已写入 %s（v%s，%s）" % (meta_path, ver, human(len(data))))

    if cur and cur != ver:
        log("本站已从 v%s 同步到 v%s" % (cur, ver))
    elif not cur:
        log("本站首次同步到 v%s" % ver)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("已中断")
        sys.exit(130)
