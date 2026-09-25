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


# ── 数据中心类（I2 加入，数据中心契约 §3.9）─────────────────────────────────
# 错误码逐条照抄 §3.9 的表（DATA_001 ~ DATA_007）。严重级别也照抄：
# `FutureDataAccessError` 是唯一的 CRITICAL —— 它不是"数据读不到"，是"读到了不该
# 看到的东西"，回测必须立刻中断而不是降级。
#
# 🔴 一处**刻意的偏离**，记在 manifest 的 divergences 里而不是暗改：
# 数据中心契约 §3.9 写的是 `class DataFeedError(DataCenterError)`，但 `DataFeedError`
# 这个名字在本模块第 115 行已经被主契约 §2.2.1 的实现占用了（基类 `QuanAutoError`）。
# 同一个模块里不能出现两个同名类，所以这里**不重新声明** `DataFeedError`，
# 而是让两个 PIT 异常继承**既有的** `DataFeedError`：契约要的行为（`except
# DataFeedError` 能捕获到 `FutureDataAccessError`）照样成立，且主契约那条既有断言
# （`DataFeedError` 只有 9 个方法会抛）不用改。
class DataCenterError(QuanAutoError):
    """数据中心异常基类（对应数据中心契约 §3.9 的根，不含取数类）。"""

    # §3.9 的表从 DATA_001 起，没有 DATA_000。这里是照 `QUAN_000` 的先例补的
    # **根哨兵码**，不是契约里的一条。故意跟取数类分开：`DataCenterError` 表示
    # "数据中心的账对不上"（版本/质量/适配器/日历/幂等），而 `DataFeedError`
    # 一族表示"这次取数取不到"，两者调用方的处置动作不同（前者去修采集，
    # 后者去改 as_of 或换源）。
    code = "DATA_000"


class DataNotAvailableError(DataFeedError):
    """该 `as_of_date` 视角下数据不可见或不存在（DATA_001）。

    **不得**退化为「用最新数据」—— 那会把未来函数藏进"正常返回"里。
    """

    code = "DATA_001"


class FutureDataAccessError(DataFeedError):
    """访问了 `as_of_date` 之后才可见的数据，即未来函数（DATA_002，CRITICAL）。

    出现这个异常意味着**回测结果已经不可信**，调用方必须中断而不是重试。
    """

    code = "DATA_002"


class DataQualityError(DataCenterError):
    """数据质量校验未通过（DATA_003），见 `dc_quality_issue`。"""

    code = "DATA_003"


class DataVersionError(DataCenterError):
    """数据版本缺失 / 未激活 / 与回测结果不匹配（DATA_004）。

    回测结果必须绑定 `data_version`；绑不上就不许标记为可复现。
    """

    code = "DATA_004"


class SourceAdapterError(DataCenterError):
    """数据源拉取或解析失败（DATA_005）：网络、限流、源返回格式无法解析。

    适配器**只**抛这一个异常类去表示"源这一侧的问题"；源字段名归一化失败也用
    它，因为那同样意味着"这个源的这份数据不能用"，而不是"上层代码写错了"。

    除消息外还带三个**可判断**的属性（类别表见下方 `SOURCE_FAILURE_KINDS`）：
    `source`（哪个源）/ `kind`（哪一类）/ `retryable`（值不值得重试）。
    只给消息的版本写不出重试策略 —— 那正是 I2a 之前的状态。
    """

    code = "DATA_005"

    def __init__(self, message: str = "", *, source: str = "", kind: str = "UNKNOWN",
                 retryable: Optional[bool] = None, code: Optional[str] = None) -> None:
        if kind not in SOURCE_FAILURE_KINDS:
            # 刻意**不降级成 UNKNOWN**。降级会让「新加的类别根本没生效」与
            # 「归类成功」长得一模一样 —— 那正是分类表最容易失效的方式。
            raise ValueError(
                "未知的取数失败类别 %r：类别表是闭集，写错必须当场红（可用值：%s）"
                % (kind, ' / '.join(SOURCE_FAILURE_KINDS)))
        super().__init__(message, code=code)
        self.source = source
        self.kind = kind
        self.retryable = (kind in RETRYABLE_SOURCE_FAILURES) if retryable is None \
            else bool(retryable)


# ── 取数失败的**分类**（I2a，2026-09-24）─────────────────────────────────────
# 契约 §3.9 的 DATA_005 只规定「源这一侧出问题就用它」，**没规定怎么区分**。
# 一句字符串不足以让调用方决定动作：`ModuleNotFoundError`（去装库）与
# `TimeoutError`（过五分钟重试）在旧实现里长得一模一样，处置却相反。
#
# 分类的判据是**调用方要做的动作**，不是异常类型本身 —— 这是选类别的唯一标准。
# 动作相同的两个失败就是同一类，哪怕报错文字完全不同。
#
# 刻意**没有**「源没给数据」这一类：在本层区分不了「真的没有交易日」与
# 「代码写错了 / 源换了字段」，把它做成类别会鼓励上层把「空结果」当异常处理。
# 那是读侧 `as_of` 语义的事，已有 `DataNotAvailableError`（DATA_001）表达。
SOURCE_FAILURE_KINDS = (
    'SDK_MISSING',             # 源库没装（ImportError）                → 动作：装库
    'SOURCE_AUTH',             # 鉴权被拒（401/403）                    → 动作：换凭证
    'SOURCE_UNREACHABLE',      # 连不上（DNS／连接被拒／断流／5xx）     → 动作：重试
    'SOURCE_TIMEOUT',          # 连上了但超时                           → 动作：重试
    'SOURCE_RATE_LIMITED',     # 被限流（429）                          → 动作：退避后重试
    'SOURCE_SCHEMA_MISMATCH',  # 通了但解析不了（键缺／类型变／非 JSON）→ 动作：改映射表
    'UNSUPPORTED',             # 这一源不覆盖这个数据面                 → 动作：换源
    'UNKNOWN',                 # 兜底：没归类的一律来这里，**不猜**      → 动作：看原始类型名
)

# 「值得原样重试」的类别。这张表是分类存在的**全部理由** ——
# 其余类别的重试是纯浪费：装库不会因为重试而成功，改映射表更不会。
RETRYABLE_SOURCE_FAILURES = (
    'SOURCE_UNREACHABLE', 'SOURCE_TIMEOUT', 'SOURCE_RATE_LIMITED',
)


class CalendarError(DataCenterError):
    """交易日历覆盖范围不足（DATA_006）。**禁止**外推。"""

    code = "DATA_006"


class IngestConflictError(DataCenterError):
    """采集幂等冲突：同版本同主键出现不同值（DATA_007）。

    说明并发跑了两份采集，按 D10 重跑；**不要**用 upsert 把冲突悄悄盖掉。
    """

    code = "DATA_007"


class DataStoreError(DataCenterError):
    """落库层的操作失败（连接断了、事务被回滚、CHECK 约束被违反）。

    数据中心契约 §3.9 的七档错误码里**没有**「存储层失败」这一档 —— 那是契约的疏漏，
    不是可以省掉的东西：一旦不定义它，驱动自己的异常（`psycopg.OperationalError` …）
    就会裸逃到调用方，而调用方是策略/回测代码，不该知道 psycopg 的存在（D9：上层不感知
    数据源差异，存储同理）。所以本类按「实现侧补齐」建档，码值顺延 `DATA_008`。

    与 `DataQualityError`（DATA_003）的分工：CHECK 约束被违反说明**上游 `validate()`
    放行了不该放行的行**，那是 bug 不是数据质量问题，所以落库层不把它映射成 DATA_003。
    """

    code = "DATA_008"


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


# ── 风控类 ────────────────────────────────────────────────────────────────
# 码取自《风控层接口契约文档》§3.7。**不在这里预支没人会抛的异常**：`RISK_003`
# （单策略熔断）与 `RISK_008`（熔断未恢复）在实现里与 `RISK_002` 走同一条路径，
# 报文里区分单元标识即可；`RISK_009`（放宽未确认）是**正常业务结果**（落 PENDING），
# 不是异常；`RISK_011`（权益峰值缺失）以 WARNING 级违规上报，也不该中断交易。
class RiskError(QuanAutoError):
    """风控层异常的共同根。"""

    code = "RISK_000"


class RiskInterceptError(RiskError):
    """风控拦截（RISK_001）—— 裁决为 REJECT/HALT。

    第二个用途（I3b 小步加入）：**留痕写入方**收到不该留痕的响应时抛它。
    「拦了却没有一行留痕」与「没拦却写了一行留痕」都会让日志失去证据价值，
    两种错都要当场炸，而不是安静地少写/多写一行。
    """

    code = "RISK_001"


class RiskBreakerTrippedError(RiskError):
    """熔断已触发导致开仓被拒（RISK_002 全账户 / RISK_003 单策略，看 `breaker_key`）。"""

    code = "RISK_002"


class RiskConfigInvalidError(RiskError):
    """规则配置非法（RISK_004）—— 类型错 / 超范围 / GLOBAL 层缺规则（D4）。

    语义要点：抛它的时候**旧值必须仍在生效**，这是「宁可旧值，不可无值，更不可半新半旧」。
    """

    code = "RISK_004"


class RiskRuleNotFoundError(RiskConfigInvalidError):
    """规则解析不到 —— GLOBAL 层都缺，按 D4 视为配置非法（沿用 RISK_004）。"""


class RiskConfigLoadError(RiskError):
    """规则加载失败（RISK_005）—— 存储层不可达**且**没有本地 LKG 缓存。"""

    code = "RISK_005"


class RiskConfigWriteError(RiskConfigLoadError):
    """规则写入失败（RISK_005）—— 契约 §3.3.1 点名存储层的阈值写入方法抛它。

    契约里这个类名一直只出现在风控契约的 `Raises:` 段里，仓库中**从未定义**（I3b 补上，
    因为存储层的写入现在真的会抛它）。

    **为什么继承 `RiskConfigLoadError` 而不是与它平级的 `RiskError`**：两者是同一件事的
    两面（存储层不可用），而风控引擎的阈值变更入口已经按 `RiskConfigLoadError` 收口 ——
    「写不进去 ⇒ 旧值继续生效」（RISK_004 的语义：宁可旧值，不可无值，更不可半新半旧）。
    做成平级会绕过那个收口，把一次写失败变成**逃出那条收口的未知异常**，调用方拿到的
    是「栈里冒出来的东西」而不是一条 RISK 码。

    本 docstring 刻意**不写出**那三个阈值写入符号的名字：`test_risk_engine.py` 有一条
    D10 静态断言在扫 `quanauto/*.py` 的字面量。这不是为了绕过检查 —— 那条纪律本来就是
    靠「除 `risk.py` 外不许出现这些字面量」守着的，写在这里等于把闸门本身磨掉一点。
    """


class RiskDegradedError(RiskError):
    """降级运行（RISK_006）—— 配置源不可达但 LKG 仍在生效，只减仓。"""

    code = "RISK_006"


class KillSwitchActiveError(RiskError):
    """Kill Switch 已激活（RISK_007）—— 开仓一律拒绝。"""

    code = "RISK_007"


class RiskRuleVersionConflictError(RiskError):
    """规则版本冲突（RISK_010）—— 例如运行中检测到版本回退。"""

    code = "RISK_010"
