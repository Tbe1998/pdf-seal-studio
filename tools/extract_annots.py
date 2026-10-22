#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从"签章后"的 PDF 提取电子签章：图片 + 精确位置尺寸 + 反推 job.json

WPS/数科(Suwell) 电子签章的形态：
  签名字段(Widget) -> /AP /N 外观流 -> 内嵌 Form -> `w 0 0 h tx ty cm /Im0 Do`
所以"章图的实际占位"必须按 cm 变换算，不能用字段 /Rect（Rect 比图大一圈，
直接铺满会把章放大约 15%）。本脚本按 cm 精确换算。

用法:
  python extract_annots.py 签章后.pdf 输出目录 [原文件.pdf(用于顶部坐标与job输入)]
"""
import json
import os
import re
import sys

import pikepdf

NUM = re.compile(rb"^[-\d.]+$")


def mul(m1, m2):
    """2D 仿射矩阵相乘 [a b c d e f]。"""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2]


def transform_rect(m, x0, y0, x1, y1):
    a, b, c, d, e, f = m
    pts = [(a * x + c * y + e, b * x + d * y + f) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def parse_placements(content: bytes):
    """解析内容流里 `.. cm /Name Do`，返回 {Name: [a,b,c,d,e,f]}。"""
    toks = content.replace(b"\n", b" ").replace(b"\r", b" ").split()
    out, nums, cur = {}, [], None
    for i, t in enumerate(toks):
        if NUM.match(t):
            nums.append(t)
            continue
        if t == b"cm" and len(nums) >= 6:
            cur = [float(x) for x in nums[-6:]]
        elif t == b"q":
            cur = cur  # 简化：不追踪 q/Q 栈深度（签章场景通常只有一层）
        elif t == b"Do" and i > 0 and toks[i - 1].startswith(b"/"):
            nm = toks[i - 1][1:].decode("latin-1")
            out[nm] = list(cur) if cur else [1, 0, 0, 1, 0, 0]
    return out


def collect(form, ctm, depth, acc):
    """递归收集 (图片对象, 屏幕矩形(相对字段Rect)), 精确按 cm 计算。"""
    if not hasattr(form, "keys") or depth > 6:
        return
    if "/Matrix" in form:
        ctm = mul(ctm, [float(x) for x in form["/Matrix"]])
    try:
        content = form.read_bytes()
    except Exception:  # noqa: BLE001
        content = b""
    places = parse_placements(content)
    xobj = (form.get("/Resources") or {}).get("/XObject") or {}
    for name, sub in dict(xobj).items():
        nm = str(name).lstrip("/")
        local = places.get(nm, [1, 0, 0, 1, 0, 0])
        m = mul(ctm, local)
        st = sub.get("/Subtype")
        if st == "/Image":
            # PDF 图像 XObject 在用户空间里画的是单位正方形 0..1，尺寸由 cm 决定
            acc.append((sub, transform_rect(m, 0, 0, 1, 1), [round(v, 3) for v in m]))
        elif st == "/Form":
            bb = [float(x) for x in sub.get("/BBox", [0, 0, 1, 1])]
            sub_ctm = mul(ctm, local)
            # Form 内的坐标以 BBox 左下为基准
            inner = collect(sub, mul(sub_ctm, [1, 0, 0, 1, bb[0], bb[1]]), depth + 1, acc)
    return acc


def main(signed_pdf, outdir, base_pdf=None):
    os.makedirs(os.path.join(outdir, "seal_img"), exist_ok=True)
    pdf = pikepdf.open(signed_pdf)
    result = {"pages": len(pdf.pages), "seals": []}

    for pno, page in enumerate(pdf.pages, 1):
        box = [float(x) for x in page.mediabox]
        ph = abs(box[3] - box[1])
        for a in page.get("/Annots", []):
            field = str(a.get("/T") or "Sig").lstrip("/")
            R = [float(x) for x in a.get("/Rect", [])]
            if len(R) != 4:
                continue
            N = (a.get("/AP") or {}).get("/N")
            acc = collect(N, [1, 0, 0, 1, 0, 0], 0, []) if hasattr(N, "keys") else []
            if not acc:
                acc = [(None, (0, 0, R[2] - R[0], R[3] - R[1]), None)]
            img, (ix0, iy0, ix1, iy1), mtx = acc[0]
            # 图片在页面上的实际矩形（PDF 坐标，原点左下）
            px0, py0 = R[0] + ix0, R[1] + iy0
            pw, phh = ix1 - ix0, iy1 - iy0
            entry = {
                "field": field, "page": pno,
                "field_rect_pdf": [round(v, 2) for v in R],
                "image_rect_pdf": [round(px0, 2), round(py0, 2), round(pw, 2), round(phh, 2)],
                "image_rect_top_left": [round(px0, 2), round(ph - py0 - phh, 2), round(pw, 2), round(phh, 2)],
                "cm": mtx, "images": [],
                "signed": bool(a.get("/V") is not None and "/ByteRange" in (a.get("/V") or {})),
            }
            for idx, (sub, rect_, _m) in enumerate(acc, 1):
                if sub is None:
                    continue
                fn = f"{field}_{idx}.png" if idx > 1 else f"{field}.png"
                try:
                    pil = pikepdf.PdfImage(sub).as_pil_image()
                    pil.save(os.path.join(outdir, "seal_img", fn))
                    entry["images"].append({"file": f"seal_img/{fn}", "px": [pil.width, pil.height]})
                except Exception as e:  # noqa: BLE001
                    entry["images"].append({"file": None, "error": str(e)})
            result["seals"].append(entry)

    sigs = []
    for s in result["seals"]:
        if not s["images"] or not s["images"][0].get("file"):
            continue
        sigs.append({
            "name": s["field"], "page": s["page"],
            "rect": s["image_rect_top_left"],
            "image": os.path.abspath(os.path.join(outdir, s["images"][0]["file"])),
            "text": "", "visual_only": True,
        })
    job = {
        "input": os.path.abspath(base_pdf) if base_pdf else "原文件.pdf",
        "output": os.path.abspath(os.path.join(outdir, "自动签章结果.pdf")),
        "origin": "top-left",
        "signatures": sigs,
    }
    with open(os.path.join(outdir, "job.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, "diff.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"页数 {result['pages']}，签章 {len(result['seals'])} 处（按 cm 精确换算）")
    for s in result["seals"]:
        im = s["images"][0] if s["images"] else {}
        print(f"  p{s['page']} {s['field']:<7} 字段框{s['field_rect_pdf'][2]:.1f}x{s['field_rect_pdf'][3]:.1f}"
              f" -> 图实际 {s['image_rect_top_left'][2]:.1f}x{s['image_rect_top_left'][3]:.1f}pt"
              f" @({s['image_rect_top_left'][0]:.1f},{s['image_rect_top_left'][1]:.1f})"
              f" 图={im.get('px')} 已签名={s['signed']}")
    print(f"\n输出: {outdir}/seal_img/*.png  {outdir}/job.json  {outdir}/diff.json")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
