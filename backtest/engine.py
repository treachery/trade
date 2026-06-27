"""回测引擎：基于可组合的入场/出场策略库逐日撮合，输出交易、买卖点、资金曲线与绩效。

所有指标均基于真实价格(OHLCV)。买卖以信号当日收盘价成交；不考虑整手限制(允许碎股)。
"""
import bisect
import itertools
from datetime import datetime

import pandas as pd

from .strategy import StrategyConfig, ENTRY_DEFAULTS, EXIT_DEFAULTS, CN_LABEL


def _build_position_fractions(config, dates, pe_series):
    """返回每个交易日的"入场仓位"数组(>1 表示融资)。

    pe_percentile: 仓位 = clamp((1-PE百分位)*2, min_leverage, max_leverage)，PE百分位为该PE在区间内的分位。
                   即 PE 最贵(分位100%)也保留最低 0.5 仓，PE 最便宜(分位0%)最高 2 倍。
    fixed:         恒定 fraction。
    无PE数据时退化为满仓 1.0。
    """
    n = len(dates)
    pos = config.position or {}
    ptype = pos.get("type", "pe_percentile")

    if ptype == "fixed":
        f = float(pos.get("fraction", 1.0))
        return [f] * n, [None] * n

    # pe_percentile
    max_lev = float(pos.get("max_leverage", 2.0))
    min_lev = float(pos.get("min_leverage", 0.5))
    pe_map = pe_series or {}
    # 对齐到交易日并前向填充
    pe_aligned = [None] * n
    last = None
    for i, d in enumerate(dates):
        if d in pe_map and pe_map[d] is not None:
            last = pe_map[d]
        pe_aligned[i] = last
    valid = sorted(v for v in pe_aligned if v is not None)
    fracs = [1.0] * n
    pcts = [None] * n
    if valid:
        m = len(valid)
        for i in range(n):
            v = pe_aligned[i]
            if v is None:
                fracs[i] = 1.0
            else:
                pct = bisect.bisect_right(valid, v) / m  # PE 百分位(0~1)
                fr = (1 - pct) * 2
                fr = max(min_lev, min(max_lev, fr))
                fracs[i] = fr
                pcts[i] = round(pct * 100, 1)
    return fracs, pcts


# ============ 基础指标 ============
def _sma(values, period):
    n = len(values)
    out = [None] * n
    if period <= 0:
        return out
    s = 0.0
    for i in range(n):
        s += values[i]
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def _ema(values, period):
    n = len(values)
    out = [None] * n
    if n == 0 or period <= 0:
        return out
    alpha = 2.0 / (period + 1)
    ema = values[0]
    out[0] = ema
    for i in range(1, n):
        ema = alpha * values[i] + (1 - alpha) * ema
        out[i] = ema
    return out


def _macd(closes, fast=12, slow=26, signal=9):
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    dea = _ema(dif, signal)
    return dif, dea


def _atr(highs, lows, closes, period):
    n = len(closes)
    out = [None] * n
    if n == 0 or period <= 0:
        return out
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    if n < period:
        return out
    # Wilder 平滑
    atr = sum(tr[:period]) / period
    out[period - 1] = atr
    for i in range(period, n):
        atr = (atr * (period - 1) + tr[i]) / period
        out[i] = atr
    return out


def _rolling_max_prior(values, period):
    """前 period 日(不含当日)的最大值；不足则 None。"""
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i >= period:
            out[i] = max(values[i - period:i])
        elif i >= 1:
            out[i] = max(values[:i])
    return out


def _rolling_min_prior(values, period):
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i >= period:
            out[i] = min(values[i - period:i])
        elif i >= 1:
            out[i] = min(values[:i])
    return out


def _max_drawdown(equity_values):
    peak = float("-inf")
    mdd = 0.0
    for eq in equity_values:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (eq - peak) / peak
            if dd < mdd:
                mdd = dd
    return mdd


def _sharpe(equity_values, periods_per_year=252):
    """由资金曲线计算年化夏普比率(无风险利率取0)。"""
    rets = []
    for i in range(1, len(equity_values)):
        p = equity_values[i - 1]
        if p > 0:
            rets.append(equity_values[i] / p - 1)
    m = len(rets)
    if m < 2:
        return 0.0
    mean = sum(rets) / m
    var = sum((r - mean) ** 2 for r in rets) / m
    sd = var ** 0.5
    if sd == 0:
        return 0.0
    return (mean / sd) * (periods_per_year ** 0.5)


# ============ 入场状态(布尔数组) ============
def _entry_state(spec, ctx):
    """返回某入场策略每日的布尔"状态"数组(满足为True)，买入发生在状态由假转真当天。"""
    t = spec.get("type")
    n = ctx["n"]
    closes = ctx["closes"]
    out = [False] * n

    if t == "ma_golden":
        f = _sma(closes, int(spec.get("fast", 50)))
        s = _sma(closes, int(spec.get("slow", 200)))
        for i in range(n):
            if f[i] is not None and s[i] is not None:
                out[i] = f[i] > s[i]

    elif t == "donchian_breakout":
        p = int(spec.get("period", 20))
        ph = _rolling_max_prior(ctx["highs"], p)
        for i in range(n):
            if ph[i] is not None:
                out[i] = closes[i] > ph[i]

    elif t == "ma_bull_stack":
        periods = spec.get("periods") or [5, 10, 20, 60]
        periods = [int(x) for x in periods]
        mas = [_sma(closes, p) for p in periods]
        for i in range(n):
            vals = [m[i] for m in mas]
            if all(v is not None for v in vals):
                ok = all(vals[k] > vals[k + 1] for k in range(len(vals) - 1))
                out[i] = ok and closes[i] > vals[0]

    elif t == "macd_golden":
        dif, dea = _macd(closes, int(spec.get("fast", 12)), int(spec.get("slow", 26)), int(spec.get("signal", 9)))
        for i in range(n):
            out[i] = dif[i] > dea[i] and dif[i] > 0

    elif t == "volume_breakout":
        p = int(spec.get("period", 20))
        mult = float(spec.get("vol_mult", 1.5))
        ph = _rolling_max_prior(ctx["highs"], p)
        avgv = _sma(ctx["vols"], p)
        for i in range(n):
            if ph[i] is not None and avgv[i] is not None and avgv[i] > 0:
                out[i] = closes[i] > ph[i] and ctx["vols"][i] > mult * avgv[i]

    return out


def _entry_label(spec):
    t = spec.get("type")
    return {
        "ma_golden": f"MA{spec.get('fast',50)}上穿MA{spec.get('slow',200)}金叉",
        "donchian_breakout": f"突破{spec.get('period',20)}日新高",
        "ma_bull_stack": "均线多头排列",
        "macd_golden": "MACD零轴上金叉",
        "volume_breakout": f"量价突破{spec.get('period',20)}日新高",
    }.get(t, t)


# ============ 出场：静态布尔(可预计算) + 动态(依赖持仓) ============
def _static_exit_state(spec, ctx):
    """不依赖持仓的出场，返回每日布尔数组；依赖持仓的返回 None(在循环内计算)。"""
    t = spec.get("type")
    n = ctx["n"]
    closes = ctx["closes"]

    if t == "ma_break":
        ma = _sma(closes, int(spec.get("period", 20)))
        return [(ma[i] is not None and closes[i] < ma[i]) for i in range(n)]

    if t == "donchian_exit":
        p = int(spec.get("period", 10))
        pl = _rolling_min_prior(ctx["lows"], p)
        return [(pl[i] is not None and closes[i] < pl[i]) for i in range(n)]

    if t == "ma_death_cross":
        f = _sma(closes, int(spec.get("fast", 50)))
        s = _sma(closes, int(spec.get("slow", 200)))
        return [(f[i] is not None and s[i] is not None and f[i] < s[i]) for i in range(n)]

    return None  # 动态


def _exit_label(spec, ctx, i):
    t = spec.get("type")
    if t == "ma_break":
        return f"跌破MA{spec.get('period',20)}"
    if t == "donchian_exit":
        return f"跌破{spec.get('period',10)}日新低"
    if t == "ma_death_cross":
        return f"MA{spec.get('fast',50)}下穿MA{spec.get('slow',200)}死叉"
    if t == "chandelier_atr":
        return f"吊灯ATR止损({spec.get('mult',3)}×ATR{spec.get('atr_period',22)})"
    if t == "trailing_pct":
        return f"移动止盈(回撤>{spec.get('pct',10)}%)"
    return t


def run_backtest(df: pd.DataFrame, config: StrategyConfig,
                 initial_capital: float = 100000.0,
                 commission: float = 0.0005,
                 pe_series: dict = None,
                 margin_rate: float = 0.0699) -> dict:
    n = len(df)
    if n == 0:
        return {"ok": False, "error": "该区间无数据"}
    if not config.entries:
        return {"ok": False, "error": "请至少选择一个入场策略"}
    if not config.exits:
        return {"ok": False, "error": "请至少选择一个出场策略"}

    dates = df["date"].tolist()
    opens = df["open"].tolist()
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    vols = df["volume"].tolist() if "volume" in df.columns else [0] * n

    ctx = {"n": n, "opens": opens, "closes": closes, "highs": highs, "lows": lows, "vols": vols}

    # 入场组合状态 -> 上升沿信号
    entry_states = [_entry_state(s, ctx) for s in config.entries]

    def combine(states_at_i, logic):
        if logic == "and":
            return all(states_at_i)
        return any(states_at_i)

    entry_combo = [combine([st[i] for st in entry_states], config.entry_logic) for i in range(n)]
    entry_signal = [False] * n
    for i in range(n):
        prev = entry_combo[i - 1] if i >= 1 else False
        entry_signal[i] = entry_combo[i] and not prev

    # 出场：静态数组 + 动态标记
    static_exits = []     # (spec, bool_array)
    atr_arr = None
    dynamic_specs = []    # specs needing 持仓上下文
    for s in config.exits:
        arr = _static_exit_state(s, ctx)
        if arr is not None:
            static_exits.append((s, arr))
        else:
            dynamic_specs.append(s)
            if s.get("type") == "chandelier_atr" and atr_arr is None:
                atr_arr = _atr(highs, lows, closes, int(s.get("atr_period", 22)))

    # 重新为每个 chandelier 计算各自 atr（可能不同周期）
    atr_cache = {}
    for s in dynamic_specs:
        if s.get("type") == "chandelier_atr":
            ap = int(s.get("atr_period", 22))
            if ap not in atr_cache:
                atr_cache[ap] = _atr(highs, lows, closes, ap)

    cash = float(initial_capital)
    shares = 0.0
    buy_price = 0.0
    entry_date = None
    entry_idx = None
    hh_since = 0.0   # 持仓期间最高价
    hc_since = 0.0   # 持仓期间最高收盘

    # 仓位管理：每个交易日的入场仓位(>1=融资)
    pos_fracs, pe_pcts = _build_position_fractions(config, dates, pe_series)
    # PE 每上升 deleverage_step 个百分位点，重算仓位逐步卸杠杆(0=不启用)
    _pos = config.position or {}
    deleverage_step = float(_pos.get("deleverage_step", 0)) if _pos.get("type") == "pe_percentile" else 0.0

    cur_frac = 0.0       # 本笔展示用(入场)仓位
    live_frac = 0.0      # 本笔当前实际仓位(卸杠杆后会下调)
    anchor_pct = None    # 上次(重)计仓位时的 PE 百分位
    cur_pe_pct = None
    cur_interest = 0.0   # 本笔持仓累计融资利息
    cur_commission = 0.0 # 本笔累计交易手续费(买入+卸杠杆减仓+卖出)
    cur_buy_amount = 0.0 # 本笔买入金额(入场市值)
    cur_entry_equity = 0.0  # 本笔入场前权益(用于算净盈亏金额)
    cur_rebalances = []  # 本笔卸杠杆减仓事件
    total_commission = 0.0
    rebalances = 0       # 卸杠杆次数(全局统计)
    trades, buys, sells, equity_curve = [], [], [], []
    buy_signal_count = sum(1 for x in entry_signal if x)

    # 预计算相邻交易日的日历天数间隔（用于按日计提融资利息）
    parsed_dates = []
    for d in dates:
        try:
            parsed_dates.append(datetime.strptime(d, "%Y-%m-%d"))
        except Exception:
            parsed_dates.append(None)
    day_gaps = [0] * n
    for i in range(1, n):
        if parsed_dates[i] is not None and parsed_dates[i - 1] is not None:
            day_gaps[i] = max((parsed_dates[i] - parsed_dates[i - 1]).days, 0)
        else:
            day_gaps[i] = 1

    daily_rate = margin_rate / 365.0
    total_interest = 0.0

    for i in range(n):
        price = closes[i]

        # 融资利息：现金为负(=借款)时按日历天数计提，利息增加负债
        if cash < 0 and daily_rate > 0 and i >= 1 and day_gaps[i] > 0:
            interest = (-cash) * daily_rate * day_gaps[i]
            cash -= interest
            total_interest += interest
            cur_interest += interest

        if shares <= 0:
            if entry_signal[i]:
                frac = pos_fracs[i]
                equity = cash  # 空仓时权益=现金
                invest = equity * frac
                shares = invest / (price * (1 + commission)) if frac > 0 else 0.0
                if shares > 0:
                    buy_comm = shares * price * commission
                    cash -= shares * price + buy_comm  # frac>1 时 cash 变负=融资
                    cur_interest = 0.0  # 新一笔持仓，利息归零
                    cur_commission = buy_comm
                    total_commission += buy_comm
                    cur_buy_amount = shares * price   # 入场买入市值(含融资部分)
                    cur_entry_equity = equity         # 入场前权益(=买入前现金)
                    cur_rebalances = []
                    buy_price = price
                    entry_date = dates[i]
                    entry_idx = i
                    hh_since = highs[i]
                    hc_since = closes[i]
                    cur_frac = frac
                    live_frac = frac
                    anchor_pct = pe_pcts[i]
                    cur_pe_pct = pe_pcts[i]
                    labels = [_entry_label(config.entries[k]) for k in range(len(entry_states)) if entry_states[k][i]]
                    buys.append({"date": dates[i], "price": round(price, 4), "position": round(frac, 3),
                                 "reason": (" & " if config.entry_logic == "and" else " / ").join(labels)})
        else:
            hh_since = max(hh_since, highs[i])
            hc_since = max(hc_since, closes[i])

            # PE 上升逐步卸杠杆：百分位每比上次重算时上升 deleverage_step 点，按新仓位减仓
            if deleverage_step > 0 and anchor_pct is not None:
                cp = pe_pcts[i]
                if cp is not None and cp >= anchor_pct + deleverage_step:
                    new_frac = pos_fracs[i]  # = clamp((1-PE百分位)*2, min, max)
                    if new_frac < live_frac:
                        equity = cash + shares * price
                        target_val = equity * new_frac
                        cur_val = shares * price
                        if target_val < cur_val and price > 0:
                            sell_sh = (cur_val - target_val) / price
                            if sell_sh > 0:
                                comm = sell_sh * price * commission
                                cash += sell_sh * price - comm
                                shares -= sell_sh
                                cur_commission += comm
                                total_commission += comm
                                rebalances += 1
                                ev = {
                                    "date": dates[i], "price": round(price, 4),
                                    "shares": round(sell_sh, 2), "amount": round(sell_sh * price, 2),
                                    "commission": round(comm, 2), "pe_pct": cp,
                                    "from_pos": round(live_frac, 3), "to_pos": round(new_frac, 3),
                                }
                                cur_rebalances.append(ev)
                                sells.append({"date": dates[i], "price": round(price, 4),
                                              "reason": f"卸杠杆减仓 {round(live_frac,3)}→{round(new_frac,3)}",
                                              "kind": "deleverage"})
                        live_frac = new_frac
                    anchor_pct = cp

            triggered = []   # (spec, True)
            bools = []
            for spec, arr in static_exits:
                b = arr[i]
                bools.append(b)
                if b:
                    triggered.append(spec)
            for spec in dynamic_specs:
                t = spec.get("type")
                b = False
                if t == "chandelier_atr":
                    ap = int(spec.get("atr_period", 22))
                    mult = float(spec.get("mult", 3))
                    a = atr_cache.get(ap, [None] * n)[i]
                    if a is not None:
                        b = price < (hh_since - mult * a)
                elif t == "trailing_pct":
                    pct = float(spec.get("pct", 10))
                    b = price < hc_since * (1 - pct / 100.0)
                bools.append(b)
                if b:
                    triggered.append(spec)

            do_exit = (all(bools) if config.exit_logic == "and" else any(bools)) and len(bools) > 0
            if do_exit:
                sell_comm = shares * price * commission
                cash += shares * price - sell_comm
                cur_commission += sell_comm
                total_commission += sell_comm
                ret = price / buy_price - 1
                reason_specs = config.exits if config.exit_logic == "and" else triggered
                reason = (" & " if config.exit_logic == "and" else " / ").join(_exit_label(s, ctx, i) for s in reason_specs)
                trades.append({
                    "entry_date": entry_date, "entry_price": round(buy_price, 4),
                    "exit_date": dates[i], "exit_price": round(price, 4),
                    "return_pct": round(ret * 100, 2), "holding_days": i - entry_idx,
                    "position": round(cur_frac, 3), "pe_pct": cur_pe_pct,
                    "interest": round(cur_interest, 2), "commission": round(cur_commission, 2),
                    "buy_amount": round(cur_buy_amount, 2), "sell_amount": round(shares * price, 2),
                    "profit": round(cash - cur_entry_equity, 2),
                    "rebalances": cur_rebalances, "reason": reason,
                })
                sells.append({"date": dates[i], "price": round(price, 4), "reason": reason})
                shares = 0.0
                buy_price = 0.0
                cur_interest = 0.0
                cur_commission = 0.0
                cur_buy_amount = 0.0
                cur_rebalances = []
                entry_date = None
                entry_idx = None

        equity_curve.append([dates[i], round(cash + shares * price, 2)])

    # 区间结束仍持仓 -> 末日收盘平仓
    if shares > 0:
        i = n - 1
        price = closes[i]
        sell_comm = shares * price * commission
        cash += shares * price - sell_comm
        cur_commission += sell_comm
        total_commission += sell_comm
        ret = price / buy_price - 1
        trades.append({
            "entry_date": entry_date, "entry_price": round(buy_price, 4),
            "exit_date": dates[i], "exit_price": round(price, 4),
            "return_pct": round(ret * 100, 2), "holding_days": i - entry_idx,
            "position": round(cur_frac, 3), "pe_pct": cur_pe_pct,
            "interest": round(cur_interest, 2), "commission": round(cur_commission, 2),
            "buy_amount": round(cur_buy_amount, 2), "sell_amount": round(shares * price, 2),
            "profit": round((cash) - cur_entry_equity, 2),
            "rebalances": cur_rebalances, "reason": "区间结束平仓",
        })
        sells.append({"date": dates[i], "price": round(price, 4), "reason": "区间结束平仓"})
        shares = 0.0
        equity_curve[-1] = [dates[i], round(cash, 2)]

    final_equity = cash
    total_return = final_equity / initial_capital - 1
    buy_hold_return = closes[-1] / closes[0] - 1
    try:
        d0 = datetime.strptime(dates[0], "%Y-%m-%d")
        d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
        years = max((d1 - d0).days / 365.25, 1e-9)
        annualized = (final_equity / initial_capital) ** (1 / years) - 1
    except Exception:
        annualized = 0.0

    wins = [t for t in trades if t["return_pct"] > 0]
    num_trades = len(trades)
    win_rate = (len(wins) / num_trades) if num_trades else 0.0
    avg_return = (sum(t["return_pct"] for t in trades) / num_trades) if num_trades else 0.0
    eq_vals = [e[1] for e in equity_curve]
    mdd = _max_drawdown(eq_vals)
    sharpe = _sharpe(eq_vals)

    diag_msg = ""
    if num_trades == 0:
        logic = "同时满足(AND)" if config.entry_logic == "and" else "任一满足(OR)"
        diag_msg = f"未触发交易：所选入场条件在本区间内没有出现「{logic}」的买点，可换用OR或调整参数。"

    benchmark = [[dates[i], round(initial_capital * closes[i] / closes[0], 2)] for i in range(n)]
    kline = [[round(opens[i], 4), round(closes[i], 4), round(lows[i], 4), round(highs[i], 4)] for i in range(n)]

    return {
        "ok": True,
        "stats": {
            "initial_capital": round(initial_capital, 2),
            "final_equity": round(final_equity, 2),
            "total_return": round(total_return * 100, 2),
            "annualized": round(annualized * 100, 2),
            "buy_hold_return": round(buy_hold_return * 100, 2),
            "num_trades": num_trades,
            "win_rate": round(win_rate * 100, 2),
            "avg_return": round(avg_return, 2),
            "max_drawdown": round(mdd * 100, 2),
            "sharpe": round(sharpe, 2),
            "margin_rate": round(margin_rate * 100, 3),
            "total_interest": round(total_interest, 2),
            "total_commission": round(total_commission, 2),
            "deleverage_count": rebalances,
        },
        "diagnostics": {"buy_signal_count": buy_signal_count, "message": diag_msg},
        "dates": dates,
        "kline": kline,
        "volumes": [round(v, 2) for v in vols],
        "trades": trades,
        "markers": {"buys": buys, "sells": sells},
        "equity": equity_curve,
        "benchmark": benchmark,
    }


# ============ 批量寻优：跑全部 3249 组合，按基金式综合评分排名 ============
def _side_structs(m):
    """某侧(入场/出场)全部"选择结构"：(选中索引元组, 逻辑)。单选只1种逻辑，多选 or/and 两种。"""
    out = []
    for k in range(1, m + 1):
        for combo in itertools.combinations(range(m), k):
            out.append((combo, "or"))
            if k >= 2:
                out.append((combo, "and"))
    return out


def _struct_label(defaults, idxs, logic):
    sep = "&" if logic == "and" else "/"
    return sep.join(CN_LABEL[defaults[i]["type"]] for i in idxs)


def run_optimization(df: pd.DataFrame, initial_capital: float = 100000.0,
                     commission: float = 0.0003, top_n: int = 10, min_trades: int = 10) -> dict:
    """对全部入场×出场结构(默认参数)做批量回测，返回综合评分 Top N。"""
    n = len(df)
    if n == 0:
        return {"ok": False, "error": "该区间无数据"}

    dates = df["date"].tolist()
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    vols = df["volume"].tolist() if "volume" in df.columns else [0] * n
    ctx = {"n": n, "opens": df["open"].tolist(), "closes": closes, "highs": highs, "lows": lows, "vols": vols}

    try:
        d0 = datetime.strptime(dates[0], "%Y-%m-%d")
        d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
        years = max((d1 - d0).days / 365.25, 1e-9)
    except Exception:
        years = 1.0

    # ---- 预计算入场：每个结构的"买入信号(上升沿)"数组 ----
    entry_states = [_entry_state(s, ctx) for s in ENTRY_DEFAULTS]
    entry_structs = _side_structs(len(ENTRY_DEFAULTS))
    entry_signals = []   # (label, idxs, logic, signal_array)
    for idxs, logic in entry_structs:
        combo = [False] * n
        for i in range(n):
            vals = [entry_states[k][i] for k in idxs]
            combo[i] = all(vals) if logic == "and" else any(vals)
        sig = [False] * n
        for i in range(n):
            sig[i] = combo[i] and (i == 0 or not combo[i - 1])
        entry_signals.append((_struct_label(ENTRY_DEFAULTS, idxs, logic), idxs, logic, sig))

    # ---- 预计算出场静态数组 + ATR ----
    ma_break_ma = _sma(closes, EXIT_DEFAULTS[0]["period"])
    ma_break_arr = [(ma_break_ma[i] is not None and closes[i] < ma_break_ma[i]) for i in range(n)]
    dl = _rolling_min_prior(lows, EXIT_DEFAULTS[3]["period"])
    donch_exit_arr = [(dl[i] is not None and closes[i] < dl[i]) for i in range(n)]
    dcf = _sma(closes, EXIT_DEFAULTS[4]["fast"])
    dcs = _sma(closes, EXIT_DEFAULTS[4]["slow"])
    death_arr = [(dcf[i] is not None and dcs[i] is not None and dcf[i] < dcs[i]) for i in range(n)]
    atr_arr = _atr(highs, lows, closes, EXIT_DEFAULTS[1]["atr_period"])
    atr_mult = float(EXIT_DEFAULTS[1]["mult"])
    trail_pct = float(EXIT_DEFAULTS[2]["pct"]) / 100.0
    exit_structs = _side_structs(len(EXIT_DEFAULTS))

    sqrt252 = 252 ** 0.5

    def simulate(entry_signal, ex_idxs, ex_logic):
        e0, e1, e2, e3, e4 = (0 in ex_idxs), (1 in ex_idxs), (2 in ex_idxs), (3 in ex_idxs), (4 in ex_idxs)
        is_and = ex_logic == "and"
        cash = initial_capital
        shares = 0.0
        buy_price = 0.0
        hh = hc = 0.0
        num_trades = 0
        wins = 0
        prev_eq = initial_capital
        sum_r = sumsq_r = 0.0
        cnt_r = 0
        peak = initial_capital
        mdd = 0.0
        for i in range(n):
            price = closes[i]
            if shares <= 0:
                if entry_signal[i]:
                    shares = cash / (price * (1 + commission))
                    cash = 0.0
                    buy_price = price
                    hh = highs[i]
                    hc = price
            else:
                if highs[i] > hh:
                    hh = highs[i]
                if price > hc:
                    hc = price
                checks = []
                if e0:
                    checks.append(ma_break_arr[i])
                if e1:
                    a = atr_arr[i]
                    checks.append(a is not None and price < hh - atr_mult * a)
                if e2:
                    checks.append(price < hc * (1 - trail_pct))
                if e3:
                    checks.append(donch_exit_arr[i])
                if e4:
                    checks.append(death_arr[i])
                do_exit = (all(checks) if is_and else any(checks)) and len(checks) > 0
                if do_exit:
                    cash += shares * price * (1 - commission)
                    if price > buy_price:
                        wins += 1
                    num_trades += 1
                    shares = 0.0
            eq = cash + shares * price
            if prev_eq > 0:
                r = eq / prev_eq - 1
                sum_r += r
                sumsq_r += r * r
                cnt_r += 1
            prev_eq = eq
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (eq - peak) / peak
                if dd < mdd:
                    mdd = dd
        if shares > 0:
            price = closes[-1]
            cash += shares * price * (1 - commission)
            if price > buy_price:
                wins += 1
            num_trades += 1
            shares = 0.0
        final = cash
        total_return = final / initial_capital - 1
        annualized = (final / initial_capital) ** (1 / years) - 1 if final > 0 else -1.0
        sharpe = 0.0
        if cnt_r >= 2:
            mean = sum_r / cnt_r
            var = sumsq_r / cnt_r - mean * mean
            sd = var ** 0.5 if var > 0 else 0.0
            if sd > 0:
                sharpe = (mean / sd) * sqrt252
        win_rate = (wins / num_trades) if num_trades else 0.0
        return total_return, annualized, mdd, sharpe, num_trades, win_rate

    # ---- 基准(买入持有) ----
    buyhold_total = closes[-1] / closes[0] - 1
    buyhold_ann = (1 + buyhold_total) ** (1 / years) - 1

    rows = []
    for e_label, e_idxs, e_logic, e_sig in entry_signals:
        for x_idxs, x_logic in exit_structs:
            tr, ann, mdd, sharpe, nt, wr = simulate(e_sig, x_idxs, x_logic)
            calmar = ann / abs(mdd) if mdd < 0 else (ann / 1e-9 if ann else 0.0)
            rows.append({
                "entry": e_label, "exit": _struct_label(EXIT_DEFAULTS, x_idxs, x_logic),
                "entry_types": [ENTRY_DEFAULTS[i]["type"] for i in e_idxs], "entry_logic": e_logic,
                "exit_types": [EXIT_DEFAULTS[i]["type"] for i in x_idxs], "exit_logic": x_logic,
                "total_return": round(tr * 100, 2), "annualized": round(ann * 100, 2),
                "excess": round((tr - buyhold_total) * 100, 2),
                "excess_ann": round((ann - buyhold_ann) * 100, 2),
                "max_drawdown": round(mdd * 100, 2), "sharpe": round(sharpe, 3),
                "calmar": round(calmar, 3), "trades": nt, "win_rate": round(wr * 100, 2),
            })

    # ---- 最小原则去重：绩效完全相同的组合，只保留"策略数量最少"的那个 ----
    # （指标全等说明多出的策略在本区间从未触发，属冗余）
    best = {}
    for r in rows:
        sig = (r["total_return"], r["annualized"], r["max_drawdown"],
               r["sharpe"], r["trades"], r["win_rate"])
        comp = len(r["entry_types"]) + len(r["exit_types"])
        rank_key = (comp, len(r["entry_types"]), len(r["exit_types"]), r["entry"], r["exit"])
        cur = best.get(sig)
        if cur is None or rank_key < cur[0]:
            best[sig] = (rank_key, r)
    rows = [v[1] for v in best.values()]

    # ---- 基金式综合评分：在 trades>=min_trades 的样本上做 z-score 加权 ----
    pool = [r for r in rows if r["trades"] >= min_trades] or rows[:]

    def zscores(key):
        xs = [r[key] for r in pool]
        m = sum(xs) / len(xs)
        var = sum((x - m) ** 2 for x in xs) / len(xs)
        sd = var ** 0.5
        return m, (sd if sd > 0 else 1.0)

    m_sh, s_sh = zscores("sharpe")
    m_ca, s_ca = zscores("calmar")
    m_ex, s_ex = zscores("excess_ann")
    m_dd, s_dd = zscores("max_drawdown")  # 越大(越接近0)越好
    W = {"sharpe": 0.35, "calmar": 0.25, "excess": 0.25, "dd": 0.15}
    for r in pool:
        z = (W["sharpe"] * (r["sharpe"] - m_sh) / s_sh
             + W["calmar"] * (r["calmar"] - m_ca) / s_ca
             + W["excess"] * (r["excess_ann"] - m_ex) / s_ex
             + W["dd"] * (r["max_drawdown"] - m_dd) / s_dd)
        r["score"] = round(z, 4)

    pool.sort(key=lambda r: r["score"], reverse=True)

    return {
        "ok": True,
        "total_combos": len(entry_signals) * len(exit_structs),
        "unique_combos": len(rows),
        "scored_pool": len(pool),
        "min_trades": min_trades,
        "benchmark": {"buy_hold_return": round(buyhold_total * 100, 2),
                      "buy_hold_annualized": round(buyhold_ann * 100, 2)},
        "weights": W,
        "top": pool[:top_n],
    }
