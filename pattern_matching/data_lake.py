"""日K数据湖与批量加载（BE-012）。

封装现有 load_kline() 为批量研究用数据湖，提供 symbol 批量加载和诊断。
"""
import time
from typing import List, Dict
from datetime import datetime, timedelta
from backtest.data import load_kline


def batch_load(symbols: List[str], start: str, end: str,
               adjust: str = "qfq", min_bars: int = 250) -> Dict:
    """批量拉取多只股票的日K数据。

    Returns:
        {"ok": True, "data": {symbol: DataFrame}, "failed": [{symbol, reason}],
         "diagnostics": {"total": N, "success": N, "failed": N, "elapsed_ms": N}}
    """
    t0 = time.time()
    data = {}
    failed = []

    for sym in symbols:
        try:
            df = load_kline(str(sym).strip(), start, end, adjust=adjust)
            if df is None or df.empty or len(df) < min_bars:
                cnt = len(df) if df is not None else 0
                failed.append({"symbol": sym, "reason": f"数据不足(仅{cnt}行, 需≥{min_bars})"})
            else:
                data[sym] = df.sort_values("date").reset_index(drop=True)
        except Exception as e:
            failed.append({"symbol": sym, "reason": f"{type(e).__name__}: {e}"})

    elapsed = (time.time() - t0) * 1000
    return {
        "ok": True,
        "data": data,
        "failed": failed,
        "diagnostics": {
            "total": len(symbols),
            "success": len(data),
            "failed": len(failed),
            "elapsed_ms": round(elapsed, 1),
        },
    }


def batch_load_from_default(watch_years: int = 5, min_bars: int = 250) -> Dict:
    """批量加载默认标的（A500成分股）的日K数据。"""
    from backtest.scanner import get_a500_constituents
    a500 = get_a500_constituents()
    symbols = [c for c, n in a500]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=365 * watch_years + 30)).strftime("%Y-%m-%d")
    return batch_load(symbols, start, end, min_bars=min_bars)
