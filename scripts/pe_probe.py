"""测 csindex 估值历史 + 创业板50 作为创业板指代理的相关性。"""
import akshare as ak

print("== csindex 399006 ==")
try:
    df = ak.stock_zh_index_value_csindex(symbol="399006")
    print("行数:", len(df), "列:", list(df.columns))
    if len(df):
        print(df.head(2).to_string())
        print(df.tail(2).to_string())
except Exception as e:
    print("FAIL:", type(e).__name__, e)

print("\n== 创业板50 lg PE 近年 ==")
try:
    raw = ak.stock_index_pe_lg(symbol="创业板50")
    col = "滚动市盈率" if "滚动市盈率" in raw.columns else raw.columns[-1]
    print("行数:", len(raw), "日期范围:", raw["日期"].min(), "~", raw["日期"].max())
    print(raw[["日期", col]].tail(3).to_string())
except Exception as e:
    print("FAIL:", e)
