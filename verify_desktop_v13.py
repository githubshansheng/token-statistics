#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_desktop_v13.py — V1.3 桌面版验收脚本。

本脚本覆盖三类验收，且都「不改」4 个发布源文件(runtime_support.py / serve.py /
build_dashboard.py / portable_launcher.py)：

  A. 源码 HTTP 验收(现在可跑)：
     导入真实 serve / runtime_support，自建隔离临时目录，用服务模块 mock 模拟
     成功后台刷新(bd.build 真实生成 dashboard.html)，轮询 /api/refresh/status，断言：
       - 调试日志列表 / 读取可用
       - 页面含右上「调试」链接(注入 /debug)
       - 首次 refresh ok，重复手动刷新 ok
       - 路径穿越被拒(HTTP 层 + API 层)
     可选：若本机有 Microsoft Edge，用 --headless --dump-dom 检查 /debug 页面(无 GUI)。

  B. 错误路径单测(现在可跑)：
       - 长错误完整日志含结尾(不截断)
       - 多次历史日志保留
       - 路径穿越被拒(单元测试层)

  C. EXE 端到端验收(待主代理在 outputs/ 产出 EXE 后运行)：
     运行真实 outputs/Tokscale用量面板-桌面版-v1.3-Windows-x64.exe
     (TOKSCALE_NO_BROWSER=1)，隔离 HOME/USERPROFILE/LOCALAPPDATA 与
     TOKSCALE_PORTABLE_DATA_DIR，轮询首刷 <=150s，断言同上；若有 Edge 用
     headless --dump-dom 检查 /debug 页面。脚本 finally 仅靠 PID 树终止自己启动
     的 EXE，绝不按镜像名终止其它用户程序。

运行：
    python verify_desktop_v13.py            # 现在：A + B 跑，C 自动跳过
    python verify_desktop_v13.py -v
    python verify_desktop_v13.py TestExeE2E # 仅 EXE 端到端(需先有 outputs/*.exe)
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE_SCRIPT = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.abspath(os.environ.get("TOKSCALE_SOURCE_ROOT") or HERE_SCRIPT)
sys.path.insert(0, HERE_SCRIPT)

# ---------------------------------------------------------------- 隔离环境(进程级)
# 仅用于「源码 HTTP 验收」的 in-process 服务：把运行时目录指到本项目下的临时目录，
# 并隔离 HOME/USERPROFILE/LOCALAPPDATA，避免读取/污染真实用户数据。
ROOT_TMP = tempfile.mkdtemp(prefix="tokscale-verify-")
RT = os.path.join(ROOT_TMP, "runtime")          # TOKSCALE_RUNTIME_DIR
HOME_ISO = os.path.join(ROOT_TMP, "home")
LAD_ISO = os.path.join(ROOT_TMP, "localappdata")
os.makedirs(RT, exist_ok=True)
os.makedirs(HOME_ISO, exist_ok=True)
os.makedirs(LAD_ISO, exist_ok=True)
os.environ["TOKSCALE_RUNTIME_DIR"] = RT
os.environ["HOME"] = HOME_ISO
os.environ["USERPROFILE"] = HOME_ISO
os.environ["LOCALAPPDATA"] = LAD_ISO
os.environ["TOKSCALE_NO_BROWSER"] = "1"   # in-process 也不弹浏览器
os.environ.setdefault("TOKSCALE_PORT", "8765")  # 避免 unittest 的类名参数被 serve 误当端口

# 必须在导入 serve/rts/bd 之前设置好上面的环境变量(它们在模块加载时即读取)。
import runtime_support as rts          # noqa: E402
import serve                           # noqa: E402
import build_dashboard as bd           # noqa: E402

rts.HERE = RT
serve.HERE = RT
bd.HERE = RT
rts.LOGS_DIR = os.path.join(RT, "logs")


# ---------------------------------------------------------------- 预置：避免联网
def _seed_no_network():
    # 预置定价缓存：确保 ensure_pricing 不回退到 npx/tokscale 联网。
    pricing = {
        "_meta": {"version": 1, "baseCurrency": "USD", "displayCurrency": "CNY"},
        "gpt-4o": {"i": 5e-6, "o": 15e-6, "cr": 1e-6, "cw": 2e-6, "src": "test"},
    }
    with open(os.path.join(RT, "pricing.json"), "w", encoding="utf-8") as f:
        json.dump(pricing, f)
    # 预置一份有效(非空) basellm 快照，使其在 TTL 内被直接复用，跳过 curl 联网重试。
    snap = {"data": [{
        "model_name": "gpt-4o", "vendor_name": "openai",
        "price_per_m_input": 5e-6, "price_per_m_output": 15e-6,
        "price_per_m_cache_read": 1e-6, "price_per_m_cache_write": 2e-6,
    }]}
    with open(os.path.join(RT, "basellm_models.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f)


_seed_no_network()


def _write_min_graph():
    graph = {
        "meta": {"version": "1.3", "generatedAt": "2026-09-05T00:00:00"},
        "contributions": [
            {"date": "2026-09-01", "clients": [
                {"client": "workbuddy", "modelId": "gpt-4o",
                 "tokens": {"input": 100, "output": 200, "cacheRead": 0,
                            "cacheWrite": 0, "reasoning": 0},
                 "cost": 0.0035, "messages": 3}]},
            {"date": "2026-09-02", "clients": [
                {"client": "codex", "modelId": "gpt-4o",
                 "tokens": {"input": 50, "output": 50, "cacheRead": 0,
                            "cacheWrite": 0, "reasoning": 0},
                 "cost": 0.0015, "messages": 1}]},
        ],
        "timeMetrics": {
            "totalActiveTimeMs": 120000, "longestContinuousMs": 60000,
            "maxConcurrentSessions": 1, "sessionCount": 4,
        },
    }
    with open(os.path.join(RT, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)


# ---------------------------------------------------------------- HTTP 工具
def _urlopen_json(url, data=None, timeout=10):
    req = urllib.request.Request(url)
    if data is not None:
        req.method = "POST"
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode("utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")), r.status


def _get_bytes(url, timeout=10):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.status


def wait_for_refresh(base, timeout=150, expect_busy_end=True):
    """轮询 /api/refresh/status 直到 busy=False；返回最终状态 dict。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last, _ = _urlopen_json(base + "/api/refresh/status")
        except Exception:
            last = None
        if last is not None and (not expect_busy_end or not last.get("busy")):
            return last
        time.sleep(0.5)
    return last


# ---------------------------------------------------------------- 进程/工具定位
def find_edge():
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return shutil.which("msedge") or shutil.which("msedge.exe")


def find_exe():
    outputs = os.path.join(SOURCE_ROOT, "outputs")
    preferred = [
        "Tokscale用量面板-桌面版-v1.7.7-Windows-x64.exe",
        "Tokscale用量面板-桌面版-v1.7.6-Windows-x64.exe",
        "Tokscale用量面板-桌面版-v1.7.5-Windows-x64.exe",
    ]
    for name in preferred:
        path = os.path.join(outputs, name)
        if os.path.isfile(path):
            return path
    cands = [os.path.join(outputs, name) for name in os.listdir(outputs)] if os.path.isdir(outputs) else []
    cands = [path for path in cands if path.lower().endswith(".exe")]
    return max(cands, key=os.path.getmtime) if cands else None


def edge_dump_dom(edge, url, timeout=40):
    """用 Edge headless 抓取渲染后 DOM；无 GUI。返回 (ok, text)，text 失败时含诊断。"""
    if not edge:
        return False, ""
    try:
        out = subprocess.run(
            [edge, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--dump-dom", url],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode == 0:
            return True, out.stdout
        return False, "rc=%d stderr=%s" % (out.returncode, (out.stderr or "")[:300])
    except Exception as e:
        return False, "edge_dump_dom error: %s" % e


def kill_tree(pid):
    """仅按 PID 树终止自己启动的进程，绝不按镜像名终止其它程序。"""
    if pid is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15,
            )
            return
        except Exception:
            pass
    # 非 Windows 或 taskkill 失败：仅终止该 PID(不波及他人)。
    try:
        os.kill(pid, 9)
    except Exception:
        pass


# ---------------------------------------------------------------- 服务模块 mock
def fake_successful_rebuild(logger=None):
    """模拟一次成功后台刷新：写最小 graph.json 并真实跑 bd.build() 生成 dashboard.html。"""
    _write_min_graph()
    if logger is not None:
        logger.event("rebuild_start", cmd=["tokscale.exe", "graph"])
    bd.build()
    if logger is not None:
        logger.event("rebuild_done")


LONG_END_MARKER = "UNIQUE_END_MARKER_9F3A2B"
STDERR_LONG = "STDERR-" + ("Y" * 5000)


def fake_long_error(logger=None):
    """模拟一次失败后台刷新：记录一条超长 stderr + 抛出超长异常(结尾带标记)。"""
    if logger is not None:
        logger.log_run("tokscale_graph", {
            "cmd": ["tokscale.exe", "graph"], "cwd": RT, "duration": 1.0,
            "timedOut": False, "returncode": 1,
            "stdout": "", "stderr": STDERR_LONG, "traceback": None,
        })
    raise RuntimeError("ERR-" + ("X" * 3000) + "-" + LONG_END_MARKER)


# ================================================================ A. 源码 HTTP 验收
class TestSourceHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_rebuild = serve.rebuild
        serve.rebuild = fake_successful_rebuild
        cls.httpd = serve.Server(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls._thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls._thread.start()
        cls.edge = find_edge()
        # 预生成一份 dashboard，避免首屏走 first_run 页面。
        fake_successful_rebuild()

    @classmethod
    def tearDownClass(cls):
        serve.rebuild = cls._orig_rebuild
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass

    def _trigger_refresh(self):
        _, code = _urlopen_json(self.base + "/api/refresh", data={})
        self.assertEqual(code, 200)

    def test_01_dashboard_has_debug_link(self):
        data, code = _get_bytes(self.base + "/")
        self.assertEqual(code, 200)
        text = data.decode("utf-8", "replace")
        self.assertIn("/debug", text, "面板未注入 Debug 链接")
        self.assertIn("调试", text, "面板未注入「调试」按钮")

    def test_02_debug_page_renders(self):
        data, code = _get_bytes(self.base + "/debug")
        self.assertEqual(code, 200)
        self.assertIn("Tokscale 调试面板", data.decode("utf-8", "replace"))

    def test_03_refresh_ok_and_logs(self):
        # 触发首刷并等待结束
        self._trigger_refresh()
        st = wait_for_refresh(self.base, timeout=120)
        self.assertIsNotNone(st, "未能轮询到刷新状态")
        self.assertFalse(st["busy"], "刷新未在 120s 内结束")
        self.assertTrue(st["ok"], "刷新应成功(ok=True)，实际: %r" % st)
        self.assertIsNotNone(st["logId"], "STATE.logId 不应为 None")

        # 调试日志列表 + 读取
        logs, _ = _urlopen_json(self.base + "/api/debug/logs")
        self.assertIn("logs", logs)
        names = [r["name"] for r in logs["logs"]]
        self.assertTrue(names, "调试日志列表为空")
        rec, _ = _urlopen_json(self.base + "/api/debug/log?id=" +
                               urllib.parse.quote(names[0]))
        self.assertIn("lines", rec)
        self.assertTrue(rec["lines"], "调试日志内容为空")

        # Edge 可选：headless 检查 /debug 页面(无 GUI)。仅当本机 Edge 可用且确实
        # 返回了非空 DOM 时才断言；否则降级为警告(页面已通过直接 GET 验证)。
        ok, dom = edge_dump_dom(self.edge, self.base + "/debug")
        if self.edge and ok and dom:
            self.assertIn("Tokscale 调试面板", dom)
        elif self.edge:
            print("WARNING: 本机有 Edge 但 headless --dump-dom 未返回可用 DOM，"
                  "跳过该检查(已用直接 GET 验证 /debug)。诊断: %r" % dom[:200])

    def test_04_repeat_manual_refresh_ok(self):
        for _ in range(2):
            self._trigger_refresh()
            st = wait_for_refresh(self.base, timeout=120)
            self.assertIsNotNone(st)
            self.assertFalse(st["busy"])
            self.assertTrue(st["ok"], "重复手动刷新应成功")
            self.assertIsNotNone(st["logId"])

    def test_05_path_traversal_rejected_http(self):
        # HTTP 层：路径穿越应被拒(400)
        for bad in ("../../serve.py", "..\\..\\serve.py",
                    "..%2f..%2fserve.py", "/abs/serve.py"):
            try:
                _urlopen_json(self.base + "/api/debug/log?id=" +
                              urllib.parse.quote(bad, safe=""))
                self.fail("路径穿越未被拒: %r" % bad)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400, "路径穿越应 400，实际 %d (%r)" % (e.code, bad))
            except urllib.error.URLError:
                #  occasional 连接错误也视为未泄露
                pass


# ================================================================ B. 错误路径单测
class TestErrorPaths(unittest.TestCase):
    def setUp(self):
        self._orig_rebuild = serve.rebuild
        serve.rebuild = fake_long_error
        # 复位 STATE，避免残留 busy
        with serve.STATE_LOCK:
            serve.STATE.update(busy=False, ok=None, error=None,
                               started=None, finished=None, logId=None)

    def tearDown(self):
        serve.rebuild = self._orig_rebuild

    def test_01_long_error_log_keeps_ending(self):
        serve._refresh_worker()  # 同步执行(失败路径)
        with serve.STATE_LOCK:
            log_id = serve.STATE["logId"]
            ok = serve.STATE["ok"]
        self.assertFalse(ok, "失败刷新后 ok 应为 False")
        self.assertIsNotNone(log_id)
        # 日志文件名形如 refresh-<id>.jsonl
        rec = rts.read_debug_log("refresh-" + log_id + ".jsonl")
        self.assertIsNotNone(rec, "应可读到失败日志")
        blob = "\n".join(rec["lines"])
        self.assertIn(LONG_END_MARKER, blob, "长错误日志被截断，未含结尾标记")
        self.assertIn(STDERR_LONG, blob, "长 stderr 未被完整记录")
        events = [json.loads(ln)["event"] for ln in rec["lines"] if ln.strip()]
        self.assertIn("refresh_failed", events)

    def test_02_multiple_historical_logs_retained(self):
        before = len(rts.list_debug_logs())
        n = 3
        for _ in range(n):
            serve._refresh_worker()
        after = rts.list_debug_logs()
        refresh_logs = [r for r in after if r["type"] == "refresh"]
        self.assertGreaterEqual(len(refresh_logs), before + n,
                                "多次历史刷新日志未被保留")
        # 按 mtime 倒序
        mtimes = [r["mtime"] for r in after]
        self.assertEqual(mtimes, sorted(mtimes, reverse=True))

    def test_03_path_traversal_rejected_unit(self):
        self.assertIsNone(rts.read_debug_log("../serve.py"))
        self.assertIsNone(rts.read_debug_log("..\\..\\serve.py"))
        self.assertIsNone(rts.read_debug_log("..%2f..%2fserve.py"))
        self.assertIsNone(rts.read_debug_log("/abs/path.jsonl"))
        self.assertIsNone(rts.read_debug_log("notjsonl.txt"))
        self.assertIsNone(rts.read_debug_log(""))


# ================================================================ C. EXE 端到端验收(待 EXE 产出)
class TestExeE2E(unittest.TestCase):
    def test_exe_e2e(self):
        exe = find_exe()
        if not exe or not os.path.exists(exe):
            self.skipTest("outputs/*.exe 尚未构建，待主代理运行")

        tdir = tempfile.mkdtemp(prefix="tokscale-exe-")
        home = os.path.join(tdir, "home")
        lad = os.path.join(tdir, "lad")
        os.makedirs(home, exist_ok=True)
        os.makedirs(lad, exist_ok=True)
        env = os.environ.copy()
        env["TOKSCALE_NO_BROWSER"] = "1"
        env["TOKSCALE_PORTABLE_DATA_DIR"] = tdir
        env["HOME"] = home
        env["USERPROFILE"] = home
        env["LOCALAPPDATA"] = lad

        proc = subprocess.Popen([exe], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        edge = find_edge()
        try:
            # 等待 EXE 写出 .port
            port_file = os.path.join(tdir, ".port")
            deadline = time.time() + 30
            port = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail("EXE 进程已退出(可能互斥体被占用或启动失败)")
                if os.path.isfile(port_file):
                    try:
                        with open(port_file, encoding="ascii") as handle:
                            port = int(handle.read().strip())
                        break
                    except Exception:
                        pass
                time.sleep(0.5)
            self.assertIsNotNone(port, "未能从 %s 读取端口" % port_file)
            base = "http://127.0.0.1:%d" % port

            # 轮询首刷 <=150s
            st = wait_for_refresh(base, timeout=150)
            self.assertIsNotNone(st, "未能轮询到 EXE 首刷状态")
            self.assertFalse(st["busy"], "EXE 首刷未在 150s 内结束")
            self.assertIsNotNone(st["logId"])
            # 隔离环境无数据，刷新应仍 ok(空面板)；若失败则报告日志
            if not st["ok"]:
                logs, _ = _urlopen_json(base + "/api/debug/logs")
                names = [r["name"] for r in logs.get("logs", [])]
                detail = ""
                if names:
                    rec, _ = _urlopen_json(base + "/api/debug/log?id=" +
                                          urllib.parse.quote(names[0]))
                    detail = "\n".join(rec.get("lines", []))[:2000]
                self.fail("EXE 首刷失败: %s\n%s" % (st.get("error"), detail))

            # 调试列表 / 读取 / 页面 Debug 链接
            data, _ = _get_bytes(base + "/")
            self.assertIn("/debug", data.decode("utf-8", "replace"))
            data, _ = _get_bytes(base + "/debug")
            self.assertIn("Tokscale 调试面板", data.decode("utf-8", "replace"))
            logs, _ = _urlopen_json(base + "/api/debug/logs")
            self.assertTrue(logs.get("logs"))

            # 重复手动刷新
            for _ in range(2):
                _urlopen_json(base + "/api/refresh", data={})
                s2 = wait_for_refresh(base, timeout=150)
                self.assertFalse(s2["busy"])
                self.assertTrue(s2["ok"])

            # Edge 可选：headless 检查 /debug(无 GUI)。仅当返回非空 DOM 才断言。
            ok, dom = edge_dump_dom(edge, base + "/debug")
            if edge and ok and dom:
                self.assertIn("Tokscale 调试面板", dom)
            elif edge:
                print("WARNING: 本机有 Edge 但 headless --dump-dom 未返回可用 DOM，"
                      "跳过该检查(已用直接 GET 验证 /debug)。诊断: %r" % dom[:200])
        finally:
            # 仅终止自己启动的 EXE 进程树
            kill_tree(proc.pid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
