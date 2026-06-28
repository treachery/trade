"""本地预热：用富途 OpenD 批量拉取美股个股 PE 历史，落地 data_cache/pe_futu_*.csv。

云端回测无需 OpenD：本脚本在本地(已登录 OpenD)拉取 PE 历史存 CSV，
把 pe_futu_*.csv 同步到云服务器 data_cache/，云端 load_futu_pe_history
检测到无 FUTU_OPEND_HOST 时自动只读这些缓存(离线模式)。

前置：启动 FutuOpenD 并登录富途账号(扫码或账号密码)。
用法:
    FUTU_OPEND_HOST=127.0.0.1 python3 scripts/prefetch_futu_pe.py
    # 也可自定义标的列表:
    FUTU_OPEND_HOST=127.0.0.1 python3 scripts/prefetch_futu_pe.py us105.AAPL us105.TSLA
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest.data import load_futu_pe_history

# 默认常用美股个股(项目代码格式 us+东财代码)
DEFAULT_SYMBOLS = [
    "us105.AAPL", "us105.TSLA", "us105.NVDA", "us105.MSFT", "us105.GOOG",
    "us105.AMZN", "us105.META", "us105.AMD", "us105.NFLX", "us105.INTC",
    "us105.BABA", "us105.JD", "us105.PDD", "us105.BIDU", "us105.NIO",
]


def main():
    host = os.environ.get("FUTU_OPEND_HOST", "").strip()
    if not host:
        print("请先设置 FUTU_OPEND_HOST=127.0.0.1 并启动 FutuOpenD 登录富途账号")
        sys.exit(1)
    symbols = sys.argv[1:] or DEFAULT_SYMBOLS
    print(f"OpenD={host}:{os.environ.get('FUTU_OPEND_PORT','11111')}  待拉取 {len(symbols)} 只\n")
    ok, fail = [], []
    for sym in symbols:
        try:
            # use_cache=False 强制联网拉取并覆盖缓存; interval_type=9=近20年由 load_futu_pe_history 内部指定
            df = load_futu_pe_history(sym, "2006-01-01", "2026-12-31", use_cache=False)
            if df is not None and not df.empty:
                ok.append(f"{sym:14s} {len(df):5d}行 {df['date'].min()}~{df['date'].max()} "
                          f"PE末={round(float(df['pe'].iloc[-1]), 2)}")
            else:
                fail.append(f"{sym}: 返回空(可能无美股权限或代码映射失败)")
        except Exception as e:
            fail.append(f"{sym}: {repr(e)[:120]}")
    print(f"✅ 成功 {len(ok)}:")
    for x in ok:
        print("  " + x)
    if fail:
        print(f"\n❌ 失败 {len(fail)}:")
        for x in fail:
            print("  " + x)
    print("\n缓存已落地 data_cache/pe_futu_*.csv")
    print("同步到云服务器:  git add data_cache/pe_futu_*.csv && git commit && git push")
    print("              或  scp data_cache/pe_futu_*.csv user@server:/app/data_cache/")
    print("云端回测将自动离线读缓存，无需 OpenD/账号。")


if __name__ == "__main__":
    main()
