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
import mimetypes
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import multisign as ms  # 复用已打磨好的贴章/正片叠底/字体逻辑

DOCS: dict[str, dict] = {}   # doc_id -> {bytes, name, pages:[(w,h)], npages}
SIGS: dict[str, dict] = {}   # sig_id -> {path, w, h, name}
TMP = os.path.join(HERE, ".studio_tmp")
PROJ = os.path.join(HERE, "projects")


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


def _page_png(doc_id: str, page: int, dpi: int) -> bytes:
    pymupdf = ms.get_fitz()
    d = DOCS[doc_id]
    doc = pymupdf.open(stream=d["bytes"], filetype="pdf")
    pix = doc[page - 1].get_pixmap(dpi=dpi, alpha=False)
    out = pix.tobytes("png")
    doc.close()
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


def _export(payload: dict) -> tuple[bytes, str]:
    """按前端提交的贴章项生成 PDF。坐标是左上角原点(pt)。"""
    doc_id = payload["doc_id"]
    items = payload.get("items") or []
    keep = payload.get("keep_ratio", True)
    base = DOCS[doc_id]["bytes"]
    name = payload.get("filename") or "已签章.pdf"

    pdf_items = []
    for i, it in enumerate(items):
        sid = it.get("sig_id") or it.get("sigId")
        if sid not in SIGS:
            continue
        page = int(it["page"])
        ph = DOCS[doc_id]["pages"][page - 1]["h_pt"]
        # 左上角坐标 -> PDF 左下角坐标
        w = float(it["w"])
        h = float(it["h"])
        if keep:
            sw, sh = SIGS[sid]["w"], SIGS[sid]["h"]
            if sw > 0 and sh > 0:
                h = w * sh / sw
        x = float(it["x"])
        y_top = float(it["y"])
        box = (x, ph - y_top - h, x + w, ph - y_top)
        spec = ms.SigSpec(name=f"gui{i}", page=page, rect=[0, 0, 0, 0],
                          image=SIGS[sid]["path"], blend="auto", visual_only=True)
        pdf_items.append((spec, "", box))

    if not pdf_items:
        raise ValueError("没有任何贴章项")
    font = ms.pick_font(None)
    out = ms.stamp_visual_batch(base, pdf_items, font)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return out, name


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
    return {"ok": not missing, "missing": missing,
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "install_cmd": "install.bat" if os.name == "nt" else "bash install.sh"}


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
                      "w": float(it["w"]), "h": float(it["h"]), "sig": key})

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
            info = _prep_sig(f.read(), s.get("name") or os.path.basename(path))
        key2sid[s["key"]] = info["sig_id"]
        sigs_out.append({"sig_id": info["sig_id"], "name": info.get("name"),
                         "w": info["w"], "h": info["h"]})
    items = []
    for it in m.get("items") or []:
        sid = key2sid.get(it.get("sig"))
        if not sid:
            continue
        items.append({"page": it["page"], "x": it["x"], "y": it["y"],
                      "w": it["w"], "h": it["h"], "sigId": sid})
    return {"name": m.get("name", safe), "sigs": sigs_out, "items": items,
            "ref_pages": m.get("ref_pages") or [], "keep_ratio": m.get("keep_ratio", True)}


def _proj_delete(name: str) -> dict:
    import shutil
    safe, base, _ = _proj_dirs(name)
    if os.path.isdir(base):
        shutil.rmtree(base)
        return {"deleted": safe}
    raise ValueError(f"工程不存在: {safe}")


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

    # ---------- 路由
    def do_GET(self):
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
                return self._json(_health())

            if u.path == "/api/project/list":
                return self._json({"projects": _proj_list()})

            if u.path == "/api/state":
                return self._json({
                    "docs": {k: {"name": v["name"], "pages": v["pages"]} for k, v in DOCS.items()},
                    "sigs": {k: {"name": v["name"], "w": v["w"], "h": v["h"]} for k, v in SIGS.items()},
                })
            return self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            payload = self._read_json()

            if u.path == "/api/open_pdf":
                data = base64.b64decode(payload["data"])
                n, pages = _pdf_info(data)
                did = uuid.uuid4().hex[:12]
                DOCS[did] = {"bytes": data, "name": payload.get("filename", "doc.pdf"),
                             "pages": pages, "npages": n}
                return self._json({"doc_id": did, "npages": n, "pages": pages,
                                   "name": DOCS[did]["name"]})

            if u.path == "/api/add_sig":
                data = base64.b64decode(payload["data"])
                return self._json(_prep_sig(data, payload.get("filename", "签名.png")))

            if u.path == "/api/project/save":
                return self._json(_proj_save(payload))

            if u.path == "/api/project/list":
                return self._json({"projects": _proj_list()})

            if u.path == "/api/project/load":
                return self._json(_proj_load(payload.get("name", "")))

            if u.path == "/api/project/delete":
                return self._json(_proj_delete(payload.get("name", "")))

            if u.path == "/api/export":
                out, name = _export(payload)
                fn = name.replace('"', "")
                return self._send(200, out, "application/pdf", {
                    "Content-Disposition": f'attachment; filename="job.pdf"; filename*=UTF-8\'\'{__import__("urllib.parse").parse.quote(fn)}',
                    "X-Filename": __import__("urllib.parse").parse.quote(fn),
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
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reset-tmp", action="store_true", help="清空临时签名缓存后启动")
    args = ap.parse_args()

    if args.reset_tmp and os.path.isdir(TMP):
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(PROJ, exist_ok=True)

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
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print("  SignStudio 已启动")
    print(f"  请在浏览器打开： {url}")
    print("  关闭本窗口即退出")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
