"""预下载10年K线+PE数据到本地缓存（data_cache/）。

覆盖范围：
  1. 主要指数（A500/沪深300/上证/深证/恒生/恒生科技/标普500/纳指100）K线+PE
  2. A500全部成分股 K线+PE
  3. 标普500全部成分股 K线+PE
  4. 港股1000亿流通市值以上 K线+PE

用法：
  python scripts/preload_data.py              # 全部下载
  python scripts/preload_data.py --indices    # 只下载指数
  python scripts/preload_data.py --a500       # 只下载A500成分股
  python scripts/preload_data.py --sp500      # 只下载标普500成分股
  python scripts/preload_data.py --hk         # 只下载港股大市值
  python scripts/preload_data.py --workers 16 # 指定线程数

下载后所有功能自动优先读缓存，运行时只拉增量数据。
"""
import os
import sys
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# 项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.data import load_kline, load_valuation_pe
from backtest.scanner import get_a500_constituents, get_sp500_constituents, get_hk_large_caps

# 10年日期范围
END = datetime.now().strftime("%Y-%m-%d")
START = (datetime.now() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")

# 主要指数
INDICES = [
    ("sh000510", "中证A500", "cn"),
    ("sh000300", "沪深300", "cn"),
    ("sh000001", "上证指数", "cn"),
    ("sz399001", "深证成指", "cn"),
    ("sz399006", "创业板指", "cn"),
    ("hkHSI", "恒生指数", "hk"),
    ("hkHSTECH", "恒生科技", "hk"),
    ("us.SPX", "标普500", "us"),
    ("us.NDX", "纳指100", "us"),
]


def preload_kline(symbol, adjust="qfq"):
    """下载K线并缓存。返回 (symbol, rows, ok, error)"""
    try:
        df = load_kline(symbol, START, END, adjust=adjust, use_cache=True)
        if df is not None and not df.empty:
            return (symbol, len(df), True, None)
        return (symbol, 0, False, "empty")
    except Exception as e:
        return (symbol, 0, False, str(e)[:80])


def preload_pe(symbol):
    """下载PE并缓存。返回 (symbol, ok, error)"""
    try:
        df = load_valuation_pe(symbol, START, END, use_cache=True)
        if df is not None and not df.empty:
            return (symbol, True, None)
        return (symbol, False, "no pe data")
    except Exception as e:
        return (symbol, False, str(e)[:80])


def batch_preload(symbols, label, workers=8, download_pe=True):
    """批量下载K线+PE。symbols: [(code, name, market), ...]"""
    total = len(symbols)
    print(f"\n{'='*60}")
    print(f"[{label}] 共 {total} 只标的, {workers} 线程并行下载")
    print(f"日期范围: {START} ~ {END}")
    print(f"{'='*60}")

    # K线下载
    t0 = time.time()
    ok_count = 0
    fail_count = 0
    fail_list = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(preload_kline, s[0]): s for s in symbols}
        done = 0
        for future in as_completed(futures):
            s = futures[future]
            symbol, rows, ok, err = future.result()
            done += 1
            if ok:
                ok_count += 1
                if done % 50 == 0 or done == total:
                    print(f"  K线进度: {done}/{total} | 成功:{ok_count} 失败:{fail_count} | 最新:{symbol} ({rows}行)")
            else:
                fail_count += 1
                fail_list.append((symbol, err))

    t1 = time.time()
    print(f"K线完成: 成功{ok_count} 失败{fail_count} 耗时{t1-t0:.0f}s")
    if fail_list:
        print(f"  失败列表(前10): {fail_list[:10]}")

    # PE下载
    if download_pe:
        pe_ok = 0
        pe_fail = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(preload_pe, s[0]): s for s in symbols}
            done = 0
            for future in as_completed(futures):
                symbol, ok, err = future.result()
                done += 1
                if ok:
                    pe_ok += 1
                else:
                    pe_fail += 1
                if done % 50 == 0 or done == total:
                    print(f"  PE进度: {done}/{total} | 成功:{pe_ok} 失败:{pe_fail}")
        t2 = time.time()
        print(f"PE完成: 成功{pe_ok} 失败:{pe_fail} 耗时{t2-t1:.0f}s")

    print(f"[{label}] 总耗时 {time.time()-t0:.0f}s")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="预下载10年K线+PE数据")
    parser.add_argument("--indices", action="store_true", help="只下载主要指数")
    parser.add_argument("--a500", action="store_true", help="只下载A500成分股")
    parser.add_argument("--sp500", action="store_true", help="只下载标普500成分股")
    parser.add_argument("--hk", action="store_true", help="只下载港股大市值")
    parser.add_argument("--no-pe", action="store_true", help="跳过PE下载")
    parser.add_argument("--workers", type=int, default=8, help="并行线程数(默认8)")
    args = parser.parse_args()

    download_pe = not args.no_pe
    workers = args.workers

    # 如果没有指定任何选项，下载全部
    all_tasks = not any([args.indices, args.a500, args.sp500, args.hk])

    print(f"预下载10年数据到 data_cache/")
    print(f"线程数: {workers} | 下载PE: {download_pe}")

    # 1. 主要指数
    if all_tasks or args.indices:
        batch_preload(INDICES, "主要指数", workers=workers, download_pe=download_pe)

    # 2. A500成分股
    if all_tasks or args.a500:
        print("\n获取A500成分股列表...")
        a500 = get_a500_constituents()
        if a500:
            a500_list = [(c, n, "cn") for c, n in a500]
            batch_preload(a500_list, "A500成分股", workers=workers, download_pe=download_pe)
        else:
            print("A500成分股列表获取失败")

    # 3. 标普500成分股
    if all_tasks or args.sp500:
        print("\n获取标普500成分股列表...")
        sp500 = get_sp500_constituents()
        if sp500:
            sp500_list = [(c, n, "us") for c, n in sp500]
            batch_preload(sp500_list, "标普500成分股", workers=workers, download_pe=download_pe)
        else:
            print("标普500成分股列表获取失败")

    # 4. 港股大市值
    if all_tasks or args.hk:
        print("\n获取港股大市值列表(流通市值≥1000亿)...")
        hk = get_hk_large_caps()
        if hk:
            hk_list = [(c, n, "hk") for c, n in hk]
            batch_preload(hk_list, "港股大市值", workers=workers, download_pe=download_pe)
        else:
            print("港股大市值列表获取失败")

    # 统计缓存
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache")
    csv_count = len([f for f in os.listdir(cache_dir) if f.endswith(".csv")])
    print(f"\n{'='*60}")
    print(f"全部完成! data_cache/ 下共有 {csv_count} 个CSV文件")
    print(f"后续运行时自动优先读缓存，只拉增量数据")


if __name__ == "__main__":
    main()
