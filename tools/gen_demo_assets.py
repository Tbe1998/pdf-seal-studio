#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成完全虚构的演示素材（不含任何真实签名/公章）：
  assets/seals/示例签名_甲.png     浅蓝手写体示意（明确标注“演示用”）
  assets/seals/示例签名_乙.png
  assets/seals/示例项目章.png      虚构图章：示例工程项目部（仅供演示）
  examples/示例监测日报.pdf        3 页示例表格，带签章锚点文字
用法：
  python tools/gen_demo_assets.py
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEAL_DIR = os.path.join(ROOT, "assets", "seals")
EX_DIR = os.path.join(ROOT, "examples")


def _font(size):
    for p in (FONT, "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "C:/Windows/Fonts/msyh.ttc"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_signature(text: str, color=(28, 56, 140), size=None, out="sig.png"):
    """画一个“手写观感”的示意签名（虚构内容），右下角标注演示用途。"""
    f = _font(150)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    tw = probe.textbbox((0, 0), text, font=f)[2]
    th = probe.textbbox((0, 0), text, font=f)[3]
    if size is None:
        size = (int(tw * 1.35) + 80, int(th * 1.9) + 60)
    img = Image.new("RGBA", size, (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    # 基线轻微倾斜，模拟手写
    layer = Image.new("RGBA", size, (255, 255, 255, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((size[0] * 0.06, size[1] * 0.10), text, font=f, fill=color + (255,))
    layer = layer.rotate(-4, resample=Image.BICUBIC, expand=False)
    img.alpha_composite(layer)
    # 手写感装饰线
    d.line([(size[0] * 0.08, size[1] * 0.78), (size[0] * 0.70, size[1] * 0.71)],
           fill=color + (200,), width=max(3, size[1] // 55), joint="curve")
    # 演示标注（缩得很小，避免被误认为真签名）
    d.text((size[0] * 0.72, size[1] * 0.80), "演示用", font=_font(int(size[1] * 0.13)),
           fill=(160, 160, 160, 230))
    img.save(out)
    return out


def make_round_seal(line1="示例工程项目部", line2="仅供演示", out="seal.png", size=760):
    """画一个虚构的圆形红章（明确写“仅供演示”）。"""
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    red = (206, 32, 39)
    pad = int(size * 0.045)
    d.ellipse([pad, pad, size - pad, size - pad], outline=red, width=max(4, size // 42))
    # 环绕文字（逐字旋转）
    f = _font(int(size * 0.115))
    cx, cy = size / 2, size / 2
    radius = size * 0.375
    n = len(line1)
    span = 250.0          # 环绕角度范围
    start = -90 - span / 2
    for i, ch in enumerate(line1):
        ang = start + span * (i / max(1, n - 1))
        rad = math.radians(ang)
        tw, th = d.textbbox((0, 0), ch, font=f)[2:]
        glyph = Image.new("RGBA", (tw + 8, th + 8), (255, 255, 255, 0))
        ImageDraw.Draw(glyph).text((4, 4), ch, font=f, fill=red + (255,))
        glyph = glyph.rotate(-(ang + 90), resample=Image.BICUBIC, expand=True)
        gx = cx + radius * math.cos(rad) - glyph.width / 2
        gy = cy + radius * math.sin(rad) - glyph.height / 2
        img.alpha_composite(glyph, (int(gx), int(gy)))
    # 五角星
    r1, r2 = size * 0.115, size * 0.048
    pts = []
    for k in range(10):
        r = r1 if k % 2 == 0 else r2
        a = math.radians(-90 + 36 * k)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill=red)
    # 下部横排小字
    f2 = _font(int(size * 0.085))
    tw = d.textbbox((0, 0), line2, font=f2)[2]
    d.text((cx - tw / 2, cy + size * 0.20), line2, font=f2, fill=red)
    img.save(out)
    return out


def make_demo_pdf(out_path: str):
    """生成 3 页虚构的“施工监测日报（示例）”PDF，带签章锚点。"""
    import pymupdf

    doc = pymupdf.open()
    font_name = "wqy"
    rows = [
        ("监测人", ""), ("复核", ""), ("技术负责人", ""),
    ]

    anchors = []   # [(页码, 标签, Rect)]

    def page(title, lines, sig_rows=(), seal_anchor=None):
        p = doc.new_page(width=595, height=842)
        p.insert_font(fontname=font_name, fontfile=FONT)
        p.insert_text((60, 90), title, fontsize=20, fontname=font_name, color=(0, 0, 0))
        p.insert_text((430, 90), "（示例文档 · 内容均为虚构）", fontsize=8, fontname=font_name,
                      color=(0.6, 0.6, 0.6))
        y = 130
        for ln in lines:
            p.insert_text((60, y), ln, fontsize=11, fontname=font_name)
            y += 22
        yy = 620
        for label, ph in sig_rows:
            p.insert_text((60, yy), f"{label}：{ph}", fontsize=12, fontname=font_name)
            yy += 34
        if seal_anchor:
            p.insert_text((60, yy + 10), seal_anchor, fontsize=12, fontname=font_name)
            p.insert_text((60, yy + 34), "2026年1月1日", fontsize=12, fontname=font_name)
        # 记录锚点真实坐标，供生成示例 job 用
        pno = len(doc) - 1
        for label, _ in sig_rows:
            hits = p.search_for(label + "：")
            if hits:
                anchors.append((pno, label, hits[0]))
        if seal_anchor:
            hits = p.search_for("单位名称：") or p.search_for("监测单位：")
            if hits:
                anchors.append((pno, "__seal__", hits[0]))
        return p

    page("施工监测日报（示例 · 第1页 封面）",
         ["工程名称：示例工程（虚构）", "标段：示例标段", "报告编号：DEMO-0001", "",
          "本文件由 tools/gen_demo_assets.py 自动生成，", "仅用于演示 PDF 多位置签章功能，内容全部虚构。"],
         sig_rows=(("项目负责人", ""), ("审核人", "")),
         seal_anchor="单位名称：示例工程有限公司（虚构）")

    page("施工监测日报（示例 · 第2页 数据）",
         ["测点编号    本次值    累计变化    变化速率    备注",
          "DEMO-01     12.3      45.6        0.8         正常",
          "DEMO-02     11.8      38.2        0.6         正常",
          "DEMO-03     13.1      51.4        0.9         正常", "",
          "以上数据均为随机生成的演示数值。"])

    page("施工监测日报（示例 · 第3页 结论）",
         ["结论：示例数据未超预警值。", "建议：继续按原频次观测。", "",
          "说明：本页同样留出签章位，用于演示多页多位置盖章。"],
         sig_rows=(("监测员", ""), ("复核", ""), ("技术负责人", "")),
         seal_anchor="监测单位：示例工程有限公司（虚构）")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        doc.subset_fonts()          # 只保留用到的字形，避免 10MB+ 的整字体嵌入
    except Exception:
        pass
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    return out_path, anchors


def make_demo_job(pdf_path: str, anchors, out_json: str):
    """根据锚点坐标生成与该示例 PDF 完全对应的签章配置。"""
    import json

    sig_a, sig_b, seal = "示例签名_甲", "示例签名_乙", "示例项目章"
    seq = [sig_a, sig_b]          # 同页多个“签名处”按顺序轮流用两个虚构签名
    k = 0
    sigs = []
    for pno, label, r in anchors:
        if label == "__seal__":
            # 项目章故意压住单位名称行，用于演示“红章压住文字/签字仍可读”
            sigs.append({"name": f"{seal}_{pno + 1}", "page": pno + 1,
                         "rect": [round(r.x1 + 6, 1), round(r.y0 - 34, 1), 102.0, 105.0],
                         "image": "assets/seals/示例项目章.png", "blend": "auto"})
        else:
            name = seq[k % len(seq)]
            k += 1
            # 签名域名称必须全局唯一，故带页码后缀
            sigs.append({"name": f"{name}_{pno + 1}_{label}", "page": pno + 1,
                         "rect": [round(r.x1 + 24, 1), round(r.y0 - 8, 1), 85.0, 29.0],
                         "image": f"assets/seals/{name}.png", "blend": "auto"})
    job = {
        "_说明": "示例签章配置（内容全部虚构）。rect=[x,y,宽,高] 单位 pt，origin=top-left 为页面左上角原点。",
        "input": os.path.relpath(pdf_path, ROOT).replace("\\", "/"),
        "output": "out/示例监测日报_已签章.pdf",
        "dpi": 300,
        "origin": "top-left",
        "signatures": sigs,
    }
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    return out_json


def main():
    os.makedirs(SEAL_DIR, exist_ok=True)
    os.makedirs(EX_DIR, exist_ok=True)
    print(make_signature("示例签名甲", color=(28, 56, 140), out=os.path.join(SEAL_DIR, "示例签名_甲.png")))
    print(make_signature("示例签名乙", color=(20, 20, 20), out=os.path.join(SEAL_DIR, "示例签名_乙.png")))
    print(make_round_seal(out=os.path.join(SEAL_DIR, "示例项目章.png")))
    pdf_path, anchors = make_demo_pdf(os.path.join(EX_DIR, "示例监测日报.pdf"))
    print(pdf_path)
    print(make_demo_job(pdf_path, anchors, os.path.join(EX_DIR, "示例签章.json")))


if __name__ == "__main__":
    main()
