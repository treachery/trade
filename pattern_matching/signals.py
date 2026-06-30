"""信号侦测引擎（第4层）。

复用 backtest.engine 的技术指标计算函数，实现文档定义的买入/卖出信号事件。
每条信号附带质量属性：强度、量能确认、波动状态、信号年龄等。
"""
import math
from datetime import datetime, timedelta

from backtest.engine import _sma, _ema, _macd, _atr, _rolling_max_prior, _rolling_min_prior
from backtest.data import load_kline


# ===== 信号定义（对应文档 5.1 节）=====
SIGNAL_DEFINITIONS = {
    "buy": [
        {"type": "breakout_20d", "label": "收盘突破20日新高", "strength": 0.7},
        {"type": "breakout_60d", "label": "收盘突破60日新高", "strength": 0.85},
        {"type": "breakout_120d", "label": "收盘突破120日新高", "strength": 0.95},
        {"type": "bb_breakout", "label": "突破布林带上轨", "strength": 0.5},
        {"type": "vol_breakout", "label": "量价突破20日新高", "strength": 0.75},
        {"type": "ma_golden_20_60", "label": "MA20上穿MA60", "strength": 0.5},
        {"type": "ma_bull_stack", "label": "多头排列初次形成", "strength": 0.75},
        {"type": "resit_ma20", "label": "价格重站MA20", "strength": 0.5},
        {"type": "rsi_oversold", "label": "RSI低位上穿30", "strength": 0.3},
        {"type": "vol_reversal", "label": "连跌后放量阳线", "strength": 0.5},
    ],
    "sell": [
        {"type": "break_20d_low", "label": "收盘跌破20日新低", "strength": 0.7},
        {"type": "break_60d_low", "label": "收盘跌破60日新低", "strength": 0.85},
        {"type": "ma_death_20_60", "label": "MA20下穿MA60", "strength": 0.5},
        {"type": "ma_bear_stack", "label": "空头排列形成", "strength": 0.75},
        {"type": "rsi_overbought", "label": "RSI高位下穿70", "strength": 0.3},
    ],
}


def _rsi(closes, period=14):
    """RSI(14)。"""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    avg_g = gains / period
    avg_l = losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-ch, 0)) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def _bollinger(closes, period=20, num_std=2):
    """布林带 (upper, middle, lower)。"""
    n = len(closes)
    mid = _sma(closes, period)
    upper, lower = [None] * n, [None] * n
    for i in range(n):
        if mid[i] is not None and i >= period - 1:
            window = closes[i - period + 1:i + 1]
            m = sum(window) / period
            var = sum((x - m) ** 2 for x in window) / period
            sd = math.sqrt(var)
            upper[i] = mid[i] + num_std * sd
            lower[i] = mid[i] - num_std * sd
    return upper, mid, lower


def _volatility(closes, period):
    """滚动波动率(标准差 of 日收益率)。"""
    n = len(closes)
    out = [None] * n
    rets = [0.0] * n
    for i in range(1, n):
        if closes[i - 1] > 0:
            rets[i] = closes[i] / closes[i - 1] - 1
    for i in range(period - 1, n):
        window = rets[i - period + 1:i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        out[i] = math.sqrt(var)
    return out


def _market_regime(closes, ma_period=60):
    """市场状态分类：BULL / BEAR / RANGE。"""
    n = len(closes)
    ma = _sma(closes, ma_period)
    if n < ma_period or ma[-1] is None:
        return "RANGE"
    total_chg = (closes[-1] / closes[max(0, n - 120)] - 1) if n > 120 else 0
    if closes[-1] > ma[-1] and total_chg > 0.10:
        return "BULL"
    if closes[-1] < ma[-1] and total_chg < -0.10:
        return "BEAR"
    return "RANGE"


def _signal_strength_label(s):
    if s >= 0.9:
        return "极强"
    if s >= 0.7:
        return "强"
    if s >= 0.5:
        return "中"
    return "弱"


def detect_signals(symbol, as_of_date=None, adjust="qfq", lookback_years=5, offline=False):
    """在指定日期 D 检测标的的买入/卖出信号事件。

    严格 Point-in-Time：仅使用 D 及以前的数据。
    返回 {symbol, as_of_date, last_close, market_regime, volatility_regime,
          buy_signals, sell_signals, has_signal, features_snapshot}
    """
    end = as_of_date or datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(end[:10], "%Y-%m-%d") - timedelta(days=365 * (lookback_years + 1))).strftime("%Y-%m-%d")

    df = load_kline(symbol, start, end, adjust=adjust, offline=offline)
    if df is None or df.empty or len(df) < 130:
        return {"symbol": symbol, "as_of_date": end, "ok": False, "error": "数据不足(需至少130个交易日)"}

    # Point-in-Time：截断到 as_of_date
    df = df[df["date"] <= end].sort_values("date").reset_index(drop=True)
    if len(df) < 130:
        return {"symbol": symbol, "as_of_date": end, "ok": False, "error": "截止日前数据不足"}

    n = len(df)
    dates = df["date"].tolist()
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    vols = df["volume"].tolist() if "volume" in df.columns else [0] * n

    # 预计算指标
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    ma120 = _sma(closes, 120)
    rsi = _rsi(closes, 14)
    bb_upper, bb_mid, bb_lower = _bollinger(closes, 20, 2)
    vol20 = _sma(vols, 20)
    atr14 = _atr(highs, lows, closes, 14)
    vol_20d = _volatility(closes, 20)
    vol_60d = _volatility(closes, 60)
    market_regime = _market_regime(closes, 60)

    # 波动状态
    vol_pct = None
    valid_vols = [v for v in vol_60d if v is not None]
    if valid_vols and vol_60d[-1] is not None:
        sv = sorted(valid_vols)
        rank = sum(1 for x in sv if x <= vol_60d[-1])
        vol_pct = rank / len(sv) * 100
    vol_regime = "HIGH" if (vol_pct or 50) > 66 else ("LOW" if (vol_pct or 50) < 33 else "MID")

    i = n - 1  # 信号确认日 = 截止日
    close_i = closes[i]
    buy_signals, sell_signals = [], []

    def _sig(sig_type, label, strength, extra=None):
        s = {
            "type": sig_type, "label": label,
            "signal_date": dates[i], "strength": strength,
            "strength_label": _signal_strength_label(strength),
            "market_regime": market_regime, "volatility_regime": vol_regime,
        }
        if extra:
            s.update(extra)
        return s

    # ---- 买入信号 ----
    # 突破20/60/120日新高
    for p, sdef in [(20, SIGNAL_DEFINITIONS["buy"][0]), (60, SIGNAL_DEFINITIONS["buy"][1]), (120, SIGNAL_DEFINITIONS["buy"][2])]:
        prior_max = _rolling_max_prior(highs, p)
        if prior_max[i] is not None and close_i > prior_max[i] and not (close_i > prior_max[i] if i > 0 else False):
            pass  # 仅检测当日是否处于突破状态（非上升沿也视为信号存在）
        if prior_max[i] is not None and close_i > prior_max[i]:
            buy_signals.append(_sig(sdef["type"], sdef["label"], sdef["strength"],
                                    {"breakout_period": p, "prior_high": round(prior_max[i], 4)}))

    # 布林带突破
    if bb_upper[i] is not None and close_i > bb_upper[i]:
        buy_signals.append(_sig("bb_breakout", SIGNAL_DEFINITIONS["buy"][3]["label"], 0.5,
                                {"bb_upper": round(bb_upper[i], 4)}))

    # 量价突破
    prior_max20 = _rolling_max_prior(highs, 20)
    if (prior_max20[i] is not None and close_i > prior_max20[i]
            and vol20[i] is not None and vol20[i] > 0 and vols[i] > 1.5 * vol20[i]):
        buy_signals.append(_sig("vol_breakout", SIGNAL_DEFINITIONS["buy"][4]["label"], 0.75,
                                {"vol_ratio": round(vols[i] / vol20[i], 2) if vol20[i] else 0}))

    # MA20上穿MA60（检测上升沿）
    if i >= 1 and ma20[i] is not None and ma60[i] is not None:
        if ma20[i] > ma60[i] and not (ma20[i - 1] is not None and ma60[i - 1] is not None and ma20[i - 1] > ma60[i - 1]):
            buy_signals.append(_sig("ma_golden_20_60", SIGNAL_DEFINITIONS["buy"][5]["label"], 0.5))

    # 多头排列初次形成
    if all(m is not None for m in [ma5[i], ma10[i], ma20[i], ma60[i]]):
        bull_now = ma5[i] > ma10[i] > ma20[i] > ma60[i] and close_i > ma5[i]
        bull_prev = (all(m is not None for m in [ma5[i-1], ma10[i-1], ma20[i-1], ma60[i-1]])
                     and ma5[i-1] > ma10[i-1] > ma20[i-1] > ma60[i-1]) if i >= 1 else False
        if bull_now and not bull_prev:
            buy_signals.append(_sig("ma_bull_stack", SIGNAL_DEFINITIONS["buy"][6]["label"], 0.75))

    # 价格重站MA20
    if ma20[i] is not None and close_i > ma20[i]:
        ma20_slope = (ma20[i] - ma20[i - 1]) if (i >= 1 and ma20[i-1] is not None) else 0
        if ma20_slope > 0 and i >= 1 and closes[i - 1] <= ma20[i - 1]:
            buy_signals.append(_sig("resit_ma20", SIGNAL_DEFINITIONS["buy"][7]["label"], 0.5))

    # RSI低位上穿30
    if rsi[i] is not None and rsi[i] >= 30 and i >= 1 and rsi[i - 1] is not None and rsi[i - 1] < 30:
        buy_signals.append(_sig("rsi_oversold", SIGNAL_DEFINITIONS["buy"][8]["label"], 0.3, {"rsi": round(rsi[i], 2)}))

    # 连跌后放量阳线
    if i >= 3 and closes[i] > closes[i - 1]:
        consec_down = all(closes[j] < closes[j - 1] for j in range(i - 2, i))
        vol_avg = _sma(vols, 5)
        if consec_down and vol_avg[i] is not None and vol_avg[i] > 0 and vols[i] > 1.2 * vol_avg[i]:
            buy_signals.append(_sig("vol_reversal", SIGNAL_DEFINITIONS["buy"][9]["label"], 0.5,
                                    {"vol_ratio": round(vols[i] / vol_avg[i], 2)}))

    # ---- 卖出信号 ----
    # 跌破20/60日新低
    for p, sdef in [(20, SIGNAL_DEFINITIONS["sell"][0]), (60, SIGNAL_DEFINITIONS["sell"][1])]:
        prior_min = _rolling_min_prior(lows, p)
        if prior_min[i] is not None and close_i < prior_min[i]:
            sell_signals.append(_sig(sdef["type"], sdef["label"], sdef["strength"],
                                     {"breakout_period": p, "prior_low": round(prior_min[i], 4)}))

    # MA20下穿MA60
    if i >= 1 and ma20[i] is not None and ma60[i] is not None:
        if ma20[i] < ma60[i] and not (ma20[i-1] is not None and ma60[i-1] is not None and ma20[i-1] < ma60[i-1]):
            sell_signals.append(_sig("ma_death_20_60", SIGNAL_DEFINITIONS["sell"][2]["label"], 0.5))

    # 空头排列形成
    if all(m is not None for m in [ma5[i], ma10[i], ma20[i], ma60[i]]):
        bear_now = ma5[i] < ma10[i] < ma20[i] < ma60[i]
        bear_prev = (all(m is not None for m in [ma5[i-1], ma10[i-1], ma20[i-1], ma60[i-1]])
                     and ma5[i-1] < ma10[i-1] < ma20[i-1] < ma60[i-1]) if i >= 1 else False
        if bear_now and not bear_prev:
            sell_signals.append(_sig("ma_bear_stack", SIGNAL_DEFINITIONS["sell"][3]["label"], 0.75))

    # RSI高位下穿70
    if rsi[i] is not None and rsi[i] <= 70 and i >= 1 and rsi[i - 1] is not None and rsi[i - 1] > 70:
        sell_signals.append(_sig("rsi_overbought", SIGNAL_DEFINITIONS["sell"][4]["label"], 0.3, {"rsi": round(rsi[i], 2)}))

    # 特征快照（供前端展示和相似检索复用）
    features_snapshot = {
        "last_close": round(close_i, 4),
        "last_date": dates[-1],
        "ma5": round(ma5[i], 4) if ma5[i] else None,
        "ma10": round(ma10[i], 4) if ma10[i] else None,
        "ma20": round(ma20[i], 4) if ma20[i] else None,
        "ma60": round(ma60[i], 4) if ma60[i] else None,
        "ma120": round(ma120[i], 4) if ma120[i] else None,
        "rsi14": round(rsi[i], 2) if rsi[i] else None,
        "atr14": round(atr14[i], 4) if atr14[i] else None,
        "volatility_20d": round(vol_20d[i] * 100, 2) if vol_20d[i] else None,
        "volatility_60d": round(vol_60d[i] * 100, 2) if vol_60d[i] else None,
        "vol_percentile": round(vol_pct, 1) if vol_pct is not None else None,
        "bb_upper": round(bb_upper[i], 4) if bb_upper[i] else None,
        "bb_lower": round(bb_lower[i], 4) if bb_lower[i] else None,
    }

    return {
        "ok": True,
        "symbol": symbol,
        "as_of_date": dates[-1],
        "last_close": round(close_i, 4),
        "market_regime": market_regime,
        "volatility_regime": vol_regime,
        "vol_percentile": round(vol_pct, 1) if vol_pct is not None else None,
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "has_signal": bool(buy_signals or sell_signals),
        "has_buy_signal": bool(buy_signals),
        "features_snapshot": features_snapshot,
        "total_bars": n,
    }
