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
             信号 → Market 单 → 风控闸门 → broker.submit_order  ← 等第 t+1 根成交
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

## 风控闸门（I3）默认是**关着的**，且这一点必须能被看见

`attach_risk_engine()` 之后，`_bar_handler` 里唯一那处 `broker.submit_order` 之前必经
`RiskEngine.check()`（PASS 原样 / REDUCE 缩量 / REJECT、HALT 拦截）。**默认不接**，
因为契约 §3.1.1 的默认阈值（单票 ≤ 10% 总资产）会把 I1 双均线策略按 90% 建仓的每一单
都 REDUCE —— 那是风控正常工作的结果，但会让 `.rounds/i1/` 那份逐段比对的复现证据当场
作废。谁要风控谁显式接；接没接可以从 `result.orders` 里被拦的单和 `risk_summary()` 看出来。

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
    DataVersionError,
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
from .risk import RiskActionEnum, RiskCheckRequest, RiskEngine, RiskSnapshot
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
        # ── 风控闸门（I3）────────────────────────────────────────────
        # 默认**不接**（理由见 `attach_risk_engine`）。`_daily_orders` / `_amount_totals`
        # 只在接了风控时参与判定，但它们**不接也要维护**：一个「没接就没有」的计数器
        # 会让「接了之后第一单的计数」变成 undefined，而不是 0。
        self._risk: Optional[RiskEngine] = None
        self._risk_stats: Dict[str, int] = {}
        self._risk_blocked: List[Dict[str, Any]] = []
        self._daily_orders: Dict[str, int] = {}
        self._amount_totals: Dict[str, Tuple[float, int]] = {}
        self._reset_risk_run_state()

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

    # ── 风控闸门（I3）────────────────────────────────────────────────
    def attach_risk_engine(self, risk_engine: RiskEngine) -> None:
        """把风控引擎接到订单路径上。接上之后每张单在提交前**必经** `RiskEngine.check()`。

        裁决在本引擎里的落法（对应契约 §3.2.1 的四种 action）：
        PASS 原样提交、REDUCE 按 `adjusted_quantity` 缩量后提交、REJECT / HALT 拦截。

        **为什么默认不接**（这是 I3 里最需要写明的取舍）：契约 §3.1.1 的默认 GLOBAL 阈值是
        「单票仓位 ≤ 10% 总资产」，而 I1 的双均线策略按 `capital × 0.9` 建仓 —— 一接上，
        每笔买入都会被 REDUCE 成总资产的 10%。那不是 bug，是风控按契约正常工作，但它会让
        `.rounds/i1/` 那份逐段比对的复现证据（`verify_backtest_reproducibility.py` 的 R5）
        当场作废。所以风控是**显式接线**：谁要它谁接。

        接线点只有一个：`_bar_handler` 里 `self._broker.submit_order(order)` 全文只出现一次，
        闸门就紧在它上面。**不要在别处提交订单**，那会绕过闸门且不留痕。

        `risk_engine` 必须已经 `load()` 过 —— 没加载的引擎会拒绝判定（D4 fail-safe），
        异常会从 `run()` 冒出来，而不是「悄悄放行」。
        """
        if not isinstance(risk_engine, RiskEngine):
            raise ConfigValidationError(
                "risk_engine 必须是 RiskEngine，收到 %r" % (type(risk_engine).__name__,)
            )
        self._risk = risk_engine
        self._reset_risk_run_state()

    def risk_summary(self) -> Dict[str, Any]:
        """风控闸门的统计。没接风控时 `attached=False` —— 「接没接」必须一眼可见。

        刻意**不进回测报告**：报告的 `deterministic` 段是 `.rounds/i1/` 那份被逐段比对的
        复现证据，往里加字段等于让已交付的证据过期。要问「这一轮过没过风控」，看两处：
        ① `result.orders` 里 `status=REJECTED` 且 `error_message` 带规则号的单；② 这里。

        `checked` = 经过 `RiskEngine.check()` 的订单数；`passed` = 缩量后仍被提交的订单数
        （含原样通过的）；`reduced` = 其中真被缩量的；`blocked` = 被拦下没进市场的。
        """
        if self._risk is None:
            return {
                "attached": False,
                "rule_version": None,
                "run_state": None,
                "checked": 0,
                "passed": 0,
                "reduced": 0,
                "blocked": 0,
                "blocked_orders": [],
            }
        summary: Dict[str, Any] = {
            "attached": True,
            "rule_version": self._risk.get_rule_version(),
            "run_state": self._risk.get_run_state().value,
            "blocked_orders": list(self._risk_blocked),
        }
        summary.update(self._risk_stats)
        return summary

    def _reset_risk_run_state(self) -> None:
        """清空**本轮**的风控统计。峰值与熔断状态故意不清 —— 它们归 `RiskEngine` 所有，
        跨 `run()` / `run_partial()` 保留正是 D7/D8 要的（换段重跑不该把熔断洗掉）。"""
        self._risk_stats = {"checked": 0, "passed": 0, "reduced": 0, "blocked": 0}
        self._risk_blocked = []
        self._daily_orders = {}
        self._amount_totals = {}

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
                row_count=0,
                warnings=["尚未执行回测，未来函数检查没有任何样本可查"],
                errors=[],
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
        # `row_count` 用**真的比过的笔数**（`checked`），不是 `len(trades)` ——
        # 找不到下单记录的那几笔根本没进比对，算进去会让 "校验了多少行" 虚高。
        # `missing_ratio` 留空字典：这个检查没有"列"的概念，编一个比值就是伪造。
        return ValidationReport(
            is_valid=not issues,
            row_count=checked,
            errors=issues,
            warnings=warnings,
        )

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
            # `validate_no_leakage()` 读的是 `self._result` ⇒ **必须先挂上去再问它**。
            # 反过来写（先问后挂）会让每一份报告里的 `validation_report` 都是那句
            # “尚未执行回测”的空壳：`is_valid=True`、`row_count=0` —— 看着永远通过。
            # 直接调 `validate_no_leakage()` 的单元用例查不出来（那时 `_result` 已经在了），
            # 只有端到端读 `result.validation_report` 才看得见（`tests/test_backtest_db_feed.py`）。
            self._result = result
            result.validation_report = self.validate_no_leakage()
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
        self._reset_risk_run_state()
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
                if not self._risk_gate(order, bar):
                    continue
                # 计数放在提交**之前**：这笔单已经离开风控、进了市场路径，无论撮合器
                # 收不收，它都算当日的下单行为（口径见 `_daily_order_count`）。
                self._note_daily_order(bar, order.symbol)
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

    # ── 风控闸门的实现（I3）──────────────────────────────────────────
    def _risk_gate(self, order: Order, bar: BarData) -> bool:
        """订单进撮合器前的闸门。返回 True = 放行（可能已缩量），False = 拦截。

        契约 §3.2.5 的 `passed` 只在 `action == PASS` 时为真，所以**不能用它当放行判据**
        —— REDUCE 也是「放行」，只是数量要缩。三种 action 在这里的落法：

        * `PASS`   —— 原样提交。
        * `REDUCE` —— 缩到 `adjusted_quantity` 再提交；缩成 0 股时按拦截处理（0 股订单
          交给 broker 只会被拒，还会把拒绝原因写成一条误导人的「资金不足」）。
        * `REJECT` / `HALT` —— 拦截，订单进 `self._rejected`。**不抛异常**：拒单是正常
          业务事件，与 broker 那边的资金不足/卖超同口径（一次性拒单不该作废整段回测）。
        """
        if self._risk is None:
            return True
        account = self._broker.get_account()
        response = self._risk.check(
            RiskCheckRequest(
                account_id=account.account_id,
                strategy_id=order.strategy_id,
                symbol=order.symbol,
                side=order.direction,
                is_open=self._is_open_order(order),
                quantity=int(order.quantity),
                price=float(bar.close),
                snapshot=self._risk_snapshot(account, order.strategy_id, order.symbol, bar),
            )
        )
        self._risk_stats["checked"] += 1
        if response.action is RiskActionEnum.REDUCE:
            allowed = int(response.adjusted_quantity)
            if allowed <= 0:
                self._record_risk_block(order, bar, response)
                return False
            if allowed < order.quantity:
                order.quantity = allowed
                self._risk_stats["reduced"] += 1
            self._risk_stats["passed"] += 1
            return True
        if response.action is RiskActionEnum.PASS:
            self._risk_stats["passed"] += 1
            return True
        self._record_risk_block(order, bar, response)
        return False

    def _record_risk_block(self, order: Order, bar: BarData, response: Any) -> None:
        """被风控拦下的单走**和 broker 拒单完全相同**的那条路径。

        这样「订单去哪了」永远只有一个答案：`result.orders` 里 `status=REJECTED` 的那些，
        `error_message` 说明是谁拒的、依据哪条规则。另存一份结构化记录供 `risk_summary()`
        与测试使用（`rule_ids` 排序去重 —— 报告要能逐字节比）。
        """
        order.status = OrderStatus.REJECTED
        order.error_message = response.message
        self._rejected.append(order)
        self._risk_stats["blocked"] += 1
        self._risk_blocked.append(
            {
                "datetime": bar.datetime.isoformat(),
                "strategy_id": order.strategy_id,
                "symbol": order.symbol,
                "side": order.direction.value,
                "quantity": int(order.quantity),
                "action": response.action.value,
                "run_state": response.run_state.value,
                "rule_ids": sorted({violation.rule_id for violation in response.violations}),
                "message": response.message,
            }
        )

    def _is_open_order(self, order: Order) -> bool:
        """这张单是开仓还是平仓（`RiskCheckRequest.is_open`）。

        判据只有一条：**卖出且手里有这个标的的持仓** ⇒ 平仓；其余（买入、卖出但无持仓）
        算开仓。把「卖出无持仓」也归到开仓是刻意偏严的：在 broker 那里它会被「卖超」拒掉，
        但在风控眼里它是「要建立负暴露」的开仓方向动作，而开仓受的约束更多 ——
        判断错时宁可更严，不可更松。
        """
        if order.direction is Direction.SELL:
            position = self._broker.get_positions().get(order.symbol)
            return position is None or position.quantity <= 0
        return True

    def _risk_snapshot(self, account: Any, strategy_id: str, symbol: str, bar: BarData) -> RiskSnapshot:
        """按契约 §3.2.3 组装快照。每个字段都是**实测值**，缺的那一个如实留空。

        * `symbol_avg_daily_amount` 用**已见 K 线**的成交额均值（含当根）：全样本均值要用到
          还没发生的成交额，那是未来函数；至今均值在第 1 根上就等于当根成交额。
        * `position_value_by_sector` **留空**：行业归类是契约 §四 明说的未定缺口，编一个
          （比如按代码前缀猜行业）会让 `max_sector_pct` 在错的数据上做决定。留空时该规则
          给 WARNING 不拦单（`risk.py` 里那条分支写了理由）。
        """
        positions = self._broker.get_positions()
        return RiskSnapshot(
            total_asset=float(account.total_capital),
            available_capital=float(account.available_capital),
            strategy_equity=self._strategy_equity(strategy_id, positions, bar),
            symbol_avg_daily_amount=self._symbol_amount_avg(bar),
            trading_date=bar.datetime.date().isoformat(),
            position_value_by_symbol={name: float(p.market_value) for name, p in positions.items()},
            daily_trade_count_by_symbol={symbol: self._daily_order_count(bar, symbol)},
        )

    def _strategy_equity(self, strategy_id: str, positions: Dict[str, Any], bar: BarData) -> float:
        """策略权益 = 该策略分到的本金 + 它的已实现现金流 + 它净持仓的浮动市值。

        为什么不直接用 `account.total_capital`：多策略回测里那是**整账户**权益，拿它当某条
        策略的权益，会让每条策略的回撤都跟着别人一起动（一条策略亏损触发全体熔断）。

        为什么净持仓要自己累：broker 的持仓是**全局**的（只按 `_trade_owner` 记成交归属），
        而契约里 `RiskSnapshot.strategy_equity` 是必填字段。与其编一个数，不如把口径写在这里。
        """
        capital = 0.0
        for _strategy, handle in self._strategies:
            if handle.strategy_id == strategy_id:
                capital = float(handle.initial_capital)
                break
        flow = 0.0
        net: Dict[str, int] = {}
        for trade in self._broker.get_trades(strategy_id):
            gross = float(trade.price) * int(trade.quantity)
            cost = float(trade.commission) + float(trade.slippage)
            # **两个符号口径必须分开**，共用一个会算错权益：现金流看「钱的进出」
            # （买入出钱 ⇒ 负），持仓看「多空方向」（买入是多头 ⇒ 正）。曾经这里只有一个
            # `sign`，于是多头持仓在 `net` 里记成负数 ⇒ 下面那个 `quantity > 0` 不成立 ⇒
            # 浮动市值永远加不上来。后果不是少算几个点：满仓时权益直接变成
            # `capital - 买入金额 - 成本`（实测 90000 - 90060.25 = **-60.25**），
            # `strategy_drawdown_pct` 因此观测到 1.0007 的假回撤，第一笔正常建仓后
            # 就把整个策略单元熔断掉（单策略回测里表现为「第三笔单莫名其妙被拒」）。
            cash_sign = -1 if trade.direction is Direction.BUY else 1
            flow += cash_sign * gross - cost
            net[trade.symbol] = net.get(trade.symbol, 0) + (-cash_sign) * int(trade.quantity)
        floating = 0.0
        for name, quantity in net.items():
            position = positions.get(name)
            # 只加多头（本系统不允许裸卖空：卖出超过持仓会被撮合器拒，
            # 所以 `net` 正常情况下不会为负；真为负也宁可不加，不加不会凭空夸大权益）。
            if position is not None and quantity > 0:
                floating += float(position.current_price) * quantity
        return capital + flow + floating

    def _symbol_amount_avg(self, bar: BarData) -> float:
        """标的「至今日均成交额」。

        `bar.amount` 缺列时 `CsvDataFeed` 已用 `close × volume` 折算过（datafeed.py）；
        这里再兜一次是留给别的 feed 的。算不出均值时**返回 0**，风控那边会因此拒绝这一单
        （`symbol_avg_daily_amount <= 0` ⇒ 拒绝）—— 这不是退化，是 D4 fail-safe：
        算不出冲击占比就不该放行。
        """
        amount = float(bar.amount or 0.0)
        if amount <= 0:
            amount = float(bar.close) * float(bar.volume or 0.0)
        total, count = self._amount_totals.get(bar.symbol, (0.0, 0))
        if amount > 0:
            total += amount
            count += 1
        self._amount_totals[bar.symbol] = (total, count)
        return total / count if count else 0.0

    def _daily_order_count(self, bar: BarData, symbol: str) -> int:
        """该标的当日**已提交给撮合器**的笔数（契约 §3.1.1：买卖合计）。

        口径说明（契约只写了「单日交易笔数」，没说数到哪一步，这里定死）：数**提交**而不是
        成交 —— 当日限笔要拦的是下单行为本身，数成交的话，一批注定不成交的单会一路放行；
        被风控拦下的单**不计**（它没进市场），被资金不足拒的单**计**（它进了市场，只是没成交）。
        """
        return int(self._daily_orders.get(self._day_symbol_key(bar, symbol), 0))

    def _note_daily_order(self, bar: BarData, symbol: str) -> None:
        key = self._day_symbol_key(bar, symbol)
        self._daily_orders[key] = int(self._daily_orders.get(key, 0)) + 1

    @staticmethod
    def _day_symbol_key(bar: BarData, symbol: str) -> str:
        return "%s|%s" % (bar.datetime.date().isoformat(), symbol)

    def _data_version(self) -> str:
        """数据版本：报告里用来回答「这轮回测读的是**哪一份**数据」。

        刻意**不**用文件修改时间 / 哈希 —— 报告要能跨机器逐字节比，而 mtime 做不到。

        分两支，而且**由 feed 自己说了算**：

        * feed 声明了 `data_version`（`DbDataFeed` 即如此，值来自 `DataCenter.as_of()`）
          ⇒ 用它，格式 `symbol@version`。空串 ⇒ 抛 `DataVersionError`：
          绝不能盖个假章上去　—— 一纸 `600000.SH::35` 出了报告，读的人没有任何
          办法知道这轮读的是哪份数据，而那个 `35` 只是**根数**。换一份 `data_version`
          重采，只要天数一样，报告就一模一样（D8 要的可复现性标记在这里是瞎的）。
        * 否则退回 I1 的老格式 `symbol:文件名:根数` —— `.rounds/i1` 的报告是逐字节
          比对的证据，不能无端改形状。

        `data_version` 用的是属性探测而不是 `isinstance(DbDataFeed)`：契约里
        `DataFeed` 并不声明这个成员，谁声明了谁就得为它负责；引擎不认识具体的 feed 类，
        依赖方向也不用反过来。
        """
        parts = []
        for symbol in sorted(self._datafeeds):
            feed = self._datafeeds[symbol]
            declared = getattr(feed, "data_version", None)
            if declared is not None:
                text = str(declared).strip()
                if not text:
                    raise DataVersionError(
                        "%s 的数据源声明了空的 data_version，报告无法标记这轮读的是哪份数据（D8）"
                        % (symbol,)
                    )
                parts.append("%s@%s" % (symbol, text))
                continue
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
