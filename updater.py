#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""updater.py —— SignStudio 自动更新内核（纯标准库，Windows/Linux/macOS 通用）

职责
  1. check_latest()     读 NAS 镜像上的 latest.json（局域网 → 公网 HTTPS 兜底），比较版本
  2. download_package() 边下边算 sha256，校验不一致直接拒绝
  3. apply_package()    解压整包 → 备份旧文件 → 覆盖程序文件（绝不碰用户数据目录）
  4. install_helper()   独立进程：等旧进程退出 → 装包 → 用同样的端口重新拉起新版
       （放在独立进程里做，避免“边服务边改自己的代码”导致半装状态）

清单（latest.json）格式
  {
    "app": "SignStudio", "version": "1.1.0", "published": "2026-10-23 18:00:00",
    "notes": "本次更新内容…",
    "package": "SignStudio-v1.1.0-all.zip",   # 必须是纯 ASCII 文件名
    "package_size": 2345678, "package_sha256": "…",
    "webapp": "SignStudioZeroInstall-v1.1.0.zip", "webapp_size": …, "webapp_sha256": "…",
    "sig": "Ed25519(raw32|msg) 的 base64（对上面除 sig 外的规范 JSON）"
  }

命令行（一般不用手敲，界面上点「立即更新」即可）
  python updater.py --check                只检查，不安装
  python updater.py --install              检查并安装（会重启界面）
  python updater.py --install-helper --pid <父进程PID> --zip <包> --port <端口> --here <目录>
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import version as vmod
    CURRENT = vmod.VERSION
    DEFAULT_MANIFESTS = list(vmod.DEFAULT_MANIFESTS)
    PUBKEY = getattr(vmod, "UPDATE_PUBKEY", "")
    USER_DATA_DIRS = tuple(getattr(vmod, "USER_DATA_DIRS", ("projects",)))
    APP_TITLE = getattr(vmod, "APP_TITLE", "SignStudio")
except Exception:                                     # 兜底，永不因版本模块报错而无法更新
    CURRENT = "0.0.0"
    DEFAULT_MANIFESTS = ["https://example.com/download/signstudio/latest.json"]
    PUBKEY = ""
    USER_DATA_DIRS = ("projects",)
    APP_TITLE = "SignStudio"

UA = "SignStudio-Updater/%s" % CURRENT
BACKUP_ROOT = os.path.join(HERE, ".update_backup")
LOG_PATH = os.path.join(HERE, "update.log")


class UpdateError(RuntimeError):
    pass


# ---------------------------------------------------------------- 日志 / 版本
def ulog(msg):
    """更新过程写 update.log，出问题让用户把这一行文件发过来即可。"""
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line


def parse_version(text):
    return vmod.version_tuple(text) if hasattr(vmod, "version_tuple") else _fallback_tuple(text)


def _fallback_tuple(text):
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


def is_newer(latest, current=None):
    return parse_version(latest) > parse_version(current or CURRENT)


# ---------------------------------------------------------------- HTTP
def _ascii_url(url):
    """URL 里的中文/空格等非 ASCII 必须百分号编码，否则 urllib 抛 ascii codec 错误。"""
    try:
        p = urllib.parse.urlsplit(url)
        path = urllib.parse.quote(p.path, safe="/%:@&=+$,-_.!~*'()")
        query = urllib.parse.quote(p.query, safe="=&%:/?@+")
        return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))
    except Exception:
        return url


def _get(url, timeout=8, as_json=True):
    req = urllib.request.Request(_ascii_url(url), headers={
        "User-Agent": UA, "Accept": "application/json, */*", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise UpdateError("HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise UpdateError("连不上（%s）" % (getattr(e, "reason", e),))
    except Exception as e:                                    # noqa: BLE001
        raise UpdateError(str(e))
    if not as_json:
        return raw
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise UpdateError("返回内容不是 JSON（可能被强制门户/代理拦了）")


# ---------------------------------------------------------------- 清单签名
def _canonical(obj):
    return json.dumps({k: v for k, v in obj.items() if k != "sig"},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signature(manifest):
    """返回 (状态, 说明)，状态 ∈ {'ok','unsigned','missing_lib','bad'}"""
    sig = (manifest.get("sig") or "").strip()
    if not sig:
        return "unsigned", "清单未签名（仅靠 HTTPS + sha256 校验）"
    if not PUBKEY or PUBKEY.startswith("REPLACE_WITH"):
        return "unsigned", "本地未内置发布公钥（仅靠 HTTPS + sha256 校验）"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:                                          # noqa: BLE001
        return "missing_lib", "缺少 cryptography，无法校验清单签名"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBKEY))
        pub.verify(base64.b64decode(sig), _canonical(manifest))
        return "ok", "清单签名校验通过"
    except Exception as e:                                      # noqa: BLE001
        return "bad", "清单签名校验失败：%s" % e


# ---------------------------------------------------------------- 检查更新
def manifest_urls(custom=None):
    """更新源顺序：显式参数 > 环境变量 SIGNSTUDIO_UPDATE_MANIFESTS > version.py 默认。"""
    if custom:
        urls = [u.strip() for u in str(custom).replace(";", ",").split(",") if u.strip()]
        if urls:
            return urls
    env = (os.environ.get("SIGNSTUDIO_UPDATE_MANIFESTS") or "").strip()
    if env:
        urls = [u.strip() for u in env.replace(";", ",").split(",") if u.strip()]
        if urls:
            return urls
    return list(DEFAULT_MANIFESTS)


def _read_manifest(url, timeout=8):
    data = _get(url, timeout=timeout)
    ver = str(data.get("version") or "").lstrip("vV")
    pkg = data.get("package") or data.get("zip") or ""
    if not ver or not pkg:
        raise UpdateError("清单缺 version/package 字段")
    sig_state, sig_msg = verify_signature(data)
    if sig_state == "bad":
        raise UpdateError(sig_msg)
    return {
        "version": ver,
        "published": data.get("published") or "",
        "notes": (data.get("notes") or "").strip(),
        "package": pkg,
        "package_url": _ascii_url(urllib.parse.urljoin(url, pkg)),
        "size": int(data.get("package_size") or 0),
        "sha256": (data.get("package_sha256") or "").strip().lower(),
        "webapp": data.get("webapp") or "",
        "webapp_url": _ascii_url(urllib.parse.urljoin(url, data["webapp"])) if data.get("webapp") else "",
        "webapp_size": int(data.get("webapp_size") or 0),
        "webapp_sha256": (data.get("webapp_sha256") or "").strip().lower(),
        "sig_state": sig_state,
        "sig_msg": sig_msg,
        "source": url,
        "source_host": urllib.parse.urlparse(url).netloc,
    }


def check_latest(custom=None, timeout=8):
    """按顺序试各更新源。返回 dict：
       {current, latest, has_update, notes, size, sha256, package_url, source, sig_state, sig_msg, checked_at}
       全部失败抛 UpdateError。"""
    errors = []
    for url in manifest_urls(custom):
        host = urllib.parse.urlparse(url).netloc or url
        try:
            info = _read_manifest(url, timeout=timeout)
        except Exception as e:                                  # noqa: BLE001
            errors.append("%s → %s" % (host, e))
            continue
        info["current"] = CURRENT
        info["has_update"] = is_newer(info["version"])
        info["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if info["has_update"] and not info["sha256"]:
            info["has_update"] = False
            info["warning"] = "清单里没有 package_sha256，为安全起见不提示更新"
        return info
    raise UpdateError("所有更新源都连不上：\n" + "\n".join("  · " + x for x in errors))


# ---------------------------------------------------------------- 下载
def download(url, dest, progress=None, timeout=60):
    """下载并实时算 sha256；返回 (字节数, sha256)。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(_ascii_url(url), headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
    except Exception as e:                                      # noqa: BLE001
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise UpdateError("下载失败：%s" % e)
    if got < 4096:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise UpdateError("下载文件过小（%d 字节），可能被网络拦截" % got)
    os.replace(tmp, dest)
    return got, h.hexdigest()


def staged_zip(ver=""):
    name = "SignStudio-pkg" + (("-v" + str(ver).lstrip("vV")) if ver else "") + ".zip"
    return os.path.join(tempfile.gettempdir(), name)


def fetch_package(info, progress=None):
    """下载整包并校验 sha256，返回本地 zip 路径。"""
    dest = staged_zip(info.get("version"))
    size, sha = download(info["package_url"], dest, progress=progress)
    want = (info.get("sha256") or "").lower()
    if want and sha != want:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise UpdateError("安装包 sha256 校验不一致（期望 %s…，实际 %s…），已拒绝安装"
                          % (want[:12], sha[:12]))
    if want:
        ulog("下载完成 %d 字节，sha256 校验通过" % size)
    else:
        ulog("下载完成 %d 字节（清单无 sha256，未校验）" % size)
    return dest


# ---------------------------------------------------------------- 安装（覆盖旧版）
def _safe_members(zf):
    """挡掉绝对路径 / ../ 逃逸的恶意 zip。"""
    out = []
    for m in zf.infolist():
        name = m.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise UpdateError("安装包里有非法路径：%s" % m.filename)
        out.append(m)
    return out


def _strip_toplevel(names):
    """包内可能有一层顶级目录（SignStudio/…），统一剥掉。"""
    tops = {n.split("/")[0] for n in names if n and not n.endswith("/")}
    nested = [n for n in names if "/" in n]
    if len(tops) == 1 and nested:
        top = tops.pop()
        return top + "/"
    return ""


def apply_package(zip_path, target=None, version_tag=""):
    """把整包覆盖到程序目录。返回 {ok, files, backup, skipped}"""
    target = os.path.abspath(target or HERE)
    stamp = "%s_%s" % (version_tag or "new", time.strftime("%Y%m%d_%H%M%S"))
    backup = os.path.join(BACKUP_ROOT, stamp)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = _safe_members(zf)
            prefix = _strip_toplevel([m.filename.replace("\\", "/") for m in members])
            written, backed = [], []
            for m in members:
                name = m.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue
                rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
                if not rel:
                    continue
                top = rel.split("/")[0]
                if top in USER_DATA_DIRS:                     # 用户数据/虚拟环境一律不碰
                    continue
                dst = os.path.join(target, rel.replace("/", os.sep))
                # 旧文件先备份一份，出问题能手动退回
                if os.path.isfile(dst):
                    bdst = os.path.join(backup, rel.replace("/", os.sep))
                    try:
                        os.makedirs(os.path.dirname(bdst), exist_ok=True)
                        shutil.copy2(dst, bdst)
                        backed.append(rel)
                    except Exception as e:                    # noqa: BLE001
                        ulog("备份失败（继续安装）：%s → %s" % (rel, e))
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                with zf.open(m) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(rel)
    except UpdateError:
        raise
    except Exception as e:                                      # noqa: BLE001
        raise UpdateError("解压安装失败：%s" % e)
    ulog("已覆盖 %d 个文件（备份 %d 个到 %s）" % (len(written), len(backed), backup))
    return {"ok": True, "files": written, "backup": backup if backed else "", "skipped_userdata": True}


def rollback(backup_dir, target=None):
    """把备份目录里的文件写回去（界面上「出问题？一键退回」用）。"""
    target = os.path.abspath(target or HERE)
    if not os.path.isdir(backup_dir):
        raise UpdateError("备份目录不存在：%s" % backup_dir)
    n = 0
    for root, _d, files in os.walk(backup_dir):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, backup_dir)
            dst = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    ulog("已回滚 %d 个文件（来自 %s）" % (n, backup_dir))
    return n


# ---------------------------------------------------------------- 进程等待 / 重启
def _gone(pid):
    stat = "/proc/%d/stat" % pid
    if os.path.exists(stat):                    # Linux：僵尸态也算退出
        try:
            with open(stat) as f:
                st = f.read().split(") ", 1)[-1]
            if st[:1] in ("Z", "X"):
                return True
        except Exception:
            pass
        return False
    try:
        os.kill(int(pid), 0)
        return False
    except OSError:
        return True
    except Exception:                                          # noqa: BLE001
        return True


def wait_pid_exit(pid, timeout=90):
    if not pid or int(pid) <= 0:
        return True
    if os.name == "nt":
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not h:
                return True
            try:
                return k32.WaitForSingleObject(h, int(timeout * 1000)) == 0
            finally:
                k32.CloseHandle(h)
        except Exception as e:                                 # noqa: BLE001
            ulog("Windows 等进程失败，改轮询：%s" % e)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _gone(int(pid)):
            return True
        time.sleep(0.4)
    return False


def spawn_detached(cmd):
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        kw["close_fds"] = True
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def helper_cmd(zip_path, parent_pid, port, target=None, version_tag=""):
    return [sys.executable, os.path.join(HERE, "updater.py"), "--install-helper",
            "--pid", str(int(parent_pid)), "--zip", zip_path,
            "--port", str(int(port or 0)), "--here", os.path.abspath(target or HERE),
            "--version", str(version_tag or "")]


def install_and_restart(zip_path, port=0, target=None, delay=0.8, version_tag=""):
    """从**正在服务的进程**调用：起一个独立安装进程，然后本进程由调用方退出。"""
    if getattr(vmod, "UPDATE_MANUAL", True):
        raise UpdateError("当前为手动更新模式（UPDATE_MANUAL=True）：程序不会自动覆盖文件，"
                          "请用「下载更新包」后手工解压覆盖。")
    cmd = helper_cmd(zip_path, os.getpid(), port, target, version_tag)
    ulog("启动安装进程：%s" % " ".join(cmd))
    spawn_detached(cmd)
    return cmd


def install_helper(parent_pid, zip_path, port=0, target=None, version_tag=""):
    """★ 由独立安装进程执行：等旧进程退出 → 覆盖文件 → 用同一端口拉起新版。"""
    target = os.path.abspath(target or HERE)
    ulog("=" * 56)
    ulog("安装进程启动：parent=%s zip=%s target=%s" % (parent_pid, zip_path, target))
    if not wait_pid_exit(parent_pid, timeout=120):
        ulog("× 等旧进程退出超时（PID %s），已中止" % parent_pid)
        return 4
    ulog("旧进程已退出")
    try:
        info = apply_package(zip_path, target, version_tag=version_tag)
        ulog("安装完成：%d 个文件" % len(info["files"]))
    except Exception as e:                                      # noqa: BLE001
        ulog("× 安装失败：%s" % e)
        _msgbox("更新失败", "新版本安装失败：\n%s\n\n请手动解压新版本覆盖。\n日志：%s" % (e, LOG_PATH))
        _relaunch(target, port)                                 # 装失败也要把旧版拉起来
        return 6
    try:
        _relaunch(target, port)
    except Exception as e:                                      # noqa: BLE001
        ulog("× 重启失败：%s" % e)
        _msgbox("更新完成", "新版本已装好，但自动重启失败。\n请手动双击开始脚本打开。")
        return 7
    ulog("安装流程完成")
    return 0


def _relaunch(target, port=0):
    start_py = os.path.join(target, "start.py")
    cmd = [sys.executable, start_py]
    if port:
        cmd += ["--port", str(int(port))]
    cmd += ["--no-browser"]                                     # 浏览器里那页会自己刷新
    ulog("重启：%s" % " ".join(cmd))
    spawn_detached(cmd)


def _msgbox(title, text):
    """打包成无控制台形态时，失败也要让用户看见。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)
    except Exception:                                          # noqa: BLE001
        print("%s: %s" % (title, text))


# ---------------------------------------------------------------- CLI
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(description="SignStudio 更新工具")
    ap.add_argument("--check", action="store_true", help="只检查是否有新版本")
    ap.add_argument("--download", action="store_true", help="只下载更新包到本机，不安装")
    ap.add_argument("--install", action="store_true", help="检查并安装（会重启界面；手动更新模式下被拒绝）")
    ap.add_argument("--install-helper", action="store_true", help="（内部用）独立安装进程")
    ap.add_argument("--rollback", default="", help="用指定备份目录回滚")
    ap.add_argument("--manifest", default="", help="自定义清单地址（逗号分隔）")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--zip", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--here", default="")
    ap.add_argument("--version", default="", help="（内部用）目标版本号，用于备份目录命名")
    args = ap.parse_args(argv)

    if args.install_helper:
        return install_helper(args.pid, args.zip, args.port, args.here, args.version)

    if args.rollback:
        n = rollback(args.rollback, args.here or None)
        print("已回滚 %d 个文件，请重新启动程序。" % n)
        return 0

    try:
        info = check_latest(args.manifest or None)
    except UpdateError as e:
        print("× 检查更新失败：%s" % e)
        return 2
    print("当前版本 v%s，最新版本 v%s（来源 %s）" % (CURRENT, info["version"], info["source_host"]))
    print("签名状态：%s" % info["sig_msg"])
    if not info["has_update"]:
        print("已是最新版本。")
        return 0
    print("\n发现新版本 v%s（%s，%.1f KB）" % (info["version"], info["published"],
                                            info["size"] / 1024.0))
    if info["notes"]:
        print("更新内容：" + info["notes"].replace("\n", "\n  "))
    if not args.install and not args.download:
        print("\n手动更新模式：要拿更新包请运行：python updater.py --download")
        print("下载后自行解压覆盖程序文件，再重新启动工具（本工具不会自动覆盖）。")
        return 0
    try:
        zip_path = fetch_package(info)
    except UpdateError as e:
        print("× 下载失败：%s" % e)
        return 3
    if args.download:
        print("\n更新包已下载：%s" % zip_path)
        print("请手动解压，把里面的文件覆盖到程序文件夹（同名文件选「替换」），再重新启动工具。")
        return 0
    if getattr(vmod, "UPDATE_MANUAL", True):
        print("× 当前为手动更新模式（version.py: UPDATE_MANUAL=True），已拒绝自动安装。")
        print("  更新包已在：%s  —— 请自行解压覆盖程序文件。" % zip_path)
        return 0
    print("安装包已下载：%s" % zip_path)
    try:
        install_and_restart(zip_path, port=args.port, version_tag=info["version"])
        print("安装进程已启动，请退出当前窗口（几秒后新版会自动起来）。")
    except UpdateError as e:
        print("× 安装失败：%s" % e)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
