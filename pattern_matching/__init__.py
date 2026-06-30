"""逐级递进相似K线策略研究模块（对应总体设计文档第3-7层）。

核心管线：
  信号侦测(第4层) → K线切片标准化(第5层) → 逐级递进检索(第6层) → 策略评估排序(第7层)

复用 backtest.data 的行情加载和 backtest.engine 的技术指标计算。
"""
from .models import (Security, DailyBar, DailyFeature, SignalEvent, PatternWindow,
                     StrategyCandidate, StrategyEvaluation, UniverseConfig,
                     WFSimulationRun, TradeDetail, PortfolioSnapshot,
                     DailyHoldingEvaluation, DailyRebalanceDecision,
                     ProgressiveRetrievalLog)
from .signals import detect_signals, SIGNAL_DEFINITIONS
from .retrieval import analyze_symbol, multi_stock_retrieval
from .evaluator import evaluate_strategies, STRATEGY_LIBRARY

__all__ = [
    "Security", "DailyBar", "DailyFeature", "SignalEvent", "PatternWindow",
    "StrategyCandidate", "StrategyEvaluation", "UniverseConfig",
    "WFSimulationRun", "TradeDetail", "PortfolioSnapshot",
    "DailyHoldingEvaluation", "DailyRebalanceDecision", "ProgressiveRetrievalLog",
    "detect_signals", "SIGNAL_DEFINITIONS",
    "analyze_symbol", "multi_stock_retrieval",
    "evaluate_strategies", "STRATEGY_LIBRARY",
]
