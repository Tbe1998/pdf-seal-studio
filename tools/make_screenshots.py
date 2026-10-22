#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用示例素材截取界面图（README 用）。只在维护仓库时运行，普通用户不需要。"""
import os

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Chrome 可执行文件：优先用环境变量 CHROME_PATH，其次常见路径 / 交给 Playwright 自行查找
CHROME = os.environ.get("CHROME_PATH", "")
if not CHROME:
    for _p in (
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if os.path.exists(_p):
            CHROME = _p
            break
IMG = os.path.join(ROOT, "docs", "images")
PDF = os.path.join(ROOT, "examples", "示例监测日报.pdf")
SIG_A = os.path.join(ROOT, "assets", "seals", "示例签名_甲.png")
SIG_B = os.path.join(ROOT, "assets", "seals", "示例签名_乙.png")
SEAL = os.path.join(ROOT, "assets", "seals", "示例项目章.png")


def main():
    os.makedirs(IMG, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME or None, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1500, "height": 950}, device_scale_factor=1.5)
        pg.goto("file://" + os.path.join(ROOT, "webapp", "签章工作台.html"))
        pg.set_input_files("#filePdf", PDF)
        pg.wait_for_function("() => document.querySelector('#pageTotal').textContent === '3'", timeout=60000)
        pg.set_input_files("#fileSig", [SIG_A, SIG_B, SEAL])
        pg.wait_for_function("() => document.querySelectorAll('#sigList .sig').length === 3", timeout=30000)
        # 依次放两个签名 + 一个章
        pg.evaluate("""() => {
            const A = S.sigs.find(s => s.name.includes('甲'));
            const B = S.sigs.find(s => s.name.includes('乙'));
            const Z = S.sigs.find(s => s.isSeal);
            S.items.push({uid:'d1', page:1, x:160, y:596, w:85, h:29, sigId:A.id});
            S.items.push({uid:'d2', page:1, x:160, y:630, w:85, h:29, sigId:B.id});
            S.items.push({uid:'d3', page:1, x:126, y:652, w:102, h:105, sigId:Z.id});
            S.activeItem = 'd3';
            renderItems(); renderItemList();
        }""")
        # 缩小到整页可见，让盖章效果出现在截图里
        pg.evaluate("() => { S.zoom = 0.62; applyZoom(); }")
        pg.wait_for_timeout(900)
        pg.screenshot(path=os.path.join(IMG, "界面-零安装版.png"))
        b.close()
    print("已生成：", os.path.join(IMG, "界面-零安装版.png"))


if __name__ == "__main__":
    main()
