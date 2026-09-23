"""策略 —— `Strategy` 抽象基类（契约 §2.8 / §2.9）与 `MA_Cross_Strategy`。

## 为什么 `MA_Cross_Strategy` 的构造签名是 `(strategy_id, config)`

契约里策略类名**只**出现在 §2.9「回测引擎使用示例」里（`MA_Cross_Strategy(short_window=5,
long_window=20)`）。而 §2.9 已被判为**非规范**（见契约附录 E1）——它同时把
`BacktestConfig` / `CsvDataFeed` / `BacktestEngine` 的构造签名都写错了。

规范侧的 `Strategy.__init__` 是 `(self, strategy_id: str, config: Dict[str, Any])`，
`BacktestEngine.add_strategy(strategy, capital, params)` 也按策略对象注入。
所以这里跟规范走：`MA_Cross_Strategy("ma-cross", {"short_window": 5, "long_window": 20})`，
同时把 `short_window` / `long_window` 做成**只读属性**，让 §2.9 里那个直观的用法
（`strategy.short_window`）依然成立。

## 与契约 §2.8 的一处有意偏差：`on_data` 不是纯函数

契约要求 `on_data(data) -> List[TradingSignal]` 是「纯函数风格，不修改策略内部可变状态」。
均线天然需要一个滑窗，所以 `MA_Cross_Strategy` 维护了 `_closes` / `_prev_short` /
`_prev_long`。妥协办法：滑窗**只**保留 `long_window` 个点（有界），并且状态由
`on_start()` 全量重置 —— 也就是说 `on_start()` 之后的行为只由输入序列决定，
同一个输入序列产生同一串信号。这条偏差记在契约附录 E5。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from .enums import DirectionEnum, SignalTypeEnum, StrategyStatusEnum
from .errors import InvalidConfigError, InvalidParamError, StrategyError
from .models import (
    BarData,
    MarketDataBundle,
    Order,
    PositionTarget,
    StrategyInfo,
    Trade,
    TradingSignal,
)

LOGGER = logging.getLogger("quanauto.strategy")

# A 股一手 100 股。目标仓位按手取整，避免「买了 37 股」这种回测里不存在的情形。
LOT_SIZE = 100


class Strategy(ABC):
    """策略基类。10 个抽象成员 + 2 个带默认实现的钩子。"""

    @abstractmethod
    def __init__(self, strategy_id: str, config: Dict[str, Any]) -> None:
        """子类必须自己解析 `config`，解析失败要抛 `InvalidConfigError`。"""

    @abstractmethod
    def on_start(self) -> None:
        """启动回调。**必须幂等**：连调两次不能变成「跑了两遍」。"""

    @abstractmethod
    def on_data(self, data: MarketDataBundle) -> List[TradingSignal]:
        """数据回调。返回 0..n 个信号（无信号返回空列表，不是 None）。"""

    def on_bar(self, bar: BarData) -> None:
        """K 线回调。基类默认**什么都不做** —— 默认实现去调 `on_data` 需要把一个
        `BarData` 拼成 `MarketDataBundle`，而 `MarketDataBundle` 的必填字段
        （`adjust_factor` / `previous_close`）在 `BarData` 里没有，硬拼就是编数据。
        走 `on_data` 的路径由引擎负责。
        """

    @abstractmethod
    def on_order(self, order: Order) -> None:
        """订单状态变更回调。"""

    @abstractmethod
    def on_trade(self, trade: Trade) -> None:
        """成交回调。"""

    @abstractmethod
    def on_stop(self) -> None:
        """停止回调。**必须幂等**。"""

    @abstractmethod
    def get_strategy_info(self) -> StrategyInfo:
        """策略元信息。"""

    @abstractmethod
    def get_target_positions(self) -> Dict[str, PositionTarget]:
        """当前的目标持仓（策略自己想持多少，不是账户实际有多少）。"""

    @abstractmethod
    def get_strategy_params(self) -> Dict[str, Any]:
        """当前参数。必须是**拷贝**，不然调用方一改就穿透到策略内部。"""

    @abstractmethod
    def update_params(self, params: Dict[str, Any]) -> bool:
        """运行期更新参数。非法参数抛 `InvalidParamError`，不要静默吞掉。"""

    def on_error(self, error: StrategyError) -> None:
        """异常回调。基类默认记一条 warning 并留在 `last_error` 里，
        这样引擎不必为了「出错了要知道」而额外包一层 try。
        """
        self.last_error = error
        LOGGER.warning("策略 %s 异常: %s", getattr(self, "strategy_id", "?"), error)


class MA_Cross_Strategy(Strategy):
    """双均线交叉。金叉买入、死叉清仓。

    参数（`config` 键）：`short_window`（int ≥ 1）、`long_window`（int ≥ 1，
    且必须大于 `short_window`）、`capital`（float ≥ 0，用于算目标手数）、
    `symbol`（str，可选；不填就用第一根数据里的 symbol）。
    """

    def __init__(self, strategy_id: str, config: Dict[str, Any]) -> None:
        self.strategy_id = strategy_id
        self.status = StrategyStatusEnum.IDLE
        self.last_error: Optional[StrategyError] = None
        self._config: Dict[str, Any] = {}
        self._closes: Deque[float] = deque()
        self._prev_short: Optional[float] = None
        self._prev_long: Optional[float] = None
        self._target: Dict[str, PositionTarget] = {}
        self._created_at = datetime.now()
        self._apply_config(config)

    # ── config ───────────────────────────────────────────────────────
    def _apply_config(self, config: Dict[str, Any]) -> None:
        """校验并落地参数。校验先做完再改状态 —— 半校验的配置会让对象停在
        「一半新一半旧」的状态，比直接抛异常糟得多。"""
        if not isinstance(config, dict):
            raise InvalidConfigError("策略配置必须是 dict，收到 %r" % (type(config).__name__,))
        merged = dict(self._config)
        merged.update(config)
        short = merged.get("short_window")
        long = merged.get("long_window")
        capital = merged.get("capital", 0.0)
        symbol = merged.get("symbol", "")
        if not isinstance(short, int) or isinstance(short, bool) or short < 1:
            raise InvalidConfigError("short_window 必须是 ≥1 的整数，收到 %r" % (short,))
        if not isinstance(long, int) or isinstance(long, bool) or long < 1:
            raise InvalidConfigError("long_window 必须是 ≥1 的整数，收到 %r" % (long,))
        if short >= long:
            raise InvalidConfigError(
                "short_window(%d) 必须小于 long_window(%d)" % (short, long)
            )
        if not isinstance(capital, (int, float)) or isinstance(capital, bool) or capital < 0:
            raise InvalidConfigError("capital 必须是 ≥0 的数，收到 %r" % (capital,))
        if not isinstance(symbol, str):
            raise InvalidConfigError("symbol 必须是字符串，收到 %r" % (symbol,))
        self._config = {
            "short_window": int(short),
            "long_window": int(long),
            "capital": float(capital),
            "symbol": symbol,
        }

    @property
    def short_window(self) -> int:
        return int(self._config["short_window"])

    @property
    def long_window(self) -> int:
        return int(self._config["long_window"])

    # ── 生命周期 ──────────────────────────────────────────────────────
    def on_start(self) -> None:
        """幂等：连调两次得到的滑窗状态与调一次完全相同。"""
        self._closes.clear()
        self._prev_short = None
        self._prev_long = None
        self._target = {}
        self.status = StrategyStatusEnum.RUNNING

    def on_stop(self) -> None:
        self.status = StrategyStatusEnum.STOPPED

    # ── 数据 ─────────────────────────────────────────────────────────
    def on_data(self, data: MarketDataBundle) -> List[TradingSignal]:
        self._closes.append(float(data.close))
        while len(self._closes) > self.long_window:
            self._closes.popleft()
        if len(self._closes) < self.long_window:
            return []

        values = list(self._closes)
        short_ma = sum(values[-self.short_window:]) / self.short_window
        long_ma = sum(values) / self.long_window
        signals: List[TradingSignal] = []
        symbol = self._config["symbol"] or data.symbol

        if self._prev_short is not None and self._prev_long is not None:
            golden = self._prev_short <= self._prev_long and short_ma > long_ma
            death = self._prev_short >= self._prev_long and short_ma < long_ma
            if golden:
                quantity = self._target_quantity(data.close)
                signals.append(
                    self._make_signal(
                        symbol=symbol,
                        direction=DirectionEnum.BUY,
                        signal_type=SignalTypeEnum.OPEN,
                        quantity=quantity,
                        price=data.close,
                        stamp=data.datetime,
                        reason="MA golden cross short=%d long=%d" % (self.short_window, self.long_window),
                    )
                )
                self._target = {
                    symbol: PositionTarget(symbol=symbol, target_quantity=quantity, reason="golden cross")
                }
            elif death:
                held = self._target.get(symbol)
                quantity = held.target_quantity if held else 0
                signals.append(
                    self._make_signal(
                        symbol=symbol,
                        direction=DirectionEnum.SELL,
                        signal_type=SignalTypeEnum.CLOSE,
                        quantity=quantity,
                        price=data.close,
                        stamp=data.datetime,
                        reason="MA death cross short=%d long=%d" % (self.short_window, self.long_window),
                    )
                )
                self._target = {}

        self._prev_short = short_ma
        self._prev_long = long_ma
        return signals

    def _target_quantity(self, price: float) -> int:
        """按 `capital` 与当前价算目标手数（向下取整到整手）。"""
        if price <= 0:
            return 0
        lots = int(self._config["capital"] // (price * LOT_SIZE))
        return max(0, lots) * LOT_SIZE

    def _make_signal(
        self,
        symbol: str,
        direction: DirectionEnum,
        signal_type: SignalTypeEnum,
        quantity: int,
        price: float,
        stamp: datetime,
        reason: str,
    ) -> TradingSignal:
        return TradingSignal(
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=direction,
            signal_type=signal_type,
            target_quantity=quantity,
            target_price=price,
            priority=5,
            timestamp=stamp,
            reason=reason,
        )

    def on_order(self, order: Order) -> None:
        """I1 不在策略侧维护订单状态（账户状态在 broker 手里，两处维护必然打架）。"""

    def on_trade(self, trade: Trade) -> None:
        """同上。"""

    # ── 内省 ─────────────────────────────────────────────────────────
    def get_strategy_info(self) -> StrategyInfo:
        return StrategyInfo(
            strategy_id=self.strategy_id,
            strategy_name="MA_Cross_Strategy",
            version="1.0.0",
            description="双均线交叉：金叉买入、死叉清仓",
            author="quanauto",
            created_at=self._created_at,
            updated_at=datetime.now(),
            status=self.status,
            params_schema={
                "short_window": {"type": "int", "minimum": 1},
                "long_window": {"type": "int", "minimum": 2},
                "capital": {"type": "number", "minimum": 0},
                "symbol": {"type": "string"},
            },
        )

    def get_target_positions(self) -> Dict[str, PositionTarget]:
        return {s: PositionTarget(symbol=p.symbol, target_quantity=p.target_quantity, reason=p.reason) for s, p in self._target.items()}

    def get_strategy_params(self) -> Dict[str, Any]:
        return dict(self._config)

    def update_params(self, params: Dict[str, Any]) -> bool:
        """运行期改参数。校验失败抛 `InvalidParamError`（**不是** `InvalidConfigError`）——
        调用方由此能区分「构造时配置就错」和「运行期改错了」，两者恢复动作不同。
        """
        try:
            self._apply_config(params)
        except InvalidConfigError as exc:
            raise InvalidParamError(str(exc)) from exc
        return True
