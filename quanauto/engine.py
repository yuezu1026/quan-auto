"""回测引擎 —— `BacktestEngine`（契约 §2.1.1）。

契约 §2.1.1 的接口是**项目符号列表**，不是 python 代码块（`ast` 解不出来）。所以签名
由 `tools/contract-signature-manifest.json` 的 `bullet_list_signatures` 承载，门禁按它比对，
并且会断言那几条项目符号在文档里**依然存在** —— 文档改了就红，不会被静默漏掉。

## 一次回测到底发生了什么

```
run()
 ├─ _prepare()            校验就绪、清空状态、订阅 BAR 事件
 └─ 对交易日历上的每个时刻 t、按标的字典序：
      发布 BAR_EVENT(bar_t) → EventEngine.run() 同步派发
        └─ _bar_handler:
             broker.on_bar(bar_t)      ← 第 t-1 根挂的单在这里按 bar_t.open 成交
             每个策略 on_data(bar_t)   ← 用 bar_t 收盘信息产生信号
             信号 → Market 单 → broker.submit_order  ← 等第 t+1 根成交
             记一次 AccountSnapshot
 └─ PerformanceAnalyzer.analyze() → 盖请求域字段（策略 id / 版本 / params / duration）
```

## 三条硬约束，都是「不这样就会骗自己」

1. **成交价 = 下一根开盘价**，不是当根收盘价。当根收盘价成交 = 隐藏的未来函数。
   `validate_no_leakage()` 会把这条**测出来**（`trade.timestamp` 必须**晚于**下单时刻）。
2. **遍历顺序全确定**：标的是 `sorted()`、交易日历去重升序、策略按注册顺序。
   dict 的插入序会跟着文件行序变，那是「换个 CSV 行序结果就变」的根源。
3. **随机只在 broker 里、且只用 `random.Random(seed)`**。引擎自己不抽随机数。
   默认 `percentage_slippage=0` ⇒ 一条随机数都不抽。

## 本迭代不做

`checkpoint()` / `restore_checkpoint()` 不做（没有它回测照样跑完，且断点续跑要先定义
「状态」的边界）。`position_limit`（单票/总仓位上限）、`enable_parallel` / `max_workers`、
`enable_checkpoint` / `checkpoint_interval`、`output_path` 这几个配置字段**读了但没执行** ——
一律登记在 `docs/迭代计划.md` 的「本迭代不做」里，不写 `pass` 假装实现。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .broker import SimulatedBroker
from .datafeed import DataFeed
from .enums import (
    BacktestStatus,
    Direction,
    DirectionEnum,
    EventType,
    MarketStatus,
    OrderStatus,
    OrderType,
)
from .errors import (
    BacktestExecutionError,
    ConfigValidationError,
    DataFeedRegistrationError,
    ExportError,
    NoResultError,
    OrderRejectedError,
    QuanAutoError,
    StrategyRegistrationError,
)
from .events import Event, EventEngine
from .models import (
    AccountSnapshot,
    BacktestConfig,
    BacktestResult,
    BarData,
    MarketDataBundle,
    Order,
    StrategyHandle,
    TradingSignal,
    ValidationReport,
)
from .performance import PerformanceAnalyzer
from .strategies import Strategy

REPORT_SCHEMA = "quanauto.backtest-report/1"
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")


# ── 模块级工具（避免给 BacktestEngine 加契约里没有的成员）─────────────────
def build_bundle(bar: BarData, feed: DataFeed, previous_close: Optional[float]) -> MarketDataBundle:
    """把 `BarData` 补成策略要的 `MarketDataBundle`。

    `previous_close` 在第一根 K 线上没有前值 ⇒ 退化成**当根开盘价**（不是 0，也不是当根
    收盘价 —— 用收盘价会让「涨跌幅」在第一根上恒等于 0，看起来像策略没反应）。
    """
    return MarketDataBundle(
        symbol=bar.symbol,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        amount=bar.amount,
        datetime=bar.datetime,
        timestamp=bar.timestamp,
        adjust_factor=feed.get_adjustment_factor(bar.symbol, bar.datetime),
        previous_close=previous_close if previous_close is not None else bar.open,
        is_trading_day=feed.get_market_status(bar.datetime) is MarketStatus.OPEN,
    )


def parse_date(text: str, label: str) -> datetime:
    """解析 `run_partial` 的日期字符串。解析不了抛 `BacktestExecutionError`。"""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    raise BacktestExecutionError("%s 不是合法日期（要 YYYY-MM-DD）：%r" % (label, text))


def to_jsonable(value: Any) -> Any:
    """把 dataclass 树转成可 JSON 序列化的结构。

    自己写而不是用 `dataclasses.asdict`：`asdict` 会把 `Enum` 原样带出来
    （`json.dumps` 直接 `TypeError`），而这里要的是 `.value`。规则固定、无随机。
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def report_payload(result: BacktestResult, seed: int) -> Dict[str, Any]:
    """报告结构刻意分成三段，**每段的比较口径不一样**：

    * `inputs` —— 这次跑用的输入（种子）。两次跑用了不同种子时它当然不同，
      所以**不能**把它放进 `deterministic`，否则「不同种子 ⇒ 报告不同」这条
      会因为种子字段本身而成立，测不出引擎到底有没有用到种子。
    * `deterministic` —— 同一输入必须逐字节相同的那部分。可复现性门禁只比这一段。
    * `runtime` —— 区分「本次执行」与「回测区间」的字段。整段被门禁排除。

      注意 `start_time` / `end_time` 是**回测区间的首尾 K 线时刻**（即
      `BacktestResult.start_time` / `.end_time`，与契约一致），**不是**挂钟时刻。
      这三项里真正天生不可复现的只有 `duration_ms`；排除整段是保守做法，
      免得将来往这里加字段时把时戳漏进去。区间信息另有一份在 `summary.window`。
    """
    return {
        "schema": REPORT_SCHEMA,
        "inputs": {"seed": seed},
        "deterministic": {
            "strategy_id": result.strategy_id,
            "status": result.status.value,
            "data_version": result.data_version,
            "strategy_version": result.strategy_version,
            "params_used": to_jsonable(result.params_used),
            "performance": to_jsonable(result.performance),
            "equity_curve": to_jsonable(result.equity_curve),
            "trades": to_jsonable(result.trades),
            "orders": to_jsonable(result.orders),
            "account_history": to_jsonable(result.account_history),
            "validation_report": to_jsonable(result.validation_report),
        },
        "runtime": {
            "duration_ms": result.duration_ms,
            "start_time": result.start_time.isoformat(),
            "end_time": result.end_time.isoformat(),
        },
    }


def dump_report(payload: Dict[str, Any]) -> str:
    """`sort_keys=True` + `indent=2` + 换行结尾 ⇒ 同输入逐字节相同。"""
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


class BacktestEngine:
    """回测引擎。一次 `run()` 对应一次完整回测。"""

    def __init__(self, config: BacktestConfig) -> None:
        if not isinstance(config, BacktestConfig):
            raise ConfigValidationError("config 必须是 BacktestConfig，收到 %r" % (type(config).__name__,))
        if config.initial_capital <= 0:
            raise ConfigValidationError("initial_capital 必须为正数，收到 %r" % (config.initial_capital,))
        if config.start_date > config.end_date:
            raise ConfigValidationError("start_date 晚于 end_date（%s > %s）" % (config.start_date, config.end_date))
        # 刻意**不**在这里检查 `datafeeds` 是否为空：数据源允许构造后再 `add_datafeed` 补，
        # 「没有数据源」这件事在 `run()` 里检查（用得着的地方），那里抛的是执行期异常。
        self._config = config
        self._events = EventEngine()
        self._analyzer = PerformanceAnalyzer()
        self._datafeeds: Dict[str, DataFeed] = dict(config.datafeeds)
        self._strategies: List[Tuple[Strategy, StrategyHandle]] = []
        self._broker = SimulatedBroker(
            initial_capital=config.initial_capital,
            seed=0,
            commission=config.commission_config,
            slippage=config.slippage_config,
            event_engine=self._events,
        )
        self._result: Optional[BacktestResult] = None
        self._stopped = False
        self._running = False
        self._last_close: Dict[str, float] = {}
        self._submitted_bar: Dict[str, datetime] = {}
        self._snapshots: List[AccountSnapshot] = []
        # 被拒的订单进不了 broker 的订单表，但**必须**留在报告里：否则「策略发了信号
        # 但什么都没发生」在结果里查无实据，只能靠猜。
        self._rejected: List[Order] = []
        self._window: Optional[Tuple[datetime, datetime]] = None
        self._seed = 0

    # ── 注册 ─────────────────────────────────────────────────────────
    def add_datafeed(self, symbol: str, datafeed: DataFeed) -> bool:
        """注册数据源。**重复注册抛异常，不返回 False** —— 返回 False 会被调用方忽略，
        然后回测静默用着旧数据源，是最难查的一类错误。返回 False 只留给「无 symbol 名称」
        这种调用方一眼能看出的情况。
        """
        if not isinstance(symbol, str) or not symbol:
            return False
        if not isinstance(datafeed, DataFeed):
            raise DataFeedRegistrationError("datafeed 必须是 DataFeed 实现，收到 %r" % (type(datafeed).__name__,))
        if symbol in self._datafeeds:
            raise DataFeedRegistrationError("标的 %s 已经注册过数据源" % symbol)
        if symbol not in datafeed.get_available_symbols():
            raise DataFeedRegistrationError("数据源里没有标的 %s" % symbol)
        self._datafeeds[symbol] = datafeed
        return True

    def add_strategy(self, strategy: Strategy, capital: float, params: Dict[str, Any]) -> StrategyHandle:
        """注册策略并分配初始资金。同 `add_datafeed`：冲突一律抛异常。"""
        if not isinstance(strategy, Strategy):
            raise StrategyRegistrationError("strategy 必须是 Strategy 子类实例，收到 %r" % (type(strategy).__name__,))
        if capital < 0:
            raise StrategyRegistrationError("capital 不能为负，收到 %r" % (capital,))
        if not isinstance(params, dict):
            raise StrategyRegistrationError("params 必须是 dict，收到 %r" % (type(params).__name__,))
        existing = {handle.strategy_id for _, handle in self._strategies}
        if strategy.strategy_id in existing:
            raise StrategyRegistrationError("策略 id %s 已经注册过" % strategy.strategy_id)
        strategy.update_params(params)
        handle = StrategyHandle(strategy_id=strategy.strategy_id, initial_capital=float(capital))
        self._strategies.append((strategy, handle))
        return handle

    def set_broker(self, broker: SimulatedBroker) -> None:
        """替换撮合器。**契约里没有这个方法**（登记在 manifest 的 `members_extra`）。

        存在的唯一理由：`BacktestConfig` 的 18 个字段里没有随机种子，而「同一条命令跑两次
        指标一致」需要一个可注入、可复现的随机源。CLI 靠这里把 `--seed` 送进去。
        """
        if not isinstance(broker, SimulatedBroker):
            raise ConfigValidationError("broker 必须是 SimulatedBroker")
        self._broker = broker
        self._seed = broker.seed

    def stop(self) -> None:
        """请求停止。**协作式**：正在跑的循环在当前 K 线处理完后退出，不中断撮合
        （硬中断会留下「订单已提交未成交」的半截状态）。"""
        self._stopped = True

    # ── 运行 ─────────────────────────────────────────────────────────
    def run(self) -> BacktestResult:
        return self._execute(self._config.start_date, self._config.end_date)

    def run_partial(self, start_date: str, end_date: str) -> BacktestResult:
        """在给定日期区间内重跑一次。区间必须落在 `BacktestConfig` 的
        `[start_date, end_date]` 之内 —— 超出就抛，不悄悄截断。"""
        start = parse_date(start_date, "start_date")
        end = parse_date(end_date, "end_date")
        if start > end:
            raise BacktestExecutionError("start_date 晚于 end_date（%s > %s）" % (start, end))
        if start < self._config.start_date or end > self._config.end_date:
            raise BacktestExecutionError(
                "区间 [%s, %s] 超出配置的 [%s, %s]"
                % (start.date(), end.date(), self._config.start_date.date(), self._config.end_date.date())
            )
        return self._execute(start, end)

    def get_result(self) -> BacktestResult:
        if self._result is None:
            raise NoResultError("还没有跑过回测，没有结果可取")
        return self._result

    def export_report(self, path: str) -> bool:
        """把最近一次结果写成 JSON 报告。没有结果时抛 `NoResultError`
        （不是 `ExportError` —— 「没东西可导」和「导出失败」是两回事）。"""
        if self._result is None:
            raise NoResultError("还没有跑过回测，没有报告可导")
        payload = report_payload(self._result, self._seed)
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(dump_report(payload))
        except OSError as exc:
            raise ExportError("报告写入失败: %s (%s)" % (path, exc)) from exc
        return True

    def validate_no_leakage(self) -> ValidationReport:
        """未来函数检查：**每一笔成交的时间戳必须晚于它那张单的下单时刻**。

        这不是「框架自夸」式的检查 —— 它是可失败的：把 `SimulatedBroker._match` 的成交价
        从 `bar.open` 改成当根 `close`、或者让引擎在同一根 K 线上先下单后撮合，
        这条就会红。没跑过回测时返回一个 `is_valid=True` 但带 warning 的空报告
        （没有数据 ≠ 通过）。
        """
        if self._result is None:
            return ValidationReport(
                is_valid=True,
                warnings=["尚未执行回测，未来函数检查没有任何样本可查"],
                issues=[],
            )
        issues: List[str] = []
        checked = 0
        for trade in self._result.trades:
            submitted = self._submitted_bar.get(trade.order_id)
            if submitted is None:
                issues.append("成交 %s 找不到对应的下单记录" % trade.trade_id)
                continue
            checked += 1
            if trade.timestamp <= submitted:
                issues.append(
                    "未来函数：成交 %s 在 %s 成交，但订单 %s 是 %s 才下的"
                    % (trade.trade_id, trade.timestamp, trade.order_id, submitted)
                )
        warnings: List[str] = []
        if checked == 0:
            warnings.append("本次回测没有成交，未来函数检查没有实际样本")
        return ValidationReport(is_valid=not issues, warnings=warnings, issues=issues)

    # ── 内部 ─────────────────────────────────────────────────────────
    def _execute(self, start: datetime, end: datetime) -> BacktestResult:
        started = time.perf_counter()
        self._running = True
        self._stopped = False
        try:
            calendar = self._prepare(start, end)
            symbols = sorted(self._datafeeds)
            for stamp in calendar:
                if self._stopped:
                    break
                for symbol in symbols:
                    bar = self._datafeeds[symbol].get_bar(symbol, stamp)
                    if bar is None:
                        continue
                    if not self._events.publish(
                        Event(event_type=EventType.BAR_EVENT, data=bar, timestamp=stamp, source="engine", priority=5)
                    ):
                        raise BacktestExecutionError("事件引擎未启动，BAR 事件发布失败")
                    self._events.run()
            trades = [t for _, handle in self._strategies for t in self._broker.get_trades(handle.strategy_id)]
            orders = [o for _, handle in self._strategies for o in self._broker.get_orders(handle.strategy_id)]
            orders.extend(self._rejected)
            result = self._analyzer.analyze(trades, orders, self._snapshots)
            result.strategy_id = ",".join(handle.strategy_id for _, handle in self._strategies)
            result.status = BacktestStatus.SUCCESS
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            result.data_version = self._data_version()
            result.strategy_version = self._strategy_version()
            result.params_used = self._params_used()
            result.validation_report = self.validate_no_leakage()
            self._result = result
            return result
        except QuanAutoError:
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底转成契约声明的异常类型
            raise BacktestExecutionError("回测执行失败: %s (%s)" % (exc, type(exc).__name__)) from exc
        finally:
            self._running = False

    def _prepare(self, start: datetime, end: datetime) -> List[datetime]:
        if not self._datafeeds:
            raise BacktestExecutionError("没有注册任何数据源")
        if not self._strategies:
            raise BacktestExecutionError("没有注册任何策略")

        self._broker.reset()
        self._last_close = {}
        self._submitted_bar = {}
        self._snapshots = []
        self._rejected = []
        self._result = None
        self._events.clear()
        self._events.start()
        self._events.subscribe(EventType.BAR_EVENT, self._bar_handler)

        for strategy, _handle in self._strategies:
            strategy.on_start()
        calendar = sorted(
            {stamp for feed in self._datafeeds.values() for stamp in feed.get_trading_calendar(start, end)}
        )
        if not calendar:
            raise BacktestExecutionError(
                "区间 [%s, %s] 内没有任何交易日" % (start.date(), end.date())
            )
        return calendar

    def _bar_handler(self, event: Event) -> None:
        """BAR 事件处理器。名字带下划线：它**不是**契约的一部分，登记在 manifest 的
        `members_extra` 里。"""
        bar: BarData = event.data
        self._broker.on_bar(bar)
        previous = self._last_close.get(bar.symbol)
        bundle = build_bundle(bar, self._datafeeds[bar.symbol], previous)
        self._last_close[bar.symbol] = bar.close
        for strategy, _handle in self._strategies:
            for signal in strategy.on_data(bundle):
                order = self._order_from_signal(signal, bar)
                if order is None:
                    continue
                try:
                    order_id = self._broker.submit_order(order)
                except OrderRejectedError as exc:
                    # 拒单是**正常业务事件**（资金不足 / 超仓），不是执行失败。
                    # 回测不能因此中断 —— 中断的话一次拒单就把整段回测作废，
                    # 而真实交易里你只是「这一单没成交」。
                    order.status = OrderStatus.REJECTED
                    order.error_message = str(exc)
                    self._rejected.append(order)
                    continue
                self._submitted_bar[str(order_id)] = bar.datetime
                strategy.on_order(order)
        account = self._broker.get_account()
        self._snapshots.append(
            AccountSnapshot(
                account_id=account.account_id,
                timestamp=bar.datetime,
                total_capital=account.total_capital,
                available_capital=account.available_capital,
                market_value=account.market_value,
                frozen_capital=account.frozen_capital,
                total_pnl=account.total_pnl,
            )
        )

    def _order_from_signal(self, signal: TradingSignal, bar: BarData) -> Optional[Order]:
        """信号 → 市价单。数量 ≤ 0 的信号直接丢掉（返回 None）：那不是「下单 0 股」，
        而是「策略在说不需要这个仓位」，送进 broker 只会被拒。"""
        if signal.target_quantity <= 0:
            return None
        return Order(
            order_id="",
            strategy_id=signal.strategy_id,
            symbol=signal.symbol or bar.symbol,
            direction=Direction.BUY if signal.direction is DirectionEnum.BUY else Direction.SELL,
            order_type=OrderType.MARKET,
            quantity=int(signal.target_quantity),
            price=None,
            stop_price=None,
            status=OrderStatus.PENDING,
            submit_time=bar.datetime,
            last_update_time=bar.datetime,
            filled_quantity=0,
            filled_price=0.0,
            commission=0.0,
            slippage=0.0,
            error_message=None,
        )

    def _data_version(self) -> str:
        """数据版本：把每个数据源的路径 + 标的 + K 线根数拼成一个短标识。

        刻意**不**用文件修改时间 / 哈希 —— 报告要能跨机器逐字节比，而 mtime 做不到。
        """
        parts = []
        for symbol in sorted(self._datafeeds):
            feed = self._datafeeds[symbol]
            path = os.path.basename(getattr(feed, "csv_path", ""))
            parts.append("%s:%s:%d" % (symbol, path, len(feed.get_available_dates(symbol))))
        return "|".join(parts)

    def _strategy_version(self) -> str:
        versions = []
        for strategy, _handle in self._strategies:
            info = strategy.get_strategy_info()
            versions.append("%s@%s" % (info.strategy_id, info.version))
        return "|".join(versions)

    def _params_used(self) -> Dict[str, Any]:
        return {strategy.strategy_id: strategy.get_strategy_params() for strategy, _handle in self._strategies}
