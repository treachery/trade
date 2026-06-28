"""数据层：通过 akshare 获取 A 股日线数据，并做本地 CSV 缓存。

akshare 免费、无需 token，数据来源为东方财富。
返回标准化后的列：date / open / close / high / low / volume / pct_chg
"""
import os
import time
from datetime import datetime

import pandas as pd


def _with_retry(fn, attempts: int = 4, delay: float = 1.5):
    """对偶发网络错误(如东财SSL中断)做重试。"""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _parse_date(s: str) -> datetime:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d")

# akshare 返回的中文列 -> 英文标准列
COL_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "涨跌幅": "pct_chg",   # 当日涨跌幅(%)，相对前一交易日收盘
}

NUMERIC_COLS = ["open", "close", "high", "low", "volume", "pct_chg"]


def _cache_path(symbol: str, adjust: str) -> str:
    adj = adjust or "none"
    return os.path.join(CACHE_DIR, f"{symbol}_{adj}.csv")


def _fetch(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """从 akshare 拉取原始数据并标准化。start/end 形如 2015-03-01。

    代码带 sh/sz 前缀(如 sh000001 上证指数) -> 走指数接口(返回全历史)；
    否则按 6 位代码走东财个股接口(个股，以及 399001 等深市指数)。
    """
    import akshare as ak  # 延迟导入

    code = str(symbol).strip()
    low = code.lower()

    if low.startswith("sh") or low.startswith("sz"):
        # 指数：一次取全历史(很快)，由上层缓存并切片
        df = _with_retry(lambda: ak.stock_zh_index_daily_em(symbol=low))
        if df is None or df.empty:
            return pd.DataFrame()
        keep = ["date", "open", "close", "high", "low", "volume"]
        df = df[[c for c in keep if c in df.columns]].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for c in ["open", "close", "high", "low", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    s = start.replace("-", "")
    e = end.replace("-", "")
    df = _with_retry(lambda: ak.stock_zh_a_hist(
        symbol=code, period="daily", start_date=s, end_date=e, adjust=adjust or "",
    ))
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=COL_MAP)
    keep = ["date"] + NUMERIC_COLS
    df = df[[c for c in keep if c in df.columns]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for c in NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_kline(symbol: str, start: str, end: str,
               adjust: str = "qfq", use_cache: bool = True) -> pd.DataFrame:
    """加载某只股票在 [start, end] 区间的日线数据。

    symbol: 6 位代码，如 '000001'（平安银行）、'600519'（贵州茅台）
    adjust: 'qfq' 前复权 / 'hfq' 后复权 / '' 不复权
    """
    symbol = str(symbol).strip()
    path = _cache_path(symbol, adjust)

    cache = pd.DataFrame()
    if use_cache and os.path.exists(path):
        try:
            cache = pd.read_csv(path, dtype={"date": str})
        except Exception:
            cache = pd.DataFrame()

    # 判断是否需要联网：
    #  1) 请求区间比缓存更早(想要更早的历史) -> 需要补；
    #  2) 缓存最新日期距"今天"≥2 个自然日(说明可能已有新交易日) -> 增量更新到最新；
    #     用"距今天"而非"距请求结束日"，这样每天/每周打开只要有新K线就会自动拉。
    #  非交易日/当天未收盘时拉不到新数据也无妨(下方失败回退缓存)，不会反复空转报错。
    need_fetch = True
    fetch_start, fetch_end = start, end
    if not cache.empty:
        cmin, cmax = cache["date"].min(), cache["date"].max()
        try:
            today = datetime.now()
            today_str = today.strftime("%Y-%m-%d")
            gap_start = (_parse_date(start) - _parse_date(cmin)).days  # >0 表示想要比缓存更早的数据
            stale_days = (today - _parse_date(cmax)).days             # 缓存最新日期距今天的自然日数
            want_earlier = gap_start > 10           # 想要的起始比缓存最早还早(超出容差)
            has_new_tradeday = stale_days >= 2       # 缓存落后今天≥2天，可能有新交易日
            if not want_earlier and not has_new_tradeday:
                need_fetch = False
            else:
                # 增量更新：拉取范围覆盖到今天；若只是补最新(不需更早历史)，只取缓存末尾之后的增量段
                fetch_end = today_str if has_new_tradeday else end
                if not want_earlier:
                    fetch_start = cmax  # 从缓存最新日开始(含重叠一天，dedup 去重)
        except Exception:
            need_fetch = not (cmin <= start and cmax >= end)

    if need_fetch:
        try:
            fetched = _fetch(symbol, fetch_start, fetch_end, adjust)
        except Exception as e:
            # 联网失败：若有缓存则回退使用缓存，否则向上抛出
            if cache.empty:
                raise
            print(f"[data] 获取 {symbol} 失败，回退本地缓存：{e}")
            fetched = pd.DataFrame()
        if not fetched.empty:
            if not cache.empty:
                cache = pd.concat([cache, fetched], ignore_index=True)
                cache = cache.drop_duplicates(subset=["date"]).sort_values("date")
            else:
                cache = fetched
            cache.to_csv(path, index=False)

    if cache.empty:
        return cache

    mask = (cache["date"] >= start) & (cache["date"] <= end)
    out = cache.loc[mask].sort_values("date").reset_index(drop=True)
    return out


# 指数代码 -> 乐咕(legulegu)指数估值名称（用于取 PE 历史）
# 注意：stock_index_pe_lg 仅支持以下有限名称(均有长历史)：
#   上证50 / 沪深300 / 上证180 / 上证380 / 中证500 / 中证1000 / 创业板50
# “上证指数 / 深证成指 / 创业板指 / 科创50 / 中小板指” 该接口不提供。
INDEX_PE_NAME = {
    "sh000300": "沪深300",
    "sh000016": "上证50",
    "sh000010": "上证180",
    "sh000009": "上证380",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sz399673": "创业板50",
    # 创业板指无直接PE源，用“创业板50”作为高度相关的代理
    "sz399006": "创业板50",
}
# 使用代理PE的标的(用于在界面提示)
INDEX_PE_PROXY = {
    "sz399006": "创业板50",
}
# 综合指数 -> 乐咕"交易所市场平均市盈率"(stock_market_pe_lg)，月度长历史，
# 作为上证指数 / 深证成指 的估值参照(口径=交易所全市场算术平均PE)。
INDEX_MARKET_PE = {
    "sh000001": "上证",
    "sz399001": "深证",
}
# 乐咕「指数滚动市盈率」可取长历史，但 akshare 内置名单不含部分新指数。
# 这里直接用乐咕 indexCode(后缀 .CSI/.SH/.SZ) 取全历史 TTM PE，可算「发布至今分位」。
# 中证A500 于 2024-09 发布，乐咕 indexCode = 000510.CSI（注意是 .CSI 不是 .SH）。
INDEX_LEGU_CSI = {
    "sh000510": "000510.CSI",   # 中证A500（自发布日起的全历史 PE）
}
# 仅能取「当前 TTM PE」的指数(中证官方 stock_zh_index_value_csindex，无长历史分位)。
# 作为 INDEX_LEGU_CSI 取数失败时的兜底。value 为 csindex 的 6 位指数代码。
INDEX_CSINDEX_PE = {
    "sh000510": "000510",   # 中证A500
}


def _legu_index_pe_by_code(index_code: str) -> pd.DataFrame:
    """直接调乐咕「指数滚动市盈率」API 取任意指数全历史 PE(date/pe)。

    复用 akshare.stock_index_pe_lg 内部的 token 生成与反爬 cookie 机制，
    仅把 indexCode 换成传入值(如 000510.CSI)，绕开其内置 symbol 名单限制。
    返回列 date / pe(滚动市盈率 ttmPe)，失败返回空 DataFrame。
    """
    try:
        import requests
        import akshare as ak
        g = ak.stock_index_pe_lg.__globals__
        hash_code = g["hash_code"]
        get_cookie_csrf = g["get_cookie_csrf"]
        mr = g["py_mini_racer"]
        js = mr.MiniRacer()
        js.eval(hash_code)
        token = js.call("hex", datetime.now().date().isoformat()).lower()
        url = "https://legulegu.com/api/stockdata/index-basic-pe"
        r = _with_retry(lambda: requests.get(
            url, params={"token": token, "indexCode": index_code},
            timeout=15,
            **get_cookie_csrf(url="https://legulegu.com/stockdata/sz50-ttm-lyr")))
        data = (r.json() or {}).get("data") or []
        if not data:
            return pd.DataFrame()
        raw = pd.DataFrame(data)
        col = "ttmPe" if "ttmPe" in raw.columns else (
            "addTtmPe" if "addTtmPe" in raw.columns else None)
        if col is None or "date" not in raw.columns:
            return pd.DataFrame()
        out = pd.DataFrame({
            "date": pd.to_datetime(raw["date"], utc=True)
                      .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"),
            "pe": pd.to_numeric(raw[col], errors="coerce"),
        })
        return out.dropna(subset=["pe"]).reset_index(drop=True)
    except Exception as e:
        print(f"[data] 乐咕指数PE({index_code})取数失败：{e}")
        return pd.DataFrame()


def load_pe(symbol: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
    """加载指数的 PE 历史，返回列 date / pe。

    支持三类长历史来源：
      - INDEX_LEGU_CSI：乐咕指数滚动市盈率(按 indexCode 直取，如中证A500 000510.CSI)，
        覆盖 akshare 内置名单未含的新指数，可算「发布至今分位」；
      - INDEX_PE_NAME：乐咕指数滚动市盈率(stock_index_pe_lg)，日度长历史；
      - INDEX_MARKET_PE：乐咕交易所市场平均市盈率(stock_market_pe_lg)，月度长历史，
        作为上证指数 / 深证成指 的估值参照。
    其它(个股/不支持)返回空 DataFrame。
    """
    symbol = str(symbol).strip().lower()
    legu_code = INDEX_LEGU_CSI.get(symbol)
    name = INDEX_PE_NAME.get(symbol)
    market = INDEX_MARKET_PE.get(symbol)
    if not legu_code and not name and not market:
        return pd.DataFrame()

    path = os.path.join(CACHE_DIR, f"pe_{symbol}.csv")
    cache = pd.DataFrame()
    if use_cache and os.path.exists(path):
        try:
            cache = pd.read_csv(path, dtype={"date": str})
        except Exception:
            cache = pd.DataFrame()

    # 缓存最新日期距今天 ≥2 个自然日就刷新；接口每次返回全历史，直接覆盖即可。
    need_fetch = cache.empty
    if not cache.empty:
        try:
            if (datetime.now() - _parse_date(cache["date"].max())).days >= 2:
                need_fetch = True
        except Exception:
            need_fetch = False

    if need_fetch:
        try:
            import akshare as ak
            if legu_code:
                # 新指数(如中证A500)：直接按 indexCode 取乐咕全历史，已是 date/pe 标准列
                df = _legu_index_pe_by_code(legu_code)
                if df is not None and not df.empty:
                    cache = df
                    cache.to_csv(path, index=False)
            else:
                if name:
                    raw = _with_retry(lambda: ak.stock_index_pe_lg(symbol=name))
                    col = "滚动市盈率" if "滚动市盈率" in raw.columns else raw.columns[-1]
                else:
                    raw = _with_retry(lambda: ak.stock_market_pe_lg(symbol=market))
                    col = "平均市盈率" if "平均市盈率" in raw.columns else (
                        "市盈率" if "市盈率" in raw.columns else raw.columns[-1])
                if raw is not None and not raw.empty:
                    df = pd.DataFrame({
                        "date": pd.to_datetime(raw["日期"]).dt.strftime("%Y-%m-%d"),
                        "pe": pd.to_numeric(raw[col], errors="coerce"),
                    })
                    cache = df
                    cache.to_csv(path, index=False)
        except Exception as e:
            print(f"[data] 获取 {symbol} PE 失败：{e}")

    if cache.empty:
        return cache
    mask = (cache["date"] >= start) & (cache["date"] <= end)
    return cache.loc[mask].sort_values("date").reset_index(drop=True)


def load_stock_pe(symbol: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
    """加载个股 TTM 市盈率历史(百度股市通,近五年)，返回列 date / pe。

    用于 A500 成分股等个股的估值近5年分位计算。
    亏损(PE<=0)的点视为无意义而剔除；仅支持 6 位个股代码。
    """
    symbol = str(symbol).strip()
    if not (symbol.isdigit() and len(symbol) == 6):
        return pd.DataFrame()

    path = os.path.join(CACHE_DIR, f"pe_stock_{symbol}.csv")
    cache = pd.DataFrame()
    if use_cache and os.path.exists(path):
        try:
            cache = pd.read_csv(path, dtype={"date": str})
        except Exception:
            cache = pd.DataFrame()

    need_fetch = cache.empty
    if not cache.empty:
        try:
            if (datetime.now() - _parse_date(cache["date"].max())).days >= 2:
                need_fetch = True
        except Exception:
            need_fetch = False

    if need_fetch:
        try:
            import akshare as ak
            raw = _with_retry(lambda: ak.stock_zh_valuation_baidu(
                symbol=symbol, indicator="市盈率(TTM)", period="近五年"))
            if raw is not None and not raw.empty:
                pe = pd.to_numeric(raw["value"], errors="coerce")
                pe = pe.where(pe > 0)                      # 亏损 PE 无意义
                df = pd.DataFrame({
                    "date": pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d"),
                    "pe": pe,
                }).dropna(subset=["pe"])
                if not df.empty:
                    cache = df
                    cache.to_csv(path, index=False)
        except Exception as e:
            print(f"[data] 获取 {symbol} 个股PE失败：{e}")

    if cache.empty:
        return cache
    mask = (cache["date"] >= start) & (cache["date"] <= end)
    return cache.loc[mask].sort_values("date").reset_index(drop=True)


def load_index_current_pe(symbol: str):
    """中证官方指数最新 TTM 市盈率(stock_zh_index_value_csindex)。

    该接口仅返回最近约 20 个交易日，无法算近5年分位，故只取「当前值」。
    用于中证A500 等乐咕无长历史 PE 的指数。返回 float 或 None。
    """
    code6 = INDEX_CSINDEX_PE.get(str(symbol).strip().lower())
    if not code6:
        return None
    try:
        import akshare as ak
        df = _with_retry(lambda: ak.stock_zh_index_value_csindex(symbol=code6))
        if df is not None and not df.empty:
            col = "市盈率2" if "市盈率2" in df.columns else (
                "市盈率1" if "市盈率1" in df.columns else None)
            if col:
                if "日期" in df.columns:
                    df = df.sort_values("日期")
                v = pd.to_numeric(df[col], errors="coerce").dropna()
                if len(v):
                    return float(v.iloc[-1])
    except Exception as e:
        print(f"[data] 获取 {symbol} 中证官方PE失败：{e}")
    return None
