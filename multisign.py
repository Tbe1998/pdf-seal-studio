#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multisign.py —— PDF 多处定位、每处内容可不同的批量数字签名工具

核心能力
  1. 按页面 + 矩形坐标(支持左上角坐标系)在 PDF 上放置任意多个签名域
  2. 每个签名域的内容(文字/图片/时间/字体大小/边框/不透明度)互不相同
  3. 每个签名域可以是独立的**标准 PDF 数字签名**(PAdES / adbe.pkcs7.detached)，
     逐次增量签名，互相叠加不破坏已有签名
  4. 每个签名域可以指定**不同的证书**(.p12/.pfx)与不同的签名字段名
  5. --preview 先出预览图核对位置，再真正签
  6. --list 列出已有签名域；--verify 校验签名完整性

注意(务必读)
  * 本工具做的是国际标准 PDF 数字签名，由 WPS / Adobe / 各类验签平台均可验证。
    它**不是** WPS 客户端"证书签名"按钮的自动化 —— WPS 没有公开的签名 API/USBKey 驱动接口。
  * 如果你的证书在 Windows 证书存储 / U 盾里，需要先导出成 .pfx/.p12(带私钥)才能给本工具用；
    若证书无法导出(硬件 U 盾)，只能走 --visual-only(只盖视觉章，无密码学签名)或手工在 WPS 里签。

用法示例
  # 1) 生成配置模板
  python multisign.py --init job.json --input 日报.pdf

  # 2) 预览签名位置(不改 PDF)
  python multisign.py -c job.json --preview preview.png

  # 3) 正式签名
  python multisign.py -c job.json

  # 4) 校验 / 列出签名
  python multisign.py --verify 日报_已签名.pdf
  python multisign.py --list 日报_已签名.pdf

  # 5) 命令行直接签(单处)
  python multisign.py --input a.pdf --output b.pdf -p "cert/my.p12:密码" \
      --at "1,320,760,160,50=监测负责人：张三" --at "1,320,690,160,50=审核：李四"
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


# ----------------------------------------------------------------------------- 配置模型
@dataclass
class CertSpec:
    pfx: str
    password: str = ""
    # 可选：告诉验签方这些信息(不参与密码学校验)
    reason: str = ""
    location: str = ""
    contact: str = ""


@dataclass
class SigSpec:
    name: str                      # 签名域名称(必须在 PDF 内唯一，字母数字下划线)
    page: int                      # 1 起始页码
    rect: List[float]              # [x, y, 宽, 高]
    text: str = ""                 # 章面文字，支持 \n 与占位符
    font_size: int = 10
    border: bool = False
    opacity: Optional[float] = None   # 不透明度；None=红章自动 0.80，其余 1.0
    blend: str = "auto"           # auto=红章烘焙正片叠底,其余普通 | bake-multiply | multiply | normal
    image: Optional[str] = None    # 章面底图(手写签名/印章 PNG，建议透明底)
    image_opacity: Optional[float] = None
    cert: Optional[Dict[str, Any]] = None   # 每处可用不同证书
    visual_only: bool = False      # True = 只盖视觉章，不做数字签名
    text_layout: str = "center"    # center | bottom | top（有底图时用 bottom 避免重叠）
    # --- 图片电子签章（无证书场景）专用 ---
    image_scale: float = 1.0       # 章图占框的比例，1.0=铺满整个框（保持比例）
    image_pos: str = "center"      # center | top | bottom | left | right 等组合，如 "top-right"
    remove_white: Optional[bool] = None  # 白底转透明；None=自动(JPG/无透明通道时开启)
    white_threshold: int = 238     # 白底判定阈值，越小越严格
    dpi: int = 300                 # 合成章面的分辨率
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    input: str
    output: str
    origin: str = "top-left"       # top-left | bottom-left
    font: Optional[str] = None
    default_cert: Optional[Dict[str, Any]] = None
    signatures: List[SigSpec] = field(default_factory=list)


# ----------------------------------------------------------------------------- 依赖加载（惰性，缺依赖时给中文提示）
_FITZ = None
_PIKEPDF = None

DEP_HINT = (
    "缺少依赖：{mod}\n"
    "请先安装依赖（任选一种）：\n"
    "  Windows:      install.bat          或  .venv\\Scripts\\pip install -r requirements.txt\n"
    "  Linux/macOS:  bash install.sh      或  pip install -r requirements.txt\n"
    "  手动单装:      pip install {pip}"
)


def get_fitz():
    """PyMuPDF：新版本向导名 pymupdf，1.24 之前只能 import fitz，两种都兼容。"""
    global _FITZ
    if _FITZ is None:
        try:
            import pymupdf as _m
        except ImportError:
            try:
                import fitz as _m  # 旧版本包名
            except ImportError:
                raise ImportError(DEP_HINT.format(mod="PyMuPDF（pymupdf / fitz）", pip="pymupdf")) from None
        _FITZ = _m
    return _FITZ


def get_pikepdf():
    global _PIKEPDF
    if _PIKEPDF is None:
        try:
            import pikepdf as _m
        except ImportError:
            raise ImportError(DEP_HINT.format(mod="pikepdf", pip="pikepdf")) from None
        _PIKEPDF = _m
    return _PIKEPDF


def get_pil():
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        raise ImportError(DEP_HINT.format(mod="Pillow（PIL）", pip="Pillow")) from None
    from PIL import Image
    return Image


# ----------------------------------------------------------------------------- 工具函数
def die(msg: str, code: int = 2):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str):
    print(f"[警告] {msg}", file=sys.stderr)


def info(msg: str):
    print(msg, flush=True)


def pick_font(explicit: Optional[str]) -> str:
    if explicit:
        if not os.path.exists(explicit):
            die(f"字体文件不存在: {explicit}")
        return explicit
    for p in DEFAULT_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    die("找不到中文字体，请用 --font 指定一个 .ttf/.ttc")


def ensure_ttf(font_path: str) -> str:
    """.ttc 字体集合必须先抽出单个 .ttf，pyHanko 不支持直接读 ttc。结果缓存复用。"""
    if not font_path.lower().endswith(".ttc"):
        return font_path
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "multisign-fonts")
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, os.path.splitext(os.path.basename(font_path))[0] + ".ttf")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(font_path):
        return out
    from fontTools.ttLib import TTFont
    tt = TTFont(font_path, fontNumber=0)
    tt.save(out)
    info(f"[字体] 已从 {os.path.basename(font_path)} 抽出 {os.path.basename(out)} 供签名使用")
    return out


def render_text(tpl: str, ctx: Dict[str, str]) -> str:
    """占位符替换: {date} {time} {datetime} {file} {page} {name} {env:XXX}"""
    out = tpl
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", v)

    def env_sub(m):
        return os.environ.get(m.group(1), "")

    import re
    out = re.sub(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", env_sub, out)
    return out.replace("\\n", "\n")


def page_geometry(pdf_path: str, page_no: int) -> Tuple[float, float, int]:
    """返回 (宽, 高, 旋转角)，按 MediaBox 计算。"""
    pikepdf = get_pikepdf()
    with pikepdf.open(pdf_path) as pdf:
        if page_no < 1 or page_no > len(pdf.pages):
            die(f"页码 {page_no} 超出范围(共 {len(pdf.pages)} 页)")
        pg = pdf.pages[page_no - 1]
        box = [float(x) for x in pg.mediabox]
        w, h = abs(box[2] - box[0]), abs(box[3] - box[1])
        rot = int(pg.get("/Rotate", 0) or 0) % 360
        return w, h, rot


TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_asset(p: Optional[str], *bases: Optional[str]) -> Optional[str]:
    """解析配置里的图片/证书路径：绝对路径直接用，否则依次在给定目录、工具目录、CWD 下找。"""
    if not p:
        return p
    if os.path.isabs(p) and os.path.exists(p):
        return p
    cands = [b for b in bases if b] + [TOOL_DIR, os.getcwd()]
    for b in cands:
        c = os.path.normpath(os.path.join(b, p))
        if os.path.exists(c):
            return c
    return p


def page_count(pdf_path: str) -> int:
    pikepdf = get_pikepdf()
    with pikepdf.open(pdf_path) as pdf:
        return len(pdf.pages)


def to_pdf_box(rect: List[float], page_w: float, page_h: float,
               origin: str, rotate: int) -> Tuple[float, float, float, float]:
    """把用户给的 [x,y,w,h] 转成 PDF 用户空间 (x1,y1,x2,y2)。"""
    x, y, w, h = (float(v) for v in rect)
    if w <= 0 or h <= 0:
        die(f"矩形宽高必须为正: {rect}")
    if origin == "top-left":
        y1 = page_h - y - h
    else:
        y1 = y
    x1, x2 = x, x + w
    y2 = y1 + h

    # 有 /Rotate 时，用户看到的"左上角"与 PDF 用户空间不一致，按顺时针旋转反推
    if rotate in (90, 270):
        warn(f"该页 /Rotate={rotate}，坐标按介质框原始方向解释；"
             f"请务必先用 --preview 核对位置")
    if rotate == 90:
        # 显示坐标系(宽=page_h, 高=page_w) -> 用户空间
        dx, dy, dw, dh = x, y, w, h
        x1 = dy
        y1 = dx
        x2 = x1 + dh
        y2 = y1 + dw
    elif rotate == 180:
        x1 = page_w - x - w
        y1 = y
        x2 = x1 + w
        y2 = y1 + h
    elif rotate == 270:
        dx, dy, dw, dh = x, y, w, h
        x1 = page_h - dy - dh
        y1 = page_w - dx - dw
        x2 = x1 + dh
        y2 = y1 + dw
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


# ----------------------------------------------------------------------------- 签名引擎
def load_signer(cert: Dict[str, Any]):
    from pyhanko.sign import signers
    pfx = cert.get("pfx") or cert.get("p12")
    if not pfx:
        die("证书配置缺少 pfx/p12 路径")
    if not os.path.exists(pfx):
        die(f"证书文件不存在: {pfx}")
    pw = cert.get("password", "")
    try:
        return signers.SimpleSigner.load_pkcs12(pfx, passphrase=pw.encode() if pw else None)
    except Exception as e:  # noqa: BLE001
        die(f"加载证书失败({pfx})：{e}\n  常见原因：密码错误 / 该文件不含私钥 / 是 USBKey 导出的公钥证书")


def build_stamp_style(sig: SigSpec, text: str, font_path: str):
    from pyhanko.stamp import TextStampStyle
    from pyhanko.pdf_utils.text import TextBoxStyle
    from pyhanko.pdf_utils.font.opentype import GlyphAccumulatorFactory
    from pyhanko.pdf_utils.layout import (
        AxisAlignment, Margins, InnerScaling, SimpleBoxLayoutRule,
    )

    rule = None
    if sig.text_layout == "bottom":
        # 文字压在框的下半部（与底图签名/印章错开）
        rule = SimpleBoxLayoutRule(
            x_align=AxisAlignment.ALIGN_MIN, y_align=AxisAlignment.ALIGN_MIN,
            margins=Margins(left=2, right=2, top=int(sig.rect[3] * 0.55), bottom=2),
            inner_content_scaling=InnerScaling.SHRINK_TO_FIT,
        )
    elif sig.text_layout == "top":
        rule = SimpleBoxLayoutRule(
            x_align=AxisAlignment.ALIGN_MIN, y_align=AxisAlignment.ALIGN_MAX,
            margins=Margins(left=2, right=2, top=2, bottom=int(sig.rect[3] * 0.55)),
            inner_content_scaling=InnerScaling.SHRINK_TO_FIT,
        )

    text_style = TextBoxStyle(
        font=GlyphAccumulatorFactory(ensure_ttf(font_path), font_size=sig.font_size),
        font_size=sig.font_size,
        box_layout_rule=rule,
    )
    bg = None
    if sig.image:
        if not os.path.exists(sig.image):
            die(f"章面图片不存在: {sig.image}")
        from pyhanko.pdf_utils.images import PdfImage
        bg = PdfImage(sig.image, writer=None)
        if sig.image_opacity is not None:
            bg.opacity = sig.image_opacity

    return TextStampStyle(
        stamp_text=text,
        text_box_style=text_style,
        border_width=1 if sig.border else 0,
        border_color=(0.6, 0.6, 0.6) if sig.border else None,
        background=bg,
        background_opacity=sig.image_opacity if sig.image_opacity is not None else 0.85,
    )


def sign_one(pdf_bytes: bytes, sig: SigSpec, style, signer,
             box: Tuple[float, float, float, float], page_idx: int) -> bytes:
    """在内存里对 pdf_bytes 追加一个增量签名，返回新字节。"""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko.sign.fields import SigFieldSpec

    w = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
    meta = signers.PdfSignatureMetadata(
        field_name=sig.name,
        reason=style_meta_reason(sig),
        location=(sig.cert or {}).get("location", "") or None,
        contact_info=(sig.cert or {}).get("contact", "") or None,
    )
    spec = SigFieldSpec(
        sig_field_name=sig.name,
        on_page=page_idx,
        box=box,
    )
    pdf_signer = signers.PdfSigner(
        meta, signer=signer, stamp_style=style, new_field_spec=spec
    )
    out = io.BytesIO()
    pdf_signer.sign_pdf(w, output=out)
    return out.getvalue()


def style_meta_reason(sig: SigSpec) -> str:
    return (sig.cert or {}).get("reason") or f"签名位置：{sig.name}"


def to_png(src: str, out_dir: Optional[str] = None) -> str:
    """把任意格式图片(jpg/jpeg/bmp/gif/tiff/webp)统一转成 PNG(RGBA) 再使用。

    out_dir 为空时，PNG 生成在原图同目录（同名 .png）。已存在且比源文件新则直接复用。
    """
    from PIL import Image

    if src.lower().endswith(".png") and Image.open(src).mode in ("RGBA", "LA", "P"):
        return src
    d = out_dir or os.path.dirname(os.path.abspath(src))
    os.makedirs(d, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(d, stem + ".png")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    im = Image.open(src)
    im.convert("RGBA").save(out, format="PNG")
    info(f"[转PNG] {os.path.basename(src)} -> {out}")
    return out


def _seal_rgba(sig: SigSpec):
    """读入章图 -> RGBA（统一按 PNG 处理），必要时把白底洗成透明。"""
    from PIL import Image

    if not sig.image:
        return None
    if not os.path.exists(sig.image):
        die(f"章图不存在: {sig.image}")
    if not sig.image.lower().endswith(".png"):
        sig.image = to_png(sig.image)
    im = Image.open(sig.image)
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA")
    auto_white = sig.remove_white
    if auto_white is None:
        auto_white = (not has_alpha) or sig.image.lower().endswith((".jpg", ".jpeg"))
    if auto_white:
        im = whiten_to_alpha(im, sig.white_threshold)
    return im


def whiten_to_alpha(im, threshold: int = 238):
    """把接近白色的像素变透明，边缘像素保留半透过渡，避免生硬锯齿。"""
    from PIL import Image

    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            lum = (r * 299 + g * 587 + b * 114) // 1000
            if lum >= threshold:
                px[x, y] = (r, g, b, 0)
            elif lum >= threshold - 22:  # 过渡带：越接近白越透明
                k = (threshold - lum) / 22.0
                px[x, y] = (r, g, b, int(a * k))
    return im


def _is_red_seal(im) -> bool:
    """判断是否红章类图（红色像素占比高）。"""
    if im is None:
        return False
    try:
        small = im.convert("RGBA").resize((60, 60))
        raw = small.tobytes()
        n = len(raw) // 4
        red = 0
        for i in range(n):
            r, g, b, a = raw[4 * i:4 * i + 4]
            if a > 40 and r > 120 and r - g > 50 and r - b > 50:
                red += 1
        return red / max(1, n) > 0.12
    except Exception:  # noqa: BLE001
        return False


def compose_stamp_png(sig: SigSpec, w_pt: float, h_pt: float, text: str,
                      font_path: str):
    """把章图 + 文字按框尺寸合成为一张透明 PNG（像素级排版，落图不失真）。"""
    from PIL import Image, ImageDraw, ImageFont

    S = sig.dpi / 72.0
    W, H = max(1, int(round(w_pt * S))), max(1, int(round(h_pt * S)))
    seal = _seal_rgba(sig)

    # 不透明度：默认 1.0（颜色饱满）。红章靠 bake-multiply 让底层内容透出，不必整体变淡；
    # 想更透可显式配置 opacity(0~1)。
    op = 1.0 if sig.opacity is None else float(sig.opacity)
    op = max(0.05, min(1.0, float(op)))

    def _apply_opacity(im):
        if op >= 0.999:
            return im
        a = im.getchannel("A").point(lambda v: int(v * op))
        im.putalpha(a)
        return im

    # 纯章图（无文字、整框铺满）：直接透传原图，避免二次重采样导致发糊
    if seal is not None and not text and sig.image_scale >= 0.999 \
            and sig.image_pos.lower() in ("center", "", "mid", "middle"):
        return _apply_opacity(seal.copy())

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if seal is not None:
        tw, th = max(1, int(W * max(0.05, min(1.0, sig.image_scale)))), \
                  max(1, int(H * max(0.05, min(1.0, sig.image_scale))))
        seal.thumbnail((tw, th), Image.LANCZOS)
        parts = sig.image_pos.lower().replace("_", "-").split("-")
        hx = next((t for t in parts if t in ("left", "right")), None)
        vy = next((t for t in parts if t in ("top", "bottom")), None)
        sx = 0 if hx == "left" else (W - seal.width if hx == "right" else (W - seal.width) // 2)
        sy = 0 if vy == "top" else (H - seal.height if vy == "bottom" else (H - seal.height) // 2)
        canvas.alpha_composite(seal, (int(sx), int(sy)))

    canvas = _apply_opacity(canvas)

    if text:
        fpx = max(6, int(round(sig.font_size * S)))
        font = ImageFont.truetype(ensure_ttf(font_path), fpx)
        draw = ImageDraw.Draw(canvas)
        lines = text.split("\n")
        lh = int(round((font.getbbox("字Ag")[3] - font.getbbox("字Ag")[1]) * 1.28)) or fpx
        block_h = lh * len(lines)
        if sig.text_layout == "top":
            y0 = 2
        elif sig.text_layout == "bottom":
            y0 = max(0, H - block_h - 2)
        else:
            y0 = max(0, (H - block_h) // 2)
        for i, ln in enumerate(lines):
            draw.text((3, y0 + i * lh), ln, font=font, fill=(20, 20, 20, 255))
    return canvas


def _page_resources(page):
    """取页面资源字典（可能继承自 Pages 节点）。"""
    pikepdf = get_pikepdf()
    node = page
    while node is not None:
        res = node.get("/Resources")
        if res is not None:
            if not isinstance(res, pikepdf.Dictionary):
                res = pikepdf.Dictionary()
                node[ pikepdf.Name("/Resources") ] = res
            return res
        node = node.get("/Parent")
    page[pikepdf.Name("/Resources")] = pikepdf.Dictionary()
    return page[pikepdf.Name("/Resources")]


def _pdf_image(pdf, pil):
    """把 PIL 图转成带软遮罩(透明通道)的 PDF 图像 XObject —— 一律以 PNG 原始像素(RGBA)导入。"""
    import zlib
    pikepdf = get_pikepdf()

    im = pil.convert("RGBA")
    w, h = im.size
    rgb = im.convert("RGB").tobytes()
    alpha = im.getchannel("A").tobytes()

    img = pikepdf.Stream(pdf, zlib.compress(rgb, 6))
    img.stream_dict.update({
        "/Type": pikepdf.Name("/XObject"), "/Subtype": pikepdf.Name("/Image"),
        "/Width": w, "/Height": h,
        "/ColorSpace": pikepdf.Name("/DeviceRGB"),
        "/BitsPerComponent": 8,
        "/Filter": pikepdf.Name("/FlateDecode"),
    })
    smask = pikepdf.Stream(pdf, zlib.compress(alpha, 6))
    smask.stream_dict.update({
        "/Type": pikepdf.Name("/XObject"), "/Subtype": pikepdf.Name("/Image"),
        "/Width": w, "/Height": h,
        "/ColorSpace": pikepdf.Name("/DeviceGray"),
        "/BitsPerComponent": 8,
        "/Filter": pikepdf.Name("/FlateDecode"),
    })
    img["/SMask"] = pdf.make_indirect(smask)
    return pdf.make_indirect(img)


def _multiply_blend(base_rgb, seal_rgba):
    """正片叠底(base × seal)，透明度沿用章图 alpha —— 底层内容透出且颜色正确。"""
    import numpy as np
    from PIL import Image

    b = np.asarray(base_rgb.convert("RGB"), dtype=np.float32)
    s = np.asarray(seal_rgba.convert("RGB"), dtype=np.float32)
    out = (b * s) / 255.0
    res = Image.fromarray(out.clip(0, 255).astype("uint8"), "RGB").convert("RGBA")
    res.putalpha(seal_rgba.getchannel("A"))
    return res


def stamp_visual_batch(pdf_bytes: bytes, items: List[Tuple[SigSpec, str, Tuple[float, float, float, float]]],
                       font_path: str) -> bytes:
    """把全部"视觉章"贴进 PDF。

    - 章图统一以 PNG(RGBA) 像素导入（内部一律 PNG，WPS/Acrobat/PDFium 效果一致）
    - blend='bake-multiply'（红章默认）：把章与底层内容做正片叠底后烘焙成透明 PNG，
      因此**红章不会遮住底下的原始文字/表格线**，且在 MuPDF/WPS/浏览器里表现完全一致
      （PDF 的 /BM /Multiply 混合模式在部分渲染器里不生效，故用烘焙法保证效果）
    - blend='normal'：普通覆盖
    - blend='multiply'：仅写 PDF 混合模式（依赖查看器支持）
    """
    if not items:
        return pdf_bytes
    pikepdf = get_pikepdf()
    fitz = get_fitz()
    Image = get_pil()

    # ---- 顺序保障：手写签名先贴，红章后贴 ----
    # 红章走正片叠底的烘焙流程，烘焙时只"看见"已经贴上的内容；
    # 若红章排在签名之前，签名会被烘焙成纯红一片。故此处按（签名, 原顺序）稳定排序。
    prepared = []
    for sig, text, box in items:
        w0, h0 = box[2] - box[0], box[3] - box[1]
        png0 = compose_stamp_png(sig, w0, h0, text, font_path)
        mode0 = (sig.blend or "auto").lower()
        if mode0 == "auto":
            mode0 = "bake-multiply" if _is_red_seal(png0) else "normal"
        prepared.append((sig, text, box, png0, mode0 == "bake-multiply"))
    prepared.sort(key=lambda t: t[4])      # False(签名) 在前，红章在后
    items = [(s, t, b) for (s, t, b, _png, _red) in prepared]
    _png_cache = {(id(s), b): png for (s, _t, b, png, _r) in prepared}
    _mode_cache = {(id(s), b): m for (s, _t, b, _p, _rr) in prepared
                   for m in [("bake-multiply" if _rr else ("multiply" if (s.blend or "").lower() == "multiply" else "normal"))]}

    # fitz 负责渲染底图（顺序叠加，保证后盖的章看到前面的章），pikepdf 负责最终写入
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    baked = []   # [(sig, box, PIL image)]

    for idx, (sig, text, box) in enumerate(items):
        page = doc[sig.page - 1]
        x1, y1, x2, y2 = box                      # PDF 坐标(原点左下)
        w, h = x2 - x1, y2 - y1
        ph = page.mediabox.height
        if int(page.rotation or 0) % 360:
            warn(f"{sig.name}: 该页有旋转 /Rotate={page.rotation}，贴章位置请用 --preview 复核")
        frect = fitz.Rect(x1, ph - y2, x2, ph - y1)   # fitz 用左上角原点
        png = _png_cache.get((id(sig), box)) or compose_stamp_png(sig, w, h, text, font_path)
        mode = _mode_cache.get((id(sig), box), "normal")

        if mode == "bake-multiply":
            dpi = max(150, int(sig.dpi))
            pix = page.get_pixmap(dpi=dpi, clip=frect, alpha=False)
            base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            seal = png.convert("RGBA").resize((pix.width, pix.height), Image.LANCZOS)
            merged = _multiply_blend(base, seal)
            baked.append((sig, box, merged))
            buf = io.BytesIO()
            merged.save(buf, format="PNG")
            doc[sig.page - 1].insert_image(frect, stream=buf.getvalue(),
                                           overlay=True, keep_proportion=False)
        else:
            baked.append((sig, box, png, mode))
            buf = io.BytesIO()
            png.save(buf, format="PNG")
            doc[sig.page - 1].insert_image(frect, stream=buf.getvalue(),
                                           overlay=True, keep_proportion=False)
    doc.close()

    # ---- 用 pikepdf 正式写入（图像一律 PNG 像素 + 软遮罩）
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    for idx, entry in enumerate(baked):
        sig, box = entry[0], entry[1]
        img_pil = entry[2]
        mode = entry[3] if len(entry) > 3 else "normal"
        page = pdf.pages[sig.page - 1]
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        res = _page_resources(page)
        if "/XObject" not in res:
            res["/XObject"] = pdf.make_indirect(pikepdf.Dictionary())
        if "/ExtGState" not in res:
            res["/ExtGState"] = pdf.make_indirect(pikepdf.Dictionary())
        img_name = f"/MsigImg{idx}"
        res["/XObject"][img_name] = _pdf_image(pdf, img_pil)
        gs_name = f"/MsigGS{idx}"
        bm = "multiply" if mode == "multiply" else "normal"
        res["/ExtGState"][gs_name] = pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/ExtGState"),
            "/BM": pikepdf.Name("/" + bm),
        }))
        content = (f"q {gs_name} gs {w:.4f} 0 0 {h:.4f} {x1:.4f} {y1:.4f} cm "
                   f"{img_name} Do Q\n").encode()
        page.contents_add(pikepdf.Stream(pdf, content))

    out = io.BytesIO()
    pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    pdf.close()
    return out.getvalue()


# ----------------------------------------------------------------------------- 预览
def make_preview(pdf_path: str, job: Job, out_png: str):
    fitz = get_fitz()

    doc = fitz.open(pdf_path)
    font_path = pick_font(job.font)
    ctx = dict(date=datetime.now().strftime("%Y-%m-%d"),
               time=datetime.now().strftime("%H:%M"),
               datetime=datetime.now().strftime("%Y-%m-%d %H:%M"),
               file=os.path.basename(pdf_path))
    for s in job.signatures:
        if s.page > len(doc):
            warn(f"{s.name}: 页码 {s.page} 超出，跳过预览")
            continue
        page = doc[s.page - 1]
        pw, ph, rot = page_geometry(pdf_path, s.page)
        box = to_pdf_box(s.rect, pw, ph, job.origin, rot)
        prect = fitz.Rect(box[0], ph - box[3], box[2], ph - box[1])
        page.draw_rect(prect, color=(1, 0, 0), width=1.2)
        txt = render_text(s.text or s.name, dict(ctx, page=str(s.page), name=s.name))
        if txt:
            page.insert_textbox(prect, txt, fontsize=s.font_size, fontfile=font_path,
                                fontname=f"PV{s.name}", color=(0, 0.35, 0.85))
        page.insert_text((prect.x0, prect.y0 - 3), s.name, fontsize=7,
                         fontfile=font_path, fontname=f"LB{s.name}", color=(1, 0, 0))
    pages_png = []
    for i in range(len(doc)):
        p = doc[i].get_pixmap(dpi=110)
        fp = f"{out_png}.p{i + 1}.png"
        p.save(fp)
        pages_png.append(fp)
    doc.close()
    # 拼成一张纵向长图，方便一次看完
    if len(pages_png) > 1:
        Image = get_pil()
        imgs = [Image.open(p) for p in pages_png]
        W = max(i.width for i in imgs)
        H = sum(i.height for i in imgs)
        canvas = Image.new("RGB", (W, H), "white")
        y = 0
        for im in imgs:
            canvas.paste(im, (0, y))
            y += im.height
        canvas.save(out_png)
        for p in pages_png:
            os.remove(p)
    else:
        shutil.move(pages_png[0], out_png)
    info(f"[预览] 已生成 {out_png}（红框=签名位置，蓝字=签名内容，仅供核对，未修改原PDF）")
    return out_png


# ----------------------------------------------------------------------------- 主流程
def run_job(job: Job, preview_only: bool = False, preview_png: str = "preview.png"):
    if not os.path.exists(job.input):
        die(f"输入 PDF 不存在: {job.input}")
    if not job.signatures:
        die("配置里没有任何签名位置(signatures 为空)")

    if preview_only:
        return make_preview(job.input, job, preview_png)

    font_path = pick_font(job.font)
    ctx = dict(date=datetime.now().strftime("%Y-%m-%d"),
               time=datetime.now().strftime("%H:%M"),
               datetime=datetime.now().strftime("%Y-%m-%d %H:%M"),
               file=os.path.basename(job.input))

    with open(job.input, "rb") as f:
        buf = f.read()
    original = buf

    npages = page_count(job.input)
    used_names: set = set()
    skipped = 0
    visual_items: List[Tuple[SigSpec, str, Tuple[float, float, float, float]]] = []
    crypto_items: List[Tuple[SigSpec, str, Tuple[float, float, float, float], Dict[str, Any]]] = []

    for i, sig in enumerate(job.signatures, 1):
        if sig.name in used_names:
            die(f"签名域名称重复: {sig.name}（每处必须唯一）")
        used_names.add(sig.name)

        if sig.page < 1 or sig.page > npages:
            warn(f"{sig.name}: 页码 {sig.page} 超出(共 {npages} 页)，已跳过")
            skipped += 1
            continue

        pw, ph, rot = page_geometry(job.input, sig.page)
        box = to_pdf_box(sig.rect, pw, ph, job.origin, rot)
        text = render_text(sig.text, dict(ctx, page=str(sig.page), name=sig.name))
        cert = sig.cert or job.default_cert

        if sig.visual_only or not cert:
            if not sig.visual_only and not cert:
                pass  # 无证书=纯图片电子签章模式，正常
            mode = "帖章(图片)" if sig.image else "文字章"
            visual_items.append((sig, text, box))
        else:
            mode = "数字签名"
            crypto_items.append((sig, text, box, cert))
        info(f"[{i}/{len(job.signatures)}] {sig.name}  p{sig.page} "
             f"box={tuple(round(v, 1) for v in box)}  内容={text!r}  -> {mode}")

    # 第一阶段：所有图片/文字章一次贴完（fitz 重写一次）
    if visual_items:
        buf = stamp_visual_batch(buf, visual_items, font_path)
        info(f"[贴章] 完成 {len(visual_items)} 处")

    # 第二阶段：证书数字签名逐次增量追加（必须放在贴章之后）
    for sig, text, box, cert in crypto_items:
        signer = load_signer(cert)
        sig.cert = cert
        style = build_stamp_style(sig, text, font_path)
        buf = sign_one(buf, sig, style, signer, box, sig.page - 1)

    out_path = job.output
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(buf)
    if skipped:
        warn(f"共跳过 {skipped} 处（页码超出）。若日报页数变少，请更新配置")
    info(f"[完成] 输出: {out_path}  ({len(original)} -> {len(buf)} 字节)")
    return out_path


# ----------------------------------------------------------------------------- 校验/列出
def cmd_list(pdf_path: str):
    from pyhanko.pdf_utils.reader import PdfFileReader
    with open(pdf_path, "rb") as f:
        r = PdfFileReader(f)
        sigs = r.embedded_signatures
        if not sigs:
            info("该 PDF 没有任何数字签名。")
            return
        for i, s in enumerate(sigs, 1):
            info(f"{i}. 签名域 {s.field_name} | 签名人 {s.signer_cert.subject.human_friendly} "
                 f"| 算法 {s.sig_object.get('/SubFilter')}")


def cmd_verify(pdf_path: str):
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    with open(pdf_path, "rb") as f:
        r = PdfFileReader(f)
        sigs = r.embedded_signatures
        if not sigs:
            info("该 PDF 没有任何数字签名。")
            return 1
        ok_all = True
        for s in sigs:
            try:
                # 自签/私有 CA 场景：用签名内嵌证书自身作为信任锚
                vc = ValidationContext(trust_roots=[s.signer_cert],
                                       allow_fetching=False,
                                       revocation_mode="soft-fail")
                st = validate_pdf_signature(s, vc)
                info(f"- {s.field_name}: 完整性={'通过' if st.intact else '失败'} "
                     f"可信={'是' if st.trusted else '否'} "
                     f"覆盖全文档={'是' if st.coverage == 'entire_file' else st.coverage} "
                     f"| 签名人={s.signer_cert.subject.human_friendly}")
                ok_all &= bool(st.intact)
            except Exception as e:  # noqa: BLE001
                info(f"- {s.field_name}: 校验异常 {e}")
                ok_all = False
        return 0 if ok_all else 1


# ----------------------------------------------------------------------------- 配置模板
def write_template(path: str, input_pdf: str):
    tpl = {
        "input": input_pdf,
        "output": input_pdf.rsplit(".", 1)[0] + "_已签名.pdf",
        "origin": "top-left",
        "font": "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "default_cert": {"pfx": "cert/my.p12", "password": "证书密码",
                         "location": "杭州", "reason": "基坑监测日报签发"},
        "signatures": [
            {"name": "jianceren", "page": 1, "rect": [330, 700, 170, 44],
             "text": "监测负责人：张三\n{date}", "font_size": 10, "border": False},
            {"name": "shenheyuan", "page": 1, "rect": [330, 640, 170, 44],
             "text": "审  核：________\n{date}", "font_size": 10, "border": False},
            {"name": "xiangmujingli", "page": 2, "rect": [330, 700, 170, 44],
             "text": "项目经理：________\n{date}", "font_size": 10, "border": False}
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    info(f"[模板] 已写入 {path}，按需改 rect/text/page 后运行：\n"
         f"  python multisign.py -c {path} --preview preview.png\n"
         f"  python multisign.py -c {path}")
    return path


SIG_FIELDS = set(SigSpec.__dataclass_fields__.keys())


def _sig_from_dict(d: Dict[str, Any]) -> SigSpec:
    """从 JSON 构造 SigSpec，忽略未知键（如注释用的"备注"），放进 extras。"""
    known = {k: v for k, v in d.items() if k in SIG_FIELDS}
    extra = {k: v for k, v in d.items() if k not in SIG_FIELDS}
    sig = SigSpec(**known)
    sig.extras = extra
    return sig


def parse_at(spec: str) -> SigSpec:
    """"page,x,y,w,h=内容[|字段名]" 形式"""
    if "=" not in spec:
        die(f"--at 格式错误: {spec}")
    pos, content = spec.split("=", 1)
    field_name = None
    if "|" in content:
        content, field_name = content.split("|", 1)
    parts = [p.strip() for p in pos.split(",")]
    if len(parts) != 5:
        die(f"--at 坐标应为 5 个数(page,x,y,w,h): {spec}")
    page, x, y, w, h = (float(p) for p in parts)
    name = field_name or f"sig{int(page)}_{int(x)}_{int(y)}"
    return SigSpec(name=name, page=int(page), rect=[x, y, w, h], text=content)


def main(argv: Optional[List[str]] = None):
    ap = argparse.ArgumentParser(
        description="PDF 多处不同内容批量数字签名工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("-c", "--config", help="作业配置 JSON")
    ap.add_argument("--init", metavar="OUT_JSON", help="生成配置模板")
    ap.add_argument("--input", help="输入 PDF(不走配置时使用)")
    ap.add_argument("--output", help="输出 PDF")
    ap.add_argument("-p", "--pfx", help="默认证书 pfx/p12，格式 路径[:密码]")
    ap.add_argument("--at", action="append", default=[],
                    help='签名位置 "page,x,y,w,h=内容[|字段名]"，可重复')
    ap.add_argument("--font", help="中文字体文件")
    ap.add_argument("--origin", choices=["top-left", "bottom-left"], help="坐标系原点")
    ap.add_argument("--preview", metavar="PNG", help="只生成占位预览图")
    ap.add_argument("--list", metavar="PDF", help="列出 PDF 中的签名域")
    ap.add_argument("--verify", metavar="PDF", help="校验 PDF 中的签名")
    ap.add_argument("--to-png", nargs="+", metavar="图/目录",
                    help="把签名/印章图片批量统一转换成 PNG（可给多个文件或目录）")
    ap.add_argument("--learn", metavar="签章后PDF",
                    help="从一份已签章的文件里学习每处章图+精确位置，生成 job.json")
    ap.add_argument("--base", help="--learn 时的未签章原文件(用于出 job 的 input)")
    ap.add_argument("-o", "--outdir", default="learn_out", help="--learn 输出目录")
    args = ap.parse_args(argv)

    if args.to_png:
        files = []
        for it in args.to_png:
            if os.path.isdir(it):
                files += [os.path.join(it, f) for f in sorted(os.listdir(it))
                          if f.lower().endswith((".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".png"))]
            elif os.path.exists(it):
                files.append(it)
            else:
                die(f"路径不存在: {it}")
        if not files:
            die("没有找到可转换的图片")
        done = []
        for f in files:
            try:
                done.append(to_png(f, args.outdir if args.outdir != "learn_out" else None))
            except Exception as e:  # noqa: BLE001
                warn(f"{f}: 转换失败 {e}")
        for d in done:
            info(f"  PNG: {d}")
        info(f"[完成] 共 {len(done)} 张已统一为 PNG")
        return

    if args.learn:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
        from extract_annots import main as learn_main
        return learn_main(args.learn, args.outdir, args.base)

    if args.list:
        return cmd_list(args.list)
    if args.verify:
        sys.exit(cmd_verify(args.verify))

    if args.init:
        if not args.input:
            die("--init 需要同时给 --input <示例PDF>")
        return write_template(args.init, args.input)

    if args.config:
        with open(args.config, encoding="utf-8") as f:
            raw = json.load(f)
        job = Job(
            input=raw["input"], output=raw.get("output") or raw["input"].replace(".pdf", "_已签名.pdf"),
            origin=raw.get("origin", "top-left"), font=raw.get("font"),
            default_cert=raw.get("default_cert"),
            signatures=[_sig_from_dict(s) for s in raw.get("signatures", [])],
        )
        base_dir = os.path.dirname(os.path.abspath(args.config))
        job.input = resolve_asset(job.input, base_dir)
        if job.font:
            job.font = resolve_asset(job.font, base_dir)
        if job.default_cert and job.default_cert.get("pfx"):
            job.default_cert["pfx"] = resolve_asset(job.default_cert["pfx"], base_dir)
        for s in job.signatures:
            if s.image:
                s.image = resolve_asset(s.image, base_dir)
            if s.cert and s.cert.get("pfx"):
                s.cert["pfx"] = resolve_asset(s.cert["pfx"], base_dir)
        if args.input:
            job.input = args.input
        if args.output:
            job.output = args.output
    else:
        if not args.input or not args.output:
            ap.print_help()
            die("给 --input/--output/--at，或用 -c 配置文件")
        if not args.at:
            die("至少给一个 --at 签名位置")
        cert = None
        if args.pfx:
            pfx, _, pw = args.pfx.partition(":")
            cert = {"pfx": pfx, "password": pw}
        job = Job(input=args.input, output=args.output,
                  origin=args.origin or "top-left", font=args.font,
                  default_cert=cert, signatures=[parse_at(s) for s in args.at])

    run_job(job, preview_only=bool(args.preview),
            preview_png=args.preview or "preview.png")


if __name__ == "__main__":
    main()
