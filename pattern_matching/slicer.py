"""K线片段切片与标准化（第5层）——支持多股票。

滑动窗口：250日窗口，步长20日。对数收益率 + z-score标准化。
性能说明：每只股票先预计算 RSI/MA/波动率等序列，再对窗口取值，避免每个窗口重复计算整条序列。
"""
import math
import os
import time
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

WINDOW_LOOKBACK = 250
SLIDE_STEP = 1   # 每个交易日采一个锚点(原来20跳过了95%候选,改为1后池子≈120万)
FORWARD_DAYS = 60


def _sma_np(values, period):
    arr = np.asarray(values, dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    if len(arr) < period:
        return out
    cs = np.cumsum(np.insert(arr, 0, 0.0))
    out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def _rsi_np(closes, period=14):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = np.full(n, np.nan, dtype=float)
    if n < period + 1:
        return out
    diff = np.diff(closes)
    gains = np.maximum(diff, 0.0)
    losses = np.maximum(-diff, 0.0)
    avg_g = np.mean(gains[:period])
    avg_l = np.mean(losses[:period])
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def _rolling_volatility_np(closes, period):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = np.full(n, np.nan, dtype=float)
    if n < period:
        return out
    rets = np.zeros(n, dtype=float)
    prev = closes[:-1]
    valid = prev > 0
    rets[1:][valid] = closes[1:][valid] / prev[valid] - 1
    cs = np.cumsum(np.insert(rets, 0, 0.0))
    cs2 = np.cumsum(np.insert(rets * rets, 0, 0.0))
    mean = (cs[period:] - cs[:-period]) / period
    mean2 = (cs2[period:] - cs2[:-period]) / period
    var = np.maximum(mean2 - mean * mean, 0.0)
    out[period - 1:] = np.sqrt(var)
    return out


def _rolling_mean_std_np(values, period):
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    mean = np.full(n, np.nan, dtype=float)
    std = np.full(n, np.nan, dtype=float)
    if n < period:
        return mean, std
    cs = np.cumsum(np.insert(arr, 0, 0.0))
    cs2 = np.cumsum(np.insert(arr * arr, 0, 0.0))
    m = (cs[period:] - cs[:-period]) / period
    m2 = (cs2[period:] - cs2[:-period]) / period
    mean[period - 1:] = m
    std[period - 1:] = np.sqrt(np.maximum(m2 - m * m, 0.0))
    return mean, std


def _percentile_rank_np(values):
    arr = np.asarray(values, dtype=float)
    out = np.full(len(arr), 0.5, dtype=float)
    valid = ~np.isnan(arr)
    vals = arr[valid]
    if len(vals) == 0:
        return out
    order = np.argsort(vals)
    ranks = np.empty(len(vals), dtype=float)
    ranks[order] = np.arange(1, len(vals) + 1, dtype=float)
    out[valid] = ranks / len(vals)
    return out


def _shape_vec_from_np(closes_np, idx, days, points):
    if idx < days or closes_np[idx] <= 0:
        return None
    seg = closes_np[idx - days:idx + 1]
    if np.any(seg <= 0):
        return None
    rel = np.log(seg / closes_np[idx])
    xs = np.linspace(0, len(rel) - 1, points)
    return np.interp(xs, np.arange(len(rel)), rel).tolist()


def _rolling_max_prior_np(values, period):
    """前period日(不含当日)的最高值，对齐 backtest 的突破口径。"""
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    for i in range(period, n):
        out[i] = np.max(arr[i - period:i])
    return out


# 信号位掩码：用于锚点处信号匹配（与 signals.py 的核心买入信号对齐）
SIGNAL_BITS = [
    "breakout_20d", "breakout_60d", "breakout_120d",
    "bb_breakout", "vol_breakout",
    "ma_golden_20_60", "ma_bull_stack",
]
N_SIGNAL_BITS = len(SIGNAL_BITS)


def _precompute_indicators(closes, highs, lows, vols):
    closes_np = np.asarray(closes, dtype=float)
    highs_np = np.asarray(highs, dtype=float)
    lows_np = np.asarray(lows, dtype=float)
    vols_np = np.asarray(vols, dtype=float)
    vmean20, vstd20 = _rolling_mean_std_np(vols_np, 20)
    v60 = _rolling_volatility_np(closes_np, 60)
    up = np.zeros(len(closes_np), dtype=float)
    up[1:] = (closes_np[1:] > closes_np[:-1]).astype(float)
    up_cs = np.cumsum(np.insert(up, 0, 0.0))
    up20 = np.full(len(closes_np), np.nan, dtype=float)
    if len(closes_np) >= 20:
        up20[19:] = (up_cs[20:] - up_cs[:-20]) / 20.0
    ma20 = _sma_np(closes_np, 20)
    bb_std = np.full(len(closes_np), np.nan, dtype=float)
    _, bbstd = _rolling_mean_std_np(closes_np, 20)
    bb_upper = ma20 + 2.0 * bbstd
    return {
        "closes": closes_np,
        "highs": highs_np,
        "lows": lows_np,
        "vols": vols_np,
        "rsi14": _rsi_np(closes_np, 14),
        "ma5": _sma_np(closes_np, 5),
        "ma10": _sma_np(closes_np, 10),
        "ma20": ma20,
        "ma60": _sma_np(closes_np, 60),
        "vol20": _rolling_volatility_np(closes_np, 20),
        "vol40": _rolling_volatility_np(closes_np, 40),
        "vol60": v60,
        "vpct60": _percentile_rank_np(v60),
        "vmean20": vmean20,
        "vstd20": vstd20,
        "vma20": _sma_np(vols_np, 20),
        "up20": up20,
        "ph20": _rolling_max_prior_np(highs_np, 20),
        "ph60": _rolling_max_prior_np(highs_np, 60),
        "ph120": _rolling_max_prior_np(highs_np, 120),
        "bb_upper": bb_upper,
    }


def _signal_mask_at(pre, idx):
    """返回锚点idx处的信号位向量(0/1)，与 SIGNAL_BITS 对齐。"""
    closes = pre["closes"]
    c = closes[idx]
    bits = [0] * N_SIGNAL_BITS
    if c <= 0:
        return bits
    # 突破 20/60/120 日新高
    for k, ph in enumerate([pre["ph20"], pre["ph60"], pre["ph120"]]):
        if not np.isnan(ph[idx]) and c > ph[idx]:
            bits[k] = 1
    # 布林带上轨突破
    if not np.isnan(pre["bb_upper"][idx]) and c > pre["bb_upper"][idx]:
        bits[3] = 1
    # 量价突破：突破20日新高 + 放量
    vma = pre["vma20"][idx]
    if (not np.isnan(pre["ph20"][idx]) and c > pre["ph20"][idx]
            and not np.isnan(vma) and vma > 0 and pre["vols"][idx] > 1.5 * vma):
        bits[4] = 1
    # MA20 上穿 MA60（金叉）
    ma20, ma60 = pre["ma20"], pre["ma60"]
    if idx >= 1 and not np.isnan(ma20[idx]) and not np.isnan(ma60[idx]):
        prev_below = (np.isnan(ma20[idx-1]) or np.isnan(ma60[idx-1]) or ma20[idx-1] <= ma60[idx-1])
        if ma20[idx] > ma60[idx] and prev_below:
            bits[5] = 1
    # 多头排列
    ma5, ma10 = pre["ma5"], pre["ma10"]
    if all(not np.isnan(m[idx]) for m in [ma5, ma10, ma20, ma60]):
        if ma5[idx] > ma10[idx] > ma20[idx] > ma60[idx] and c > ma5[idx]:
            bits[6] = 1
    return bits


def _features_at(pre, idx):
    closes = pre["closes"]
    highs = pre["highs"]
    lows = pre["lows"]
    vols = pre["vols"]
    if idx < 120 or idx >= len(closes):
        return None
    c = closes[idx]
    if c <= 0:
        return None

    # 形态向量：5/10/20/30 天（用于4级递进过滤）
    # 采样点数大致与天数成比例,保证短期片段不失真
    sh5 = _shape_vec_from_np(closes, idx, 5, 6)
    sh10 = _shape_vec_from_np(closes, idx, 10, 12)
    sh20 = _shape_vec_from_np(closes, idx, 20, 24)
    sh30 = _shape_vec_from_np(closes, idx, 30, 30)
    # 兼容字段：保留 shape40/shape60 供环境重排参考(可选,但不再用于递进)
    sh40 = _shape_vec_from_np(closes, idx, 40, 32)
    sh60 = _shape_vec_from_np(closes, idx, 60, 40)
    if sh5 is None or sh10 is None or sh20 is None or sh30 is None or sh40 is None or sh60 is None:
        return None

    r5 = (c / closes[idx - 5] - 1) if closes[idx - 5] > 0 else 0
    r10 = (c / closes[idx - 10] - 1) if closes[idx - 10] > 0 else 0
    r20 = (c / closes[idx - 20] - 1) if closes[idx - 20] > 0 else 0
    vs = pre["vstd20"][idx]
    vm = pre["vmean20"][idx]
    vz = (vols[idx] - vm) / vs if not np.isnan(vs) and vs > 0 else 0
    ph20 = np.max(highs[max(0, idx - 20):idx])
    bs = (c / ph20 - 1) if ph20 > 0 else 0
    rsi = pre["rsi14"][idx]
    rv = rsi / 100.0 if not np.isnan(rsi) else 0.5
    t20 = [float(r5), float(r10), float(r20), float(vz), float(bs), float(rv)]

    hi20 = np.max(highs[idx - 19:idx + 1])
    lo20 = np.min(lows[idx - 19:idx + 1])
    dd20 = (c / hi20 - 1) if hi20 > 0 else 0
    ma20 = pre["ma20"]
    ma60 = pre["ma60"]
    ms20 = ((ma20[idx] - ma20[idx - 5]) / ma20[idx - 5]) if (not np.isnan(ma20[idx]) and not np.isnan(ma20[idx - 5]) and ma20[idx - 5] > 0) else 0
    ms60 = ((ma60[idx] - ma60[idx - 5]) / ma60[idx - 5]) if (not np.isnan(ma60[idx]) and not np.isnan(ma60[idx - 5]) and ma60[idx - 5] > 0) else 0
    vol20 = pre["vol20"][idx] if not np.isnan(pre["vol20"][idx]) else 0
    rr = ((hi20 - lo20) / lo20) if lo20 > 0 else 0
    t40 = [float(dd20), float(ms20), float(ms60), float(vol20), float(rr)]

    tr120 = (c / closes[idx - 120] - 1) if closes[idx - 120] > 0 else 0
    tr60 = (c / closes[idx - 60] - 1) if closes[idx - 60] > 0 else 0
    vpct = pre["vpct60"][idx]
    lb = min(250, idx)
    h52 = np.max(highs[idx - lb + 1:idx + 1])
    l52 = np.min(lows[idx - lb + 1:idx + 1])
    dh = (c / h52 - 1) if h52 > 0 else 0
    dl = (c / l52 - 1) if l52 > 0 else 0
    t60 = [float(tr120), float(tr60), float(vpct), float(dh), float(dl)]

    md = ((c - ma60[idx]) / ma60[idx]) if (not np.isnan(ma60[idx]) and ma60[idx] > 0) else 0
    up = pre["up20"][idx] if not np.isnan(pre["up20"][idx]) else 0.5
    env = [float(md), float(vpct), float(up)]

    sig_mask = _signal_mask_at(pre, idx)

    return {"t20": t20, "t40": t40, "t60": t60, "env": env,
            "shape5": sh5, "shape10": sh10, "shape20": sh20, "shape30": sh30,
            "shape40": sh40, "shape60": sh60,
            "signal_mask": sig_mask}


def extract_features(closes, highs, lows, vols, idx):
    """兼容旧接口：单点提取技术特征 + K线形态向量。"""
    pre = _precompute_indicators(closes, highs, lows, vols)
    return _features_at(pre, idx)


# ===== 向量化批处理：一只股票所有锚点一次性算出全部特征矩阵 =====
def _shape_matrix_batch(closes_np, anchors, days, points):
    """对所有锚点 anchors 批量生产 shape 矩阵 (len(anchors), points)。
    对每个 anchor ai: rel = log(closes[ai-days:ai+1] / closes[ai]) 再线性插值到 points 点。
    """
    N = len(anchors)
    # 取出每个 anchor 对应的 days+1 长度的窗口 → 矩阵 (N, days+1)
    # offsets: -days..0
    offsets = np.arange(-days, 1)
    idx_mat = anchors[:, None] + offsets[None, :]    # (N, days+1)
    seg = closes_np[idx_mat]                          # (N, days+1)
    entry = closes_np[anchors][:, None]               # (N, 1)
    # 对 seg<=0 或 entry<=0 的行做掩码（极少见，分红停牌期）
    bad = (seg <= 0).any(axis=1) | (entry[:, 0] <= 0)
    # 安全计算 log；对 bad 行结果之后会被替换为 0
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.log(seg / entry)
    rel[bad] = 0.0

    # 线性插值到 points 点：xs ∈ [0, days]，对每行同样的插值索引
    # 因为 xs 间距固定且 fp 间距固定（0..days 整数），可以预算权重一次
    xs = np.linspace(0, days, points)
    left = np.clip(np.floor(xs).astype(int), 0, days - 1)
    right = left + 1
    frac = xs - left                                  # (points,)
    # 每行做 rel[:, left]*(1-frac) + rel[:, right]*frac
    out = rel[:, left] * (1 - frac) + rel[:, right] * frac    # (N, points)
    return out.astype(np.float32, copy=False), bad


def _features_batch(pre, anchors):
    """对所有锚点一次性生产全部特征向量。返回 dict[key -> (N, D) float32]。"""
    closes = pre["closes"]
    highs = pre["highs"]
    lows = pre["lows"]
    vols = pre["vols"]

    N = len(anchors)
    c = closes[anchors]                               # (N,)
    valid_c = c > 0

    # ---- shape 矩阵（6/12/24/30/32/40 点）----
    sh5, b5 = _shape_matrix_batch(closes, anchors, 5, 6)
    sh10, b10 = _shape_matrix_batch(closes, anchors, 10, 12)
    sh20, b20 = _shape_matrix_batch(closes, anchors, 20, 24)
    sh30, b30 = _shape_matrix_batch(closes, anchors, 30, 30)
    sh40, b40 = _shape_matrix_batch(closes, anchors, 40, 32)
    sh60, b60 = _shape_matrix_batch(closes, anchors, 60, 40)
    bad_shape = b5 | b10 | b20 | b30 | b40 | b60

    # ---- t20 ----
    def _ret_at(off):
        prev = closes[anchors - off]
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(prev > 0, c / prev - 1, 0.0)
        return r

    r5 = _ret_at(5)
    r10 = _ret_at(10)
    r20 = _ret_at(20)
    vstd = pre["vstd20"][anchors]
    vmean = pre["vmean20"][anchors]
    with np.errstate(divide="ignore", invalid="ignore"):
        vz = np.where((~np.isnan(vstd)) & (vstd > 0),
                      (vols[anchors] - vmean) / np.where(vstd > 0, vstd, 1.0), 0.0)
    # ph20 不含当日：前 20 日最大值（已在 pre["ph20"] 算好，但那是含掩码 nan，需替换）
    ph20 = pre["ph20"][anchors]
    ph20_safe = np.where(np.isnan(ph20), c, ph20)
    bs = np.where(ph20_safe > 0, c / ph20_safe - 1, 0.0)
    rsi = pre["rsi14"][anchors]
    rv = np.where(np.isnan(rsi), 0.5, rsi / 100.0)
    t20 = np.stack([r5, r10, r20, vz, bs, rv], axis=1).astype(np.float32)

    # ---- t40 ----
    # hi20/lo20: 含当日 20 日最高/最低
    # 用滚动 max/min（向量化）
    def _rolling_max(arr, w):
        n = len(arr)
        out = np.full(n, np.nan, dtype=arr.dtype)
        if n < w:
            return out
        # 朴素 O(n*w) 也行（w=20 小），但向量化用最大滑窗技巧：用 stride
        from numpy.lib.stride_tricks import sliding_window_view
        v = sliding_window_view(arr, w)               # (n-w+1, w)
        out[w - 1:] = v.max(axis=1)
        return out

    def _rolling_min(arr, w):
        from numpy.lib.stride_tricks import sliding_window_view
        n = len(arr)
        out = np.full(n, np.nan, dtype=arr.dtype)
        if n < w:
            return out
        v = sliding_window_view(arr, w)
        out[w - 1:] = v.min(axis=1)
        return out

    hi20_full = _rolling_max(highs, 20)
    lo20_full = _rolling_min(lows, 20)
    hi20 = hi20_full[anchors]
    lo20 = lo20_full[anchors]
    dd20 = np.where(hi20 > 0, c / hi20 - 1, 0.0)
    ma20 = pre["ma20"]
    ma60 = pre["ma60"]
    ma20_a = ma20[anchors]
    ma20_a5 = ma20[anchors - 5]
    ma60_a = ma60[anchors]
    ma60_a5 = ma60[anchors - 5]
    with np.errstate(divide="ignore", invalid="ignore"):
        ms20 = np.where((~np.isnan(ma20_a)) & (~np.isnan(ma20_a5)) & (ma20_a5 > 0),
                        (ma20_a - ma20_a5) / np.where(ma20_a5 > 0, ma20_a5, 1.0), 0.0)
        ms60 = np.where((~np.isnan(ma60_a)) & (~np.isnan(ma60_a5)) & (ma60_a5 > 0),
                        (ma60_a - ma60_a5) / np.where(ma60_a5 > 0, ma60_a5, 1.0), 0.0)
    vol20 = pre["vol20"][anchors]
    vol20 = np.where(np.isnan(vol20), 0.0, vol20)
    rr = np.where(lo20 > 0, (hi20 - lo20) / lo20, 0.0)
    t40 = np.stack([dd20, ms20, ms60, vol20, rr], axis=1).astype(np.float32)

    # ---- t60 ----
    tr120 = _ret_at(120)
    tr60 = _ret_at(60)
    vpct = pre["vpct60"][anchors]
    vpct = np.where(np.isnan(vpct), 0.5, vpct)
    # 52 周(<=250)高低：用 250 日窗口（早期可能不够则放小）
    hi250 = _rolling_max(highs, 250)[anchors]
    lo250 = _rolling_min(lows, 250)[anchors]
    # 起始锚点 idx < 250 时 hi250/lo250 = nan，回退用从 0 到 idx 的极值——这里近似用 c
    hi250 = np.where(np.isnan(hi250), c, hi250)
    lo250 = np.where(np.isnan(lo250), c, lo250)
    dh = np.where(hi250 > 0, c / hi250 - 1, 0.0)
    dl = np.where(lo250 > 0, c / lo250 - 1, 0.0)
    t60 = np.stack([tr120, tr60, vpct, dh, dl], axis=1).astype(np.float32)

    # ---- env ----
    with np.errstate(divide="ignore", invalid="ignore"):
        md = np.where((~np.isnan(ma60_a)) & (ma60_a > 0),
                      (c - ma60_a) / np.where(ma60_a > 0, ma60_a, 1.0), 0.0)
    up = pre["up20"][anchors]
    up = np.where(np.isnan(up), 0.5, up)
    env = np.stack([md, vpct, up], axis=1).astype(np.float32)

    # ---- 信号位向量（7维，T-2~T+2 窗口聚合）----
    # 先对【整条序列】计算每一天的 7 位信号，然后用 ±2 滚动 OR 聚合到锚点
    n_full = len(closes)
    closes_full = closes
    highs_full = highs
    vols_full = vols
    ph20_full = pre["ph20"]
    ph60_full = pre["ph60"]
    ph120_full = pre["ph120"]
    bb_upper_full = pre["bb_upper"]
    vma20_full = pre["vma20"]
    ma5_full = pre["ma5"]
    ma10_full = pre["ma10"]
    ma20_full = pre["ma20"]
    ma60_full = pre["ma60"]
    nan_or = np.isnan  # 简写

    B0 = ((~nan_or(ph20_full)) & (closes_full > ph20_full))
    B1 = ((~nan_or(ph60_full)) & (closes_full > ph60_full))
    B2 = ((~nan_or(ph120_full)) & (closes_full > ph120_full))
    B3 = ((~nan_or(bb_upper_full)) & (closes_full > bb_upper_full))
    B4 = ((~nan_or(ph20_full)) & (closes_full > ph20_full) &
          (~nan_or(vma20_full)) & (vma20_full > 0) & (vols_full > 1.5 * vma20_full))
    # 金叉：MA20 上穿 MA60
    ma20_prev_full = np.empty_like(ma20_full); ma20_prev_full[0] = np.nan; ma20_prev_full[1:] = ma20_full[:-1]
    ma60_prev_full = np.empty_like(ma60_full); ma60_prev_full[0] = np.nan; ma60_prev_full[1:] = ma60_full[:-1]
    prev_below_full = nan_or(ma20_prev_full) | nan_or(ma60_prev_full) | (ma20_prev_full <= ma60_prev_full)
    B5 = ((~nan_or(ma20_full)) & (~nan_or(ma60_full)) & (ma20_full > ma60_full) & prev_below_full)
    all_valid_full = ~(nan_or(ma5_full) | nan_or(ma10_full) | nan_or(ma20_full) | nan_or(ma60_full))
    B6 = (all_valid_full & (ma5_full > ma10_full) & (ma10_full > ma20_full) &
          (ma20_full > ma60_full) & (closes_full > ma5_full))

    sig_full = np.stack([B0, B1, B2, B3, B4, B5, B6], axis=1).astype(np.uint8)  # (n_full, 7)

    # ±2 天窗口的逐位 OR：对每个锚点 a, 聚合 sig_full[a-2 .. a+2] 的任一触发
    WIN = 2     # 前后各2天，共 5 天
    # padding 边界(开头/结尾 a±2 越界时)：填 0
    pad = np.zeros((WIN, 7), dtype=np.uint8)
    sig_padded = np.vstack([pad, sig_full, pad])               # (n_full + 2*WIN, 7)
    # 用累加和差分高效求滑窗内每位最大值(因为 0/1 矩阵,sum>0 即代表至少一次触发)
    # 用 cumsum 一次性向量化算每个锚点 5 天窗口内每列的触发次数 → >0 即聚合 OR
    cs = np.cumsum(sig_padded, axis=0, dtype=np.int32)         # (n_full+2*WIN, 7)
    cs = np.vstack([np.zeros((1, 7), dtype=np.int32), cs])      # 前置 0 方便差分
    # 锚点 a 对应 padded 中的索引 a+WIN, 窗口区间 [a, a+2*WIN+1) 在 padded 上, 即 [a, a+5)
    # cs 已比 padded 多 1 行, 所以 sum(a..a+5) = cs[a+5] - cs[a]
    a_pad = anchors                                             # padded 的偏移就是 a (因为前缀加了 WIN 行 + cs 前置 1 行 → -WIN+(WIN)+1=+1? 重算)
    # 重新推导：a 在原序列, 对应 padded 中 a+WIN, 5天窗口=[a+WIN-WIN, a+WIN+WIN+1)=[a, a+5)
    # cs[i] = padded[0..i-1] 累计 (前置过 0); sum(a..a+5) = cs[a+5] - cs[a]
    sig_window_sum = cs[a_pad + 2 * WIN + 1] - cs[a_pad]         # (N, 7)
    sig_mask = (sig_window_sum > 0).astype(np.float32)

    # 整体有效掩码：c>0 且 shape 段无 0/负值
    valid = valid_c & (~bad_shape)

    return {
        "shape5": sh5, "shape10": sh10, "shape20": sh20, "shape30": sh30,
        "shape40": sh40, "shape60": sh60,
        "t20": t20, "t40": t40, "t60": t60, "env": env,
        "signal_mask": sig_mask,
        "valid": valid,
    }


def build_windows_for_stock(dates, opens, closes, highs, lows, vols, as_of_idx, forward_days=FORWARD_DAYS):
    """为单只股票构建滑动窗口片段库（向量化批处理版）。

    重要：为支持百万级候选,这里 **不** 预存 K 线 OHLC(那会让内存爆 3GB+),
    只存元数据 + 特征向量。K 线 OHLC 由 retrieval 在选出 top-N 后按需回填。

    返回: list of (meta_dict, feature_dict)，feature_dict 已是 numpy 向量（非 list）
    """
    closes_np = np.asarray(closes, dtype=np.float64)
    highs_np = np.asarray(highs, dtype=np.float64)
    lows_np = np.asarray(lows, dtype=np.float64)
    vols_np = np.asarray(vols, dtype=np.float64)
    n = len(closes_np)
    min_a = max(WINDOW_LOOKBACK, 120)
    max_a = as_of_idx - forward_days
    if max_a < min_a:
        return []

    anchors = np.arange(min_a, max_a + 1, SLIDE_STEP, dtype=np.int64)
    anchors = anchors[anchors < n]
    if len(anchors) == 0:
        return []

    # 预计算技术指标
    pre = _precompute_indicators(closes_np, highs_np, lows_np, vols_np)
    # 批量算所有特征
    feats = _features_batch(pre, anchors)
    valid = feats["valid"]
    # 过滤掉无效锚点
    valid_ids = np.where(valid)[0]
    if len(valid_ids) == 0:
        return []
    anchors_v = anchors[valid_ids]

    # 前瞻收益和路径（批量）
    H_LIST = [5, 10, 20, 40, 60]
    fwd_mat = {}
    for h in H_LIST:
        end_idx = anchors_v + h
        ok = end_idx < n
        r = np.full(len(anchors_v), np.nan, dtype=np.float64)
        entry = closes_np[anchors_v[ok]]
        with np.errstate(divide="ignore", invalid="ignore"):
            r[ok] = (closes_np[end_idx[ok]] / np.where(entry > 0, entry, 1.0) - 1) * 100
        fwd_mat[h] = r

    # mdd（向量化）和 fp（仍需循环但很轻量）
    anchors_idx = anchors[valid_ids]                  # 实际锚点位置数组
    N = len(anchors_idx)
    entries = closes_np[anchors_idx]
    mdds = np.zeros(N, dtype=np.float64)
    fp_list = [None] * N
    # 这层循环仅为生成 fp 列表（每个长度不同, 难以纯向量化），但内部用 numpy 操作
    for k in range(N):
        ai = int(anchors_idx[k])
        end = min(ai + forward_days + 1, n)
        future = closes_np[ai + 1:end]
        entry_p = float(entries[k])
        if len(future) and entry_p > 0:
            fp_list[k] = ((future / entry_p - 1) * 100).round(2).tolist()
            peaks = np.maximum.accumulate(np.insert(future, 0, entry_p))[1:]
            mdds[k] = float(np.min(future / peaks - 1)) * 100
        else:
            fp_list[k] = []

    # 元数据（dates 是 list，逐项取很快；这层循环是 O(N) Python 但无 numpy 计算）
    metas = []
    for k in range(N):
        ai = int(anchors_idx[k])
        fwd = {f"r_{h}d": (None if np.isnan(fwd_mat[h][k]) else round(float(fwd_mat[h][k]), 2))
               for h in H_LIST}
        fwd["max_drawdown"] = round(float(mdds[k]), 2)
        metas.append({
            "anchor_idx": ai,
            "anchor_date": dates[ai],
            "entry_price": round(float(entries[k]), 4),
            "fwd_returns": fwd,
            "fwd_path": fp_list[k],
        })

    # 返回矩阵化特征(已是 N×D ndarray) + 元数据列表
    feat_mats = {key: feats[key][valid_ids] for key in
                 ["shape5", "shape10", "shape20", "shape30", "shape40", "shape60",
                  "t20", "t40", "t60", "env", "signal_mask"]}
    return metas, feat_mats


def _build_one_serializable(payload):
    """ProcessPool 入口：避免传递 DataFrame 跨进程,只传必要列表/数组。"""
    sym, dates, opens, closes, highs, lows, vols, as_of_idx, forward_days = payload
    metas, feat_mats = build_windows_for_stock(
        dates, opens, closes, highs, lows, vols, as_of_idx, forward_days)
    return sym, metas, feat_mats


def build_multi_stock_windows(symbol_data_dict, as_of_date, forward_days=FORWARD_DAYS,
                              n_workers=None, use_processes=True):
    """跨股票构建片段库（并行，向量化批处理）。

    use_processes=True 时用 ProcessPool 真正并行（绕过 GIL）；False 用 ThreadPool（调试用）。

    返回:
      meta_list: [{"symbol","anchor_date","anchor_idx","entry_price","fwd_returns","fwd_path"}, ...]
      feature_matrices: dict[key -> (M, D) float32 ndarray]，KNN 直接矩阵运算
      norm_stats: dict[key -> (mean, std)]
    K 线 OHLC 由 retrieval 在最终选出 top-N 后回填(避免 120 万窗口存 3GB+ OHLC)。
    """
    from concurrent.futures import ProcessPoolExecutor

    cpu = os.cpu_count() or 4
    # 线程/进程数：尽量靠近 CPU 总数,但不超过股票数(每股 1 个并发任务)
    if n_workers is None:
        n_workers = min(cpu, len(symbol_data_dict))
    n_workers = max(1, n_workers)

    payloads = []
    for sym, d in symbol_data_dict.items():
        df = d["df"]
        payloads.append((
            sym,
            df["date"].tolist(),
            df["open"].tolist(),
            df["close"].tolist(),
            df["high"].tolist(),
            df["low"].tolist(),
            df["volume"].tolist() if "volume" in df.columns else [0] * len(df),
            d["as_of_idx"],
            forward_days,
        ))

    label = "进程池" if use_processes else "线程池"
    print(f"[slicer] 窗口构建({label}): {n_workers}并发 (CPU={cpu}核), {len(payloads)}只股票, 步长={SLIDE_STEP}")
    t0 = time.time()
    per_stock = []
    Executor = ProcessPoolExecutor if use_processes else ThreadPoolExecutor
    try:
        with Executor(max_workers=n_workers) as executor:
            for sym, metas, feat_mats in executor.map(_build_one_serializable, payloads, chunksize=1):
                if metas:
                    per_stock.append((sym, metas, feat_mats))
    except Exception as e:
        # 进程池失败（Windows spawn 限制等）→ 回退到线程池
        print(f"[slicer] 进程池失败({e})，回退线程池")
        per_stock = []
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            for sym, metas, feat_mats in executor.map(_build_one_serializable, payloads):
                if metas:
                    per_stock.append((sym, metas, feat_mats))
    print(f"[slicer] 并发构建完成 ({time.time()-t0:.1f}s): {len(per_stock)}只股票产出窗口")

    M = sum(len(metas) for _, metas, _ in per_stock)
    if M < 5:
        return [], None, None

    print(f"[slicer] 候选窗口总数 M={M}，开始拼接 + 标准化…")
    t1 = time.time()

    # 拼接：每只股票已经是矩阵,vstack 一次性合并(高度向量化)
    FEAT_KEYS = ["t20", "t40", "t60", "env",
                 "shape5", "shape10", "shape20", "shape30", "shape40", "shape60",
                 "signal_mask"]
    raw_mats = {}
    for key in FEAT_KEYS:
        raw_mats[key] = np.vstack([fm[key] for _, _, fm in per_stock]).astype(np.float32, copy=False)

    # 元数据扁平化：附加 symbol
    meta_list = []
    for sym, metas, _ in per_stock:
        for m in metas:
            m["symbol"] = sym
            meta_list.append(m)

    # z-score 标准化（每个特征独立）
    norm_stats = {}
    feat_norm = {}
    for key in FEAT_KEYS:
        if key == "signal_mask":
            feat_norm[key] = raw_mats[key]            # 0/1 不标准化
            continue
        mat = raw_mats[key]
        m = mat.mean(axis=0)
        s = mat.std(axis=0)
        s[s == 0] = 1.0
        norm_stats[key] = (m, s)
        feat_norm[key] = ((mat - m) / s).astype(np.float32)

    mem_mb = sum(mat.nbytes for mat in feat_norm.values()) / 1024 / 1024
    print(f"[slicer] 标准化完成 ({time.time()-t1:.1f}s): 特征矩阵共 {mem_mb:.1f} MB ({M}个窗口)")
    return meta_list, feat_norm, norm_stats
