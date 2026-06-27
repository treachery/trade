"""股票策略回测核心包。"""
from .strategy import StrategyConfig, ENTRY_TYPES, EXIT_TYPES
from .engine import run_backtest, run_optimization
from .data import load_kline, load_pe, INDEX_PE_PROXY

__all__ = ["StrategyConfig", "ENTRY_TYPES", "EXIT_TYPES",
           "run_backtest", "run_optimization", "load_kline", "load_pe", "INDEX_PE_PROXY"]
