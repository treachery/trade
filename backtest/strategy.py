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


POSITION_TYPES = ("pe_percentile", "fixed")


@dataclass
class StrategyConfig:
    entries: list = field(default_factory=list)  # [{"type":..., 其它参数}]
    exits: list = field(default_factory=list)
    entry_logic: str = "or"   # "or" / "and"
    exit_logic: str = "or"
    # 仓位管理：决定入场时投入多少仓位(可>1=融资)
    #  pe_percentile: 仓位 = clamp((1-PE百分位)*2, min_leverage, max_leverage)
    #  fixed:         仓位 = fraction
    position: dict = field(default_factory=lambda: {
        "type": "pe_percentile", "max_leverage": 2.0, "min_leverage": 0.5, "deleverage_step": 10.0})

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        d = d or {}
        entries = [e for e in d.get("entries", []) if isinstance(e, dict) and e.get("type") in ENTRY_TYPES]
        exits = [e for e in d.get("exits", []) if isinstance(e, dict) and e.get("type") in EXIT_TYPES]
        el = "and" if str(d.get("entry_logic", "or")).lower() == "and" else "or"
        xl = "and" if str(d.get("exit_logic", "or")).lower() == "and" else "or"

        pos = d.get("position") or {}
        ptype = pos.get("type", "pe_percentile")
        if ptype not in POSITION_TYPES:
            ptype = "pe_percentile"
        if ptype == "fixed":
            position = {"type": "fixed", "fraction": float(pos.get("fraction", 1.0))}
        else:
            position = {"type": "pe_percentile",
                        "max_leverage": float(pos.get("max_leverage", 2.0)),
                        "min_leverage": float(pos.get("min_leverage", 0.5)),
                        "deleverage_step": float(pos.get("deleverage_step", 10.0))}
        return cls(entries=entries, exits=exits, entry_logic=el, exit_logic=xl, position=position)

    def to_dict(self):
        return {
            "entries": self.entries,
            "exits": self.exits,
            "entry_logic": self.entry_logic,
            "exit_logic": self.exit_logic,
            "position": self.position,
        }
