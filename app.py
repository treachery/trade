"""股票策略回测平台 - Flask 入口。

运行：
  python app.py
然后浏览器打开 http://127.0.0.1:5000
"""
import os
import time
import json
import threading
import urllib.request
import traceback
from collections import defaultdict, deque, OrderedDict

from flask import Flask, render_template, request, jsonify

from backtest import load_kline, load_pe, run_backtest, run_optimization, StrategyConfig, INDEX_PE_PROXY

app = Flask(__name__)


# ===== 服务端 LRU 缓存：最多100组回测/寻优结果，避免重复计算 =====
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
        "s": symbol, "st": start, "e": end, "a": adjust,
        "ic": round(initial_capital, 2), "cm": round(commission, 6),
        "mr": round(margin_rate, 6), "cfg": config_dict,
    }, sort_keys=True, ensure_ascii=False)


def _opt_cache_key(symbol, start, end, adjust, commission, top_n, min_trades):
    return "opt:" + json.dumps({
        "s": symbol, "st": start, "e": end, "a": adjust,
        "cm": round(commission, 6), "tn": top_n, "mt": min_trades,
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


@app.before_request
def _rate_limit_guard():
    if request.path.startswith("/api/"):
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
        pe_series = None
        pe_available = False
        pe_proxy = INDEX_PE_PROXY.get(str(symbol).strip().lower())
        if config.position.get("type") == "pe_percentile":
            pe_df = load_pe(symbol, start, end)
            if pe_df is not None and not pe_df.empty:
                pe_series = dict(zip(pe_df["date"], pe_df["pe"]))
                pe_available = True

        result = run_backtest(df, config, initial_capital=initial_capital,
                              commission=commission, pe_series=pe_series,
                              margin_rate=margin_rate)
        result["meta"] = {
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

        # 服务端缓存命中 → 直接返回
        ck = _opt_cache_key(symbol, start, end, adjust, commission, top_n, min_trades)
        cached = _cache_get(ck)
        if cached is not None:
            return jsonify(cached)

        df = load_kline(symbol, start, end, adjust=adjust)
        if df is None or df.empty:
            return jsonify({"ok": False, "error": f"未获取到 {symbol} 在 {start}~{end} 的数据。"})

        result = run_optimization(df, initial_capital=initial_capital, commission=commission,
                                  top_n=top_n, min_trades=min_trades)
        result["meta"] = {"symbol": symbol, "start": start, "end": end, "rows": len(df)}
        if result.get("ok"):
            _cache_put(ck, result)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


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


# ===== 启动预热：默认参数的回测 + 寻优 + 三段市场环境（异步，不阻塞启动）=====
_warmup_done = False

def _warmup():
    """预热服务端缓存：默认回测参数 + 一键寻优 + 牛熊牛熊三段，新用户打开即秒出。"""
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
            "entries": [{"type": "ma_golden", "fast": 50, "slow": 200},
                        {"type": "donchian_breakout", "period": 20}],
            "exits": [{"type": "ma_break", "period": 20},
                      {"type": "ma_death_cross", "fast": 50, "slow": 200}],
            "position": {"type": "pe_percentile", "max_leverage": 2.0,
                         "min_leverage": 0.5, "deleverage_step": 10.0},
        })
        ck = _bt_cache_key("sh000300", ten_ago, today, "qfq", 100000, 0.0005, 0.0699, default_cfg.to_dict())
        if _cache_get(ck) is None:
            df = load_kline("sh000300", ten_ago, today, adjust="qfq")
            if df is not None and not df.empty:
                pe_df = load_pe("sh000300", ten_ago, today)
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
        # 3. 三段市场环境最佳策略
        for s, e in [("2019-01-01", "2021-02-18"), ("2021-02-18", "2024-02-01"), ("2019-01-01", "2024-02-01")]:
            rk = _opt_cache_key("sh000300", s, e, "qfq", 0.0005, 1, 5)
            if _cache_get(rk) is None:
                df = load_kline("sh000300", s, e, adjust="qfq")
                if df is not None and not df.empty:
                    r = run_optimization(df, initial_capital=100000, commission=0.0005, top_n=1, min_trades=5)
                    r["meta"] = {"symbol": "sh000300", "start": s, "end": e, "rows": len(df)}
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


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug, threaded=True)
