"""标的信号扫描：对一批标的，逐个跑项目定义的 5 个入场指标 + 5 个清仓指标，
输出"近 N 个交易日内触发的信号 + 当前估值(PE近5年分位) + 建议仓位"。

供 trade-notify 订阅通知页使用。所有指标实现直接复用 engine.py，保证与回测口径一致。

清仓指标说明：
  - 静态 3 个(跌破均线/唐奇安下轨/双均线死叉)：直接按指标定义取"由未触发→触发"的上升沿。
  - 动态 2 个(吊灯ATR/移动止盈)：回测里依赖"持仓最高点"，在无持仓的扫描场景下，
    用「近 high_window 个交易日的滚动最高点」作为参照，语义=从近期高点回落超过阈值，
    作为清仓提示同样合理且可独立解释。
"""
import os
import json
import time
import threading
from datetime import datetime, timedelta

from .data import (load_kline, load_pe, load_stock_pe, load_index_current_pe,
                   INDEX_PE_NAME, INDEX_MARKET_PE, INDEX_LEGU_CSI, CACHE_DIR)
from .strategy import ENTRY_DEFAULTS, EXIT_DEFAULTS
from .engine import (_entry_state, _static_exit_state, _atr,
                     _trailing_pe_percentiles, _entry_label, _exit_label)


# ===== 扫描结果缓存（按交易日失效）=====
# 同一标的、同一最新交易日(last_date)、同一参数(lookback/adjust/high_window)，
# 扫描结果只计算一次；出现新 K 线(last_date 变化)才重算。落地 json，重启不丢。
_SCAN_CACHE_PATH = os.path.join(CACHE_DIR, "scan_results.json")
_scan_cache_lock = threading.Lock()
_scan_cache = None   # 内存镜像：{key: {"last_date":..., "result":...}}


def _scan_cache_key(symbol, lookback_days, adjust, high_window):
    return f"{symbol}|{lookback_days}|{adjust}|{high_window}"


def _load_scan_cache():
    global _scan_cache
    if _scan_cache is not None:
        return _scan_cache
    data = {}
    if os.path.exists(_SCAN_CACHE_PATH):
        try:
            with open(_SCAN_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    _scan_cache = data
    return _scan_cache


def _scan_cache_get_fresh(key, today_str):
    """同一自然日内已扫过(cache_day==今天) -> 直接返回缓存结果，
    连 load_kline 都不调用(0 网络 0 计算)。跨天/新交易日则不命中，走完整流程。"""
    cache = _load_scan_cache()
    with _scan_cache_lock:
        item = cache.get(key)
    if item and item.get("cache_day") == today_str:
        return item.get("result")
    return None


def _scan_cache_put(key, last_date, today_str, result):
    cache = _load_scan_cache()
    with _scan_cache_lock:
        cache[key] = {"last_date": last_date, "cache_day": today_str,
                      "result": result, "ts": time.time()}
        try:
            tmp = _SCAN_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, _SCAN_CACHE_PATH)
        except Exception as e:
            print(f"[scanner] 扫描结果缓存写入失败：{e}")


# ===== 默认监听标的 =====
# 5 个主要宽基指数：
#   沪深300 / 创业50  -> 乐咕日度 PE，可算近5年分位；
#   上证指数 / 深证成指 -> 交易所市场平均 PE(月度)，可算近5年分位；
#   中证A500          -> 中证官方当前 TTM PE(无长历史分位)。
BROAD_INDICES = [
    ("sh000510", "中证A500"),
    ("sh000300", "沪深300"),
    ("sz399673", "创业50"),
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
]
# 行业指数已按需求取消，仅保留宽基 + 中证A500成分股。
# (变量保留为空列表以兼容历史引用)
INDUSTRY_INDICES = []
A500_INDEX_CODE = "000510"   # 中证A500 指数代码


def get_a500_constituents(use_cache=True, max_age_days=7):
    """获取中证A500成分股 [[code, name], ...]，本地缓存 7 天。失败返回空列表。"""
    path = os.path.join(CACHE_DIR, "a500_cons.json")
    if use_cache and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - d.get("ts", 0) < max_age_days * 86400 and d.get("list"):
                return d["list"]
        except Exception:
            pass

    out = []
    try:
        import akshare as ak
        df = None
        # 优先中证指数官方成分接口，失败再退新浪接口
        try:
            df = ak.index_stock_cons_csindex(symbol=A500_INDEX_CODE)
        except Exception:
            df = ak.index_stock_cons(symbol=A500_INDEX_CODE)
        if df is not None and not df.empty:
            def _pick(cols, key):
                # 优先"成分券/品种/证券"列，避免误取"指数代码/指数名称"
                for pref in ("成分券", "品种", "证券"):
                    for c in cols:
                        if pref in str(c) and key in str(c):
                            return c
                for c in cols:                       # 退而求其次：含key但不含"指数"
                    if key in str(c) and "指数" not in str(c):
                        return c
                return None
            code_col = _pick(df.columns, "代码")
            name_col = _pick(df.columns, "名称")
            if code_col:
                seen = set()
                for _, r in df.iterrows():
                    code = str(r[code_col]).strip().split(".")[0].zfill(6)
                    if not code.isdigit() or code in seen:
                        continue
                    seen.add(code)
                    name = str(r[name_col]).strip() if name_col else code
                    out.append([code, name])
    except Exception as e:
        print(f"[scanner] 获取中证A500成分股失败：{e}")

    if out:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "list": out}, f, ensure_ascii=False)
        except Exception:
            pass
    return out


def default_symbols(include_a500=True):
    """默认监听标的：宽基 + 行业 + (可选)中证A500成分股。
    返回 [[symbol, name, category], ...]"""
    syms = [[c, n, "broad"] for c, n in BROAD_INDICES]
    syms += [[c, n, "industry"] for c, n in INDUSTRY_INDICES]
    if include_a500:
        syms += [[c, n, "a500"] for c, n in get_a500_constituents()]
    return syms


# ===== 估值与建议仓位 =====
def _pe_level(p):
    if p is None:
        return "未知"
    if p < 20:
        return "极低估"
    if p < 40:
        return "偏低"
    if p < 60:
        return "中性"
    if p < 80:
        return "偏高"
    return "高估"


def _suggest_position(pe_info, has_entry, has_exit):
    """建议参考仓位。
    有PE：按近5年分位线性映射到 [30%, 100%]（越便宜仓位越高）。
    无PE：按入场/清仓信号方向给定性建议。"""
    if pe_info and pe_info.get("percentile") is not None:
        p = pe_info["percentile"]
        frac = 1.0 - 0.7 * (p / 100.0)          # 分位0%→100% ; 分位100%→30%
        base = max(0.3, min(1.0, frac))
        return {"value": round(base * 100), "text": f"{round(base * 100)}%（按PE近5年{p}%分位估算）"}
    if has_exit and not has_entry:
        return {"value": None, "text": "偏空 · 建议降仓/观望（出现清仓信号）"}
    if has_entry and not has_exit:
        return {"value": None, "text": "偏多 · 可逢低参与（出现入场信号）"}
    if has_entry and has_exit:
        return {"value": None, "text": "震荡 · 信号交织，控制仓位"}
    return {"value": None, "text": "—（无估值数据，参考信号方向）"}


def _score_level(score):
    """推荐买入分 -> 文字档位。"""
    if score >= 75:
        return "强烈推荐"
    if score >= 60:
        return "偏多关注"
    if score >= 45:
        return "中性"
    if score >= 30:
        return "偏空谨慎"
    return "建议回避"


def _recommend_score(entries, exits, pe_info, lookback_days):
    """综合「推荐买入」打分（0~100，基准 50 中性）。

    加分项(看多)：有买入信号、买入信号越多、买入信号距今越近、估值分位越低。
    减分项(看空)：有卖出信号、卖出信号越多、卖出信号距今越近、估值分位越高、PE绝对值过高(>30)。

    返回 {score, level, buy, sell, valuation, pe_penalty} —— 各部分贡献(便于展示/排序)。
    """
    lb = max(1, int(lookback_days or 10))

    # 时间近度权重：今日触发=1.0，越久越小，窗口末端约 0.3
    def _tw(days_ago):
        d = min(max(int(days_ago or 0), 0), lb)
        return max(0.3, 1.0 - 0.7 * (d / lb))

    # 单个信号基准 12 分（含时间衰减），多个累加，各自封顶 ±50
    buy_raw = sum(_tw(s.get("days_ago", lb)) for s in (entries or []))
    sell_raw = sum(_tw(s.get("days_ago", lb)) for s in (exits or []))
    buy_score = round(min(buy_raw * 12.0, 50.0), 1)
    sell_score = round(min(sell_raw * 12.0, 50.0), 1)

    # 估值贡献 = 分位项 + PE绝对值惩罚。
    # ① 分位项：近5年分位线性映射到 [-20, +20]，分位 0%→+20，100%→-20。
    #    无长历史分位(中证A500/无PE)记 0，不影响信号方向。
    # ② PE绝对值惩罚：PE 超过 30 后，每多 10 PE 扣 1 分（线性，封顶 -15）。
    #    哪怕分位很低，PE 本身过高(如 30/40/50)也要降温，避免"贵但便宜"假象。
    pct_score = 0.0
    if pe_info and pe_info.get("percentile") is not None:
        p = float(pe_info["percentile"])
        pct_score = round((50.0 - p) / 50.0 * 20.0, 1)

    pe_penalty = 0.0
    if pe_info and pe_info.get("pe") is not None:
        try:
            pe_abs = float(pe_info["pe"])
            if pe_abs > 30:
                pe_penalty = round(min((pe_abs - 30.0) / 10.0, 15.0), 1)
        except (TypeError, ValueError):
            pe_penalty = 0.0

    val_score = round(pct_score - pe_penalty, 1)

    raw = 50.0 + buy_score - sell_score + val_score
    score = int(max(0, min(100, round(raw))))
    return {
        "score": score,
        "level": _score_level(score),
        "buy": buy_score,
        "sell": sell_score,
        "valuation": val_score,
        "pe_penalty": pe_penalty,
    }


def _resolve_pe_info(symbol, dates, today, n):
    """取标的「当前估值」(PE 及分位)：
      指数(乐咕指数PE / 交易所市场平均PE / 中证A500乐咕全历史) -> 分位；
      中证A500 取乐咕失败时 -> 中证官方当前 TTM PE(无分位)兜底；
      个股(A500成分股等) -> 百度 TTM PE 近5年分位。
    返回 {pe, percentile, level} 或 None。"""
    low = symbol.lower()
    pe_start = (datetime.now() - timedelta(days=365 * 6)).strftime("%Y-%m-%d")

    def _from_series(loader):
        pe_df = loader(symbol, pe_start, today)
        if pe_df is None or pe_df.empty:
            return None
        pe_series = dict(zip(pe_df["date"], pe_df["pe"]))
        pe_aligned, pcts = _trailing_pe_percentiles(dates, pe_series)
        for k in range(n - 1, -1, -1):
            if pe_aligned[k] is not None:
                return {"pe": round(pe_aligned[k], 2), "percentile": pcts[k],
                        "level": _pe_level(pcts[k])}
        return None

    # 1) 指数：乐咕指数PE / 交易所市场平均PE / 中证A500(乐咕全历史) -> 分位
    #    A500 自 2024-09 发布，分位口径为「发布至今」(窗口内全部样本)，仍可用于估值高低判断。
    if low in INDEX_LEGU_CSI or low in INDEX_PE_NAME or low in INDEX_MARKET_PE:
        try:
            r = _from_series(load_pe)
            if r is not None:
                return r
        except Exception as e:
            print(f"[scanner] {symbol} 指数PE取数失败：{e}")

    # 2) 中证A500 兜底：乐咕取数失败时，用中证官方当前 TTM PE(无分位)
    try:
        cur = load_index_current_pe(symbol)
    except Exception:
        cur = None
    if cur is not None:
        return {"pe": round(cur, 2), "percentile": None, "level": "—"}

    # 3) 个股(A500成分股等)：百度 TTM PE 近5年分位
    if symbol.isdigit():
        try:
            return _from_series(load_stock_pe)
        except Exception as e:
            print(f"[scanner] {symbol} 个股PE取数失败：{e}")
    return None


def scan_symbol(symbol, name="", category="", lookback_days=10,
                adjust="qfq", high_window=60):
    """扫描单个标的，返回信号/估值/建议仓位 dict。
    lookback_days: 只报告最近这么多个交易日内触发的信号。"""
    symbol = str(symbol).strip()
    today = datetime.now().strftime("%Y-%m-%d")
    # 取约 640 自然日历史，保证 MA200 等长周期指标可计算
    start = (datetime.now() - timedelta(days=640)).strftime("%Y-%m-%d")

    ck = _scan_cache_key(symbol, lookback_days, adjust, high_window)

    # ① 当日缓存命中：同一自然日内已扫过 -> 直接返回，连 load_kline 都不调用(0网络0计算)。
    cached = _scan_cache_get_fresh(ck, today)
    if cached is not None:
        out = dict(cached)
        out["name"] = name or out.get("name", "")
        out["category"] = category or out.get("category", "")
        out["cached"] = True
        return out

    try:
        df = load_kline(symbol, start, today, adjust=adjust)
    except Exception as e:
        return {"symbol": symbol, "name": name, "category": category, "error": f"取数失败:{e}"}
    if df is None or df.empty or len(df) < 30:
        return {"symbol": symbol, "name": name, "category": category, "error": "数据不足"}

    last_date = str(df["date"].iloc[-1])
    dates = df["date"].tolist()
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    vols = df["volume"].tolist() if "volume" in df.columns else [0] * len(df)
    n = len(df)
    ctx = {"n": n, "opens": df["open"].tolist(), "closes": closes,
           "highs": highs, "lows": lows, "vols": vols}

    cut = max(1, n - lookback_days)   # 仅看最近 lookback_days 个交易日

    def latest_rise(state):
        """最近一次"上升沿(由False变True)"的索引；近窗口内无则 None。"""
        for i in range(n - 1, cut - 1, -1):
            if state[i] and not state[i - 1]:
                return i
        return None

    entry_hits, exit_hits = [], []

    # ---- 5 个入场指标 ----
    for spec in ENTRY_DEFAULTS:
        st = _entry_state(spec, ctx)
        i = latest_rise(st)
        if i is not None:
            entry_hits.append({"type": spec["type"], "label": _entry_label(spec),
                               "date": dates[i], "days_ago": n - 1 - i})

    # ---- 清仓：静态 3 个 ----
    for spec in EXIT_DEFAULTS:
        if spec["type"] not in ("ma_break", "donchian_exit", "ma_death_cross"):
            continue
        arr = _static_exit_state(spec, ctx)
        i = latest_rise(arr)
        if i is not None:
            exit_hits.append({"type": spec["type"], "label": _exit_label(spec, ctx, i),
                              "date": dates[i], "days_ago": n - 1 - i})

    # ---- 清仓：吊灯ATR（基于近 high_window 日滚动最高价）----
    chan = next(s for s in EXIT_DEFAULTS if s["type"] == "chandelier_atr")
    atr_arr = _atr(highs, lows, closes, int(chan.get("atr_period", 22)))
    mult = float(chan.get("mult", 3))
    chan_state = [False] * n
    for i in range(n):
        a = atr_arr[i]
        if a is not None:
            hh = max(highs[max(0, i - high_window + 1):i + 1])
            chan_state[i] = closes[i] < hh - mult * a
    i = latest_rise(chan_state)
    if i is not None:
        exit_hits.append({"type": "chandelier_atr", "label": _exit_label(chan, ctx, i),
                          "date": dates[i], "days_ago": n - 1 - i})

    # ---- 清仓：移动止盈（基于近 high_window 日滚动最高收盘）----
    trail = next(s for s in EXIT_DEFAULTS if s["type"] == "trailing_pct")
    pct = float(trail.get("pct", 10)) / 100.0
    trail_state = [False] * n
    for i in range(n):
        hc = max(closes[max(0, i - high_window + 1):i + 1])
        trail_state[i] = closes[i] < hc * (1 - pct)
    i = latest_rise(trail_state)
    if i is not None:
        exit_hits.append({"type": "trailing_pct", "label": _exit_label(trail, ctx, i),
                          "date": dates[i], "days_ago": n - 1 - i})

    # ---- 当前估值(PE 近5年分位) ----
    pe_info = _resolve_pe_info(symbol, dates, today, n)

    suggest = _suggest_position(pe_info, bool(entry_hits), bool(exit_hits))

    # 按触发时间从近到远排序
    entry_hits.sort(key=lambda x: x["days_ago"])
    exit_hits.sort(key=lambda x: x["days_ago"])

    # 综合「推荐买入」打分(0~100)
    score = _recommend_score(entry_hits, exit_hits, pe_info, lookback_days)

    result = {
        "symbol": symbol, "name": name, "category": category,
        "last_date": dates[-1], "last_close": round(closes[-1], 3),
        "pe": pe_info, "suggest": suggest, "score": score,
        "entries": entry_hits, "exits": exit_hits,
        "has_signal": bool(entry_hits or exit_hits),
    }
    # 写入扫描结果缓存(记录最新交易日 + 写入当天)，供同一自然日内复用
    _scan_cache_put(ck, last_date, today, result)
    return result
