# -*- coding: utf-8 -*-
"""ssl_trust.py — 统一的 SSL 证书信任库构造（仅标准库，无第三方依赖）

为什么需要这个模块
==================
程序原来直接用 ``ssl.create_default_context()``，它**只读操作系统的信任库**。
实测（2026-10-14）：目标法院站点下发的是三级链

    ①  *.court.gov.cn      （叶子）
    ②  CFCA OV OCA         （中间 CA）
    ③  CFCA EV ROOT        （自签名根，服务器把它也一起下发了）

客户端必须在自己信任库里找到 ③ 才能完成校验。**新装的 Windows 常常缺这张根**
（根库靠 Windows Update 逐步补齐；全新装机、离线内网、组策略裁剪、精简镜像都不全），
于是报：

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    self-signed certificate in certificate chain

因为服务器把自签名根也塞进了链里，所以错误码是 19（X509_V_ERR_SELF_SIGNED_CERT_IN_CHAIN），
而不是「找不到签发者」的 20。

本模块的做法：**系统信任库 + 随程序内置的公共 CA，叠加使用。**

    ⚠️ 是「叠加」（load_verify_locations 累加），**不是替换**。
    因此企业/单位自己安装的代理根证书依然有效 —— 不会把做 HTTPS 拦截的内网环境弄坏。

内置证书放在 ``assets/ca/``（用 ``.crt`` —— 证书的标准扩展名）：

    cfca_ev_root.crt   法院站点的信任锚（核心，缺它必挂）
    cfca_ov_oca.crt    中间 CA（服务器现在会下发，内置作保险）
    ca-bundle.crt      全量公共根（Mozilla 体系），兜底更新源等其它站点

证书均为**公开的公共 CA 证书**，不含任何私钥或隐私信息。
"""

import hashlib
import os
import ssl
import sys

# 内置证书目录（相对包根，兼容 PyInstaller onefile 的 sys._MEIPASS）
CA_SUBDIR = os.path.join("assets", "ca")
# 历史常量，保留以兼容外部引用；加载逻辑已改为下面的固定清单，不再按扩展名扫描。
CA_EXTS = (".crt", ".cer", ".pem")

# 内置证书的**固定清单**：文件名 + 内容哈希。
#
# 为什么不能扫目录（2026-10-31 安全审查）：早期实现是 os.listdir 取 assets/ca/
# 下所有 *.crt/*.cer/*.pem 全部 load_verify_locations。那等于「目录里放了什么就
# 信任什么」——程序目录（绿色版所在位置、打包后的 _MEIPASS 解包目录）当前用户
# 可写，投放一张自签根即可让本进程后续所有 HTTPS（含更新下载）被解密替换。
# 现在只认这三个名字，且内容哈希必须与发布时一致，否则跳过并写日志。
#
# ⚠️ 换证书 / 升级 ca-bundle 时必须同步更新这里的哈希，否则该证书会被跳过
#    （日志里会出现「内容与预期不一致」）。这是刻意设计，不是 bug。
BUILTIN_CA = (
    ("cfca_ev_root.crt", "4da4140976f6c2b38f960e149fb4a374a9a6c1bf29279d80a8fc285881e62313"),
    ("cfca_ov_oca.crt",  "5fea95256174aa650ca1376e6251a9ab5ca8f3c802b137031d5f464bcf0ab6c0"),
    ("ca-bundle.crt",    "7416d8751c49b11e12b9744b10950fda2b54ec38882ae91f46d20560e888a521"),
)


def _file_sha256(path):
    """算文件 SHA256；读不了返回空串。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(256 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def base_dir():
    """包根目录：打包运行时是解包目录 _MEIPASS，源码运行时是本文件所在目录。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def builtin_ca_files():
    """列出可用的内置 CA 文件（固定清单，只返回真实存在的）。"""
    d = os.path.join(base_dir(), CA_SUBDIR)
    out = []
    for name, _sha in BUILTIN_CA:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            out.append(p)
    return out


def build_ssl_context(verify=True, log_fn=None):
    """构造 SSLContext：默认信任库 + 内置 CA 叠加。

    verify=False 时关闭校验（仅供 ``--no-verify`` 应急通道使用，默认永远为 True）。

    返回 ``(ctx, info)``：
        ctx  —— 可用的 SSLContext
        info —— 诊断字典，含 ok / total / loaded / failed / files，
                出问题时可直接打进日志，用户报障时一眼能看出「内置 CA 到底有没有加载上」。
    """
    ctx = ssl.create_default_context()
    info = {"verify": bool(verify), "total": 0, "loaded": [], "failed": [], "files": []}

    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            info["total"] = len(ctx.get_ca_certs())
        except Exception:
            pass
        return ctx, info

    try:
        info["total"] = len(ctx.get_ca_certs())
    except Exception:
        pass

    expect = dict(BUILTIN_CA)
    for path in builtin_ca_files():
        name = os.path.basename(path)
        info["files"].append(name)
        want = expect.get(name, "")
        if want and _file_sha256(path) != want:
            # 内容与发布时不一致 → 宁可不加载。这里防的是「有人往程序目录里
            # 放一张同名根证书」：信任锚必须是发布时固定的那几张。
            reason = "内容与发布时不一致（可能被替换），已跳过"
            info["failed"].append("%s: %s" % (name, reason))
            if log_fn:
                log_fn("[SSL] 内置证书校验失败 %s：%s" % (name, reason))
            continue
        try:
            # load_verify_locations 是**累加**语义：在已有信任库基础上追加，
            # 不会丢弃系统库 —— 这是「叠加而非替换」的实现基础。
            ctx.load_verify_locations(cafile=path)
            info["loaded"].append(name)
        except Exception as e:  # noqa: BLE001
            # 单个文件损坏不能拖垮整体：其余证书照常生效
            info["failed"].append("%s: %s" % (name, e))
            if log_fn:
                log_fn("[SSL] 内置证书加载失败 %s：%s" % (name, e))

    try:
        info["total_after"] = len(ctx.get_ca_certs())
    except Exception:
        info["total_after"] = info["total"]

    if log_fn:
        log_fn("[SSL] 信任库 %s 张 → %s 张（内置加载 %d/%d）"
               % (info.get("total"), info.get("total_after"),
                  len(info["loaded"]), len(info["files"])))
    return ctx, info


def describe_cert_error(exc):
    """把 SSL 证书错误翻成用户看得懂的中文，并给出该做什么。

    返回 None 表示「不是证书类错误」，交回调用方按普通网络错误处理。
    """
    if not isinstance(exc, ssl.SSLCertVerificationError):
        # 部分环境抛的是包了一层的 URLError(SSLCertVerificationError)
        reason = getattr(exc, "reason", None)
        if not isinstance(reason, ssl.SSLCertVerificationError):
            return None
        exc = reason

    code = getattr(exc, "verify_code", None)
    detail = getattr(exc, "verify_message", "") or str(exc)

    if code == 19:      # 链里有自签名证书，但本机不信任它
        return (
            "安全连接校验失败：这台电脑的证书库里缺少法院网站使用的根证书。\n\n"
            "程序已尝试使用内置证书；仍然失败通常有两种原因：\n"
            "  1) 电脑的系统证书库不完整（新装系统常见）——可让程序更新到最新版，"
            "或请管理员补装 CFCA 根证书；\n"
            "  2) 所在网络做了 HTTPS 拦截（单位防火墙 / 安全软件），"
            "替换成了它自己的证书 —— 这种情况需要联系网络管理员。\n\n"
            "原始错误：%s" % detail
        )
    if code == 20:      # 找不到签发者
        return ("安全连接校验失败：本机找不到该网站的签发机构（多半是证书链不完整）。\n\n"
                "请先确认电脑系统时间是否正确，再联系网络管理员。\n\n"
                "原始错误：%s" % detail)
    if code == 18:      # 叶子证书本身就是自签名
        return ("安全连接校验失败：该网站出示的证书是自签名的，正常官网不会如此，"
                "**极可能是所在网络做了 HTTPS 拦截**。\n\n"
                "请勿在此时关闭证书校验，应联系网络管理员确认。\n\n"
                "原始错误：%s" % detail)
    return ("安全连接校验失败（错误码 %s）。\n\n"
            "原始错误：%s" % (code, detail))


if __name__ == "__main__":
    # 手动自检：python ssl_trust.py [host]
    host = sys.argv[1] if len(sys.argv) > 1 else "zxfw.court.gov.cn"
    ctx, info = build_ssl_context(log_fn=print)
    print("内置证书文件 :", info["files"] or "（无）")
    print("成功加载     :", info["loaded"])
    print("加载失败     :", info["failed"] or "（无）")
    import socket
    try:
        with socket.create_connection((host, 443), timeout=20) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                print("连接 %s : 通过（%s）" % (host, ss.version()))
    except Exception as e:  # noqa: BLE001
        msg = describe_cert_error(e)
        print("连接 %s : 失败\n%s" % (host, msg or e))
