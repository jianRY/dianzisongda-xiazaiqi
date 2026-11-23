# -*- coding: utf-8 -*-
"""ui_kit —— 法院文书下载器界面控件库（方案 A · 经典商务蓝，无第三方依赖）。

设计要点：
  * DPI 感知在本模块 import 时立即生效（必须在任何 import tkinter 之前被 import）。
  * 颜色统一取自 SKIN（单皮肤，方案 A 商务蓝）。
  * 尺寸一律过 u()（乘 DPI 缩放）；字号用「点」，Tk 自动按 DPI 换算（只接受整数点值）。
  * Tk 无圆角/投影 → 全部 Canvas 自绘。
"""
import ctypes
import time


def _apply_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()       # 老系统兜底
        except Exception:
            pass


_apply_dpi_awareness()

import tkinter as tk
from tkinter import ttk

SCALE = 1.0


def setup_scale(root):
    """按屏幕实际 DPI 推导缩放比（150% → 1.5）。"""
    global SCALE
    try:
        SCALE = root.winfo_fpixels("1i") / 96.0
    except Exception:
        SCALE = 1.0
    return SCALE


def refresh_scale(root):
    """重新按**当前所在显示器**的 DPI 计算缩放比，变化返回 True。

    ⚠️ 已知边界：Tk 不能重排已经创建出来的控件的像素尺寸，所以这里只更新
    SCALE（影响之后新建的弹窗/画布），不会让主界面自动「重新长一遍」。
    调用方应把「比例变了」当成「提示用户重启更准 + 重新设定窗口大小」来用，
    不要指望它把整个界面重排 —— 那属于重建 UI，不在本函数职责内。
    """
    global SCALE
    try:
        cur = root.winfo_fpixels("1i") / 96.0
    except Exception:
        return False
    if not cur or abs(cur - SCALE) < 0.02:
        return False
    SCALE = cur
    return True


def u(v):
    """逻辑尺寸 → 物理像素。"""
    return int(round(v * SCALE))


FONT_UI = "Microsoft YaHei UI"


def f(size, bold=False):
    """字号用「点」，Tk 按 DPI 自动换算。⚠️ 只接受整数点值。"""
    size = int(round(size))
    return (FONT_UI, size, "bold") if bold else (FONT_UI, size)


def parent_bg(widget, fallback="#FFFFFF"):
    try:
        return widget.cget("bg")
    except Exception:
        return fallback


# ───────────────────────── 皮肤（方案 A · 经典商务蓝） ─────────────────────────
class Skin:
    bg = "#F2F4F8"
    card = "#FFFFFF"
    border = "#DFE4EC"
    text = "#1F2937"
    muted = "#6B7280"
    faint = "#9AA3AF"
    accent = "#2E5DA8"
    accent_d = "#254C8C"
    accent_l = "#E8EEF9"
    track = "#E4E9F1"
    ok = "#15803D"
    warn = "#B45309"
    bad = "#B91C1C"
    # 历史常量：v2.16.0 前顶栏是整条深蓝，这两项只服务于旧画法。
    # 保留是因为外部脚本/预览稿可能引用，删掉会 ImportError/AttributeError。
    header = "#2E5DA8"
    header_text_dim = "#BBD0EE"
    gold = "#D9A441"
    # v2.16.0 顶栏改白底扁平（对齐发票工具那套：白底 + 浅分割线 + 徽章 + 文字入口）
    header_bg = "#FFFFFF"
    header_line = "#E8EBF2"
    header_title = "#1F2937"
    header_link = "#6B7280"
    badge_bg = "#EEF2FF"
    badge_fg = "#1D4ED8"
    radius_card = 8
    radius_btn = 8
    btn_h = 46


SKIN = Skin()


# ───────────────────────── 绘制原语 ─────────────────────────
def rr(cv, x1, y1, x2, y2, r, fill, tags=None):
    """圆角矩形填充：4 个扇形 + 2 个矩形拼合（outline 同色，避免接缝）。"""
    tags = tags or ()
    r = max(0, r)
    kw = dict(fill=fill, outline=fill, tags=tags)
    d = 2 * r
    if r > 0:
        cv.create_arc(x1, y1, x1 + d, y1 + d, start=90, extent=90, style="pieslice", **kw)
        cv.create_arc(x2 - d, y1, x2, y1 + d, start=0, extent=90, style="pieslice", **kw)
        cv.create_arc(x1, y2 - d, x1 + d, y2, start=180, extent=90, style="pieslice", **kw)
        cv.create_arc(x2 - d, y2 - d, x2, y2, start=270, extent=90, style="pieslice", **kw)
    cv.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
    cv.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)


def rr_border(cv, x1, y1, x2, y2, r, border, fill, tags=None):
    """带 1px 描边的圆角矩形 = 外圈描边色 + 内缩 1px 填充色。"""
    rr(cv, x1, y1, x2, y2, r, border, tags)
    rr(cv, x1 + 1, y1 + 1, x2 - 1, y2 - 1, max(0, r - 1), fill, tags)


def text_w(cv, text, font):
    """量文字宽度（临时画一个再删）。"""
    t = cv.create_text(-9999, -9999, text=text, font=font)
    b = cv.bbox(t)
    cv.delete(t)
    return (b[2] - b[0]) if b else 0


# ───────────────────────── 控件 ─────────────────────────
class Card(tk.Canvas):
    """圆角白卡：外部布局正常 pack/grid，内部内容塞进 self.body。

    auto=True  → 高度由内容决定（普通卡片）
    fill=True  → 高度由父容器分配（日志卡片）
    """

    def __init__(self, master, pad=12, auto=True, fill=False):
        sk = SKIN
        self.pad = u(pad)
        self.auto, self.fill_mode = auto, fill
        self.radius = u(sk.radius_card)
        bg = parent_bg(master, sk.bg)
        super().__init__(master, bg=bg, highlightthickness=0, bd=0,
                         height=u(40) if auto else u(60))
        self.body = tk.Frame(self, bg=sk.card)
        self._win = self.create_window(self.pad, self.pad, window=self.body,
                                       anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.bind("<Configure>", self._on_canvas)

    def _on_body(self, e):
        if self.auto:
            want = e.height + 2 * self.pad
            if want != self.winfo_height():
                self.configure(height=want)

    def _on_canvas(self, e):
        w = max(1, e.width - 2 * self.pad)
        if self.fill_mode:
            self.itemconfigure(self._win, width=w,
                               height=max(1, e.height - 2 * self.pad))
        else:
            self.itemconfigure(self._win, width=w)
        self._paint(e.width, e.height)

    def repaint(self):
        """按**当前** DPI 比例重画背景（不移动 body，不重建控件）。

        ⚠️ 只用于「显示器缩放变了」这种场景：已建控件的像素尺寸改不了，
           但圆角半径、线宽这类纯绘制参数可以按新比例重算，否则边框会与
           内容错开 1~2 像素，露出一小截多余细线。
        """
        if not self.winfo_exists():
            return
        self.radius = u(SKIN.radius_card)
        self._paint(self.winfo_width(), self.winfo_height())

    def _paint(self, w, h):
        if not self.winfo_exists():
            return
        self.delete("cardbg")
        if w < 4 or h < 4:
            return
        # 半径按**当前** SCALE 现算，别用构造时缓存的值：换显示器后旧值会让
        # 圆角与边框对不上（视觉上就是「多出来一条线」）。
        rr_border(self, 0, 0, w - 1, h - 1, u(SKIN.radius_card), SKIN.border,
                  SKIN.card, ("cardbg",))
        self.tag_lower("cardbg")


class RoundButton(tk.Canvas):
    """自绘圆角按钮。kind: primary | ghost | danger。"""

    def __init__(self, master, text="", command=None, kind="primary",
                 height=None, width=None, font_size=12, icon=None):
        sk = SKIN
        self.text, self.command, self.kind, self.icon = text, command, kind, icon
        self.font_size = font_size
        self._enabled = True
        self._hover = False
        bg = parent_bg(master, sk.card)
        h = u(height if height is not None else sk.btn_h)
        kw = dict(height=h, bg=bg, highlightthickness=0, bd=0)
        if width:
            kw["width"] = u(width)
        super().__init__(master, **kw)
        self.configure(cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def configure_state(self, state="normal", text=None, command=None):
        """state: normal | disabled；text / command 给了就一并换。"""
        self._enabled = (state == "normal")
        if text is not None:
            self.text = text
        if command is not None:
            self.command = command
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def _colors(self):
        sk = SKIN
        if not self._enabled:
            if self.kind == "ghost":
                return "#FBFCFE", sk.faint, sk.border
            return "#DCE4F5", "#9FB4E0", None
        if self.kind == "primary":
            return sk.accent_d if self._hover else sk.accent, "#FFFFFF", None
        if self.kind == "danger":
            return "#FEF2F2" if self._hover else sk.card, sk.bad, sk.border
        return "#F3F5F9" if self._hover else sk.card, sk.text, sk.border

    def _draw(self):
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        fill, fg, bd = self._colors()
        if bd:
            rr_border(self, 1, 1, w - 2, h - 2, u(SKIN.radius_btn), bd, fill)
        else:
            rr(self, 1, 1, w - 2, h - 2, u(SKIN.radius_btn), fill)
        cx, cy = w / 2, h / 2
        if self.icon:
            tw = text_w(self, self.text, f(self.font_size, True))
            iw = text_w(self, self.icon, f(self.font_size - 3))
            total = iw + u(10) + tw
            x0 = cx - total / 2
            self.create_text(x0, cy, text=self.icon, font=f(self.font_size - 3),
                             fill=fg, anchor="w")
            self.create_text(x0 + iw + u(10), cy, text=self.text,
                             font=f(self.font_size, True), fill=fg, anchor="w")
        else:
            self.create_text(cx, cy, text=self.text, font=f(self.font_size, True),
                             fill=fg)

    def repaint(self):
        """按当前 DPI 比例重画（换显示器/改缩放后调用）。"""
        self._draw()

    def _enter(self, _e):
        self._hover = True
        self._draw()

    def _leave(self, _e):
        self._hover = False
        self._draw()

    def _click(self, _e):
        if self._enabled and self.command:
            self.command()


class RoundCheck(tk.Canvas):
    """自绘复选框（18px 圆角方块 + 对勾）。"""

    def __init__(self, master, text="", variable=None, font_size=None,
                 command=None):
        sk = SKIN
        self.text = text
        self.var = variable or tk.BooleanVar(value=False)
        self.command = command
        self.font_size = font_size or 10
        bg = parent_bg(master, sk.card)
        self._size = u(18)
        super().__init__(master, bg=bg, highlightthickness=0, bd=0,
                         height=self._size, width=u(420), cursor="hand2")
        self._var_trace = self.var.trace_add("write", self._on_var)
        # 销毁时摘掉 trace，避免共享变量在控件销毁后仍调 _draw()
        self.bind("<Destroy>", self._on_destroy)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._toggle)

    def repaint(self):
        """按当前 DPI 比例重画（方块尺寸跟随控件实际高度，不做重排）。"""
        self._draw()

    def _on_var(self, *_):
        self._draw()
        if self.command:
            try:
                self.command()
            except Exception:
                pass

    def _on_destroy(self, e):
        if e.widget is not self:
            return
        try:
            self.var.trace_remove("write", self._var_trace)
        except Exception:
            pass

    def _toggle(self, _e):
        self.var.set(not self.var.get())

    def _draw(self):
        if not self.winfo_exists():
            return
        self.delete("all")
        h = self.winfo_height()
        s = self._size
        y = (h - s) / 2
        x = u(2)
        if self.var.get():
            rr(self, x, y, x + s, y + s, u(4), SKIN.accent)
            self.create_line(x + s * 0.26, y + s * 0.52, x + s * 0.44, y + s * 0.70,
                             x + s * 0.76, y + s * 0.30, fill="#FFFFFF",
                             width=u(2), capstyle="round", joinstyle="round")
        else:
            rr_border(self, x, y, x + s, y + s, u(4), "#CBD3DF", SKIN.card)
        self.create_text(x + s + u(9), h / 2, text=self.text,
                         font=f(self.font_size), fill=SKIN.text, anchor="w")


class SegmentedControl(tk.Canvas):
    """分段选择器：等分若干档，点击回调（替代旧 Radiobutton 组）。"""

    def __init__(self, master, options, value, command=None,
                 width=None, height=28):
        sk = SKIN
        self.options = list(options)          # [(key, label), ...]
        self.value = value
        self.command = command
        self._enabled = True
        bg = parent_bg(master, sk.card)
        super().__init__(master, bg=bg, highlightthickness=0, bd=0,
                         height=u(height), width=u(width or 180), cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)

    def repaint(self):
        """按当前 DPI 比例重画（圆角/文字间距随新比例，控件尺寸不动）。"""
        self._draw()

    def set_value(self, key):
        self.value = key
        self._draw()

    def set_enabled(self, enabled=True):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def _draw(self):
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 4:
            return
        sk = SKIN
        trough, sel_bg = "#EDF0F6", sk.card
        sel_fg, fg = sk.accent_d, (sk.faint if not self._enabled else sk.muted)
        rr(self, 0, 0, w - 1, h - 1, u(7), trough)
        n = len(self.options)
        if n == 0:
            return
        seg = w / n
        for i, (key, label) in enumerate(self.options):
            x1 = i * seg
            x2 = x1 + seg
            if key == self.value:
                rr(self, x1 + u(2), u(2), x2 - u(2), h - u(2), u(5), sel_bg)
                self.create_text((x1 + x2) / 2, h / 2, text=label,
                                 font=f(9, self._enabled), fill=sel_fg)
            else:
                self.create_text((x1 + x2) / 2, h / 2, text=label,
                                 font=f(9), fill=fg)

    def _click(self, e):
        if not self._enabled:
            return
        w = self.winfo_width()
        n = len(self.options)
        if n == 0:
            return
        i = min(n - 1, max(0, int(e.x / (w / n))))
        key = self.options[i][0]
        if key != self.value:
            self.value = key
            self._draw()
            if self.command:
                self.command(key)


class RoundProgress(tk.Canvas):
    """圆角进度条；取消/停止态用琥珀色。"""

    def __init__(self, master, height=9, width=None):
        sk = SKIN
        bg = parent_bg(master, sk.bg)
        kw = dict(bg=bg, highlightthickness=0, bd=0, height=u(height))
        if width:
            kw["width"] = u(width)
        super().__init__(master, **kw)
        self._value = 0.0
        self._stopped = False
        self.bind("<Configure>", lambda e: self._draw())

    def repaint(self):
        """按当前 DPI 比例重画（轨道圆角取控件实际高度，已填充部分保持不变）。"""
        self._draw()

    def set_value(self, pct, stopped=False):
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = 0.0
        self._value = max(0.0, min(100.0, pct))
        self._stopped = bool(stopped)
        self._draw()

    def reset(self):
        self.set_value(0)

    def _draw(self):
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        sk = SKIN
        track = sk.track
        fill = sk.warn if self._stopped else sk.accent
        rr(self, 0, 1, w - 1, h - 2, (h - 3) / 2, track)
        fw = (w - 1) * self._value / 100.0
        if fw >= h - 2:
            rr(self, 0, 1, fw, h - 2, (h - 3) / 2, fill)
        elif fw > 2:
            rr(self, 0, 1, 2 * (h - 3) / 2 + 2, h - 2, (h - 3) / 2, fill)


class LogView(tk.Frame):
    """带分级着色的日志区（✓ 绿 / ⚠ 琥珀 / ✗ 红 / 标题蓝 / ⏸ 蓝）。"""

    # 保留的最大行数。超过后一次性掐掉最旧的一批（不是每加一行删一行，
    # 否则大日志量下每条都要重排整个 Text，反而更慢）。
    MAX_LINES = 3000

    def __init__(self, master):
        sk = SKIN
        super().__init__(master, bg=sk.card)
        self.txt = tk.Text(self, bd=0, highlightthickness=0, bg=sk.card,
                           fg=sk.text, font=f(9), wrap="word",
                           padx=u(2), pady=u(2), spacing1=u(1), spacing3=u(2))
        self.txt.tag_configure("ok", foreground=sk.ok)
        self.txt.tag_configure("warn", foreground=sk.warn)
        self.txt.tag_configure("bad", foreground=sk.bad)
        self.txt.tag_configure("hl", foreground=sk.accent_d)
        self.txt.tag_configure("dim", foreground=sk.faint)
        self.sb = ttk.Scrollbar(self, orient="vertical", style="P.Vertical.TScrollbar",
                                command=self.txt.yview)
        self.txt.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(u(4), 0))
        self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.txt.configure(state=tk.DISABLED)

    @staticmethod
    def tag_for(msg):
        if "✓" in msg:
            return "ok"
        if "⚠" in msg or "跳过" in msg:
            return "warn"
        if "✗" in msg or "失败" in msg or "错误" in msg:
            return "bad"
        if "取消" in msg or "⏸" in msg:
            return "hl"
        if msg.startswith("===") or msg.startswith("---") or msg.startswith("法院文书下载器"):
            return "hl"
        return None

    def append(self, msg):
        self.txt.configure(state=tk.NORMAL)
        self.txt.insert(tk.END, msg + "\n", self.tag_for(msg) or ())
        # 行数上限：下载几百份文书时日志能涨到几万行，Text 每插一行都要重排整块
        # 缓冲并刷新滚动条，界面会越跑越卡（还占内存）。超过上限就掐掉最旧的
        # 一批，留出余量再触发，摊薄单次开销。
        try:
            lines = int(self.txt.index("end-1c").split(".")[0])
            if lines > self.MAX_LINES + 500:
                self.txt.delete("1.0", "%d.0" % (lines - self.MAX_LINES + 1))
        except Exception:  # noqa: BLE001
            pass
        self.txt.see(tk.END)
        self.txt.configure(state=tk.DISABLED)

    def clear(self):
        self.txt.configure(state=tk.NORMAL)
        self.txt.delete("1.0", tk.END)
        self.txt.configure(state=tk.DISABLED)


def style_ttk():
    """把 ttk 的 Entry / Scrollbar 调成与皮肤一致的扁平风。"""
    sk = SKIN
    st = ttk.Style()
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure("P.TEntry", fieldbackground=sk.card, foreground=sk.text,
                 insertcolor=sk.text, bordercolor=sk.border, lightcolor=sk.border,
                 darkcolor=sk.border, padding=(u(8), u(7)), relief="flat")
    st.configure("P.Vertical.TScrollbar", gripcount=0, background="#C9D2DE",
                 darkcolor="#C9D2DE", lightcolor="#C9D2DE",
                 troughcolor="#F6F8FB", bordercolor="#F6F8FB",
                 arrowcolor=sk.card, arrowsize=u(10))
