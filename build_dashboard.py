#!/usr/bin/env python3
"""从 tokscale graph.json 生成自包含交互式 HTML 面板。

特性：
  - 纯本地、零 CDN 依赖、可离线打开
  - 客户端按钮 + 模型组合框（可搜索）+ 时间范围 三重筛选，切换即时生效
  - 时间范围支持精确到时分秒：边界日（起止日非整天部分）用 tokscale 小时级
    聚合切片，天×模型分项按当日占比折算；整天范围与按天数据完全一致
  - 模型分项明细表（输入/输出/缓存/推理/成本）
  - 模型定价：右键点击模型名查看输入/输出/缓存读写单价，支持改价并按自定义价格重算成本
    （改价保存在浏览器 localStorage，原始数据文件不被修改）

用法：
    python build_dashboard.py

依赖：仅 Python 标准库。首次运行会为缺失定价的模型调用 tokscale pricing（联网，已缓存）。
"""

import hashlib
import json
import glob
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import runtime_support as rts

HERE = os.path.abspath(os.environ.get("TOKSCALE_RUNTIME_DIR") or os.path.dirname(os.path.abspath(__file__)))
GRAPH = os.path.join(HERE, "graph.json")
OUT = os.path.join(HERE, "dashboard.html")
PRICING = os.path.join(HERE, "pricing.json")
COST_CONFIG = os.path.join(HERE, "model-cost-config.json")
DOMESTIC_RATE = 6.78
FOREIGN_RATE = 1.0
DOMESTIC_MODEL_MARKERS = (
    "deepseek", "glm", "z-ai", "zhipu", "qwen", "alibaba",
    "kimi", "moonshot", "minimax", "mimo", "xiaomi", "hunyuan",
    "tencent", "doubao", "baichuan", "yi-", "01-ai", "stepfun",
    "hy3", "hy4", "hunyuan",
)


def is_domestic_model(model):
    """按模型/厂商 ID 判断国内大模型；未知模型按国外模型处理。"""
    m = (model or "").lower()
    return any(x in m for x in DOMESTIC_MODEL_MARKERS)


def default_multiplier(model):
    """GPT 系列默认倍率 0.25，其他模型默认 1。"""
    return 0.25 if "gpt" in (model or "").lower() else 1.0


def ensure_cost_config(models):
    """初始化/补齐每个模型的汇率和倍率配置，保留用户已修改值。"""
    raw = {}
    if os.path.exists(COST_CONFIG):
        try:
            with open(COST_CONFIG, encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
    configs = raw.get("models") if isinstance(raw, dict) else None
    configs = configs if isinstance(configs, dict) else {}
    changed = False
    for model in sorted(models):
        current = configs.get(model)
        if not isinstance(current, dict):
            domestic = is_domestic_model(model)
            configs[model] = {
                "exchangeRate": DOMESTIC_RATE if domestic else FOREIGN_RATE,
                "multiplier": default_multiplier(model),
                "domestic": domestic,
            }
            changed = True
            continue
        detected_domestic = is_domestic_model(model)
        domestic = bool(current.get("domestic", detected_domestic))
        rate = current.get("exchangeRate")
        multiplier = current.get("multiplier")
        if domestic != detected_domestic:
            # 分类规则升级时，只在汇率仍是旧分类默认值的情况下迁移默认汇率，保留人工改价。
            if isinstance(rate, (int, float)) and rate == (DOMESTIC_RATE if domestic else FOREIGN_RATE):
                current["exchangeRate"] = DOMESTIC_RATE if detected_domestic else FOREIGN_RATE
                rate = current["exchangeRate"]
            current["domestic"] = detected_domestic
            domestic = detected_domestic
            changed = True
        if not isinstance(rate, (int, float)) or rate <= 0:
            current["exchangeRate"] = DOMESTIC_RATE if domestic else FOREIGN_RATE
            changed = True
        if not isinstance(multiplier, (int, float)) or multiplier <= 0:
            current["multiplier"] = default_multiplier(model)
            changed = True
        if "domestic" not in current:
            current["domestic"] = domestic
            changed = True
    out = {
        "_meta": {
            "version": 1,
            "baseCurrency": "USD",
            "displayCurrency": "CNY",
            "domesticDefaultRate": DOMESTIC_RATE,
            "foreignDefaultRate": FOREIGN_RATE,
            "formula": "usdCost * exchangeRate * multiplier",
            "updatedAt": (raw.get("_meta") or {}).get("updatedAt") if isinstance(raw, dict) else None,
        },
        "models": configs,
    }
    if changed or not os.path.exists(COST_CONFIG):
        out["_meta"]["updatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = COST_CONFIG + ".tmp.%s" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, COST_CONFIG)
        print("  成本配置已初始化/补齐: %d 个模型" % len(configs))
    return configs

# 价格权威源:用户开源项目 basellm/llm-metadata(每 6 小时 CI 自动发布新价)
BASELLM_URL = "https://raw.githubusercontent.com/basellm/llm-metadata/main/dist/api/newapi/models.json"
BASELLM_SNAP = os.path.join(HERE, "basellm_models.json")
BASELLM_TTL = 6 * 3600  # 与上游 CI 对齐:TTL 内直接复用本地快照,不重复下载

BUCKET_LABELS = [
    ("i", "输入", "#378ADD"),
    ("o", "输出", "#1D9E75"),
    ("cr", "缓存读取", "#BA7517"),
    ("cw", "缓存写入", "#7F77DD"),
    ("r", "推理", "#D4537E"),
]
CLIENT_COLORS = {
    "all": "#5F5E5A", "codex": "#185FA5", "opencode": "#0F6E56",
    "claude": "#993C1D", "hermes": "#534AB7", "workbuddy": "#A32D2D",
    "openclaw": "#854F0B", "cursor": "#3B6D11", "gemini": "#0C447C",
}


# ---------------------------------------------------------------- 定价获取

def _norm_price(j):
    p = (j or {}).get("pricing") or {}
    if not p:
        return None
    return {
        "i": p.get("inputCostPerToken"),
        "o": p.get("outputCostPerToken"),
        "cr": p.get("cacheReadInputTokenCost"),
        "cw": p.get("cacheWriteInputTokenCost"),
        "src": j.get("source", ""),
    }


def _fetch_one(model):
    """用共享 run_command 取单模型定价；线程池内失败时记录到同次刷新日志，不截断。"""
    try:
        cmd = rts.resolve_tokscale_command(["pricing", model, "--json"])
    except RuntimeError as e:
        # 冻结版缺 exe：明确失败，记录到当前刷新日志（不静默吞掉）。
        if rts.current_refresh_logger is not None:
            rts.current_refresh_logger.log_pricing(model, {
                "timedOut": False, "returncode": None,
                "stdout": "", "stderr": str(e), "traceback": None,
            })
        return None
    result = rts.run_command(cmd, cwd=HERE, timeout=180, silent=True)
    if rts.current_refresh_logger is not None:
        rts.current_refresh_logger.log_pricing(model, result)
    s = result.get("stdout") or ""
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return _norm_price(json.loads(s[a:b + 1]))
    except Exception:
        return None


# ---------------------------------------------------------------- basellm/llm-metadata 定价同步

VENDOR_ALIAS = {
    "z-ai": "zai", "zhihui": "zai", "deepseek-ai": "deepseek",
    "gemini": "google", "moonshotai": "moonshot", "x-ai": "xai",
    "qwen": "alibaba", "models.dev": "modelsdev",
}
# 无厂商前缀的裸模型名,遇到同名多厂商时按此优先级取权威厂商(优先级 0 最高)
VENDOR_PREF = ["openai", "anthropic", "google", "zai", "deepseek", "moonshot",
               "alibaba", "minimax", "xiaomi", "xai", "tencent"]
# 默认排除名单:这些模型不随开源项目自动覆盖(与权威源存在口径冲突,保留现价)。
# deepseek 系保留:panel 现价 = deepseek 官方 PEAK 价($0.44/$1.32/$0.014),basellm
# 数据库给出的 $0.14/$0.28/$0.0028 是其自身的折扣值,非 deepseek 官方权威,不应覆盖。
# 可在 pricing.json 的 _meta.excludeFromBasellm 中增删(会与默认名单合并)。
DEFAULT_BASELLM_EXCLUDE = {"mimo-v2-pro", "deepseek-v4-flash", "deepseek-ai/deepseek-v4-pro"}


def _norm_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _download_text(url, timeout=25):
    """统一静默下载；每次失败完整记录，离线时沿用缓存。"""
    for attempt in range(4):
        cmd = ["curl", "--fail", "--show-error", "-s", "-m", str(timeout),
               "-A", "tokscale-dashboard/1.3"]
        if attempt < 3:
            cmd += ["--noproxy", "*"]
        result = rts.run_command(cmd + [url], timeout=timeout + 15, silent=True)
        if rts.current_refresh_logger:
            rts.current_refresh_logger.log_run("pricing_download", result)
        if result["returncode"] == 0 and result["stdout"]:
            return result["stdout"]
    print("  basellm 下载失败，完整错误见 Debug 日志")
    return None


def _fetch_basellm():
    """下载 basellm/llm-metadata 最新 models.json 并写快照;失败时退回本地快照。"""
    txt = _download_text(BASELLM_URL)
    if txt is None and os.path.exists(BASELLM_SNAP):
        try:
            txt = open(BASELLM_SNAP, encoding="utf-8").read()
        except Exception:
            txt = None
    if not txt:
        return None
    try:
        items = (json.loads(txt) or {}).get("data") or []
    except Exception as e:
        print("  basellm 数据解析失败: %s" % e)
        return None
    try:
        with open(BASELLM_SNAP, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass
    return items


def _resolve_basellm(model, idx):
    """把面板模型名映射到 basellm 记录(vendor 拆解 + 别名归一 + 权威厂商优先)。"""
    if "/" in model:
        v, mm = model.split("/", 1)
        vn = _norm_key(VENDOR_ALIAS.get(v, v))
        cands = [r for r in idx.get(_norm_key(mm), []) if _norm_key(r.get("vendor_name")) == vn]
        if cands:
            return cands[0]
    cands = idx.get(_norm_key(model), [])
    return cands[0] if cands else None


def sync_basellm(cache, meta):
    """以开源项目 basellm/llm-metadata 为权威源,覆盖式更新已收录模型单价。

    6 小时 TTL 内复用本地快照不重复下载;下载失败则沿用现有价格,不影响面板。
    返回 (cache, 命中的模型集合);basellm 缺失的模型保持原值,由 tokscale pricing 兜底。
    """
    items, fresh = None, True
    if os.path.exists(BASELLM_SNAP) and time.time() - os.path.getmtime(BASELLM_SNAP) < BASELLM_TTL:
        fresh = False
        try:
            items = (json.load(open(BASELLM_SNAP, encoding="utf-8")) or {}).get("data")
        except Exception:
            items = None
        if not items:
            items, fresh = _fetch_basellm(), True
    else:
        items = _fetch_basellm()
    if not items:
        print("  basellm 同步跳过(无快照/网络不可用),沿用现有定价")
        return cache, set()
    # 建立索引:归一模型名 -> 候选(按权威厂商排序)
    idx = {}
    for r in items:
        idx.setdefault(_norm_key(r.get("model_name")), []).append(r)
    for v in idx.values():
        v.sort(key=lambda r: (VENDOR_PREF.index(_norm_key(r.get("vendor_name")))
                              if _norm_key(r.get("vendor_name")) in VENDOR_PREF else 99))
    hits = set()
    exclude = set(meta.get("excludeFromBasellm") or []) | DEFAULT_BASELLM_EXCLUDE
    for m, p in cache.items():
        if m in exclude:
            continue
        rec = _resolve_basellm(m, idx)
        if not rec:
            continue
        if not isinstance(p, dict):
            # 定价为 null 的条目:basellm 收录了该模型则补齐价格(如 glm5 改名后的 z-ai/glm5)
            p = cache[m] = {}
        val = {k: rec.get(k) for k in ("price_per_m_input", "price_per_m_output",
                                       "price_per_m_cache_read", "price_per_m_cache_write")}
        upd = False
        for pk, bk in (("i", "price_per_m_input"), ("o", "price_per_m_output"),
                       ("cr", "price_per_m_cache_read"), ("cw", "price_per_m_cache_write")):
            x = val.get(bk)
            if x is not None:          # basellm 未提供该项(如无缓存价)则保留原值
                p[pk] = x / 1e6
                upd = True
        if upd:
            p["src"] = "basellm/llm-metadata"
            hits.add(m)
    if hits:
        meta["pricingSource"] = "basellm/llm-metadata"
        if fresh:
            meta["basellmSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        else:
            meta["basellmSyncedAt"] = datetime.fromtimestamp(
                os.path.getmtime(BASELLM_SNAP)).strftime("%Y-%m-%d %H:%M") + "(快照)"
        meta["basellmMatched"] = len(hits)
        meta["excludeFromBasellm"] = sorted(exclude)
    print("  basellm 同步: %s,命中 %d/%d 个模型(排除 %d 个: %s)"
          % ("最新在线" if fresh else "本地快照", len(hits), len(cache),
             len(exclude), ",".join(sorted(exclude)) or "-"))
    return cache, hits


def ensure_pricing(models):
    """读 pricing.json 缓存 → basellm 开源项目同步已有模型 → 缺失模型 tokscale pricing 补齐。"""
    if not models:
        return {}
    meta, cache = {}, {}
    for path in (PRICING, PRICING + ".new"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f) or {}
            if isinstance(raw, dict):
                meta = raw.get("_meta") or {}
                # 保留全部条目(含定价为 null 的):null 条目若 basellm 收录会被下方同步补价;
                # 若也未收录则维持 null 并避免每次构建都当"缺失"联网重查(源都没有,重查徒增耗时)。
                cache = {k: v for k, v in raw.items() if k != "_meta"}
                break
        except Exception:
            cache = {}
    cache, hits = sync_basellm(cache, meta)
    miss = [m for m in models if m not in cache]
    # 排除名单:仅当模型被开源项目错误覆盖(src=basellm/llm-metadata)时回刷到 tokscale 权威源,
    # 一次性回正,避免每次构建都重复联网拉取固定价(3 模型 ~21s 网络)
    refresh = [m for m in DEFAULT_BASELLM_EXCLUDE
               if m in cache and (cache[m] or {}).get("src") == "basellm/llm-metadata"]
    if refresh:
        print("  排除名单回刷到 tokscale: %s" % ", ".join(sorted(refresh)))
        with ThreadPoolExecutor(max_workers=6) as ex:
            for m, res in zip(refresh, ex.map(_fetch_one, refresh)):
                if res:
                    cache[m] = res
    if miss:
        print("获取 %d 个模型的定价(tokscale 兜底,basellm 未收录;首次较慢,之后走缓存)..."
              % len(miss))
        done = 0
        total = len(miss)
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(_fetch_one, m): m for m in miss}
            for fut in as_completed(futures):
                m = futures[fut]
                try:
                    res = fut.result()
                except Exception:
                    res = None
                cache[m] = res
                done += 1
                rts.update_progress("获取模型定价",
                                    f"{done}/{total} · {m}",
                                    pct=72 + done * 24 // max(total, 1),
                                    line="定价 " + m)
    if miss or hits or refresh:
        _save_pricing(meta, cache)
        ok = sum(1 for v in cache.values() if v)
        print("定价完成: %d/%d 个模型有价格数据" % (ok, len(cache)))
    return cache


def _save_pricing(meta, cache):
    """持久化定价缓存；主文件被占用(杀软/同步盘锁)时降级到 .new，两者皆败则跳过。"""
    body = {**cache, "_meta": meta}
    for cand in (PRICING, PRICING + ".new"):
        tmp = cand + ".tmp.%d" % os.getpid()
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=1)
            os.replace(tmp, cand)
            return
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    print("  警告: pricing.json 被占用且无法替换,本次定价仅内存生效,下次构建会重新获取")


# ---------------------------------------------------------------- WorkBuddy 积分

def parse_credits():
    """解析 WorkBuddy 本地会话日志中的积分消耗（providerData.rawUsage.credit）。

    返回 {(date, modelId): credit}。tokscale 不统计积分，这是面板自己读原始日志补的。
    逐文件上报实时进度（供启动蒙版进度条）。
    """
    credits = defaultdict(float)
    root = os.path.expanduser("~/.workbuddy/projects")
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    total = len(files)
    for idx, f in enumerate(files, 1):
        try:
            rts.update_progress("解析 WorkBuddy 积分日志",
                                f"{idx}/{total} 个会话文件",
                                pct=64 + idx * 8 // max(total, 1),
                                line="解析 " + os.path.basename(f))
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    if "rawUsage" not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    pd = o.get("providerData") or {}
                    ru = pd.get("rawUsage") or {}
                    c = ru.get("credit", 0) or 0
                    m = pd.get("model")
                    ts = o.get("timestamp")
                    if not m or not ts or not c:
                        continue
                    d = datetime.fromtimestamp(ts / 1000).date().isoformat()
                    credits[(d, m)] += c
        except Exception:
            continue
    return credits


# ---------------------------------------------------------------- 模板

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:#f4f2ee;color:#2c2c2a;font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.45;padding:12px 16px 40px}
.wrap{max-width:1680px;margin:0 auto}
.hdr{display:flex;gap:12px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;margin-bottom:8px}
.hdr h1{font-size:18px;font-weight:600;letter-spacing:-.2px;margin:0}
.hdr .sub{font-size:12px;color:#8a8880;margin:2px 0 0}
.rf{display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.rb{font:inherit;font-size:12px;display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border:1px solid #ddd9d0;background:#fff;border-radius:6px;cursor:pointer;color:#444441;white-space:nowrap}
.rb:hover{border-color:#b4b2a9}
.rb.pri{background:#2c2c2a;color:#fff;border-color:#2c2c2a}
.rb.pri:hover{background:#3d3d3a}
.rb.busy{pointer-events:none;opacity:.85}
.rb.busy svg{animation:rspin 1s linear infinite}
@keyframes rspin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.rb.busy svg{animation:none}#scanMask .fill{transition:none}}
#autoOn{position:absolute;opacity:0;pointer-events:none}
.autoToggle{font-size:12px;color:#444441;display:inline-flex;align-items:center;gap:6px;cursor:pointer;white-space:nowrap;user-select:none}
.autoSwitch{width:28px;height:16px;border-radius:999px;background:#c9c7bf;position:relative;transition:.18s}
.autoSwitch:after{content:"";position:absolute;width:12px;height:12px;left:2px;top:2px;border-radius:50%;background:#fff;box-shadow:0 1px 2px #0003;transition:.18s}
#autoOn:checked+.autoSwitch{background:#2c2c2a}
#autoOn:checked+.autoSwitch:after{transform:translateX(12px)}
#autoOn:focus-visible+.autoSwitch{outline:2px solid #185FA5;outline-offset:2px}
#autoSel{font:inherit;font-size:12px;padding:4px 6px;border:1px solid #ddd9d0;border-radius:6px;background:#fff;color:#2c2c2a;cursor:pointer}
#autoSel:disabled{opacity:.5;cursor:not-allowed}
#autoCount{font-size:12px;color:#8a8880;min-width:78px;white-space:nowrap}
#autoCount.err{color:#A32D2D}
.bar{position:sticky;top:0;background:#f4f2ee;z-index:5;padding:6px 0 8px;border-bottom:1px solid #e5e3dc;margin-bottom:8px}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.fb{font:inherit;font-size:12px;padding:5px 10px;border:1px solid #ddd9d0;background:#fff;border-radius:6px;cursor:pointer;color:#444441;display:flex;align-items:center;gap:6px}
.fb:hover{border-color:#b4b2a9}
.fb.active{background:#2c2c2a;color:#fff;border-color:#2c2c2a}
.fb .amt{font-size:11px;color:#a3a19a;font-variant-numeric:tabular-nums}
.fb.active .amt,.fb.active .usd{color:#c9c7bf}
.mwrap{display:flex;align-items:center;gap:6px;margin-left:auto}
.mwrap label{font-size:12px;color:#8a8880;white-space:nowrap}
.combo{position:relative}
#modelSearch{font:inherit;font-size:12px;padding:5px 24px 5px 8px;border:1px solid #ddd9d0;border-radius:6px;background:#fff;color:#2c2c2a;width:220px}
#modelSearch:focus{outline:none;border-color:#185FA5;box-shadow:0 0 0 2px #185FA533}
#modelSearch::placeholder{color:#a3a19a}
.caret{position:absolute;right:4px;top:50%;transform:translateY(-50%);border:none;background:none;cursor:pointer;color:#8a8880;padding:6px 4px;display:flex}
.caret:hover{color:#444441}
.clist{position:absolute;top:calc(100% + 4px);left:0;width:100%;min-width:280px;max-height:280px;overflow-y:auto;background:#fff;border:1px solid #ddd9d0;border-radius:8px;z-index:30}
.ch{font-size:11px;color:#a3a19a;padding:5px 8px;border-bottom:1px solid #f1efe8;position:sticky;top:0;background:#fff}
.ci{display:flex;justify-content:space-between;gap:12px;padding:6px 8px;font-size:12px;cursor:pointer;color:#444441;align-items:baseline}
.ci:hover,.ci.kb{background:#f1efe8}
.ci.sel{color:#185FA5;font-weight:500}
.ci .c{color:#a3a19a;font-size:11px;font-variant-numeric:tabular-nums;white-space:nowrap}
.ci.none{cursor:default;color:#a3a19a}
.trow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
.tb{font:inherit;font-size:12px;padding:4px 9px;border:1px solid #ddd9d0;background:#fff;border-radius:6px;cursor:pointer;color:#444441}
.tb:hover{border-color:#b4b2a9}
.tb.active{background:#185FA5;color:#fff;border-color:#185FA5}
.dtpair{display:inline-flex;align-items:center;gap:6px;flex-wrap:nowrap}
.dtpair label{font-size:12px;color:#8a8880;white-space:nowrap}
input[type=date],input[type=datetime-local]{font:inherit;font-size:12px;padding:4px 8px;border:1px solid #ddd9d0;border-radius:6px;background:#fff;color:#2c2c2a;font-variant-numeric:tabular-nums}
input[type=datetime-local]{min-width:17.5em;width:17.5em;flex:0 0 17.5em}
input[type=date]:focus,input[type=datetime-local]:focus{outline:none;border-color:#185FA5;box-shadow:0 0 0 2px #185FA533}
.rb:focus-visible,.fb:focus-visible,.tb:focus-visible,.detail-filter select:focus-visible,#autoSel:focus-visible,.ovbar button:focus-visible{outline:2px solid #185FA5;outline-offset:2px}
.sep{font-size:12px;color:#a3a19a;margin:0 2px}
.note{font-size:12px;color:#8a8880;background:#fff;border:1px solid #e5e3dc;border-left:3px solid #BA7517;border-radius:6px;padding:6px 10px;margin:6px 0 0}
.ovbar{display:flex;align-items:center;gap:8px;font-size:12px;color:#8a8880;background:#fff;border:1px solid #e5e3dc;border-left:3px solid #A32D2D;border-radius:6px;padding:6px 10px;margin-bottom:8px;flex-wrap:wrap}
.ovbar button{font:inherit;font-size:12px;border:1px solid #ddd9d0;background:#fff;border-radius:6px;padding:3px 10px;cursor:pointer;color:#444441}
.ovbar button:hover{border-color:#b4b2a9}
.card{background:#fff;border:1px solid #e5e3dc;border-radius:8px;padding:10px 12px;margin-bottom:10px}
.card h2{font-size:13px;font-weight:600;margin-bottom:2px}
.card .hint{font-size:11px;color:#8a8880;margin-bottom:8px;line-height:1.45}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.g2>.card{margin-bottom:0;min-width:0}
.detail-filter{display:flex;align-items:center;gap:8px;justify-content:flex-end;margin:0 0 8px;flex-wrap:wrap}
.detail-filter label{font-size:12px;color:#8a8880;white-space:nowrap}
.detail-filter select{font:inherit;font-size:12px;min-width:170px;padding:6px 28px 6px 9px;border:1px solid #ddd9d0;border-radius:8px;background:#fff;color:#2c2c2a;cursor:pointer}
.detail-filter select:focus{outline:none;border-color:#888780}
.detail-filter .scope{font-size:11px;color:#a3a19a}
.vt{font-size:14px;font-weight:600;margin:4px 0 8px;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.vt .vshare{font-size:12px;color:#a3a19a;font-weight:400}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:8px;margin-bottom:10px}
.kpi{background:#fff;border:1px solid #e5e3dc;border-radius:8px;padding:8px 10px}
.kpi .k{font-size:11px;color:#8a8880;margin-bottom:2px}
.kpi .v{font-size:16px;font-weight:600;letter-spacing:-.2px;line-height:1.25}
.usd{font-size:11px;font-weight:400;color:#8a8880;margin-left:4px;white-space:nowrap}
.kpi .v .usd{font-size:12px;vertical-align:middle}
.kpi .n{font-size:11px;color:#a3a19a;margin-top:1px}
.row{display:grid;grid-template-columns:140px 1fr 168px 52px;align-items:center;gap:8px;margin-bottom:4px;font-size:12px}
.rname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#444441}
.rtrack{background:#f1efe8;border-radius:3px;height:12px;overflow:hidden}
.rfill{height:100%;border-radius:3px;min-width:1px}
.rval{text-align:right;font-variant-numeric:tabular-nums}
.rval .pm{display:block;color:#8a8880;font-size:11px;line-height:1.2;margin-top:0}
.rshare{text-align:right;color:#a3a19a;font-size:11px;font-variant-numeric:tabular-nums}
.stack{display:flex;height:18px;border-radius:4px;overflow:hidden;background:#f1efe8;margin-bottom:8px}
.seg{height:100%}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:#444441}
.lg{display:flex;align-items:center;gap:5px}
.dot{width:8px;height:8px;border-radius:2px;display:inline-block}
.muted{color:#a3a19a}
.empty{font-size:12px;color:#a3a19a;padding:6px 0}
.tw{overflow-x:auto;overflow-y:visible;-webkit-overflow-scrolling:touch}
.tw table{table-layout:auto;width:max-content;min-width:100%;font-size:12px}
.tw th,.tw td{padding:5px 10px;white-space:nowrap}
.tw th{white-space:nowrap;line-height:1.25}
.tw .cost .usd{margin-left:6px}
.tw td:first-child,.tw th:first-child{min-width:168px;max-width:none;white-space:nowrap}
.heatwrap{overflow-x:auto;overflow-y:hidden;padding-bottom:2px}
.heatwrap svg{display:block;max-width:none;flex:none}
table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
th{font-weight:500;color:#8a8880;text-align:right;padding:5px 10px;border-bottom:1px solid #e5e3dc;white-space:nowrap;position:sticky;top:0;background:#fff;z-index:1}
th:first-child{text-align:left}
td{padding:5px 10px;border-bottom:1px solid #f1efe8;text-align:right;white-space:nowrap}
td:first-child{text-align:left;color:#444441}
tbody tr:hover td{background:#faf9f7}
tbody [data-pm],.ci[data-pm]{cursor:context-menu}
.ov{font-size:10px;color:#A32D2D;border:1px solid #E24B4A;border-radius:4px;padding:0 4px;margin-right:6px;white-space:nowrap}
#priceCard{position:fixed;z-index:100;width:340px;background:#fff;border:1px solid #ddd9d0;border-radius:10px;padding:12px 14px;font-size:12px;color:#2c2c2a}
#priceCard .t{font-size:13px;font-weight:500;margin-bottom:2px;word-break:break-all}
#priceCard .s{font-size:11px;color:#a3a19a;margin-bottom:8px}
#priceCard table{width:100%;border-collapse:collapse;margin-bottom:2px}
#priceCard td{padding:3px 0;text-align:right;font-variant-numeric:tabular-nums;border:none}
#priceCard td:first-child{text-align:left;color:#8a8888}
#priceCard td:first-child{text-align:left;color:#8a8880}
#priceCard input{width:100%;font:inherit;font-size:12px;padding:4px 6px;border:1px solid #ddd9d0;border-radius:6px;box-sizing:border-box;text-align:right}
#priceCard input:focus{outline:none;border-color:#888780}
#priceCard .a{display:flex;gap:8px;margin-top:10px}
#priceCard button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid #ddd9d0;background:#fff;border-radius:6px;cursor:pointer;color:#444441}
#priceCard button:hover{border-color:#b4b2a9}
#priceCard button.pri{background:#2c2c2a;color:#fff;border-color:#2c2c2a}
.foot{font-size:12px;color:#a3a19a;text-align:center;margin-top:28px}
#scanMask{position:fixed;inset:0;z-index:200;background:rgba(250,249,247,.9);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center}
#scanMask[hidden]{display:none}
#scanMask .card{width:400px;max-width:86vw;background:#fff;border:1px solid #e5e3dc;border-radius:14px;padding:24px 26px;box-shadow:0 10px 34px #00000012}
#scanMask h3{font-size:15px;font-weight:500;margin:0 0 4px}
#scanMask .dt{font-size:12px;color:#77756f;margin:0 0 12px;min-height:18px}
#scanMask .track{height:6px;background:#ece9e2;border-radius:4px;overflow:hidden}
#scanMask .fill{height:100%;background:#185fa5;border-radius:4px;transition:width .6s ease}
#scanMask .lines{margin-top:12px;font-size:11px;color:#96938c;max-height:110px;overflow:hidden;line-height:1.9}
#budgetBadge{font-size:12px;white-space:nowrap;color:#8a8880;min-width:52px;text-align:right}
#budgetBadge.warn{color:#BA7517;font-weight:500}
#budgetBadge.over{color:#A32D2D;font-weight:500}
#budgetCard{position:fixed;z-index:100;width:300px;background:#fff;border:1px solid #ddd9d0;border-radius:10px;padding:12px 14px;font-size:12px;color:#2c2c2a;box-shadow:0 10px 34px #00000012}
#budgetCard h4{font-size:13px;font-weight:500;margin:0 0 2px}
#budgetCard .s{font-size:11px;color:#a3a19a;margin-bottom:4px}
#budgetCard label{display:block;margin:8px 0 3px;color:#8a8880}
#budgetCard input{width:100%;font:inherit;font-size:12px;padding:4px 6px;border:1px solid #ddd9d0;border-radius:6px;box-sizing:border-box;text-align:right}
#budgetCard input:focus{outline:none;border-color:#888780}
#budgetCard .a{display:flex;gap:8px;margin-top:10px}
#budgetCard button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid #ddd9d0;background:#fff;border-radius:6px;cursor:pointer;color:#444441}
#budgetCard button:hover{border-color:#b4b2a9}
#budgetCard button.pri{background:#2c2c2a;color:#fff;border-color:#2c2c2a}
.tswitch{display:flex;gap:6px;margin-bottom:8px}
@media(max-width:1100px){.g2{grid-template-columns:1fr}.dtpair{flex-wrap:wrap}}
@media(max-width:640px){.row{grid-template-columns:100px 1fr 132px 48px}.mwrap{margin-left:0;width:100%}#modelSearch{flex:1;width:auto}.detail-filter{justify-content:flex-start}.detail-filter select{flex:1;min-width:0}input[type=datetime-local]{min-width:16em;width:16em;flex-basis:16em}}
"""

HTML_TMPL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tokscale 用量面板</title>
<style>__CSS__</style></head><body>
<div id="scanMask" hidden><div class="card">
  <h3 id="maskStage">正在更新数据</h3>
  <p class="dt" id="maskDetail">准备中…</p>
  <div class="track"><div class="fill" id="maskFill" style="width:4%"></div></div>
  <div class="lines" id="maskLines"></div>
</div></div>
<div class='wrap'>
<div class="hdr"><div><h1>AI 用量面板</h1>
<div class="sub">数据区间 __RANGE__ · tokscale v__VER__ · 生成于 __GEN__ · 价格源 __PRICESRC__</div></div>
<div class="rf">
  <button id="refreshBtn" class="rb pri" type="button" title="后台静默重扫本地日志并更新数据，完成后自动刷新本页"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg><span id="refreshLb">刷新数据</span></button>
  <label class="autoToggle" title="开启或关闭定时自动更新">
    <input id="autoOn" type="checkbox" checked><span class="autoSwitch"></span><span>自动更新</span>
  </label>
  <select id="autoSel" title="自动更新间隔">
    <option value="1" selected>每 1 分钟</option>
    <option value="5">每 5 分钟</option>
    <option value="15">每 15 分钟</option>
    <option value="30">每 30 分钟</option>
    <option value="60">每 60 分钟</option>
  </select>
  <span id="autoCount" class="muted"></span>
  <button id="exportBtn" class="rb" type="button" title="把当前筛选范围内的明细数据导出为 CSV 文件">导出CSV</button>
  <button id="budgetBtn" class="rb" type="button" title="设置月度预算（积分 / 人民币成本），接近上限时提醒">预算</button>
  <span id="budgetBadge"></span>
</div></div>
<div class="bar">
  <div class="filters">
    <div id="clientBtns" class="filters" style="gap:8px"></div>
    <div class="mwrap"><label>模型</label>
      <div class="combo" id="modelCombo">
        <input id="modelSearch" type="text" placeholder="全部模型 · 点击或输入筛选" autocomplete="off">
        <button id="comboToggle" class="caret" title="展开/收起"><svg width="10" height="6" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
        <div id="modelList" class="clist" hidden></div>
      </div>
    </div>
  </div>
  <div class="trow">
    <button class="tb" data-t="today">今天</button>
    <button class="tb" data-t="7d">近 7 天</button>
    <button class="tb" data-t="30d">近 30 天</button>
    <button class="tb" data-t="month">本月</button>
    <button class="tb" data-t="all">全部时间</button>
    <span class="sep" style="margin:0 4px">自定义</span>
    <span class="dtpair">
      <label for="fromDate">起</label>
      <input type="datetime-local" id="fromDate" step="1" title="起始时间，精确到秒">
      <span class="sep">至</span>
      <label for="toDate">止</label>
      <input type="datetime-local" id="toDate" step="1" title="结束时间，精确到秒">
    </span>
  </div>
  <div class="note">筛选：客户端 × 模型 × 时间（精确到秒）。成本同时显示人民币终价与美元原价；右键模型名可改单价/汇率/倍率（终价＝美元×汇率×倍率）。非整日起止按小时桶折算（标题栏标「近似值」）；整天 00:00:00–23:59:59 与按天数据一致。自动更新默认每 1 分钟静默进行。</div>
</div>
<div id="content"></div>
<div class="foot">本面板数据来自本地 graph.json，未向任何服务器上传；价格以美元单价为基础，最终成本统一显示人民币：美元原始成本 × 每模型汇率 × 每模型倍率。长上下文调用与特殊计费档可能存在差异</div>
</div><div id="priceCard" hidden></div><div id="budgetCard" hidden></div><script>__JS__</script></body></html>"""

JS = r"""
let DATA=__DATA__;
let PRICES=DATA.prices||{};
let COST_CFG=DATA.costConfigs||{};
let dataGen=DATA.gen||'';
let allDates=[];  // 初始化依赖下方 TODAY/isoLocal，见 initDates()
const recomputeDates=()=>{const ds=DATA.entries.map(e=>e.d).filter(Boolean).sort();const first=ds[0]||TODAY,last=(ds.length&&ds[ds.length-1]>TODAY)?ds[ds.length-1]:TODAY,s=new Date(first+'T00:00:00'),e=new Date(last+'T00:00:00');const out=[];for(let d=new Date(s);d<=e;d.setDate(d.getDate()+1))out.push(isoLocal(d));allDates=out.length?out:[TODAY];};
/* 免刷新更新：后台重建完成后拉取 /api/data 局部重渲染；gen 相同则跳过 */
function applyData(d){
  if(!d||!Array.isArray(d.entries))throw new Error('bad data');
  DATA=d;PRICES=d.prices||{};COST_CFG=d.costConfigs||{};
  dataGen=d.gen||dataGen;
  if(typeof d.fp==='string')dataFp=d.fp;
  recomputeDates();
  syncUI();render();saveState();
}
const $=id=>document.getElementById(id);

/* ---------- 前端 fetch 失败本地记录，待重连上传（不记录任何环境变量/会话内容）---------- */
const __DBG_LS='__tokscaleClientErrors';
function dbgRecord(kind,detail){try{let a=JSON.parse(localStorage.getItem(__DBG_LS)||'[]');a.push({t:Date.now(),kind:String(kind).slice(0,32),detail:String(detail||'').slice(0,2000)});if(a.length>50)a=a.slice(-50);localStorage.setItem(__DBG_LS,JSON.stringify(a));}catch(e){}}
function dbgFlush(){try{let a=JSON.parse(localStorage.getItem(__DBG_LS)||'[]');if(!a.length)return;const batch=a;localStorage.removeItem(__DBG_LS);fetch('/api/debug/client-error',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries:batch})}).then(r=>{if(!r.ok)dbgRecord('flush-http',String(r.status));}).catch(()=>{for(const e of batch)dbgRecord(e.kind,e.detail);});}catch(e){}}
function jget(url){return fetch(url).then(r=>{if(!r.ok)dbgRecord('http',url+' -> '+r.status);dbgFlush();return r.json();}).catch(e=>{dbgRecord('net',url+' -> '+String(e));throw e;});}
function jpost(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>{if(!r.ok)dbgRecord('http',url+' -> '+r.status);dbgFlush();return r.json();}).catch(e=>{dbgRecord('net',url+' -> '+String(e));throw e;});}
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmtT=n=>n>=1e8?(n/1e8).toFixed(2)+' 亿':n>=1e4?(n/1e4).toFixed(1)+' 万':Math.round(n).toLocaleString();
const fmtC=x=>(x>0&&x<0.01)?'￥'+x.toFixed(4):'￥'+x.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtUSD=x=>(x>0&&x<0.01)?'$'+x.toFixed(4):'$'+x.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtBoth=(c,u)=>`${fmtC(c)} <span class="usd" title="应用汇率和倍率前的美元原价">${fmtUSD(u)}</span>`;
const fmtPerM=(c,t)=>t>0?fmtC(c/t*1e6)+'/M':'—';
const fmtP=x=>x<=0?'0%':x<0.01?'<0.01%':x.toFixed(2)+'%';
const fmtM=v=>v==null?'—':'$'+(v>=1?v.toFixed(2):v.toFixed(4));
const MON=['一','二','三','四','五','六','日'];
const BK=[['i','输入','#378ADD'],['o','输出','#1D9E75'],['cr','缓存读取','#BA7517'],['cw','缓存写入','#7F77DD'],['r','推理','#D4537E']];
const CLI_COLOR={all:'#5F5E5A',codex:'#185FA5',opencode:'#0F6E56',claude:'#993C1D',hermes:'#534AB7',workbuddy:'#A32D2D',openclaw:'#854F0B'};

/* ---------- 改价（localStorage，仅本浏览器） ---------- */
const store=(()=>{try{if(typeof localStorage==='undefined')return null;return localStorage;}catch(e){return null;}})();
const OVR=(()=>{try{return store?JSON.parse(store.getItem('tokscaleOvr')||'{}'):{}}catch(e){return{}}})();
const saveOvr=()=>{try{if(store)store.setItem('tokscaleOvr',JSON.stringify(OVR))}catch(e){}};
const SRC_BASELLM=Object.values(PRICES).filter(p=>p&&p.src==='basellm/llm-metadata').length;
// 价格单位:OVR 与 PRICES 均保存为美元/token；弹框显示时乘 1e6 转为美元/百万 token。
// 先计算美元原始成本，再应用每模型配置：最终人民币成本 = 美元成本 × exchangeRate × multiplier。
const usdCost=e=>{const ov=OVR[e.m];
  if(ov){const ro=ov.o||0;return e.i*(ov.i||0)+e.o*ro+e.cr*(ov.cr||0)+e.cw*(ov.cw||0)+e.r*ro;}
  const p=PRICES[e.m];
  if(p&&p.src==='basellm/llm-metadata'){
    const ro=p.o||0;
    return e.i*(p.i||0)+e.o*ro+e.cr*(p.cr||0)+e.cw*(p.cw||0)+e.r*ro;
  }
  return e.cost;
};
const costCfg=m=>COST_CFG[m]||{exchangeRate:1,multiplier:/gpt/i.test(m)?0.25:1};
const effCost=e=>{const c=costCfg(e.m);return usdCost(e)*(Number(c.exchangeRate)||1)*(Number(c.multiplier)||1);};

let state={client:'all',model:'all',preset:'today',detailClient:'all'};
let range={start:null,end:null};

/* ---------- 时间筛选（支持时分秒；边界日按小时折算） ---------- */
const normDT=v=>{if(!v)return null;v=String(v).trim().replace(' ','T');
  if(/^\d{4}-\d{2}-\d{2}$/.test(v))return v+'T00:00:00';
  const m=v.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2}))?(?:\.\d+)?$/);
  if(m)return m[1]+':'+(m[2]||'00');
  return v;};
const hourEnd=bs=>{const hh=+bs.slice(11,13);
  return hh<23?bs.slice(0,11)+String(hh+1).padStart(2,'0')+':00:00':shiftISO(bs.slice(0,10),1)+'T00:00:00';};
let rangeApprox=false;  // 本轮筛选是否含折算成分（边界日）
/* 边界日（起止日非整天部分）折算因子：小时桶聚合 ÷ 当天日聚合，天级预算一次 */
function partialDayFactors(){
  const fac={};
  if((!DATA.hours||!DATA.hours.length)||(!range.start&&!range.end))return fac;
  const sDT=range.start||'0000-01-01T00:00:00',eDT=range.end||'9999-12-31T23:59:59';
  const sT=range.start?range.start.slice(11,19):'00:00:00',eT=range.end?range.end.slice(11,19):'23:59:59';
  const sd=range.start?range.start.slice(0,10):null,ed=range.end?range.end.slice(0,10):null;
  const need=d=>(sd&&d===sd&&sT>'00:00:00')||(ed&&d===ed&&eT<'23:59:59');
  const hit={},day={};
  for(const h of DATA.hours){
    const bs=h.h;if(bs>=eDT)continue;
    if(hourEnd(bs)<=sDT)continue;
    const k=bs.slice(0,10),o=hit[k]||(hit[k]={t:0,m:0});
    o.t+=h.i+h.o+h.cr+h.cw;o.m+=h.msg||0;
  }
  for(const e of DATA.entries){
    const d=e.d;if(sd&&d<sd)continue;if(ed&&d>ed)continue;
    if(!need(d))continue;
    const o=day[d]||(day[d]={t:0,m:0});
    o.t+=e.i+e.o+e.cr+e.cw;o.m+=e.msg||0;
  }
  for(const d in day){
    const hh=hit[d];
    if(!hh){fac[d]=null;continue;}  // 无小时数据：整天计入（退化）
    const dt=day[d].t,dm=day[d].m;
    /* 日级快照可能滞后于小时级（graph.json 未及时刷新），因子封顶 1 防止虚增 */
    fac[d]={t:dt>0?Math.min(1,hh.t/dt):(hh.t>0?1:0),m:dm>0?Math.min(1,hh.m/dm):(hh.m>0?1:0)};
  }
  return fac;
}
/* 当前筛选下的有效条目：整天原样，边界日缩放克隆（成本由 token 等比推导，保持口径一致） */
function filteredEntries(){
  rangeApprox=false;
  if(!range.start&&!range.end)return DATA.entries;
  const sd=range.start?range.start.slice(0,10):null,ed=range.end?range.end.slice(0,10):null;
  const sT=range.start?range.start.slice(11,19):'00:00:00',eT=range.end?range.end.slice(11,19):'23:59:59';
  const need=d=>(sd&&d===sd&&sT>'00:00:00')||(ed&&d===ed&&eT<'23:59:59');
  const fac=partialDayFactors(),out=[];
  for(const e of DATA.entries){
    const d=e.d;if(sd&&d<sd)continue;if(ed&&d>ed)continue;
    const f=need(d)?(fac[d]===undefined?null:fac[d]):undefined;
    if(f){  // 非 null：按小时折算
      if(f.t<0.999||f.m<0.999)rangeApprox=true;
      out.push({d:e.d,c:e.c,m:e.m,i:e.i*f.t,o:e.o*f.t,cr:e.cr*f.t,cw:e.cw*f.t,r:e.r*f.t,
        msg:Math.round(e.msg*f.m),cd:(e.cd||0)*f.t,cost:(e.cost||0)*f.t});
    }else{
      if(f===null)rangeApprox=true;  // 边界日缺小时数据，整天计入
      out.push(e);
    }
  }
  return out;
}

/* ---------- 数据范围 ---------- */
const isoLocal=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
const TODAY=isoLocal(new Date());
const shiftISO=(iso,n)=>{const d=new Date(iso+'T00:00:00');d.setDate(d.getDate()+n);return isoLocal(d);};
recomputeDates();  // 初始化 allDates（定义见文件头）
const effDates=()=>{const sd=range.start?range.start.slice(0,10):null,ed=range.end?range.end.slice(0,10):null;return allDates.filter(d=>(!sd||d>=sd)&&(!ed||d<=ed));};
const totalAll=()=>DATA.entries.reduce((s,e)=>s+effCost(e),0);
const totalUsdAll=()=>DATA.entries.reduce((s,e)=>s+usdCost(e),0);
const costTotalsBy=(key,final=true)=>{const m={};DATA.entries.forEach(e=>m[e[key]]=(m[e[key]]||0)+(final?effCost(e):usdCost(e)));return m;};
const clientTotals=()=>{const c=costTotalsBy('c'),u=costTotalsBy('c',false);return Object.entries(c).map(([n,v])=>[n,v,u[n]||0]).sort((a,b)=>b[1]-a[1]);};
const modelTotals=()=>{const c=costTotalsBy('m'),u=costTotalsBy('m',false);return Object.entries(c).map(([n,v])=>[n,v,u[n]||0]).sort((a,b)=>b[1]-a[1]);};

/* ---------- 聚合 ---------- */
function agg(){
  const byDate={},mc={},mu={},mt={},cc={},cu={},bk={},mbk={},cbk={};
  let cost=0,usd=0,tokens=0,msgs=0,credits=0;
  const entries=filteredEntries();
  for(const e of entries){
    if(state.client!=='all'&&e.c!==state.client)continue;
    if(state.model!=='all'&&e.m!==state.model)continue;
    const u=usdCost(e),c=effCost(e);
    const d=byDate[e.d]||(byDate[e.d]={cost:0,usd:0,tokens:0,msgs:0,cd:0,b:{}});
    d.cost+=c;d.usd+=u;d.msgs+=e.msg;d.cd+=e.cd||0;
    const t=e.i+e.o+e.cr+e.cw+e.r;
    for(const[k]of BK){d.b[k]=(d.b[k]||0)+e[k];bk[k]=(bk[k]||0)+e[k];}
    d.tokens+=t;tokens+=t;cost+=c;usd+=u;msgs+=e.msg;
    mc[e.m]=(mc[e.m]||0)+c;mu[e.m]=(mu[e.m]||0)+u;mt[e.m]=(mt[e.m]||0)+t;
    cc[e.c]=(cc[e.c]||0)+c;cu[e.c]=(cu[e.c]||0)+u;
    const mb=mbk[e.m]||(mbk[e.m]={i:0,o:0,cr:0,cw:0,r:0,msg:0,cd:0});
    const cb=cbk[e.c]||(cbk[e.c]={i:0,o:0,cr:0,cw:0,r:0,msg:0,cd:0});
    for(const[k]of BK){mb[k]+=e[k];cb[k]+=e[k];}
    mb.msg+=e.msg;cb.msg+=e.msg;
    mb.cd+=e.cd||0;cb.cd+=e.cd||0;credits+=e.cd||0;
  }
  return{byDate,mc,mu,mt,cc,cu,bk,mbk,cbk,cost,usd,tokens,msgs,cd:credits};
}

/* ---------- 渲染 ---------- */
function kpis(a,dates,models){
  let active=0,peak=null;
  for(const d of dates){const r=a.byDate[d];if(r&&r.tokens>0){active++;if(!peak||r.cost>peak.c)peak={d,c:r.cost,u:r.usd};}}
  const karr=[
    ['总 Token',fmtT(a.tokens),a.tokens.toLocaleString()],
    ['总成本',fmtBoth(a.cost,a.usd),'最终人民币 · 美元原价额度'],
    ['活跃天数',active,`跨度 ${dates.length} 天`],
    ['日均成本',fmtBoth(active?a.cost/active:0,active?a.usd/active:0),'按活跃天计'],
    ['单日峰值',peak?fmtBoth(peak.c,peak.u):'—',peak?peak.d:'—'],
    ['消息总数',a.msgs.toLocaleString(),`${models} 个模型`],
  ];
  if(a.cd>0)karr.push(['积分消耗',a.cd.toLocaleString('zh-CN',{maximumFractionDigits:2}),a.cost>0?`约 ${fmtC(a.cost/a.cd)}/积分 · WorkBuddy 原始记录`:'WorkBuddy 原始记录']);
  return `<div class="kpis">
  ${karr.map(([k,v,n])=>`<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join('')}
  </div>`;
}

let trendMetric=(()=>{try{const v=store.getItem('tokscaleTrend');return['cost','tokens','cd'].indexOf(v)>=0?v:'cost'}catch(e){return'cost'}})();
function trendSwitch(){
  const items=[['cost','成本'],['tokens','Token'],['cd','积分']];
  return `<div class="tswitch">${items.map(([k,lb])=>`<button class="tb${trendMetric===k?' active':''}" data-trend="${k}">${lb}</button>`).join('')}</div>`;
}
function daily(dates,a){
  if(!dates.length)return '<div class="empty">该范围内无数据</div>';
  if(trendMetric==='cd'&&!(a.cd>0))return '<div class="empty">当前筛选范围内无积分数据（积分来自 WorkBuddy 本地日志）</div>';
  const W=920,H=250,pl=66,pr=12,pt=12,pb=30,pw=W-pl-pr,ph=H-pt-pb;
  const getVal=r=>!r?0:(trendMetric==='tokens'?r.tokens:trendMetric==='cd'?(r.cd||0):r.cost);
  const col=trendMetric==='tokens'?'#BA7517':trendMetric==='cd'?'#7F77DD':'#378ADD';
  const mx=Math.max(...dates.map(d=>getVal(a.byDate[d])),0)||1;
  const fmtV=v=>trendMetric==='tokens'?fmtT(v):trendMetric==='cd'?Math.round(v).toLocaleString():'￥'+Math.round(v).toLocaleString();
  const slot=pw/dates.length,bw=Math.min(Math.max(1.5,slot*0.74),40);
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="每日用量趋势">`;
  for(let i=0;i<5;i++){const y=pt+ph-ph*i/4;
    s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="#e5e3dc"/>`;
    s+=`<text x="${pl-8}" y="${y}" text-anchor="end" dominant-baseline="central" font-size="11" fill="#8a8880">${fmtV(mx*i/4)}</text>`;}
  dates.forEach((d,i)=>{const r=a.byDate[d];if(!r||getVal(r)<=0)return;
    const x=pl+i*slot+(slot-bw)/2,h=getVal(r)/mx*ph,y=pt+ph-h;
    s+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h,0.8).toFixed(1)}" rx="1.5" fill="${col}"><title>${d}  ${fmtT(r.tokens)} tokens · 最终 ${fmtC(r.cost)} · 原价 ${fmtUSD(r.usd)}${r.cd>0?' · 积分 '+r.cd.toLocaleString('zh-CN',{maximumFractionDigits:2}):''} · ${r.msgs} 条消息</title></rect>`;});
  const step=Math.max(1,Math.floor(dates.length/7));
  dates.forEach((d,i)=>{if(i%step!==0)return;
    s+=`<text x="${(pl+i*slot+slot/2).toFixed(1)}" y="${H-11}" text-anchor="middle" font-size="11" fill="#8a8880">${d.slice(5)}</text>`;});
  return s+'</svg>';
}

function heat(dates,a){
  if(!dates.length)return '';
  const start=new Date(dates[0]+'T00:00:00');
  const pad=(start.getDay()+6)%7;
  const mx=Math.max(...dates.map(d=>a.byDate[d]?a.byDate[d].cost:0),0)||1;
  const cell=13,gap=3,weeks=Math.floor((pad+dates.length+6)/7);
  const W=34+weeks*(cell+gap)+12,H=7*(cell+gap)+30;
  const lv=v=>v<=0?0:(r=>r<.25?1:r<.5?2:r<.75?3:4)(v/mx);
  let s=`<div class="heatwrap"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="贡献热力图">`;
  let lm=null;const marks={};
  dates.forEach((d,i)=>{const m=new Date(d+'T00:00:00').getMonth()+1;if(m!==lm){marks[Math.floor((pad+i)/7)]=m;lm=m;}});
  for(const[c,m]of Object.entries(marks))s+=`<text x="${34+c*(cell+gap)}" y="10" font-size="11" fill="#8a8880">${m}月</text>`;
  MON.forEach((w,i)=>{if(i%2===0)s+=`<text x="26" y="${20+i*(cell+gap)+cell/2}" text-anchor="end" font-size="10" fill="#8a8880">${w}</text>`;});
  const FILL=['#eceae4','#cfe0f2','#85b7eb','#378add','#185fa5'];
  dates.forEach((d,i)=>{const r=a.byDate[d],v=r?r.cost:0;
    s+=`<rect x="${34+Math.floor((pad+i)/7)*(cell+gap)}" y="${16+(pad+i)%7*(cell+gap)}" width="${cell}" height="${cell}" rx="2.5" fill="${FILL[lv(v)]}"><title>${d}  ${v>0?`最终 ${fmtC(v)}  原价 ${fmtUSD(r.usd)}`:'无数据'}</title></rect>`;});
  return s+'</svg></div>';
}

function stack(bk,total){
  if(total<=0)return '<div class="empty">该范围内无数据</div>';
  let s='<div class="stack">';
  for(const[k,lb,col]of BK){const v=bk[k]||0;if(v<=0)continue;
    s+=`<div class="seg" style="width:${(v/total*100).toFixed(2)}%;background:${col}" title="${lb} ${fmtT(v)} (${(v/total*100).toFixed(1)}%)"></div>`;}
  s+='</div><div class="legend">';
  for(const[k,lb,col]of BK){const v=bk[k]||0;if(v<=0)continue;
    s+=`<div class="lg"><span class="dot" style="background:${col}"></span>${lb} <b>${fmtT(v)}</b> <span class="muted">${fmtP(v/total*100)}</span></div>`;}
  return s+'</div>';
}

function bars(pairs,total,unit,color,usds={},tokens={}){
  pairs=pairs.filter(p=>p[1]>0).sort((a,b)=>b[1]-a[1]);
  if(!pairs.length)return '<div class="empty">无数据</div>';
  const mx=Math.max(...pairs.map(p=>p[1]))||1;
  return pairs.map(([n,v,u])=>{
    const val=unit==='cost'?fmtBoth(v,u==null?(usds[n]||0):u)+(tokens[n]>0?`<span class="pm">${fmtPerM(v,tokens[n])}</span>`:''):fmtT(v);
    return `<div class="row"><div class="rname" title="${esc(n)}">${esc(n)}</div>`
      +`<div class="rtrack"><div class="rfill" style="width:${(v/mx*100).toFixed(1)}%;background:${color}"></div></div>`
      +`<div class="rval">${val}</div><div class="rshare">${fmtP(total?v/total*100:0)}</div></div>`;}).join('');
}

function detailRows(map,costs,usds){
  const rows=Object.entries(map).map(([n,b])=>({n,b,cost:costs[n]||0,usd:usds[n]||0,cd:b.cd||0,tot:b.i+b.o+b.cr+b.cw+b.r}));
  return rows.sort((a,b)=>b.tot-a.tot);
}

function detailClients(){
  const clients=new Set();
  for(const e of filteredEntries()){
    if(state.client!=='all'&&e.c!==state.client)continue;
    clients.add(e.c);
  }
  return [...clients].sort((a,b)=>a.localeCompare(b,'zh-CN'));
}

function normalizeDetailClient(){
  const clients=detailClients();
  if(state.detailClient!=='all'&&clients.indexOf(state.detailClient)<0)state.detailClient='all';
  return clients;
}

function modelDetailAgg(){
  const mbk={},mc={},mu={};
  for(const e of filteredEntries()){
    if(state.client!=='all'&&e.c!==state.client)continue;
    if(state.detailClient!=='all'&&e.c!==state.detailClient)continue;
    const b=mbk[e.m]||(mbk[e.m]={i:0,o:0,cr:0,cw:0,r:0,msg:0,cd:0});
    for(const[k]of BK)b[k]+=e[k];
    b.msg+=e.msg;b.cd+=e.cd||0;
    mc[e.m]=(mc[e.m]||0)+effCost(e);
    mu[e.m]=(mu[e.m]||0)+usdCost(e);
  }
  return{mbk,mc,mu};
}

function detailClientFilter(clients){
  const options=[`<option value="all"${state.detailClient==='all'?' selected':''}>全部客户端</option>`]
    .concat(clients.map(c=>`<option value="${esc(c)}"${state.detailClient===c?' selected':''}>${esc(c)}</option>`));
  const inherited=state.client==='all'?'独立筛选，仅影响本表':`全局已限定 ${esc(state.client)}`;
  return `<div class="detail-filter"><label for="detailClientFilter">明细客户端</label>`
    +`<select id="detailClientFilter" title="仅筛选模型分项明细，不改变上方指标和图表">${options.join('')}</select>`
    +`<span class="scope">${inherited}</span></div>`;
}

function detailTable(rows,modelRows=true){
  if(!rows.length)return '<div class="empty">无数据</div>';
  const showCd=rows.some(r=>r.cd>0);
  let s=`<div class="tw"><table class="${showCd?'has-credit':'no-credit'}"><thead><tr><th>名称</th><th class="num">输入</th><th class="num">输出</th>`
    +'<th class="num">缓存读</th><th class="num">缓存写</th><th class="num">推理</th><th class="total">合计</th><th class="hit">命中率</th>'
    +(showCd?'<th class="credit">积分</th><th class="metric">积分/M</th><th class="metric">元/积分</th>':'')+'<th class="rate">汇率</th><th class="rate">倍率</th><th class="metric">元/M</th><th class="metric">平均每调</th><th class="cost">成本 / $额度</th></tr></thead><tbody>';
  for(const r of rows){
    const denom=r.b.i+r.b.cr;
    const hit=denom>0?(r.b.cr/denom*100).toFixed(1)+'%':'—';
    const badge=modelRows&&OVR[r.n]?'<span class="ov">已改价</span>':'';
    const cfg=modelCfg(modelRows?r.n:state.model);
    const cdCells=showCd?(()=>{
      if(!(r.cd>0))return `<td class="credit">—</td><td class="metric">—</td><td class="metric">—</td>`;
      const perM=r.tot>0?r.cd/r.tot*1e6:0, perCd=r.cost/r.cd;
      return `<td class="credit">${r.cd.toLocaleString('zh-CN',{maximumFractionDigits:2})}</td>`
        +`<td class="metric">${r.tot>0?(perM>0&&perM<0.01?'<0.01':perM.toLocaleString('zh-CN',{maximumFractionDigits:2})):'—'}</td>`
        +`<td class="metric">${fmtC(perCd)}</td>`;
    })():'';
    s+=`<tr${modelRows?` data-pm="${esc(r.n)}"`:''}><td title="${esc(r.n)}">${badge}${esc(r.n)}</td>`
      +`<td class="num">${fmtT(r.b.i)}</td><td class="num">${fmtT(r.b.o)}</td><td class="num">${fmtT(r.b.cr)}</td>`
      +`<td class="num">${fmtT(r.b.cw)}</td><td class="num">${fmtT(r.b.r)}</td><td class="total"><b>${fmtT(r.tot)}</b></td>`
      +`<td class="hit">${hit}</td>`+cdCells
      +`<td class="rate">${fmtCfg(cfg.exchangeRate)}</td><td class="rate">×${fmtCfg(cfg.multiplier)}</td><td class="metric">${fmtPerM(r.cost,r.tot)}</td><td class="metric">${r.b.msg>0?fmtC(r.cost/r.b.msg):'—'}</td><td class="cost">${fmtBoth(r.cost,r.usd)}</td></tr>`;
  }
  return s+'</tbody></table></div>';
}

function card(title,hint,body){
  return `<div class="card"><h2>${title}</h2><div class="hint">${hint}</div>${body}</div>`;
}

function render(){
  const a=agg();
  const dates=effDates();
  refreshClientBtns();
  const modelName=state.model==='all'?'全部模型':state.model;
  const cliName=state.client==='all'?'全部客户端':state.client;
  const nModels=Object.keys(a.mc).length;
  const ta=totalAll();
  const fmtDT=v=>v?v.replace('T',' '):'…';
  const rl=range.start||range.end?` · ${fmtDT(range.start)} ~ ${fmtDT(range.end)}${rangeApprox?'（边界日按小时折算，近似值）':''}`:'';
  let h=`<div class="vt">${esc(cliName)} × ${esc(modelName)}<span class="vshare">占总成本 ${fmtP(ta?a.cost/ta*100:0)}${rl}</span></div>`;
  if(!DATA.entries.length)h+='<div class="note">未在本机发现可统计的 AI 客户端会话记录。面板会保留运行；产生新记录后点击「刷新数据」即可。</div>';
  const novr=Object.keys(OVR).length;
  if(novr)h+=`<div class="ovbar">${novr} 个模型使用浏览器自定义美元单价；汇率与倍率已按本机配置文件计算<button id="ovReset">恢复全部默认价格</button></div>`;
  h+=kpis(a,dates,nModels);
  h+='<div class="g2">'+card('用量趋势','按当前筛选聚合 · 点击切换 Token / 成本 / 积分指标',trendSwitch()+daily(dates,a))+card('活跃度热力图','颜色深浅＝当天成本',heat(dates,a))+'</div>';
  h+=card('Token 构成',`合计 ${fmtT(a.tokens)}`,stack(a.bk,a.tokens));
  if(state.model==='all'){
    const detailClientsNow=normalizeDetailClient();
    const da=modelDetailAgg();
    h+=card('模型分项明细','本表可按客户端独立筛选，不影响上方 KPI/趋势/热力图；右键模型行可改美元单价、汇率和倍率。元/M＝人民币终价÷Token×1e6；平均每调＝人民币终价÷调用次数；积分/M＝积分÷Token×1e6；元/积分＝人民币终价÷积分；命中率＝缓存读取÷(输入+缓存读取)',
            detailClientFilter(detailClientsNow)+detailTable(detailRows(da.mbk,da.mc,da.mu),true));
  }else{
    h+=card('客户端分项明细',`模型 ${esc(state.model)} 在各客户端的分项用量`,
            detailTable(detailRows(a.cbk,a.cc,a.cu),false));
  }
  const showModelBars=state.model==='all';
  const showClientBars=!(state.client!=='all'&&state.model==='all');
  if(showClientBars)h+=card('客户端分布','按最终成本排序；金额后附美元原价额度',bars(Object.entries(a.cc),a.cost,'cost','#1D9E75',a.cu));
  if(showModelBars)h+=card('模型成本','按最终成本排序；主金额后附美元原价额度，下一行显示该模型加权平均元/M（每百万 Token 人民币成本）',bars(Object.entries(a.mc),a.cost,'cost',CLI_COLOR[state.client]||'#378ADD',a.mu,a.mt));
  if(showModelBars)h+=card('模型 Token','按消耗量排序，与成本排序可能不同',bars(Object.entries(a.mt),a.tokens,'tokens','#BA7517'));
  h+=card('时间指标','全局统计，不受筛选影响（并发会话会叠加计时）',(()=>{
    const tm=DATA.tm;
    const f=ms=>!ms?'—':(ms/36e5>=24?(ms/36e5/24).toFixed(1)+' 天':(ms/36e5).toFixed(1)+' 小时');
    return `<div class="kpis" style="margin-bottom:0">${[['累计活跃时长',f(tm.totalActiveTimeMs)],['最长连续',f(tm.longestContinuousMs)],['最高并发会话',tm.maxConcurrentSessions],['会话总数',tm.sessionCount.toLocaleString()]].map(([k,v])=>`<div class="kpi"><div class="k">${k}</div><div class="v" style="font-size:18px">${v}</div></div>`).join('')}</div>`;})());
  $('content').innerHTML=h;
  const detailFilter=$('detailClientFilter');
  if(detailFilter)detailFilter.onchange=()=>{state.detailClient=detailFilter.value;render();saveState();};
  const rb=$('ovReset');
  if(rb)rb.onclick=()=>{for(const k in OVR)delete OVR[k];saveOvr();render();};
  document.querySelectorAll('[data-trend]').forEach(b=>b.onclick=()=>{trendMetric=b.dataset.trend;try{store.setItem('tokscaleTrend',trendMetric)}catch(e){};render();});
  renderBudgetBadge();
}

/* ---------- 客户端按钮 ---------- */
function refreshClientBtns(){
  let h=`<button class="fb${state.client==='all'?' active':''}" data-v="all">全部<span class="amt">${fmtBoth(totalAll(),totalUsdAll())}</span></button>`;
  for(const[c,v,u]of clientTotals())h+=`<button class="fb${state.client===c?' active':''}" data-v="${esc(c)}">${esc(c)}<span class="amt">${fmtBoth(v,u)}</span></button>`;
  $('clientBtns').innerHTML=h;
  $('clientBtns').querySelectorAll('.fb').forEach(b=>b.onclick=()=>{state.client=b.dataset.v;state.model='all';closeList();syncUI();render();saveState();});
}

/* ---------- 模型组合框 ---------- */
function modelPool(){
  if(state.client==='all')return modelTotals();
  const c={},u={};DATA.entries.forEach(e=>{if(e.c===state.client){c[e.m]=(c[e.m]||0)+effCost(e);u[e.m]=(u[e.m]||0)+usdCost(e);}});
  return Object.entries(c).map(([m,v])=>[m,v,u[m]||0]).sort((a,b)=>b[1]-a[1]);
}
let listOpen=false,kbIdx=-1;
const visibleItems=()=>[...$('modelList').querySelectorAll('.ci[data-v]')];
function refreshModelList(){
  const q=($('modelSearch').value||'').trim().toLowerCase();
  const pool=modelPool();
  const list=q?pool.filter(([m])=>m.toLowerCase().indexOf(q)>=0):pool;
  let h=`<div class="ch">${q?list.length+'/'+pool.length+' 匹配':pool.length+' 个模型'} · 右键看单价</div>`;
  h+=`<div class="ci${state.model==='all'?' sel':''}" data-v="all"><span>全部模型</span><span class="c">${fmtBoth(pool.reduce((s,[,v])=>s+v,0),pool.reduce((s,[,,u])=>s+u,0))}</span></div>`;
  if(state.model!=='all'&&!list.some(([m])=>m===state.model)){
    const cur=pool.find(([m])=>m===state.model);
    h+=`<div class="ci sel" data-v="${esc(state.model)}" data-pm="${esc(state.model)}"><span>${esc(state.model)} <span class="ov">已改价</span></span><span class="c">${cur?fmtBoth(cur[1],cur[2]):''}</span></div>`;
  }
  if(!list.length)h+='<div class="ci none"><span>无匹配模型</span></div>';
  for(const[m,v,u]of list){
    const badge=OVR[m]?' <span class="ov">已改价</span>':'';
    h+=`<div class="ci${m===state.model?' sel':''}" data-v="${esc(m)}" data-pm="${esc(m)}"><span title="${esc(m)}">${esc(m)}${badge}</span><span class="c">${fmtBoth(v,u)}</span></div>`;}
  $('modelList').innerHTML=h;
  $('modelList').querySelectorAll('.ci[data-v]').forEach(el=>el.addEventListener('mousedown',ev=>{if(ev.button!==0)return;ev.preventDefault();pickModel(el.dataset.v);}));
  kbIdx=-1;
}
function openList(){if(!listOpen){listOpen=true;$('modelList').hidden=false;}refreshModelList();}
function closeList(){listOpen=false;$('modelList').hidden=true;}
function pickModel(v){
  state.model=v;
  $('modelSearch').value=v==='all'?'':v;
  closeList();render();saveState();
}
function moveKb(d){
  const its=visibleItems();if(!its.length)return;
  kbIdx=Math.min(Math.max(kbIdx+d,0),its.length-1);
  its.forEach((el,i)=>el.classList.toggle('kb',i===kbIdx));
  if(its[kbIdx]&&its[kbIdx].scrollIntoView)its[kbIdx].scrollIntoView({block:'nearest'});
}
$('modelSearch').addEventListener('focus',openList);
$('modelSearch').addEventListener('input',()=>{if(!listOpen)openList();else refreshModelList();});
$('modelSearch').addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){e.preventDefault();if(listOpen)moveKb(1);else{openList();kbIdx=0;moveKb(0);}}
  else if(e.key==='ArrowUp'){e.preventDefault();moveKb(-1);}
  else if(e.key==='Enter'){e.preventDefault();const its=visibleItems();const it=its[kbIdx>=0?Math.min(kbIdx,its.length-1):0];if(it)pickModel(it.dataset.v);}
  else if(e.key==='Escape'){closeList();$('modelSearch').value=state.model==='all'?'':state.model;$('modelSearch').blur();}
});
$('comboToggle').addEventListener('click',()=>{listOpen?closeList():openList();});
$('modelSearch').addEventListener('blur',()=>setTimeout(closeList,120));
if(document.addEventListener)document.addEventListener('click',e=>{if(!e.target.closest||!e.target.closest('#modelCombo'))closeList();});

/* ---------- 价格右键菜单 ---------- */
let pcModel=null,pcXY=[100,100],pcMode='view';
const PM=v=>v==null?null:v*1e6;
function priceOf(m){return OVR[m]||PRICES[m]||null;}
function modelCfg(m){return costCfg(m);}
function fmtCfg(v){return Number(v).toLocaleString('zh-CN',{maximumFractionDigits:6});}
function fmtRow(k,label,p){
  return `<tr><td>${label}</td><td>${fmtM(PM(p[k]))}</td></tr>`;
}
function renderCard(m,mode){
  pcModel=m;pcMode=mode;
  const card=$('priceCard');
  const ovr=OVR[m],base=PRICES[m],cfg=modelCfg(m);
  let h=`<div class="t">${esc(m)}</div>`;
  if(mode==='view'){
    const p=priceOf(m)||{};
    h+=`<div class="s">${ovr?'已改价（浏览器自定义价格）':(base?'价格来源：'+base.src:'无价格数据')} · 美元/百万 token</div>`;
    const usage=DATA.entries.filter(e=>e.m===m).reduce((a,e)=>{const t=e.i+e.o+e.cr+e.cw+e.r;a.t+=t;a.c+=effCost(e);a.cd+=e.cd||0;a.calls+=e.msg||0;return a;},{t:0,c:0,cd:0,calls:0});
    h+='<table>'+fmtRow('i','输入',p)+fmtRow('o','输出',p)+fmtRow('cr','缓存读取',p)+fmtRow('cw','缓存写入',p)
      +`<tr><td>元/M</td><td>${fmtPerM(usage.c,usage.t)}</td></tr>`
      +`<tr><td>平均每调</td><td>${usage.calls>0?fmtC(usage.c/usage.calls):'—'}</td></tr>`
      +(usage.cd>0?`<tr><td>积分/M</td><td>${usage.t>0?(usage.cd/usage.t*1e6).toLocaleString('zh-CN',{maximumFractionDigits:2}):'—'}</td></tr>`
        +`<tr><td>元/积分</td><td>${fmtC(usage.c/usage.cd)}</td></tr>`:'')
      +`<tr><td>美元→人民币汇率</td><td>${fmtCfg(cfg.exchangeRate)}</td></tr>`
      +`<tr><td>成本倍率</td><td>× ${fmtCfg(cfg.multiplier)}</td></tr></table>`;
    h+=`<div class="s" style="margin-top:7px">已使用美元原价额度按折算前单价统计；最终成本：美元原始成本 × ${fmtCfg(cfg.exchangeRate)} × ${fmtCfg(cfg.multiplier)}，计价单位 ￥</div>`;
    h+=`<div class="a"><button class="pri" data-a="edit">编辑配置</button>${ovr?'<button data-a="reset">恢复默认价格</button>':''}</div>`;
  }else{
    const p=priceOf(m)||{};
    h+='<div class="s">价格单位为美元/百万 token；汇率和倍率保存到本机配置文件</div><table>';
    for(const[k,lb]of[['i','输入'],['o','输出'],['cr','缓存读取'],['cw','缓存写入']]){
      const v=PM(p[k]);
      h+=`<tr><td>${lb}</td><td><input data-k="${k}" type="number" min="0" step="any" value="${v==null?'':v}"></td></tr>`;}
    h+=`<tr><td>美元→人民币汇率</td><td><input id="pcRate" type="number" min="0.000001" max="1000" step="any" value="${cfg.exchangeRate}"></td></tr>`
      +`<tr><td>成本倍率</td><td><input id="pcMultiplier" type="number" min="0.000001" max="1000" step="any" value="${cfg.multiplier}"></td></tr>`;
    h+='</table><div class="a"><button class="pri" data-a="save">保存到配置文件</button><button data-a="cancel">取消</button></div>';
  }
  card.innerHTML=h;
  card.hidden=false;
  place();
}
function place(){
  const card=$('priceCard');
  const w=card.offsetWidth||300,h=card.offsetHeight||180;
  const W=(typeof innerWidth!=='undefined')?innerWidth:1200;
  const H=(typeof innerHeight!=='undefined')?innerHeight:800;
  const x=Math.min(pcXY[0]+14,Math.max(8,W-w-12));
  const y=Math.min(pcXY[1]+14,Math.max(8,H-h-12));
  card.style.left=x+'px';card.style.top=y+'px';
}
function showCard(m,ev){
  pcXY=ev&&typeof ev.clientX==='number'?[ev.clientX,ev.clientY]:pcXY;
  renderCard(m,'view');
}
function hideCard(){const card=$('priceCard');card.hidden=true;pcModel=null;pcMode='view';}
$('priceCard').addEventListener('contextmenu',e=>e.preventDefault());
$('priceCard').addEventListener('click',e=>{
  const b=e.target.closest?e.target.closest('button[data-a]'):null;if(!b)return;
  const a=b.dataset.a,m=pcModel;
  if(a==='edit')renderCard(m,'edit');
  else if(a==='cancel')renderCard(m,'view');
  else if(a==='save'){
    const p={i:0,o:0,cr:0,cw:0};
    let bad=false;
    $('priceCard').querySelectorAll('input[data-k]').forEach(inp=>{
      const raw=inp.value.trim();
      if(raw===''){p[inp.dataset.k]=0;return;}
      const v=parseFloat(raw);
      if(isNaN(v)||v<0)bad=true;else p[inp.dataset.k]=v/1e6;
    });
    const rate=parseFloat($('pcRate').value),multiplier=parseFloat($('pcMultiplier').value);
    if(!Number.isFinite(rate)||rate<=0||rate>1000||!Number.isFinite(multiplier)||multiplier<=0||multiplier>1000)bad=true;
    if(bad){b.textContent='数值无效';return;}
    b.disabled=true;b.textContent='保存中…';
    jpost('/api/model-cost-config',{model:m,exchangeRate:rate,multiplier})
      .then(async r=>{const j=r;if(!j||!j.ok)throw new Error(j.error||'保存失败');return j;})
      .then(j=>{OVR[m]=p;saveOvr();COST_CFG[m]={...(COST_CFG[m]||{}),...j.config};render();renderCard(m,'view');})
      .catch(err=>{b.disabled=false;b.textContent='保存失败';b.title=String(err.message||err).slice(0,300);});
  }
  else if(a==='reset'){delete OVR[m];saveOvr();render();renderCard(m,'view');}
});
if(document.addEventListener){
  document.addEventListener('contextmenu',e=>{
    const t=e.target.closest?e.target.closest('[data-pm]'):null;
    if(!t)return;
    e.preventDefault();
    showCard(t.dataset.pm,e);
  });
  document.addEventListener('mousedown',e=>{
    const inside=e.target.closest&&e.target.closest('#priceCard');
    const model=e.target.closest&&e.target.closest('[data-pm]');
    if(!inside&&!model)hideCard();
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape')hideCard();});
  if(typeof addEventListener==='function')addEventListener('resize',hideCard);
}

/* ---------- 时间筛选 ---------- */
function setPreset(t){
  state.preset=t;
  const mx=allDates[allDates.length-1];
  if(t==='today')range={start:TODAY+'T00:00:00',end:TODAY+'T23:59:59'};
  else if(t==='all')range={start:null,end:null};
  else if(t==='7d')range={start:shiftISO(mx,-6)+'T00:00:00',end:mx+'T23:59:59'};
  else if(t==='30d')range={start:shiftISO(mx,-29)+'T00:00:00',end:mx+'T23:59:59'};
  else range={start:mx.slice(0,8)+'01'+'T00:00:00',end:mx+'T23:59:59'};
  $('fromDate').value=range.start||'';
  $('toDate').value=range.end||'';
  syncUI();render();saveState();
}
document.querySelectorAll('.tb').forEach(b=>b.onclick=()=>setPreset(b.dataset.t));
function onCustomDate(){
  const f=normDT($('fromDate').value),e2=normDT($('toDate').value);
  range={start:f||null,end:e2||null};
  if(f&&e2&&f>e2){range.start=e2;range.end=f;$('fromDate').value=e2;$('toDate').value=f;}
  state.preset='custom';syncUI();render();saveState();
}
$('fromDate').onchange=onCustomDate;
$('toDate').onchange=onCustomDate;

function syncUI(){
  if(state.model==='all')$('modelSearch').value='';
  document.querySelectorAll('.tb').forEach(b=>b.classList.toggle('active',b.dataset.t===state.preset));
}

/* ---------- 月度预算告警（全局口径，不受筛选影响，保存在本浏览器） ---------- */
const BUD_KEY='tokscaleBudget';
let budget=(()=>{try{const v=JSON.parse(store.getItem(BUD_KEY)||'null');return v&&typeof v==='object'?v:{cd:null,cny:null};}catch(e){return{cd:null,cny:null};}})();
const saveBudget=()=>{try{store.setItem(BUD_KEY,JSON.stringify(budget))}catch(e){}};
function monthTotals(){
  const ym=TODAY.slice(0,7);
  let cd=0,cny=0,usd=0;
  for(const e of DATA.entries){if((e.d||'').slice(0,7)!==ym)continue;cd+=e.cd||0;cny+=effCost(e);usd+=usdCost(e);}
  return{cd,cny,usd};
}
function renderBudgetBadge(){
  const el=$('budgetBadge');
  const mt=monthTotals();
  let ratio=null,part='';
  if(budget.cd>0&&mt.cd>0){ratio=Math.max(ratio||0,mt.cd/budget.cd*100);part=`积分 ${(mt.cd/budget.cd*100).toFixed(0)}%`;}
  if(budget.cny>0&&mt.cny>0){const r=mt.cny/budget.cny*100;if(r>(ratio||0))part=`成本 ${(r).toFixed(0)}%`;ratio=Math.max(ratio||0,r);}
  if(ratio==null){el.textContent='';el.title='';return;}
  el.textContent='本月 '+part;
  el.className=ratio>=100?'over':ratio>=80?'warn':'';
  el.title=`本月已用：积分 ${mt.cd.toLocaleString('zh-CN',{maximumFractionDigits:2})} · 成本 ${fmtC(mt.cny)}${budget.cd>0?` / 上限 ${budget.cd.toLocaleString()} 积分`:''}${budget.cny>0?` / 上限 ${fmtC(budget.cny)}`:''}`;
}
let budgetCardOpen=false;
function renderBudgetCard(){
  const c=$('budgetCard');
  const mt=monthTotals();
  let h=`<h4>月度预算</h4><div class="s">本月已用：积分 ${mt.cd.toLocaleString('zh-CN',{maximumFractionDigits:2})} · 成本 ${fmtBoth(mt.cny,mt.usd)}</div>`;
  h+='<label>积分月上限（留空不限）</label><input id="budCd" type="number" min="0" step="any" value="'+(budget.cd==null?'':budget.cd)+'">';
  h+='<label>人民币成本月上限 ￥（留空不限）</label><input id="budCny" type="number" min="0" step="any" value="'+(budget.cny==null?'':budget.cny)+'">';
  h+='<div class="a"><button class="pri" data-a="save">保存</button><button data-a="clear">清除</button><button data-a="cancel">取消</button></div>';
  c.innerHTML=h;c.hidden=false;budgetCardOpen=true;
  const x=Math.min((typeof innerWidth!=='undefined'?innerWidth:1200)-320,Math.max(8,(typeof innerWidth!=='undefined'?innerWidth:1200)-320));
  c.style.left=x+'px';c.style.top='52px';
  c.onclick=e=>{
    const b=e.target.closest?e.target.closest('button[data-a]'):null;if(!b)return;
    const a=b.dataset.a;
    if(a==='cancel'){c.hidden=true;budgetCardOpen=false;return;}
    if(a==='clear'){budget={cd:null,cny:null};saveBudget();c.hidden=true;budgetCardOpen=false;renderBudgetBadge();return;}
    const v1=parseFloat($('budCd').value),v2=parseFloat($('budCny').value);
    budget={cd:(Number.isFinite(v1)&&v1>0)?v1:null,cny:(Number.isFinite(v2)&&v2>0)?v2:null};
    saveBudget();c.hidden=true;budgetCardOpen=false;renderBudgetBadge();
  };
}
$('budgetBtn').addEventListener('click',()=>{budgetCardOpen?($('budgetCard').hidden=true,budgetCardOpen=false):renderBudgetCard();});
document.addEventListener('mousedown',e=>{
  if(!budgetCardOpen)return;
  if(!(e.target.closest&&e.target.closest('#budgetCard'))&&!(e.target.closest&&e.target.closest('#budgetBtn'))){$('budgetCard').hidden=true;budgetCardOpen=false;}
});

/* ---------- CSV 导出（当前筛选范围的明细行） ---------- */
$('exportBtn').addEventListener('click',()=>{
  const head=['日期','客户端','模型','输入','输出','缓存读取','缓存写入','推理','合计Token','消息数','积分','美元成本','人民币最终成本'];
  const lines=[head.join(',')];
  let n=0;
  for(const e of filteredEntries()){
    if(state.client!=='all'&&e.c!==state.client)continue;
    if(state.model!=='all'&&e.m!==state.model)continue;
    const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
    const t=e.i+e.o+e.cr+e.cw+e.r;
    lines.push([e.d,e.c,e.m,e.i,e.o,e.cr,e.cw,e.r,t,e.msg,(e.cd||0).toFixed(4),
      (usdCost(e)).toFixed(6),(effCost(e)).toFixed(6)].map(q).join(','));
    n++;
  }
  if(!n){autoCount.textContent='当前筛选无数据可导出';return;}
  const blob=new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
  const a2=document.createElement('a');
  a2.href=URL.createObjectURL(blob);
  a2.download='tokscale明细_'+TODAY.replace(/-/g,'')+'.csv';
  document.body.appendChild(a2);a2.click();
  setTimeout(()=>{URL.revokeObjectURL(a2.href);a2.remove();},3000);
  autoCount.textContent='已导出 '+n+' 行';
});

/* ---------- 状态记忆（自动刷新后恢复筛选） ---------- */
const ST_KEY='tokscaleState';
const saveState=()=>{try{if(store)store.setItem(ST_KEY,JSON.stringify({client:state.client,model:state.model,preset:state.preset,detailClient:state.detailClient,from:range.start||'',to:range.end||''}))}catch(e){}};
const loadState=()=>{try{if(!store)return null;const v=JSON.parse(store.getItem(ST_KEY)||'null');return v&&typeof v==='object'?v:null;}catch(e){return null;}};

/* ---------- 后台静默刷新与自动定时 ---------- */
const AUTO_KEY='tokscaleAuto';
const AUTO_ON_KEY='tokscaleAutoOn';
const legacyAuto=(()=>{try{return store?store.getItem(AUTO_KEY):null}catch(e){return null}})();
let autoMin=(()=>{const v=parseInt(legacyAuto||'1',10);return isNaN(v)||v<=0?1:v;})();
let autoOn=(()=>{try{
  if(!store)return true;
  const v=store.getItem(AUTO_ON_KEY);
  if(v!==null)return v!=='0';
  return legacyAuto!=='0';
}catch(e){return true}})();
let busy=false,nextIn=0;
const refreshBtn=$('refreshBtn'),refreshLb=$('refreshLb'),autoOnEl=$('autoOn'),autoSel=$('autoSel'),autoCount=$('autoCount');
autoOnEl.checked=autoOn;
autoSel.value=String(autoMin);
function setBusy(b,lb){
  busy=b;
  refreshBtn.classList.toggle('busy',b);
  refreshLb.textContent=lb;
}
const fmtL=s=>{const m=Math.floor(s/60),x=s%60;return m+':'+String(x).padStart(2,'0');};
function syncAutoUI(reset){
  autoOnEl.checked=autoOn;
  autoSel.disabled=!autoOn;
  autoCount.classList.remove('err');
  autoCount.title='';
  if(!autoOn){nextIn=0;autoCount.textContent='自动更新已关闭';return;}
  if(reset||nextIn<=0)nextIn=autoMin*60;
  autoCount.textContent='下次更新 '+fmtL(nextIn);
}
function onRefreshFail(msg){
  setBusy(false,'刷新数据');
  autoCount.textContent='上次更新失败';
  autoCount.classList.add('err');
  autoCount.title=(msg||'未知错误').slice(0,400);
}
/* ---------- 扫描进度蒙版 ---------- */
const maskEl=$('scanMask'),maskFill=$('maskFill'),maskStage=$('maskStage'),maskDetail=$('maskDetail'),maskLines=$('maskLines');
let maskShown=false;
function showMask(p){
  p=p||{};
  maskEl.hidden=false;maskShown=true;
  maskStage.textContent=p.stage||'正在更新数据';
  maskDetail.textContent=p.detail||'';
  maskFill.style.width=Math.max(4,p.pct||4)+'%';
  if(p.lines&&p.lines.length)maskLines.innerHTML=p.lines.slice(-5).map(l=>'<div>'+esc(l)+'</div>').join('');
}
function hideMask(){maskEl.hidden=true;maskShown=false;maskFill.style.width='4%';maskLines.innerHTML='';}
function applyRefreshResult(){
  /* 自适应间隔：单次刷新耗时超过当前间隔时，自动放宽到足够档位 */
  return jget('/api/refresh/status').then(j=>{
    const sec=j&&j.lastRefreshSec;
    if(sec&&sec>autoMin*60){
      const need=[1,5,15,30,60].find(o=>o*60>=sec*1.15)||60;
      if(need>autoMin){autoMin=need;try{store.setItem(AUTO_KEY,String(autoMin))}catch(e){}autoSel.value=String(autoMin);}
    }
    const hash=j&&j.dataHash;
    if(!hash||hash===lastAppliedHash){setBusy(false,'刷新数据');syncAutoUI(false);return;}
    return jget('/api/data').then(d=>{
      if(d&&d.fp===dataFp){lastAppliedHash=hash;setBusy(false,'刷新数据');syncAutoUI(false);return;}
      lastAppliedHash=hash;
      applyData(d);  // 失败时由外层 catch 整页重载兜底
      setBusy(false,'刷新数据');syncAutoUI(false);
    });
  });
}
let lastAppliedHash=null;  // 上次已应用的 dashboard-data 指纹（/api/refresh/status 提供）
function pollUntilDone(silent){
  jget('/api/refresh/status').then(j=>{
    if(j.busy){
      if(silent){autoCount.textContent='后台更新中 '+Math.round((j.progress&&j.progress.pct)||0)+'%';}
      else showMask(j.progress);
      setTimeout(()=>pollUntilDone(silent),1000);return;
    }
    hideMask();
    if(j.ok===true){applyRefreshResult().catch(()=>location.reload());return;}
    if(j.ok===false)onRefreshFail(j.error||'更新失败');
    else{setBusy(false,'刷新数据');syncAutoUI(false);}
  }).catch(()=>setTimeout(()=>pollUntilDone(silent),2500));
}
let dataFp=DATA.fp||'';
function refresh(){
  if(busy)return;
  setBusy(true,'更新中…');
  autoCount.textContent='后台更新中…';
  jpost('/api/refresh',{}).then(j=>{
    if(j.busy&&j.ok===false){onRefreshFail(j.error||'未知错误');return;}
    pollUntilDone(true);
  }).catch(()=>pollUntilDone(true));
}
refreshBtn.addEventListener('click',refresh);
autoOnEl.addEventListener('change',()=>{
  autoOn=autoOnEl.checked;
  try{if(store)store.setItem(AUTO_ON_KEY,autoOn?'1':'0')}catch(e){}
  syncAutoUI(true);
});
autoSel.addEventListener('change',()=>{
  autoMin=parseInt(autoSel.value,10)||1;
  try{if(store)store.setItem(AUTO_KEY,String(autoMin))}catch(e){}
  syncAutoUI(true);
});
syncAutoUI(true);
setInterval(()=>{
  if(autoOn&&!busy){
    nextIn--;
    if(nextIn<=0){nextIn=autoMin*60;refresh();}
    else autoCount.textContent='下次更新 '+fmtL(nextIn);
  }
},1000);

/* ---------- 启动：恢复筛选 + 跟随服务端进行中的更新 ---------- */
const saved=loadState();
function applyRestore(){
  if(!saved)return false;
  state.client=typeof saved.client==='string'?saved.client:'all';
  state.model=typeof saved.model==='string'?saved.model:'all';
  state.detailClient=typeof saved.detailClient==='string'?saved.detailClient:'all';
  $('modelSearch').value=state.model==='all'?'':state.model;
  if(saved.preset==='custom'&&saved.from){
    const f=normDT(saved.from),e2=normDT(saved.to||'');
    $('fromDate').value=f||'';$('toDate').value=e2||'';
    range={start:f||null,end:e2||null};
    state.preset='custom';syncUI();render();saveState();return true;
  }
  if(saved.preset&&['today','7d','30d','month','all'].indexOf(saved.preset)>=0){
    setPreset(saved.preset);saveState();return true;
  }
  return false;
}
if(!applyRestore())setPreset('today');
jget('/api/model-cost-config').then(j=>{
  if(j&&j.models){Object.assign(COST_CFG,j.models);render();}
}).catch(()=>{});
jget('/api/refresh/status').then(j=>{
  if(j&&j.busy){
    setBusy(true,'更新中…');
    const first=j.mode==='first';  // 仅首次扫描显示蒙版，其余静默
    if(first)showMask(j.progress);else autoCount.textContent='后台更新中…';
    pollUntilDone(!first);
  }
}).catch(()=>{});
dbgFlush();
"""

def load_pricing_meta():
    """读取 pricing.json 的 _meta(价格源与同步时间)。"""
    try:
        with open(PRICING, encoding="utf-8") as f:
            raw = json.load(f)
        return raw.get("_meta") or {} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def price_src_text():
    meta = load_pricing_meta()
    t = str(meta.get("basellmSyncedAt") or "")[:19]
    if meta.get("pricingSource") == "basellm/llm-metadata":
        return "basellm/llm-metadata · 同步于 " + (t or "?")
    return "tokscale(LiteLLM/OpenRouter/Models.dev)"


def fetch_hourly():
    """调用 tokscale hourly --json 获取小时级聚合，用于时间筛选精确到时分秒。

    小时条目含输入/输出/缓存读写 token 与消息数（无推理与按模型拆分），
    因此仅用于边界日的总量切片，天×模型分项由前端按当日占比折算。
    任一环节失败返回空列表，时间筛选退化为按天粒度，不影响其他功能。
    """
    exe = os.path.join(HERE, "tokscale.exe")
    if not os.path.isfile(exe):
        print("  跳过小时级数据：未找到 tokscale.exe")
        return []
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [exe, "hourly", "--json", "--no-spinner", "--hide-zero"],
            capture_output=True, timeout=600, creationflags=flags,
        )
        if proc.returncode != 0:
            print("  警告: tokscale hourly 退出码 %s，时间筛选退化为按天" % proc.returncode)
            return []
        text = proc.stdout.decode("utf-8", "replace")
        start = text.find("{")  # 跳过可能的前导警告行
        if start < 0:
            print("  警告: tokscale hourly 无 JSON 输出，时间筛选退化为按天")
            return []
        rows = (json.loads(text[start:]) or {}).get("entries") or []
        hours = []
        for row in rows:
            hh = str(row.get("hour") or "").strip()
            if not hh:
                continue
            hh = hh.replace(" ", "T")
            if len(hh) == 13:  # "2026-03-17T02"
                hh += ":00:00"
            elif len(hh) == 16:  # "2026-03-17T02:00"
                hh += ":00"
            hours.append({
                "h": hh,
                "i": row.get("input", 0) or 0,
                "o": row.get("output", 0) or 0,
                "cr": row.get("cacheRead", 0) or 0,
                "cw": row.get("cacheWrite", 0) or 0,
                "msg": row.get("messageCount", 0) or 0,
            })
        print(f"  小时级聚合 {len(hours)} 条（时间筛选支持到时分秒）")
        return hours
    except Exception as exc:
        print("  警告: 获取小时级数据失败(%r)，时间筛选退化为按天" % exc)
        return []


def build():
    with open(GRAPH, encoding="utf-8") as f:
        d = json.load(f)

    meta = d.get("meta") or {}
    entries = []
    models = set()
    for day in d["contributions"]:
        for e in day["clients"]:
            t = e.get("tokens") or {}
            models.add(e["modelId"])
            entries.append({
                "d": day["date"], "c": e["client"], "m": e["modelId"],
                "i": t.get("input", 0), "o": t.get("output", 0),
                "cr": t.get("cacheRead", 0), "cw": t.get("cacheWrite", 0),
                "r": t.get("reasoning", 0),
                "cost": round(e.get("cost", 0) or 0, 6),
                "msg": e.get("messages", 0) or 0,
            })
    entries.sort(key=lambda x: (x["d"], x["c"], x["m"]))

    # WorkBuddy 积分：优先挂到已有条目，未匹配的 (日期,模型) 补一条纯积分记录
    credits = parse_credits()
    wb_pairs = {(e["d"], e["m"]) for e in entries if e["c"] == "workbuddy"}
    credit_total = 0.0
    for e in entries:
        if e["c"] == "workbuddy":
            v = credits.get((e["d"], e["m"]), 0)
            e["cd"] = round(v, 4)
            credit_total += v
    for (dd, mm), v in credits.items():
        if (dd, mm) in wb_pairs:
            continue
        entries.append({"d": dd, "c": "workbuddy", "m": mm,
                        "i": 0, "o": 0, "cr": 0, "cw": 0, "r": 0,
                        "cost": 0, "msg": 0, "cd": round(v, 4)})
        credit_total += v
    entries.sort(key=lambda x: (x["d"], x["c"], x["m"]))
    if credit_total:
        print(f"  WorkBuddy 积分合计 {credit_total:.2f}（来自本地会话日志）")

    prices = ensure_pricing(sorted(models)) or {}
    all_models = sorted({e["m"] for e in entries})
    cost_configs = ensure_cost_config(all_models)
    clean = {m: p for m, p in prices.items() if p}

    gen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hours = fetch_hourly()
    payload = {"tm": d.get("timeMetrics") or {}, "entries": entries,
               "prices": clean, "costConfigs": cost_configs,
               "hours": hours, "gen": gen}
    # 数据指纹：内容未变化时前端跳过重渲染（gen 每次构建都变，不参与指纹）
    fp_src = json.dumps({"e": entries, "p": clean, "c": cost_configs, "h": hours},
                        ensure_ascii=False, sort_keys=True)
    payload["fp"] = hashlib.md5(fp_src.encode("utf-8")).hexdigest()
    data_js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert "__DATA__" not in data_js

    # 数据文件落盘：供 /api/data 免刷新更新使用（前端拉取后局部重渲染）
    data_path = os.path.join(HERE, "dashboard-data.json")
    try:
        tmp = data_path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, data_path)
    except OSError as e:
        print("  警告: dashboard-data.json 写入失败(%s)，免刷新更新退化为整页刷新" % e)

    dates = sorted({e["d"] for e in entries if e.get("d")})
    range_text = f"{dates[0]} 至 {dates[-1]}" if dates else "本机暂无记录"
    html = (HTML_TMPL
            .replace("__CSS__", CSS)
            .replace("__JS__", JS.replace("__DATA__", data_js))
            .replace("__RANGE__", range_text)
            .replace("__VER__", str(meta.get("version", "?")))
            .replace("__GEN__", str(meta.get("generatedAt", ""))[:19].replace("T", " "))
            .replace("__PRICESRC__", price_src_text()))

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    clients_n = {e["c"] for e in entries}
    print(f"已生成 {OUT}")
    print(f"  {len(html):,} 字节 · {len(dates)} 天 · {len(clients_n)} 客户端 · {len(clean)}/{len(models)} 模型有定价 · {len(entries)} 条记录")


if __name__ == "__main__":
    build()
