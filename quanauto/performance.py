"""绩效分析 —— `PerformanceAnalyzer`（契约 §2.5.1）。

## `calculate_win_rate` 这类方法到底怎么算「赢」

契约把 `calculate_win_rate(self, trades: List[Trade]) -> float` 摆在那儿，但
`Trade`（§2.4.2，10 个字段）**没有盈亏字段**，只有方向、价格、数量、费用、时间戳。
所以「一笔交易是赢是亏」必须自己从成交流水推：按标的做 **FIFO 逐笔配对**，
买入建 lot、卖出平 lot，一次平仓算一个 round trip，其盈亏 = 价差 − 两侧分摊的费用。

这不是我发明的口径，是「只给成交明细」时唯一算得出来的口径。分摊方式（费用按股数比例
摊到每个 lot）写在 `round_trips()` 里，可复核。

## 空输入一律返回 0，不抛异常

契约没说空输入怎么办。全 0 是「没有数据 ⇒ 没有指标」最不容易被误读的表达，比抛
`ZeroDivisionError` 好。**代价**是：全 0 的 `PerformanceMetrics` 与「真的算出来全是 0」
长得一样 —— 所以可复现性门禁里有一条硬守卫，要求报告里必须有 ≥1 笔成交且净值非平凡，
否则判 FAIL（防空转）。指标 API 不该替调用方做这个判断，门禁来做。
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Tuple

from .enums import BacktestStatus, Direction
from .models import (
    AccountSnapshot,
    BacktestResult,
    DrawdownMetrics,
    EquityPoint,
    Order,
    PerformanceMetrics,
    ReturnMetrics,
    RiskMetrics,
    Trade,
)

TRADING_DAYS = 252
SECONDS_PER_DAY = 86400.0


# ── 模块级数值工具 ────────────────────────────────────────────────────────
def mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: List[float]) -> float:
    """总体标准差（不是样本标准差）。样本口径在 n=2 时能把一个点放大成 sqrt(2) 倍，
    回测里序列本来就短，用总体口径更稳。"""
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def percentile(values: List[float], q: float) -> float:
    """线性插值分位数（不引 numpy）。`q` 取 0~100。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q / 100.0
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def relative_returns(equity_curve: List[float]) -> List[float]:
    """相邻两点的相对收益。基点净值为 0 时跳过那一段（不出 inf）。"""
    out: List[float] = []
    for prev, cur in zip(equity_curve, equity_curve[1:]):
        out.append((cur - prev) / prev if prev else 0.0)
    return out


def drawdown_series(equity_curve: List[float]) -> List[float]:
    """逐点回撤（正数百分比）。"""
    peak = 0.0
    out: List[float] = []
    for value in equity_curve:
        peak = max(peak, value)
        out.append((peak - value) / peak if peak else 0.0)
    return out


def round_trips(trades: List[Trade]) -> List[Tuple[float, float, datetime, datetime]]:
    """FIFO 配对，每个 round trip 返回 `(盈亏金额, 价差额, 建仓时间, 平仓时间)`。

    盈亏单位是**金额**（不是百分比）：这样 `calculate_profit_factor(gross_profit,
    gross_loss)` 那两个金额入参才有来源。费用按股数比例摊，未配平的挂单不收尾
    （剩下的 lot 没有平仓价，算不出盈亏，硬算就是编数据）。
    """
    books: Dict[str, Deque[List[float]]] = {}
    trips: List[Tuple[float, float, datetime, datetime]] = []
    for trade in trades:
        book = books.setdefault(trade.symbol, deque())
        if trade.direction is Direction.BUY:
            fee_rate = trade.trade_fee / trade.quantity if trade.quantity else 0.0
            book.append([float(trade.quantity), float(trade.price), fee_rate, trade.timestamp])
            continue
        remaining = int(trade.quantity)
        sell_fee_rate = trade.trade_fee / trade.quantity if trade.quantity else 0.0
        while remaining > 0 and book:
            lot = book[0]
            matched = int(min(remaining, lot[0]))
            pnl = (trade.price - lot[1]) * matched - lot[2] * matched - sell_fee_rate * matched
            trips.append((pnl, matched * (trade.price - lot[1]), lot[3], trade.timestamp))
            lot[0] -= matched
            remaining -= matched
            if lot[0] == 0:
                book.popleft()
    return trips


class PerformanceAnalyzer:
    """绩效指标计算器。全部方法都是无状态的纯计算（没有 `self` 状态）。"""

    def analyze(
        self,
        trades: List[Trade],
        orders: List[Order],
        account_history: List[AccountSnapshot]
    ) -> BacktestResult:
        """由成交、订单、账户快照算出 `BacktestResult`。

        **请求域字段（`strategy_id` / 版本 / `params_used` / `duration_ms`）在这里填
        占位值**（空串 / 0），由 `BacktestEngine.run()` 盖上去 —— 分析器拿不到这些上下文，
        硬要它猜就是编数据。
        """
        equity = [s.total_capital for s in account_history]
        returns = relative_returns(equity)
        returns_metrics = self.calculate_returns(equity)
        drawdown = self.calculate_drawdown(equity)
        risk = self.calculate_risk(returns, 0.0)
        trips = round_trips(trades)
        gross_profit = sum(pnl for pnl, _, _, _ in trips if pnl > 0)
        gross_loss = -sum(pnl for pnl, _, _, _ in trips if pnl < 0)

        curve: List[EquityPoint] = []
        dd_curve = drawdown.drawdown_curve
        for index, snapshot in enumerate(account_history):
            curve.append(
                EquityPoint(
                    timestamp=snapshot.timestamp,
                    equity=snapshot.total_capital,
                    cash=snapshot.available_capital,
                    market_value=snapshot.market_value,
                    drawdown=dd_curve[index] if index < len(dd_curve) else 0.0,
                )
            )

        performance = PerformanceMetrics(
            total_return=returns_metrics.total_return,
            annual_return=returns_metrics.annual_return,
            max_drawdown=drawdown.max_drawdown,
            sharpe_ratio=risk.sharpe_ratio,
            sortino_ratio=risk.sortino_ratio,
            calmar_ratio=risk.calmar_ratio,
            win_rate=self.calculate_win_rate(trades),
            profit_factor=self.calculate_profit_factor(gross_profit, gross_loss),
            profit_loss_ratio=self.calculate_profit_loss_ratio(trades),
            max_consecutive_losses=self.calculate_max_consecutive_losses(trades),
            avg_hold_period=self.calculate_avg_hold_period(trades),
            total_trades=len(trips),
            total_commission=sum(t.commission for t in trades),
            final_equity=equity[-1] if equity else 0.0,
        )
        return BacktestResult(
            strategy_id="",
            status=BacktestStatus.SUCCESS,
            start_time=account_history[0].timestamp if account_history else datetime(1970, 1, 1),
            end_time=account_history[-1].timestamp if account_history else datetime(1970, 1, 1),
            duration_ms=0,
            performance=performance,
            equity_curve=curve,
            trades=list(trades),
            orders=list(orders),
            account_history=list(account_history),
            error_message=None,
            data_version="",
            strategy_version="",
            params_used={},
            validation_report=None,
        )

    def calculate_returns(self, equity_curve: List[float]) -> ReturnMetrics:
        """收益指标。年化按 252 个交易日折算（基点净值 ≤ 0 时不年化，直接 0）。"""
        daily = relative_returns(equity_curve)
        total = 0.0
        if len(equity_curve) >= 2 and equity_curve[0]:
            total = (equity_curve[-1] - equity_curve[0]) / equity_curve[0]
        cumulative: List[float] = []
        running = 1.0
        for value in daily:
            running *= 1.0 + value
            cumulative.append(running - 1.0)
        annual = 0.0
        if daily and equity_curve[0] > 0:
            base = 1.0 + total
            if base <= 0.0:
                # 亏光了（或数据怪异到净值归零以下）。负数底数的分数次幂在 Python 里
                # 会**静默返回复数**，一路带着走会到 json.dumps 才炸，所以在这里就拦住。
                annual = -1.0
            else:
                try:
                    annual = base ** (TRADING_DAYS / len(daily)) - 1.0
                except OverflowError:
                    # 极短序列 + 极大收益会让指数爆炸。年化在这种输入下没意义，记 0。
                    annual = 0.0
        return ReturnMetrics(
            total_return=total,
            annual_return=annual,
            daily_return_mean=mean(daily),
            daily_return_std=stdev(daily),
            cumulative_returns=cumulative,
            daily_returns=daily,
        )

    def calculate_risk(
        self,
        returns: List[float],
        risk_free_rate: float
    ) -> RiskMetrics:
        """风险指标。

        `information_ratio` / `tracking_error` / `beta` / `alpha` 一律 `0.0`：
        这四个必须有**基准收益序列**，而本方法的入参里没有基准。填 0.0 的含义是
        「没有输入」，不是「算出来是 0」—— 要这几个指标得先用 `DataFeed` 取基准数据。
        """
        equity: List[float] = [1.0]
        for value in returns:
            equity.append(equity[-1] * (1.0 + value))
        drawdown = self.calculate_drawdown(equity)
        annual_return = self.calculate_returns(equity).annual_return
        threshold = percentile(returns, 5.0)
        tail = [r for r in returns if r <= threshold] if returns else []
        return RiskMetrics(
            sharpe_ratio=self.calculate_sharpe(returns, risk_free_rate),
            sortino_ratio=self.calculate_sortino(returns, risk_free_rate),
            calmar_ratio=self.calculate_calmar(annual_return, drawdown.max_drawdown),
            information_ratio=0.0,
            tracking_error=0.0,
            beta=0.0,
            alpha=0.0,
            volatility_annual=stdev(returns) * math.sqrt(TRADING_DAYS),
            value_at_risk_95=-percentile(returns, 5.0),
            value_at_risk_99=-percentile(returns, 1.0),
            conditional_var_95=-mean(tail) if tail else 0.0,
        )

    def calculate_drawdown(self, equity_curve: List[float]) -> DrawdownMetrics:
        """回撤指标。`max_drawdown_duration` 单位是**采样点数**（净值序列一天一个点，
        所以等于交易日数）—— 契约写的是「持续天数」，只有日频序列下两者才相等，
        这点在 docstring 里点明，免得周频数据上被当成 bug。
        """
        curve = drawdown_series(equity_curve)
        peak = 0.0
        duration = 0
        longest = 0
        for value in equity_curve:
            peak = max(peak, value)
            if value < peak:
                duration += 1
                longest = max(longest, duration)
            else:
                duration = 0
        return DrawdownMetrics(
            max_drawdown=max(curve) if curve else 0.0,
            max_drawdown_duration=longest,
            current_drawdown=curve[-1] if curve else 0.0,
            drawdown_curve=curve,
        )

    def calculate_sharpe(
        self,
        returns: List[float],
        risk_free_rate: float
    ) -> float:
        """夏普比率。无风险利率按**年化**输入，折成日频；波动为 0 时返回 0（不出 inf）。"""
        if not returns:
            return 0.0
        sigma = stdev(returns)
        if sigma == 0.0:
            return 0.0
        excess = mean(returns) - risk_free_rate / TRADING_DAYS
        return excess / sigma * math.sqrt(TRADING_DAYS)

    def calculate_sortino(
        self,
        returns: List[float],
        risk_free_rate: float
    ) -> float:
        """索提诺比率。分母是**下行**标准差；没有负收益时返回 0。"""
        if not returns:
            return 0.0
        downside = [r for r in returns if r < 0.0]
        if not downside:
            return 0.0
        sigma = math.sqrt(sum(r * r for r in downside) / len(returns))
        if sigma == 0.0:
            return 0.0
        excess = mean(returns) - risk_free_rate / TRADING_DAYS
        return excess / sigma * math.sqrt(TRADING_DAYS)

    def calculate_calmar(
        self,
        annual_return: float,
        max_drawdown: float
    ) -> float:
        """卡玛比率。回撤为 0 时返回 0（不是 inf —— 无穷大在 JSON 里不合法）。"""
        if max_drawdown == 0.0:
            return 0.0
        return annual_return / max_drawdown

    def calculate_profit_factor(
        self,
        gross_profit: float,
        gross_loss: float
    ) -> float:
        """盈亏比（总盈利 / 总亏损）。没有亏损时返回 0，理由同上：inf 不可序列化。"""
        if gross_loss == 0.0:
            return 0.0
        return gross_profit / gross_loss

    def calculate_win_rate(self, trades: List[Trade]) -> float:
        """胜率 = 盈利 round trip 数 / 总 round trip 数。没有平仓交易时返回 0。"""
        trips = round_trips(trades)
        if not trips:
            return 0.0
        return sum(1 for pnl, _, _, _ in trips if pnl > 0) / len(trips)

    def calculate_profit_loss_ratio(self, trades: List[Trade]) -> float:
        """平均盈利 / 平均亏损（绝对额）。任一侧为空时返回 0。"""
        trips = round_trips(trades)
        wins = [pnl for pnl, _, _, _ in trips if pnl > 0]
        losses = [-pnl for pnl, _, _, _ in trips if pnl < 0]
        if not wins or not losses:
            return 0.0
        average_loss = mean(losses)
        if average_loss == 0.0:
            return 0.0
        return mean(wins) / average_loss

    def calculate_max_consecutive_losses(self, trades: List[Trade]) -> int:
        """最大连续亏损次数（按平仓时间顺序）。"""
        longest = 0
        current = 0
        for pnl, _, _, _ in round_trips(trades):
            if pnl < 0:
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return longest

    def calculate_avg_hold_period(self, trades: List[Trade]) -> float:
        """平均持仓周期，单位**天**（= 平仓时间 − 建仓时间 的均值）。"""
        trips = round_trips(trades)
        if not trips:
            return 0.0
        spans = [(close_at - open_at).total_seconds() / SECONDS_PER_DAY for _, _, open_at, close_at in trips]
        return mean(spans)
