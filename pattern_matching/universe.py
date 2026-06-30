"""股票池管理（BE-010）。

管理交易候选池(trading)和研究参考池(reference)，明确两者用途边界。
支持 A500/SP500/港股大市值/自定义股票池的创建、保存和查询。
"""
import os
import json
import time
from typing import Optional, List, Dict, Any

from backtest.scanner import get_a500_constituents, get_sp500_constituents, get_hk_large_caps
from backtest.data import CACHE_DIR

UNIVERSE_CONFIG_PATH = os.path.join(CACHE_DIR, "universe_configs.json")


def _load_configs() -> dict:
    if os.path.exists(UNIVERSE_CONFIG_PATH):
        try:
            with open(UNIVERSE_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {"configs": {}}


def _save_configs(data: dict):
    try:
        with open(UNIVERSE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[universe] 保存失败: {e}")


def _fetch_source(source: str) -> List[Dict]:
    """根据来源获取股票列表, 返回 [{symbol, name, market}]. """
    if source == "a500":
        return [{"symbol": c, "name": n, "market": "cn"}
                for c, n in get_a500_constituents()]
    elif source == "sp500":
        return [{"symbol": c[2:] if c.startswith("us") else c,
                 "name": n, "market": "us"}
                for c, n in get_sp500_constituents()]
    elif source == "hk_large":
        return [{"symbol": c[2:] if c.startswith("hk") else c,
                 "name": n, "market": "hk"}
                for c, n in get_hk_large_caps()]
    return []


def universe_options() -> dict:
    """返回可选的股票池来源。"""
    return {
        "ok": True,
        "sources": [
            {"id": "a500", "name": "中证A500成分股", "market": "cn"},
            {"id": "sp500", "name": "标普500成分股", "market": "us"},
            {"id": "hk_large", "name": "港股大市值(流通≥1000亿)", "market": "hk"},
            {"id": "custom", "name": "自定义列表", "market": "mixed"},
        ],
    }


def preview_universe(sources: List[str], custom_symbols: List[str] = None) -> dict:
    """预览股票池内容（不保存）。"""
    symbols: Dict[str, Dict] = {}
    for src in sources:
        for s in _fetch_source(src):
            symbols.setdefault(s["symbol"], s)

    if custom_symbols:
        for sym in custom_symbols:
            s = sym.strip()
            if s:
                symbols.setdefault(s, {"symbol": s, "name": s, "market": "custom"})

    result = list(symbols.values())
    return {
        "ok": True,
        "symbols": result,
        "count": len(result),
        "sources": sources,
    }


def create_universe(universe_type: str, name: str, sources: List[str],
                    custom_symbols: List[str] = None) -> dict:
    """创建或更新一个股票池配置。"""
    if universe_type not in ("trading", "reference"):
        return {"ok": False, "error": "type 必须是 trading 或 reference"}

    symbols = []
    seen = set()
    for src in sources:
        for s in _fetch_source(src):
            if s["symbol"] not in seen:
                seen.add(s["symbol"])
                symbols.append(s)

    if custom_symbols:
        for sym in custom_symbols:
            sym = sym.strip()
            if sym and sym not in seen:
                seen.add(sym)
                symbols.append({"symbol": sym, "name": sym, "market": "custom"})

    uid = f"{universe_type}_{name}_{int(time.time())}"
    config = {
        "universe_id": uid,
        "universe_type": universe_type,
        "name": name,
        "sources": sources,
        "custom_symbols": custom_symbols or [],
        "symbols": symbols,
        "count": len(symbols),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    data = _load_configs()
    data["configs"][uid] = config
    _save_configs(data)

    return {"ok": True, "universe": config}


def get_universe(universe_id: str) -> dict:
    """查询某个股票池。"""
    data = _load_configs()
    cfg = data["configs"].get(universe_id)
    if cfg is None:
        return {"ok": False, "error": f"股票池 {universe_id} 不存在"}
    return {"ok": True, "universe": cfg}


def list_universes(universe_type: str = None) -> dict:
    """列出所有股票池。"""
    data = _load_configs()
    configs = list(data["configs"].values())
    if universe_type:
        configs = [c for c in configs if c["universe_type"] == universe_type]
    return {"ok": True, "universes": configs, "count": len(configs)}
