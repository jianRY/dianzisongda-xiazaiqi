# -*- coding: utf-8 -*-
"""生成 Inno Setup 安装包所需资源（安装向导配图 + 中文语言包），并输出 installer.iss。

产物（都放在项目根的 installer/ 下，随仓库走，便于以后复用）：
    installer/ChineseSimplified.isl   Inno 非官方简体中文语言包
    installer/wizard_image.bmp        安装向导左侧大图 164x314
    installer/wizard_small.bmp        安装向导右上角小图 55x58

用法：python make_installer_assets.py [版本号]
"""
import os
import shutil
import sys

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
OUTDIR = os.path.join(ROOT, "installer")
CACHE_ISL = os.path.join(ROOT, ".pybuild_cache", "installer", "ChineseSimplified.isl")
ISL_URL = ("https://raw.githubusercontent.com/kira-96/"
           "Inno-Setup-Chinese-Simplified-Translation/main/ChineseSimplified.isl")
PROXY = "http://127.0.0.1:10808"

NAVY_TOP = (16, 46, 82)
NAVY_BOTTOM = (6, 20, 40)
GOLD = (222, 178, 74)
FONT_CANDS = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\simhei.ttf"]


def vertical_gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h), Image.BILINEAR)


def radial_glow(size, center, radius, color, strength=110):
    w, h = size
    g = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(g)
    cx, cy = center
    steps = 40
    for i in range(steps, 0, -1):
        r = radius * i / steps
        v = int(strength * (1 - i / steps) ** 1.6)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=v)
    g = g.filter(ImageFilter.GaussianBlur(radius * 0.06))
    layer = Image.new("RGB", (w, h), color)
    return layer, g


def load_font(size):
    for p in FONT_CANDS:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return None


def build_wizard_image(icon):
    W, H = 164, 314
    bg = vertical_gradient((W, H), NAVY_TOP, NAVY_BOTTOM).convert("RGB")

    # 背后柔光，让图标浮起来
    layer, mask = radial_glow((W, H), (W // 2, 118), 92, (48, 108, 178), 120)
    bg = Image.composite(layer, bg, mask)

    # 顶部与底部各一条金色细线，呼应图标的金色天平
    d = ImageDraw.Draw(bg)
    d.line([(28, 22), (W - 28, 22)], fill=(GOLD[0] // 2, GOLD[1] // 2, GOLD[2] // 2), width=1)
    d.line([(28, H - 30), (W - 28, H - 30)], fill=(GOLD[0] // 3, GOLD[1] // 3, GOLD[2] // 3), width=1)

    # 居中放应用图标
    s = 108
    ic = icon.convert("RGBA").resize((s, s), Image.LANCZOS)
    bg.paste(ic, ((W - s) // 2, 118 - s // 2), ic)

    # 底部文字（字体缺失则跳过，不冒险画方块）
    font = load_font(15)
    if font:
        text = "法院文书下载器"
        tmp = ImageDraw.Draw(bg)
        try:
            tw = tmp.textlength(text, font=font)
        except Exception:
            tw = W
        tmp.text(((W - tw) / 2, H - 68), text, font=font, fill=(214, 226, 240))
        font2 = load_font(10)
        if font2:
            sub = "电子送达文书下载"
            try:
                sw = tmp.textlength(sub, font=font2)
            except Exception:
                sw = W
            tmp.text(((W - sw) / 2, H - 48), sub, font=font2, fill=(126, 154, 186))

    # Inno 要求 24 位 BMP
    out = os.path.join(OUTDIR, "wizard_image.bmp")
    bg.convert("RGB").save(out, format="BMP")
    return out


def build_small_image(icon):
    W, H = 55, 58
    bg = vertical_gradient((W, H), NAVY_TOP, NAVY_BOTTOM).convert("RGB")
    s = 40
    ic = icon.convert("RGBA").resize((s, s), Image.LANCZOS)
    bg.paste(ic, ((W - s) // 2, (H - s) // 2), ic)
    out = os.path.join(OUTDIR, "wizard_small.bmp")
    bg.convert("RGB").save(out, format="BMP")
    return out


def ensure_isl():
    """确保 install_/ChineseSimplified.isl 存在（优先用缓存，缺失则联网下载）。"""
    dst = os.path.join(OUTDIR, "ChineseSimplified.isl")
    if os.path.isfile(dst) and os.path.getsize(dst) > 3000:
        return dst
    if os.path.isfile(CACHE_ISL):
        shutil.copyfile(CACHE_ISL, dst)
        return dst
    import urllib.request
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    req = urllib.request.Request(ISL_URL, headers={"User-Agent": "Mozilla/5.0"})
    data = opener.open(req, timeout=60).read()
    if len(data) < 3000 or b"LanguageName" not in data:
        raise SystemExit("下载中文语言包失败（内容异常）")
    open(dst, "wb").write(data)
    return dst


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    print("语言包:", ensure_isl())

    icon = Image.open(os.path.join(ASSETS, "app_icon_1024.png"))
    p1 = build_wizard_image(icon)
    p2 = build_small_image(icon)
    for p in (p1, p2):
        im = Image.open(p)
        print("生成:", p, im.size, "%d 字节" % os.path.getsize(p))


if __name__ == "__main__":
    main()
