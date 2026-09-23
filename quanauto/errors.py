"""异常定义 —— 契约「附录 D 错误码完整列表」的那张表就是这里的 `code` 属性。

设计要点（写给下一个改这里的人）：

* 层次不是随便摆的。`StrategyError` 是**策略运行时**异常的根，因为契约把它当成类型
  用在签名里：`on_error(self, error: StrategyError) -> None`。所以凡是会被交给
  `on_error` 的异常都必须是 `StrategyError` 的子类，别图省事挂在别处。
* `code` 直接沿用契约附录 D 的错误码（`STRATEGY_001` 那批），没有自创。自创码会让
  「日志里的 code」和文档里的表对不上，而这种错没人会去查。
* 本模块**只定义 I1 竖切真的会抛的**异常。契约里模块一/模块二提到但 I1 不做的那些
  （检查点、适配度、市场状态），刻意不在这里预支 —— 预支的异常没人抛，就等于没有测试
  覆盖的死代码，还会让「已实现」看起来比实际多。
"""

from __future__ import annotations

from typing import Optional


class QuanAutoError(Exception):
    """本项目所有异常的根。

    契约没有给它起名字 —— 起草时是按模块分别命名异常的。这里补一个公用根，是为了让
    调用方能写 `except QuanAutoError` 兜底，而不是 `except Exception`（后者会把
    `KeyboardInterrupt` 之外的编程错误一起吞掉）。
    """

    code = "QUAN_000"

    def __init__(self, message: str = "", *, code: Optional[str] = None) -> None:
        super().__init__(message or self.__class__.__doc__ or self.__class__.__name__)
        self.message = str(self.args[0])
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - 单纯的美化
        return "[%s] %s" % (self.code, self.message)


# ── 配置类 ────────────────────────────────────────────────────────────────
class ConfigError(QuanAutoError):
    """配置相关异常的共同根。"""

    code = "STRATEGY_004"


class ConfigValidationError(ConfigError):
    """`BacktestConfig` 自身不自洽（缺数据源、日期倒挂、资金为负 …）。"""

    code = "BACKTEST_001"


class InvalidConfigError(ConfigError):
    """策略初始化时拿到的配置不合法（缺参数、类型不对、参数越界）。"""

    code = "STRATEGY_004"


class InvalidParamError(ConfigError):
    """`update_params()` 收到的参数不合法。"""

    code = "STRATEGY_004"


# ── 策略类 ────────────────────────────────────────────────────────────────
class BaseStrategyError(QuanAutoError):
    """策略异常的公共基类（契约在文字里提到过这个名字，但没给出规范块）。"""

    code = "STRATEGY_006"


class StrategyError(BaseStrategyError):
    """策略运行时异常根 —— 就是 `on_error(error: StrategyError)` 的那个类型。"""

    code = "STRATEGY_006"


class StrategyExecutionError(StrategyError):
    """策略在 `on_data()` 里执行出错。"""

    code = "STRATEGY_006"


class StrategyRegistrationError(StrategyError):
    """策略注册失败。"""

    code = "STRATEGY_009"


class DuplicateStrategyError(StrategyRegistrationError):
    """策略 ID 重复。"""

    code = "STRATEGY_001"


class StrategyNotFoundError(StrategyError):
    """按 ID 找不到策略。"""

    code = "STRATEGY_002"


class IllegalStateError(StrategyError):
    """当前状态不允许该操作（没启动就停止、重复启动 …）。"""

    code = "STRATEGY_003"


# ── 数据类 ────────────────────────────────────────────────────────────────
class DataValidationError(QuanAutoError):
    """输入数据没有通过校验（缺列、NaN、时间戳乱序 …）。"""

    code = "STRATEGY_005"


class DataFeedError(QuanAutoError):
    """数据源读取失败 —— `DataFeed` 的 9 个方法在契约里都声明会抛它。"""

    code = "BACKTEST_002"


class DataFeedRegistrationError(DataFeedError):
    """把数据源挂到回测引擎上时失败。"""

    code = "BACKTEST_002"


# ── 回测类 ────────────────────────────────────────────────────────────────
class BacktestError(QuanAutoError):
    """回测相关异常的共同根。"""

    code = "BACKTEST_001"


class BacktestExecutionError(BacktestError):
    """回测执行过程中失败。"""

    code = "BACKTEST_001"


class NoResultError(BacktestError):
    """结果还没生成就要取结果。"""

    code = "BACKTEST_001"


class ExportError(BacktestError):
    """导出报告失败。"""

    code = "BACKTEST_001"


# ── 撮合类 ────────────────────────────────────────────────────────────────
class BrokerError(QuanAutoError):
    """模拟撮合相关异常的共同根。"""

    code = "BROKER_001"


class OrderRejectedError(BrokerError):
    """订单被拒（参数非法、标的不在可交易列表 …）。"""

    code = "BROKER_002"


class InsufficientFundsError(BrokerError):
    """资金不足 —— 契约附录 C 要求「资金校验测试」，就靠它才断言得出来。"""

    code = "BROKER_001"
