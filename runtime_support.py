#!/usr/bin/env python3
"""V1.3 共享运行支撑。

职责：
- run_command: 统一的子进程执行封装。Windows 上以 CREATE_NO_WINDOW +
  STARTF_USESHOWWINDOW/SW_HIDE 隐藏窗口、stdin=DEVNULL、shell=False、捕获 UTF-8；
  返回结构化结果，记录完整 cmd/cwd/duration/returncode/stdout/stderr/是否超时/traceback。
- resolve_tokscale_command: 解析 tokscale 调用命令。冻结版(便携包)绝不回退到 npx，
  缺少 tokscale.exe 时明确抛错；非冻结源码运行保留 npx 兜底，不破坏原有行为。
- RefreshLogger: 每次刷新一个 uuid，持久化到 logs/refresh-ID.jsonl（JSONL，逐行事件），
  线程安全。线程池定价/网络失败也在同一次刷新日志中记录，内容不截断。
- list_debug_logs / read_debug_log: 给 /api/debug 使用，强制安全文件名校验，
  绝不接受任意路径。

仅依赖 Python 标准库。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime

HERE = os.path.abspath(
    os.environ.get("TOKSCALE_RUNTIME_DIR")
    or os.path.dirname(os.path.abspath(__file__))
)

# PyInstaller 冻结后 sys._MEIPASS 存在，表示便携版（冻结版）。
IS_FROZEN = bool(getattr(sys, "_MEIPASS", None))

LOGS_DIR = os.path.join(HERE, "logs")

# 当前进行中的刷新日志（由 serve.py worker 设置），build_dashboard.py 的线程池复用它。
current_refresh_logger = None

# ---------------------------------------------------------------- 扫描进度（供前端蒙版轮询）

PROGRESS = {"stage": "", "detail": "", "pct": 0, "ts": "", "lines": []}
_PROGRESS_LOCK = threading.Lock()


def reset_progress():
    with _PROGRESS_LOCK:
        PROGRESS.update(stage="", detail="", pct=0,
                        ts=_now_iso(), lines=[])


def update_progress(stage="", detail="", pct=None, line=None):
    """更新当前刷新阶段进度；line 为可选的滚动动态行（保留最近 8 条）。"""
    with _PROGRESS_LOCK:
        if stage:
            PROGRESS["stage"] = stage
        if detail:
            PROGRESS["detail"] = detail
        if pct is not None:
            PROGRESS["pct"] = max(0, min(100, int(pct)))
        PROGRESS["ts"] = _now_iso()
        if line:
            PROGRESS["lines"] = (PROGRESS["lines"] + [line])[-8:]


def progress_snapshot():
    with _PROGRESS_LOCK:
        return {k: (list(v) if isinstance(v, list) else v)
                for k, v in PROGRESS.items()}


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 子进程执行

def _win_startupinfo():
    """Windows：隐藏控制台窗口。返回 (STARTUPINFO, creationflags)，非 Windows 返回 (None, None)。"""
    if os.name != "nt":
        return None, None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    flags = 0
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    flags |= create_no_window
    return si, flags


def run_command(args, cwd=None, timeout=None, env=None, silent=False):
    """执行命令并返回结构化结果。

    返回 dict 字段：cmd, cwd, timeout, duration, timedOut, returncode,
    stdout, stderr, traceback, silent。
    - Windows：CREATE_NO_WINDOW + STARTF_USESHOWWINDOW/SW_HIDE + stdin=DEVNULL + shell=False。
    - 捕获 UTF-8（errors=replace），完整输出不截断。
    - 超时或异常均记录 traceback/标记，绝不抛给调用方（由调用方决定如何处理）。
    """
    cwd = cwd or HERE
    si, flags = _win_startupinfo()
    kw = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    child_env = dict(os.environ if env is None else env)
    if IS_FROZEN and os.name == "nt":
        # Windows 冻结版隔离继承的 PATH（部分桌面宿主会配置 npm 包装器）。
        # macOS 冻结版保持原样：内置原生二进制不依赖 PATH，且 Finder 启动
        # 时继承的 PATH 本就干净，隔离反而会破坏用户自定义工具链。
        system_root = child_env.get("SystemRoot", r"C:\Windows")
        child_env["PATH"] = os.pathsep.join([os.path.join(system_root, "System32"), system_root])
        child_env["COMSPEC"] = os.path.join(system_root, "System32", "cmd.exe")
    kw["env"] = child_env
    if si is not None:
        kw["startupinfo"] = si
        if flags is not None:
            kw["creationflags"] = flags

    started = time.time()
    timed_out = False
    tb = None
    rc = None
    out = ""
    err = ""
    proc = None
    try:
        proc = subprocess.Popen(args, **kw)
        try:
            so, se = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            try:
                so, se = proc.communicate()
            except Exception:
                so, se = "", ""
        rc = None if timed_out else proc.returncode
        out = so or ""
        err = se or ""
    except Exception:
        tb = traceback.format_exc()
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    duration = round(time.time() - started, 3)
    return {
        "cmd": list(args) if isinstance(args, (list, tuple)) else str(args),
        "cwd": cwd,
        "timeout": timeout,
        "duration": duration,
        "timedOut": timed_out,
        "returncode": rc,
        "stdout": out,
        "stderr": err,
        "traceback": tb,
        "silent": silent,
    }


def resolve_tokscale_command(sub_args):
    """返回 tokscale 调用命令列表；sub_args 为子命令及参数。

    冻结版(便携包)：必须使用内置原生二进制（Windows: tokscale.exe /
    macOS: tokscale），绝不回退 npx；缺二进制时明确抛 RuntimeError。
    非冻结源码运行：有内置二进制则用，否则回退 npx（保留已有行为）。
    """
    sub_args = list(sub_args)
    binary_name = "tokscale.exe" if os.name == "nt" else "tokscale"
    bundled = os.path.join(HERE, binary_name)
    if os.path.isfile(bundled):
        return [bundled] + sub_args
    if IS_FROZEN:
        raise RuntimeError(
            "便携版缺少 %s，无法执行：" % binary_name
            + " ".join(sub_args)
            + "（冻结版不会回退到 npx，请重新安装完整的便携包）"
        )
    npx = shutil.which("npx.cmd") or shutil.which("npx") or "npx"
    return [npx, "--yes", "tokscale@latest"] + sub_args


# ---------------------------------------------------------------- 刷新日志

class RefreshLogger:
    """一次刷新(含 tokscale 导出 + 构建 + 线程池定价)对应一个 uuid 的 JSONL 日志。"""

    def __init__(self, log_id=None):
        self.id = log_id or uuid.uuid4().hex
        self.path = os.path.join(LOGS_DIR, "refresh-%s.jsonl" % self.id)
        self._lock = threading.Lock()
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
        except Exception:
            pass
        self._write({"event": "start", "id": self.id, "ts": _now_iso()})

    def event(self, name, **fields):
        rec = {"event": name, "ts": _now_iso()}
        rec.update(fields)
        self._write(rec)

    def log_run(self, name, result):
        """记录一次 run_command 结果(来自 runtime_support.run_command)。"""
        if not isinstance(result, dict):
            self.event(name, raw=str(result))
            return
        self.event(
            name,
            cmd=result.get("cmd"),
            cwd=result.get("cwd"),
            duration=result.get("duration"),
            timedOut=result.get("timedOut"),
            returncode=result.get("returncode"),
            stdout=result.get("stdout"),
            stderr=result.get("stderr"),
            traceback=result.get("traceback"),
        )

    def log_pricing(self, model, result):
        """记录单模型定价获取结果(线程池内调用，线程安全)。"""
        if not isinstance(result, dict):
            self.event("pricing", model=model, error="no-result")
            return
        self.event(
            "pricing",
            model=model,
            timedOut=result.get("timedOut"),
            returncode=result.get("returncode"),
            stdout=result.get("stdout"),
            stderr=result.get("stderr"),
            traceback=result.get("traceback"),
        )

    def _write(self, rec):
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass


# ---------------------------------------------------------------- debug 日志查询

_SAFE_NAME_RE = None


def _valid_log_name(name):
    """仅允许 logs/ 下的 alnum/_/- + .jsonl 文件名，拒绝路径穿越与任意路径。"""
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if not name.endswith(".jsonl"):
        return False
    if len(name) > 256:
        return False
    return all(c.isalnum() or c in "_-." for c in name)


def list_debug_logs():
    """列出 LOGS_DIR 下所有 *.jsonl 调试日志，按修改时间倒序。"""
    out = []
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        for fn in os.listdir(LOGS_DIR):
            if not _valid_log_name(fn):
                continue
            full = os.path.join(LOGS_DIR, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            prefix = fn.split("-", 1)[0] if "-" in fn else "log"
            out.append({
                "name": fn,
                "id": fn[: -len(".jsonl")],
                "type": prefix,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        out.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception:
        pass
    return out


def read_debug_log(name):
    """读取单个调试日志，安全文件名校验，返回 {name, lines:[...]} 或 None。"""
    if not _valid_log_name(name):
        return None
    full = os.path.join(LOGS_DIR, name)
    # 二次防御：绝对路径必须仍在 LOGS_DIR 内。
    try:
        if os.path.abspath(full) != os.path.normpath(full):
            return None
        base = os.path.abspath(LOGS_DIR)
        if os.path.dirname(os.path.abspath(full)) != base:
            return None
    except Exception:
        return None
    try:
        with open(full, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError:
        return None
    return {"name": name, "lines": lines}


def write_client_error(entries):
    """把前端上报的 client-error 条目写入 logs/client-error-ID.jsonl。"""
    log_id = uuid.uuid4().hex
    name = "client-error-%s.jsonl" % log_id
    full = os.path.join(LOGS_DIR, name)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        rec = {
            "event": "client-error",
            "id": log_id,
            "ts": _now_iso(),
            "entries": entries if isinstance(entries, list) else [],
        }
        with open(full, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return None
    return name
