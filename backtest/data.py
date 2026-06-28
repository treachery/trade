"""数据层：通过 akshare 获取 A 股日线数据，并做本地 CSV 缓存。

akshare 免费、无需 token，数据来源为东方财富。
返回标准化后的列：date / open / close / high / low / volume / pct_chg
"""
import os
import time
import threading
from collections import deque
from datetime import datetime, timedelta

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

# 港股/美股改用 yfinance(雅虎财经)，比 akshare 走东财快几十倍。
# 项目代码 -> 雅虎 Ticker 代码。
# 港股指数：
HK_INDICES_MAP = {
    "HSI": "^HSI",          # 恒生指数
    "HSTECH": "HSTECH.HK",  # 恒生科技指数
    "HSCEI": "^HSCE",       # 恒生中国企业指数(国企指数)
}
# 美股指数：
US_INDICES_MAP = {
    ".SPX": "^GSPC",   # 标普500（页面统一代码）
    ".INX": "^GSPC",   # 标普500（兼容旧输入）
    ".NDX": "^NDX",    # 纳斯达克100
}
# 美股指数估值别名：富途不支持美股指数估值，PE 走 WorldPERatio 月度长历史。
US_SP500_PE_ALIASES = {"INX", "SPX", "GSPC", "^GSPC", ".INX", ".SPX"}
US_NDX_PE_ALIASES = {"NDX", "^NDX", ".NDX", "NASDAQ100", "NASDAQ-100"}
WORLD_PERATIO_INDEX_URL = {
    "sp500": "https://worldperatio.com/index/sp-500/",
    "ndx": "https://worldperatio.com/index/nasdaq-100/",
}


def _cache_path(symbol: str, adjust: str) -> str:
    adj = adjust or "none"
    return os.path.join(CACHE_DIR, f"{symbol}_{adj}.csv")


def market_of(symbol: str) -> str:
    """按代码前缀判断市场：
      'hk'(港股,如 hkHSI/hk00700) / 'us'(美股,如 us.SPX/us105.AAPL) / 'cn'(A股,默认)。
    """
    low = str(symbol).strip().lower()
    if low.startswith("hk"):
        return "hk"
    if low.startswith("us"):
        return "us"
    return "cn"


def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    """把中文列(开盘/收盘/...)标准化为 date/open/close/high/low/volume。"""
    df = df.rename(columns=COL_MAP)
    keep = ["date"] + NUMERIC_COLS
    df = df[[c for c in keep if c in df.columns]].copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for c in NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _yahoo_ticker(symbol: str):
    """项目代码 -> 雅虎 Ticker。
      港股指数 hkHSI/hkHSTECH/hkHSCEI -> ^HSI/HSTECH.HK/^HSCE；
      港股个股 hk00700 -> 0700.HK(去前导0到4位)；
      美股指数 us.SPX(us.INX兼容)/us.NDX -> ^GSPC/^NDX；
      美股个股 us105.AAPL -> AAPL(取 . 后纯符号)。
    """
    code = str(symbol).strip()
    mkt = market_of(code)
    if mkt == "hk":
        body = code[2:]
        if body.upper() in HK_INDICES_MAP:
            return HK_INDICES_MAP[body.upper()]
        # 港股个股：东财5位代码 -> 雅虎4位(去一个前导0).HK，如 00700 -> 0700.HK
        digits = "".join(ch for ch in body if ch.isdigit())
        return f"{digits.zfill(4)[-4:] if len(digits) > 4 else digits.zfill(4)}.HK" \
            if digits else None
    if mkt == "us":
        body = code[2:]
        if body.upper() in US_INDICES_MAP:
            return US_INDICES_MAP[body.upper()]
        # 美股个股：us105.AAPL -> AAPL；us AAPL -> AAPL
        return body.split(".")[-1].upper() if body else None
    return None


def _fetch_yf(symbol: str, start: str, end: str) -> pd.DataFrame:
    """用 yfinance 取港股/美股日线，标准化为 date/open/close/high/low/volume。
    auto_adjust=True 已做前复权(对个股=后复权口径，趋势一致)。
    """
    tk = _yahoo_ticker(symbol)
    if not tk:
        return pd.DataFrame()
    try:
        import yfinance as yf
        # end 含右开区间，+1 天确保包含 end 当日
        end_p = (_parse_date(end) + timedelta(days=1)).strftime("%Y-%m-%d")
        raw = _with_retry(lambda: yf.Ticker(tk).history(
            start=start, end=end_p, auto_adjust=True, raise_errors=False))
        if raw is None or raw.empty:
            return pd.DataFrame()
        raw = raw.reset_index()
        date_col = "Date" if "Date" in raw.columns else raw.columns[0]
        out = pd.DataFrame({
            "date": pd.to_datetime(raw[date_col]).dt.strftime("%Y-%m-%d"),
            "open": pd.to_numeric(raw.get("Open"), errors="coerce"),
            "close": pd.to_numeric(raw.get("Close"), errors="coerce"),
            "high": pd.to_numeric(raw.get("High"), errors="coerce"),
            "low": pd.to_numeric(raw.get("Low"), errors="coerce"),
            "volume": pd.to_numeric(raw.get("Volume"), errors="coerce"),
        })
        return out.dropna(subset=["close"]).reset_index(drop=True)
    except Exception as e:
        print(f"[data] yfinance 取 {symbol}({tk}) 失败：{e}")
        return pd.DataFrame()


def _fetch_ak_overseas(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """akshare 港股/美股兜底(yfinance 取空时用)。如恒生科技指数雅虎历史缺失。"""
    import akshare as ak
    code = str(symbol).strip()
    mkt = market_of(code)
    try:
        if mkt == "hk":
            body = code[2:]
            if body.upper() in HK_INDICES_MAP:
                df = _with_retry(lambda: ak.stock_hk_index_daily_em(symbol=body.upper()))
                if df is None or df.empty:
                    return pd.DataFrame()
                df = df.rename(columns={"latest": "close"})
                keep = ["date", "open", "close", "high", "low", "volume"]
                df = df[[c for c in keep if c in df.columns]].copy()
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                for c in ["open", "close", "high", "low", "volume"]:
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                return df
            s, e = start.replace("-", ""), end.replace("-", "")
            df = _with_retry(lambda: ak.stock_hk_hist(
                symbol=body, period="daily", start_date=s, end_date=e, adjust=adjust or ""))
            return _norm_cols(df) if df is not None and not df.empty else pd.DataFrame()
        if mkt == "us":
            body = code[2:]
            if body.upper() in {".SPX": 1, ".INX": 1, ".NDX": 1}:
                sina = {".SPX": ".INX", ".INX": ".INX", ".NDX": ".NDX"}[body.upper()]
                df = _with_retry(lambda: ak.index_us_stock_sina(symbol=sina))
                if df is None or df.empty:
                    return pd.DataFrame()
                keep = ["date", "open", "close", "high", "low", "volume"]
                df = df[[c for c in keep if c in df.columns]].copy()
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                for c in ["open", "close", "high", "low", "volume"]:
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                return df
            s, e = start.replace("-", ""), end.replace("-", "")
            df = _with_retry(lambda: ak.stock_us_hist(
                symbol=body, period="daily", start_date=s, end_date=e, adjust=adjust or ""))
            return _norm_cols(df) if df is not None and not df.empty else pd.DataFrame()
    except Exception as e:
        print(f"[data] akshare 兜底取 {symbol} 失败：{e}")
    return pd.DataFrame()


def _fetch(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """拉取并标准化日线(列 date/open/close/high/low/volume)。

    A股走 akshare(东财)；港股/美股走 yfinance(雅虎，快几十倍)。
    """
    code = str(symbol).strip()
    mkt = market_of(code)

    # ---------- 港股 / 美股：yfinance(快) ----------
    if mkt in ("hk", "us"):
        df = _fetch_yf(code, start, end)
        if df is not None and not df.empty:
            return df
        # yfinance 取空(如恒生科技指数雅虎历史缺失) -> akshare 兜底
        return _fetch_ak_overseas(code, start, end, adjust)

    import akshare as ak  # 延迟导入(仅A股需要)

    # ---------- A股 ----------
    low = code.lower()
    if low.startswith("sh") or low.startswith("sz"):
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
    return _norm_cols(df) if df is not None and not df.empty else pd.DataFrame()


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
            gap_start = (_parse_date(cmin) - _parse_date(start)).days  # >0 表示请求起点早于缓存最早日(需补更早历史)
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
    # OHLC 数值化并剔除缺失/<=0 的脏数据行(如恒生科技指数部分日期 NaN、A股 qfq 偶发负价格)。
    # 否则 NaN 会随 jsonify 序列化成非法 JSON 的 "NaN"，导致前端解析失败。
    if "close" in out.columns and len(out):
        for c in ("open", "high", "low", "close"):
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=[c for c in ("open", "high", "low", "close") if c in out.columns])
        good = out["close"] > 0
        for c in ("open", "high", "low"):
            if c in out.columns:
                good &= out[c] > 0
        out = out[good].reset_index(drop=True)
    # 成交量：部分指数(如恒生科技)无成交量，NaN 兜底为 0，避免污染 JSON。
    if "volume" in out.columns and len(out):
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
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

    path = os.path.join(CACHE_DIR, f"pe_stock_{symbol}_10y.csv")
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
                symbol=symbol, indicator="市盈率(TTM)", period="近十年"))
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


def load_hk_pe(symbol: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
    """加载港股个股 TTM 市盈率历史(百度股市通)，返回列 date / pe。
    symbol 形如 hk00700 或 00700。可算近若干年分位。亏损 PE 剔除。
    """
    body = str(symbol).strip()
    if body.lower().startswith("hk"):
        body = body[2:]
    if not body.isdigit():
        return pd.DataFrame()

    path = os.path.join(CACHE_DIR, f"pe_hk_{body}_10y.csv")
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
            raw = _with_retry(lambda: ak.stock_hk_valuation_baidu(
                symbol=body, indicator="市盈率(TTM)", period="近十年"))
            if raw is not None and not raw.empty:
                pe = pd.to_numeric(raw["value"], errors="coerce")
                pe = pe.where(pe > 0)
                df = pd.DataFrame({
                    "date": pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d"),
                    "pe": pe,
                }).dropna(subset=["pe"])
                if not df.empty:
                    cache = df
                    cache.to_csv(path, index=False)
        except Exception as e:
            print(f"[data] 获取 {symbol} 港股PE失败：{e}")

    if cache.empty:
        return cache
    mask = (cache["date"] >= start) & (cache["date"] <= end)
    return cache.loc[mask].sort_values("date").reset_index(drop=True)


# 美股当前 PE 缓存(spot 接口一次返回全市场，缓存共享，1天有效)
_us_pe_cache_ts: float = 0.0
_us_pe_cache_map: dict[str, float] = {}
_us_pe_lock = threading.Lock()

# Futu/moomoo get_valuation_detail 服务端限频：30次/30秒。
# 扫描标普500时会并发触发大量估值请求，必须全局限速，否则后续标的会被拒绝并显示无PE。
_futu_pe_lock = threading.Lock()
_futu_pe_req_times = deque()
_FUTU_PE_MAX_CALLS = 28
_FUTU_PE_WINDOW_SEC = 30.0


def _wait_futu_pe_rate_limit():
    while True:
        now = time.time()
        while _futu_pe_req_times and now - _futu_pe_req_times[0] >= _FUTU_PE_WINDOW_SEC:
            _futu_pe_req_times.popleft()
        if len(_futu_pe_req_times) < _FUTU_PE_MAX_CALLS:
            _futu_pe_req_times.append(now)
            return
        sleep_s = _FUTU_PE_WINDOW_SEC - (now - _futu_pe_req_times[0]) + 0.2
        time.sleep(max(0.2, sleep_s))


def load_us_current_pe(symbol: str):
    """美股个股当前市盈率(stock_us_spot_em 的「市盈率」字段)。
    symbol 形如 us105.AAPL 或 105.AAPL。百度美股历史PE接口已失效，故只取当前值(无分位)。
    返回 float 或 None。
    """
    body = str(symbol).strip()
    if body.lower().startswith("us"):
        body = body[2:]
    try:
        import akshare as ak
        global _us_pe_cache_ts, _us_pe_cache_map
        # 全市场 spot 一次拉取，缓存 1 天，避免每只票都拉；并发扫描时只允许一个线程刷新。
        if time.time() - _us_pe_cache_ts > 86400 or not _us_pe_cache_map:
            with _us_pe_lock:
                if time.time() - _us_pe_cache_ts > 86400 or not _us_pe_cache_map:
                    spot = _with_retry(lambda: ak.stock_us_spot_em())
                    m = {}
                    if spot is not None and not spot.empty and "代码" in spot.columns and "市盈率" in spot.columns:
                        for _, r in spot[["代码", "市盈率"]].iterrows():
                            try:
                                pe = float(r["市盈率"])
                            except (TypeError, ValueError):
                                continue
                            if pd.notna(pe):
                                m[str(r["代码"]).strip()] = pe
                    _us_pe_cache_map = m
                    _us_pe_cache_ts = time.time()
        pe = _us_pe_cache_map.get(body)
        if pe is not None and pe > 0:
            return float(pe)
    except Exception as e:
        print(f"[data] 获取 {symbol} 美股PE失败：{e}")
    return None


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


def _us_worldperatio_index_key(symbol: str):
    """项目美股指数代码 -> WorldPERatio 索引键。页面统一 us.SPX/us.NDX，兼容旧代码。"""
    s = str(symbol).strip().upper()
    if s.startswith("US"):
        s = s[2:]
    raw = s
    pure = s.lstrip(".")
    if raw in US_SP500_PE_ALIASES or pure in US_SP500_PE_ALIASES:
        return "sp500"
    if raw in US_NDX_PE_ALIASES or pure in US_NDX_PE_ALIASES:
        return "ndx"
    return None


def _is_sp500_index(symbol: str) -> bool:
    """是否为标普500指数代码。保留给旧调用兼容。"""
    return _us_worldperatio_index_key(symbol) == "sp500"


def load_us_index_pe_worldperatio(symbol: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
    """加载美股指数历史 PE(WorldPERatio 月度长历史)，返回列 date / pe。

    支持：
      - 标普500：us.SPX（兼容 us.INX/us..SPX/^GSPC），WorldPERatio sp-500 页面；
      - 纳指100：us.NDX（兼容 ^NDX），WorldPERatio nasdaq-100 页面。
    WorldPERatio 页面将历史序列内嵌在 JS 变量 detailPE_data 中，日期为 Date.UTC(year, month0, day)。
    """
    key = _us_worldperatio_index_key(symbol)
    if not key:
        return pd.DataFrame()

    path = os.path.join(CACHE_DIR, f"pe_us_{key}_worldperatio.csv")
    cache = pd.DataFrame()
    if use_cache and os.path.exists(path):
        try:
            cache = pd.read_csv(path, dtype={"date": str})
        except Exception:
            cache = pd.DataFrame()

    need_fetch = cache.empty
    if not cache.empty:
        try:
            if (datetime.now() - _parse_date(str(cache["date"].max()))).days >= 2:
                need_fetch = True
        except Exception:
            need_fetch = False

    if need_fetch:
        try:
            import re
            import requests
            url = WORLD_PERATIO_INDEX_URL[key]
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = _with_retry(lambda: requests.get(url, headers=headers, timeout=15))
            resp.raise_for_status()
            html = resp.text
            idx = html.find("detailPE_data =")
            if idx < 0:
                return pd.DataFrame()
            end_idx = html.find(";", idx)
            chunk = html[idx:end_idx if end_idx > idx else len(html)]
            rows = []
            for y, m0, d, v in re.findall(
                    r"Date\.UTC\((\d{4}),\s*(\d+),\s*(\d+)\),\s*([\-\d\.]+)", chunk):
                try:
                    rows.append({
                        "date": f"{int(y):04d}-{int(m0) + 1:02d}-{int(d):02d}",
                        "pe": float(v),
                    })
                except Exception:
                    continue
            if rows:
                rows = sorted((r for r in rows if r["pe"] > 0), key=lambda r: r["date"])
                df = pd.DataFrame(rows).drop_duplicates(subset=["date"]).reset_index(drop=True)
                if not df.empty:
                    cache = df
                    cache.to_csv(path, index=False)
        except Exception as e:
            print(f"[data] 获取 {symbol} WorldPERatio PE 失败：{e}")

    if cache.empty:
        return cache
    mask = (cache["date"] >= start) & (cache["date"] <= end)
    return cache.loc[mask].sort_values("date").reset_index(drop=True)


def load_us_pe_history(symbol: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
    """美股个股历史静态 PE 序列(收盘价 / 最近已公告年报 EPS)，返回列 date / pe。

    用 akshare 美股财务指标(年报)的 BASIC_EPS + yfinance 不复权收盘价自算。
    口径=静态 PE(lyr)：每个交易日用「已公告的最新年报 EPS」，
    生效日 = 财报截止日 + 90 天(保守，避免用到未披露数据)。
    亏损年(EPS<=0)剔除；返回序列可算近5年分位。失败返回空(回测退化满仓)。
    """
    body = str(symbol).strip()
    if body.lower().startswith("us"):
        body = body[2:]
    pure = body.split(".")[-1].upper() if body else ""
    if not pure or pure in ("INX", "SPX", "IXIC", "NDX", "DJI", "RUT"):   # 指数无个股EPS
        return pd.DataFrame()

    path = os.path.join(CACHE_DIR, f"pe_us_{pure}.csv")
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
            # 1) 年报净利润(生效日=报告期+90天)。用净利润而非 EPS：避免美股拆股导致
            #    EPS(历史股本口径)与复权价(拆股调整后)口径不一致。
            fin = _with_retry(lambda: ak.stock_financial_us_analysis_indicator_em(
                symbol=pure, indicator="年报"))
            np_list = []  # [(effective_date_str, net_profit), ...]
            if fin is not None and not fin.empty and "REPORT_DATE" in fin.columns:
                for _, r in fin.iterrows():
                    try:
                        rd = pd.to_datetime(r["REPORT_DATE"])
                        ni = pd.to_numeric(r.get("PARENT_HOLDER_NETPROFIT"), errors="coerce")
                    except Exception:
                        continue
                    if pd.isna(ni) or ni <= 0 or pd.isna(rd):
                        continue
                    np_list.append(((rd + timedelta(days=90)).strftime("%Y-%m-%d"), float(ni)))
            np_list.sort(key=lambda x: x[0])
            if not np_list:
                return pd.DataFrame()

            # 2) 前复权收盘价(已含拆股调整) + 当前股本
            import yfinance as yf
            tk = yf.Ticker(pure)
            end_p = (_parse_date(end) + timedelta(days=1)).strftime("%Y-%m-%d")
            raw = _with_retry(lambda: tk.history(
                start=start, end=end_p, auto_adjust=True, raise_errors=False))
            if raw is None or raw.empty:
                return pd.DataFrame()
            shares = None
            try:
                shares = tk.info.get("sharesOutstanding")
            except Exception:
                pass
            if not shares:
                try:
                    shares = tk.fast_info.get("shares_outstanding")
                except Exception:
                    shares = None
            if not shares:
                return pd.DataFrame()
            raw = raw.reset_index()
            date_col = "Date" if "Date" in raw.columns else raw.columns[0]
            kl = pd.DataFrame({
                "date": pd.to_datetime(raw[date_col]).dt.strftime("%Y-%m-%d"),
                "close": pd.to_numeric(raw["Close"], errors="coerce"),
            }).dropna(subset=["close"])
            kl = kl[kl["close"] > 0].sort_values("date").reset_index(drop=True)

            # 3) PE = 前复权价 × 当前股本 / 生效年报净利润  (= 历史市值 / 净利润)
            eff_dates = [e[0] for e in np_list]
            eff_np = [e[1] for e in np_list]
            rows = []
            j = -1
            for _, kr in kl.iterrows():
                d = kr["date"]
                while j + 1 < len(eff_dates) and eff_dates[j + 1] <= d:
                    j += 1
                if j < 0:
                    continue
                rows.append({"date": d, "pe": round(float(kr["close"]) * shares / eff_np[j], 4)})
            if rows:
                cache = pd.DataFrame(rows)
                cache.to_csv(path, index=False)
        except Exception as e:
            print(f"[data] 美股 {symbol} 历史PE自算失败：{e}")

    if cache.empty:
        return cache
    mask = (cache["date"] >= start) & (cache["date"] <= end)
    return cache.loc[mask].sort_values("date").reset_index(drop=True)


# 港股指数 -> 富途 IDX 代码（已实测 get_valuation_detail 可取 4000+ 条长历史 PE）
HK_FUTU_INDEX = {
    "HSI": "HK.800000",     # 恒生指数
    "HSTECH": "HK.800700",  # 恒生科技指数
    "HSCEI": "HK.800100",   # 恒生国企指数
}


def _to_futu_code(symbol):
    """项目代码 -> 富途代码。富途账户仅有港股/美股行情权限，故只对港美返回代码。

    分工(已实测验证)：
      - 港股指数 hkHSI/hkHSTECH/hkHSCEI -> HK.800000/800700/800100（富途可取长历史 PE）；
      - 港股个股 hk00700 -> HK.00700（补足 5 位）；
      - 美股个股 us105.AAPL -> US.AAPL；
      - 美股指数 us.SPX/us.NDX 等 -> None（富途服务端硬拒绝：US stock indices are not supported）；
      - A股(sh/sz/6位数字) -> None（富途无 A 股行情权限，回落到乐咕/百度原接口）。
    """
    s = str(symbol).strip()
    low = s.lower()
    if low.startswith("hk"):
        body = s[2:]
        if body.upper() in HK_FUTU_INDEX:
            return HK_FUTU_INDEX[body.upper()]
        digits = "".join(c for c in body if c.isdigit())
        return f"HK.{digits.zfill(5)}" if digits else None
    if low.startswith("us"):
        body = s[2:]
        pure = body.split(".")[-1].upper() if body else ""
        if pure in ("INX", "SPX", "IXIC", "NDX", "DJI", "RUT"):
            return None  # 美股指数富途服务端不支持估值，退化到其它源/满仓
        return f"US.{pure}" if pure else None
    # A股(sh/sz/6位数字)：富途账户无 A 股行情权限，返回 None -> 走乐咕/百度原接口
    return None


def load_futu_pe_history(symbol, start, end, use_cache=True):
    """通过富途 OpenAPI 估值详情接口(get_valuation_detail)取历史 PE 序列(date/pe)。

    需环境变量 FUTU_OPEND_HOST(默认127.0.0.1)/FUTU_OPEND_PORT(默认11111)指向已登录的 FutuOpenD。
    用 valuation_type=1(PE) + interval_type=9(近20年)，取 trend.historical_items 的每日 PE。
    口径由富途统一计算(规避美股拆股/回购的自算陷阱)，且支持个股+指数(港美A)。
    未配置 OpenD 时：只读本地缓存(云端部署用，缓存由本地富途拉取后同步)，不联网。
    缓存为空且无 OpenD 时返回空(由上层退化到其它源/满仓)。
    """
    code = _to_futu_code(symbol)
    if not code:
        return pd.DataFrame()
    path = os.path.join(CACHE_DIR, f"pe_futu_{code.replace('.', '_')}.csv")
    cache = pd.DataFrame()
    if use_cache and os.path.exists(path):
        try:
            cache = pd.read_csv(path, dtype={"date": str})
        except Exception:
            cache = pd.DataFrame()
    host = os.environ.get("FUTU_OPEND_HOST", "").strip()
    # 无 OpenD(云端部署)：只读本地缓存(由本地富途拉取后同步)，不联网
    if not host:
        if cache.empty:
            return cache
        mask = (cache["date"] >= start) & (cache["date"] <= end)
        return cache.loc[mask].sort_values("date").reset_index(drop=True)
    port = int(os.environ.get("FUTU_OPEND_PORT", "11111"))

    need_fetch = cache.empty
    if not cache.empty:
        try:
            if (datetime.now() - _parse_date(cache["date"].max())).days >= 2:
                need_fetch = True
        except Exception:
            need_fetch = False

    if need_fetch:
        try:
            from futu import OpenQuoteContext, RET_OK
            with _futu_pe_lock:
                _wait_futu_pe_rate_limit()
                ctx = OpenQuoteContext(host=host, port=port)
                try:
                    # 估值详情：历史 PE 序列(trend.historical_items)，interval_type=9=近20年
                    ret, data = ctx.get_valuation_detail(
                        code=code, valuation_type=1, interval_type=9)
                    if ret == RET_OK and data:
                        items = (data.get("trend") or {}).get("historical_items") or []
                        rows = []
                        for it in items:
                            try:
                                d = pd.to_datetime(it.get("time_str") or it.get("time")).strftime("%Y-%m-%d")
                                pe = pd.to_numeric(it.get("value"), errors="coerce")
                            except Exception:
                                continue
                            if pd.notna(pe) and pe != 0:
                                rows.append({"date": d, "pe": float(pe)})
                        if rows:
                            cache = pd.DataFrame(rows).sort_values("date") \
                                .drop_duplicates("date").reset_index(drop=True)
                            cache.to_csv(path, index=False)
                finally:
                    ctx.close()
        except Exception as e:
            print(f"[data] 富途 {symbol}({code}) 估值取数失败：{e}")

    if cache.empty:
        return cache
    mask = (cache["date"] >= start) & (cache["date"] <= end)
    return cache.loc[mask].sort_values("date").reset_index(drop=True)


def load_valuation_pe(symbol: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
    """统一估值入口：按标的类型/市场自适应分发到对应 PE 历史源，返回列 date / pe。

    数据源分工(富途仅有港股/美股权限，A股走乐咕/百度)：
      ① 富途 OpenAPI：港股指数(HK.800000等)、港股个股、美股个股 —— _to_futu_code 能映射出代码才走；
      ② 美股指数：标普500(us.SPX) / 纳指100(us.NDX) 走 WorldPERatio 月度长历史 PE；
      ③ A股指数(乐咕长历史) / A股个股(百度近十年TTM) / 港股个股(百度近十年TTM，富途取空时兜底)；
      ④ 其它美股指数：富途服务端不支持、暂无免费长历史源 -> 退化满仓；
      ⑤ 美股个股未接富途时：自算有拆股/回购口径陷阱 -> 退化满仓。
    富途取不到(无OpenD/无缓存/无权限/不支持)时自动回落到下方对应源，互不影响。
    """
    low = str(symbol).strip().lower()
    # 0) 富途(仅港美)：港股指数/港美个股优先走富途；A股/美股指数 _to_futu_code 返回 None
    #    -> 直接空 df，回落到下方对应源。无 OpenD 时读本地缓存，无缓存亦回落。
    futu_df = load_futu_pe_history(symbol, start, end, use_cache)
    if futu_df is not None and not futu_df.empty:
        return futu_df

    # 1) 美股指数：富途/moomoo 不支持美股指数估值，标普500/纳指100改走 WorldPERatio
    if _us_worldperatio_index_key(symbol):
        return load_us_index_pe_worldperatio(symbol, start, end, use_cache)

    # 2) A股指数：乐咕指数PE / 交易所市场平均PE / 中证A500乐咕全历史
    if low in INDEX_LEGU_CSI or low in INDEX_PE_NAME or low in INDEX_MARKET_PE:
        return load_pe(symbol, start, end, use_cache)

    mkt = market_of(symbol)
    # 2) 港股个股(hk+数字)走百度兜底；港股指数若富途未命中(云端无缓存)则此处无源 -> 退化满仓
    if mkt == "hk":
        return load_hk_pe(symbol, start, end, use_cache)
    # 3) 美股：标普500/纳指100已在上方走 WorldPERatio；其它美股指数/个股无富途缓存时退化满仓
    if mkt == "us":
        us_index_df = load_us_index_pe_worldperatio(symbol, start, end, use_cache)
        if us_index_df is not None and not us_index_df.empty:
            return us_index_df
        return pd.DataFrame()

    # 4) A股个股(6位数字)
    body = str(symbol).strip()
    if body.isdigit() and len(body) == 6:
        return load_stock_pe(symbol, start, end, use_cache)
    return pd.DataFrame()
