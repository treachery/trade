"""验证：缓存有新交易日时自动增量更新到最新。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from backtest.data import load_kline, _cache_path

sym, adj = "sh000300", "qfq"
# 1) 先确保有缓存
df = load_kline(sym, "2024-01-01", "2026-12-31", adjust=adj)
print("初次加载行数:", len(df), "最新日:", df["date"].max())

# 2) 人为把缓存截断到一个较早日期，模拟"上次只拉到旧数据"
path = _cache_path(sym, adj)
full = pd.read_csv(path, dtype={"date": str})
truncated = full[full["date"] <= "2026-05-01"].copy()
truncated.to_csv(path, index=False)
print("\n已把缓存截断到:", truncated["date"].max(), "(模拟上周的数据)")

# 3) 再次请求(结束日=今天)，应自动增量拉到最新
df2 = load_kline(sym, "2024-01-01", "2026-12-31", adjust=adj)
print("再次加载行数:", len(df2), "最新日:", df2["date"].max())
print("是否已自动更新到最新:", df2["date"].max() > "2026-05-01")
