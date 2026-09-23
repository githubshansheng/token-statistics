#!/usr/bin/env python3
"""本地面板服务：静态提供最新面板，数据改为手动/定时后台静默更新。

行为变化(v2)：
- GET / 、/index.html 、/dashboard.html 只返回已生成的最新 dashboard.html，
  不再触发数据重建（刷新页面 = 静态秒开）。
- POST /api/refresh ：在后台线程执行一次完整重建（tokscale graph +
  build_dashboard.py），立即返回 JSON，不阻塞浏览器。
- GET  /api/refresh/status ：返回最近一次(含进行中)重建状态 JSON。
  页面右上角「刷新数据」按钮与自动定时(默认 1 分钟)即调用 /api/refresh，
  完成后页面自动 reload 拿到新数据。
- 首次启动若 dashboard.html 不存在，会在启动时同步构建一次。

用法：
    python serve.py            # 默认端口 8765
    python serve.py 9000       # 指定端口

不联网上传任何数据，tokscale 仅读取本地文件。按 Ctrl+C 停止服务。
"""

import http.server
import hashlib
import html
import json
import os
import runpy
import socket
import sys
import urllib.parse
import socketserver
import threading
import time
import traceback
from datetime import datetime

import runtime_support as rts

HERE = os.path.abspath(os.environ.get("TOKSCALE_RUNTIME_DIR") or os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("TOKSCALE_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
DASHBOARD = os.path.join(HERE, "dashboard.html")
DASHBOARD_DATA = os.path.join(HERE, "dashboard-data.json")
COST_CONFIG = os.path.join(HERE, "model-cost-config.json")
NPM_CACHE = os.path.join(HERE, ".npm-cache")
REBUILD_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()

# 后台刷新状态；mode: "first"=首次扫描(可显示蒙版) / "silent"=静默刷新(前端不弹蒙版)
STATE = {"busy": False, "ok": None, "error": None, "mode": "silent",
         "started": None, "finished": None, "logId": None, "lastRefreshSec": None}

_DATA_FP = {"key": None, "hash": None}


def data_fingerprint():
    """dashboard-data.json 内容 md5（按 mtime+size 缓存）；不存在返回 None。"""
    try:
        st = os.stat(DASHBOARD_DATA)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    if _DATA_FP["key"] != key:
        h = hashlib.md5()
        try:
            with open(DASHBOARD_DATA, "rb") as f:
                h.update(f.read())
        except OSError:
            return None
        _DATA_FP["key"] = key
        _DATA_FP["hash"] = h.hexdigest()
    return _DATA_FP["hash"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def rebuild(logger=None):
    """串行导出数据并生成面板（在后台线程中调用）。

    logger: 可选 rts.RefreshLogger，用于把完整 cmd/cwd/duration/returncode/
    stdout/stderr/traceback 持久化到 logs/refresh-ID.jsonl（不截断）。
    冻结版(便携包)缺少 tokscale.exe 时明确失败，绝不回退 npx。
    全程更新 rts.PROGRESS 供前端蒙版轮询。
    """
    with REBUILD_LOCK:
        rts.reset_progress()
        os.makedirs(NPM_CACHE, exist_ok=True)
        env = os.environ.copy()
        env["NPM_CONFIG_CACHE"] = NPM_CACHE
        env["NO_PROXY"] = "localhost,127.0.0.1"
        env["no_proxy"] = "localhost,127.0.0.1"

        cmd = rts.resolve_tokscale_command(
            ["graph", "--output", "graph.json", "--no-spinner"])
        if logger:
            logger.event("tokscale_graph_start", cmd=cmd)
        rts.update_progress("扫描本机 AI 会话日志", "tokscale 引擎启动中…", pct=6)
        _stop_heartbeat = threading.Event()

        def _cache_size_mb():
            """tokscale 数据缓存目录大小(MB)；用于给静默扫描一个可感知的进度细节。"""
            total = 0
            root = os.path.join(HERE, ".tokscale")
            for dirpath, _, files in os.walk(root):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
            return total / 1048576.0

        def _heartbeat():
            started = time.time()
            n = 0
            while not _stop_heartbeat.wait(1.0):
                n = int(time.time() - started)
                detail = f"tokscale 引擎静默扫描中 · 已扫描 {n} 秒"
                if n % 5 == 0:  # 目录遍历有开销,每 5 秒采样一次缓存增长
                    try:
                        detail += f" · 数据缓存 {_cache_size_mb():.0f} MB"
                    except Exception:
                        pass
                rts.update_progress("扫描本机 AI 会话日志", detail,
                                    pct=min(62, 6 + n * 1.8))

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        try:
            result = rts.run_command(cmd, cwd=HERE, env=env, timeout=600, silent=True)
        finally:
            _stop_heartbeat.set()
            hb.join(timeout=2)
        if logger:
            logger.log_run("tokscale_graph", result)
        if result["timedOut"]:
            rts.update_progress("扫描失败", "tokscale 执行超时(>600s)", pct=100)
            raise RuntimeError("tokscale graph 执行超时(>600s)")
        if result["traceback"]:
            rts.update_progress("扫描失败", "tokscale 启动失败", pct=100)
            raise RuntimeError("tokscale graph 启动失败：\n" + result["traceback"])
        if result["returncode"] != 0:
            rts.update_progress("扫描失败", f"tokscale 退出码 {result['returncode']}", pct=100)
            raise RuntimeError(
                f"tokscale 退出码 {result['returncode']}\n"
                f"--- stdout ---\n{result['stdout']}\n"
                f"--- stderr ---\n{result['stderr']}"
            )
        rts.update_progress("生成面板", "汇总用量与定价…", pct=64,
                            line="tokscale 扫描完成")

        old_cwd = os.getcwd()
        captured = []
        try:
            os.chdir(HERE)
            if logger:
                logger.event("build_start")
            _tee_runpy(os.path.join(HERE, "build_dashboard.py"), captured, logger)
        except Exception as e:
            rts.update_progress("构建失败", str(e)[:200], pct=100)
            raise RuntimeError(f"面板构建失败：{e}") from e
        finally:
            os.chdir(old_cwd)
            if logger:
                logger.event("build_done", stdout="".join(captured))
        rts.update_progress("完成", "面板已更新", pct=100)


def _tee_runpy(path, captured, logger=None):
    """在 __main__ 命名空间运行 build_dashboard.py，并把其 stdout 同时收集到 captured
    列表（供刷新日志持久化），不破坏原有打印。"""
    real_stdout = sys.stdout
    tee = _TeeStdout(real_stdout, captured)
    old = sys.stdout
    sys.stdout = tee
    try:
        runpy.run_path(path, run_name="__main__")
    finally:
        sys.stdout = old


class _TeeStdout:
    def __init__(self, real, captured):
        self._real = real
        self._captured = captured

    def write(self, s):
        try:
            self._captured.append(s)
        except Exception:
            pass
        try:
            self._real.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


def _refresh_worker(mode="silent"):
    t0 = time.time()
    started = _now()
    logger = rts.RefreshLogger()
    rts.current_refresh_logger = logger
    with STATE_LOCK:
        STATE.update(busy=True, ok=None, error=None, mode=mode,
                     started=started, finished=None, logId=logger.id)
    print(f"[{started}] 后台数据更新开始 (log={logger.id})", flush=True)
    try:
        rebuild(logger)
        with STATE_LOCK:
            STATE.update(busy=False, ok=True, error=None,
                         finished=_now(), logId=logger.id,
                         lastRefreshSec=round(time.time() - t0, 1))
        try:
            # 首次扫描完成标记：此后所有刷新(含后续启动)一律静默。
            os.makedirs(HERE, exist_ok=True)
            with open(os.path.join(HERE, ".first_scan_done"), "w") as f:
                f.write(_now())
        except Exception:
            pass
        logger.event("refresh_ok", logId=logger.id)
        print(f"[{STATE['finished']}] 后台数据更新完成 (log={logger.id})", flush=True)
    except Exception as e:
        tb = traceback.format_exc()
        logger.event("refresh_failed", error=str(e), traceback=tb, logId=logger.id)
        print(f"[{_now()}] 后台数据更新失败：{e}", flush=True)
        with STATE_LOCK:
            STATE.update(busy=False, ok=False, error=str(e),
                         finished=_now(), logId=logger.id,
                         lastRefreshSec=round(time.time() - t0, 1))


def start_refresh(mode="silent"):
    """若无后台更新在跑则启动一个；返回是否本次新启动。

    mode: "first"=首次扫描（前端显示进度蒙版）；"silent"=静默刷新（默认）。
    """
    with STATE_LOCK:
        if STATE["busy"]:
            return False
        STATE["busy"] = True  # 先占位，防止并发双启动
        STATE["mode"] = mode
    threading.Thread(target=_refresh_worker, daemon=True,
                     kwargs={"mode": mode}).start()
    return True


def snapshot_state():
    with STATE_LOCK:
        snap = {k: STATE[k] for k in ("busy", "ok", "error", "mode", "lastRefreshSec",
                                      "started", "finished", "logId")}
    snap["progress"] = rts.progress_snapshot()
    snap["dataHash"] = data_fingerprint()
    return snap


def read_cost_config():
    with CONFIG_LOCK:
        try:
            with open(COST_CONFIG, encoding="utf-8") as f:
                data = json.load(f) or {}
            return data if isinstance(data, dict) else {"models": {}}
        except Exception:
            return {"_meta": {"version": 1, "baseCurrency": "USD",
                              "displayCurrency": "CNY"}, "models": {}}


def save_model_cost_config(model, exchange_rate, multiplier):
    if not isinstance(model, str) or not model.strip() or len(model) > 300:
        raise ValueError("模型 ID 无效")
    try:
        exchange_rate = float(exchange_rate)
        multiplier = float(multiplier)
    except (TypeError, ValueError):
        raise ValueError("汇率和倍率必须是数字")
    if not (0 < exchange_rate <= 1000):
        raise ValueError("汇率必须大于 0 且不超过 1000")
    if not (0 < multiplier <= 1000):
        raise ValueError("倍率必须大于 0 且不超过 1000")
    with CONFIG_LOCK:
        try:
            with open(COST_CONFIG, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        models = data.get("models")
        if not isinstance(models, dict):
            models = data["models"] = {}
        old = models.get(model) if isinstance(models.get(model), dict) else {}
        models[model] = {
            **old,
            "exchangeRate": exchange_rate,
            "multiplier": multiplier,
        }
        meta = data.get("_meta")
        if not isinstance(meta, dict):
            meta = data["_meta"] = {}
        meta.update(version=1, baseCurrency="USD", displayCurrency="CNY",
                    formula="usdCost * exchangeRate * multiplier",
                    updatedAt=_now())
        tmp = COST_CONFIG + ".tmp.%s.%s" % (os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, COST_CONFIG)
        return models[model]


def first_run_page():
    """首次运行的轻量加载页：自动扫描本机日志，成功后切换到正式面板。"""
    return inject_debug("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tokscale 用量面板正在初始化</title><style>
*{box-sizing:border-box}body{margin:0;background:#faf9f7;color:#2c2c2a;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.7}
main{max-width:620px;margin:14vh auto 0;padding:0 24px}.box{background:#fff;border:1px solid #e5e3dc;border-radius:14px;padding:28px 30px;box-shadow:0 8px 28px #0000000a}
h1{font-size:20px;font-weight:500;margin:0 0 8px}.sub{font-size:13px;color:#77756f;margin:0}.status{display:flex;align-items:center;gap:10px;margin:22px 0 8px;font-size:14px}
.track{height:6px;background:#ece9e2;border-radius:4px;overflow:hidden;margin:14px 0 6px}
.fill{height:100%;background:#185fa5;border-radius:4px;transition:width .6s ease}
.dyn{font-size:11px;color:#96938c;max-height:120px;overflow:hidden;line-height:1.9;margin-top:10px}
.spin{width:17px;height:17px;border:2px solid #d9d6ce;border-top-color:#185fa5;border-radius:50%;animation:r .8s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
.err{white-space:pre-wrap;background:#fff4f2;border:1px solid #f1c7c0;color:#8f2d20;border-radius:8px;padding:10px 12px;font-size:12px;margin-top:14px;max-height:180px;overflow:auto}
button{font:inherit;font-size:13px;border:1px solid #2c2c2a;background:#2c2c2a;color:#fff;border-radius:8px;padding:7px 14px;cursor:pointer;margin-top:12px}button[hidden]{display:none}
.note{font-size:12px;color:#96938c;margin-top:18px}</style></head><body><main><div class="box"><h1>正在初始化本机用量面板</h1>
<p class="sub">首次运行会扫描当前 Windows 用户的本地 AI 客户端会话记录，不会上传数据。通常需要数秒到数十秒。</p>
<div class="status"><span id="spin" class="spin"></span><span id="text">正在扫描本机日志并生成面板…</span></div>
<div class="track"><div class="fill" id="fill" style="width:4%"></div></div>
<div class="dyn" id="dyn"></div>
<div id="error" class="err" hidden></div><button id="retry" hidden>重新扫描</button>
<p class="note">以后启动将直接显示上次成功生成的面板，数据可在页面中静默刷新。</p></div></main><script>
const text=document.getElementById('text'),spin=document.getElementById('spin'),err=document.getElementById('error'),retry=document.getElementById('retry');
const fill=document.getElementById('fill'),dyn=document.getElementById('dyn');
function failed(msg){spin.hidden=true;text.textContent='首次扫描未完成';err.textContent=msg||'未知错误';err.hidden=false;retry.hidden=false;}
function poll(){fetch('/api/refresh/status',{cache:'no-store'}).then(r=>r.json()).then(j=>{
  if(j.busy){const p=j.progress||{};fill.style.width=Math.max(4,p.pct||6)+'%';
    if(p.stage)text.textContent=p.stage;if(p.detail)dyn.textContent=p.detail;
    if(p.lines&&p.lines.length)dyn.innerHTML=p.lines.slice(-5).map(l=>'<div>'+l+'</div>').join('');
    setTimeout(poll,1000);return;}
  if(j.ok===true){location.reload();return;}
  if(j.ok===false){failed(j.error);return;}
  setTimeout(poll,1200);}).catch(()=>setTimeout(poll,1800));}
retry.onclick=()=>{retry.hidden=true;err.hidden=true;spin.hidden=false;text.textContent='正在重新扫描…';fetch('/api/refresh',{method:'POST'}).then(()=>poll()).catch(()=>poll());};
poll();
</script></body></html>""")


# ---------------------------------------------------------------- Debug 注入与面板


def error_page(msg):
    safe = html.escape(msg)
    return inject_debug(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>面板暂不可用</title><style>
body{{font-family:-apple-system,"Segoe UI","PingFang SC",sans-serif;background:#faf9f7;
color:#2c2c2a;padding:48px 24px;max-width:720px;margin:0 auto;line-height:1.7}}
h1{{font-size:19px;font-weight:500}} code{{background:#f1efe8;padding:2px 6px;border-radius:4px;font-size:13px}}
pre{{background:#fff;border:1px solid #e5e3dc;border-radius:8px;padding:14px;overflow:auto;font-size:12px}}
</style></head><body><h1>面板暂时无法生成</h1>
<p>尚无可回退的历史页面。请稍后在页面点「刷新数据」，或确认 <code>npx</code> 可用。</p>
<pre>{safe}</pre></body></html>""")


# ---------------------------------------------------------------- Debug 注入与面板

# 由 serve 在“提供所有 dashboard HTML 时”注入：右上固定 Debug 按钮（旧缓存页也生效）。
DEBUG_INJECT = (
    '<script>(function(){if(window.__tokscaleDebugInjected)return;'
    'window.__tokscaleDebugInjected=true;'
    'var b=document.createElement("a");b.href="/debug";b.target="_self";'
    'b.textContent="调试";'
    'b.style.cssText="position:fixed;top:12px;right:12px;z-index:99999;'
    'background:#2c2c2a;color:#fff;font:13px/1 -apple-system,\\\'Segoe UI\\\','
    '\\\'Microsoft YaHei\\\',sans-serif;padding:8px 13px;border-radius:8px;'
    'text-decoration:none;box-shadow:0 4px 14px rgba(0,0,0,.18)";'
    'document.body.appendChild(b);})();</script>'
)


def inject_debug(html_text):
    """把右上固定 Debug 按钮注入到 HTML 文本（在 </body> 前）。"""
    if "</body>" in html_text:
        return html_text.replace("</body>", DEBUG_INJECT + "</body>", 1)
    return html_text + DEBUG_INJECT


def _debug_page():
    """同窗口 Debug UI：列出日志、查看完整内容、刷新列表、返回面板。"""
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tokscale 调试面板</title><style>
*{box-sizing:border-box}body{margin:0;background:#faf9f7;color:#2c2c2a;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
header{position:sticky;top:0;background:#2c2c2a;color:#fff;padding:12px 18px;display:flex;gap:12px;align-items:center;z-index:5}
header h1{font-size:15px;font-weight:500;margin:0;flex:1}
header button{font:inherit;font-size:13px;border:1px solid #5a5a55;background:#3a3a36;color:#fff;border-radius:8px;padding:6px 12px;cursor:pointer}
header a{font:inherit;font-size:13px;border:1px solid #5a5a55;background:#fff;color:#2c2c2a;border-radius:8px;padding:6px 12px;text-decoration:none}
main{max-width:980px;margin:18px auto;padding:0 18px}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e3dc;border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid #eee;white-space:nowrap}
th{background:#f3f1ea;font-weight:500;color:#666}
tr:last-child td{border-bottom:none}
tr.click{cursor:pointer}
tr.click:hover{background:#f6f4ee}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;background:#eef;color:#346}
.tag.client{background:#fde;color:#933}
pre{background:#1e1e1e;color:#e6e6e6;padding:14px;border-radius:10px;overflow:auto;max-height:62vh;font-size:12px;white-space:pre-wrap;word-break:break-word}
.muted{color:#96938c;font-size:12px}
#view{margin-top:16px}
#back2{margin-top:14px}
</style></head><body><header><h1>Tokscale 调试面板</h1>
<button id="refresh">刷新列表</button><a href="/">返回用量面板</a></header>
<main><div id="list">加载中…</div><div id="view" hidden></div>
<button id="back2" hidden>← 返回列表</button></main><script>
const listEl=document.getElementById('list'),viewEl=document.getElementById('view'),back2=document.getElementById('back2');
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function renderList(rows){if(!rows.length){listEl.innerHTML='<p class="muted">暂无调试日志。</p>';return;}
let h='<table><thead><tr><th>类型</th><th>文件</th><th>大小</th><th>时间</th></tr></thead><tbody>';
for(const r of rows){h+='<tr class="click" data-name="'+esc(r.name)+'"><td>'+(r.type==='client'?'<span class="tag client">前端错误</span>':'<span class="tag">刷新</span>')+'</td><td>'+esc(r.name)+'</td><td>'+(r.size/1024).toFixed(1)+' KB</td><td>'+esc(r.mtime)+'</td></tr>';}
h+='</tbody></table>';listEl.innerHTML=h;
listEl.querySelectorAll('tr.click').forEach(tr=>tr.onclick=()=>openLog(tr.dataset.name));}
function openLog(name){fetch('/api/debug/log?id='+encodeURIComponent(name)).then(r=>r.json()).then(j=>{if(!j||j.error){viewEl.innerHTML='<pre>读取失败：'+(j?j.error:'未知')+'</pre>';return;}
let txt=j.lines.map(ln=>{try{return JSON.stringify(JSON.parse(ln),null,1);}catch(e){return ln;}}).join('\\n');
viewEl.innerHTML='<pre>'+esc(txt)+'</pre>';viewEl.hidden=false;listEl.hidden=true;back2.hidden=false;}).catch(e=>{viewEl.innerHTML='<pre>请求失败：'+esc(e)+'</pre>';viewEl.hidden=false;listEl.hidden=true;back2.hidden=false;});}
function load(){fetch('/api/debug/logs',{cache:'no-store'}).then(r=>r.json()).then(j=>{const rows=(j&&j.logs)||[];listEl.hidden=false;viewEl.hidden=true;back2.hidden=true;renderList(rows);}).catch(e=>{listEl.innerHTML='<p class="muted">列表加载失败：'+esc(e)+'</p>';});}
document.getElementById('refresh').onclick=load;back2.onclick=load;load();
</script></body></html>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            raise ValueError("请求内容长度无效")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            raise ValueError("请求 JSON 无效")

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/refresh":
            triggered = start_refresh("silent")
            self.send_json({"triggered": triggered, **snapshot_state()})
            return
        if p == "/api/model-cost-config":
            try:
                body = self.read_json_body()
                saved = save_model_cost_config(
                    body.get("model"), body.get("exchangeRate"),
                    body.get("multiplier"))
                self.send_json({"ok": True, "model": body.get("model"),
                                "config": saved})
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)}, code=400)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)[-400:]}, code=500)
            return
        if p == "/api/debug/client-error":
            try:
                body = self.read_json_body()
                entries = body.get("entries") if isinstance(body, dict) else None
                if not isinstance(entries, list):
                    entries = []
                # 仅保留错误条目本身，绝不包含环境变量或会话内容。
                clean = []
                for e in entries[:200]:
                    if isinstance(e, dict):
                        clean.append({
                            "t": e.get("t"),
                            "kind": str(e.get("kind"))[:32],
                            "detail": str(e.get("detail"))[:2000],
                        })
                name = rts.write_client_error(clean)
                self.send_json({"ok": True, "name": name})
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)}, code=400)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, code=500)
            return
        self.send_json({"error": "not found"}, code=404)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/refresh/status":
            self.send_json(snapshot_state())
            return
        if p == "/api/data":
            try:
                with open(DASHBOARD_DATA, "rb") as f:
                    body = f.read()
                self.send_response(200)
            except OSError:
                self.send_json({"error": "no data file"}, code=404)
                return
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/model-cost-config":
            self.send_json(read_cost_config())
            return
        if p == "/api/debug/logs":
            self.send_json({"logs": rts.list_debug_logs()})
            return
        if p == "/api/debug/log":
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            name = ""
            for pair in q.split("&"):
                if pair.startswith("id="):
                    name = pair[3:]
            name = urllib.parse.unquote(name)
            rec = rts.read_debug_log(name)
            if rec is None:
                self.send_json({"error": "invalid or missing log id"}, code=400)
                return
            self.send_json(rec)
            return
        if p == "/debug":
            body = _debug_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return
        if p in ("/", "/index.html", "/dashboard.html"):
            if os.path.isfile(DASHBOARD) and os.path.getsize(DASHBOARD) > 0:
                try:
                    with open(DASHBOARD, "rb") as f:
                        data = f.read()
                except OSError:
                    self.send_error(404)
                    return
                # 提供所有 dashboard HTML 时注入右上固定 Debug 按钮（旧缓存页也生效）。
                if b"</body>" in data:
                    data = data.replace(b"</body>",
                                        DEBUG_INJECT.encode("utf-8") + b"</body>", 1)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(data)
                return
            else:
                state = snapshot_state()
                if state["busy"] or state["ok"] is None:
                    body = first_run_page().encode("utf-8")
                    code = 200
                else:
                    body = error_page(state["error"] or "首次扫描失败").encode("utf-8")
                    code = 503
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_json({"error": "not found"}, code=404)

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    if not (os.path.isfile(DASHBOARD) and os.path.getsize(DASHBOARD) > 0):
        print("dashboard.html 不存在，启动时同步构建一次…")
        rebuild()
        print("初始构建完成。")
    addr = ("127.0.0.1", PORT)
    with Server(addr, Handler) as httpd:
        print(f"面板服务已启动：http://localhost:{PORT}")
        print("页面为静态秒开；更新数据请 POST /api/refresh 或使用页面按钮/自动定时。Ctrl+C 停止。")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止。")
