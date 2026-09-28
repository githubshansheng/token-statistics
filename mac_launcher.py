#!/usr/bin/env python3
"""Tokscale Dashboard macOS 版启动器（免安装 .app）。

与 Windows 便携版(portable_launcher.py)行为对齐，按 macOS 惯例实现：
- 数据目录：~/Library/Application Support/TokscaleDashboard
  （可用 TOKSCALE_PORTABLE_DATA_DIR 环境变量覆盖，供隔离验收）
- 单实例：flock 数据目录锁文件；已有实例在跑时直接让位退出
- 桌面窗口：pywebview(WKWebView) 原生窗口；点关闭即退出（mac 惯例，无托盘常驻）
- 启动流程与 Windows 版一致：复制运行时 → 缓存面板直出 + 后台静默刷新
  （首次运行显示进度蒙版）→ 服务就绪后开窗口
- TOKSCALE_NO_BROWSER=1：无头模式，仅保持本地服务不开窗口（自动化验收用）

通用发行包不携带任何面板快照或用户统计数据；首次启动只扫描当前
macOS 用户的本地会话日志，数据文件均由运行时写入数据目录。
"""

import atexit
import fcntl
import os
import platform
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "TokscaleDashboard"
SUPPORTED_MACHINES = {"arm64", "aarch64", "x86_64", "amd64"}
REQUIRED_FILES = (
    "serve.py",
    "build_dashboard.py",
    "runtime_support.py",
    "tokscale",
)


def bundle_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_dir():
    override = os.environ.get("TOKSCALE_PORTABLE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    root = Path.home() / "Library" / "Application Support"
    return root / APP_NAME


def open_system_browser(url):
    """GUI 失败时，回退到系统浏览器打开面板。"""
    if os.environ.get("TOKSCALE_NO_BROWSER") != "1":
        try:
            webbrowser.open(url, new=1)
        except Exception as exc:
            write_log("系统浏览器打开失败：%r" % exc)


def run_desktop_window(url):
    """原生桌面窗口（WKWebView 内核）。

    点窗口关闭 = 退出程序（macOS 惯例）；后台服务随窗口关闭而停止。
    """
    import webview

    window = webview.create_window(
        "Tokscale 用量面板",
        url,
        width=1680,
        height=900,
        min_size=(1100, 640),
        background_color="#faf9f7",
    )
    # pywebview 要求在主线程启动事件循环；窗口关闭后 start() 返回。
    webview.start()
    write_log("桌面窗口已关闭，程序退出")


def _notify(text):
    """尝试用系统通知提示（失败静默，仅日志兜底）。"""
    try:
        import subprocess

        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "Tokscale 用量面板"'
             % text.replace('"', "'")],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def current_url():
    try:
        port = int((data_dir() / ".port").read_text(encoding="ascii").strip())
        if dashboard_is_ready(port):
            return f"http://127.0.0.1:{port}/"
    except Exception:
        pass
    return None


def dashboard_is_ready(port):
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/refresh/status",
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def write_log(text):
    try:
        root = data_dir()
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (root / "portable.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


def ensure_supported_platform():
    """macOS 专用启动器；原生二进制按 arm64/x86_64 由 npx 拉取对应包。"""
    if sys.platform != "darwin":
        raise RuntimeError("此应用仅支持 macOS 系统。")
    machine = platform.machine().lower()
    if machine and machine not in SUPPORTED_MACHINES:
        raise RuntimeError(
            "不支持的处理器架构 "
            f"({platform.machine() or '未知'})，需要 arm64 或 x86_64。"
        )


def _sync_file(source, target):
    """覆盖单个运行时文件；目标被占用时退避重试，仍失败且目标已存在则沿用旧文件。

    典型场景：上一实例的后台 tokscale 扫描子进程尚未退出，仍占用运行
    目录里的 tokscale 二进制（Text file busy）。此时沿用旧文件不影响
    本次运行（通常版本相同），下次启动会重新尝试同步。
    """
    last = None
    for _ in range(6):
        try:
            shutil.copy2(source, target)
            return
        except OSError as exc:
            # mac 上运行中的二进制被覆盖报 EBUSY/ETXTBSY(非 PermissionError)
            last = exc
            if "Text file busy" not in str(exc) and not isinstance(exc, PermissionError):
                raise
            time.sleep(0.5)
    if os.path.isfile(str(target)):
        write_log(
            "覆盖 %s 失败（文件被上一实例占用），本次沿用现有文件：%s"
            % (os.path.basename(str(target)), last)
        )
        return
    raise last


def ensure_runtime():
    src = bundle_dir()
    dst = data_dir()
    dst.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_FILES:
        source = src / name
        target = dst / name
        if not source.exists():
            raise FileNotFoundError(f"应用缺少资源：{name}")
        _sync_file(source, target)
    return dst


def migrate_legacy_tps_dashboard(runtime):
    """一次性把含不准确 TPS 指标的旧缓存重建为当前模板。

    仅复用本机已经存在的 graph.json，不触发第二次 tokscale 扫描；重建失败时
    保留旧页备份并让常规后台刷新接管，避免继续向用户展示已废弃指标。
    """
    dashboard = runtime / "dashboard.html"
    try:
        text = dashboard.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    markers = ("模型平均速度 TOP12", "平均TPS", "平均 TPS")
    if not any(marker in text for marker in markers):
        return
    graph = runtime / "graph.json"
    if graph.is_file():
        try:
            import runpy

            runtime_text = str(runtime)
            if runtime_text not in sys.path:
                sys.path.insert(0, runtime_text)
            runpy.run_path(str(runtime / "build_dashboard.py"), run_name="__main__")
            rebuilt = dashboard.read_text(encoding="utf-8", errors="replace")
            if any(marker in rebuilt for marker in markers):
                raise RuntimeError("重建后的面板仍包含旧 TPS 指标")
            write_log("已使用本机缓存数据升级面板并移除平均 TPS 指标")
            return
        except Exception as exc:
            write_log("旧 TPS 面板缓存即时升级失败，将由后台刷新重建：%r" % exc)
    try:
        backup = runtime / "dashboard.pre-v171.html"
        if not backup.exists():
            shutil.copy2(dashboard, backup)
        dashboard.write_text(
            "<!doctype html><meta charset='utf-8'><title>Tokscale 用量面板</title>"
            "<style>body{font-family:-apple-system,PingFang SC,sans-serif;padding:48px;"
            "color:#444;background:#faf9f7}p{color:#777}</style>"
            "<h2>正在升级本地数据面板…</h2><p>已移除不准确的平均 TPS 指标，"
            "后台重建完成后将自动显示最新数据。</p>"
            "<script>setTimeout(()=>location.reload(),2000)</script>",
            encoding="utf-8",
        )
    except OSError as exc:
        write_log("旧 TPS 面板缓存隔离失败：%r" % exc)


_lock_handle = None


def acquire_lock():
    """flock 单实例锁；同一数据目录只允许一个实例。返回锁文件句柄或 None。"""
    global _lock_handle
    root = data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = (root / ".lock").open("w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        _lock_handle = handle
        return handle
    except Exception as exc:
        write_log("单实例锁创建失败（忽略，允许继续启动）：%r" % exc)
        return "degraded"


def close_lock(handle):
    global _lock_handle
    if handle and handle != "degraded":
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        except Exception:
            pass
    _lock_handle = None


def port_is_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.35):
            return True
    except OSError:
        return False


def choose_port():
    preferred = 8765
    if not port_is_open(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    lock = acquire_lock()
    if not lock:
        # 已有实例在跑：把已有面板带出来（若就绪），本次让位退出。
        url = current_url()
        write_log("检测到已有实例运行，本次让位退出" + ("，已打开现有面板" if url else ""))
        if url and os.environ.get("TOKSCALE_NO_BROWSER") != "1":
            open_system_browser(url)
        return 0

    try:
        ensure_supported_platform()
        runtime = ensure_runtime()
        migrate_legacy_tps_dashboard(runtime)
        port = choose_port()
        (runtime / ".port").write_text(str(port), encoding="ascii")
        url = f"http://127.0.0.1:{port}/"
        write_log(
            f"启动 macOS 免安装版，端口 {port}，系统 {platform.platform()}，"
            f"架构 {platform.machine()}"
        )
        os.chdir(str(runtime))
        os.environ.update({
            "TOKSCALE_PORT": str(port),
            "TOKSCALE_RUNTIME_DIR": str(runtime),
            "PYTHONUTF8": "1",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        })
        sys.argv = [str(runtime / "serve.py"), str(port)]
        import serve
        # serve.py 默认 allow_reuse_address=False（Windows 保守语义）。
        # macOS 上进程被杀后端口进 TIME_WAIT，严格绑定会 EADDRINUSE
        # 导致二次启动失败；BSD 语义下 SO_REUSEADDR 仅放行 TIME_WAIT
        # 重绑，不会像 Windows 那样允许双活监听，平台内覆盖是安全的。
        serve.Server.allow_reuse_address = True
        from serve import Handler, Server

        httpd = Server(("127.0.0.1", port), Handler)
        atexit.register(httpd.server_close)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        # 每次启动都后台静默重扫一次：单飞不阻塞首屏，
        # 把上次数据更新为本机最新数据。
        from serve import start_refresh
        # 有缓存面板(上次扫描结果落盘)→ 直接读缓存首屏 + 静默刷新；
        # 无任何缓存 → 首次扫描，显示进度蒙版。
        dash = runtime / "dashboard.html"
        has_cache = dash.is_file() and dash.stat().st_size > 0
        if has_cache:
            write_log("读取本地缓存面板，后台静默更新数据")
            start_refresh("silent")
        else:
            write_log("首次运行：开始扫描当前用户的本机 AI 会话记录")
            start_refresh("first")
        if not wait_ready(url, timeout=30):
            httpd.shutdown()
            raise RuntimeError("本地面板服务启动失败，请重新运行程序。")
        write_log(f"服务就绪：{url}")

        if os.environ.get("TOKSCALE_NO_BROWSER") == "1":
            # 无头验收模式：不弹窗口不开浏览器，仅保持后台服务。
            server_thread.join()
            return 0

        try:
            run_desktop_window(url)  # 阻塞直到用户关闭窗口
            write_log("停止本地面板服务")
            httpd.shutdown()
            httpd.server_close()
            server_thread.join(timeout=5)
            return 0
        except Exception as gui_exc:
            write_log("桌面窗口启动失败，回退系统浏览器：\n"
                      + "".join(traceback.format_exception_only(type(gui_exc), gui_exc)))
            _notify("桌面窗口组件加载失败，已改用浏览器打开")
            open_system_browser(url)
            server_thread.join()
            return 0
    except Exception as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        write_log("启动失败：\n" + detail)
        _notify("启动失败：%s（详见数据目录 portable.log）" % exc)
        print(detail, file=sys.stderr)
        return 1
    finally:
        close_lock(_lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
