"""股票策略回测平台 - Flask 入口。

运行：
  python app.py
然后浏览器打开 http://127.0.0.1:5000
"""
import os
import time
import json
import smtplib
import threading
import urllib.request
import traceback
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime, timedelta
from collections import defaultdict, deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify

from backtest import (load_kline, load_pe, load_valuation_pe, run_backtest,
                      run_optimization, StrategyConfig, INDEX_PE_PROXY,
                      ENTRY_DEFAULTS, EXIT_DEFAULTS, CN_LABEL)
from backtest.scanner import (scan_symbol, default_symbols, get_a500_constituents,
                              get_sp500_constituents, get_hk_large_caps,
                              BROAD_INDICES, INDUSTRY_INDICES, HK_INDICES, US_INDICES)
from backtest.data import CACHE_DIR, load_kline, load_valuation_pe
from pattern_matching import analyze_symbol, evaluate_strategies, detect_signals
from pattern_matching.universe import (universe_options, preview_universe,
                                        create_universe, get_universe, list_universes)

# ===== 预下载状态 =====
_preload_status = {"running": False, "done": False, "total": 0, "ok": 0, "fail": 0,
                   "current": "", "stage": "", "log": [], "log_count": 0, "start_time": 0,
                   "total_bytes": 0, "speed": "0 KB/s", "total_data": "0 B", "start_bytes": 0}
_byte_history = []

app = Flask(__name__)


def _minus_years(date_str, years):
    """date_str 往前推 years 年(用于加载更早的PE历史)；闰日(2/29)退到 2/28。"""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        try:
            return d.replace(year=d.year - years).strftime("%Y-%m-%d")
        except ValueError:
            return d.replace(year=d.year - years, day=28).strftime("%Y-%m-%d")
    except Exception:
        return date_str


# ===== 服务端 LRU 缓存：最多100组回测/寻优结果，避免重复计算 =====
# 数据源/估值逻辑变更时提升版本，避免命中旧 PE 可用性结果。
DATA_SOURCE_VERSION = "2026-06-28-or-entry-fix-v1"
# 寻优打分逻辑变更时提升版本，避免命中旧评分结果。
OPT_SCORE_VERSION = "2026-06-29-no-filter-v1"
_SERVER_CACHE = OrderedDict()
_SERVER_CACHE_MAX = 100
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        if key in _SERVER_CACHE:
            _SERVER_CACHE.move_to_end(key)
            return _SERVER_CACHE[key]
    return None


def _cache_put(key, val):
    with _cache_lock:
        _SERVER_CACHE[key] = val
        _SERVER_CACHE.move_to_end(key)
        while len(_SERVER_CACHE) > _SERVER_CACHE_MAX:
            _SERVER_CACHE.popitem(last=False)


def _bt_cache_key(symbol, start, end, adjust, initial_capital, commission, margin_rate, config_dict):
    """回测缓存键：规范化所有参数，保证相同参数命中同一缓存。"""
    return "bt:" + json.dumps({
        "v": DATA_SOURCE_VERSION,
        "s": symbol, "st": start, "e": end, "a": adjust,
        "ic": round(initial_capital, 2), "cm": round(commission, 6),
        "mr": round(margin_rate, 6), "cfg": config_dict,
    }, sort_keys=True, ensure_ascii=False)


def _opt_cache_key(symbol, start, end, adjust, commission, top_n, min_trades, return_basis="excess"):
    return "opt:" + json.dumps({
        "v": OPT_SCORE_VERSION,
        "s": symbol, "st": start, "e": end, "a": adjust,
        "cm": round(commission, 6), "tn": top_n, "mt": min_trades, "rb": return_basis,
    }, sort_keys=True, ensure_ascii=False)


# ===== 限流：每秒1次 / 每分钟10次 / 每小时100次 / 每天1000次 =====
_RATE_LIMITS = {  # window_seconds: max_count
    1: 1,
    60: 10,
    3600: 100,
    86400: 1000,
}
_rate_buckets = defaultdict(lambda: {w: deque() for w in _RATE_LIMITS})


def _client_ip():
    """提取真实客户端IP。优先级：
    X-Real-IP(Nginx的remote_addr，最可靠) > CF-Connecting-IP > X-Forwarded-For > remote_addr。
    注意：X-Forwarded-For 可被客户端伪造（如 Cloudflare WARP 会塞1.1.1.1），不优先使用。"""
    xri = request.headers.get("X-Real-IP", "")
    if xri:
        ip = xri.strip()
        if ip and not ip.startswith(("127.", "10.", "172.", "192.168.")):
            return ip
    cf = request.headers.get("CF-Connecting-IP", "")
    if cf:
        return cf.strip()
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        for part in fwd.split(","):
            ip = part.strip()
            if ip and not ip.startswith(("127.", "10.", "172.", "192.168.")):
                return ip
        return fwd.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


# IP 归属地查询缓存（避免重复请求）
_ip_location_cache = {}


def _ip_location(ip):
    """查询IP归属地，返回 '地区 运营商' 字符串。失败返回空串。"""
    if not ip or ip.startswith(("127.", "10.", "172.", "192.168.", "0.")):
        return "内网"
    if ip in _ip_location_cache:
        return _ip_location_cache[ip]
    try:
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,isp"
        req = urllib.request.Request(url, headers={"User-Agent": "trade-feedback/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        if d.get("status") == "success":
            loc = " ".join(x for x in [d.get("country"), d.get("regionName"), d.get("city")] if x)
            isp = d.get("isp", "")
            result = f"{loc} · {isp}" if isp else loc
        else:
            result = "未知地区"
    except Exception:
        result = "未知地区"
    _ip_location_cache[ip] = result
    return result


def _check_rate_limit():
    """返回 (ok, msg)。同一IP多窗口限流。"""
    ip = _client_ip()
    now = time.time()
    buckets = _rate_buckets[ip]
    for window, maxcnt in _RATE_LIMITS.items():
        dq = buckets[window]
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= maxcnt:
            return False, f"请求过于频繁，已被限流：每秒最多1次/每分钟10次/每小时100次/每天1000次。请稍后再试。"
    # 通过，记录
    for window in _RATE_LIMITS:
        buckets[window].append(now)
    return True, None


# 是否完全禁用限流：DISABLE_RATE_LIMIT=1 强制关闭（备用开关）
_DISABLE_RATE_LIMIT = os.environ.get("DISABLE_RATE_LIMIT", "") == "1"

# 限流豁免：轻量「读取/轮询」类接口不计数，避免前端进度轮询(每1.2s一次)和
# 页面加载的纯读取请求撑爆限流额度。限流只针对重计算/触发类接口
# (回测/寻优/扫描启动/推送/订阅写入)。
_RATE_EXEMPT_PATHS = {
    "/api/notify/scan_status",   # 扫描进度轮询(高频)
    "/api/notify/scan_cancel",   # 取消当前扫描任务
    "/api/notify/latest",        # 读取最近一次扫描结果
    "/api/notify/symbols",       # 读取可选标的列表
    "/api/notify/subscriptions", # 读取订阅列表
}


def _is_rate_exempt():
    """是否豁免限流：豁免路径 + 所有 GET 读取请求(只读不触发重计算)。"""
    if request.path in _RATE_EXEMPT_PATHS:
        return True
    # 订阅的 GET 查询(读取单组)也豁免；POST/DELETE(写入)仍限流
    if request.path == "/api/notify/subscription" and request.method == "GET":
        return True
    return False


def _is_local_direct_request():
    """本地直连调试请求：回环地址且无任何反向代理转发头。
    生产经 Nginx 反代时会带 X-Real-IP / X-Forwarded-For 等头，
    不会被误判为本地，限流仍照常生效。"""
    if (request.headers.get("X-Real-IP")
            or request.headers.get("X-Forwarded-For")
            or request.headers.get("CF-Connecting-IP")):
        return False
    ra = request.remote_addr or ""
    return ra.startswith("127.") or ra in ("::1", "localhost")


@app.before_request
def _rate_limit_guard():
    if request.path.startswith("/api/"):
        # 本地调试直连放行，不触发限流（不影响生产环境）
        if _DISABLE_RATE_LIMIT or _is_local_direct_request():
            return
        # 轻量读取/轮询接口豁免，不占用限流额度
        if _is_rate_exempt():
            return
        ok, msg = _check_rate_limit()
        if not ok:
            return jsonify({"ok": False, "error": msg, "rate_limited": True}), 429


@app.after_request
def ensure_utf8_charset(resp):
    """给所有文本类响应补 charset=utf-8，避免繁体(Big5)等地区浏览器按本地编码解码导致中文乱码。"""
    ct = resp.headers.get("Content-Type", "")
    if ct and "charset" not in ct.lower() and (
        ct.startswith("text/") or "javascript" in ct or "json" in ct
        or "css" in ct or "xml" in ct
    ):
        resp.headers["Content-Type"] = f"{ct}; charset=utf-8"
    return resp


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/help")
def help_page():
    return render_template("help.html")


@app.route("/trade-notify")
def trade_notify_page():
    return render_template("trade_notify.html")


@app.route("/pattern-research")
def pattern_research_page():
    return render_template("pattern_research.html")


@app.route("/pattern-help")
def pattern_help_page():
    return render_template("pattern_help.html")


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    try:
        body = request.get_json(force=True) or {}
        symbol = str(body.get("symbol", "000001")).strip()
        start = str(body.get("start", "2015-03-01")).strip()
        end = str(body.get("end", "2018-03-01")).strip()
        adjust = str(body.get("adjust", "qfq")).strip()
        if adjust not in ("qfq", "hfq", ""):
            adjust = "qfq"
        initial_capital = float(body.get("initial_capital", 100000))
        commission = float(body.get("commission", 0.0005))
        margin_rate = float(body.get("margin_rate", 0.0699))

        config = StrategyConfig.from_dict(body.get("strategy", {}))

        # 服务端缓存命中 → 直接返回（不计限流，因为是缓存）
        ck = _bt_cache_key(symbol, start, end, adjust, initial_capital, commission, margin_rate, config.to_dict())
        cached = _cache_get(ck)
        if cached is not None:
            return jsonify(cached)

        df = load_kline(symbol, start, end, adjust=adjust)
        if df is None or df.empty:
            return jsonify({"ok": False, "error": f"未获取到 {symbol} 在 {start}~{end} 的数据，请检查代码或区间。"})

        # PE 仓位管理所需的市盈率序列(仅支持的指数有)
        # 近5年滚动百分位需 start 前 5 年历史，故 PE 从 start-5y 开始加载
        pe_series = None
        pe_available = False
        pe_proxy = INDEX_PE_PROXY.get(str(symbol).strip().lower())
        pos = config.position
        need_pe = pos.get("entry") == "pe_percentile" or pos.get("reduce") == "pe_percentile"
        if need_pe:
            # 统一估值入口：A股指数/A股个股/港股个股均可取近5年分位；港股指数/美股退化满仓
            pe_df = load_valuation_pe(symbol, _minus_years(start, 5), end)
            if pe_df is not None and not pe_df.empty:
                pe_series = dict(zip(pe_df["date"], pe_df["pe"]))
                pe_available = True

        result = run_backtest(df, config, initial_capital=initial_capital,
                              commission=commission, pe_series=pe_series,
                              margin_rate=margin_rate)
        result["meta"] = {
            "data_source_version": DATA_SOURCE_VERSION,
            "symbol": symbol,
            "start": start,
            "end": end,
            "adjust": adjust,
            "rows": len(df),
            "strategy": config.to_dict(),
            "pe_available": pe_available,
            "pe_proxy": pe_proxy if pe_available else None,
        }
        if result.get("ok"):
            _cache_put(ck, result)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    try:
        body = request.get_json(force=True) or {}
        symbol = str(body.get("symbol", "399001")).strip()
        start = str(body.get("start", "2015-01-01")).strip()
        end = str(body.get("end", "2025-10-31")).strip()
        adjust = str(body.get("adjust", "qfq")).strip()
        if adjust not in ("qfq", "hfq", ""):
            adjust = "qfq"
        initial_capital = float(body.get("initial_capital", 100000))
        commission = float(body.get("commission", 0.0005))
        top_n = int(body.get("top_n", 10))
        min_trades = int(body.get("min_trades", 10))
        return_basis = str(body.get("return_basis", "excess")).strip().lower()
        if return_basis not in ("excess", "annual"):
            return_basis = "excess"

        # 服务端缓存命中 → 直接返回
        ck = _opt_cache_key(symbol, start, end, adjust, commission, top_n, min_trades, return_basis)
        cached = _cache_get(ck)
        if cached is not None:
            return jsonify(cached)

        df = load_kline(symbol, start, end, adjust=adjust)
        if df is None or df.empty:
            return jsonify({"ok": False, "error": f"未获取到 {symbol} 在 {start}~{end} 的数据。"})

        result = run_optimization(df, initial_capital=initial_capital, commission=commission,
                                  top_n=top_n, min_trades=min_trades, return_basis=return_basis)
        result["meta"] = {"symbol": symbol, "start": start, "end": end, "rows": len(df),
                          "opt_score_version": OPT_SCORE_VERSION}
        if result.get("ok"):
            _cache_put(ck, result)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


# ===== 相似K线策略研究 API（对应总体设计文档第4-7层）=====
@app.route("/api/pattern/signals", methods=["POST"])
def api_pattern_signals():
    """信号侦测：在指定日期检测标的的买入/卖出信号事件。"""
    try:
        body = request.get_json(force=True) or {}
        symbol = str(body.get("symbol", "sh000300")).strip()
        as_of = str(body.get("as_of_date", "")).strip() or None
        adjust = str(body.get("adjust", "qfq")).strip()
        lookback = int(body.get("lookback_years", 5))
        result = detect_signals(symbol, as_of_date=as_of, adjust=adjust, lookback_years=lookback)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/pattern/analyze", methods=["POST"])
def api_pattern_analyze():
    """端到端分析：信号侦测 → 相似K线检索 → 策略评估排序。"""
    try:
        body = request.get_json(force=True) or {}
        symbol = str(body.get("symbol", "sh000300")).strip()
        as_of = str(body.get("as_of_date", "")).strip() or None
        adjust = str(body.get("adjust", "qfq")).strip()
        lookback = int(body.get("lookback_years", 6))
        top_k = int(body.get("top_k", 10))
        ref_symbols = body.get("reference_symbols", None)
        print(f"[pattern] symbol={symbol} as_of={as_of or 'latest'} top_k={top_k} ref={ref_symbols}")

        t0 = time.time()
        analysis = analyze_symbol(symbol, as_of_date=as_of, adjust=adjust,
                                  lookback_years=lookback, top_k=top_k,
                                  reference_symbols=ref_symbols)
        t1 = time.time()
        if not analysis.get("ok"):
            print(f"[pattern] 失败: {analysis.get('error')}")
            return jsonify(analysis)
        print(f"[pattern] 检索({t1-t0:.1f}s): {analysis['total_stocks']} stocks, {analysis.get('total_windows',0)} wins")

        retrieval = analysis.get("retrieval", {})
        final_top = retrieval.get("final_top", [])
        final_top_eval = retrieval.get("final_top_eval", final_top)
        # 评估两份：Top-K(展示用,精华小样本) + Top-100(大样本,有统计置信度)
        eval_result = evaluate_strategies(final_top)
        eval_result_full = evaluate_strategies(final_top_eval)
        analysis["strategy_eval"] = eval_result          # 兼容旧前端：默认为 Top-K
        analysis["strategy_eval_top"] = eval_result      # Top-K 评估(明确命名)
        analysis["strategy_eval_full"] = eval_result_full  # Top-100 评估
        t2 = time.time()
        best_top = eval_result.get('best_strategy', {}).get('strategy_name', '?')
        best_full = eval_result_full.get('best_strategy', {}).get('strategy_name', '?')
        print(f"[pattern] 全流程({t2-t0:.1f}s): topK_best={best_top} top100_best={best_full}")
        return jsonify(analysis)
    except Exception as e:
        traceback.print_exc()
        print(f"[pattern] 异常: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/pattern/retrieval/query", methods=["POST"])
def api_retrieval_query():
    """BE-033: 相似检索 API — 返回 query_kline + similar_windows + stage_logs"""
    try:
        body = request.get_json(force=True) or {}
        symbol = str(body.get("symbol", "sh000300")).strip()
        as_of = str(body.get("as_of_date", "")).strip() or None
        adjust = str(body.get("adjust", "qfq")).strip()
        lookback = int(body.get("lookback_years", 6))
        top_k = int(body.get("top_k", 10))
        ref_symbols = body.get("reference_symbols", None)
        print(f"[retrieval/query] symbol={symbol} top_k={top_k}")

        t0 = time.time()
        result = analyze_symbol(symbol, as_of_date=as_of, adjust=adjust,
                                lookback_years=lookback, top_k=top_k,
                                reference_symbols=ref_symbols)
        t1 = time.time()
        result["diagnostics"] = {
            "elapsed_ms": round((t1 - t0) * 1000, 1),
            "reference_stocks": result.get("total_stocks", 0),
            "total_windows": result.get("total_windows", 0),
        }
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


# ===== 股票池管理 API（BE-010）=====
@app.route("/api/universe/options")
def api_universe_options():
    return jsonify(universe_options())


@app.route("/api/universe/preview", methods=["POST"])
def api_universe_preview():
    try:
        body = request.get_json(force=True) or {}
        sources = body.get("sources", ["a500"])
        custom = body.get("custom_symbols", [])
        return jsonify(preview_universe(sources, custom))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/universe", methods=["POST"])
def api_universe_create():
    try:
        body = request.get_json(force=True) or {}
        result = create_universe(
            universe_type=str(body.get("type", "reference")),
            name=str(body.get("name", "")).strip() or "unnamed",
            sources=body.get("sources", ["a500"]),
            custom_symbols=body.get("custom_symbols", []),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/universe", methods=["GET"])
def api_universe_list():
    ut = request.args.get("type", "")
    return jsonify(list_universes(ut or None))


@app.route("/api/universe/<universe_id>", methods=["GET"])
def api_universe_get(universe_id):
    return jsonify(get_universe(universe_id))


# ===== 预下载10年K线+PE数据 =====
_PRELOAD_INDICES = [
    ("sh000510", "中证A500"), ("sh000300", "沪深300"), ("sh000001", "上证指数"),
    ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("hkHSI", "恒生指数"), ("hkHSTECH", "恒生科技"),
    ("us.SPX", "标普500"), ("us.NDX", "纳指100"),
]
_PRELOAD_START = (datetime.now() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")
_PRELOAD_END = datetime.now().strftime("%Y-%m-%d")


def _preload_worker(symbols, stage_label, download_pe=True):
    """后台预下载工作函数。多数据源多线程并行下载。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading as _th
    total = len(symbols)
    _preload_status["total"] = total
    _preload_status["ok"] = 0
    _preload_status["fail"] = 0
    _preload_status["stage"] = stage_label
    _lock = _th.Lock()

    # 3个K线数据源，动态调度：共享队列，哪个空闲取下一个
    SOURCES = ["sina", "tx", "em"]
    import queue as _q
    task_queue = _q.Queue()
    for sym in symbols:
        task_queue.put(sym)
    for s in SOURCES:
        print(f"[preload] source={s} ready")

    def _dl_one(sym_code, source):
        t0 = time.time()
        try:
            # 离线检查缓存
            df = load_kline(sym_code, _PRELOAD_START, _PRELOAD_END, adjust="qfq",
                            use_cache=True, offline=True, source=source)
            cache_rows = len(df) if df is not None and not df.empty else 0
            cache_min = str(df["date"].min())[:10] if cache_rows > 0 else ""
            cache_max = str(df["date"].max())[:10] if cache_rows > 0 else ""

            # 缓存已有3年以上且数据是最新的 → 跳过
            if cache_rows >= 250:
                years_covered = (datetime.strptime(cache_max, "%Y-%m-%d") - datetime.strptime(cache_min, "%Y-%m-%d")).days / 365.0
                days_old = (datetime.now() - datetime.strptime(cache_max, "%Y-%m-%d")).days
                if years_covered >= 3.0 and days_old <= 5:
                    if download_pe:
                        try: load_valuation_pe(sym_code, _PRELOAD_START, _PRELOAD_END, use_cache=True)
                        except Exception: pass
                    dt = time.time() - t0
                    return (sym_code, True, dt, "skip(%.1fy %s~%s)" % (years_covered, cache_min, cache_max))

            # 联网下载（缓存不足/数据旧/上市短 都要下载）
            df = load_kline(sym_code, _PRELOAD_START, _PRELOAD_END, adjust="qfq",
                            use_cache=True, source=source)
            new_rows = len(df) if df is not None and not df.empty else 0

            if new_rows > 0 and df is not None and not df.empty:
                data_min = str(df["date"].min())[:10]
                data_max = str(df["date"].max())[:10]
                years = (datetime.strptime(data_max, "%Y-%m-%d") - datetime.strptime(data_min, "%Y-%m-%d")).days / 365.0
                days_old = (datetime.now() - datetime.strptime(data_max, "%Y-%m-%d")).days

                if new_rows > cache_rows:
                    # 下载了新数据
                    if download_pe:
                        try: load_valuation_pe(sym_code, _PRELOAD_START, _PRELOAD_END, use_cache=True)
                        except Exception: pass
                    dt = time.time() - t0
                    return (sym_code, True, dt, "dl[%s](%d->%d %.1fy %s~%s)" % (source, cache_rows, new_rows, years, data_min, data_max))
                elif days_old <= 5:
                    # 行数没变但数据是最新的 → 全部历史就这么多（上市短或已全量）
                    if download_pe:
                        try: load_valuation_pe(sym_code, _PRELOAD_START, _PRELOAD_END, use_cache=True)
                        except Exception: pass
                    dt = time.time() - t0
                    return (sym_code, True, dt, "ok[%s](%drows %.1fy %s~%s)" % (source, new_rows, years, data_min, data_max))
                else:
                    # 行数没变且数据不是最新 → 网络下载失败
                    dt = time.time() - t0
                    return (sym_code, False, dt, "netfail[%s](%drows until %s)" % (source, cache_rows, data_max))
            else:
                dt = time.time() - t0
                return (sym_code, False, dt, "empty[%s]" % source)
        except Exception as e:
            dt = time.time() - t0
            return (sym_code, False, dt, "%s[%s]: %s" % (type(e).__name__, source, str(e)[:80]))

    # 字节采样线程
    _byte_history.clear()
    _byte_history.append((time.time(), 0))
    def _sample_bytes():
        while _preload_status["running"]:
            _byte_history.append((time.time(), _scan_cache_bytes()))
            if len(_byte_history) > 60:
                _byte_history.pop(0)
            time.sleep(2)
    sampler = _th.Thread(target=_sample_bytes, daemon=True)
    sampler.start()

    # 3个源从共享队列动态抢任务，快的下完帮慢的继续
    done = [0]
    def _source_worker(source_name):
        while _preload_status["running"]:
            try:
                sym = task_queue.get_nowait()
            except _q.Empty:
                break
            code = sym[0]
            sym_code, ok, dt, detail = _dl_one(code, source_name)
            with _lock:
                done[0] += 1
                if ok:
                    _preload_status["ok"] += 1
                    msg = f"[{done[0]}/{total}] {sym_code} {detail} {dt:.1f}s"
                else:
                    _preload_status["fail"] += 1
                    msg = f"[{done[0]}/{total}] {sym_code} FAIL {detail} {dt:.1f}s"
                _preload_status["current"] = f"{done[0]}/{total} {sym_code}"
                _preload_status["log"].append(msg)
                _preload_status["log_count"] += 1
                if len(_preload_status["log"]) > 200:
                    _preload_status["log"] = _preload_status["log"][-100:]
            task_queue.task_done()

    threads = []
    for src in SOURCES:
        t = _th.Thread(target=_source_worker, args=(src,), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    csv_count = len([f for f in os.listdir(CACHE_DIR) if f.endswith(".csv")])
    _preload_status["log"].append(f"[done] {stage_label}: ok={_preload_status['ok']} fail={_preload_status['fail']} csv={csv_count}")
    _preload_status["log_count"] += 1
    _preload_status["running"] = False
    _preload_status["done"] = True


def _scan_cache_bytes():
    """直接扫描data_cache目录，返回实际总字节数"""
    try:
        return sum(os.path.getsize(os.path.join(CACHE_DIR, f))
                   for f in os.listdir(CACHE_DIR) if f.endswith(".csv"))
    except Exception:
        return 0


def _start_preload(scope="all"):
    """启动预下载后台任务。"""
    import threading
    if _preload_status["running"]:
        return {"ok": False, "error": "预下载正在进行中"}
    _preload_status.update(running=True, done=False, total=0, ok=0, fail=0,
                           current="", stage="", log=[], start_time=time.time(),
                           start_bytes=_scan_cache_bytes())

    symbols = []
    if scope in ("all", "indices"):
        symbols.extend(_PRELOAD_INDICES)
        _preload_status["log"].append(f"[指数] {_PRELOAD_INDICES.__len__()}只")
    if scope in ("all", "a500"):
        a500 = get_a500_constituents()
        if a500:
            symbols.extend(a500)
            _preload_status["log"].append(f"[A500] {len(a500)}只")
    if scope in ("all", "sp500"):
        sp500 = get_sp500_constituents()
        if sp500:
            symbols.extend(sp500)
            _preload_status["log"].append(f"[标普500] {len(sp500)}只")
    if scope in ("all", "hk"):
        hk = get_hk_large_caps()
        if hk:
            symbols.extend(hk)
            _preload_status["log"].append(f"[港股] {len(hk)}只")

    if not symbols:
        _preload_status["running"] = False
        return {"ok": False, "error": "未获取到任何标的列表"}

    _preload_status["total"] = len(symbols)
    t = threading.Thread(target=_preload_worker, args=(symbols, f"预下载({scope})"), daemon=True)
    t.start()
    return {"ok": True, "total": len(symbols)}


@app.route("/api/preload/start", methods=["POST"])
def api_preload_start():
    """启动预下载。body: {"scope": "all|indices|a500|sp500|hk"}"""
    body = request.get_json(force=True) or {}
    scope = str(body.get("scope", "all")).strip()
    result = _start_preload(scope)
    return jsonify(result)


@app.route("/api/preload/stop", methods=["POST"])
def api_preload_stop():
    """停止预下载。"""
    _preload_status["running"] = False
    _preload_status["log"].append("[停止] 用户手动停止下载")
    return jsonify({"ok": True})


@app.route("/api/preload/status")
def api_preload_status():
    """查询预下载进度。"""
    elapsed = time.time() - _preload_status.get("start_time", 0) if _preload_status.get("running") else 0
    total_bytes = _scan_cache_bytes()

    # 滑动窗口计算实时速率：取15秒前的字节数，算差值
    now = time.time()
    speed_bps = 0
    if len(_byte_history) >= 2:
        target_t = now - 15.0  # 15秒前
        prev_bytes = _byte_history[0][1]
        for i in range(len(_byte_history) - 1, -1, -1):
            if _byte_history[i][0] <= target_t:
                prev_bytes = _byte_history[i][1]
                break
        cur_bytes = _byte_history[-1][1]
        dt_recent = _byte_history[-1][0] - (target_t if _byte_history[0][0] <= target_t else _byte_history[0][0])
        if dt_recent > 0:
            speed_bps = (cur_bytes - prev_bytes) / dt_recent
    # 如果实时速率为0但确实在下载，用平均速率兜底
    start_bytes = _preload_status.get("start_bytes", 0)
    if speed_bps <= 0 and _preload_status.get("running") and elapsed > 5 and total_bytes > start_bytes:
        speed_bps = (total_bytes - start_bytes) / elapsed
    if speed_bps > 1048576:
        speed = "%.1f MB/s" % (speed_bps / 1048576)
    elif speed_bps > 0:
        speed = "%.0f KB/s" % (speed_bps / 1024)
    else:
        speed = "0 KB/s"

    # 已下载总量
    if total_bytes > 1073741824:
        total_str = "%.1f GB" % (total_bytes / 1073741824)
    elif total_bytes > 1048576:
        total_str = "%.1f MB" % (total_bytes / 1048576)
    elif total_bytes > 1024:
        total_str = "%.0f KB" % (total_bytes / 1024)
    else:
        total_str = "%d B" % total_bytes
    return jsonify({
        "running": _preload_status["running"],
        "done": _preload_status["done"],
        "total": _preload_status["total"],
        "ok": _preload_status["ok"],
        "fail": _preload_status["fail"],
        "current": _preload_status["current"],
        "stage": _preload_status["stage"],
        "elapsed": round(elapsed, 1),
        "speed": speed,
        "total_data": total_str,
        "log": _preload_status["log"][-100:],
        "log_total": _preload_status.get("log_count", 0),
    })


# ===== 反馈接口：把用户意见同步为 GitHub issue =====
GITHUB_REPO = os.environ.get("GITHUB_REPO", "treachery/trade")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    try:
        body = request.get_json(force=True) or {}
        text = str(body.get("text", "")).strip()
        contact = str(body.get("contact", "")).strip()
        if not text:
            return jsonify({"ok": False, "error": "反馈内容不能为空"})
        if len(text) > 4000:
            return jsonify({"ok": False, "error": "反馈内容过长（上限4000字）"})

        title = f"[用户反馈] {text[:40]}{'…' if len(text) > 40 else ''}"
        ip = _client_ip()
        loc = _ip_location(ip)
        body_md = text
        if contact:
            body_md += f"\n\n---\n**联系方式：** {contact}"
        body_md += f"\n\n---\n*来源IP：{ip}（{loc}） · {time.strftime('%Y-%m-%d %H:%M:%S')}*"

        # 有 token 才真正创建 issue；否则记录到服务端日志
        if GITHUB_TOKEN:
            try:
                payload = json.dumps({"title": title, "body": body_md, "labels": ["user-feedback"]}).encode("utf-8")
                url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
                req = urllib.request.Request(url, data=payload, method="POST")
                req.add_header("Authorization", f"token {GITHUB_TOKEN}")
                req.add_header("Accept", "application/vnd.github+json")
                req.add_header("Content-Type", "application/json; charset=utf-8")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    issue_data = json.loads(resp.read().decode("utf-8"))
                    issue_url = issue_data.get("html_url", "")
                    print(f"[feedback] 已创建 GitHub issue: {issue_url}")
                    return jsonify({"ok": True, "issue_url": issue_url})
            except Exception as ge:
                print(f"[feedback] 创建GitHub issue失败，改为记录日志: {ge}")
                print(f"[feedback] {title}\n{body_md}")
                return jsonify({"ok": True, "issue_url": "", "note": "反馈已记录，但GitHub issue创建失败（请检查GITHUB_TOKEN配置）。"})
        else:
            print(f"[feedback] 未配置GITHUB_TOKEN，仅记录: {title}\n{body_md}")
            return jsonify({"ok": True, "issue_url": "", "note": "反馈已收到并记录（服务端未配置GITHUB_TOKEN，未自动创建GitHub issue）。"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


# ===================================================================
# ===== trade-notify：买卖信号订阅通知 =====
# ===================================================================
_SUB_PATH = os.path.join(CACHE_DIR, "notify_subscription.json")
_LATEST_PATH = os.path.join(CACHE_DIR, "notify_latest.json")

# 内存中的扫描任务表：task_id -> 进度/结果快照
_scan_tasks = OrderedDict()
_scan_lock = threading.Lock()
_SCAN_TASK_MAX = 20


def _default_subscription(name="默认订阅"):
    """单组订阅：宽基 + 行业 + 中证A500，近10交易日，自动扫描关闭。"""
    return {
        "name": name,           # 订阅名(仅展示)
        "symbols": None,        # None 表示用默认标的（含A500，动态获取）
        "include_a500": True,
        "include_sp500": False,
        "include_hk": False,
        "lookback_days": 10,
        "webhook": "",          # 接收地址之一：webhook URL
        "email": "",            # 接收地址之一：邮箱(SMTP 发送)
        "auto_enabled": False,
        "holdings": [],         # 我的持仓：[[code, name], ...]，持仓中出现清仓信号会重点提醒
    }


def _coerce_subscription(d, fallback_name="默认订阅"):
    """把任意 dict 规整为一组完整订阅(补齐缺省字段)。"""
    base = _default_subscription(fallback_name)
    if isinstance(d, dict):
        base.update({k: v for k, v in d.items() if k in base})
    base["name"] = str(base.get("name") or fallback_name).strip() or fallback_name
    base["holdings"] = _normalize_holdings(base.get("holdings"))
    return base


def _normalize_holdings(raw):
    """把各种形态的持仓输入规整为 [[code, name], ...]。"""
    out = []
    for it in (raw or []):
        if isinstance(it, dict):
            code = str(it.get("symbol") or it.get("code") or "").strip()
            nm = str(it.get("name") or "").strip()
        elif isinstance(it, (list, tuple)):
            code = str(it[0]).strip() if len(it) >= 1 else ""
            nm = str(it[1]).strip() if len(it) >= 2 else ""
        else:
            code = str(it).strip()
            nm = ""
        if code:
            out.append([code, nm or code])
    return out


def _load_subscriptions():
    """加载订阅列表 [sub, ...]。兼容旧的单组格式(自动迁移为一组)。"""
    if os.path.exists(_SUB_PATH):
        try:
            with open(_SUB_PATH, encoding="utf-8") as f:
                d = json.load(f)
            # 新格式：{"subscriptions": [...]}
            if isinstance(d, dict) and isinstance(d.get("subscriptions"), list):
                subs, seen = [], set()
                for i, s in enumerate(d["subscriptions"]):
                    one = _coerce_subscription(s, f"订阅{i + 1}")
                    nm = one["name"]
                    while nm in seen:           # 名称去重
                        nm += "_"
                    one["name"] = nm
                    seen.add(nm)
                    subs.append(one)
                if subs:
                    return subs
            # 旧格式：单个 dict -> 迁移为一组
            if isinstance(d, dict):
                return [_coerce_subscription(d, "默认订阅")]
        except Exception:
            pass
    return [_default_subscription()]


def _save_subscriptions(subs):
    try:
        with open(_SUB_PATH, "w", encoding="utf-8") as f:
            json.dump({"subscriptions": subs}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[notify] 保存订阅失败：{e}")


def _find_subscription(name):
    """按名称取一组订阅；name 为空时返回第一组。"""
    subs = _load_subscriptions()
    if not name:
        return subs[0] if subs else _default_subscription()
    for s in subs:
        if s["name"] == name:
            return s
    return None


def _load_subscription():
    """兼容旧调用：返回第一组订阅(主要给测试推送/手动扫描缺省持仓用)。"""
    subs = _load_subscriptions()
    return subs[0] if subs else _default_subscription()


def _resolve_symbols(sub):
    """把订阅配置解析为待扫描标的列表 [[symbol, name, category], ...]。
    持仓(holdings)中的票一律并入扫描范围(去重)，确保能检测其清仓信号。"""
    syms = sub.get("symbols")
    out = []
    if syms:
        for it in syms:
            if isinstance(it, (list, tuple)) and len(it) >= 1:
                code = str(it[0]).strip()
                name = str(it[1]).strip() if len(it) >= 2 else code
                cat = str(it[2]).strip() if len(it) >= 3 else "custom"
                out.append([code, name, cat])
    if not out:
        out = list(default_symbols(
            include_a500=sub.get("include_a500", True),
            include_sp500=sub.get("include_sp500", False),
            include_hk_large=sub.get("include_hk", False),
        ))

    # 保存的 symbols 通常只含指数/自定义；成分股由开关动态展开，避免把数百只股票写进订阅文件。
    have = {str(r[0]).strip() for r in out}
    def _append_dynamic(items, cat):
        for code, name in items:
            code = str(code).strip()
            if code and code not in have:
                out.append([code, name, cat])
                have.add(code)

    if sub.get("include_a500", True):
        _append_dynamic(get_a500_constituents(), "a500")
    if sub.get("include_sp500", False):
        _append_dynamic(get_sp500_constituents(), "sp500")
    if sub.get("include_hk", False):
        _append_dynamic(get_hk_large_caps(), "hk")

    # 并入持仓标的(去重)：持仓里若有不在列表中的票，补进来并标记 category=holding
    for code, name in _normalize_holdings(sub.get("holdings")):
        if code not in have:
            out.append([code, name, "holding"])
            have.add(code)
    return out


def _scan_default_backtest_config():
    """信号扫描 Top10 附加回测使用的固定策略：唐奇安突破/均线多头排列 → 移动止盈&唐奇安下轨。"""
    return StrategyConfig.from_dict({
        "entry_logic": "or",
        "exit_logic": "and",
        "entry_window": 5,
        "exit_window": 5,
        "stop_loss_pct": 10.0,
        "entries": [
            {"type": "donchian_breakout", "period": 20},
            {"type": "ma_bull_stack", "periods": [5, 10, 20, 60]},
        ],
        "exits": [
            {"type": "trailing_pct", "pct": 10},
            {"type": "donchian_exit", "period": 10},
        ],
        "position": {"entry": "fixed", "reduce": "none", "max_leverage": 1.0,
                     "min_leverage": 1.0, "reduce_start": 50.0,
                     "reduce_step": 10.0, "reduce_pct": 10.0},
    })


# ===== 形态判断 + 形态→策略映射（用于扫描 Top10 的"形态推荐策略"组）=====
def _detect_pattern(df):
    """根据近1年走势判断形态：长牛/长熊/牛熊/熊牛/震荡。

    取样：起点、终点、最高、最低，并每约3个月(63交易日)采样一个收盘点；
    依据总涨跌幅、最高/最低出现的相对位置(前半段 vs 后半段)综合判断。
    返回 (pattern_key, pattern_label)。
    """
    closes = df["close"].tolist()
    n = len(closes)
    if n < 60:
        return "range", "震荡"
    first, last = closes[0], closes[-1]
    hi = max(closes)
    lo = min(closes)
    hi_idx = closes.index(hi)
    lo_idx = closes.index(lo)
    total_chg = (last / first - 1) if first > 0 else 0.0           # 区间总涨跌幅
    amplitude = (hi / lo - 1) if lo > 0 else 0.0                    # 峰谷振幅
    half = n / 2.0
    hi_late = hi_idx >= half      # 最高点在后半段
    lo_late = lo_idx >= half      # 最低点在后半段

    # 阈值：±35% 视为单边趋势；振幅大但首尾变化小视为震荡
    UP, DOWN = 0.35, -0.35
    if total_chg >= UP and not hi_late and lo_idx < hi_idx:
        # 先低后高、终点接近高位但高点偏早 → 牛转熊(冲高回落)
        return ("cycle", "牛转熊") if (hi - last) / hi > 0.15 else ("bull", "长牛")
    if total_chg >= UP:
        return "bull", "长牛"
    if total_chg <= DOWN and lo_late:
        return "bear", "长熊"
    if total_chg <= DOWN:
        # 跌幅大但最低点偏早、终点回升 → 熊转牛
        return ("bearbull", "熊转牛") if (last - lo) / lo > 0.15 else ("bear", "长熊")
    # 首尾变化不大：看后半段相对前半段方向区分 熊牛/牛熊/震荡
    if lo_late and (last - lo) / max(lo, 1e-9) > 0.15:
        return "bearbull", "熊转牛"
    if hi_late and (hi - last) / max(hi, 1e-9) > 0.15:
        return "cycle", "牛转熊"
    return "range", "震荡"


# 形态 -> 推荐策略组合（入场OR / 出场AND）。趋势形态偏趋势跟踪，震荡偏均值回归式快进快出。
_PATTERN_STRATEGY = {
    "bull": {  # 长牛：趋势突破入场，移动止盈/跌破均线出场
        "entries": [{"type": "donchian_breakout", "period": 20},
                    {"type": "ma_bull_stack", "periods": [5, 10, 20, 60]}],
        "exits": [{"type": "trailing_pct", "pct": 10}, {"type": "ma_break", "period": 20}],
    },
    "bear": {  # 长熊：金叉确认才进，快速止损止盈出
        "entries": [{"type": "ma_golden", "fast": 50, "slow": 200},
                    {"type": "macd_golden", "fast": 12, "slow": 26, "signal": 9}],
        "exits": [{"type": "ma_break", "period": 20}, {"type": "chandelier_atr", "atr_period": 22, "mult": 3}],
    },
    "cycle": {  # 牛转熊：突破入场，吊灯ATR+跌破均线及时离场
        "entries": [{"type": "donchian_breakout", "period": 20},
                    {"type": "macd_golden", "fast": 12, "slow": 26, "signal": 9}],
        "exits": [{"type": "chandelier_atr", "atr_period": 22, "mult": 3}, {"type": "ma_break", "period": 20}],
    },
    "bearbull": {  # 熊转牛：金叉/多头排列入场，移动止盈让利润奔跑
        "entries": [{"type": "ma_golden", "fast": 50, "slow": 200},
                    {"type": "ma_bull_stack", "periods": [5, 10, 20, 60]}],
        "exits": [{"type": "trailing_pct", "pct": 10}, {"type": "ma_death_cross", "fast": 50, "slow": 200}],
    },
    "range": {  # 震荡：突破入场，唐奇安下轨/移动止盈快进快出
        "entries": [{"type": "donchian_breakout", "period": 20},
                    {"type": "ma_golden", "fast": 50, "slow": 200}],
        "exits": [{"type": "donchian_exit", "period": 10}, {"type": "trailing_pct", "pct": 10}],
    },
}


def _pattern_strategy_config(pattern_key):
    spec = _PATTERN_STRATEGY.get(pattern_key, _PATTERN_STRATEGY["range"])
    return StrategyConfig.from_dict({
        "entry_logic": "or", "exit_logic": "and",
        "entry_window": 5, "exit_window": 5, "stop_loss_pct": 10.0,
        "entries": spec["entries"], "exits": spec["exits"],
        "position": {"entry": "fixed", "reduce": "none", "max_leverage": 1.0, "min_leverage": 1.0},
    })


def _optimized_top1_config(top1):
    """把一键寻优 Top1 的 type 列表还原成带默认参数的 StrategyConfig。"""
    edefs = {d["type"]: d for d in ENTRY_DEFAULTS}
    xdefs = {d["type"]: d for d in EXIT_DEFAULTS}
    entries = [dict(edefs[t]) for t in (top1.get("entry_types") or []) if t in edefs]
    exits = [dict(xdefs[t]) for t in (top1.get("exit_types") or []) if t in xdefs]
    return StrategyConfig.from_dict({
        "entry_logic": top1.get("entry_logic", "or"),
        "exit_logic": top1.get("exit_logic", "and"),
        "entry_window": 5, "exit_window": 5, "stop_loss_pct": 10.0,
        "entries": entries, "exits": exits,
        "position": {"entry": "fixed", "reduce": "none", "max_leverage": 1.0, "min_leverage": 1.0},
    })


def _bt_summary(df, cfg, strategy_label, start, end):
    """跑一组回测并提炼用于卡片/通知的摘要。失败返回 ok=False。"""
    try:
        ret = run_backtest(df, cfg, initial_capital=100000, commission=0.0005,
                           pe_series=None, margin_rate=0.0699)
        st = ret.get("stats") or {}
        total_ret = st.get("total_return")
        buy_hold = st.get("buy_hold_return")
        excess = round(float(total_ret) - float(buy_hold), 2) \
            if total_ret is not None and buy_hold is not None else None
        return {
            "ok": True, "strategy": strategy_label, "start": start, "end": end,
            "total_return": total_ret, "buy_hold_return": buy_hold, "excess_return": excess,
            "annualized": st.get("annualized"), "max_drawdown": st.get("max_drawdown"),
            "num_trades": st.get("num_trades"), "win_rate": st.get("win_rate"),
            "short_trade_rate": st.get("short_trade_rate"),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "strategy": strategy_label}


# 通知推荐过滤门槛：推荐组近1年回测需同时满足，否则不推送买入（页面卡片仍展示并标注原因）。
NOTIFY_ANN_MIN = 5.0      # 年化收益 ≥ 5%
NOTIFY_MDD_MIN = -20.0    # 最大回撤 ≥ -20%（即跌幅不超过20%）
NOTIFY_WR_MIN = 30.0      # 胜率 ≥ 30%


def _notify_filter(b):
    """判断回测摘要是否通过通知推荐门槛。返回 (是否通过, 未达标原因列表)。"""
    if not b or not b.get("ok"):
        return False, ["无有效回测"]
    reasons = []
    try:
        ann = float(b.get("annualized"))
        dd = float(b.get("max_drawdown"))
        wr = float(b.get("win_rate"))
    except (TypeError, ValueError):
        return False, ["回测数据缺失"]
    if ann < NOTIFY_ANN_MIN:
        reasons.append(f"年化{ann}%<{NOTIFY_ANN_MIN}%")
    if dd < NOTIFY_MDD_MIN:
        reasons.append(f"回撤{dd}%>{NOTIFY_MDD_MIN}%")
    if wr < NOTIFY_WR_MIN:
        reasons.append(f"胜率{wr}%<{NOTIFY_WR_MIN}%")
    return (len(reasons) == 0), reasons


def _bt_quality(b):
    """回测质量打分：年化(小数)/回撤平方惩罚，用于在两组里选更优者。失败给极小值。"""
    if not b or not b.get("ok"):
        return -1e9
    try:
        ann = float(b.get("annualized") or 0) / 100.0
        mdd = abs(float(b.get("max_drawdown") or 0)) / 100.0
    except (TypeError, ValueError):
        return -1e9
    return ann * max(0.2, (1.0 - mdd) ** 2)


def _attach_scan_top_backtests(results):
    """扫描完成后，为推荐值 Top10 各跑两组近1年回测并选优：
      ① 形态推荐策略：先判断近1年形态(长牛/长熊/牛熊/熊牛/震荡)，按形态映射的策略跑一组；
      ② 一键寻优最优：对该标的近1年跑全组合寻优，取 Top1 策略跑一组。
    两组都记录到卡片(bt_pattern / bt_optimized)，并选效果更好者标为推荐(bt_best / bt5y 兼容字段)。
    """
    if not results:
        return results
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=365 * 1)).strftime("%Y-%m-%d")
    ranked = sorted(results, key=lambda r: (r.get("score") or {}).get("score", -1e9), reverse=True)[:10]
    for idx, r in enumerate(ranked, 1):
        symbol = str(r.get("symbol", "")).strip()
        if not symbol:
            continue
        try:
            df = load_kline(symbol, start, end, adjust="qfq")
            if df is None or df.empty:
                r["bt5y"] = {"rank": idx, "ok": False, "error": "无K线数据"}
                continue

            # ① 形态判断 + 形态推荐策略回测
            pat_key, pat_label = _detect_pattern(df)
            pat_cfg = _pattern_strategy_config(pat_key)
            pat_strat_label = (
                "/".join(CN_LABEL.get(e["type"], e["type"]) for e in pat_cfg.entries)
                + " → "
                + "&".join(CN_LABEL.get(e["type"], e["type"]) for e in pat_cfg.exits))
            bt_pattern = _bt_summary(df, pat_cfg, pat_strat_label, start, end)
            bt_pattern.update({"pattern": pat_label, "pattern_key": pat_key,
                               "entry_types": [e["type"] for e in pat_cfg.entries],
                               "exit_types": [e["type"] for e in pat_cfg.exits],
                               "entry_logic": "or", "exit_logic": "and"})

            # ② 一键寻优 Top1 回测
            bt_optimized = {"ok": False, "error": "寻优无结果", "strategy": "一键寻优"}
            try:
                opt = run_optimization(df, initial_capital=100000, commission=0.0005,
                                       top_n=1, min_trades=10, return_basis="excess")
                if opt.get("ok") and opt.get("top"):
                    top1 = opt["top"][0]
                    opt_cfg = _optimized_top1_config(top1)
                    opt_label = f"{top1.get('entry', '')} → {top1.get('exit', '')}"
                    bt_optimized = _bt_summary(df, opt_cfg, opt_label, start, end)
                    bt_optimized.update({"entry_types": top1.get("entry_types", []),
                                         "exit_types": top1.get("exit_types", []),
                                         "entry_logic": top1.get("entry_logic", "or"),
                                         "exit_logic": top1.get("exit_logic", "and")})
            except Exception as e:
                bt_optimized = {"ok": False, "error": f"{type(e).__name__}: {e}", "strategy": "一键寻优"}

            # 选优：两组比质量分，高者为推荐
            q_pat, q_opt = _bt_quality(bt_pattern), _bt_quality(bt_optimized)
            if q_opt > q_pat:
                bt_pattern["recommended"] = False
                bt_optimized["recommended"] = True
                best, best_source = bt_optimized, "optimized"
            else:
                bt_pattern["recommended"] = True
                bt_optimized["recommended"] = False
                best, best_source = bt_pattern, "pattern"

            # 推荐组是否通过通知门槛 + 未达标原因（页面卡片展示用；通知推送据此过滤）
            notify_pass, notify_reasons = _notify_filter(best)
            r["bt_pattern"] = bt_pattern
            r["bt_optimized"] = bt_optimized
            r["bt_best"] = {**best, "rank": idx, "source": best_source,
                            "notify_pass": notify_pass, "notify_fail_reasons": notify_reasons}
            # 兼容旧字段：notify.js/通知过滤仍读 bt5y，指向推荐组
            r["bt5y"] = {**best, "rank": idx, "symbol": symbol, "source": best_source,
                         "notify_pass": notify_pass, "notify_fail_reasons": notify_reasons}
        except Exception as e:
            r["bt5y"] = {"rank": idx, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return results


def _save_latest(results, total, lookback_days, source="manual"):
    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "total": total,
        "lookback_days": lookback_days,
        "results": results,
    }
    try:
        with open(_LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"[notify] 保存最新结果失败：{e}")
    return payload


def _load_latest():
    if os.path.exists(_LATEST_PATH):
        try:
            with open(_LATEST_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _pe_text(pe):
    """估值简述。"""
    if not pe:
        return "无PE"
    if pe.get("percentile") is None:
        return f"PE{pe['pe']}·当前TTM"
    return f"PE{pe['pe']}/{pe['percentile']}%分位·{pe['level']}"


def _format_notify_text(payload, holdings=None):
    """把扫描结果汇总成一段适合 IM 推送的 markdown 文本。

    结构：
      ① ⚠️ 持仓清仓提醒：持仓(holdings)中出现清仓信号的标的，重点列出；
      ② 🏆 推荐值 Top10：按「推荐值」从高到低的前 10 只，含详情。
    """
    results = payload.get("results", [])
    lb = payload.get("lookback_days", 10)
    hold_codes = {str(c).strip() for c, _ in _normalize_holdings(holdings)}

    lines = [f"### 📈 买卖信号通知（近{lb}个交易日）",
             f"扫描 {payload.get('total', 0)} 个标的 · {payload.get('time', '')}", ""]

    # ① 持仓清仓重点提醒
    if hold_codes:
        hold_exits = [r for r in results
                      if str(r.get("symbol")).strip() in hold_codes and r.get("exits")]
        hold_exits.sort(key=lambda r: min((s.get("days_ago", 99) for s in r["exits"]), default=99))
        lines.append("#### ⚠️ 持仓清仓提醒")
        if hold_exits:
            for r in hold_exits:
                sigs = "、".join(
                    f"{s['label']}（{'今日' if s.get('days_ago') == 0 else str(s.get('days_ago')) + '日前'}）"
                    for s in r["exits"])
                lines.append(f"- 🔴 **{r.get('name') or r.get('symbol')}**({r.get('symbol')}) "
                             f"触发清仓：{sigs} | {_pe_text(r.get('pe'))}")
        else:
            lines.append("- ✅ 你的持仓近期均未触发清仓信号。")
        lines.append("")

    # ② 推荐值 Top10：推送前按1年回测质量过滤
    ranked = sorted(results, key=lambda r: (r.get("score") or {}).get("score", -1e9), reverse=True)
    raw_top = ranked[:10]

    def _bt5y_pass(r):
        b = r.get("bt5y") or {}
        if "notify_pass" in b:        # 优先用回测时算好的判定(口径一致)
            return bool(b.get("notify_pass"))
        return _notify_filter(b)[0]   # 兼容旧数据

    top = [r for r in raw_top if _bt5y_pass(r)]
    lines.append("#### 🏆 推荐值 Top10（已过滤：1年年化≥5%、最大回撤≤20%、胜率≥30%）")
    if not raw_top:
        lines.append("近期无标的触发信号。")
        return "\n".join(lines)
    if not top:
        lines.append("推荐值Top10均未通过1年回测过滤条件，本次不推荐买入标的。")
        return "\n".join(lines)
    for idx, r in enumerate(top, 1):
        sc = r.get("score") or {}
        if sc.get("score") is not None:
            sc_txt = (f"{sc['score']}·{sc['level']}"
                      f"（交易{sc.get('trade', 0):+} / 估值{sc.get('valuation', 0):+}）")
        else:
            sc_txt = "—"
        tag = []
        if r.get("entries"):
            buy_sig = "、".join(s["label"] for s in r["entries"][:3])
            tag.append(f"🟢{buy_sig}")
        if r.get("exits"):
            tag.append("🔴有清仓信号")
        sug = r.get("suggest", {}).get("text", "")
        b = r.get("bt5y") or {}
        src = {"pattern": "形态推荐", "optimized": "一键寻优"}.get(b.get("source"), "")
        pat = b.get("pattern")
        if b.get("ok"):
            head = f"1年回测·⭐推荐策略[{src}]" + (f"·形态{pat}" if pat else "")
            bt_txt = (f"{head}：{b.get('strategy', '')} → 策略{b.get('total_return')}% / "
                      f"年化{b.get('annualized')}% / 回撤{b.get('max_drawdown')}% / 胜率{b.get('win_rate')}%")
        else:
            bt_txt = "1年回测：—"
        lines.append(
            f"{idx}. 【{sc_txt}】**{r.get('name') or r.get('symbol')}**({r.get('symbol')}) "
            f"最新价{r.get('last_close')} | {_pe_text(r.get('pe'))} | 建议仓位{sug} | {bt_txt}"
            + (f"\n   {' / '.join(tag)}" if tag else ""))
    return "\n".join(lines)


def _push_webhook(url, text):
    """推送到 webhook。自动适配企业微信机器人 / Server酱 / 通用 JSON。"""
    if not url:
        return False, "未配置webhook"
    try:
        low = url.lower()
        if "qyapi.weixin" in low:                      # 企业微信群机器人
            payload = {"msgtype": "markdown", "markdown": {"content": text}}
        elif "sctapi.ftqq" in low or "sc.ftqq" in low:  # Server酱
            first = text.strip().splitlines()[0] if text.strip() else "信号通知"
            payload = {"title": first[:60], "desp": text}
        elif "oapi.dingtalk" in low:                    # 钉钉机器人
            payload = {"msgtype": "markdown", "markdown": {"title": "买卖信号通知", "text": text}}
        else:                                            # 通用
            payload = {"text": text, "content": text}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True, "ok"
    except Exception as e:
        print(f"[notify] webhook 推送失败：{e}")
        return False, str(e)


def _smtp_config():
    """从环境变量读取 SMTP 发件配置。
      SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM / SMTP_SSL(1/0)
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "465")),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "pass": os.environ.get("SMTP_PASS", "").strip(),
        "from": os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")).strip(),
        "ssl": os.environ.get("SMTP_SSL", "1") != "0",
    }


def _send_email(to_addr, subject, text):
    """通过 SMTP 发送一封纯文本邮件。发件配置取自环境变量(见 _smtp_config)。"""
    if not to_addr:
        return False, "未配置邮箱"
    cfg = _smtp_config()
    if cfg is None:
        return False, "服务端未配置SMTP(需设置 SMTP_HOST 等环境变量)"
    try:
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header(subject[:120], "utf-8")
        msg["From"] = formataddr(("trade-notify", cfg["from"]))
        msg["To"] = to_addr
        if cfg["ssl"]:
            srv = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20)
        else:
            srv = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
            srv.starttls()
        try:
            if cfg["user"]:
                srv.login(cfg["user"], cfg["pass"])
            srv.sendmail(cfg["from"], [to_addr], msg.as_string())
        finally:
            srv.quit()
        return True, "ok"
    except Exception as e:
        print(f"[notify] 邮件推送失败：{e}")
        return False, str(e)


def _dispatch_notify(sub, text, subject="trade-notify 买卖信号通知"):
    """按订阅配置的接收方式分发(webhook + email 都发)。返回 [(渠道, ok, msg), ...]。"""
    out = []
    url = str(sub.get("webhook", "")).strip()
    mail = str(sub.get("email", "")).strip()
    if url:
        ok, msg = _push_webhook(url, text)
        out.append(("webhook", ok, msg))
    if mail:
        ok, msg = _send_email(mail, subject, text)
        out.append(("email", ok, msg))
    return out


def _run_scan_task(task_id, symbols, lookback_days, source="manual", webhook="", holdings=None):
    """后台执行批量扫描，逐个更新进度。仅保留有信号的标的。"""
    results = []
    total = len(symbols)
    # 并发数：IO 密集(网络抓数)，默认 10，可用环境变量 SCAN_WORKERS 覆盖；上限 24 防限频
    try:
        workers = max(1, min(int(os.environ.get("SCAN_WORKERS", 10)), 24))
    except Exception:
        workers = 10
    workers = min(workers, total) or 1

    use_cache = (source != "manual")
    holding_codes = {str(c).strip() for c, _ in (holdings or [])}  # 持仓代码集合(始终保留在结果里)
    def _scan_one(item):
        sym, name, cat = (item + ["", ""])[:3] if isinstance(item, list) else (item, "", "")
        try:
            return scan_symbol(sym, name, cat, lookback_days=lookback_days, use_cache=use_cache)
        except Exception as e:
            return {"symbol": sym, "name": name, "category": cat, "error": str(e)}

    def _is_cancelled():
        with _scan_lock:
            t = _scan_tasks.get(task_id)
            return t is None or t.get("cancel_requested") or t.get("status") == "cancelled"

    done = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    futures = []
    try:
        for item in symbols:
            if _is_cancelled():
                break
            futures.append(ex.submit(_scan_one, item))
        for fut in as_completed(futures):
            if _is_cancelled():
                ex.shutdown(wait=False, cancel_futures=True)
                return
            r = fut.result()
            done += 1
            with _scan_lock:
                t = _scan_tasks.get(task_id)
                if t is None or t.get("cancel_requested") or t.get("status") == "cancelled":
                    ex.shutdown(wait=False, cancel_futures=True)
                    return
                t["done"] = done
                t["current"] = r.get("name") or r.get("symbol") or ""
                if r.get("has_signal") or str(r.get("symbol", "")).strip() in holding_codes:
                    results.append(r)
                    t["results"] = list(results)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if _is_cancelled():
        return
    with _scan_lock:
        t = _scan_tasks.get(task_id)
        if t is not None and not t.get("cancel_requested") and t.get("status") != "cancelled":
            t["done"] = total
            t["current"] = "正在回测推荐值Top10..."
            t["status"] = "backtesting"
            t["results"] = list(results)
    results = _attach_scan_top_backtests(results)
    if _is_cancelled():
        return
    payload = _save_latest(results, total, lookback_days, source=source)
    with _scan_lock:
        t = _scan_tasks.get(task_id)
        if t is not None and not t.get("cancel_requested") and t.get("status") != "cancelled":
            t["done"] = total
            t["current"] = ""
            t["status"] = "finished"
            t["finished"] = True
            t["results"] = results
    if webhook and not _is_cancelled():
        _push_webhook(webhook, _format_notify_text(payload, holdings=holdings))


@app.route("/api/notify/symbols")
def api_notify_symbols():
    """返回可选标的列表：A股宽基/A500成分 + 港股指数/大市值成分 + 美股指数/标普500成分。
    成分股较重(联网)，按需用 query 开关控制是否返回。"""
    try:
        include_a500 = request.args.get("include_a500", "1") != "0"
        include_sp500 = request.args.get("include_sp500", "0") != "0"
        include_hk = request.args.get("include_hk", "0") != "0"
        a500 = get_a500_constituents() if include_a500 else []
        sp500 = get_sp500_constituents() if include_sp500 else []
        hk_large = get_hk_large_caps() if include_hk else []
        return jsonify({
            "ok": True,
            "broad": [{"symbol": c, "name": n} for c, n in BROAD_INDICES],
            "industry": [{"symbol": c, "name": n} for c, n in INDUSTRY_INDICES],
            "a500": [{"symbol": c, "name": n} for c, n in a500],
            "hk_index": [{"symbol": c, "name": n} for c, n in HK_INDICES],
            "us_index": [{"symbol": c, "name": n} for c, n in US_INDICES],
            "sp500": [{"symbol": c, "name": n} for c, n in sp500],
            "hk": [{"symbol": c, "name": n} for c, n in hk_large],
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/notify/scan", methods=["POST"])
def api_notify_scan():
    """启动一次扫描任务（异步），返回 task_id 供轮询。"""
    try:
        body = request.get_json(force=True) or {}
        lookback_days = max(1, min(int(body.get("lookback_days", 10)), 60))
        symbols = body.get("symbols")
        if symbols:
            norm = []
            for it in symbols:
                if isinstance(it, dict):
                    norm.append([str(it.get("symbol", "")).strip(),
                                 str(it.get("name", "")).strip(),
                                 str(it.get("category", "custom")).strip()])
                elif isinstance(it, (list, tuple)):
                    norm.append([str(it[0]).strip(),
                                 str(it[1]).strip() if len(it) > 1 else "",
                                 str(it[2]).strip() if len(it) > 2 else "custom"])
            symbols = [s for s in norm if s[0]]
        else:
            symbols = default_symbols(include_a500=bool(body.get("include_a500", True)))

        # 持仓：优先用请求体，缺省读已保存订阅；并入扫描范围(去重)以便检测清仓信号
        holdings = _normalize_holdings(body.get("holdings")) \
            if "holdings" in body else _normalize_holdings(_load_subscription().get("holdings"))
        have = {str(s[0]).strip() for s in symbols}
        for code, name in holdings:
            if code not in have:
                symbols.append([code, name, "holding"])
                have.add(code)

        if not symbols:
            return jsonify({"ok": False, "error": "没有可扫描的标的"})

        task_id = f"scan_{int(time.time() * 1000)}"
        with _scan_lock:
            _scan_tasks[task_id] = {
                "status": "running", "finished": False, "cancelled": False,
                "cancel_requested": False,
                "total": len(symbols), "done": 0, "current": "",
                "results": [], "started": time.time(),
            }
            while len(_scan_tasks) > _SCAN_TASK_MAX:
                _scan_tasks.popitem(last=False)

        webhook = str(body.get("webhook", "")).strip()
        threading.Thread(target=_run_scan_task,
                         args=(task_id, symbols, lookback_days, "manual", webhook, holdings),
                         daemon=True).start()
        return jsonify({"ok": True, "task_id": task_id, "total": len(symbols)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/notify/scan_status")
def api_notify_scan_status():
    task_id = request.args.get("task_id", "")
    with _scan_lock:
        t = _scan_tasks.get(task_id)
        if t is None:
            return jsonify({"ok": False, "error": "任务不存在或已过期"})
        snap = {
            "ok": True, "status": t["status"], "finished": t["finished"],
            "cancelled": bool(t.get("cancelled")),
            "total": t["total"], "done": t["done"], "current": t["current"],
            "results": t["results"],
        }
    return jsonify(snap)


@app.route("/api/notify/scan_cancel", methods=["POST"])
def api_notify_scan_cancel():
    """取消一次正在进行的扫描任务。"""
    try:
        body = request.get_json(force=True) or {}
        task_id = str(body.get("task_id", "")).strip()
        if not task_id:
            return jsonify({"ok": False, "error": "缺少 task_id"})
        with _scan_lock:
            t = _scan_tasks.get(task_id)
            if t is None:
                return jsonify({"ok": False, "error": "任务不存在或已过期"})
            t["cancel_requested"] = True
            t["cancelled"] = True
            t["finished"] = True
            t["status"] = "cancelled"
            t["current"] = ""
        return jsonify({"ok": True, "task_id": task_id, "cancelled": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _apply_sub_fields(sub, body):
    """把请求体字段写入一组订阅(原地修改并返回)。"""
    if "name" in body:
        sub["name"] = str(body["name"]).strip() or sub.get("name") or "未命名订阅"
    if "symbols" in body:
        syms = body.get("symbols")
        if syms is None:
            sub["symbols"] = None
        else:
            norm = []
            for it in syms:
                if isinstance(it, dict):
                    norm.append([str(it.get("symbol", "")).strip(),
                                 str(it.get("name", "")).strip(),
                                 str(it.get("category", "custom")).strip()])
                elif isinstance(it, (list, tuple)):
                    norm.append([str(it[0]).strip(),
                                 str(it[1]).strip() if len(it) > 1 else "",
                                 str(it[2]).strip() if len(it) > 2 else "custom"])
            sub["symbols"] = [s for s in norm if s[0]]
    if "include_a500" in body:
        sub["include_a500"] = bool(body["include_a500"])
    if "lookback_days" in body:
        sub["lookback_days"] = max(1, min(int(body["lookback_days"]), 60))
    if "webhook" in body:
        sub["webhook"] = str(body["webhook"]).strip()
    if "email" in body:
        sub["email"] = str(body["email"]).strip()
    if "auto_enabled" in body:
        sub["auto_enabled"] = bool(body["auto_enabled"])
    if "holdings" in body:
        sub["holdings"] = _normalize_holdings(body["holdings"])
    return sub


@app.route("/api/notify/subscriptions", methods=["GET"])
def api_notify_subscriptions():
    """返回全部订阅列表。"""
    return jsonify({"ok": True, "subscriptions": _load_subscriptions()})


@app.route("/api/notify/subscription", methods=["GET", "POST", "DELETE"])
def api_notify_subscription():
    """单组订阅的增改删(按 name 标识)。
      GET   ?name=xxx       取某组(缺省第一组)
      POST  {name, ...}     新建或更新(按 name upsert；body 带 old_name 可改名)
      DELETE ?name=xxx      删除某组
    """
    try:
        if request.method == "GET":
            name = request.args.get("name", "")
            sub = _find_subscription(name)
            if sub is None:
                return jsonify({"ok": False, "error": "订阅不存在"})
            return jsonify({"ok": True, "subscription": sub})

        if request.method == "DELETE":
            name = request.args.get("name", "")
            subs = _load_subscriptions()
            rest = [s for s in subs if s["name"] != name]
            if len(rest) == len(subs):
                return jsonify({"ok": False, "error": "订阅不存在"})
            # 删到 0 组时，保存空列表(下次 _load 会回落到默认组，但当前真实反映"已删除")
            _save_subscriptions(rest)
            return jsonify({"ok": True, "subscriptions": rest})

        # POST：upsert（唯一标识=接收地址 webhook/email；name 仅展示）
        body = request.get_json(force=True) or {}
        subs = _load_subscriptions()
        # 定位目标：优先 old_name(沿用现有定位逻辑)，否则 name
        key = str(body.get("old_name") or body.get("name") or "").strip()
        target = next((s for s in subs if s["name"] == key), None)
        if target is None:
            target = _default_subscription(str(body.get("name") or "新订阅").strip() or "新订阅")
            subs.append(target)
        _apply_sub_fields(target, body)

        # 唯一性校验：webhook / email 各自不能与其它组重复(空值不校验)
        others = [s for s in subs if s is not target]
        new_url = str(target.get("webhook", "")).strip()
        new_mail = str(target.get("email", "")).strip()
        if not new_url and not new_mail:
            return jsonify({"ok": False, "error": "请至少填写一个接收地址（Webhook 或 邮箱）"})
        if new_url and any(str(s.get("webhook", "")).strip() == new_url for s in others):
            return jsonify({"ok": False, "error": f"该 Webhook 地址已被其它订阅使用，每个接收地址只能建一个订阅"})
        if new_mail and any(str(s.get("email", "")).strip() == new_mail for s in others):
            return jsonify({"ok": False, "error": f"该邮箱已被其它订阅使用，每个接收地址只能建一个订阅"})

        _save_subscriptions(subs)
        return jsonify({"ok": True, "subscription": target, "subscriptions": subs})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/notify/latest")
def api_notify_latest():
    return jsonify({"ok": True, "latest": _load_latest()})


@app.route("/api/notify/test_webhook", methods=["POST"])
def api_notify_test_webhook():
    """测试推送：按当前填写的接收方式(webhook + 邮箱)发送，内容与每日盘后一致。
    优先用最近一次扫描结果；若还没扫描过，则发一条配置成功的提示。"""
    body = request.get_json(force=True) or {}
    url = str(body.get("webhook", "")).strip()
    mail = str(body.get("email", "")).strip()
    holdings = _normalize_holdings(body.get("holdings")) \
        if "holdings" in body else _normalize_holdings(_load_subscription().get("holdings"))

    if not url and not mail:
        return jsonify({"ok": False, "msg": "请先填写 Webhook 或 邮箱地址"})

    latest = _load_latest()
    if latest and latest.get("results"):
        text = "### ✅ trade-notify 测试推送\n" \
               "以下为最近一次扫描结果(每日盘后推送的内容样式)：\n\n" \
               + _format_notify_text(latest, holdings=holdings)
    else:
        text = "### ✅ trade-notify 测试消息\n你的接收地址配置成功。" \
               "完成一次「立即扫描」后，每日盘后会推送：⚠️持仓清仓提醒 + 🏆推荐值Top10。"

    results = _dispatch_notify({"webhook": url, "email": mail}, text)
    ok_all = all(ok for _, ok, _ in results) and bool(results)
    msg = "；".join(f"{ch}:{'成功' if ok else msg}" for ch, ok, msg in results)
    return jsonify({"ok": ok_all, "msg": msg or "无可用渠道"})


def _sub_symbol_codes(sub):
    """该组订阅关心的标的代码集合(标的 + 持仓)。"""
    codes = {str(r[0]).strip() for r in _resolve_symbols(sub)}
    codes |= {c for c, _ in _normalize_holdings(sub.get("holdings"))}
    return codes


def _filter_results_for_sub(all_results, sub):
    """从全集扫描结果里，筛出该组订阅关心的标的(用于分组推送)。"""
    codes = _sub_symbol_codes(sub)
    return [r for r in all_results if str(r.get("symbol")).strip() in codes]


# ===== 每日收盘后自动扫描（后台守护线程，best-effort）=====
def _notify_daily_loop():
    """每 10 分钟检查：交易日收盘后(15:30+)、今日未跑过，则扫描一次全集，
    再对每个开启自动的订阅，从全集结果中筛选其标的/持仓后分别推送。"""
    while True:
        try:
            subs = _load_subscriptions()
            auto_subs = [s for s in subs if s.get("auto_enabled")]
            if auto_subs:
                now = datetime.now()
                after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 30)
                latest = _load_latest()
                done_today = latest and latest.get("date") == now.strftime("%Y-%m-%d") \
                    and latest.get("source") == "auto"
                if after_close and now.weekday() < 5 and not done_today:
                    print(f"[notify] 触发每日自动扫描（{len(auto_subs)} 组订阅）…")
                    # 全集 = 所有开启自动的订阅的标的+持仓并集；lookback 取各组最大值
                    lb = max((int(s.get("lookback_days", 10)) for s in auto_subs), default=10)
                    union = {}
                    for s in auto_subs:
                        for item in _resolve_symbols(s):
                            sym, nm, cat = (item + ["", ""])[:3]
                            union.setdefault(str(sym).strip(), [sym, nm, cat])
                    all_results = []
                    for sym, nm, cat in union.values():
                        try:
                            r = scan_symbol(sym, nm, cat, lookback_days=lb)
                            if r.get("has_signal"):
                                all_results.append(r)
                        except Exception:
                            pass
                    # 全集结果存档(供站内/测试推送复用)
                    _save_latest(all_results, len(union), lb, source="auto")
                    # 分组筛选 + 各自推送(webhook + email 都发)
                    for s in auto_subs:
                        if not s.get("webhook") and not s.get("email"):
                            continue
                        sub_results = _filter_results_for_sub(all_results, s)
                        payload = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                   "total": len(_sub_symbol_codes(s)),
                                   "lookback_days": s.get("lookback_days", 10),
                                   "results": sub_results}
                        text = _format_notify_text(payload, holdings=s.get("holdings"))
                        chans = _dispatch_notify(s, text, subject=f"trade-notify｜{s['name']}买卖信号")
                        ch_txt = ",".join(f"{c}{'✓' if ok else '✗'}" for c, ok, _ in chans)
                        print(f"[notify] 订阅「{s['name']}」推送：{len(sub_results)} 个有信号 [{ch_txt}]")
                    print(f"[notify] 自动扫描完成：全集 {len(all_results)}/{len(union)} 个有信号")
        except Exception as e:
            print(f"[notify] 自动扫描循环出错（不影响服务）：{e}")
        time.sleep(600)


def _start_notify_daemon():
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("FLASK_DEBUG"):
        threading.Thread(target=_notify_daily_loop, daemon=True).start()


# ===== 启动预热：默认参数的回测 + 寻优 + 三段市场环境（异步，不阻塞启动）=====
_warmup_done = False

def _warmup():
    """预热服务端缓存：默认回测参数 + 一键寻优 + 牛/熊/牛熊/熊牛四段，新用户打开即秒出。"""
    global _warmup_done
    if _warmup_done:
        return
    _warmup_done = True
    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    ten_ago = (datetime.now() - timedelta(days=3650)).strftime("%Y-%m-%d")
    try:
        print("[warmup] 开始预热默认参数缓存...")
        # 1. 默认回测参数（与前端默认一致）
        default_cfg = StrategyConfig.from_dict({
            "entry_logic": "or", "exit_logic": "and",
            "stop_loss_pct": 10.0,
            "entries": [{"type": "ma_golden", "fast": 50, "slow": 200},
                        {"type": "donchian_breakout", "period": 20}],
            "exits": [{"type": "ma_break", "period": 20},
                      {"type": "ma_death_cross", "fast": 50, "slow": 200}],
            "position": {"entry": "pe_percentile", "reduce": "pe_percentile",
                         "max_leverage": 2.0, "min_leverage": 0.5,
                         "reduce_step": 10.0, "reduce_pct": 10.0},
        })
        ck = _bt_cache_key("sh000300", ten_ago, today, "qfq", 100000, 0.0005, 0.0699, default_cfg.to_dict())
        if _cache_get(ck) is None:
            df = load_kline("sh000300", ten_ago, today, adjust="qfq")
            if df is not None and not df.empty:
                pe_df = load_pe("sh000300", _minus_years(ten_ago, 5), today)
                pe_series = dict(zip(pe_df["date"], pe_df["pe"])) if pe_df is not None and not pe_df.empty else None
                r = run_backtest(df, default_cfg, initial_capital=100000, commission=0.0005,
                                 pe_series=pe_series, margin_rate=0.0699)
                r["meta"] = {"symbol": "sh000300", "start": ten_ago, "end": today, "adjust": "qfq",
                             "rows": len(df), "strategy": default_cfg.to_dict(),
                             "pe_available": pe_series is not None, "pe_proxy": None}
                if r.get("ok"):
                    _cache_put(ck, r)
                    print(f"[warmup] 默认回测已缓存（{len(df)}交易日）")
        # 2. 默认一键寻优
        ok_ck = _opt_cache_key("sh000300", ten_ago, today, "qfq", 0.0005, 10, 10)
        if _cache_get(ok_ck) is None:
            df = load_kline("sh000300", ten_ago, today, adjust="qfq")
            if df is not None and not df.empty:
                r = run_optimization(df, initial_capital=100000, commission=0.0005, top_n=10, min_trades=10)
                r["meta"] = {"symbol": "sh000300", "start": ten_ago, "end": today, "rows": len(df)}
                if r.get("ok"):
                    _cache_put(ok_ck, r)
                    print("[warmup] 默认寻优已缓存")
        # 3. 五段市场环境最佳策略：牛市 / 熊市 / 一轮牛熊 / 熊牛 / 近十年（基准=创业板指 sz399006）
        decade_start = (datetime.now() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")
        for s, e in [("2019-01-01", "2021-02-18"), ("2021-02-18", "2024-02-01"),
                     ("2019-01-01", "2024-02-01"), ("2021-02-18", "2026-06-22"),
                     (decade_start, today)]:
            rk = _opt_cache_key("sz399006", s, e, "qfq", 0.0005, 1, 5)
            if _cache_get(rk) is None:
                df = load_kline("sz399006", s, e, adjust="qfq")
                if df is not None and not df.empty:
                    r = run_optimization(df, initial_capital=100000, commission=0.0005, top_n=1, min_trades=5)
                    r["meta"] = {"symbol": "sz399006", "start": s, "end": e, "rows": len(df)}
                    if r.get("ok"):
                        _cache_put(rk, r)
                        print(f"[warmup] 市场环境 {s}~{e} 已缓存")
        print(f"[warmup] 预热完成，缓存共 {len(_SERVER_CACHE)} 组")
    except Exception as e:
        print(f"[warmup] 预热出错（不影响服务）: {e}")


def _start_warmup():
    """在非 reloader 子进程、或 gunicorn worker 中启动一次预热。"""
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("FLASK_DEBUG"):
        threading.Thread(target=_warmup, daemon=True).start()


_start_warmup()
_start_notify_daemon()


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug, threaded=True)
