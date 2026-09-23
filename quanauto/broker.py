"""模拟撮合 —— `SimulatedBroker`（契约 §2.4.1）。

## 撮合时序（这是全篇最要紧的一条，写错了回测就自带未来函数）

```
第 t 根 K 线：engine 先 broker.on_bar(bar_t)   ← 上一根提交的单子在这里成交，成交价 = bar_t.open
              再 strategy.on_data(...)         ← 策略看到 bar_t 收盘后才产生信号
              最后 broker.submit_order(...)   ← 信号变成挂单，等第 t+1 根
```

成交价用**下一根的开盘价**而不是当根的收盘价，就是为了让「信号用的信息」严格早于
「成交用的价格」。用当根收盘价成交是隐藏的未来函数，很多回测框架栽在这儿。

## 随机源与可复现

`slippage_config.percentage_slippage > 0` 时，该笔的**滑点金额**按
`rate = pct + rng.uniform(-pct, +pct)` 抖动（模拟真实撮合的价差不确定性），
**默认 `percentage_slippage = 0.0` ⇒ 完全不抽随机数 ⇒ 退化成确定性撮合。**

滑点是**金额（元）**，和手续费一样直接从现金里扣，**不**去改成交价 —— §2.4.7 里
`fixed_slippage` / `min_slippage` / `max_slippage` 的单位全是「元」，`Trade.slippage`
的注释也是「滑点损失」。把滑点折进 `price` 会同时踩两个坑：`Trade.price` 不再是市场价；
而 `round_trips()` 是拿前后两个 `price` 之差算盈亏的，滑点再进一次 `trade_fee`
就是重复计费。

`rng` 是 `random.Random(seed)` 的**独立实例**，不碰全局 `random`：

* 用全局 `random` 的话，同进程里别人抽一次数就会改变本回测的结果 —— 那是「同一条命令
  跑两次指标不一致」的经典成因。
* 抽数的**次数**也必须确定：挂单是按提交顺序（`_pending` 列表）遍历的，不按 dict 序。

## 契约偏差

`SimulatedBroker` 在契约里**没有 `__init__`**（块里第一个成员就是 `submit_order`）。
这里加了 `__init__(self, initial_capital, seed=0, ...)`，登记在 manifest 的
`members_extra` 里。没有它就没法注入本金和随机种子。
"""

from __future__ import annotations

import random
import threading
from datetime import datetime
from typing import Dict, List, Optional

from .enums import Direction, EventType, OrderStatus, PositionDirection
from .errors import InsufficientFundsError, OrderRejectedError
from .events import Event, EventEngine
from .models import (
    Account,
    BarData,
    CommissionConfig,
    Fill,
    Order,
    OrderId,
    Position,
    SlippageConfig,
    Trade,
)


def commission_for(commission: CommissionConfig, direction: Direction, amount: float) -> float:
    """按 §2.4.6 的成本模型算这一笔的费率合计。

    要点：`stock_commission_rate` 有 `min_commission` 地板（默认 5 元），所以
    小额单的成本几乎全被地板支配 —— 「成交额 × 费率」这种算法在回测里会系统性低估成本。
    印花税按 `stamp_tax_on_sell_only` 决定是否只在卖出计。
    """
    rate_fee = amount * commission.stock_commission_rate
    fee = max(rate_fee, commission.min_commission)
    fee += amount * commission.transfer_fee
    fee += amount * commission.handling_fee
    if not commission.stamp_tax_on_sell_only or direction is Direction.SELL:
        fee += amount * commission.stamp_tax_rate
    return fee


def slippage_for(slippage: SlippageConfig, rng: random.Random, price: float, quantity: int) -> float:
    """单笔滑点**金额**（元，正数 = 亏）。抖动**只**在 `percentage_slippage > 0` 时才抽数。

    按 §2.4.7 的字面意思实现：`fixed_slippage` 是**每笔**的固定损失（「与交易金额无关」
    ⇒ **不**乘数量）；`percentage_slippage` 是成交额的比例；上下限 `min/max_slippage`
    也按「元」夹**总金额**（默认 `min_slippage = 0` ⇒ 抖到负数会被夹回 0，
    也就是「不占市场的便宜」，滑点不会变成收益）。

    `volume_impact_factor` 契约只说「与订单量占市场成交量的比例相关」，而
    `SlippageConfig` 里**没有市场成交量**这个输入，那个比例无从算起 ⇒ 这里把它当作
    成交额上的**额外比例**合并进 `percentage_slippage` 同一项。这是明确记下来的
    近似，不是从契约推导出来的；要真算冲击得先给 broker 喂成交量。

    每笔**恰好抽一次**数（`percentage_slippage > 0` 时），抽数次数与订单顺序绑定，
    所以同一个种子下抽数序列完全确定。
    """
    amount = float(price) * int(quantity)
    rate = slippage.percentage_slippage + slippage.volume_impact_factor
    if slippage.percentage_slippage > 0.0:
        rate += rng.uniform(-slippage.percentage_slippage, slippage.percentage_slippage)
    cost = slippage.fixed_slippage + rate * amount
    return min(max(cost, slippage.min_slippage), slippage.max_slippage)


class SimulatedBroker:
    """单标的维度的现金账户 + 多头持仓 + 限价/市价撮合。"""

    def __init__(
        self,
        initial_capital: float,
        seed: int = 0,
        commission: Optional[CommissionConfig] = None,
        slippage: Optional[SlippageConfig] = None,
        event_engine: Optional[EventEngine] = None,
    ) -> None:
        if initial_capital <= 0:
            raise OrderRejectedError("初始资金必须为正数，收到 %r" % (initial_capital,))
        self._initial = float(initial_capital)
        self._seed = int(seed)
        self._rng = random.Random(self._seed)
        self._commission = commission or CommissionConfig()
        self._slippage = slippage or SlippageConfig()
        self._events = event_engine
        self._lock = threading.RLock()
        self._orders: Dict[str, Order] = {}
        self._pending: List[str] = []
        self._trades: List[Trade] = []
        # `Trade` 在契约里是 10 个字段、**没有** `strategy_id`（§2.4.2 逐字如此），
        # 但 `get_trades(strategy_id)` 要按策略过滤。所以归属关系单独存一张表，
        # 而不是往 dataclass 上挂一个契约里不存在的属性（那会让 `Trade` 的
        # 相等性 / `repr` / 序列化在不同代码路径下表现不一致）。
        self._trade_owner: Dict[str, str] = {}
        self._fills: List[Fill] = []
        self._positions: Dict[str, Position] = {}
        self._last_price: Dict[str, float] = {}
        self._cash = self._initial
        self._frozen = 0.0
        self._seq = 0
        self._trade_seq = 0

    @property
    def seed(self) -> int:
        """随机种子。契约里没有这个成员（登记在 manifest 的 `members_extra`）。

        理由：报告必须能说明「这次结果的随机来自哪个种子」，否则「同一条命令跑两次结果一致」
        只是一个当时凑巧成立的观察，没人能复制。
        """
        return self._seed

    # ── 订单 ─────────────────────────────────────────────────────────
    def submit_order(self, order: Order) -> OrderId:
        """接收订单。**这里是唯一做资金校验的地方** —— 到成交时才发现钱不够，
        回测会静默跳过订单，看起来像「策略没发信号」。"""
        with self._lock:
            if order.quantity <= 0:
                raise OrderRejectedError("下单数量必须为正数，收到 %r" % (order.quantity,))
            if order.status not in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
                raise OrderRejectedError("订单状态 %s 不可提交" % order.status.value)
            estimate = order.price if order.price is not None else self._last_price.get(order.symbol)
            if estimate is None:
                raise OrderRejectedError("标的 %s 还没有参考价，无法估算成本" % order.symbol)
            gross = float(estimate) * order.quantity
            if order.direction is Direction.BUY:
                needed = gross + commission_for(self._commission, Direction.BUY, gross)
                if needed > self._cash - self._frozen:
                    raise InsufficientFundsError(
                        "可用资金 %.2f 不足以买入 %s x%d（需 %.2f）"
                        % (self._cash - self._frozen, order.symbol, order.quantity, needed)
                    )
                self._frozen += needed
            if not order.order_id:
                self._seq += 1
                order.order_id = "ord-%06d" % self._seq
            order.status = OrderStatus.SUBMITTED
            order.last_update_time = order.submit_time
            self._orders[order.order_id] = order
            self._pending.append(order.order_id)
            self._emit(EventType.ORDER_EVENT, order, order.submit_time, "broker")
            return OrderId(order.order_id)

    def cancel_order(self, order_id: OrderId) -> bool:
        """撤单。只有**还没成交**的单子能撤；已成交的返回 False（不抛异常）。"""
        with self._lock:
            order = self._orders.get(str(order_id))
            if order is None or order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                return False
            if order.direction is Direction.BUY:
                estimate = order.price if order.price is not None else self._last_price.get(order.symbol, 0.0)
                gross = float(estimate) * order.quantity
                self._frozen = max(0.0, self._frozen - gross - commission_for(self._commission, Direction.BUY, gross))
            order.status = OrderStatus.CANCELLED
            self._pending = [oid for oid in self._pending if oid != str(order_id)]
            return True

    # ── 查询 ─────────────────────────────────────────────────────────
    def get_account(self) -> Account:
        """账户快照。

        `_cash` 是**现金池（含冻结）**，可用部分才是 `_cash - _frozen`，所以净资产里
        冻结那部分**只能加一次**。这里曾经写成 `_cash + _frozen + market_value`，
        等于把挂单冻结的钱算两遍：每次下单的那根 K 线净资产凭空翻倍（实测 10 万本金
        + 94151 冻结 ⇒ 快照 194151），净值曲线上每隔几天一个尖峰，
        `max_drawdown` 报 49.9%（真实值 2.8%）、`sharpe` 报 1.20（真实值为负）。
        指标算得再对，输入是错的也全废 —— 所以这里显式写成 `可用 + 冻结 + 市值`。
        """
        with self._lock:
            market_value = sum(p.market_value for p in self._positions.values())
            available_capital = self._cash - self._frozen
            total = available_capital + self._frozen + market_value
            return Account(
                account_id="sim-account",
                total_capital=total,
                available_capital=available_capital,
                market_value=market_value,
                frozen_capital=self._frozen,
                total_pnl=total - self._initial,
            )

    def get_positions(self) -> Dict[str, Position]:
        with self._lock:
            return {s: p for s, p in self._positions.items()}

    def get_orders(self, strategy_id: str) -> List[Order]:
        with self._lock:
            return [o for o in self._orders.values() if o.strategy_id == strategy_id]

    def get_trades(self, strategy_id: str) -> List[Trade]:
        with self._lock:
            return [t for t in self._trades if self._trade_owner.get(t.trade_id) == strategy_id]

    def get_fills(self) -> List[Fill]:
        with self._lock:
            return list(self._fills)

    # ── 配置 ─────────────────────────────────────────────────────────
    def set_commission(self, commission: CommissionConfig) -> None:
        with self._lock:
            self._commission = commission

    def set_slippage(self, slippage: SlippageConfig) -> None:
        with self._lock:
            self._slippage = slippage

    def on_bar(self, bar: BarData) -> None:
        """新 K 线到达：先更新市值，再撮合上一轮挂下的单子。"""
        with self._lock:
            self._last_price[bar.symbol] = bar.close
            for symbol, position in self._positions.items():
                price = bar.close if symbol == bar.symbol else position.current_price
                position.current_price = price
                position.market_value = price * position.quantity
                base = position.cost_price * position.quantity
                position.unrealized_pnl = position.market_value - base
                position.pnl_percentage = (position.unrealized_pnl / base * 100.0) if base else 0.0
            for order_id in list(self._pending):
                order = self._orders[order_id]
                if order.symbol != bar.symbol:
                    continue
                self._match(order, bar)

    # ── 内部：撮合 ────────────────────────────────────────────────────
    def _match(self, order: Order, bar: BarData) -> None:
        price = bar.open
        if order.price is not None and order.direction is Direction.BUY:
            if bar.open > order.price:
                return  # 限价买：开盘价就贵过限价，本根不成交（挂单继续等）
        if order.price is not None and order.direction is Direction.SELL:
            if bar.open < order.price:
                return
        if order.direction is Direction.SELL and not self._can_sell(order):
            # 卖超：持仓不够，这一单**不可能**成交。拒绝必须在改动任何状态**之前**做。
            # 原先是在 `_apply_position` 里抛异常，而那里已经被改过一半 ——
            # 现金已入账、`Trade` 已进成交表、订单已标 FILLED，然后异常一路冒到引擎，
            # 变成「一笔不存在的成交留在报告里」+「整段回测作废」。
            self._reject(order, bar, "卖出超过持仓")
            return
        gross = price * order.quantity
        fee = commission_for(self._commission, order.direction, gross)
        slip = slippage_for(self._slippage, self._rng, price, order.quantity)
        if order.direction is Direction.BUY:
            self._cash -= gross + fee + slip
            self._frozen = max(0.0, self._frozen - gross - commission_for(self._commission, Direction.BUY, gross))
        else:
            self._cash += gross - fee - slip

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.filled_price = price
        order.commission = fee
        order.slippage = slip
        order.last_update_time = bar.datetime

        self._trade_seq += 1
        trade = Trade(
            trade_id="trd-%06d" % self._trade_seq,
            order_id=order.order_id,
            symbol=order.symbol,
            direction=order.direction,
            price=price,
            quantity=order.quantity,
            commission=fee,
            slippage=slip,
            timestamp=bar.datetime,
            trade_fee=fee + slip,
        )
        self._trade_owner[trade.trade_id] = order.strategy_id
        self._trades.append(trade)
        self._fills.append(
            Fill(
                fill_id="fil-%06d" % self._trade_seq,
                order_id=order.order_id,
                symbol=order.symbol,
                direction=order.direction,
                price=price,
                quantity=order.quantity,
                commission=fee,
                slippage=slip,
                timestamp=bar.datetime,
            )
        )
        self._apply_position(order, price, fee, slip)
        self._pending.remove(order.order_id)
        self._emit(EventType.TRADE_EVENT, trade, bar.datetime, "broker")

    def _can_sell(self, order: Order) -> bool:
        """卖单能不能成交：持仓必须够。"""
        position = self._positions.get(order.symbol)
        return position is not None and position.quantity >= order.quantity

    def _held_quantity(self, symbol: str) -> int:
        position = self._positions.get(symbol)
        return 0 if position is None else int(position.quantity)

    def _reject(self, order: Order, bar: BarData, reason: str) -> None:
        """把一张挂单标成拒单并摘出挂单队列。**不抛异常** —— 拒单是正常业务事件。

        订单本身留在 `_orders` 里（不会被摘掉），所以它照样出现在报告的 `orders` 中，
        状态是 `REJECTED`、`error_message` 带上原因。没有这一步的话，「策略发了信号
        但什么都没发生」在结果里查无实据。
        """
        order.status = OrderStatus.REJECTED
        order.error_message = "%s（%s x%d，持仓 %d）" % (
            reason,
            order.symbol,
            order.quantity,
            self._held_quantity(order.symbol),
        )
        order.last_update_time = bar.datetime
        if order.order_id in self._pending:
            self._pending.remove(order.order_id)
        self._emit(EventType.ORDER_EVENT, order, bar.datetime, "broker")

    def _apply_position(self, order: Order, price: float, fee: float, slip: float) -> None:
        position = self._positions.get(order.symbol)
        if order.direction is Direction.BUY:
            if position is None:
                position = Position(
                    symbol=order.symbol,
                    direction=PositionDirection.LONG,
                    quantity=0,
                    market_value=0.0,
                    cost_price=0.0,
                    current_price=price,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    pnl_percentage=0.0,
                )
                self._positions[order.symbol] = position
            base = position.cost_price * position.quantity
            quantity = position.quantity + order.quantity
            # 摊销成本价含**全部**买入摩擦（手续费 + 滑点）：这样卖出时的
            # `(price - cost_price) * quantity` 才是这笔的真实盈亏。
            position.cost_price = (base + price * order.quantity + fee + slip) / quantity
            position.quantity = quantity
            position.current_price = price
            position.market_value = price * quantity
            position.unrealized_pnl = position.market_value - position.cost_price * quantity
            position.pnl_percentage = (position.unrealized_pnl / (position.cost_price * quantity) * 100.0)
            return

        if position is None or position.quantity < order.quantity:
            # 第二道保险：`_match` 开头已经用 `_can_sell()` 拦过了，这里**不该**被触发。
            # 留着是为了将来有人把 `_match` 里的检查挪到后面时立刻炸掉，而不是静默写坏账。
            raise OrderRejectedError(
                "卖出 %s x%d 超过持仓 %d" % (order.symbol, order.quantity, self._held_quantity(order.symbol))
            )
        position.realized_pnl += (price - position.cost_price) * order.quantity - fee - slip
        position.quantity -= order.quantity
        if position.quantity == 0:
            del self._positions[order.symbol]
            return
        position.current_price = price
        position.market_value = price * position.quantity
        position.unrealized_pnl = position.market_value - position.cost_price * position.quantity
        position.pnl_percentage = (position.unrealized_pnl / (position.cost_price * position.quantity) * 100.0)

    def _emit(self, event_type: EventType, data: object, stamp: datetime, source: str) -> None:
        if self._events is None:
            return
        self._events.publish(Event(event_type=event_type, data=data, timestamp=stamp, source=source, priority=5))

    def reset(self) -> None:
        """回到全新状态。`_rng` **重新播种**（不是重置为当前状态），
        这样 `reset()` 之后再跑一遍，结果与第一次逐位相同。"""
        with self._lock:
            self._rng = random.Random(self._seed)
            self._orders.clear()
            self._pending.clear()
            self._trades.clear()
            self._trade_owner.clear()
            self._fills.clear()
            self._positions.clear()
            self._last_price.clear()
            self._cash = self._initial
            self._frozen = 0.0
            self._seq = 0
            self._trade_seq = 0
