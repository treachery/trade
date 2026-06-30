"""逐级递进相似K线检索引擎（第6层）——跨股票检索版（矩阵化）。

四级形态递进过滤：
  Layer 1: 近5日形态  → top 10000
  Layer 2: 近10日形态 → top 1000
  Layer 3: 近20日形态 → top 100
  Layer 4: 近30日形态 → top 50
  最终：形态相似度(原始对数收益率RMSE) + 信号Jaccard相似度 综合评分 → top 10

工程要点：
- 候选池可达百万级；slicer 用 float32 矩阵存储，KNN 用 numpy 矩阵运算批处理。
- 窗口对象本身只存元数据；K 线 OHLC 在最终 top-K 选出后从原 df 按需回填。
"""
import os
import time
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from backtest.data import load_kline
from backtest.scanner import get_a500_constituents
from .signals import detect_signals
from .slicer import build_multi_stock_windows, extract_features

REF_LIMIT = 500  # 参考池股票数量：默认覆盖A500全部成分股

# 四级递进目标候选数（候选不足时按上一级总数缩放）
LAYER_TARGETS = [10000, 1000, 100, 50]
LAYER_KEYS = ["shape5", "shape10", "shape20", "shape30"]
LAYER_DAYS = {"shape5": 5, "shape10": 10, "shape20": 20, "shape30": 30}

# 形态综合距离权重（越近期权重越高）
SHAPE_W = {"shape5": 0.40, "shape10": 0.30, "shape20": 0.20, "shape30": 0.10}


def _topk_indices_by_distance(query_vec, mat, ids, k):
    """对 mat[ids] 中每行计算到 query_vec 的欧氏距离，返回距离最小的 k 个 (id, dist)。
    mat: 全集 (M, D) 矩阵；ids: 上一级候选索引 np.ndarray
    """
    sub = mat[ids]                                  # (n, D)
    diff = sub - query_vec[np.newaxis, :]           # 广播
    dists = np.sqrt(np.einsum("ij,ij->i", diff, diff))   # (n,) 比 sum(diff**2,axis=1) 快
    k = min(k, len(dists))
    if k <= 0:
        return ids[:0], dists[:0]
    part = np.argpartition(dists, k - 1)[:k]
    order = part[np.argsort(dists[part])]
    return ids[order], dists[order]


def multi_stock_retrieval(meta_list, feat_norm, norm_stats, q_features, top_k=10, top_k_eval=100):
    """矩阵化四级递进检索 + 综合评分。

    Args:
        meta_list:    [{symbol, anchor_date, anchor_idx, entry_price, fwd_returns, fwd_path}, ...]
        feat_norm:    dict[key -> (M, D) float32]，已 z-score 标准化
        norm_stats:   dict[key -> (mean, std)]，用于标准化查询向量
        q_features:   查询点特征(未标准化)
        top_k:        前端展示用的相似片段数(默认10条)
        top_k_eval:   策略评估用的样本数(默认100条,达到统计学有效性阈值)
    """
    M = len(meta_list)
    if M == 0:
        return {"ok": False, "error": "无候选片段"}

    # 标准化查询向量（每层各一份）
    q_norm = {}
    for key in LAYER_KEYS:
        m, s = norm_stats[key]
        q_norm[key] = ((np.asarray(q_features[key], dtype=np.float32) - m) / s).astype(np.float32)

    # 查询点的原始(未标准化) shape 向量，用于最终 RMSE 拟合度评估
    q_raw = {key: np.asarray(q_features[key], dtype=np.float32) for key in LAYER_KEYS}

    # 信号位向量(0/1)
    q_sig = np.asarray(q_features.get("signal_mask", []), dtype=np.float32)

    # 计算每级目标 k（库不够时缩，但 >= top_k）
    targets = []
    prev = M
    for tgt in LAYER_TARGETS:
        k = min(tgt, prev)
        k = max(k, top_k)
        targets.append(k)
        prev = k

    cur_ids = np.arange(M, dtype=np.int64)
    layer_dists = {}    # key -> {idx: dist}
    stage_logs = []

    for layer_idx, key in enumerate(LAYER_KEYS):
        k = targets[layer_idx]
        t0 = time.time()
        mat = feat_norm[key]
        new_ids, new_dists = _topk_indices_by_distance(q_norm[key], mat, cur_ids, k)
        dt = time.time() - t0
        layer_dists[key] = dict(zip(new_ids.tolist(), new_dists.tolist()))
        days = LAYER_DAYS[key]
        stage_logs.append({"layer": key, "days": days,
                           "input": int(len(cur_ids)), "output": int(len(new_ids)),
                           "target": k, "elapsed_ms": round(dt * 1000, 1)})
        print(f"[retrieval] Layer{layer_idx+1} {days}日形态: {len(cur_ids)} -> {len(new_ids)} ({dt*1000:.0f}ms)")
        cur_ids = new_ids

    # ===== 最终综合评分 =====
    # 1) 加权形态距离 → shape_sim
    # 2) 原始对数收益率向量的 RMSE → fit_rmse（直观"形状拟合度"，不受标准化抹平）
    # 3) 信号 Jaccard 相似度 → sig_sim
    final = []
    for i in cur_ids.tolist():
        # 形态综合标准化距离
        shape_dist = sum(SHAPE_W[k] * layer_dists[k].get(i, 9.0) for k in LAYER_KEYS)
        shape_sim = 1.0 / (1.0 + shape_dist)

        # 原始 shape20 RMSE（百分比口径）：sqrt(mean( (q-c)^2 )) on log-return
        # 越小越像；做成 0~1 的 fit 分(20% 形变以内 → ~1, 越大越接近 0)
        c20 = feat_norm["shape20"][i]   # 注意：这是标准化后的，我们要原始
        # 拿原始 shape20：feat_norm 是标准化的；norm_stats[shape20] = (mean, std)，可还原
        m20, s20 = norm_stats["shape20"]
        c20_raw = c20 * s20 + m20
        rmse = float(np.sqrt(np.mean((q_raw["shape20"] - c20_raw) ** 2)))
        # 形变 20% 内 fit≈1，越大趋近0（exp 衰减）
        fit_sim = float(np.exp(-rmse / 0.05))   # 5% 形变 → fit≈0.37, 2% → ~0.67, 1% → ~0.82

        # 信号 Jaccard
        cand_sig = feat_norm["signal_mask"][i]
        if q_sig.size > 0 and cand_sig.size == q_sig.size and (q_sig.sum() + cand_sig.sum()) > 0:
            inter = float(np.minimum(q_sig, cand_sig).sum())
            union = float(np.maximum(q_sig, cand_sig).sum())
            sig_sim = inter / union if union > 0 else 0.0
        else:
            sig_sim = 0.5

        # 综合评分：形态 45% + 拟合 40% + 信号 15%
        # 形态: 标准化向量的相似度(消除幅度看走势曲线)
        # 拟合: 未标准化对数收益率 RMSE(看实际幅度是否贴合)
        # 信号: T-2~T+2 窗口内的技术信号 Jaccard 相似度
        score = 0.45 * shape_sim + 0.40 * fit_sim + 0.15 * sig_sim
        final.append((i, score, shape_sim, fit_sim, sig_sim, shape_dist, rmse))

    final.sort(key=lambda x: x[1], reverse=True)
    # 同时构造两个集合：
    #   final_top         — 用户指定的 top_k(展示用,稍后会回填 K 线 OHLC)
    #   final_top_eval    — 固定 100 条(策略评估用,样本量大才有统计置信度)
    EVAL_K = 100
    final_full = final[:max(top_k, EVAL_K)]

    def _build(rank, i, score, shape_sim, fit_sim, sig_sim, shape_dist, rmse):
        meta = meta_list[i]
        return {
            "rank": rank,
            "symbol": meta["symbol"],
            "anchor_date": meta["anchor_date"],
            "anchor_idx": meta["anchor_idx"],
            "similarity": round(score, 4),
            "shape_similarity": round(shape_sim, 4),
            "fit_similarity": round(fit_sim, 4),
            "signal_similarity": round(sig_sim, 4),
            "rmse_pct": round(rmse * 100, 2),
            "combined_distance": round(shape_dist, 4),
            "shape_distance": round(layer_dists["shape5"].get(i, 0), 4),
            "layer_distances": {k: round(layer_dists[k].get(i, 0), 4) for k in LAYER_KEYS},
            "entry_price": meta["entry_price"],
            "fwd_returns": meta["fwd_returns"],
            "fwd_path": meta["fwd_path"],
        }

    results_all = [_build(r + 1, *t) for r, t in enumerate(final_full)]
    results_top = results_all[:top_k]                # 展示用(回填 K 线)
    results_eval = results_all[:min(EVAL_K, len(results_all))]  # 评估用(无需 K 线)

    stages = {st["layer"]: {"input": st["input"], "output": st["output"], "target": st["target"]}
              for st in stage_logs}
    stages["final_score"] = {"input": int(len(cur_ids)), "output": len(results_top)}

    return {"ok": True, "final_top": results_top, "final_top_eval": results_eval,
            "total_candidates": M, "stages": stages, "stage_logs": stage_logs}


def _attach_kline_for_top(results, symbol_data):
    """为最终 top 结果回填 K 线 OHLC（从原 df 现取，避免百万窗口内存爆炸）。"""
    for r in results:
        sym = r["symbol"]
        sd = symbol_data.get(sym)
        if not sd:
            continue
        df = sd["df"]
        ai = r["anchor_idx"]
        n = len(df)
        kl_start = max(0, ai - 60)
        kl_end = min(n - 1, ai + 60)
        opens = df["open"].tolist()
        closes = df["close"].tolist()
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        dates = df["date"].tolist()
        r["kline_ohlc"] = [[round(opens[i], 4), round(closes[i], 4),
                            round(lows[i], 4), round(highs[i], 4)]
                           for i in range(kl_start, kl_end + 1)]
        r["kline_dates"] = dates[kl_start:kl_end + 1]
        r["anchor_offset"] = ai - kl_start
    return results


def analyze_symbol(symbol, as_of_date=None, adjust="qfq", lookback_years=6, top_k=10,
                   reference_symbols=None, ref_limit=REF_LIMIT, allow_online=False):
    """跨股票分析：信号侦测 → 跨股票切片建库 → 矩阵化递进检索。"""
    t_start = time.time()

    # 1. 信号侦测
    sig = detect_signals(symbol, as_of_date=as_of_date, adjust=adjust,
                         lookback_years=lookback_years, offline=True)
    if not sig.get("ok") and allow_online:
        print(f"[pattern] 离线缓存无数据，允许联网回退: {symbol}")
        sig = detect_signals(symbol, as_of_date=as_of_date, adjust=adjust,
                             lookback_years=lookback_years, offline=False)
    if not sig.get("ok"):
        sig["error"] = f"{sig.get('error', '数据不足')}；相似K线分析默认不联网，请先预下载该标的K线缓存"
        return sig
    end = sig["as_of_date"]
    print(f"[pattern] 信号侦测完成 ({time.time()-t_start:.1f}s)")

    # 2. 加载查询标的历史
    start = (datetime.strptime(end[:10], "%Y-%m-%d") - timedelta(days=365 * lookback_years)).strftime("%Y-%m-%d")
    try:
        df = load_kline(symbol, start, end, adjust=adjust, offline=True)
        if (df is None or df.empty or len(df) < 250) and allow_online:
            print(f"[pattern] 查询标的{symbol}离线缓存不足，允许联网回退")
            df = load_kline(symbol, start, end, adjust=adjust, offline=False)
    except Exception as e:
        print(f"[pattern] 查询标的{symbol}加载失败: {e}")
        return {"ok": False, "error": f"加载{symbol}行情数据失败(网络错误)，请稍后重试"}
    if df is None or df.empty or len(df) < 250:
        return {"ok": False, "error": f"查询标的{symbol}历史数据不足(需>=250日)"}
    df = df.sort_values("date").reset_index(drop=True)

    # 3. 参考池
    if reference_symbols is None:
        a500 = get_a500_constituents()
        reference_symbols = [c for c, n in a500[:ref_limit] if c != symbol]
    reference_symbols = [symbol] + [s for s in reference_symbols if s != symbol]
    print(f"[pattern] 参考池: {len(reference_symbols)} 只股票, 开始并行加载K线...")

    # 4. 并行加载所有参考标的K线（带重试）
    def _load_one(s, retries=3):
        for attempt in range(retries + 1):
            try:
                sdf = load_kline(s, start, end, adjust=adjust, offline=True)
                if (sdf is None or sdf.empty or len(sdf) < 250) and allow_online:
                    sdf = load_kline(s, start, end, adjust=adjust, offline=False)
                if sdf is not None and not sdf.empty and len(sdf) >= 250:
                    sdf = sdf.sort_values("date").reset_index(drop=True)
                    sdf = sdf[sdf["date"] <= end]
                    if len(sdf) >= 250:
                        return s, {"df": sdf, "as_of_idx": len(sdf) - 1}
                return s, None
            except Exception:
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return s, None

    symbol_data = {}
    failed = 0
    # K线加载是磁盘IO+pandas解析,IO期间释放GIL → 多线程能加速。放开到 CPU 核数(上限 32 防过载)
    max_workers = min(max(8, os.cpu_count() or 8), 32, len(reference_symbols))
    print(f"[pattern] K线加载(离线缓存模式): {max_workers}线程, {len(reference_symbols)}只股票")
    t_load = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_load_one, s) for s in reference_symbols]
        for future in as_completed(futures):
            s, data = future.result()
            if data:
                symbol_data[s] = data
            else:
                failed += 1
    print(f"[pattern] K线加载完成 ({time.time()-t_load:.1f}s): 成功{len(symbol_data)}, 失败{failed}")

    if len(symbol_data) < 2:
        return {"ok": False, "error": f"可用参考标的不足(仅{len(symbol_data)}只), 失败{failed}只"}

    # 5. 构建片段库（矩阵化）
    t_build = time.time()
    meta_list, feat_norm, norm_stats = build_multi_stock_windows(symbol_data, end)
    if meta_list is None or len(meta_list) < 5:
        return {"ok": False, "error": "无法构建足够的历史片段"}
    print(f"[pattern] 片段库构建完成 ({time.time()-t_build:.1f}s): {len(meta_list)}个片段 (来自{len(symbol_data)}只股票)")

    # 6. 提取查询标的特征
    qdf = symbol_data[symbol]["df"]
    qidx = symbol_data[symbol]["as_of_idx"]
    qcloses = qdf["close"].tolist()
    qhighs = qdf["high"].tolist()
    qlows = qdf["low"].tolist()
    qvols = qdf["volume"].tolist() if "volume" in qdf.columns else [0] * len(qdf)
    qf = extract_features(qcloses, qhighs, qlows, qvols, qidx)
    if qf is None:
        return {"ok": False, "error": "无法提取查询标的特征"}

    # 7. 查询标的近120日K线
    q_opens = qdf["open"].tolist()
    k_start = max(0, qidx - 119)
    query_kline = {
        "dates": qdf["date"].tolist()[k_start:qidx + 1],
        "ohlc": [[round(q_opens[i], 4), round(qcloses[i], 4),
                  round(qlows[i], 4), round(qhighs[i], 4)]
                 for i in range(k_start, qidx + 1)],
        "volumes": [round(qvols[i], 2) for i in range(k_start, qidx + 1)],
    }

    # 8. 矩阵化递进检索
    t_retrieval = time.time()
    retrieval = multi_stock_retrieval(meta_list, feat_norm, norm_stats, qf, top_k=top_k)
    print(f"[pattern] 递进检索完成 ({time.time()-t_retrieval:.1f}s)")

    # 9. 为 top 结果回填 K 线 OHLC
    if retrieval.get("ok"):
        _attach_kline_for_top(retrieval["final_top"], symbol_data)

    print(f"[pattern] 总耗时 {time.time()-t_start:.1f}s")

    return {
        "ok": True, "symbol": symbol, "as_of_date": end,
        "signal_info": sig, "query_kline": query_kline,
        "retrieval": retrieval,
        "total_stocks": len(symbol_data), "total_windows": len(meta_list),
    }
