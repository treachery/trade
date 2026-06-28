"""策略配置：可选的 5 个入场策略 + 5 个出场策略，支持 AND / OR 组合。

入场(entry) 可选类型：
  - "ma_golden"         双均线金叉：MA(fast) 上穿 MA(slow)         参数 fast=50, slow=200
  - "donchian_breakout" 唐奇安通道突破：收盘创近 period 日新高      参数 period=20
  - "ma_bull_stack"     均线多头排列：MA5>MA10>MA20>MA60 且价在MA5上 参数 periods=[5,10,20,60]
  - "macd_golden"       MACD零轴上金叉：DIF上穿DEA 且 DIF>0          参数 fast=12, slow=26, signal=9
  - "volume_breakout"   量价突破：收盘创近 period 日新高 且 量>均量×mult  参数 period=20, vol_mult=1.5

出场(exit) 可选类型：
  - "ma_break"          收盘跌破 MA(period)                          参数 period=20
  - "chandelier_atr"    吊灯ATR止损：收盘跌破 (持仓最高价 - mult×ATR)  参数 atr_period=22, mult=3
  - "trailing_pct"      移动止盈：收盘较持仓最高收盘回撤超 pct%        参数 pct=10
  - "donchian_exit"     唐奇安下轨：收盘创近 period 日新低            参数 period=10
  - "ma_death_cross"    双均线死叉：MA(fast) 下穿 MA(slow)            参数 fast=50, slow=200

组合逻辑：
  entry_logic / exit_logic = "or"（任一满足）或 "and"（同时满足）。
  入场在"组合条件由假变真"的当天买入；出场在"组合条件成立"的当天卖出。
"""
from dataclasses import dataclass, field

ENTRY_TYPES = ("ma_golden", "donchian_breakout", "ma_bull_stack", "macd_golden", "volume_breakout")
EXIT_TYPES = ("ma_break", "chandelier_atr", "trailing_pct", "donchian_exit", "ma_death_cross")

# 寻优用的默认参数（与前端默认值一致）
ENTRY_DEFAULTS = [
    {"type": "ma_golden", "fast": 50, "slow": 200},
    {"type": "donchian_breakout", "period": 20},
    {"type": "ma_bull_stack", "periods": [5, 10, 20, 60]},
    {"type": "macd_golden", "fast": 12, "slow": 26, "signal": 9},
    {"type": "volume_breakout", "period": 20, "vol_mult": 1.5},
]
EXIT_DEFAULTS = [
    {"type": "ma_break", "period": 20},
    {"type": "chandelier_atr", "atr_period": 22, "mult": 3},
    {"type": "trailing_pct", "pct": 10},
    {"type": "donchian_exit", "period": 10},
    {"type": "ma_death_cross", "fast": 50, "slow": 200},
]
CN_LABEL = {
    "ma_golden": "双均线金叉", "donchian_breakout": "唐奇安突破", "ma_bull_stack": "均线多头排列",
    "macd_golden": "MACD金叉", "volume_breakout": "量价突破",
    "ma_break": "跌破均线", "chandelier_atr": "吊灯ATR", "trailing_pct": "移动止盈",
    "donchian_exit": "唐奇安下轨", "ma_death_cross": "双均线死叉",
}


# 建仓策略(entry)：
#   pe_percentile  按"买入点近5年PE百分位"线性映射仓位：分位0%→最高仓位，100%→最低仓位
#   fixed          固定满仓 = 用户设置的最高仓位(max_leverage)
ENTRY_POSITION_TYPES = ("pe_percentile", "fixed")
# 减仓策略(reduce)：
#   none           不减仓
#   pe_percentile  持仓中PE近5年百分位每上升 reduce_step 个点，按PE曲线降到对应仓位
#   profit         相对买入点每上涨 reduce_step%，仓位下调 reduce_pct 个百分点
REDUCE_POSITION_TYPES = ("none", "pe_percentile", "profit")


def _default_position():
    return {
        "entry": "pe_percentile",      # 建仓策略
        "reduce": "pe_percentile",     # 减仓策略
        "max_leverage": 2.0,           # 最高仓位(也是 fixed 建仓的满仓值、PE曲线上沿)
        "min_leverage": 0.5,           # 最低仓位(PE曲线下沿)
        "reduce_step": 10.0,           # pe_percentile:百分位点步长 / profit:涨幅%步长
        "reduce_pct": 10.0,            # profit 专用：每步下调的仓位百分点
    }


@dataclass
class StrategyConfig:
    entries: list = field(default_factory=list)  # [{"type":..., 其它参数}]
    exits: list = field(default_factory=list)
    entry_logic: str = "or"   # "or" / "and"
    exit_logic: str = "or"
    entry_window: int = 5     # 入场AND容忍窗口(交易日)：各策略在窗口内先后满足即触发；1=同天满足
    exit_window: int = 5      # 出场AND容忍窗口(交易日)：各策略在窗口内先后满足即触发；1=同天满足
    # 仓位管理：建仓(entry) + 减仓(reduce) 两段解耦
    position: dict = field(default_factory=_default_position)

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        d = d or {}
        entries = [e for e in d.get("entries", []) if isinstance(e, dict) and e.get("type") in ENTRY_TYPES]
        exits = [e for e in d.get("exits", []) if isinstance(e, dict) and e.get("type") in EXIT_TYPES]
        el = "and" if str(d.get("entry_logic", "or")).lower() == "and" else "or"
        xl = "and" if str(d.get("exit_logic", "or")).lower() == "and" else "or"
        entry_window = max(1, int(d.get("entry_window", 5)))
        exit_window = max(1, int(d.get("exit_window", 5)))

        pos = d.get("position") or {}
        max_lev = float(pos.get("max_leverage", 2.0))
        min_lev = float(pos.get("min_leverage", 0.5))

        # 建仓策略（兼容旧结构 type=fixed/pe_percentile，旧 fixed.fraction 作为满仓）
        entry = pos.get("entry")
        if entry is None:
            entry = "fixed" if pos.get("type") == "fixed" else "pe_percentile"
        if entry not in ENTRY_POSITION_TYPES:
            entry = "pe_percentile"
        if pos.get("type") == "fixed" and "fraction" in pos and "max_leverage" not in pos:
            max_lev = float(pos.get("fraction", 1.0))

        # 减仓策略（兼容旧 deleverage_step：>0 视为 pe_percentile 减仓）
        reduce = pos.get("reduce")
        if reduce is None:
            ds = float(pos.get("deleverage_step", 0) or 0)
            reduce = "pe_percentile" if ds > 0 else "none"
        if reduce not in REDUCE_POSITION_TYPES:
            reduce = "none"

        reduce_step = float(pos.get("reduce_step", pos.get("deleverage_step", 10.0)) or 0)
        reduce_pct = float(pos.get("reduce_pct", 10.0) or 0)

        position = {
            "entry": entry, "reduce": reduce,
            "max_leverage": max_lev, "min_leverage": min_lev,
            "reduce_step": reduce_step, "reduce_pct": reduce_pct,
        }
        return cls(entries=entries, exits=exits, entry_logic=el, exit_logic=xl,
                   entry_window=entry_window, exit_window=exit_window, position=position)

    def to_dict(self):
        return {
            "entries": self.entries,
            "exits": self.exits,
            "entry_logic": self.entry_logic,
            "exit_logic": self.exit_logic,
            "entry_window": self.entry_window,
            "exit_window": self.exit_window,
            "position": self.position,
        }
