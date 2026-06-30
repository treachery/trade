"""候选策略评估引擎（第7层）。

在相似历史片段的前瞻走势上评估候选策略，基于多目标稳健评分排序输出推荐。
包含完整评估指标体系：收益类/胜率类/风险类/效率类/统计类 + Bootstrap置信区间。
"""
import math
import random
import numpy as np


# ===== 策略库（对应文档 6.1 节，≤10个）=====
STRATEGY_LIBRARY = [
    {"id": "DIR-5", "name": "信号后持有5日", "category": "direct", "holding_days": 5,
     "entry": "signal_next_open", "exit": "fixed_days", "params": {"days": 5}},
    {"id": "DIR-10", "name": "信号后持有10日", "category": "direct", "holding_days": 10,
     "entry": "signal_next_open", "exit": "fixed_days", "params": {"days": 10}},
    {"id": "DIR-20", "name": "信号后持有20日", "category": "direct", "holding_days": 20,
     "entry": "signal_next_open", "exit": "fixed_days", "params": {"days": 20}},
    {"id": "DIR-60", "name": "信号后持有60日", "category": "direct", "holding_days": 60,
     "entry": "signal_next_open", "exit": "fixed_days", "params": {"days": 60}},
    {"id": "SL-5", "name": "持有20日+5%止损", "category": "risk", "holding_days": 20,
     "entry": "signal_next_open", "exit": "stop_loss", "params": {"days": 20, "stop_loss_pct": 5.0}},
    {"id": "SL-8", "name": "持有20日+8%止损", "category": "risk", "holding_days": 20,
     "entry": "signal_next_open", "exit": "stop_loss", "params": {"days": 20, "stop_loss_pct": 8.0}},
    {"id": "TRAIL-5", "name": "持有20日+5%移动止盈", "category": "risk", "holding_days": 20,
     "entry": "signal_next_open", "exit": "trailing", "params": {"days": 20, "trail_pct": 5.0}},
    {"id": "TRAIL-10", "name": "持有40日+10%移动止盈", "category": "risk", "holding_days": 40,
     "entry": "signal_next_open", "exit": "trailing", "params": {"days": 40, "trail_pct": 10.0}},
    {"id": "TIME-10", "name": "持有至10日不涨即出", "category": "risk", "holding_days": 10,
     "entry": "signal_next_open", "exit": "time_stop", "params": {"days": 10}},
    {"id": "BUYHOLD-60", "name": "基准：买入持有60日", "category": "benchmark", "holding_days": 60,
     "entry": "signal_next_open", "exit": "fixed_days", "params": {"days": 60}},
]


def _bootstrap_ci(returns, n_boot=1000, ci=0.95):
    """Bootstrap 置信区间(收益均值的95%CI)。"""
    if not returns:
        return (None, None)
    arr = np.array(returns)
    n = len(arr)
    if n < 2:
        return (round(float(arr.mean()), 2), round(float(arr.mean()), 2))
    boots = []
    for _ in range(n_boot):
        sample = arr[np.random.randint(0, n, size=n)]
        boots.append(float(sample.mean()))
    boots.sort()
    alpha = (1 - ci) / 2
    lo = boots[int(n_boot * alpha)]
    hi = boots[int(n_boot * (1 - alpha))]
    return (round(lo, 2), round(hi, 2))


def _evaluate_strategy_on_fragments(strategy, fragments):
    """在相似片段的前瞻走势上评估单个策略。

    fragments: list of {fwd_returns, fwd_path, entry_price, anchor_date}
    每个片段的前瞻走势已预计算(相对于入场价的百分比收益路径)。

    返回完整评估指标 dict。
    """
    p = strategy["params"]
    exit_type = strategy["exit"]
    max_days = p.get("days", 20)
    stop_loss_pct = p.get("stop_loss_pct", 0) / 100.0
    trail_pct = p.get("trail_pct", 0) / 100.0

    returns = []
    weights = []                # 每条 return 对应的 K线相似度权重
    holding_days_list = []
    exit_reasons = []
    anchor_dates = []           # 每笔交易对应的锚定日(用于按时间排序算累计回撤)
    symbols = []                # 每笔交易对应的标的(展示用)
    single_drawdowns = []       # 单笔持有期内的最大回撤(从入场到出场期间的最低点)

    for frag in fragments:
        path = frag.get("fwd_path", [])
        if not path:
            continue
        # 取该片段的相似度作为权重(无则默认1.0等权)
        w = frag.get("similarity")
        try:
            w = float(w) if w is not None else 1.0
        except (TypeError, ValueError):
            w = 1.0
        # 防止负权或异常值；similarity 通常在 [0,1]
        w = max(0.0, w)
        # path[0] = T+1 的收益%, path[1] = T+2, ...
        # 模拟逐日出场
        exit_ret = None
        exit_day = max_days
        reason = f"持有{max_days}日"

        if exit_type == "stop_loss" and stop_loss_pct > 0:
            for d, ret in enumerate(path[:max_days], 1):
                if ret / 100.0 <= -stop_loss_pct:
                    exit_ret = ret
                    exit_day = d
                    reason = f"止损({p.get('stop_loss_pct')}%)"
                    break
            if exit_ret is None:
                exit_ret = path[min(max_days, len(path)) - 1] if path else 0
                exit_day = max_days

        elif exit_type == "trailing" and trail_pct > 0:
            peak = 0.0
            for d, ret in enumerate(path[:max_days], 1):
                if ret > peak:
                    peak = ret
                if (peak - ret) / 100.0 >= trail_pct and peak > 0:
                    exit_ret = ret
                    exit_day = d
                    reason = f"移动止盈(回撤{p.get('trail_pct')}%)"
                    break
            if exit_ret is None:
                exit_ret = path[min(max_days, len(path)) - 1] if path else 0
                exit_day = max_days

        elif exit_type == "time_stop":
            # 持有 days 日，若不涨(收益<=0)则提前出场
            target_day = min(max_days, len(path))
            ret_at_target = path[target_day - 1] if target_day <= len(path) else path[-1]
            if ret_at_target <= 0:
                exit_ret = ret_at_target
                exit_day = target_day
                reason = f"时间止损({max_days}日不涨)"
            else:
                exit_ret = ret_at_target
                exit_day = target_day
                reason = f"持有{max_days}日"

        else:  # fixed_days
            target_day = min(max_days, len(path))
            exit_ret = path[target_day - 1] if target_day <= len(path) and target_day >= 1 else (path[-1] if path else 0)
            exit_day = target_day

        returns.append(exit_ret)
        weights.append(w)
        holding_days_list.append(exit_day)
        exit_reasons.append(reason)
        anchor_dates.append(str(frag.get("anchor_date", "")))
        symbols.append(str(frag.get("symbol", "")))
        # 单笔持有期内的最大回撤：从入场到 exit_day 期间, 路径相对入场点的最低值
        held_path = path[:exit_day] if exit_day > 0 else []
        single_drawdowns.append(min(held_path) if held_path else 0.0)

    if not returns:
        return {"strategy_id": strategy["id"], "strategy_name": strategy["name"],
                "category": strategy["category"], "ok": False, "error": "无可用样本"}

    arr = np.array(returns)
    w_arr = np.array(weights, dtype=float)
    n = len(arr)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = abs(float(np.mean(losses))) if losses else 0.0
    profit_loss_ratio = round(avg_win / avg_loss, 3) if avg_loss > 0 else None

    # 相似度加权平均收益率：sum(w_i * r_i) / sum(w_i)
    # K线越相似的样本对结果的指导意义越大；权重全0则回退到等权均值
    w_sum = float(w_arr.sum())
    if w_sum > 0:
        weighted_mean = float((w_arr * arr).sum() / w_sum)
        # 加权胜率：盈利样本的权重占总权重比例
        win_w = float(w_arr[arr > 0].sum())
        weighted_win_rate = round(win_w / w_sum * 100, 1)
    else:
        weighted_mean = float(arr.mean())
        weighted_win_rate = round(len(wins) / n * 100, 1)

    # ===== 最大回撤：按时间顺序累加 100 笔交易的"模拟账户曲线" =====
    # 思路: 假设过去这些年里, 每次出现相似形态都按此策略做一笔交易,
    #       账户曲线最难看时从历史最高点跌掉了多少。
    # 步骤: 1) 按 anchor_date 升序排序 2) 累加 returns 得 cumulative curve
    #       3) 求曲线相对历史最高点的最大跌幅
    if anchor_dates and any(d for d in anchor_dates):
        # 按 anchor_date 升序索引
        order = sorted(range(n), key=lambda i: anchor_dates[i] or "")
    else:
        order = list(range(n))
    sorted_returns = arr[order]
    sorted_dates = [anchor_dates[i] for i in order]
    sorted_symbols = [symbols[i] for i in order]
    cum_curve = np.cumsum(sorted_returns)        # 累计收益曲线(单位 %)
    # 累计曲线初始点为 0；用 np.maximum.accumulate 找到运行中的历史峰值
    running_peak = np.maximum.accumulate(np.concatenate([[0.0], cum_curve]))[1:]
    drawdown_curve = cum_curve - running_peak    # 每一时刻的回撤(<=0)
    mdd = float(drawdown_curve.min()) if len(drawdown_curve) else 0.0
    # 累计曲线返回给前端画图(往前补一个 0 作起点)
    equity_curve = [0.0] + [round(float(v), 2) for v in cum_curve.tolist()]
    dd_curve = [0.0] + [round(float(v), 2) for v in drawdown_curve.tolist()]

    # 单笔最大回撤：100 笔交易中, 持有期内"最痛"的那笔从入场跌了多少
    single_mdd_min = round(min(single_drawdowns), 2) if single_drawdowns else 0.0
    single_mdd_avg = round(float(np.mean(single_drawdowns)), 2) if single_drawdowns else 0.0

    # 最差20%分位
    q20 = float(np.percentile(arr, 20)) if n >= 5 else float(min(returns))

    # Bootstrap CI
    boot_lo, boot_hi = _bootstrap_ci(returns)

    # 偏度/峰度
    skewness = round(float(_skewness(arr)), 3) if n >= 3 else 0
    kurt = round(float(_kurtosis(arr)), 3) if n >= 4 else 0

    # 年化夏普(假设252交易日，日收益≈持有期收益/持有天数)
    avg_hold = float(np.mean(holding_days_list)) if holding_days_list else max_days
    if avg_hold > 0 and np.std(arr) > 0:
        sharpe = round(float(np.mean(arr) / np.std(arr) * math.sqrt(252 / avg_hold)), 3)
    else:
        sharpe = 0.0

    # 卡玛比率
    calmar = round(float(np.median(arr) / abs(mdd)), 3) if mdd != 0 else 0.0

    return {
        "strategy_id": strategy["id"],
        "strategy_name": strategy["name"],
        "category": strategy["category"],
        "ok": True,
        "sample_count": n,
        "mean_return": round(float(arr.mean()), 2),
        "weighted_mean_return": round(weighted_mean, 2),     # 按K线相似度加权的平均收益率
        "weighted_win_rate": weighted_win_rate,              # 按相似度加权的胜率
        "median_return": round(float(np.median(arr)), 2),
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_loss_ratio": profit_loss_ratio,
        "max_single_loss": round(float(arr.min()), 2),
        "worst_quantile_20": round(q20, 2),
        "max_drawdown": round(mdd, 2),                       # 累计账户曲线的最大回撤(已按时间排序)
        "single_trade_mdd_min": single_mdd_min,              # 最痛单笔交易持有期跌幅
        "single_trade_mdd_avg": single_mdd_avg,              # 单笔平均回撤
        "sharpe": sharpe,
        "calmar": calmar,
        "avg_holding_days": round(avg_hold, 1),
        "bootstrap_lower": boot_lo,
        "bootstrap_upper": boot_hi,
        "skewness": skewness,
        "kurtosis": kurt,
        "exit_reasons": exit_reasons[:5],  # 展示前5个出场原因
        "returns": returns,  # 等权样本(原始顺序,按相似度)
        # 按时间排序的样本 + 累计曲线(前端画图用)
        "sorted_dates": sorted_dates,
        "sorted_symbols": sorted_symbols,
        "sorted_returns": [round(float(v), 2) for v in sorted_returns.tolist()],
        "equity_curve": equity_curve,           # 累计收益曲线 (起点 0)
        "drawdown_curve": dd_curve,             # 回撤曲线 (起点 0, 之后 <= 0)
    }


def _skewness(arr):
    n = len(arr)
    if n < 3:
        return 0.0
    m = arr.mean()
    s = arr.std()
    if s == 0:
        return 0.0
    return float(np.sum(((arr - m) / s) ** 3) / n)


def _kurtosis(arr):
    n = len(arr)
    if n < 4:
        return 0.0
    m = arr.mean()
    s = arr.std()
    if s == 0:
        return 0.0
    return float(np.sum(((arr - m) / s) ** 4) / n - 3)


def _robust_score(ev, all_median_ranks=None, all_win_ranks=None):
    """多目标稳健评分（对应文档 6.3 节）。

    综合评分 = 中位数收益排名×25% + 胜率排名×20% + 盈亏比排名×15%
             + 最差20%分位收益排名×20% + 卡玛比率排名×10% + Bootstrap稳定性排名×10%
             - 样本数不足惩罚 - 参数复杂度惩罚 - 近期失效惩罚
    """
    if not ev.get("ok"):
        return -999.0

    n = ev["sample_count"]
    # 样本数惩罚
    if n < 30:
        sample_penalty = -0.5
    elif n < 100:
        sample_penalty = -0.2
    else:
        sample_penalty = 0.0

    # Bootstrap稳定性：CI下界>0 为正
    boot_lo = ev.get("bootstrap_lower")
    boot_stability = 1.0 if (boot_lo is not None and boot_lo > 0) else (0.3 if boot_lo is not None else 0.5)

    # 各指标(用于排名的原始值)
    median_ret = ev["median_return"]
    win_rate = ev["win_rate"]
    pl_ratio = ev.get("profit_loss_ratio") or 0
    worst_q20 = ev["worst_quantile_20"]
    calmar = ev["calmar"]

    # 基准分(绝对值评分，而非纯排名，避免少量策略时排名区分度低)
    # 中位数收益：每1% → 0.02分
    s_median = min(max(median_ret / 100.0 * 2.0, -1), 1)
    # 胜率：50% → 0, 100% → 1, 0% → -1
    s_win = min(max((win_rate - 50) / 50, -1), 1)
    # 盈亏比：1.0 → 0, 3.0 → 1
    s_pl = min(max((pl_ratio - 1) / 2, -1), 1)
    # 最差20%分位：0% → 0, -10% → -0.5
    s_q20 = min(max(worst_q20 / 100.0 * 5, -1), 1)
    # 卡玛：0 → 0, 2 → 1
    s_calmar = min(max(calmar / 2, -1), 1)
    # Bootstrap稳定性
    s_boot = boot_stability

    # 复杂度惩罚：非benchmark类别每多一个参数 -0.05
    complexity_penalty = -0.05 if ev["category"] not in ("direct", "benchmark") else 0.0

    score = (0.25 * s_median + 0.20 * s_win + 0.15 * s_pl
             + 0.20 * s_q20 + 0.10 * s_calmar + 0.10 * s_boot
             + sample_penalty + complexity_penalty)
    return round(score, 4)


def evaluate_strategies(fragments):
    """在相似片段上评估全部候选策略并排序。

    fragments: list of {fwd_path, fwd_returns, entry_price, anchor_date, similarity}
    返回 {strategies, best_strategy, summary}
    """
    if not fragments:
        return {"ok": False, "error": "无相似片段可供评估"}

    results = []
    for strat in STRATEGY_LIBRARY:
        ev = _evaluate_strategy_on_fragments(strat, fragments)
        if ev.get("ok"):
            ev["robust_score"] = _robust_score(ev)
        results.append(ev)

    # 排序：按稳健评分降序
    valid = [r for r in results if r.get("ok")]
    valid.sort(key=lambda r: r.get("robust_score", -999), reverse=True)

    # 置信度标注
    for r in valid:
        n = r["sample_count"]
        if n < 30:
            r["confidence"] = "LOW"
            r["confidence_note"] = "样本不足30，置信度低，仅供参考"
        elif n < 100:
            r["confidence"] = "MED"
            r["confidence_note"] = "样本30-100，中等置信度"
        else:
            r["confidence"] = "HIGH"
            r["confidence_note"] = "样本充足(>100)，高置信度"

    best = valid[0] if valid else None

    # 汇总统计
    sample_count = len(fragments)

    def _pair(key):
        """提取 (return, similarity) 对，过滤 None。"""
        out = []
        for f in fragments:
            r = f.get("fwd_returns", {}).get(key)
            if r is None:
                continue
            try:
                s = float(f.get("similarity") or 0.0)
            except (TypeError, ValueError):
                s = 0.0
            out.append((float(r), max(0.0, s)))
        return out

    def _stats(pairs):
        if not pairs:
            return None
        arr = np.array([r for r, _ in pairs])
        w = np.array([s for _, s in pairs], dtype=float)
        wins = arr[arr > 0]
        w_sum = float(w.sum())
        if w_sum > 0:
            w_mean = float((w * arr).sum() / w_sum)
            w_win = round(float(w[arr > 0].sum()) / w_sum * 100, 1)
        else:
            w_mean = float(arr.mean())
            w_win = round(len(wins) / len(arr) * 100, 1)
        return {
            "mean": round(float(arr.mean()), 2),
            "weighted_mean": round(w_mean, 2),               # 按K线相似度加权的均值
            "weighted_win_rate": w_win,                       # 按相似度加权的胜率
            "median": round(float(np.median(arr)), 2),
            "win_rate": round(len(wins) / len(arr) * 100, 1),
            "min": round(float(arr.min()), 2),
            "max": round(float(arr.max()), 2),
            "std": round(float(arr.std()), 2),
        }

    summary = {
        "sample_count": sample_count,
        "buy_hold_stats": {
            "r_5d": _stats(_pair("r_5d")),
            "r_10d": _stats(_pair("r_10d")),
            "r_20d": _stats(_pair("r_20d")),
            "r_40d": _stats(_pair("r_40d")),
            "r_60d": _stats(_pair("r_60d")),
        },
    }

    return {
        "ok": True,
        "strategies": valid,
        "best_strategy": best,
        "summary": summary,
        "strategy_count": len(valid),
    }
