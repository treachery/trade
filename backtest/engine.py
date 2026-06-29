"""回测引擎：基于可组合的入场/出场策略库逐日撮合，输出交易、买卖点、资金曲线与绩效。

所有指标均基于真实价格(OHLCV)。信号日收盘确认，下一交易日开盘成交；不考虑整手限制(允许碎股)。
"""
import bisect
import itertools
from datetime import datetime

import pandas as pd

from .strategy import StrategyConfig, ENTRY_DEFAULTS, EXIT_DEFAULTS, CN_LABEL


SHORT_HOLDING_DAYS = 3


def _safe_num(v, default=0.0):
    """把 NaN/Inf/None 兜底为 default，避免 jsonify 产生非法 JSON 的 NaN/Infinity。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN 或 ±Inf
        return default
    return f


def _years_ago(date_str, years):
    """date_str 往前推 years 年，返回 'YYYY-MM-DD'；闰日(2/29)退到 2/28。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        try:
            return d.replace(year=d.year - years).strftime("%Y-%m-%d")
        except ValueError:
            return d.replace(year=d.year - years, day=28).strftime("%Y-%m-%d")
    except Exception:
        return None


def _trailing_pe_percentiles(dates, pe_series, window_years=5):
    """对每个回测交易日，计算其 PE 在"近 window_years 年"滚动窗口内的百分位(0~100)。

    pe_series 需包含回测开始前 window_years 年的 PE 历史，区间起点才能正确取分位。
    返回 (pe_aligned, pcts)：pe_aligned 为前向填充到交易日的 PE 值；pcts 为对应近5年分位。
    分位 0% = 近5年最便宜(PE 最低)，100% = 近5年最贵。
    """
    n = len(dates)
    pe_map = pe_series or {}
    items = sorted((d, v) for d, v in pe_map.items() if v is not None and v != 0)
    pe_dates = [d for d, _ in items]
    pe_vals = [v for _, v in items]
    m = len(items)

    pe_aligned = [None] * n
    pcts = [None] * n
    if m == 0:
        return pe_aligned, pcts

    for i, d in enumerate(dates):
        hi = bisect.bisect_right(pe_dates, d)  # 日期 <= d 的条数
        if hi == 0:
            continue
        cur = pe_vals[hi - 1]                  # 当前 PE(前向填充)
        pe_aligned[i] = cur
        ws = _years_ago(d, window_years)
        lo = bisect.bisect_right(pe_dates, ws) if ws else 0  # 窗口 (d-Ny, d]
        if cur <= 0:
            continue
        window = [x for x in pe_vals[lo:hi] if x > 0]
        if not window:
            continue
        sw = sorted(window)
        rank = bisect.bisect_right(sw, cur)
        pcts[i] = round(rank / len(sw) * 100, 1)
    return pe_aligned, pcts


def _build_position_fractions(config, dates, pe_series):
    """返回每个交易日的"建仓仓位"数组(>1 表示融资) 与 近5年PE百分位数组。

    建仓策略(entry)：
      pe_percentile: 仓位 = max_lev − (max_lev−min_lev)×(近5年PE分位/100)。
                     即 分位 0%(最便宜)→最高仓位，分位 100%(最贵)→最低仓位，线性插值。
                     无PE数据时退化为满仓 1.0。
      fixed:         固定满仓 = max_leverage。
    """
    n = len(dates)
    pos = config.position or {}
    entry_type = pos.get("entry", "pe_percentile")
    max_lev = float(pos.get("max_leverage", 2.0))
    min_lev = float(pos.get("min_leverage", 0.5))

    pe_aligned, pcts = _trailing_pe_percentiles(dates, pe_series)

    fracs = [1.0] * n
    for i in range(n):
        if entry_type == "fixed":
            fracs[i] = max_lev
        else:  # pe_percentile
            p = pcts[i]
            fracs[i] = 1.0 if p is None else (max_lev - (max_lev - min_lev) * (p / 100.0))
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

    entry_signal = [False] * n
    if config.entry_logic == "and":
        # AND：要求各子策略"同时满足"才进场。容忍窗口 window>1 时，各策略在
        # [i-window+1, i] 内曾满足即算满足；对合成状态取上升沿。
        window = max(1, getattr(config, "entry_window", 1))
        if window > 1 and len(entry_states) >= 2:
            def _recent_true(state):
                out = [False] * n
                cnt = 0
                for i in range(n):
                    if state[i]:
                        cnt += 1
                    if i >= window and state[i - window]:
                        cnt -= 1
                    out[i] = cnt > 0
                return out
            recents = [_recent_true(st) for st in entry_states]
            entry_combo = [all(r[i] for r in recents) for i in range(n)]
        else:
            entry_combo = [all(st[i] for st in entry_states) for i in range(n)]
        for i in range(n):
            prev = entry_combo[i - 1] if i >= 1 else False
            entry_signal[i] = entry_combo[i] and not prev
    else:
        # OR：任一子策略"各自产生买入信号(各自的上升沿)"即进场。
        # 关键：上升沿在子策略层面分别计算后再取并集，避免某个长期为 True 的子状态
        # (如牛市里 ma_golden 持续金叉)把 OR 合成状态长期顶为 True，从而吞掉其它
        # 子信号(如唐奇安反复突破)的独立触发。
        for st in entry_states:
            for i in range(n):
                prev = st[i - 1] if i >= 1 else False
                if st[i] and not prev:
                    entry_signal[i] = True

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

    # 出场 AND 容忍窗口：各策略在窗口内先后触发即算"同时满足"。
    # exit_window=1 退化为严格同天满足；OR 不受窗口影响。
    # 用每个出场策略"最近一次触发的交易日索引"判定：i - last < window 即视为窗口内仍有效。
    exit_window = max(1, getattr(config, "exit_window", 1)) if config.exit_logic == "and" else 1
    n_exits = len(static_exits) + len(dynamic_specs)
    use_exit_window = exit_window > 1 and n_exits >= 2
    # 每个出场策略最近一次触发日索引(-∞ 表示尚未触发)；顺序=先 static 后 dynamic，与下方 bools 一致
    exit_last_true = [-(10 ** 9)] * n_exits

    cash = float(initial_capital)
    shares = 0.0
    buy_price = 0.0
    entry_date = None
    entry_idx = None
    hh_since = 0.0   # 持仓期间最高价
    hc_since = 0.0   # 持仓期间最高收盘

    # 仓位管理：每个交易日的建仓仓位(>1=融资) + 近5年PE百分位
    pos_fracs, pe_pcts = _build_position_fractions(config, dates, pe_series)
    _pos = config.position or {}
    stop_loss_pct = max(0.0, float(getattr(config, "stop_loss_pct", 10.0) or 0.0))
    _max_lev = float(_pos.get("max_leverage", 2.0))
    _min_lev = float(_pos.get("min_leverage", 0.5))
    reduce_type = _pos.get("reduce", "none")          # none / pe_percentile / profit
    reduce_start = float(_pos.get("reduce_start", 50.0) or 0)
    reduce_step = float(_pos.get("reduce_step", 0) or 0)
    reduce_pct = float(_pos.get("reduce_pct", 0) or 0)

    def _pe_target_frac(p):
        """近5年PE分位 p(0~100) 对应的目标仓位(与建仓PE曲线一致)。"""
        return _max_lev - (_max_lev - _min_lev) * (p / 100.0)

    cur_frac = 0.0       # 本笔展示用(入场)仓位
    live_frac = 0.0      # 本笔当前目标仓位(减仓后会下调)
    pe_steps_done = 0    # PE减仓：本笔已动作的分位档数
    profit_steps_done = 0  # 盈利减仓：本笔已动作的涨幅档数
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

    pending_entry = None   # 收盘确认入场，下一交易日开盘执行
    pending_reduce = None  # 收盘确认减仓，下一交易日开盘执行
    pending_exit = None    # 收盘确认出场/止损，下一交易日开盘执行

    for i in range(n):
        close_price = closes[i]
        open_price = opens[i]
        if open_price is None or open_price <= 0:
            open_price = close_price
        price = close_price

        # 融资利息：现金为负(=借款)时按日历天数计提，利息增加负债
        if cash < 0 and daily_rate > 0 and i >= 1 and day_gaps[i] > 0:
            interest = (-cash) * daily_rate * day_gaps[i]
            cash -= interest
            total_interest += interest
            cur_interest += interest

        entered_today = False

        # 先执行上一交易日收盘后确认的卖出/减仓/买入信号，成交价=今日开盘价。
        if pending_exit is not None and shares > 0:
            exec_price = open_price
            sell_comm = shares * exec_price * commission
            cash += shares * exec_price - sell_comm
            cur_commission += sell_comm
            total_commission += sell_comm
            ret = exec_price / buy_price - 1
            reason = pending_exit.get("reason", "出场")
            trades.append({
                "entry_date": entry_date, "entry_price": round(buy_price, 4),
                "exit_date": dates[i], "exit_price": round(exec_price, 4),
                "return_pct": round(ret * 100, 2), "holding_days": i - entry_idx,
                "position": round(cur_frac, 3), "pe_pct": cur_pe_pct,
                "interest": round(cur_interest, 2), "commission": round(cur_commission, 2),
                "buy_amount": round(cur_buy_amount, 2), "sell_amount": round(shares * exec_price, 2),
                "profit": round(cash - cur_entry_equity, 2),
                "rebalances": cur_rebalances, "reason": reason,
            })
            sells.append({"date": dates[i], "price": round(exec_price, 4), "reason": reason})
            shares = 0.0
            buy_price = 0.0
            cur_interest = 0.0
            cur_commission = 0.0
            cur_buy_amount = 0.0
            cur_rebalances = []
            entry_date = None
            entry_idx = None
            exit_last_true = [-(10 ** 9)] * n_exits
            pending_exit = None
            pending_reduce = None

        if pending_reduce is not None and shares > 0:
            exec_price = open_price
            equity = cash + shares * exec_price
            target_val = equity * pending_reduce["new_frac"]
            cur_val = shares * exec_price
            if target_val < cur_val and exec_price > 0:
                sell_sh = (cur_val - target_val) / exec_price
                if sell_sh > 0:
                    comm = sell_sh * exec_price * commission
                    cash += sell_sh * exec_price - comm
                    shares -= sell_sh
                    cur_commission += comm
                    total_commission += comm
                    rebalances += 1
                    ev = {
                        "date": dates[i], "price": round(exec_price, 4),
                        "shares": round(sell_sh, 2), "amount": round(sell_sh * exec_price, 2),
                        "commission": round(comm, 2),
                        "from_pos": round(pending_reduce["from_frac"], 3),
                        "to_pos": round(pending_reduce["new_frac"], 3),
                    }
                    ev.update(pending_reduce.get("ev_extra") or {})
                    cur_rebalances.append(ev)
                    sells.append({"date": dates[i], "price": round(exec_price, 4),
                                  "reason": pending_reduce.get("reason", "减仓"), "kind": "deleverage"})
                live_frac = pending_reduce["new_frac"]
            pending_reduce = None

        if pending_entry is not None and shares <= 0:
            exec_price = open_price
            frac = pending_entry["frac"]
            equity = cash
            invest = equity * frac
            shares = invest / (exec_price * (1 + commission)) if frac > 0 else 0.0
            if shares > 0:
                buy_comm = shares * exec_price * commission
                cash -= shares * exec_price + buy_comm
                cur_interest = 0.0
                cur_commission = buy_comm
                total_commission += buy_comm
                cur_buy_amount = shares * exec_price
                cur_entry_equity = equity
                cur_rebalances = []
                buy_price = exec_price
                entry_date = dates[i]
                entry_idx = i
                hh_since = highs[i]
                hc_since = closes[i]
                cur_frac = frac
                live_frac = frac
                cur_pe_pct = pending_entry.get("pe_pct")
                pe_steps_done = (int((cur_pe_pct - reduce_start) // reduce_step) + 1
                                 if reduce_step > 0 and cur_pe_pct is not None and cur_pe_pct >= reduce_start else 0)
                profit_steps_done = 0
                buys.append({"date": dates[i], "price": round(exec_price, 4), "position": round(frac, 3),
                             "reason": pending_entry.get("reason", "入场")})
                entered_today = True
            pending_entry = None

        if shares <= 0:
            # 今日收盘后确认入场信号，下一交易日开盘买入；最后一天无法执行则忽略。
            if entry_signal[i] and i + 1 < n:
                frac = pos_fracs[i]
                labels = [_entry_label(config.entries[k]) for k in range(len(entry_states)) if entry_states[k][i]]
                pending_entry = {
                    "frac": frac,
                    "pe_pct": pe_pcts[i],
                    "reason": (" & " if config.entry_logic == "and" else " / ").join(labels),
                }
        else:
            # 入场执行当天不再用同日收盘信号触发出场，保持原回测不做同日进出。
            if not entered_today:
                hh_since = max(hh_since, highs[i])
                hc_since = max(hc_since, closes[i])

                # ===== 减仓策略：今日收盘确认，下一交易日开盘执行 =====
                equity = cash + shares * close_price
                cur_ratio = (shares * close_price / equity) if equity > 0 else 0.0
                new_frac = None
                from_frac = cur_ratio
                ev_extra = {}
                reduce_reason = ""
                if reduce_type == "pe_percentile" and reduce_step > 0:
                    cp = pe_pcts[i]
                    if cp is not None and cp >= reduce_start:
                        steps = int((cp - reduce_start) // reduce_step) + 1
                        if steps > pe_steps_done:
                            pe_steps_done = steps
                            nf = _pe_target_frac(cp)
                            if nf < live_frac:
                                new_frac = nf
                                from_frac = live_frac
                                ev_extra = {"pe_pct": cp}
                                reduce_reason = f"PE减仓 {round(live_frac,3)}→{round(nf,3)}"
                elif reduce_type == "profit" and reduce_step > 0 and reduce_pct > 0 and buy_price > 0:
                    gain_pct = (close_price / buy_price - 1) * 100.0
                    steps = int((gain_pct - reduce_start) // reduce_step) + 1 if gain_pct >= reduce_start else 0
                    if steps > profit_steps_done:
                        new_steps = steps - profit_steps_done
                        profit_steps_done = steps
                        nf = max(cur_ratio - new_steps * (reduce_pct / 100.0), 0.0)
                        if nf < cur_ratio - 1e-9:
                            new_frac = nf
                            from_frac = cur_ratio
                            ev_extra = {"gain": round(gain_pct, 2)}
                            reduce_reason = f"盈利减仓(+{round(gain_pct,1)}%) {round(cur_ratio,3)}→{round(nf,3)}"
                if new_frac is not None and i + 1 < n:
                    pending_reduce = {"new_frac": new_frac, "from_frac": from_frac,
                                      "ev_extra": ev_extra, "reason": reduce_reason}

                triggered = []
                bools = []
                specs_in_order = []
                for spec, arr in static_exits:
                    b = arr[i]
                    bools.append(b)
                    specs_in_order.append(spec)
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
                            b = close_price < (hh_since - mult * a)
                    elif t == "trailing_pct":
                        pct = float(spec.get("pct", 10))
                        b = close_price < hc_since * (1 - pct / 100.0)
                    bools.append(b)
                    specs_in_order.append(spec)
                    if b:
                        triggered.append(spec)

                stop_loss_hit = False
                stop_loss_reason = ""
                if stop_loss_pct > 0 and buy_price > 0:
                    drawdown_from_entry = (close_price / buy_price - 1.0) * 100.0
                    if drawdown_from_entry <= -stop_loss_pct:
                        stop_loss_hit = True
                        stop_loss_reason = f"入场价止损({round(drawdown_from_entry, 2)}%≤-{round(stop_loss_pct, 2)}%)"

                for k, b in enumerate(bools):
                    if b:
                        exit_last_true[k] = i

                if config.exit_logic == "and":
                    if use_exit_window:
                        do_exit = all((i - exit_last_true[k]) < exit_window for k in range(n_exits)) \
                            and n_exits > 0
                    else:
                        do_exit = all(bools) and len(bools) > 0
                else:
                    do_exit = any(bools) and len(bools) > 0
                if stop_loss_hit:
                    do_exit = True
                if do_exit and i + 1 < n:
                    if stop_loss_hit:
                        reason = stop_loss_reason
                    else:
                        reason_specs = config.exits if config.exit_logic == "and" else triggered
                        reason = (" & " if config.exit_logic == "and" else " / ").join(_exit_label(s, ctx, i) for s in reason_specs)
                    pending_exit = {"reason": reason}
                    pending_reduce = None

        equity_curve.append([dates[i], round(cash + shares * close_price, 2)])

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

    num_trades = len(trades)
    short_trades = [t for t in trades if int(t.get("holding_days", 10 ** 9) or 0) <= SHORT_HOLDING_DAYS]
    short_trade_count = len(short_trades)
    short_trade_rate = (short_trade_count / num_trades) if num_trades else 0.0
    # 超短持仓视为无效扰动：持仓 <= SHORT_HOLDING_DAYS 的交易无论盈亏均不计为胜利。
    wins = [t for t in trades
            if t["return_pct"] > 0 and int(t.get("holding_days", 10 ** 9) or 0) > SHORT_HOLDING_DAYS]
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
    kline = [[round(_safe_num(opens[i]), 4), round(_safe_num(closes[i]), 4),
              round(_safe_num(lows[i]), 4), round(_safe_num(highs[i]), 4)] for i in range(n)]

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
            "short_holding_days": SHORT_HOLDING_DAYS,
            "short_trade_count": short_trade_count,
            "short_trade_rate": round(short_trade_rate * 100, 2),
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
        "volumes": [round(_safe_num(v), 2) for v in vols],
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
                     commission: float = 0.0003, top_n: int = 10, min_trades: int = 10,
                     return_basis: str = "excess") -> dict:
    """对全部入场×出场结构(默认参数)做批量回测，返回综合评分 Top N。

    return_basis: 收益评分基准。"excess"=超额年化(相对买入持有)，"annual"=年化收益。默认 excess。
    """
    n = len(df)
    if n == 0:
        return {"ok": False, "error": "该区间无数据"}

    dates = df["date"].tolist()
    opens = df["open"].tolist()
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
    # 各子策略各自的上升沿(用于 OR：任一子信号独立触发即进场)
    state_signals = []
    for st in entry_states:
        ss = [st[i] and (i == 0 or not st[i - 1]) for i in range(n)]
        state_signals.append(ss)
    entry_signals = []   # (label, idxs, logic, signal_array)
    for idxs, logic in entry_structs:
        sig = [False] * n
        if logic == "and":
            # AND：组合状态(同时满足)的上升沿
            combo = [all(entry_states[k][i] for k in idxs) for i in range(n)]
            for i in range(n):
                sig[i] = combo[i] and (i == 0 or not combo[i - 1])
        else:
            # OR：各子策略上升沿取并集，避免长期为真的子状态压制其它子信号
            for i in range(n):
                sig[i] = any(state_signals[k][i] for k in idxs)
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
    stop_loss_pct = 10.0

    def simulate(entry_signal, ex_idxs, ex_logic):
        e0, e1, e2, e3, e4 = (0 in ex_idxs), (1 in ex_idxs), (2 in ex_idxs), (3 in ex_idxs), (4 in ex_idxs)
        is_and = ex_logic == "and"
        cash = initial_capital
        shares = 0.0
        buy_price = 0.0
        entry_idx = None
        hh = hc = 0.0
        num_trades = 0
        short_trades = 0
        wins = 0
        held_days = 0   # 累计持仓的交易日数(用于平均持仓比例)
        prev_eq = initial_capital
        sum_r = sumsq_r = 0.0
        cnt_r = 0
        peak = initial_capital
        mdd = 0.0
        pending_entry = False
        pending_exit = False
        for i in range(n):
            price = closes[i]
            open_price = opens[i] if opens[i] and opens[i] > 0 else price

            if pending_exit and shares > 0:
                cash += shares * open_price * (1 - commission)
                holding_days = i - entry_idx if entry_idx is not None else 0
                if holding_days <= SHORT_HOLDING_DAYS:
                    short_trades += 1
                elif open_price > buy_price:
                    wins += 1
                num_trades += 1
                shares = 0.0
                buy_price = 0.0
                entry_idx = None
                pending_exit = False

            entered_today = False
            if pending_entry and shares <= 0:
                shares = cash / (open_price * (1 + commission))
                cash = 0.0
                buy_price = open_price
                entry_idx = i
                hh = highs[i]
                hc = price
                pending_entry = False
                entered_today = True

            if shares <= 0:
                if entry_signal[i] and i + 1 < n:
                    pending_entry = True
            else:
                if not entered_today:
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
                    if stop_loss_pct > 0 and buy_price > 0 and (price / buy_price - 1) * 100 <= -stop_loss_pct:
                        do_exit = True
                    if do_exit and i + 1 < n:
                        pending_exit = True
            if shares > 0:
                held_days += 1
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
            holding_days = (n - 1) - entry_idx if entry_idx is not None else 0
            if holding_days <= SHORT_HOLDING_DAYS:
                short_trades += 1
            elif price > buy_price:
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
        short_trade_rate = (short_trades / num_trades) if num_trades else 0.0
        avg_position_ratio = (held_days / n) if n else 0.0
        return (total_return, annualized, mdd, sharpe, num_trades, win_rate,
                short_trades, short_trade_rate, avg_position_ratio)

    # ---- 基准(买入持有) ----
    buyhold_total = closes[-1] / closes[0] - 1
    buyhold_ann = (1 + buyhold_total) ** (1 / years) - 1

    rows = []
    for e_label, e_idxs, e_logic, e_sig in entry_signals:
        for x_idxs, x_logic in exit_structs:
            tr, ann, mdd, sharpe, nt, wr, st, sr, apr = simulate(e_sig, x_idxs, x_logic)
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
                "short_holding_days": SHORT_HOLDING_DAYS,
                "short_trades": st, "short_trade_rate": round(sr * 100, 2),
                "avg_position_ratio": round(apr, 4),
            })

    # ---- 最小原则去重：绩效完全相同的组合，只保留"策略数量最少"的那个 ----
    # （指标全等说明多出的策略在本区间从未触发，属冗余）
    best = {}
    for r in rows:
        sig = (r["total_return"], r["annualized"], r["max_drawdown"],
               r["sharpe"], r["trades"], r["win_rate"], r["short_trades"],
               r["short_trade_rate"], r["avg_position_ratio"])
        comp = len(r["entry_types"]) + len(r["exit_types"])
        rank_key = (comp, len(r["entry_types"]), len(r["exit_types"]), r["entry"], r["exit"])
        cur = best.get(sig)
        if cur is None or rank_key < cur[0]:
            best[sig] = (rank_key, r)
    rows = [v[1] for v in best.values()]

    # ===== 第一步：全部策略进入评分（不做准入过滤）=====
    # 评分体系(卡玛/夏普/收益/资金效率×回撤惩罚×超短单惩罚)已能区分优劣；
    # 1笔交易也能赚钱，夏普=0(样本不足)只拉低该项得分但不直接淘汰。
    pool = rows
    fallback_pool = False

    # ===== 第二步：综合评分（绝对锚定 + clamp(-1,1)，仅对合格策略排序）=====
    # 各子项归一到 [-1, 1]（年化/超额年化/回撤在数据里是百分数，计算时先 /100 还原为小数）：
    #   CalmarScore            = clamp(Calmar / 2)
    #   SharpeScore            = clamp(Sharpe / 2)
    #   ReturnScore            = clamp(收益(小数) / 0.15)   ← 收益基准可选 超额年化/年化
    #   CapitalEfficiencyScore = clamp((年化(小数) / 平均持仓比例) / 0.50)  ← 低持仓拿到同样收益→高分
    # 加权分 = 0.35×Calmar + 0.30×Sharpe + 0.20×Return + 0.15×CapitalEfficiency
    # 回撤惩罚   DrawdownPenalty   = max(0.2, (1 - 最大回撤幅度) ** 2)
    # 超短单惩罚 ShortTradePenalty = max(0.5, 1 - 超短单(≤3日)占比)
    # FinalScore = 加权分 × DrawdownPenalty × ShortTradePenalty
    def _clamp(v, lo=-1.0, hi=1.0):
        return max(lo, min(hi, v))

    use_excess = (return_basis != "annual")  # 默认超额年化
    W = {"calmar": 0.35, "sharpe": 0.30, "return": 0.20, "capital_eff": 0.15}
    for r in pool:
        ann_frac = r["annualized"] / 100.0           # 年化收益(小数)
        ret_basis_frac = (r["excess_ann"] / 100.0) if use_excess else ann_frac  # 收益评分用
        avg_pos = r.get("avg_position_ratio") or 0.0  # 平均持仓比例(0~1)
        calmar_score = _clamp(r["calmar"] / 2.0)
        sharpe_score = _clamp(r["sharpe"] / 2.0)
        return_score = _clamp(ret_basis_frac / 0.15)
        # 资金利用效率：年化 / 平均持仓比例(始终用年化，体现单位持仓的赚钱能力)；无持仓记 0
        if avg_pos > 1e-9:
            capital_eff_score = _clamp((ann_frac / avg_pos) / 0.50)
        else:
            capital_eff_score = 0.0
        base_score = (W["calmar"] * calmar_score
                      + W["sharpe"] * sharpe_score
                      + W["return"] * return_score
                      + W["capital_eff"] * capital_eff_score)
        mdd_abs = abs(r["max_drawdown"]) / 100.0     # 最大回撤幅度(小数, >=0)
        drawdown_penalty = max(0.2, (1.0 - mdd_abs) ** 2)
        short_rate = (r.get("short_trade_rate") or 0.0) / 100.0  # 超短单占比(小数)
        short_trade_penalty = max(0.5, 1.0 - short_rate)
        r["return_score"] = round(return_score, 4)
        r["capital_eff_score"] = round(capital_eff_score, 4)
        r["drawdown_penalty"] = round(drawdown_penalty, 4)
        r["short_trade_penalty"] = round(short_trade_penalty, 4)
        r["score"] = round(base_score * drawdown_penalty * short_trade_penalty, 4)

    pool.sort(key=lambda r: r["score"], reverse=True)

    return {
        "ok": True,
        "total_combos": len(entry_signals) * len(exit_structs),
        "unique_combos": len(rows),
        "scored_pool": len(pool),
        "min_trades": 0,
        "return_basis": "annual" if not use_excess else "excess",
        "filters": {"enabled": False, "fallback": False, "window_years": round(years, 2)},
        "benchmark": {"buy_hold_return": round(buyhold_total * 100, 2),
                      "buy_hold_annualized": round(buyhold_ann * 100, 2)},
        "weights": W,
        "top": pool[:top_n],
    }
