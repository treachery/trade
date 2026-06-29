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
import math
import threading
from datetime import datetime, timedelta

from .data import (load_kline, load_pe, load_stock_pe, load_index_current_pe,
                   load_hk_pe, load_valuation_pe, market_of,
                   INDEX_PE_NAME, INDEX_MARKET_PE, INDEX_LEGU_CSI, CACHE_DIR)
from .strategy import ENTRY_DEFAULTS, EXIT_DEFAULTS
from .engine import (_entry_state, _static_exit_state, _atr,
                     _trailing_pe_percentiles, _entry_label, _exit_label)


# ===== 扫描结果缓存（按15:00分界失效）=====
# 同一标的、同一参数(lookback/adjust/high_window)，扫描结果在每个15:00分界区间内只算一次。
# 盘前(00:00~14:59)和盘后(15:00~23:59)各为一个区间，跨15:00自动失效（收盘数据更新）。落地 json，重启不丢。
_SCAN_CACHE_PATH = os.path.join(CACHE_DIR, "scan_results.json")
# 扫描推荐值/打分逻辑变更时提升版本，避免复用旧扫描结果。
SCAN_SCORE_VERSION = "2026-06-29-strat-link-v1"
_scan_cache_lock = threading.Lock()
_scan_cache = None   # 内存镜像：{key: {"last_date":..., "result":...}}


def _scan_cache_key(symbol, lookback_days, adjust, high_window):
    return f"{SCAN_SCORE_VERSION}|{symbol}|{lookback_days}|{adjust}|{high_window}"


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


def _cache_slot(ts):
    """把时间戳归到15:00分界区间：当天<15:00归昨天，≥15:00归今天。
    盘前/盘后各算一个独立区间，15:00后收盘数据更新会自动失效盘前缓存。"""
    dt = datetime.fromtimestamp(ts)
    if dt.hour < 15:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _scan_cache_get_fresh(key, today_str):
    """缓存命中：当前时间与缓存时间在同一个15:00分界区间内。
    盘前(00:00~14:59)为一个区间，盘后(15:00~23:59)为另一个，跨15:00自动失效重算。"""
    cache = _load_scan_cache()
    with _scan_cache_lock:
        item = cache.get(key)
    if item and _cache_slot(time.time()) == _cache_slot(item.get("ts", 0)):
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

# ===== 港股 / 美股指数 =====
HK_INDICES = [
    ("hkHSI", "恒生指数"),
    ("hkHSTECH", "恒生科技指数"),
    ("hkHSCEI", "恒生国企指数"),
]
US_INDICES = [
    ("us.SPX", "标普500"),
    ("us.NDX", "纳斯达克100"),
]
# 港股个股池：流通市值阈值(港币)。1000 亿 = 1e11。
HK_MIN_MKTCAP = 1e11
# A500 成分股：流通市值阈值(人民币)。500 亿 = 5e10。
A500_MIN_MKTCAP = 5e10
# 标普500 成分股：流通市值阈值(美元)。500 亿 = 5e10。
SP500_MIN_MKTCAP = 5e10


def get_sp500_constituents(use_cache=True, max_age_days=7):
    """获取标普500成分股 [[us代码, 名称], ...]，代码为 us+东财格式(如 us105.AAPL)。
    在线抓取(维基/GitHub双源)拿到纯字母 symbol，再用 akshare 美股 spot 映射成东财代码。
    本地缓存 7 天，失败返回空列表。
    """
    path = os.path.join(CACHE_DIR, "sp500_cons.json")
    if use_cache and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - d.get("ts", 0) < max_age_days * 86400 and d.get("list"):
                return d["list"]
        except Exception:
            pass

    import pandas as pd
    import requests
    import io
    H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
    # 1) 拿纯字母 symbol + 名称(双源兜底)
    pairs = []   # [(SYMBOL, name), ...]
    try:
        r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                         headers=H, timeout=20)
        tbl = pd.read_html(io.StringIO(r.text), header=0)[0]
        for _, row in tbl.iterrows():
            sym = str(row.get("Symbol", "")).strip().replace(".", "-")
            nm = str(row.get("Security", "")).strip()
            if sym:
                pairs.append((sym, nm))
    except Exception as e:
        print(f"[scanner] 维基标普500抓取失败，尝试备用源：{e}")
    if not pairs:
        try:
            r = requests.get("https://raw.githubusercontent.com/datasets/"
                             "s-and-p-500-companies/main/data/constituents.csv",
                             headers=H, timeout=20)
            tbl = pd.read_csv(io.StringIO(r.text))
            for _, row in tbl.iterrows():
                sym = str(row.get("Symbol", "")).strip()
                nm = str(row.get("Security", "")).strip()
                if sym:
                    pairs.append((sym, nm))
        except Exception as e:
            print(f"[scanner] GitHub标普500抓取失败：{e}")

    if not pairs:
        return []

    # 2) 用 akshare 美股 spot 把纯字母 symbol 映射成东财代码(105.AAPL)
    out = []
    try:
        import akshare as ak
        spot = ak.stock_us_spot_em()
        # 东财代码形如 105.AAPL，截取 . 后的纯符号建索引
        sym2full = {}
        sym2mc = {}
        mc_col = next((c for c in spot.columns if "流通市值" in str(c)), None)
        for _, r in spot.iterrows():
            full = str(r["代码"]).strip()
            pure = full.split(".")[-1].upper()
            sym2full.setdefault(pure, (full, str(r["名称"]).strip()))
            if mc_col:
                try:
                    sym2mc[pure] = float(r.get(mc_col) or 0)
                except (TypeError, ValueError):
                    pass
        for sym, nm in pairs:
            hit = sym2full.get(sym.upper())
            if hit and (not mc_col or sym2mc.get(sym.upper(), 0) >= SP500_MIN_MKTCAP):
                out.append([f"us{hit[0]}", nm or hit[1]])
        # 容错：市值过滤后过少(可能是单位/接口问题)，回退到不过滤
        if mc_col and len(out) < 50 and len(pairs) > 50:
            print(f"[scanner] 标普500市值过滤后仅 {len(out)} 只，回退到不过滤")
            out = [[f"us{sym2full[s.upper()][0]}", nm or sym2full[s.upper()][1]]
                   for s, nm in pairs if s.upper() in sym2full]
    except Exception as e:
        print(f"[scanner] 标普500代码映射失败：{e}")

    # 降级：spot 映射失败时用纯字母代码(us.AAPL)，yfinance 可直接处理
    if not out and pairs:
        print("[scanner] 标普500降级为纯字母代码(跳过东财映射/市值过滤)")
        out = [[f"us.{sym}", nm] for sym, nm in pairs]

    if out:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "list": out}, f, ensure_ascii=False)
        except Exception:
            pass
    return out


def get_hk_large_caps(use_cache=True, max_age_days=7, min_mktcap=HK_MIN_MKTCAP):
    """获取流通市值≥min_mktcap(港币)的港股 [[hk代码, 名称], ...]，代码为 hk+5位(如 hk00700)。
    用东财港股列表 API(f21=流通市值)筛选。本地缓存 7 天，失败返回空列表。
    """
    path = os.path.join(CACHE_DIR, "hk_largecap_cons.json")
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
        import requests
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "500", "po": "1", "np": "1", "fltt": "2", "invt": "2",
            "fs": "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2",  # 港股主板各板块
            "fields": "f12,f14,f21", "fid": "f21",            # f21=流通市值，按其降序
        }
        r = requests.get(url, params=params, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        data = (r.json() or {}).get("data") or {}
        for x in (data.get("diff") or []):
            code = str(x.get("f12", "")).strip()
            name = str(x.get("f14", "")).strip()
            mcap = x.get("f21")
            try:
                mcap = float(mcap)
            except (TypeError, ValueError):
                continue
            if code and mcap >= min_mktcap:
                out.append([f"hk{code}", name or code])
            elif mcap < min_mktcap:
                # 已按 f21 降序，遇到第一个不达标即可停止
                break
    except Exception as e:
        print(f"[scanner] 获取港股大市值成分失败：{e}")

    if out:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "list": out}, f, ensure_ascii=False)
        except Exception:
            pass
    return out


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

        # 流通市值过滤 + 名称补全（用 A股 spot 行情）
        if out:
            try:
                spot = ak.stock_zh_a_spot_em()
                mc_col = next((c for c in spot.columns if "流通市值" in str(c)), None)
                nm_col = next((c for c in spot.columns if "名称" in str(c) and "指数" not in str(c)), None)
                mc_map, nm_map = {}, {}
                for _, r in spot.iterrows():
                    code = str(r.get("代码", "")).strip().zfill(6)
                    if mc_col:
                        try: mc_map[code] = float(r.get(mc_col) or 0)
                        except (TypeError, ValueError): pass
                    if nm_col:
                        nm_map[code] = str(r.get(nm_col, "")).strip()
                # 名称补全：csindex 接口缺名称(或名称=代码)的用 spot 数据补
                out = [[c, (n if n and n != c else nm_map.get(c, c))] for c, n in out]
                # 流通市值过滤
                if mc_col:
                    filtered = [[c, n] for c, n in out if mc_map.get(c, 0) >= A500_MIN_MKTCAP]
                    # 容错：过滤后过少(可能是单位/接口问题)，回退到不过滤
                    if len(filtered) >= 50 or len(out) < 50:
                        out = filtered
                    else:
                        print(f"[scanner] A500市值过滤后仅 {len(filtered)} 只，回退到不过滤")
            except Exception as e:
                print(f"[scanner] A500流通市值过滤失败：{e}")
    except Exception as e:
        print(f"[scanner] 获取中证A500成分股失败：{e}")

    if out:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "list": out}, f, ensure_ascii=False)
        except Exception:
            pass
    return out


def default_symbols(include_a500=True, include_sp500=False, include_hk_large=False):
    """默认监听标的：A股宽基 + (可选)中证A500成分股 + (可选)港美指数/成分股。
    返回 [[symbol, name, category], ...]
      category: broad(A股宽基) / a500(A500成分) /
                hk_index / hk(港股成分) / us_index / sp500(标普成分)
    """
    syms = [[c, n, "broad"] for c, n in BROAD_INDICES]
    syms += [[c, n, "industry"] for c, n in INDUSTRY_INDICES]
    # 港美指数默认纳入(轻量，5个)
    syms += [[c, n, "hk_index"] for c, n in HK_INDICES]
    syms += [[c, n, "us_index"] for c, n in US_INDICES]
    if include_a500:
        syms += [[c, n, "a500"] for c, n in get_a500_constituents()]
    if include_sp500:
        syms += [[c, n, "sp500"] for c, n in get_sp500_constituents()]
    if include_hk_large:
        syms += [[c, n, "hk"] for c, n in get_hk_large_caps()]
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
    """推荐值 -> 文字档位。零轴制：>0 推荐买入，<0 建议卖出。"""
    if score > 20:
        return "强烈推荐买入"
    if score > 0:
        return "推荐买入"
    if score < -20:
        return "强烈建议卖出"
    if score < 0:
        return "建议卖出"
    return "中性观望"


def _recommend_score(entries, exits, pe_info, lookback_days):
    """综合「推荐值」打分（零轴制，>0 推荐买入，<0 建议卖出）。

    交易分数：只买入=正分，只卖出=负分，买卖信号同时出现=0（价格行为混乱，不做方向判断）。
    估值分数：分位越低越加分，分位越高/PE绝对值过高越减分。

    返回 {score, level, trade, valuation, buy, sell, conflict, valuation_pct, valuation_abs} —— 各部分贡献(便于展示/排序)。
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

    # 估值分数：分数越高代表估值越便宜，最终区间 [-12.5, 12.5]（整体权重减半）。
    # PE5YPercentile 占 80% 权重，PE 绝对值占 20% 权重。
    # - 分位项：0% -> +25，50% -> 0，100% -> -25。
    # - PE项：PE=20 -> 0；PE越低越高，越高越低，限制在 [-25, 25]；PE<=0 记为 -10。
    # - 两项都有：0.8 * 分位项 + 0.2 * PE项；只有 PE 时使用 PE项；无 PE 时为 0。
    # - 估值分最终 ×0.5（交易分不变），降低估值对总推荐值的影响。
    def _clamp(v, lo=-25.0, hi=25.0):
        return max(lo, min(hi, v))

    pct_score = None
    pe_score = None
    if pe_info and pe_info.get("percentile") is not None:
        p = float(pe_info["percentile"])
        pct_score = _clamp((50.0 - p) / 2.0)

    if pe_info and pe_info.get("pe") is not None:
        try:
            pe_abs = float(pe_info["pe"])
            pe_score = -10.0 if pe_abs <= 0 else _clamp(-25.0 * math.log10(pe_abs / 20.0))
        except (TypeError, ValueError):
            pe_score = None

    if pct_score is not None and pe_score is not None:
        val_score = round((0.8 * pct_score + 0.2 * pe_score) * 0.5, 1)
    elif pct_score is not None:
        val_score = round(pct_score * 0.5, 1)
    elif pe_score is not None:
        val_score = round(pe_score * 0.5, 1)
    else:
        val_score = 0.0

    has_buy = bool(entries)
    has_sell = bool(exits)
    conflict = has_buy and has_sell
    if conflict:
        trade_score = 0.0
    elif has_buy:
        trade_score = buy_score
    elif has_sell:
        trade_score = -sell_score
    else:
        trade_score = 0.0

    score = round(trade_score + val_score, 1)
    return {
        "score": score,
        "level": _score_level(score),
        "trade": round(trade_score, 1),
        "valuation": val_score,
        "buy": buy_score,
        "sell": sell_score,
        "conflict": conflict,
        "valuation_pct": round(pct_score, 2) if pct_score is not None else None,
        "valuation_pe": round(pe_score, 2) if pe_score is not None else None,
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

    mkt = market_of(symbol)

    # 3) 港股个股：百度港股 TTM PE -> 近若干年分位
    if mkt == "hk":
        body = symbol[2:] if low.startswith("hk") else symbol
        if body.isdigit():
            try:
                return _from_series(load_hk_pe)
            except Exception as e:
                print(f"[scanner] {symbol} 港股PE取数失败：{e}")
        return None

    # 4) 美股：统一估值入口。
    # 美股个股走 moomoo/Futu 估值接口；美股指数(us.SPX/us.NDX)走 WorldPERatio。
    # 注意：scan_symbol 只会在有买卖信号时调用本函数，避免 500+ 成分股全量估值拖慢扫描。
    if mkt == "us":
        try:
            return _from_series(load_valuation_pe)
        except Exception as e:
            print(f"[scanner] {symbol} 美股PE取数失败：{e}")
        return None

    # 5) A股个股(A500成分股等)：百度 TTM PE 近5年分位
    if symbol.isdigit():
        try:
            return _from_series(load_stock_pe)
        except Exception as e:
            print(f"[scanner] {symbol} 个股PE取数失败：{e}")
    return None


# 新浪行情接口查 A股名称（轻量、稳定），用于补全持仓等只传代码不传名称的标的
_sina_name_cache = {}

def _sina_name(symbol):
    """用新浪行情接口查单只 A股名称。失败返回 None。结果缓存。"""
    if not symbol or not symbol.isdigit() or len(symbol) != 6:
        return None
    if symbol in _sina_name_cache:
        return _sina_name_cache[symbol]
    prefix = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
    try:
        import requests as _req
        r = _req.get(f"http://hq.sinajs.cn/list={prefix}{symbol}",
                     timeout=5, headers={"Referer": "http://finance.sina.com.cn"})
        r.encoding = "gbk"
        val = r.text.split('"')[1] if '"' in r.text else ""
        nm = val.split(",")[0] if val else None
        if nm:
            _sina_name_cache[symbol] = nm
        return nm
    except Exception:
        return None


def scan_symbol(symbol, name="", category="", lookback_days=10,
                adjust="qfq", high_window=60, use_cache=True):
    """扫描单个标的，返回信号/估值/建议仓位 dict。
    lookback_days: 只报告最近这么多个交易日内触发的信号。"""
    symbol = str(symbol).strip()
    # 名称补全：名称为空或等于代码时(如持仓只传代码)，用新浪行情接口查名称
    if not name or name == symbol:
        _nm = _sina_name(symbol)
        if _nm:
            name = _nm
    today = datetime.now().strftime("%Y-%m-%d")
    # 取约 640 自然日历史，保证 MA200 等长周期指标可计算
    start = (datetime.now() - timedelta(days=640)).strftime("%Y-%m-%d")

    ck = _scan_cache_key(symbol, lookback_days, adjust, high_window)

    # ① 缓存命中(use_cache=True时)：同一15:00分界区间内已扫过 -> 直接返回。
    if use_cache:
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

    has_signal = bool(entry_hits or exit_hits)

    # ---- 当前估值(PE 近5年分位) ----
    # 扫描列表只展示有买卖信号的标的；无信号标的不计算估值，避免 500+ 成分股扫描时被 PE 接口拖慢。
    pe_info = _resolve_pe_info(symbol, dates, today, n) if has_signal else None

    suggest = _suggest_position(pe_info, bool(entry_hits), bool(exit_hits))

    # 按触发时间从近到远排序
    entry_hits.sort(key=lambda x: x["days_ago"])
    exit_hits.sort(key=lambda x: x["days_ago"])

    # 综合「推荐值」打分（零轴制）
    score = _recommend_score(entry_hits, exit_hits, pe_info, lookback_days)

    result = {
        "symbol": symbol, "name": name, "category": category,
        "last_date": dates[-1], "last_close": round(closes[-1], 3),
        "pe": pe_info, "suggest": suggest, "score": score,
        "entries": entry_hits, "exits": exit_hits,
        "has_signal": has_signal,
    }
    # 写入扫描结果缓存(记录最新交易日 + 写入当天)，供同一自然日内复用
    _scan_cache_put(ck, last_date, today, result)
    return result
