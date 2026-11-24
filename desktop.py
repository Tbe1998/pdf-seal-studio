#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SignStudio 桌面入口（打 exe 用）

打开后：
  1. 后台静默起本地服务，监听 0.0.0.0:8765（局域网其他电脑可用浏览器访问）；
  2. 本机弹一个原生窗口直接使用（pywebview）；没有 WebView2 时退回
     「控制面板小窗口 + 默认浏览器」；
  3. 窗口关闭即退出，服务随之停止。

参数：
  --port 8765        指定端口（被占用会自动顺延）
  --local-only       只在 127.0.0.1 监听，不共享给局域网
  --no-window        不弹窗（无头/调试用），只跑服务
  --browser          强制用默认浏览器，不用原生窗口
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signstudio as ss          # noqa: E402
import version as vmod           # noqa: E402

LOG_DIR = os.path.join(ss.DATA_ROOT, "logs")


def setup_log():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        logging.basicConfig(
            filename=os.path.join(LOG_DIR, "signstudio-desktop.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            encoding="utf-8",
        )
    except Exception:                                          # noqa: BLE001
        logging.basicConfig(level=logging.INFO)


def is_our_app(port: int) -> bool:
    """端口上已经有 SignStudio 在跑就复用它，避免重复开一堆服务。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=0.8) as r:
            data = r.read(400).decode("utf-8", "ignore")
        return '"ok": true' in data or '"ok":true' in data
    except Exception:                                          # noqa: BLE001
        return False


def run_server(host: str, port: int, local_only: bool):
    srv, real_port, n_shared = ss.bootstrap(host=host, port_pref=port, local_only=local_only)
    t = threading.Thread(target=srv.serve_forever, name="signstudio-http", daemon=True)
    t.start()
    logging.info("server on %s:%s, shared seals=%s, data=%s",
                 ss.HOST, real_port, n_shared, ss.DATA_ROOT)
    return srv, real_port, n_shared


def lan_urls(port: int) -> list:
    if ss.HOST == "127.0.0.1":
        return []
    return ["http://%s:%d/" % (ip, port) for ip in ss._lan_ips()]


def open_native(url: str) -> bool:
    """优先用原生窗口（WebView2）。成功返回 True（窗口关闭后才返回）。"""
    try:
        import webview                      # type: ignore
    except Exception as e:                                     # noqa: BLE001
        logging.warning("pywebview 不可用：%s", e)
        return False
    try:
        webview.create_window("SignStudio v%s · PDF 电子签章" % vmod.VERSION, url,
                              width=1440, height=920, min_size=(960, 640))
        webview.start()                     # 阻塞到窗口关闭
        return True
    except Exception as e:                                     # noqa: BLE001
        logging.warning("原生窗口启动失败：%s", e)
        return False


def control_window(url: str, urls: list):
    """没有原生窗口时的兜底：一个小控制窗 + 浏览器里的工作台。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as e:                                     # noqa: BLE001
        logging.warning("tkinter 不可用：%s", e)
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    root = tk.Tk()
    root.title("SignStudio v%s · 局域网共享中" % vmod.VERSION)
    root.geometry("520x330")
    root.configure(bg="#f5f7fa")

    tk.Label(root, text="SignStudio 正在后台运行", font=("Microsoft YaHei", 15, "bold"),
             bg="#f5f7fa", fg="#1f2937").pack(pady=(18, 4))
    tk.Label(root, text="本机打开（点下面的按钮）：", bg="#f5f7fa", fg="#6b7280").pack()
    tk.Label(root, text=url, bg="#f5f7fa", fg="#0369a1",
             font=("Consolas", 12), cursor="hand2").pack(pady=(0, 8))

    if urls:
        tk.Label(root, text="局域网其他电脑用浏览器打开：", bg="#f5f7fa", fg="#6b7280").pack()
        for u in urls:
            tk.Label(root, text=u, bg="#f5f7fa", fg="#0f766e",
                     font=("Consolas", 12)).pack()
        tk.Label(root, text="（同事打不开时，检查 Windows 防火墙是否放行）",
                 bg="#f5f7fa", fg="#9ca3af", font=("Microsoft YaHei", 9)).pack(pady=(2, 8))

    def copy_first(u):
        root.clipboard_clear()
        root.clipboard_append(u)
        messagebox.showinfo("已复制", u)

    bar = tk.Frame(root, bg="#f5f7fa")
    bar.pack(pady=10)
    tk.Button(bar, text="打开工作台", width=12, command=lambda: webbrowser.open(url)).pack(side="left", padx=6)
    if urls:
        tk.Button(bar, text="复制局域网地址", width=14,
                  command=lambda: copy_first(urls[0])).pack(side="left", padx=6)
    tk.Button(bar, text="退出", width=8, command=root.destroy).pack(side="left", padx=6)

    webbrowser.open(url)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="SignStudio 桌面版")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--browser", action="store_true", help="强制使用默认浏览器")
    args = ap.parse_args()

    setup_log()
    logging.info("start desktop v%s frozen=%s exe=%s", vmod.VERSION, ss.FROZEN,
                 sys.executable)

    # 已经有实例在跑：直接开窗口指向它，不再起第二个服务
    if is_our_app(args.port) and not args.no_window:
        url = "http://127.0.0.1:%d/" % args.port
        if args.browser or not open_native(url):
            webbrowser.open(url)
            control_window(url, lan_urls(args.port))
        return

    srv, port, _ = run_server(args.host, args.port, args.local_only)
    url = "http://127.0.0.1:%d/" % port
    urls = lan_urls(port)

    try:
        if args.no_window:
            while True:
                time.sleep(3600)
        if args.browser:
            webbrowser.open(url)
            control_window(url, urls)
        else:
            if not open_native(url):
                control_window(url, urls)
    except KeyboardInterrupt:
        pass
    finally:
        logging.info("shutdown")
        try:
            srv.shutdown()
        except Exception:                                      # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
