"""统一业务对象与数据模型（BE-001）。

对齐总体设计第 7 章核心表字段，支持 JSON 序列化。
"""

from dataclasses import dataclass, field, asdict
from datetime import date as Date
from typing import Optional, List, Dict, Any


def _to_dict(obj):
    """递归转 dict，处理 dataclass/list/基本类型。"""
    if obj is None:
        return None
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, Date):
        return obj.isoformat()
    return obj


@dataclass
class Security:
    """证券主表。"""
    symbol: str                   # 证券代码
    name: str = ""                # 证券名称
    exchange: str = ""            # SSE / SZSE / HKEX / NYSE / NASDAQ
    sector: str = ""              # 行业板块
    industry: str = ""            # 细分行业
    market_cap: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class DailyBar:
    """日K行情。"""
    symbol: str
    date: str
    open: float = 0.0
    close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    amount: Optional[float] = None
    adj_factor: Optional[float] = None
    adjusted_open: Optional[float] = None
    adjusted_high: Optional[float] = None
    adjusted_low: Optional[float] = None
    adjusted_close: Optional[float] = None
    is_suspended: bool = False
    limit_up_price: Optional[float] = None
    limit_down_price: Optional[float] = None
    turnover_rate: Optional[float] = None

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class DailyFeature:
    """日频特征快照。"""
    symbol: str
    date: str
    return_1d: Optional[float] = None
    return_5d: Optional[float] = None
    return_10d: Optional[float] = None
    return_20d: Optional[float] = None
    return_60d: Optional[float] = None
    volatility_20d: Optional[float] = None
    volatility_60d: Optional[float] = None
    atr_14: Optional[float] = None
    ma_5: Optional[float] = None
    ma_10: Optional[float] = None
    ma_20: Optional[float] = None
    ma_60: Optional[float] = None
    ma_120: Optional[float] = None
    ma_20_slope: Optional[float] = None
    ma_60_slope: Optional[float] = None
    rsi_14: Optional[float] = None
    macd_dif: Optional[float] = None
    macd_dea: Optional[float] = None
    macd_hist: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    volume_zscore_20d: Optional[float] = None
    volume_ratio_5d: Optional[float] = None
    market_regime: str = ""         # BULL / BEAR / RANGE
    volatility_regime: str = ""     # HIGH / MID / LOW
    sector_relative_strength: Optional[float] = None
    distance_from_52w_high: Optional[float] = None
    distance_from_52w_low: Optional[float] = None
    feature_version: str = "v1"

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class SignalEvent:
    """信号事件。"""
    event_id: str
    symbol: str
    signal_date: str
    signal_type: str               # breakout_20d_high / ma_golden_20_60 …
    signal_category: str = ""      # buy / sell / risk_off
    direction: str = ""            # buy / sell
    strength: float = 0.0
    strength_label: str = ""
    signal_params: dict = field(default_factory=dict)
    feature_snapshot_id: str = ""
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    valid_next_trade_date: Optional[str] = None
    decay_rate: float = 0.0
    is_active: bool = True
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class PatternWindow:
    """K线片段。"""
    window_id: str
    symbol: str
    name: str = ""
    anchor_date: str = ""
    anchor_idx: int = 0
    lookback_days: int = 250
    forward_days: int = 60
    entry_price: float = 0.0
    feature_vector: Optional[Dict] = None
    feature_version: str = "v1"
    is_forward_window_complete: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class StrategyCandidate:
    """候选策略。"""
    strategy_id: str
    strategy_name: str = ""
    strategy_category: str = ""    # direct / confirm / pullback / risk / benchmark
    entry_rule: dict = field(default_factory=dict)
    exit_rule: dict = field(default_factory=dict)
    risk_rule: dict = field(default_factory=dict)
    parameter_set: dict = field(default_factory=dict)
    holding_days: int = 0
    version: str = "v1"
    is_active: bool = True

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class StrategyEvaluation:
    """策略评估结果。"""
    evaluation_id: str = ""
    query_event_id: str = ""
    strategy_id: str = ""
    strategy_name: str = ""
    strategy_category: str = ""
    sample_count: int = 0
    mean_return: float = 0.0
    median_return: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: Optional[float] = None
    worst_quantile_20: float = 0.0
    max_single_loss: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    calmar_ratio: float = 0.0
    avg_holding_days: float = 0.0
    bootstrap_lower: Optional[float] = None
    bootstrap_upper: Optional[float] = None
    return_skewness: float = 0.0
    return_kurtosis: float = 0.0
    robust_score: float = 0.0
    confidence: str = ""           # HIGH / MED / LOW
    confidence_note: str = ""
    returns: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class UniverseConfig:
    """股票池配置。"""
    universe_id: str = ""
    universe_type: str = ""        # trading / reference
    name: str = ""
    symbols: List[Dict] = field(default_factory=list)
    index_filter: Optional[Dict] = None

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WFSimulationRun:
    """Walk-Forward 验证运行。"""
    run_id: str
    simulation_start: str = ""
    simulation_end: str = ""
    trading_universe_id: str = ""
    reference_universe_id: str = ""
    max_position_pct: float = 10.0
    max_positions: int = 10
    initial_cash: float = 1_000_000.0
    benchmark_symbol: str = ""
    execution_assumption: str = "next_open"
    commission_rate: float = 0.00025
    slippage_rate: float = 0.001
    stamp_duty_rate: float = 0.001
    config_version: str = "v1"
    status: str = "pending"

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class TradeDetail:
    """模拟交易明细。"""
    run_id: str = ""
    trade_id: str = ""
    symbol: str = ""
    side: str = ""                 # long / flat
    entry_date: str = ""
    entry_price: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    position_pct: float = 0.0
    shares: float = 0.0
    entry_signal_event_id: str = ""
    exit_signal_type: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    similarity_query_id: str = ""
    reference_sample_count: int = 0
    expected_return_at_entry: Optional[float] = None
    expected_win_rate_at_entry: Optional[float] = None
    entry_commission: float = 0.0
    exit_commission: float = 0.0
    slippage_cost: float = 0.0
    stamp_duty: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0
    exit_reason: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class PortfolioSnapshot:
    """每日组合快照。"""
    run_id: str
    date: str
    portfolio_value: float = 0.0
    cash: float = 0.0
    cash_pct: float = 0.0
    positions_json: dict = field(default_factory=dict)
    gross_exposure: float = 0.0
    daily_return: float = 0.0
    cumulative_return: float = 0.0
    benchmark_value: float = 0.0
    benchmark_return: float = 0.0

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class DailyHoldingEvaluation:
    """每日持仓评估。"""
    run_id: str = ""
    date: str = ""
    holding_symbol: str = ""
    entry_date: str = ""
    entry_price: float = 0.0
    holding_days: int = 0
    current_signal_score: float = 0.0
    signal_decay_status: str = ""
    pattern_drift_score: float = 0.0
    updated_similarity_score: float = 0.0
    updated_strategy_score: float = 0.0
    expected_return: float = 0.0
    expected_drawdown: float = 0.0
    opportunity_cost_score: float = 0.0
    hold_score: float = 0.0
    risk_action: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class DailyRebalanceDecision:
    """每日再平衡决策。"""
    run_id: str = ""
    date: str = ""
    decision_type: str = ""       # hold / switch / clear / new_buy / cash
    current_holding_symbol: str = ""
    best_candidate_symbol: str = ""
    current_hold_score: float = 0.0
    best_candidate_score: float = 0.0
    cash_score: float = 0.0
    switch_threshold: float = 0.0
    position_pct: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class ProgressiveRetrievalLog:
    """递进检索日志。"""
    query_id: str = ""
    as_of_date: str = ""
    symbol: str = ""
    signal_event_id: str = ""
    stage: str = ""               # t20 / t40 / t60 / env
    lookback_days: int = 0
    input_candidate_count: int = 0
    output_candidate_count: int = 0
    candidate_window_ids: List[str] = field(default_factory=list)
    candidate_scores: List[float] = field(default_factory=list)
    feature_weights: dict = field(default_factory=dict)
    retrieval_config_version: str = "v1"

    def to_dict(self) -> dict:
        return _to_dict(self)
