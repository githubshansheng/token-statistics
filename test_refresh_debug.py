#!/usr/bin/env python3
"""V1.3 单测：run_command（非零/超时）、刷新日志持久化与查询、线程池定价失败记录。

通过前两个验证 run_command 的返回结构；通过后两个验证 RefreshLogger 与 debug 查询，
以及线程池定价失败被记录到同一次刷新日志（不截断）。不构建、不启动 GUI。
"""

import os
import sys
import tempfile
import json
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 关键：把运行时目录指到临时目录，避免污染仓库（HERE 在模块导入时即被读取）。
_TMP = tempfile.TemporaryDirectory()
os.environ["TOKSCALE_RUNTIME_DIR"] = _TMP.name

import runtime_support as rts  # noqa: E402
import build_dashboard as bd  # noqa: E402


class TestRunCommand(unittest.TestCase):
    def test_nonzero_returncode_and_output(self):
        result = rts.run_command(
            [sys.executable, "-c",
             "import sys; sys.stderr.write('boom-err'); sys.stdout.write('ok-out'); sys.exit(3)"],
            timeout=30,
        )
        self.assertEqual(result["returncode"], 3)
        self.assertFalse(result["timedOut"])
        self.assertIn("ok-out", result["stdout"])
        self.assertIn("boom-err", result["stderr"])
        self.assertIn("cmd", result)
        self.assertIn("cwd", result)
        self.assertIn("duration", result)
        self.assertIsNone(result["traceback"])

    def test_timeout_is_flagged(self):
        result = rts.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
        )
        self.assertTrue(result["timedOut"])
        self.assertIsNone(result["returncode"])


class TestRefreshLogging(unittest.TestCase):
    def test_logger_persist_and_query(self):
        logger = rts.RefreshLogger()
        logger.event("rebuild_start", cmd=["tokscale.exe", "graph"])
        logger.log_run("tokscale_graph", {
            "cmd": ["x"], "cwd": rts.HERE, "duration": 1.2, "timedOut": False,
            "returncode": 0, "stdout": "OUT" * 5000, "stderr": "ERR",
            "traceback": None,
        })
        logger.event("refresh_failed", error="kaboom",
                     traceback="TRACEBACK-LINE\nsecond-line")
        # 文件存在且内容不截断
        self.assertTrue(os.path.isfile(logger.path))
        with open(logger.path, encoding="utf-8") as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        events = {e["event"] for e in lines}
        self.assertIn("start", events)
        self.assertIn("rebuild_start", events)
        self.assertIn("tokscale_graph", events)
        self.assertIn("refresh_failed", events)
        # 验证不截断：stdout 完整保留
        tg = next(e for e in lines if e["event"] == "tokscale_graph")
        self.assertEqual(len(tg["stdout"]), len("OUT") * 5000)
        self.assertIn("TRACEBACK-LINE", [e.get("traceback") for e in lines
                                         if e["event"] == "refresh_failed"][0])

        # 查询接口
        listing = rts.list_debug_logs()
        names = [r["name"] for r in listing]
        self.assertIn(os.path.basename(logger.path), names)
        rec = rts.read_debug_log(os.path.basename(logger.path))
        self.assertIsNotNone(rec)
        self.assertEqual(len(rec["lines"]), len(lines))

    def test_read_rejects_unsafe_names(self):
        self.assertIsNone(rts.read_debug_log("../serve.py"))
        self.assertIsNone(rts.read_debug_log("..%2f..%2fetc%2fpasswd"))
        self.assertIsNone(rts.read_debug_log("notjsonl.txt"))
        self.assertIsNone(rts.read_debug_log(""))
        self.assertIsNone(rts.read_debug_log("/abs/path.jsonl"))


class TestFrozenNoNpxFallback(unittest.TestCase):
    def test_frozen_missing_exe_raises(self):
        saved = rts.IS_FROZEN
        rts.IS_FROZEN = True
        try:
            # 临时目录没有 tokscale.exe
            with self.assertRaises(RuntimeError):
                rts.resolve_tokscale_command(["graph", "--output", "graph.json"])
        finally:
            rts.IS_FROZEN = saved

    def test_source_allows_npx_fallback(self):
        # 临时目录无 tokscale.exe 且非冻结 → 回退 npx
        saved = rts.IS_FROZEN
        rts.IS_FROZEN = False
        try:
            cmd = rts.resolve_tokscale_command(["pricing", "gpt-4", "--json"])
            self.assertTrue(any("npx" in str(c) for c in cmd))
        finally:
            rts.IS_FROZEN = saved


class TestThreadedPricingFailureLogged(unittest.TestCase):
    def test_pricing_failure_recorded_in_same_refresh(self):
        logger = rts.RefreshLogger()
        rts.current_refresh_logger = logger
        try:
            # 让 run_command 永远返回“失败”（不真正执行 npx/网络）
            orig = rts.run_command

            def fake_run(args, cwd=None, timeout=None, env=None, silent=False):
                return {
                    "cmd": list(args), "cwd": cwd or rts.HERE, "timeout": timeout,
                    "duration": 0.01, "timedOut": False, "returncode": 1,
                    "stdout": "", "stderr": "simulated pricing failure",
                    "traceback": None, "silent": silent,
                }

            rts.run_command = fake_run
            # 避免 basellm 联网：临时目录无快照，_download_text 会尝试 curl 但失败返回 None
            prices = bd.ensure_pricing(["some-missing-model-x"])
            # 模型定价缺失（None），但失败已记录到同次刷新日志
            self.assertIsNone(prices.get("some-missing-model-x"))
            with open(logger.path, encoding="utf-8") as f:
                events = [json.loads(ln) for ln in f if ln.strip()]
            pricing_events = [e for e in events if e["event"] == "pricing"]
            self.assertTrue(pricing_events, "线程池定价失败必须记录到刷新日志")
            pe = pricing_events[0]
            self.assertEqual(pe["model"], "some-missing-model-x")
            self.assertEqual(pe["returncode"], 1)
            self.assertEqual(pe["stderr"], "simulated pricing failure")
            rts.run_command = orig
        finally:
            rts.current_refresh_logger = None


class TestSecondPrecisionFilter(unittest.TestCase):
    """切片 JS 模板，验证秒级时间规范化、边界日折算与表格不压缩列宽。"""

    def setUp(self):
        js = bd.JS
        start = js.find("const normDT=")
        end = js.find("/* ---------- 数据范围 ---------- */")
        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        helpers = (
            "function shiftISO(iso,n){const d=new Date(iso+'T00:00:00');"
            "d.setDate(d.getDate()+n);"
            "return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')"
            "+'-'+String(d.getDate()).padStart(2,'0');}\n"
        )
        self.prelude = helpers + js[start:end]

    def _run(self, extra, data):
        node = os.path.join(
            r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-2",
            "node.exe",
        )
        if not os.path.isfile(node):
            node = "node"
        script = (
            "const DATA=" + json.dumps(data, ensure_ascii=False) + ";\n"
            "let range={start:null,end:null};\n"
            + self.prelude + extra
        )
        tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write(script)
        tmp.close()
        try:
            out = __import__("subprocess").run(
                [node, tmp.name], capture_output=True, text=True, timeout=15
            )
        finally:
            os.unlink(tmp.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_norm_dt_keeps_seconds(self):
        extra = (
            "console.log(JSON.stringify({"
            "date:normDT('2026-09-09'),"
            "min:normDT('2026-09-09T08:15'),"
            "sec:normDT('2026-09-09T08:15:07'),"
            "space:normDT('2026-09-09 08:15:07')"
            "}));"
        )
        got = self._run(extra, {"hours": [], "entries": []})
        self.assertEqual(got["date"], "2026-09-09T00:00:00")
        self.assertEqual(got["min"], "2026-09-09T08:15:00")
        self.assertEqual(got["sec"], "2026-09-09T08:15:07")
        self.assertEqual(got["space"], "2026-09-09T08:15:07")

    def test_partial_day_scales_by_hour_bucket(self):
        data = {
            "hours": [
                {"h": "2026-09-09T00:00:00", "i": 50, "o": 0, "cr": 0, "cw": 0, "msg": 1},
                {"h": "2026-09-09T12:00:00", "i": 50, "o": 0, "cr": 0, "cw": 0, "msg": 1},
            ],
            "entries": [
                {"d": "2026-09-09", "c": "workbuddy", "m": "glm",
                 "i": 100, "o": 0, "cr": 0, "cw": 0, "r": 0, "msg": 2, "cost": 10, "cd": 0},
            ],
        }
        extra = (
            "range={start:'2026-09-09T12:00:00',end:'2026-09-09T23:59:59'};"
            "const rows=filteredEntries();"
            "console.log(JSON.stringify({i:rows[0].i,msg:rows[0].msg,approx:rangeApprox}));"
        )
        got = self._run(extra, data)
        self.assertAlmostEqual(got["i"], 50.0, places=4)
        self.assertEqual(got["msg"], 1)
        self.assertTrue(got["approx"])

    def test_full_day_preset_does_not_scale(self):
        data = {
            "hours": [
                {"h": "2026-09-09T00:00:00", "i": 40, "o": 0, "cr": 0, "cw": 0, "msg": 1},
            ],
            "entries": [
                {"d": "2026-09-09", "c": "workbuddy", "m": "glm",
                 "i": 100, "o": 0, "cr": 0, "cw": 0, "r": 0, "msg": 2, "cost": 10, "cd": 0},
            ],
        }
        extra = (
            "range={start:'2026-09-09T00:00:00',end:'2026-09-09T23:59:59'};"
            "const rows=filteredEntries();"
            "console.log(JSON.stringify({i:rows[0].i,approx:rangeApprox}));"
        )
        got = self._run(extra, data)
        self.assertEqual(got["i"], 100)
        self.assertFalse(got["approx"])


class TestDenseLayoutCss(unittest.TestCase):
    def test_table_not_fixed_and_datetime_second_step(self):
        self.assertIn("table-layout:auto", bd.CSS)
        self.assertIn("width:max-content", bd.CSS)
        self.assertIn(".tw{overflow-x:auto", bd.CSS)
        self.assertNotIn("table-layout:fixed", bd.CSS)
        self.assertIn('type="datetime-local"', bd.HTML_TMPL)
        self.assertIn('step="1"', bd.HTML_TMPL)
        self.assertIn("min-width:17.5em", bd.CSS)
        self.assertIn(".g2{", bd.CSS)
        self.assertIn("max-width:1680px", bd.CSS)
        self.assertIn('h+=\'<div class="g2">\'', bd.JS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
