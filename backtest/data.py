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


def load_pe(symbol: str, start: str, end: str, use_cache: bool = True) -> pd.DataFrame:
    """加载指数的 PE(滚动市盈率) 历史，返回列 date / pe。

    仅支持 INDEX_PE_NAME 中的指数；其它(个股/不支持)返回空 DataFrame。
    """
    symbol = str(symbol).strip().lower()
    name = INDEX_PE_NAME.get(symbol)
    if not name:
        return pd.DataFrame()

    path = os.path.join(CACHE_DIR, f"pe_{symbol}.csv")
    cache = pd.DataFrame()
    if use_cache and os.path.exists(path):
        try:
            cache = pd.read_csv(path, dtype={"date": str})
        except Exception:
            cache = pd.DataFrame()

    # 缓存最新日期距今天 ≥2 个自然日就刷新(PE 数据每个交易日更新)；
    # stock_index_pe_lg 每次返回全历史，直接覆盖即可。
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
            raw = _with_retry(lambda: ak.stock_index_pe_lg(symbol=name))
            if raw is not None and not raw.empty:
                col = "滚动市盈率" if "滚动市盈率" in raw.columns else raw.columns[-1]
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
