#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""start.py —— SignStudio 可靠启动器（bat/sh 只负责找到 Python 并调用它）

做什么：
  1. 打印运行环境（Python 版本、路径），方便排错
  2. 自检依赖 PyMuPDF / pikepdf / Pillow，缺了就自动 pip 安装（清华→阿里→官方）
  3. 装完复查，仍缺则给出可直接复制的命令（不静默失败）
  4. 启动 SignStudio 图形界面

用法：
  python start.py            启动界面（启动时后台自动检查更新）
  python start.py --check    只做依赖自检，不启动
  python start.py --diag     生成诊断报告 _诊断报告.txt（发给技术支持）
  python start.py --check-update  只检查有没有新版本
  python start.py --update        检查并安装更新（会重启界面）
  python start.py --no-update-check  本次启动不检查更新（离线用）
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import version as vmod
except Exception:                                            # noqa: BLE001
    vmod = None

MIRRORS = [
    ("清华镜像", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云镜像", "https://mirrors.aliyun.com/pypi/simple/"),
    ("官方源", ""),
]
PKGS = ["pymupdf", "pikepdf", "Pillow"]


def log(msg=""):
    print(msg, flush=True)


def hr(ch="="):
    log(ch * 62)


def py_info():
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "prefix": sys.prefix,
        "cwd": os.getcwd(),
        "here": HERE,
    }


def check_deps():
    """返回缺失项列表 [(显示名, 导入名, pip包名)]。"""
    missing = []
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        try:
            import fitz  # noqa: F401
        except ImportError:
            missing.append(("PyMuPDF", "pymupdf", "pymupdf"))
    try:
        import pikepdf  # noqa: F401
    except ImportError:
        missing.append(("pikepdf", "pikepdf", "pikepdf"))
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append(("Pillow", "PIL", "Pillow"))
    return missing


def ensure_pip() -> bool:
    """确保当前解释器有 pip（极少数 Python 安装不带 pip，例如部分精简版/商店版）。"""
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    log("    检测到没有 pip，尝试用 ensurepip 修复 …")
    for args in (["--upgrade"], []):
        try:
            p = subprocess.run([sys.executable, "-m", "ensurepip"] + args,
                               capture_output=True, text=True, timeout=300)
            if p.returncode == 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def pip_install(pkgs):
    """依次尝试各镜像安装；返回 (成功?, 输出摘要)。"""
    if not ensure_pip():
        return False, "当前 Python 环境没有 pip，且 ensurepip 修复失败。建议重新安装官方版 Python 并勾选 pip。"
    last = ""
    for name, idx in MIRRORS:
        cmd = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
        if idx:
            cmd += ["-i", idx, "--trusted-host", idx.split("//")[-1].split("/")[0]]
        cmd += list(pkgs)
        log(f"  → 用{name}安装：{' '.join(pkgs)}")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            out = (p.stdout or "") + (p.stderr or "")
            last = out[-1500:]
            if p.returncode == 0:
                return True, out
            tail = "\n".join([ln for ln in out.strip().splitlines()[-4:]])
            log(f"    失败（返回码 {p.returncode}）：{tail}")
            log("    换下一个源…")
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            log(f"    异常：{last}")
    return False, last


def ensure_venv():
    """当前不在虚拟环境里时，尝试创建 .venv 并重启自身（更干净，避免污染系统环境）。"""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return None
    venv_dir = os.path.join(HERE, ".venv")
    py = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin",
                      "python.exe" if os.name == "nt" else "python")
    if not os.path.exists(py):
        log(f"[1/3] 创建虚拟环境 .venv …")
        try:
            subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True,
                           capture_output=True, timeout=300)
        except Exception as e:  # noqa: BLE001
            log(f"     创建失败：{e}（改用当前环境安装依赖）")
            return None
    return py if os.path.exists(py) else None


def diag_report():
    info = py_info()
    missing = check_deps()
    path = os.path.join(HERE, "_诊断报告.txt")
    lines = ["SignStudio 诊断报告", "=" * 40]
    for k, v in info.items():
        lines.append(f"{k}: {v}")
    lines.append(f"缺失依赖: {[m[0] for m in missing] or '无'}")
    try:
        import pip  # noqa: F401
        lines.append("pip: 可用")
    except ImportError:
        lines.append("pip: 不可用（python -m ensurepip 可修复）")
    lines.append("")
    lines.append("系统 pip 版本：")
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True, timeout=60)
        lines.append((p.stdout or p.stderr or "").strip())
    except Exception as e:  # noqa: BLE001
        lines.append(f"(获取失败 {e})")
    lines.append("")
    lines.append("已安装的相关包：")
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True, timeout=120)
        for ln in (p.stdout or "").splitlines():
            if any(k in ln.lower() for k in ("mupdf", "fitz", "pikepdf", "pillow", "playwright")):
                lines.append("  " + ln)
    except Exception as e:  # noqa: BLE001
        lines.append(f"(获取失败 {e})")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("\n".join(lines))
    hr()
    log(f"诊断报告已保存：{path}")
    return 0


def main():
    args = set(sys.argv[1:])
    os.chdir(HERE)

    if "--diag" in args:
        return diag_report()

    if "--check-update" in args or "--update" in args:
        passthrough = ["--check"] if "--check-update" in args else ["--install"]
        p = subprocess.run([sys.executable, os.path.join(HERE, "updater.py")] + passthrough)
        return p.returncode

    info = py_info()
    hr()
    log("  SignStudio 启动检查")
    log(f"  版本   v{getattr(vmod, 'VERSION', '?')}")
    log(f"  Python {info['python']}  ({'虚拟环境' if info['in_venv'] else '系统环境'})")
    log(f"  路径   {info['executable']}")
    hr()

    missing = check_deps()
    if not missing:
        log("[依赖] 全部就绪 ✓")
    else:
        log(f"[依赖] 缺少：{', '.join(m[0] for m in missing)}")

    if "--check" in args:
        log("")
        log("自检结束。" + ("可以直接运行：python signstudio.py" if not missing else "请先安装依赖。"))
        return 0 if not missing else 1

    if missing:
        venv_py = ensure_venv()
        if venv_py and os.path.abspath(venv_py) != os.path.abspath(sys.executable):
            # 在新建的 .venv 里继续（重新执行本脚本）
            log("[2/3] 切换到 .venv 环境继续…")
            p = subprocess.run([venv_py, os.path.abspath(__file__)] + sys.argv[1:])
            return p.returncode
        log("[2/3] 正在安装依赖，请稍候（首次可能要 1~3 分钟）…")
        ok, out = pip_install([m[2] for m in missing])
        if not ok:
            hr("!")
            log("  依赖安装失败。可能原因：没有联网 / 公司代理 / 被安全软件拦截。")
            log("  你可以手动执行下面这条命令试试：")
            log(f'    "{sys.executable}" -m pip install {" ".join(m[2] for m in missing)} '
                f'-i https://pypi.tuna.tsinghua.edu.cn/simple')
            log("  下面是 pip 的最后输出：")
            log(out or "(无输出)")
            hr("!")
            return 1
        missing = check_deps()
        if missing:
            log(f"[!] 仍然缺少：{[m[0] for m in missing]}（可能装到了别的 Python 环境）")
            log(f'    请手动执行："{sys.executable}" -m pip install {" ".join(m[2] for m in missing)}')
            return 1
        log("[3/3] 依赖安装完成 ✓")

    hr()
    log("  正在启动图形界面…（浏览器会自动打开，关闭本窗口即退出）")
    hr()
    try:
        import signstudio
        # 把 --check/--diag 之外的参数透传给 signstudio（如 --port 9000 --no-browser）
        passthrough = [a for a in sys.argv[1:] if a not in ("--check", "--diag")]
        sys.argv = ["signstudio.py"] + passthrough
        signstudio.main()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        hr("!")
        log(f"  启动失败：{type(e).__name__}: {e}")
        log("  请把上面这段报错发给技术支持。")
        hr("!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
