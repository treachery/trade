"""命令行快速验证：对默认示例跑一次回测并打印结果。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import load_kline, run_backtest, StrategyParams

df = load_kline("000001", "2015-03-01", "2018-03-01", adjust="qfq")
print("数据行数:", len(df))
print(df.head(2).to_string())

params = StrategyParams(prior_down_days=5, buy_gain_pct=1.0, sell_consecutive_down=3)
res = run_backtest(df, params, initial_capital=100000)
print("\n=== 绩效 ===")
for k, v in res["stats"].items():
    print(f"{k}: {v}")
print("\n=== 交易明细 ===")
for i, t in enumerate(res["trades"], 1):
    print(i, t)
print("\n买入点数:", len(res["markers"]["buys"]), " 卖出点数:", len(res["markers"]["sells"]))
