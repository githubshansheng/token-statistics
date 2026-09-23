#!/usr/bin/env python3
"""Tokscale Dashboard Windows 便携版启动器。"""

import atexit
import ctypes
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
MUTEX_NAME = "Local\\TokscaleDashboardPortable"  # 实际名称按数据目录哈希派生，见 acquire_mutex()
SUPPORTED_MACHINES = {"amd64", "x86_64", "arm64", "aarch64"}
REQUIRED_FILES = (
    "serve.py",
    "build_dashboard.py",

    "runtime_support.py",
    "tokscale.exe",
)


def bundle_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_dir():
    override = os.environ.get("TOKSCALE_PORTABLE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / APP_NAME


def open_system_browser(url):
    """WebView2 缺失或 GUI 失败时，回退到系统浏览器打开面板。"""
    if os.environ.get("TOKSCALE_NO_BROWSER") != "1":
        webbrowser.open(url, new=1)


def webview2_runtime_version():
    """返回已安装的 WebView2 运行时版本号；未安装返回 None。

    Win10 21H2+/Win11 一般出厂自带；缺失时可安装 Evergreen 运行时。
    """
    import winreg
    keys = (
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in keys:
            try:
                v = winreg.QueryValueEx(winreg.OpenKey(hive, path), "pv")[0]
                if v:
                    return v
            except OSError:
                pass
    return None


def run_desktop_window(url):
    """原生桌面窗口（WebView2 内核）+ 托盘常驻。

    点窗口关闭 = 最小化到托盘（首次给出气泡提示）；托盘菜单：显示面板 /
    开机自启开关 / 退出程序。后台服务与数据更新随进程常驻。
    """
    import threading
    import webview

    window = webview.create_window(
        "Tokscale 用量面板",
        url,
        width=1680,
        height=900,
        min_size=(1100, 640),
        background_color="#faf9f7",
    )

    state = {"quit": False, "tray": None, "hinted": False}

    def show_panel(icon=None, item=None):
        if state["quit"]:
            return
        try:
            window.show()
        except Exception as exc:
            write_log("托盘显示面板失败：%r" % exc)

    def stop_tray(icon=None):
        tray = icon or state["tray"]
        if tray is None:
            return
        try:
            tray.stop()
        except Exception as exc:
            write_log("托盘停止失败：%r" % exc)

    def quit_app(icon=None, item=None):
        # 必须先进入退出态。window.destroy() 会再次触发 closing 事件；
        # on_closing 看到退出态后放行，否则会被“关闭即隐藏”逻辑取消。
        if state["quit"]:
            return
        state["quit"] = True
        write_log("收到托盘退出命令，正在关闭程序")
        stop_tray(icon)
        try:
            window.destroy()
        except Exception as exc:
            write_log("托盘退出时关闭窗口失败：%r" % exc)

    def toggle_autostart(icon, item):
        enable = not autostart_enabled()
        try:
            set_autostart(enable)
            write_log("开机自启：" + ("开启" if enable else "关闭"))
        except Exception as exc:
            write_log("开机自启设置失败：%r" % exc)

    def on_closing():
        if state["quit"]:
            # 返回值不是 False，允许托盘“退出”触发的窗口销毁继续执行。
            return True

        # 普通点击窗口关闭按钮：返回 False 取消关闭，隐藏到托盘常驻。
        try:
            window.hide()
        except Exception as exc:
            write_log("窗口隐藏到托盘失败：%r" % exc)
        if not state["hinted"]:
            state["hinted"] = True
            try:
                if state["tray"] is not None:
                    state["tray"].notify(
                        "面板已最小化到托盘，数据仍在后台自动更新；右键托盘图标可退出",
                        "Tokscale 用量面板",
                    )
            except Exception:
                pass
        return False

    window.events.closing += on_closing

    def run_tray():
        try:
            import pystray
            from pystray import Menu, MenuItem

            menu = Menu(
                MenuItem("显示面板", show_panel, default=True),
                MenuItem("开机自启",
                         toggle_autostart,
                         checked=lambda item: autostart_enabled()),
                MenuItem("退出程序", quit_app),
            )
            icon = pystray.Icon("TokscaleDashboard", _tray_image(),
                                "Tokscale 用量面板", menu)
            state["tray"] = icon
            icon.run()
        except Exception as exc:
            write_log("托盘不可用，直接关闭窗口即退出：%r" % exc)

    threading.Thread(target=run_tray, daemon=True).start()
    webview.start(gui="edgechromium")
    # webview.start 返回 = 窗口已真正销毁。无论返回路径如何都清理托盘，
    # 避免异常关闭后残留任务栏图标。
    stop_tray()
    write_log("桌面窗口已关闭" + ("，程序退出" if state["quit"] else ""))


AUTOSTART_NAME = "TokscaleDashboard"


def _exe_path():
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def autostart_enabled():
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if enable:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ,
                              '"%s"' % _exe_path())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, AUTOSTART_NAME)
        except FileNotFoundError:
            pass


def _tray_image():
    """程序化生成托盘图标（无需外部资源文件）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(44, 44, 42, 255))
    draw.ellipse([14, 16, 34, 36], fill=(55, 138, 221, 255))
    draw.ellipse([30, 24, 50, 44], fill=(29, 158, 117, 255))
    draw.rectangle([14, 46, 50, 51], fill=(250, 249, 247, 255))
    return img


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
    """便携包内置 x64 程序；支持 x64 Windows 及可运行 x64 应用的 ARM64 Windows。"""
    if os.name != "nt":
        raise RuntimeError("此便携版仅支持 Windows 系统。")
    machine = platform.machine().lower()
    # machine 可能为空(如极简环境下 PROCESSOR_ARCHITECTURE 缺失)：
    # 此时依赖指针宽度判断——32 位 Windows 根本无法加载 x64 EXE。
    if machine and machine not in SUPPORTED_MACHINES:
        raise RuntimeError(
            "此文件需要 64 位 Windows，不支持当前处理器架构 "
            f"({platform.machine() or '未知'})。"
        )
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("此便携版需要 64 位 Windows，不能在 32 位系统中运行。")


def _sync_file(source, target):
    """覆盖单个运行时文件；目标被占用时退避重试，仍失败且目标已存在则沿用旧文件。

    典型场景：用户从托盘退出面板后立即重开，上一实例的后台 tokscale 扫描
    子进程尚未退出，仍占用运行目录里的 tokscale.exe（WinError 32）。此时
    沿用旧文件不影响本次运行（通常版本相同），下次启动会重新尝试同步。
    """
    last = None
    for _ in range(6):
        try:
            shutil.copy2(source, target)
            return
        except PermissionError as exc:
            last = exc
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
            raise FileNotFoundError(f"便携版缺少资源：{name}")
        _sync_file(source, target)
    # 打包时内置的面板快照（可选资源）：仅当本机还没有面板时用它做首屏，
    # 随后的后台刷新会重扫本机数据覆盖它；已有面板的用户不受影响。
    bundled_dash = src / "dashboard.html"
    target_dash = dst / "dashboard.html"
    if bundled_dash.is_file() and not target_dash.exists():
        try:
            shutil.copy2(bundled_dash, target_dash)
        except PermissionError as exc:
            write_log("内置面板快照落盘失败（文件被占用），将由后台刷新重建：%r" % exc)
        else:
            write_log("使用打包内置面板快照作为首屏，后台将重扫本机数据")
    return dst


def migrate_legacy_tps_dashboard(runtime):
    """一次性把含不准确 TPS 指标的旧缓存重建为当前模板。

    仅复用本机已经存在的 graph.json，不触发第二次 tokscale 扫描；重建失败时
    保留旧页备份并让常规后台刷新接管，避免继续向用户展示已废弃指标。
    旧 models-perf.json 保留但不再读取，避免升级过程删除用户磁盘文件。
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
            "<style>body{font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:48px;"
            "color:#444;background:#faf9f7}p{color:#777}</style>"
            "<h2>正在升级本地数据面板…</h2><p>已移除不准确的平均 TPS 指标，"
            "后台重建完成后将自动显示最新数据。</p>"
            "<script>setTimeout(()=>location.reload(),2000)</script>",
            encoding="utf-8",
        )
    except OSError as exc:
        write_log("旧 TPS 面板缓存隔离失败：%r" % exc)


def acquire_mutex():
    if os.name != "nt":
        return None, True
    # 互斥体按数据目录隔离：同一数据目录只允许一个实例；
    # 不同数据目录(如隔离验收)互不干扰。
    import hashlib
    tag = hashlib.md5(str(data_dir()).encode("utf-8", "replace")).hexdigest()[:10]
    name = f"Local\\TokscaleDashboardPortable-{tag}"
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None, True
    already_exists = kernel32.GetLastError() == 183
    return handle, not already_exists


def close_mutex(handle):
    if handle and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(handle)


def kill_stale_instances():
    """重复启动时查杀已在运行的旧实例进程。

    冻结版按 EXE 名称前缀匹配（去掉版本后缀，可同时覆盖旧版本 EXE 名），
    并清理运行数据目录下残留的 tokscale 扫描子进程；源码运行仅匹配
    命令行含 portable_launcher.py 的 python 进程，避免误伤其他程序。
    """
    if os.name != "nt":
        return
    import re
    import subprocess

    my_pid = os.getpid()
    if getattr(sys, "frozen", False):
        name = os.path.basename(sys.executable)
        stem = name[:-4] if name.lower().endswith(".exe") else name
        cut = stem.find("-v")
        if cut > 0 and re.match(r"v[\d.]+(-|$)", stem[cut + 1:]):
            stem = stem[:cut]
        proc_match = "$_.Name -like '%s*'" % stem
    else:
        proc_match = ("$_.Name -match '^python(w)?\\.exe$' -and "
                      "$_.CommandLine -like '*portable_launcher.py*'")
    scan_match = ("$_.Name -ieq 'tokscale.exe' -and "
                  "$_.CommandLine -like '*%s*'" % data_dir())
    # 排除自身进程树（venv shim / PyInstaller 引导进程都是「父+子」两级），
    # 只杀旧实例；祖先即使不是本程序（如 cmd）也在排除集合里，无副作用。
    ps = (
        "$mine=%d;" % my_pid
        + "$excl=@($mine);"
        + "$cur=(Get-CimInstance Win32_Process -Filter \"ProcessId=$mine\").ParentProcessId;"
        + "for($i=0;$i -lt 3 -and $cur;$i++){ $excl+=$cur; "
        + "$cur=(Get-CimInstance Win32_Process -Filter \"ProcessId=$cur\").ParentProcessId }"
        + "Get-CimInstance Win32_Process | Where-Object { "
        + "($_.ProcessId -ne $mine) -and ($excl -notcontains $_.ProcessId) "
        + "-and ( (%s) -or (%s) ) } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction SilentlyContinue }" % (proc_match, scan_match)
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        write_log("查杀旧实例进程失败：%r" % exc)


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


def message(title, text, error=False):
    if os.name == "nt":
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def main():
    mutex, first = acquire_mutex()
    atexit.register(close_mutex, mutex)
    if not first:
        # 需求：重复启动时先查杀已启动的旧实例，再正常启动新实例。
        write_log("检测到已有实例，先查杀旧进程再启动")
        kill_stale_instances()
        # 旧实例退出后互斥体才会释放；先关闭本进程句柄再轮询重建。
        atexit.unregister(close_mutex)
        close_mutex(mutex)
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.4)
            mutex, first = acquire_mutex()
            if first:
                atexit.register(close_mutex, mutex)
                write_log("旧实例已结束，继续启动")
                break
        if not first:
            # 旧进程未能结束（如权限不足），回退为提示，避免双实例并存。
            mutex, first = acquire_mutex()
            atexit.register(close_mutex, mutex)
            url = current_url()
            if url:
                write_log("旧进程未能结束：面板仍在运行，保持现有桌面窗口。")
            elif os.environ.get("TOKSCALE_NO_BROWSER") == "1":
                # 自动化验收模式：不弹阻塞对话框
                write_log("旧进程未能结束(验收模式)：本次直接退出。")
            else:
                message(
                    "Tokscale 用量面板",
                    "旧进程结束失败，面板可能仍在运行。"
                    "请结束旧进程后重试。",
                    error=True,
                )
            return 0

    try:
        ensure_supported_platform()
        runtime = ensure_runtime()
        migrate_legacy_tps_dashboard(runtime)
        port = choose_port()
        (runtime / ".port").write_text(str(port), encoding="ascii")
        url = f"http://127.0.0.1:{port}/"
        write_log(
            f"启动通用便携版，端口 {port}，系统 {platform.platform()}，"
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
        from serve import Handler, Server

        httpd = Server(("127.0.0.1", port), Handler)
        atexit.register(httpd.server_close)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        # 每次启动都后台静默重扫一次：单飞不阻塞首屏，
        # 把打包快照（或上次数据）更新为本机最新数据。
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
            # 自动化验收模式：不弹窗口不开浏览器，仅保持后台服务。
            server_thread.join()
            return 0

        wv2 = webview2_runtime_version()
        if wv2:
            write_log(f"桌面窗口：WebView2 {wv2}")
            try:
                run_desktop_window(url)  # 阻塞直到用户从托盘退出
                write_log("停止本地面板服务")
                httpd.shutdown()
                httpd.server_close()
                server_thread.join(timeout=5)
                return 0
            except Exception as gui_exc:
                write_log("桌面窗口启动失败，回退系统浏览器：\n"
                          + "".join(traceback.format_exception_only(type(gui_exc), gui_exc)))
                message(
                    "Tokscale 用量面板",
                    "桌面窗口组件加载失败，已改用系统浏览器打开。\n"
                    f"原因：{gui_exc}",
                    error=True,
                )
                open_system_browser(url)
                server_thread.join()
                return 0
        write_log("未检测到 WebView2 运行时，回退系统浏览器。")
        message(
            "Tokscale 用量面板",
            "未检测到 Microsoft WebView2 运行时（Win10/11 一般自带）。\n"
            "已改用系统浏览器打开面板；如需桌面窗口，可安装\n"
            "“WebView2 Evergreen Runtime”后重新运行本程序。",
        )
        open_system_browser(url)
        server_thread.join()
        return 0
    except Exception as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        write_log("启动失败：\n" + detail)
        message(
            "Tokscale 用量面板",
            f"{exc}\n\n诊断日志：{data_dir() / 'portable.log'}",
            error=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
