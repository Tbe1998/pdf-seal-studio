#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SignStudio —— PDF 电子签章可视化工具（本地网页版窗口）

功能
  1. 导入 PNG 电子签名/印章，设置默认尺寸(mm)
  2. 导入 PDF 并预览（翻页、缩放）
  3. 在预览页面上框选位置 → 指定用哪个签名 → 可拖动/缩放/删除
  4. 一键导出签好章的 PDF（红章自动正片叠底，不遮挡底层内容）

运行
  python signstudio.py            # 自动打开浏览器 http://127.0.0.1:8765
  python signstudio.py --port 9000 --no-browser

依赖：pymupdf、pikepdf、Pillow（已在 requirements.txt 里）
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import mimetypes
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import multisign as ms  # 复用已打磨好的贴章/正片叠底/字体逻辑
import version as vmod  # 版本号唯一来源
import updater as up    # 自动更新内核

DOCS: "OrderedDict[str, dict]" = OrderedDict()   # doc_id -> {bytes, name, pages, npages}
MAX_DOCS = 40                # 同时在内存里保留的 PDF 数（批量盖章一次要导入多份）
SIGS: dict[str, dict] = {}   # sig_id -> {path, w, h, name, shared}
TMP = os.path.join(HERE, ".studio_tmp")
PROJ = os.path.join(HERE, "projects")

# 局域网共享：签名图与工程都落在本机磁盘（shared/seals、projects/），客户端只通过 HTTP 读写
SHARE = os.path.join(HERE, "shared")
SEAL_DIR = os.path.join(SHARE, "seals")
SEAL_INDEX = os.path.join(SHARE, "seals.json")
LIB: dict[str, dict] = {}    # sig_id -> {name, file, added_at, hash}
HOST = "0.0.0.0"             # 默认监听所有网卡，局域网内其他电脑可直接访问

SERVER_PORT = 0              # 实际监听端口（更新后要用同一个端口重启）

# 更新状态（后台线程写，接口读）：phase ∈ idle/checking/available/downloading/
# installing/restarting/uptodate/error；progress 0~100
UPDATE_LOCK = threading.Lock()
UPDATE_STATE: dict = {
    "current": vmod.VERSION,
    "phase": "idle",
    "progress": 0,
    "message": "",
    "checked_at": "",
    "info": None,        # 有新版本时：{version, notes, size, sha256, published, source_host, sig_msg}
    "error": "",
    "applied": "",       # 已装到的版本
}


def _upd(**kw):
    with UPDATE_LOCK:
        UPDATE_STATE.update(kw)


def _upd_snapshot() -> dict:
    with UPDATE_LOCK:
        d = dict(UPDATE_STATE)
    return d


def _ensure_tmp():
    os.makedirs(TMP, exist_ok=True)
    return TMP


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode()


def _pdf_info(data: bytes):
    pymupdf = ms.get_fitz()
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = []
    for i in range(len(doc)):
        r = doc[i].rect
        pages.append({"index": i + 1, "w_pt": round(r.width, 2), "h_pt": round(r.height, 2),
                      "rot": int(doc[i].rotation or 0)})
    n = len(doc)
    doc.close()
    return n, pages


# 页面渲染缓存：翻页/缩放/来回切换时同一页会反复请求，每次都重新打开 PDF + 光栅化
# 既慢又费 CPU（27 页 11MB 的日报约 60~120ms/次）。按 (doc_id, page, dpi) 缓存，
# 用字节预算做 LRU 淘汰，避免大文档把内存吃满。
_PAGE_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_PAGE_CACHE_LOCK = threading.Lock()
_PAGE_CACHE_BYTES = 0
_PAGE_CACHE_MAX_BYTES = 96 * 1024 * 1024
_PAGE_CACHE_MAX_ITEMS = 200


def _page_cache_get(key):
    global _PAGE_CACHE_BYTES
    with _PAGE_CACHE_LOCK:
        data = _PAGE_CACHE.get(key)
        if data is not None:
            _PAGE_CACHE.move_to_end(key)
        return data


def _page_cache_put(key, data: bytes):
    global _PAGE_CACHE_BYTES
    with _PAGE_CACHE_LOCK:
        if key in _PAGE_CACHE:
            _PAGE_CACHE_BYTES -= len(_PAGE_CACHE.pop(key))
        _PAGE_CACHE[key] = data
        _PAGE_CACHE_BYTES += len(data)
        while _PAGE_CACHE and (len(_PAGE_CACHE) > _PAGE_CACHE_MAX_ITEMS
                               or _PAGE_CACHE_BYTES > _PAGE_CACHE_MAX_BYTES):
            _, old = _PAGE_CACHE.popitem(last=False)
            _PAGE_CACHE_BYTES -= len(old)


def _page_cache_clear(doc_id: str | None = None):
    """清缓存；给 doc_id 时只清该文档的所有页。"""
    global _PAGE_CACHE_BYTES
    with _PAGE_CACHE_LOCK:
        if doc_id is None:
            _PAGE_CACHE.clear()
            _PAGE_CACHE_BYTES = 0
            return
        for k in [k for k in _PAGE_CACHE if k[0] == doc_id]:
            _PAGE_CACHE_BYTES -= len(_PAGE_CACHE.pop(k))


def _page_cache_stats() -> dict:
    with _PAGE_CACHE_LOCK:
        return {"items": len(_PAGE_CACHE), "bytes": _PAGE_CACHE_BYTES}


def _doc_put(doc_id: str, doc: dict):
    """登记打开的 PDF，并按 LRU 淘汰旧文档（连同它的页面缓存一起清掉）。"""
    DOCS[doc_id] = doc
    DOCS.move_to_end(doc_id)
    while len(DOCS) > MAX_DOCS:
        old_id, _ = DOCS.popitem(last=False)
        _page_cache_clear(old_id)


def _page_png(doc_id: str, page: int, dpi: int) -> bytes:
    key = (doc_id, int(page), int(dpi))
    hit = _page_cache_get(key)
    if hit is not None:
        return hit
    pymupdf = ms.get_fitz()
    d = DOCS[doc_id]
    doc = pymupdf.open(stream=d["bytes"], filetype="pdf")
    try:
        pix = doc[page - 1].get_pixmap(dpi=dpi, alpha=False)
        out = pix.tobytes("png")
    finally:
        doc.close()
    _page_cache_put(key, out)
    return out


def _prep_sig(data: bytes, name: str) -> dict:
    """签名 PNG 预处理：统一 RGBA、白底洗透明，存到临时目录."""
    Image = ms.get_pil()
    _ensure_tmp()
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    # 白底洗透明（仅当原图没有真正透明通道时）
    if data[:4] == b"\x89PNG" and im.getchannel("A").getextrema()[0] == 255:
        im = ms.whiten_to_alpha(im, 238)
    sid = uuid.uuid4().hex[:12]
    path = os.path.join(TMP, f"sig_{sid}.png")
    im.save(path, format="PNG")
    SIGS[sid] = {"path": path, "w": im.width, "h": im.height, "name": name}
    return {"sig_id": sid, "w": im.width, "h": im.height, "name": name}


# ---------------- 共享签名库（存在本机 shared/seals） ----------------
def _ensure_share():
    os.makedirs(SEAL_DIR, exist_ok=True)


def _safe_stem(name: str, fallback: str = "seal") -> str:
    import re
    s = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name or "").strip()
    s = os.path.splitext(s)[0].strip() or fallback
    return s[:40]


def _lib_meta() -> list:
    try:
        with open(SEAL_INDEX, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:                                          # noqa: BLE001
        return []


def _lib_save_meta(items: list):
    _ensure_share()
    tmp = SEAL_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SEAL_INDEX)


def _lib_register(rec: dict) -> str | None:
    """把共享库里的一条记录注册成可用签名（SIGS + LIB），返回 sig_id。"""
    path = os.path.join(SEAL_DIR, rec.get("file", ""))
    if not rec.get("file") or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        info = _prep_sig(f.read(), rec.get("name") or os.path.basename(path))
    sid = info["sig_id"]
    SIGS[sid]["shared"] = True
    import hashlib
    with open(path, "rb") as f:
        fh = hashlib.sha1(f.read()).hexdigest()
    LIB[sid] = {"name": info.get("name") or rec.get("name"), "file": rec["file"],
                "added_at": rec.get("added_at", ""), "hash": rec.get("hash", ""),
                "file_hash": rec.get("file_hash") or fh}
    return sid


def _lib_load():
    """启动时把共享签名全部重新注册，任何电脑打开工具都能直接看到。"""
    _ensure_share()
    ok = []
    for rec in _lib_meta():
        if _lib_register(rec):
            ok.append(rec)
    # 索引里指向的文件丢了：顺手清掉，避免界面上出现空条目
    if len(ok) != len(_lib_meta()):
        _lib_save_meta(ok)
    return len(ok)


def _lib_seed_from_assets() -> int:
    """首次启动把程序自带 assets/seals 里的章导入共享库，升级后不用重新导入。"""
    base = os.path.join(HERE, "assets", "seals")
    if not os.path.isdir(base):
        return 0
    n = 0
    for fn in sorted(os.listdir(base)):
        if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")):
            continue
        p = os.path.join(base, fn)
        try:
            with open(p, "rb") as f:
                data = f.read()
            if _shared_sid_by_hash(data):
                continue
            _lib_add(fn, data)
            n += 1
        except Exception as e:                                  # noqa: BLE001
            print("  ！导入既有签名 %s 失败：%s" % (fn, e))
    return n


def _lib_add(name: str, data: bytes) -> tuple[str, bool]:
    """把一张签名存进共享库（同一张图按 sha1 去重）。返回 (sig_id, 是否已存在)。"""
    import hashlib
    digest = hashlib.sha1(data).hexdigest()
    for sid, rec in LIB.items():
        if rec.get("hash") == digest and os.path.exists(SIGS[sid]["path"]):
            return sid, True

    info = _prep_sig(data, name or "签名.png")
    sid = info["sig_id"]
    _ensure_share()
    base = "%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), _safe_stem(name))
    fn, k = base + ".png", 2
    while os.path.exists(os.path.join(SEAL_DIR, fn)):
        fn = "%s(%d).png" % (base, k)
        k += 1
    # 存清洗过的 PNG（白底已转透明），保证所有客户端拿到的是同一份
    with open(SIGS[sid]["path"], "rb") as src:
        stored = src.read()
    with open(os.path.join(SEAL_DIR, fn), "wb") as dst:
        dst.write(stored)
    rec = {"name": info.get("name") or name, "file": fn,
           "added_at": time.strftime("%Y-%m-%d %H:%M:%S"), "hash": digest,
           "file_hash": hashlib.sha1(stored).hexdigest()}
    meta = _lib_meta()
    meta.append(rec)
    _lib_save_meta(meta)
    SIGS[sid]["shared"] = True
    LIB[sid] = rec
    return sid, False


def _shared_sid_by_hash(data: bytes) -> str | None:
    import hashlib
    h = hashlib.sha1(data).hexdigest()
    for sid, rec in LIB.items():
        if sid in SIGS and h in (rec.get("hash"), rec.get("file_hash")):
            return sid
    return None


def _lib_list() -> list:
    out = []
    for sid, rec in LIB.items():
        s = SIGS.get(sid)
        if not s:
            continue
        out.append({"sig_id": sid, "name": s.get("name") or rec.get("name"),
                    "w": s["w"], "h": s["h"], "file": rec.get("file", ""),
                    "added_at": rec.get("added_at", "")})
    out.sort(key=lambda x: x.get("added_at") or "")
    return out


def _lib_delete(sid: str) -> dict:
    rec = LIB.get(sid)
    if not rec:
        raise ValueError("共享签名不存在（可能已被其他电脑删除）")
    path = os.path.join(SEAL_DIR, rec.get("file", ""))
    if os.path.exists(path):
        os.remove(path)
    LIB.pop(sid, None)
    SIGS.pop(sid, None)
    _lib_save_meta([r for r in _lib_meta() if r.get("file") != rec.get("file")])
    return {"deleted": rec.get("name"), "file": rec.get("file")}


def _lan_ips() -> list:
    """本机在局域网里的 IPv4 地址（用于启动时提示其他电脑访问哪个地址）。"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:                                          # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:                                          # noqa: BLE001
        pass
    return sorted(i for i in ips if i and not i.startswith("127.") and i != "0.0.0.0")


def _is_lan_ip(ip: str) -> bool:
    """只放行局域网/本机来源，避免误把端口转发到公网后裸奔。"""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def _rotated_sig(sid: str, rot: float) -> str:
    """生成按角度旋转后的透明 PNG（PIL 正角度 = 逆时针），按 (签名, 角度) 缓存。"""
    Image = ms.get_pil()
    key = int(round(float(rot) * 100))
    dst = os.path.join(_ensure_tmp(), f"rot_{sid}_{key}.png")
    if os.path.exists(dst):
        return dst
    im = Image.open(SIGS[sid]["path"]).convert("RGBA")
    # expand=True：画布扩到旋转后的外接矩形，四角保持透明
    im.rotate(float(rot), expand=True, resample=Image.BICUBIC).save(dst, format="PNG")
    return dst


def _build_pdf_items(doc_id: str, src_pages: list, items: list, keep: bool):
    """把前端贴章项换算成 PDF 坐标，供任意目标文档使用。

    src_pages 是「坐标基准文档」的页尺寸；目标文档页尺寸不同时按页宽/高等比缩放，
    这样同一套位置可以直接套到版式相近的另一份 PDF 上。
    返回 (pdf_items, missing, skipped)；missing=签名失效，skipped=页码超出目标文档。
    """
    pages = DOCS[doc_id]["pages"]
    pdf_items = []
    missing = []
    skipped = []
    for i, it in enumerate(items):
        sid = it.get("sig_id") or it.get("sigId")
        if sid not in SIGS:
            # 旧版本这里直接 continue：服务重启后签名缓存没了，用户点导出会得到一个
            # "成功"但没有章的 PDF。宁可报错也不能交付错文件。
            missing.append(i + 1)
            continue
        page = int(it["page"])
        if page < 1 or page > len(pages):
            skipped.append({"item": i + 1, "page": page, "pages": len(pages)})
            continue
        ref = src_pages[page - 1] if page - 1 < len(src_pages) else None
        sx = sy = 1.0
        if ref and ref.get("w_pt") and ref.get("h_pt"):
            sx = pages[page - 1]["w_pt"] / ref["w_pt"]
            sy = pages[page - 1]["h_pt"] / ref["h_pt"]
        ph = pages[page - 1]["h_pt"]
        # 左上角坐标 -> PDF 左下角坐标
        w = float(it["w"]) * sx
        h = float(it["h"]) * sy
        if keep:
            sw, sh = SIGS[sid]["w"], SIGS[sid]["h"]
            if sw > 0 and sh > 0:
                h = w * sh / sw
        x = float(it["x"]) * sx
        y_top = float(it["y"]) * sy

        try:
            rot = float(it.get("rot") or 0.0)
        except (TypeError, ValueError):
            rot = 0.0
        rot = ((rot + 180.0) % 360.0) - 180.0      # 归一化到 (-180, 180]
        img_path = SIGS[sid]["path"]
        if abs(rot) > 0.01:
            # 绕中心旋转：外接矩形变大，中心点不动
            cx, cy = x + w / 2.0, y_top + h / 2.0
            th = math.radians(rot)
            bw = abs(w * math.cos(th)) + abs(h * math.sin(th))
            bh = abs(w * math.sin(th)) + abs(h * math.cos(th))
            x, y_top, w, h = cx - bw / 2.0, cy - bh / 2.0, bw, bh
            img_path = _rotated_sig(sid, rot)

        box = (x, ph - y_top - h, x + w, ph - y_top)
        spec = ms.SigSpec(name=f"gui{i}", page=page, rect=[0, 0, 0, 0],
                          image=img_path, blend="auto", visual_only=True)
        pdf_items.append((spec, "", box))
    return pdf_items, missing, skipped


def _export(payload: dict) -> tuple[bytes, str]:
    """按前端提交的贴章项生成 PDF。坐标是左上角原点(pt)。

    每项可带 rot（角度，逆时针为正）：章图绕自身中心旋转，
    导出时把画布扩到旋转后的外接矩形，保证位置中心不变、不裁切。
    """
    doc_id = payload["doc_id"]
    items = payload.get("items") or []
    keep = payload.get("keep_ratio", True)
    base = DOCS[doc_id]["bytes"]
    name = payload.get("filename") or "已签章.pdf"

    pdf_items, missing, skipped = _build_pdf_items(
        doc_id, payload.get("ref_pages") or DOCS[doc_id]["pages"], items, keep)

    if missing:
        raise ValueError("有 %d 个签名已失效（服务重启后签名缓存会清空），"
                         "请重新导入签名图片后再导出" % len(missing))
    if skipped:
        raise ValueError("第 %d 个签名的页码 %d 超出文档页数（共 %d 页）"
                         % (skipped[0]["item"], skipped[0]["page"], skipped[0]["pages"]))
    if not pdf_items:
        raise ValueError("没有任何贴章项")
    font = ms.pick_font(None)
    out = ms.stamp_visual_batch(base, pdf_items, font)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return out, name


def _zip_name_ok(name: str) -> str:
    import re
    n = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip() or "已签章.pdf"
    return n


def _export_batch(payload: dict) -> tuple[bytes, str, list]:
    """批量盖章：用同一套贴章位置，给多份 PDF 各生成一份已签章文件，打包成 zip。

    payload:
      master_id   坐标基准文档 doc_id（用户在它上面摆的章）
      ref_pages   基准文档的页尺寸（缺省取 master_id 自己的）
      docs        [{doc_id, filename}, ...] 要盖章的全部文件（含基准文档）
      items       贴章项
      keep_ratio  是否按签名原图比例
    返回 (zip_bytes, zip_name)
    """
    import zipfile
    master_id = payload.get("master_id") or payload.get("doc_id")
    if master_id not in DOCS:
        raise ValueError("基准文档不存在（服务可能已重启），请重新导入 PDF")
    docs = payload.get("docs") or []
    if not docs:
        raise ValueError("没有要盖章的 PDF")
    items = payload.get("items") or []
    if not items:
        raise ValueError("还没有放置任何签名")
    keep = payload.get("keep_ratio", True)
    src_pages = payload.get("ref_pages") or DOCS[master_id]["pages"]
    font = ms.pick_font(None)

    buf = io.BytesIO()
    used = set()
    report = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in docs:
            did = d.get("doc_id")
            fname = d.get("filename") or "doc.pdf"
            if did not in DOCS:
                report.append({"file": fname, "ok": False, "error": "文档已失效，请重新导入"})
                continue
            try:
                pdf_items, missing, skipped = _build_pdf_items(did, src_pages, items, keep)
                if missing:
                    raise ValueError("有 %d 个签名已失效，请重新导入签名图片" % len(missing))
                if not pdf_items:
                    raise ValueError("所有章都超出该文档页数（共 %d 页）" % len(DOCS[did]["pages"]))
                out = ms.stamp_visual_batch(DOCS[did]["bytes"], pdf_items, font)
            except Exception as e:                      # noqa: BLE001
                report.append({"file": fname, "ok": False, "error": str(e)})
                continue
            stem = fname[:-4] if fname.lower().endswith(".pdf") else fname
            arc = _zip_name_ok("%s_已签章.pdf" % stem)
            k = 2
            while arc in used:
                arc = _zip_name_ok("%s_已签章(%d).pdf" % (stem, k))
                k += 1
            used.add(arc)
            zf.writestr(arc, out)
            report.append({"file": fname, "ok": True, "out": arc,
                           "skipped": len(skipped), "pages": len(DOCS[did]["pages"])})

    ok_n = sum(1 for r in report if r.get("ok"))
    if not ok_n:
        errs = "；".join("%s: %s" % (r["file"], r.get("error")) for r in report[:3])
        raise ValueError("全部文件都未能盖章（%s）" % errs)
    zname = "SignStudio_批量签章_%s.zip" % time.strftime("%Y%m%d_%H%M")
    # 把逐份结果写进 zip 里的一个说明文件，用户解压后能看到哪几份被跳过
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_批量盖章结果.txt",
                    "\n".join(["%s  %s%s" % (
                        "OK  " if r.get("ok") else "FAIL",
                        r["file"],
                        ("  跳过 %d 处（该文件页数不足）" % r["skipped"]) if r.get("ok") and r.get("skipped")
                        else ("" if r.get("ok") else "  " + str(r.get("error"))),
                    ) for r in report]) + "\n")
    return buf.getvalue(), zname, report



def _health() -> dict:
    """依赖自检：缺什么就明确告诉用户装什么。"""
    missing = []
    for label, fn in (("PyMuPDF(pymupdf/fitz)", ms.get_fitz),
                      ("pikepdf", ms.get_pikepdf),
                      ("Pillow(PIL)", ms.get_pil)):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            missing.append(f"{label} -> {e}")
    port = SERVER_PORT or 8765
    return {"ok": not missing, "missing": missing,
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "install_cmd": "install.bat" if os.name == "nt" else "bash install.sh",
            "host": HOST,
            "lan": [f"http://{ip}:{port}/" for ip in _lan_ips()],
            "share_dir": SHARE,
            "n_shared_seals": len(LIB),
            "n_projects": len(_proj_list())}


def _proj_dirs(name: str):
    import re
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip() or "未命名"
    base = os.path.join(PROJ, safe)
    return safe, base, os.path.join(base, "seals")


def _proj_save(payload: dict) -> dict:
    """把当前签名文件 + 每处位置保存成一个可复用工程。"""
    import shutil
    name = payload.get("name") or "未命名工程"
    safe, base, seals = _proj_dirs(name)
    os.makedirs(seals, exist_ok=True)

    sig_map = {}
    meta_sigs = []
    for i, sid in enumerate(payload.get("sig_ids") or []):
        s = SIGS.get(sid)
        if not s:
            continue
        fn = f"{i:02d}_" + (os.path.basename(s["path"]).split("_", 1)[-1] or "seal.png")
        if not fn.lower().endswith(".png"):
            fn += ".png"
        dst = os.path.join(seals, fn)
        shutil.copyfile(s["path"], dst)
        key = f"s{i}"
        sig_map[sid] = key
        meta_sigs.append({"key": key, "file": f"seals/{fn}", "name": s["name"]})

    items = []
    for it in payload.get("items") or []:
        key = sig_map.get(it.get("sigId"))
        if not key:
            continue
        items.append({"page": int(it["page"]), "x": float(it["x"]), "y": float(it["y"]),
                      "w": float(it["w"]), "h": float(it["h"]), "sig": key,
                      "rot": float(it.get("rot") or 0.0)})

    meta = {
        "name": safe,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ref_pages": payload.get("ref_pages") or [],
        "signatures": meta_sigs,
        "items": items,
        "keep_ratio": bool(payload.get("keep_ratio", True)),
    }
    with open(os.path.join(base, "project.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return {"name": safe, "path": base, "n_items": len(items), "n_sigs": len(meta_sigs)}


def _proj_import(payload: dict) -> dict:
    """导入「零安装版」保存的工程（浏览器 localStorage 里的 signstudio_projects_v1）。

    兼容三种输入：
      1) 整个 localStorage：{"工程A": {...}, "工程B": {...}}
      2) 单个工程对象：{name, sigs:[{key,name,dataUrl}], items:[...], ref_pages:[...]}
      3) 带包装：{"signstudio_projects_v1": {...}}
    签名图存在 dataUrl（base64 PNG）里，导入时落盘成 projects/<名称>/seals/*.png。
    """
    import base64
    import re as _re
    raw = payload.get("data")
    if isinstance(raw, str):
        obj = json.loads(raw)
    elif isinstance(raw, dict):
        obj = raw
    else:
        raise ValueError("没有收到工程内容")
    if isinstance(obj, dict) and "signstudio_projects_v1" in obj:
        obj = obj["signstudio_projects_v1"]

    if isinstance(obj, dict) and "items" in obj:
        projs = {obj.get("name") or payload.get("name") or "导入的工程": obj}
    elif isinstance(obj, dict):
        projs = {k: v for k, v in obj.items() if isinstance(v, dict) and "items" in v}
    else:
        raise ValueError("工程格式不对（不是零安装版的工程 JSON）")
    if not projs:
        raise ValueError("没找到可导入的工程（缺少 items 字段）")

    out = []
    for pname, p in projs.items():
        safe, base, seals = _proj_dirs(str(pname))
        os.makedirs(seals, exist_ok=True)
        sig_meta = []
        keys = set()
        for i, s in enumerate(p.get("sigs") or []):
            du = s.get("dataUrl") or ""
            m = _re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", du, _re.S)
            if not m:
                continue
            ext = m.group(1).lower()
            ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(ext, ext)
            try:
                data = base64.b64decode(m.group(2))
            except Exception:                                  # noqa: BLE001
                continue
            nm = _re.sub(r'[\\/:*?"<>|]+', "_", (s.get("name") or "seal")).strip() or "seal"
            fn = "%02d_%s.%s" % (i, nm, ext)
            with open(os.path.join(seals, fn), "wb") as f:
                f.write(data)
            key = s.get("key") or ("s%d" % i)
            sig_meta.append({"key": key, "file": "seals/" + fn, "name": s.get("name") or nm})
            keys.add(key)

        items, orphan = [], 0
        for it in p.get("items") or []:
            k = it.get("sig")
            if keys and k not in keys:
                orphan += 1
                continue
            items.append({"page": int(it.get("page") or 1),
                          "x": float(it.get("x") or 0), "y": float(it.get("y") or 0),
                          "w": float(it.get("w") or 0), "h": float(it.get("h") or 0),
                          "sig": k, "rot": float(it.get("rot") or 0)})

        meta = {
            "name": safe,
            "saved_at": p.get("saved_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "imported_from": "零安装版",
            "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ref_pages": p.get("ref_pages") or [],
            "signatures": sig_meta,
            "items": items,
            "keep_ratio": True,
        }
        with open(os.path.join(base, "project.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        out.append({"name": safe, "n_items": len(items), "n_sigs": len(sig_meta),
                    "orphan_items": orphan})
    return {"imported": out}


def _proj_list() -> list:
    out = []
    if not os.path.isdir(PROJ):
        return out
    for d in sorted(os.listdir(PROJ)):
        pj = os.path.join(PROJ, d, "project.json")
        if not os.path.exists(pj):
            continue
        try:
            with open(pj, encoding="utf-8") as f:
                m = json.load(f)
            out.append({"name": m.get("name", d), "saved_at": m.get("saved_at", ""),
                        "n_items": len(m.get("items") or []),
                        "n_sigs": len(m.get("signatures") or [])})
        except Exception:  # noqa: BLE001
            continue
    return out


def _proj_load(name: str) -> dict:
    """载入工程：把工程里的 PNG 重新注册为可用签名，并返回位置清单。"""
    safe, base, seals = _proj_dirs(name)
    pj = os.path.join(base, "project.json")
    if not os.path.exists(pj):
        raise ValueError(f"工程不存在: {safe}")
    with open(pj, encoding="utf-8") as f:
        m = json.load(f)
    key2sid = {}
    sigs_out = []
    for s in m.get("signatures") or []:
        path = os.path.join(base, s["file"])
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        # 工程里的签名如果和共享库是同一张图，直接复用共享库的 id，避免签名列表出现重名条目
        sid = _shared_sid_by_hash(data)
        if sid:
            info = {"sig_id": sid, "name": SIGS[sid]["name"], "w": SIGS[sid]["w"], "h": SIGS[sid]["h"]}
        else:
            info = _prep_sig(data, s.get("name") or os.path.basename(path))
        key2sid[s["key"]] = info["sig_id"]
        sigs_out.append({"sig_id": info["sig_id"], "name": info.get("name"),
                         "w": info["w"], "h": info["h"]})
    items = []
    for it in m.get("items") or []:
        sid = key2sid.get(it.get("sig"))
        if not sid:
            continue
        items.append({"page": it["page"], "x": it["x"], "y": it["y"],
                      "w": it["w"], "h": it["h"], "sigId": sid,
                      "rot": float(it.get("rot") or 0.0)})
    return {"name": m.get("name", safe), "sigs": sigs_out, "items": items,
            "ref_pages": m.get("ref_pages") or [], "keep_ratio": m.get("keep_ratio", True)}


def _proj_delete(name: str) -> dict:
    import shutil
    safe, base, _ = _proj_dirs(name)
    if os.path.isdir(base):
        shutil.rmtree(base)
        return {"deleted": safe}
    raise ValueError(f"工程不存在: {safe}")


# ---------------- 自动更新：检查 / 下载 / 安装 ----------------
def update_check(force: bool = True, timeout: int = 8) -> dict:
    """读更新清单并比较版本。返回给前端的精简结果（不含更新包地址等细节的也用得上）。"""
    if not force and _upd_snapshot().get("checked_at"):
        return _upd_snapshot()
    _upd(phase="checking", message="正在检查更新…", error="")
    try:
        info = up.check_latest(timeout=timeout)
    except Exception as e:                                    # noqa: BLE001
        msg = str(e)
        up.ulog("检查更新失败：%s" % msg)
        _upd(phase="error", error=msg, message="检查更新失败：" + msg.splitlines()[0],
             checked_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return _upd_snapshot()
    if info["has_update"]:
        _upd(phase="available", checked_at=info["checked_at"],
             message="发现新版本 v%s" % info["version"],
             info={k: info.get(k) for k in ("version", "notes", "size", "sha256", "published",
                                            "source_host", "sig_msg", "sig_state", "package_url",
                                            "package", "warning")},
             error="")
    else:
        _upd(phase="uptodate", checked_at=info["checked_at"], info=None,
             message="已是最新版本（v%s）" % vmod.VERSION, error="")
    up.ulog("检查更新：当前 v%s，最新 v%s，has_update=%s，来源 %s"
            % (vmod.VERSION, info["version"], info["has_update"], info["source_host"]))
    return _upd_snapshot()


def update_auto_check_async(delay: float = 1.5):
    """启动时后台静默检查（不阻塞开界面，失败也不打扰用户）。"""
    def _run():
        time.sleep(delay)
        update_check(force=True, timeout=8)
    t = threading.Thread(target=_run, name="update-check", daemon=True)
    t.start()
    return t


def update_apply_async(port: int = 0):
    """下载 → 校验 → 交给独立进程安装 → 本进程退出。整个过程进度写在 UPDATE_STATE。"""
    def _progress(got, total):
        pct = int(got * 100 / total) if total else min(95, int(got / 65536))
        _upd(phase="downloading", progress=max(1, min(99, pct)),
             message="正在下载更新包 %d%%（%.1f/%.1f MB）"
                     % (pct, got / 1048576.0, (total or 0) / 1048576.0))
    info = _upd_snapshot().get("info") or {}
    if not info:
        _upd(phase="error", error="没有可用的更新信息，请先点『检查更新』", progress=0)
        return
    try:
        _upd(phase="downloading", progress=1, message="开始下载 v%s…" % info.get("version"))
        # info 里可能缺 package_url（快照裁剪过），重新读一次清单拿完整字段
        full = up.check_latest(timeout=10)
        if not full["has_update"]:
            _upd(phase="uptodate", progress=0, message="已是最新版本", info=None)
            return
        zip_path = up.fetch_package(full, progress=_progress)
        _upd(phase="installing", progress=100,
             message="校验通过，正在安装 v%s…" % full["version"])
        up.install_and_restart(zip_path, port=port or SERVER_PORT,
                               version_tag=full["version"])
        _upd(phase="restarting", applied=full["version"],
             message="更新完成，正在重启…（页面会自动刷新）")
        time.sleep(0.6)
        up.ulog("主进程退出，等待安装进程接管")
        os._exit(0)                       # 交由 install-helper 覆盖文件并重启
    except Exception as e:                                    # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        up.ulog("更新失败：%s" % msg)
        _upd(phase="error", error=msg, progress=0, message="更新失败：" + str(e).splitlines()[0])


def _version_payload() -> dict:
    st = _upd_snapshot()
    st.update({"version": vmod.VERSION,
               "manual": bool(getattr(vmod, "UPDATE_MANUAL", True)),
               "latest_on_server": (st.get("info") or {}).get("version") or "",
               "manifests": vmod.DEFAULT_MANIFESTS})
    return st


class Handler(BaseHTTPRequestHandler):
    server_version = "SignStudio/1.0"

    def log_message(self, fmt, *args):  # 安静点
        pass

    # ---------- 基础响应
    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200):
        self._send(code, _json_bytes(obj))

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _lan_only(self) -> bool:
        """只接受局域网/本机来源。防止有人把端口映射到公网后，签章和工程被陌生人读写。"""
        ip = self.client_address[0] if self.client_address else ""
        if _is_lan_ip(ip):
            return True
        self._json({"error": "只允许局域网内访问（来源 %s 不在内网地址范围）" % ip}, 403)
        return False

    # ---------- 路由
    def do_GET(self):
        if not self._lan_only():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                html = os.path.join(HERE, "studio.html")
                if not os.path.exists(html):
                    return self._send(500, "找不到 studio.html".encode(), "text/plain; charset=utf-8")
                with open(html, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if u.path.startswith("/api/page/"):
                parts = [x for x in u.path.split("/") if x]
                doc_id, page = parts[2], parts[3]
                dpi = int(q.get("dpi", ["110"])[0])
                if doc_id not in DOCS:
                    return self._json({"error": "文档不存在"}, 404)
                return self._send(200, _page_png(doc_id, int(page), dpi), "image/png")

            if u.path.startswith("/api/sig/"):
                sid = u.path.split("/")[-1]
                s = SIGS.get(sid)
                if not s:
                    return self._json({"error": "签名不存在"}, 404)
                with open(s["path"], "rb") as f:
                    return self._send(200, f.read(), "image/png")

            if u.path == "/api/health":
                h = _health()
                h["version"] = vmod.VERSION
                return self._json(h)

            if u.path == "/api/version":
                return self._json(_version_payload())

            if u.path == "/api/update/status":
                return self._json(_version_payload())

            if u.path == "/api/update/download":
                return self._update_download()

            if u.path == "/api/project/list":
                return self._json({"projects": _proj_list()})

            if u.path == "/api/lib/seals":
                return self._json({"seals": _lib_list(), "dir": SEAL_DIR})

            if u.path == "/api/state":
                return self._json({
                    "docs": {k: {"name": v["name"], "pages": v["pages"]} for k, v in DOCS.items()},
                    "sigs": {k: {"name": v["name"], "w": v["w"], "h": v["h"],
                                 "shared": bool(v.get("shared"))} for k, v in SIGS.items()},
                })
            return self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _update_download(self):
        """手动更新用：把最新整包下载下来交给浏览器保存，由用户自己解压覆盖。

        不做任何覆盖动作，也不重启进程 —— 覆盖完全由用户手工完成。
        """
        try:
            info = up.check_latest(timeout=10)
            if not info.get("package_url"):
                return self._json({"ok": False, "error": "更新清单里没有更新包地址"}, 500)
            path = up.fetch_package(info)          # 已下载且 sha256 一致时直接复用
            with open(path, "rb") as f:
                body = f.read()
        except Exception as e:                     # noqa: BLE001
            msg = "%s: %s" % (type(e).__name__, e)
            up.ulog("下载更新包失败：%s" % msg)
            return self._json({"ok": False, "error": msg}, 500)
        name = info.get("package") or ("SignStudio-v%s.zip" % info.get("version", ""))
        up.ulog("已把更新包 v%s（%.1f KB）交给浏览器下载：%s"
                % (info.get("version", "?"), len(body) / 1024.0, name))
        return self._send(200, body, "application/zip",
                          {"Content-Disposition": 'attachment; filename="%s"' % name})

    def do_POST(self):
        if not self._lan_only():
            return
        u = urlparse(self.path)
        try:
            payload = self._read_json()

            if u.path == "/api/open_pdf":
                data = base64.b64decode(payload["data"])
                n, pages = _pdf_info(data)
                did = uuid.uuid4().hex[:12]
                _doc_put(did, {"bytes": data, "name": payload.get("filename", "doc.pdf"),
                               "pages": pages, "npages": n})
                return self._json({"doc_id": did, "npages": n, "pages": pages,
                                   "name": DOCS[did]["name"]})

            if u.path == "/api/add_sig":
                data = base64.b64decode(payload["data"])
                name = payload.get("filename", "签名.png")
                if payload.get("share", True):
                    sid, dup = _lib_add(name, data)
                    s = SIGS[sid]
                    return self._json({"sig_id": sid, "w": s["w"], "h": s["h"],
                                       "name": s["name"], "shared": True, "duplicate": dup})
                return self._json(_prep_sig(data, name))

            if u.path == "/api/lib/seal/delete":
                return self._json(_lib_delete(payload.get("sig_id", "")))

            if u.path == "/api/update/check":
                # 前端点「检查更新」：同步检查（最多等 9 秒），立刻回结果
                update_check(force=True, timeout=9)
                return self._json(_version_payload())

            if u.path == "/api/update/apply":
                # 手动更新模式：不下载、不覆盖、不重启，交给用户自己替换文件
                if getattr(vmod, "UPDATE_MANUAL", True):
                    return self._json({"ok": False,
                                       "error": "当前为手动更新模式：请点「下载更新包」，"
                                                "解压后把新文件覆盖到程序文件夹，再重启工具。"})
                st = _upd_snapshot()
                if st.get("phase") in ("downloading", "installing", "restarting"):
                    return self._json({"ok": False, "error": "更新正在进行中，请稍候"})
                if not st.get("info"):
                    return self._json({"ok": False, "error": "没有待安装的新版本，请先检查更新"})
                threading.Thread(target=update_apply_async, kwargs={"port": SERVER_PORT},
                                 name="update-apply", daemon=True).start()
                return self._json({"ok": True, "started": True, "target": st["info"].get("version")})

            if u.path == "/api/project/save":
                return self._json(_proj_save(payload))

            if u.path == "/api/project/list":
                return self._json({"projects": _proj_list()})

            if u.path == "/api/project/load":
                return self._json(_proj_load(payload.get("name", "")))

            if u.path == "/api/project/import":
                return self._json(_proj_import(payload))

            if u.path == "/api/project/delete":
                return self._json(_proj_delete(payload.get("name", "")))

            if u.path == "/api/export":
                out, name = _export(payload)
                fn = name.replace('"', "")
                return self._send(200, out, "application/pdf", {
                    "Content-Disposition": f'attachment; filename="job.pdf"; filename*=UTF-8\'\'{__import__("urllib.parse").parse.quote(fn)}',
                    "X-Filename": __import__("urllib.parse").parse.quote(fn),
                })

            if u.path == "/api/export_batch":
                out, name, report = _export_batch(payload)
                fn = name.replace('"', "")
                ok_n = sum(1 for r in report if r.get("ok"))
                return self._send(200, out, "application/zip", {
                    "Content-Disposition": f'attachment; filename="batch.zip"; filename*=UTF-8\'\'{__import__("urllib.parse").parse.quote(fn)}',
                    "X-Filename": __import__("urllib.parse").parse.quote(fn),
                    "X-Batch-Report": __import__("urllib.parse").parse.quote(
                        json.dumps({"ok": ok_n, "total": len(report), "report": report},
                                   ensure_ascii=False)),
                })

            return self._json({"error": "unknown api"}, 404)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def _free_port(preferred: int) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return 0


def main():
    ap = argparse.ArgumentParser(description="SignStudio —— PDF 电子签章可视化工具")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址：默认 0.0.0.0（局域网可访问）；只想本机用就写 127.0.0.1")
    ap.add_argument("--local-only", action="store_true",
                    help="只在 127.0.0.1 上监听（关闭局域网共享）")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-update-check", action="store_true",
                    help="本次启动不检查更新（离线环境用）")
    ap.add_argument("--reset-tmp", action="store_true", help="清空临时签名缓存后启动")
    args = ap.parse_args()

    global SERVER_PORT, HOST

    if args.reset_tmp and os.path.isdir(TMP):
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(PROJ, exist_ok=True)
    os.makedirs(SEAL_DIR, exist_ok=True)
    n_shared = _lib_load()
    n_seed = _lib_seed_from_assets()
    n_shared += n_seed

    HOST = "127.0.0.1" if args.local_only else (args.host or "0.0.0.0")

    h = _health()
    if not h["ok"]:
        print("!" * 60)
        print("  依赖不完整，导入 PDF 会失败：")
        for m in h["missing"]:
            print("   -", m.split("\n")[0])
        print(f"  请先安装依赖（{h['install_cmd']}），或执行：")
        print(f'     "{h["executable"]}" -m pip install -r requirements.txt')
        print("!" * 60)

    port = _free_port(args.port)
    SERVER_PORT = port
    srv = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 62)
    print(f"  SignStudio v{vmod.VERSION} 已启动")
    print(f"  本机打开： {url}")
    if HOST in ("0.0.0.0", ""):
        lan = _lan_ips()
        if lan:
            print("  局域网其他电脑打开（同一 WiFi/网线）：")
            for ip in lan:
                print(f"    http://{ip}:{port}/")
            print("  ※ Windows 首次启动如弹防火墙提示，请勾选「专用网络」并允许访问。")
        else:
            print("  ！没有检测到局域网 IP（可能未联网），其他电脑暂时无法访问。")
    else:
        print(f"  仅本机访问（监听 {HOST}），局域网共享已关闭。")
    print(f"  共享签名库：{SEAL_DIR}（{n_shared} 张）  工程目录：{PROJ}")
    print("  关闭本窗口即退出")
    print("=" * 62)
    if not args.no_update_check:
        # 启动时后台静默检查更新（不阻塞开界面，失败也不弹窗打扰）
        update_auto_check_async()
        print("  （已在后台检查更新，有新版本会在界面顶部提示）")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
