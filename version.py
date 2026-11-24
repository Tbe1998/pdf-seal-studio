#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""version.py —— 版本号与更新通道的唯一来源（改版本只改这里）

所有模块（signstudio.py / start.py / updater.py / tools/release.py）都必须 import 本文件，
不要在别处硬编码版本号、镜像目录或更新地址。
"""

VERSION = "1.7.2"                 # ← 唯一版本号
APP_TITLE = "SignStudio"
APP_TITLE_CN = "签章工作台"

# 更新方式：False = 一键自动（点「立即更新」→ 下载 → 校验 → 覆盖 → 同端口重启）
#           True  = 手动（只给下载包，用户自己解压覆盖）
# 2026-10-24 用户最终选择：自动安装，只要他点一下确认即可。
UPDATE_MANUAL = False

# NAS 镜像目录（/volume1/web/download/<MIRROR_SUBDIR>/）
MIRROR_SUBDIR = "signstudio"
NAS_HOST = "192.168.1.6"
PUBLIC_HOST = "www.topbei.cn"

LAN_BASE = "http://%s/download/%s" % (NAS_HOST, MIRROR_SUBDIR)
PUBLIC_BASE = "https://%s/download/%s" % (PUBLIC_HOST, MIRROR_SUBDIR)

# 更新清单地址（按顺序尝试：家里局域网最快，公网兜底）
DEFAULT_MANIFESTS = [
    LAN_BASE + "/latest.json",
    PUBLIC_BASE + "/latest.json",
]

# 零安装网页版用的“可被 <script> 跨域加载”清单（file:// 下 fetch 会被 CORS 拦，
# 但 <script src> 不受同源限制，所以网页版检查更新走这个文件）
VERSION_JS_URL = PUBLIC_BASE + "/version.js"
VERSION_JS_LAN_URL = LAN_BASE + "/version.js"

# 清单签名公钥（Ed25519, base64 raw）。私钥只在发布机上，
# 用户拿到的清单若带 sig 就必须能被这把公钥验过，否则拒绝更新。
UPDATE_PUBKEY = "qs8wzo/1QAG0AYBbOHVQbzcL5R1rULcHNwRkjGGFcHA="

# 自更新时绝对不碰的用户数据目录（升级只覆盖程序文件）
USER_DATA_DIRS = ("projects", "shared", "jobs", "in", "out", "demo", "cert", "assets", ".studio_tmp", ".venv")


def version_tuple(text=""):
    """'v1.1.0' -> (1, 1, 0)，用于比较大小。"""
    t = (text or "").strip().lstrip("vV")
    parts = []
    for seg in t.replace("-", ".").split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])
