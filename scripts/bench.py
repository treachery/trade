import sys, os, time, itertools, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest import load_kline, run_backtest, StrategyConfig

SYMBOL, START, END = "399001", "2015-01-01", "2025-10-31"

ENTRY_DEFAULTS = [
    {"type": "ma_golden", "fast": 50, "slow": 200},
    {"type": "donchian_breakout", "period": 20},
    {"type": "ma_bull_stack", "periods": [5, 10, 20, 60]},
    {"type": "macd_golden", "fast": 12, "slow": 26, "signal": 9},
    {"type": "volume_breakout", "period": 20, "vol_mult": 1.5},
]
EXIT_DEFAULTS = [
    {"type": "ma_break", "period": 20},
    {"type": "chandelier_atr", "atr_period": 22, "mult": 3},
    {"type": "trailing_pct", "pct": 10},
    {"type": "donchian_exit", "period": 10},
    {"type": "ma_death_cross", "fast": 50, "slow": 200},
]

CN = {
    "ma_golden": "双均线金叉", "donchian_breakout": "唐奇安突破", "ma_bull_stack": "均线多头排列",
    "macd_golden": "MACD金叉", "volume_breakout": "量价突破",
    "ma_break": "跌破均线", "chandelier_atr": "吊灯ATR", "trailing_pct": "移动止盈",
    "donchian_exit": "唐奇安下轨", "ma_death_cross": "双均线死叉",
}


def side_configs(defaults):
    cfgs = []
    for k in range(1, len(defaults) + 1):
        for combo in itertools.combinations(range(len(defaults)), k):
            specs = [defaults[i] for i in combo]
            cfgs.append((specs, "or"))
            if k >= 2:
                cfgs.append((specs, "and"))
    return cfgs


def name(specs, logic):
    sep = "&" if logic == "and" else "/"
    return sep.join(CN[s["type"]] for s in specs)


df = load_kline(SYMBOL, START, END)
entry_cfgs = side_configs(ENTRY_DEFAULTS)
exit_cfgs = side_configs(EXIT_DEFAULTS)
print(f"{SYMBOL} {START}~{END}  交易日={len(df)}  组合={len(entry_cfgs)*len(exit_cfgs)}")

run_backtest(df, StrategyConfig(entries=[ENTRY_DEFAULTS[0]], exits=[EXIT_DEFAULTS[0]]))  # 预热

t0 = time.perf_counter()
rows = []
for e_specs, e_logic in entry_cfgs:
    for x_specs, x_logic in exit_cfgs:
        cfg = StrategyConfig(entries=e_specs, exits=x_specs, entry_logic=e_logic, exit_logic=x_logic)
        r = run_backtest(df, cfg, initial_capital=100000)
        if not r.get("ok"):
            continue
        s = r["stats"]
        mdd = abs(s["max_drawdown"]) or 1e-9
        calmar = s["annualized"] / mdd
        rows.append({
            "entry": name(e_specs, e_logic), "exit": name(x_specs, x_logic),
            "total_return": s["total_return"], "annualized": s["annualized"],
            "max_dd": s["max_drawdown"], "trades": s["num_trades"],
            "win_rate": s["win_rate"], "calmar": round(calmar, 2),
        })
elapsed = time.perf_counter() - t0
print(f"跑完 {len(rows)} 个，用时 {elapsed:.1f} 秒\n")

# 存 CSV
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results_all.csv")
with open(out, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=["entry", "exit", "total_return", "annualized", "max_dd", "trades", "win_rate", "calmar"])
    w.writeheader()
    w.writerows(rows)
print("全部结果已保存:", out)


def show(title, key, flt=None):
    data = [r for r in rows if (flt(r) if flt else True)]
    data.sort(key=lambda r: r[key], reverse=True)
    print(f"\n=== {title} ===")
    print(f"{'#':>2} {'总回报':>8} {'年化':>7} {'最大回撤':>8} {'卡玛':>6} {'交易':>4} {'胜率':>6}  入场 -> 出场")
    for i, r in enumerate(data[:10], 1):
        print(f"{i:>2} {r['total_return']:>7.1f}% {r['annualized']:>6.1f}% {r['max_dd']:>7.1f}% "
              f"{r['calmar']:>6} {r['trades']:>4} {r['win_rate']:>5.0f}%  {r['entry']} -> {r['exit']}")


buyhold = run_backtest(df, StrategyConfig(entries=[ENTRY_DEFAULTS[0]], exits=[EXIT_DEFAULTS[0]]))["stats"]["buy_hold_return"]
print(f"参考：同期买入持有 = {buyhold}%")
show("Top10 按【总回报】(原始, 可能过拟合)", "total_return")
show("Top10 按【卡玛比率】(年化/回撤, 且交易≥10笔, 更稳健)", "calmar", flt=lambda r: r["trades"] >= 10)
