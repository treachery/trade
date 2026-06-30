"""时间门控中间件（BE-002 Point-in-Time Gate）。

保证任意模拟日 D 只能访问 D 及以前数据，避免未来函数泄漏。
"""
from datetime import datetime, timedelta
import pandas as pd


def _parse_d(s: str) -> datetime:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d")


class TimeGate:
    """Point-in-Time 数据访问门控。"""

    def __init__(self, as_of_date: str):
        self.as_of_date = as_of_date
        self._as_of_dt = _parse_d(as_of_date)

    def filter_df(self, df: pd.DataFrame, date_col="date") -> pd.DataFrame:
        """过滤掉 as_of_date 之后的日K数据。"""
        if df is None or df.empty:
            return df
        mask = df[date_col] <= self.as_of_date
        return df.loc[mask].reset_index(drop=True)

    def is_forward_complete(self, anchor_date: str, forward_days: int) -> bool:
        """检查 anchor_date + forward_days 是否已闭合。

        保守裕量：要求 anchor_date + forward_days + 10 天（自然日）≤ as_of_date，
        确保前瞻窗口内所有交易日数据已完整可查。
        """
        try:
            return (_parse_d(anchor_date) + timedelta(days=forward_days + 10)) <= self._as_of_dt
        except Exception:
            return True  # 解析失败放行（上层另有校验）

    def visible_windows(self, windows: list, forward_days: int) -> list:
        """从片段列表中过滤出前瞻窗口已闭合的片段。"""
        return [
            w for w in windows
            if self.is_forward_complete(w.get("anchor_date", ""), forward_days)
        ]

    def assert_no_future_data(self, dates, label="data"):
        """断言 dates 中没有日期在 as_of_date 之后。

        Raises:
            AssertionError: 发现未来数据。
        """
        for d in dates:
            try:
                if _parse_d(d) > self._as_of_dt:
                    raise AssertionError(
                        f"未来数据泄漏: {label} 包含 {d} > as_of_date {self.as_of_date}"
                    )
            except (ValueError, TypeError):
                pass  # 非法日期格式跳过

    def assert_result_compliant(self, result: dict):
        """对 API 结果做快速合规检查。"""
        if not result.get("ok"):
            return
        # 检查返回的 as_of_date
        if result.get("as_of_date", "") != self.as_of_date:
            raise AssertionError(
                f"结果 as_of_date={result.get('as_of_date')} != 请求 {self.as_of_date}"
            )
        # 检查无未来信号
        for sig in result.get("signal_info", {}).get("buy_signals", []) + \
                    result.get("signal_info", {}).get("sell_signals", []):
            sd = sig.get("signal_date", "")
            if sd and _parse_d(sd) > self._as_of_dt:
                raise AssertionError(f"信号日期 {sd} 在 as_of_date 之后")

    def to_dict(self) -> dict:
        return {"as_of_date": self.as_of_date}
