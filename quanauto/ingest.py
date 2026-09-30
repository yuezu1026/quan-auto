"""采集 → 落库：把「源这一次取回来的帧」真的写进数据中心那两张表（I2 收口 ①ⓑ）。

这个模块补的是**产品写入路径**，不是又一层实现。

## 为什么单独一个模块

* `quanauto/datasources.py`（采集侧）**不许**出现任何数据库驱动 import，惰性导入
  也算 —— `tools/verify_data_center_adapter.py` 的 A5 判的就是这条。适配器负责
  「把源的话翻译成我们的标准列」；一旦它也能连库，「取数失败」与「落库失败」就会
  在同一个 `except` 里被混成一句话，而这两件事调用方要做的事完全不同。
* `quanauto/pgstore.py` 是**存取实现**（SQL / 标度 / 幂等判等），它刻意不知道源长
  什么样。
* `SourceAdapter` 自己的类注释写着「不写数据库（由 DataCenter 统一写入，见 D1/D10）」。
  ⇒ 「谁来把这两半接起来」这个问题此前没有答案，本模块就是那个答案。

本模块**只做编排**：取数 → 校验 → 盖戳 → 落库 → 留痕。它不含 SQL 逻辑（SQL 只是
两条写 `dc_ingest_run` 的语句）、不含源协议知识。

## 一次批次的形状

1. **前置**（不碰网、不碰库）：标的非空且可归一化、起止有序、`data_version` 非空。
   前置失败**不留批次行** —— 那不是一次批次，是一次调用写错了。
2. `dc_ingest_run` 里**先写一行 `RUNNING`**。失败的批次才是日志最该留下的东西，
   所以是「先开行、后取数」，不是「取到再记」。
3. `adapter.fetch_*` → `adapter.validate(frame)`：`is_valid=False` ⇒ **一行都不写**，
   抛 `DataQualityError`。契约 §2.4 不允许用空集表示「找不到数据」，而
   `validate_frame` 对空帧就给 `is_valid=False` ⇒ 空帧天然走这一支，不必另写守卫。
4. 帧 → 行对象（盖 `source = adapter.source_name()`、`data_version`）→
   `PgBarIngestor.upsert_daily_bars` / `PgFactorIngestor.upsert_adjust_factors`。
5. 收尾：`status` 看**帧有没有覆盖全部请求标的**（缺 ⇒ `PARTIAL`，`db/data_center.sql`
   里 `dc_ingest_run.status` 的列注释就是这么定义 `PARTIAL` 的），
   `row_count` = 本批**落定**的行数（`inserted + skipped`，即 `IngestReport.total`）。

## 这里**没有**做的事（别把本模块读成「I2 已收口」）

* 只接了**两张表**：`dc_daily_bar` / `dc_adjust_factor`。`dc_quality_issue` 仍零写入者
  （质量标记要的那套「停牌/涨跌停」判据本迭代没做）。
* **没有主备源自动切换、没有重试、没有退避**。`SourceAdapterError.retryable` 是给
  **上层**的判据，本模块不消费它：「换个源再试一次」是调用方的决定（换 adapter 再
  调一次本函数），不是本模块的隐藏行为。
* **没有并发**：一次调用一条连接、串行。两张表各自的 `ON CONFLICT` 保证重跑幂等
  （契约 §3.10 的幂等要求），但不保证两个进程同时采同一段不打架。
* **没有产品入口**：`quanauto/cli.py` **不开**采集子命令，本模块只能被 Python 代码调用。
  这一条是**有意**的、不是漏做 —— 入口一开出来就成了一条对外承诺，而本层**从没在
  任何真实 PostgreSQL 上跑过**（`tests/test_ingest.py` 用的是假连接；真库那边只有
  `db/*.smoke.sql` 那条约束触测通道，它跑的不是本模块）。
* `get_dividend()` 仍恒 `0.0`（分红那条缺口与本模块无关，别在这里顺手改）。

**一句话边界**：本模块主张的是「**两个 ingestor 有了产品调用点**」—— 这正是
`docs/迭代计划.md` 里 I2 收口条件 ①ⓑ 那句话的字面内容。它**不**主张这条路径被
真实走过（入口未开、真库未验，见上），也**不**主张采集覆盖面已经够了（只两张表、
无主备切换）。

## 语汇的来源（一处都不许改方向）

`dc_ingest_run` 的 `status` / `priority` 取值域**照抄** DDL 的
`ck_dc_ingest_status` / `ck_dc_ingest_priority`。DDL 一个字都不能动（动一次就要重跑
四份 SQL 冒烟快照），所以一致性由 `tests/test_ingest.py` 从**DDL 文本**里把两个
`CHECK` 的值读出来比对：抄一份常量而不钉住它，就是下一个会漂的东西。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, List, Mapping, Sequence, Tuple

from .datacenter import AdjustFactorPoint, DailyBar
from .datasources import SourceAdapter, normalize_symbol
from .enums import SourcePriority
from .errors import (
    DataQualityError,
    DataStoreError,
    DataVersionError,
    InvalidParamError,
    QuanAutoError,
)
from .pgstore import IngestReport, PgBarIngestor, PgFactorIngestor, SqlConnection

LOGGER = logging.getLogger("quanauto.ingest")

__all__ = (
    "FACTOR_CHANNEL",
    "DAILY_CHANNEL",
    "INGEST_PRIORITIES",
    "INGEST_STATUSES",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "SQL_FINISH_RUN",
    "SQL_START_RUN",
    "IngestRun",
    "IngestRunLog",
    "ingest_adjust_factors",
    "ingest_daily_bars",
)

# ── 语汇：照抄 `db/data_center.sql` 的 CHECK，不许在这里发明新值 ──────────────
# 顺序也照 DDL 写（RUNNING/SUCCESS/PARTIAL/FAILED、PRIMARY/FALLBACK），这样人工
# 逐字对照时不用在脑子里重排。
STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"
INGEST_STATUSES = (STATUS_RUNNING, STATUS_SUCCESS, STATUS_PARTIAL, STATUS_FAILED)

INGEST_PRIORITIES = (SourcePriority.PRIMARY.value, SourcePriority.FALLBACK.value)

DAILY_CHANNEL = "日线"
FACTOR_CHANNEL = "复权因子"

#: 开批次。`status` 也走参数（不写死在 SQL 里），这样「状态字面量一共出现在几个
#: 地方」答案是「模块顶部那五个常量」，而不是「常量 + 两条 SQL 的字符串里」。
SQL_START_RUN = (
    "INSERT INTO dc_ingest_run (adapter, priority, start_date, end_date, "
    "symbol_count, data_version, status) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "RETURNING run_id"
)

#: 收批次。`finished_at` 用 `CURRENT_TIMESTAMP(3)`（与 DDL 默认值同一个精度），
#: 不把时间当参数传 —— 传参就得决定「谁的时间」，而这行日志的时间戳应当来自库。
SQL_FINISH_RUN = (
    "UPDATE dc_ingest_run "
    "SET status = %s, row_count = %s, error_message = %s, finished_at = CURRENT_TIMESTAMP(3) "
    "WHERE run_id = %s "
    "RETURNING run_id"
)


# ── 前置检查（都不碰网、不碰库）───────────────────────────────────────────────
def _requested_symbols(symbols: Sequence[str]) -> Tuple[str, ...]:
    """请求的标的清单 → 归一化后的元组，**保序去重**。

    为什么要在这里归一化，而不是原样丢给适配器：收尾时要拿「帧里的 symbol」算
    `missing`，而帧里的 symbol 已经被 `normalize_daily_bar` / `normalize_adjust_factor`
    归一过了（`600000` → `600000.SH`）。请求侧不归一的话，`['600000']` 与帧里的
    `['600000.SH']` 永远对不上 ⇒ 每个标的都被算成「没取到」，批次**每次都假报 PARTIAL**。
    这种错不会炸、只会让状态列永远不可信，所以钉了一条测试。

    归一化失败走 `SourceAdapterError`（`normalize_symbol` 本来就抛它）—— 「写错了
    一个标的代码」与「这个源这份数据不能用」在调用方眼里是同一类事：都得改输入。
    """
    if symbols is None or isinstance(symbols, (str, bytes)):
        raise InvalidParamError("symbols 必须是标的代码的序列，收到 %r —— 传一个字符串会"
                                "被按字符拆开，所以这里直接拒绝" % (symbols,))
    ordered: List[str] = []
    seen = set()
    for code in symbols:
        normalized = normalize_symbol(code)
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    if not ordered:
        raise InvalidParamError("symbols 是空的：一次批次至少要有一个标的"
                                "（空标的会取回空帧，而空帧在契约 §2.4 里必须翻译成显式失败，"
                                "不是「校验通过」）")
    return tuple(ordered)


def _check_window(start: Any, end: Any) -> None:
    """起止必须是 `date` 且有序。**拒绝 `datetime`**：见下方注释。"""
    for value, name in ((start, "start"), (end, "end")):
        if isinstance(value, datetime):
            # 刻意拒绝而不是自动截断成日期：`fetch_*` 的形参类型是 `date`，截断会
            # 让「同一天的两个窗口」因为隐藏的时分秒而可能不相等 —— 采集窗口是
            # 自然日，不是时刻。
            raise InvalidParamError("%s 必须是 datetime.date，收到 datetime（%r）："
                                    "采集窗口按自然日算，带上时分秒会让同一天的两个窗口"
                                    "不再相等" % (name, value))
        if not isinstance(value, date):
            raise InvalidParamError("%s 必须是 datetime.date，收到 %r（%s）"
                                    % (name, value, type(value).__name__))
    if end < start:
        raise InvalidParamError("采集窗口反了：end=%s 早于 start=%s" % (end, start))


def _require_version(data_version: Any) -> None:
    """`data_version` 非空（D8）。空值也过不了下游 `PgBarIngestor._require_version`，
    但那时已经开了批次行、取完数了 —— 在这里拦住能省掉一次网络往返和一条脏日志。"""
    if not isinstance(data_version, str) or not data_version.strip():
        raise DataVersionError("写库必须显式绑定 data_version（D8：回测结果要能绑上版本），"
                               "收到 %r" % (data_version,))


def _priority_value(priority: Any) -> str:
    """`SourcePriority` → `dc_ingest_run.priority` 的那个字面量。"""
    value = getattr(priority, "value", priority)
    if value not in INGEST_PRIORITIES:
        raise InvalidParamError(
            "priority 必须是 SourcePriority.PRIMARY / FALLBACK 之一（对应 "
            "ck_dc_ingest_priority），收到 %r" % (priority,))
    return value


# ── 批次留痕 ─────────────────────────────────────────────────────────────────
def _run(conn: SqlConnection, sql: str, params: Sequence[Any], what: str) -> List[Mapping[str, Any]]:
    """执行一条语句并把驱动异常收成 `DataStoreError`。

    与 `pgstore._run` 是**同一个形状**（本项目异常原样穿过、驱动异常包一层 `from exc`）
    而不是 import 它的私有函数：`pgstore` 那条出口的语义是「存取实现」的出口，这条是
    「批次留痕」的出口，两者将来要不要合，取决于要不要把留痕也做成一个 Store —— 那是
    一个设计决定，不该由一个 import 悄悄替人做掉。**这里只有两条语句，形状抄的是约定，
    不是实现。**
    """
    try:
        return list(conn.execute(sql, params))
    except QuanAutoError:
        raise
    except Exception as exc:
        raise DataStoreError("%s 失败（%s）：%s" % (what, type(exc).__name__, exc)) from exc


class IngestRunLog:
    """`dc_ingest_run` 的开/收两条语句。

    单独一个类而不是两个模块级函数，是为了让「一个批次恰好一行、行先开后退」这件事
    有个**对象**可以钉：测试塞一个假连接进来，就能断言「取数前后各一条 SQL、且第二条
    带的 `run_id` 正是第一条返回的那个」。
    """

    def __init__(self, conn: SqlConnection) -> None:
        self.conn = conn

    def start(self, *, adapter: str, priority: str, start_date: date, end_date: date,
              symbol_count: int, data_version: str) -> int:
        """开一行 `RUNNING` 并返回 `run_id`。"""
        rows = _run(
            self.conn, SQL_START_RUN,
            (adapter, priority, start_date, end_date, symbol_count, data_version, STATUS_RUNNING),
            "开采集批次（dc_ingest_run）",
        )
        if not rows:
            # `INSERT ... RETURNING` 不给行 = 驱动/连接这一层坏了。这里**不能**
            # 退化成「run_id 就用 None，继续跑」：那会让收尾的 UPDATE 静默改 0 行，
            # 整条留痕看起来跑过了、实际一行没写。
            raise DataStoreError("开采集批次没有返回 run_id：`INSERT ... RETURNING run_id` "
                                 "应当总是给出一行")
        run_id = rows[0].get("run_id")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            raise DataStoreError("dc_ingest_run 返回的 run_id 不是整数：%r（%s）"
                                 % (run_id, type(run_id).__name__))
        return run_id

    def finish(self, run_id: int, *, status: str, row_count: int,
               error_message: str = "") -> None:
        """收批次。`status` 不许是 `RUNNING` —— 收批次却让行停在「跑着」，就等于留了
        一条永远不会结束的日志。"""
        if status not in INGEST_STATUSES:
            raise InvalidParamError("status=%r 不在 dc_ingest_run 的取值域 %s 里"
                                    % (status, INGEST_STATUSES))
        if status == STATUS_RUNNING:
            raise InvalidParamError("收批次时不能把状态写回 RUNNING：那会让这一行永远"
                                    "停在「跑着」，看日志的人无从分辨「还在跑」与「崩了没记上」")
        rows = _run(
            self.conn, SQL_FINISH_RUN,
            (status, row_count, error_message, run_id),
            "收采集批次（dc_ingest_run）",
        )
        if not rows:
            raise DataStoreError("收采集批次没有改到任何行（run_id=%s）：这一行不存在？"
                                 "那么前面那条 INSERT 到底走到哪里去了" % (run_id,))


@dataclass(frozen=True)
class IngestRun:
    """一次采集批次的结果。字段与 `dc_ingest_run` 的那一行**一一对应**（`run_id`
    就是 DDL 的主键，`row_count` 是 `inserted + skipped`）。

    刻意**不**带 `ValidationReport`：`is_valid=False` 那条路是抛异常出去的，所以这个
    对象只在「数据已经落定了」时存在（与 `IngestReport` 的约定同一条：报告拿到 ⇒ 整批
    落定）。

    `status` 在这里**只会是** `SUCCESS` 或 `PARTIAL` —— `FAILED` 不产出对象，它抛异常。
    留 `status` 字段是因为它是 `dc_ingest_run` 的列，读日志的人要能一眼对上。
    """

    run_id: int
    status: str
    adapter: str
    priority: str
    data_version: str
    start_date: date
    end_date: date
    requested_symbols: Tuple[str, ...]
    received_symbols: Tuple[str, ...]
    missing_symbols: Tuple[str, ...]
    inserted: int
    skipped: int
    warnings: Tuple[str, ...] = ()

    @property
    def row_count(self) -> int:
        return self.inserted + self.skipped

    @property
    def complete(self) -> bool:
        """这批有没有覆盖全部请求的标的。**不是**「成功/失败」—— `PARTIAL` 也是落定了的。"""
        return not self.missing_symbols


# ── 帧 → 行对象 ──────────────────────────────────────────────────────────────
def _to_row(row_class: Any, record: Mapping[str, Any], source: str,
            data_version: str) -> Any:
    """一条帧记录 → `DailyBar` / `AdjustFactorPoint`。

    `source` / `data_version` 在这里盖戳，不指望源给：`source` 是**溯源**（哪一次
    采集写进去的），`data_version` 是**版本**（D8），两者都是本层的知识，源不可能知道。

    刻意**不**先筛一遍列清单：帧走到这里已经过 `validate_frame`，它的列恰好就是
    某个标准 schema（多出来的源列名会在那里被判红灯）—— 而标准 schema 的列是
    `DAILY_BAR_COLUMNS`（8 列，不含 `source`/`data_version`），行对象却多这两列。
    拿 schema 当构造参数就会把盖戳的那两列筛掉，于是「戳是怎么盖上的」变成一段
    看不见的隐式行为。直接把「记录 + 戳」当关键字参数，少了哪一列就当场 `TypeError`。
    """
    values = dict(record)
    values["source"] = source
    values["data_version"] = data_version
    try:
        return row_class(**values)
    except TypeError as exc:
        # 走到这里说明「帧里的列」与「行对象的字段」不再一一对应 —— 要么是行对象
        # 新加了一个必填字段（要把 `ingest` 也改上），要么是帧里多了列（那本该被
        # `validate_frame` 拦下）。无论哪种，都不是数据脏，所以报 `DataQualityError`
        # 并把真正的原因带上：落库那一步根本没开始。
        raise DataQualityError(
            "无法用帧里的列构造 %s：%s —— 帧的列与行对象的字段不再一一对应"
            % (getattr(row_class, "__name__", row_class), exc)) from exc


# ── 编排主体 ─────────────────────────────────────────────────────────────────
def _ingest_batch(adapter: SourceAdapter, *, channel: str, row_class: Any,
                  fetch: Callable[..., Any],
                  write: Callable[[Sequence[Any]], IngestReport],
                  symbols: Sequence[str], start: date, end: date, data_version: str,
                  conn: SqlConnection, priority: Any) -> IngestRun:
    """日线 / 复权因子两条通道共用的编排。

    刻意**不做成模板方法**（不在 `IngestRunLog` 或适配器上加钩子）：两条通道的差别
    只有「取哪个方法 / 建哪个行对象 / 调哪个 upsert」三件事，各写一个三行的公开函数
    比一个带 `channel=` 判断的通用入口更好读 ——
    `pgstore` 里 `PgBarIngestor` / `PgFactorIngestor` 不合并也是同一个理由（判等列
    集合 6 vs 1、标度 4 vs 8 是真的，合并会把它们变成两个布尔参数）。
    """
    requested = _requested_symbols(symbols)
    _check_window(start, end)
    _require_version(data_version)
    priority_value = _priority_value(priority)

    adapter_name = adapter.source_name()
    log = IngestRunLog(conn)
    run_id = log.start(adapter=adapter_name, priority=priority_value, start_date=start,
                       end_date=end, symbol_count=len(requested), data_version=data_version)
    try:
        frame = fetch(list(requested), start, end)
        report = adapter.validate(frame)
        if not report.is_valid:
            raise DataQualityError(
                "%s 取回的%s帧没通过校验（%d 行）：%s —— 按契约「is_valid 为 False 时"
                "不得写入」，本批一行都没落库"
                % (adapter_name, channel, report.row_count,
                   "；".join(report.errors) or "（校验器没给出原因）"))

        rows = [_to_row(row_class, record, adapter_name, data_version)
                for record in frame.to_dict("records")]
        settled = write(rows)
        if settled.total != len(rows):
            # 这一条在**行已经落库之后**才可能触发（`PsycopgConnection` 是
            # autocommit）⇒ 抛出去会把批次标成 FAILED，而表里的行还在。宁可这样也
            # 不能反过来：把「账对不上」的批次标成 SUCCESS，等于让日志主动撒谎。
            # 消息里必须写清这个事实，否则下一个读日志的人会以为「一行都没写」。
            raise DataStoreError(
                "落库报告说处理了 %d 行，而帧里有 %d 行 —— 账对不上。注意：**前面的行"
                "已经落库了**（本层的连接是 autocommit），要修的是这次的写入口，"
                "不是重跑这个批次" % (settled.total, len(rows)))

        received = tuple(sorted({str(symbol) for symbol in frame["symbol"]}))
        got = set(received)
        missing = tuple(symbol for symbol in requested if symbol not in got)
        status = STATUS_PARTIAL if missing else STATUS_SUCCESS
        note = "" if not missing else ("帧只覆盖 %d 个标的，缺：%s"
                                      % (len(received), "、".join(missing)))
        log.finish(run_id, status=status, row_count=settled.total, error_message=note)
        return IngestRun(
            run_id=run_id, status=status, adapter=adapter_name, priority=priority_value,
            data_version=data_version, start_date=start, end_date=end,
            requested_symbols=requested, received_symbols=received,
            missing_symbols=missing, inserted=settled.inserted,
            skipped=settled.skipped, warnings=tuple(report.warnings),
        )
    except Exception as exc:  # noqa: BLE001 - 见下方注释：这里必须拦宽，且必须原样重抛
        # 刻意拦 `Exception` 而不是只拦 `QuanAutoError`：留痕的价值恰恰在于**崩了也
        # 留下**。只拦本项目异常的话，一个 `KeyError`（本仓库的 bug）会让这一行永远
        # 停在 RUNNING，而「还在跑」与「崩了没记上」在日志里长得一模一样。
        _record_failure(log, run_id, exc)
        raise


def _record_failure(log: IngestRunLog, run_id: int, exc: BaseException) -> None:
    """把批次标成 `FAILED`。**绝不允许盖住正在抛的那个异常**。"""
    message = str(exc) if isinstance(exc, QuanAutoError) else "%s: %s" % (type(exc).__name__, exc)
    try:
        log.finish(run_id, status=STATUS_FAILED, row_count=0, error_message=message)
    except Exception:  # noqa: BLE001 - 记不上就算了，但绝不能盖住真正的失败
        LOGGER.warning("采集批次 run_id=%s 的失败状态没记上（dc_ingest_run 仍是 RUNNING）",
                       run_id, exc_info=True)


# ── 两个公开入口 ─────────────────────────────────────────────────────────────
def ingest_daily_bars(adapter: SourceAdapter, symbols: Sequence[str], start: date, end: date,
                      *, conn: SqlConnection, data_version: str,
                      priority: SourcePriority = SourcePriority.PRIMARY) -> IngestRun:
    """采集一批**日线**并落进 `dc_daily_bar`。

    `conn` / `data_version` 是 keyword-only 且**没有默认值**：漏掉它们应当是
    `TypeError`，不是「悄悄用了一个默认版本号」。`data_version` 尤其 —— 它绑错一次，
    这批数据就永远挂在一个错的版本上（D8）。

    异常（按调用方要做的动作分组）:
        `InvalidParamError` / `DataVersionError`: 入参写错了，改输入重调，没碰网也没留痕。
        `SourceAdapterError`: 源这一侧的问题（`kind` / `retryable` 见 errors 的类别表）。
        `DataQualityError`: 帧没过校验（**一行都没落库**），去修采集。
        `DataStoreError`: 库这一侧的问题（含「开/收批次行」失败）。
    """
    return _ingest_batch(
        adapter, channel=DAILY_CHANNEL, row_class=DailyBar,
        fetch=adapter.fetch_daily_bar, symbols=symbols, start=start, end=end,
        data_version=data_version, conn=conn, priority=priority,
        write=lambda rows: PgBarIngestor(conn).upsert_daily_bars(rows),
    )


def ingest_adjust_factors(adapter: SourceAdapter, symbols: Sequence[str], start: date,
                          end: date, *, conn: SqlConnection, data_version: str,
                          priority: SourcePriority = SourcePriority.PRIMARY) -> IngestRun:
    """采集一批**复权因子**并落进 `dc_adjust_factor`（走的是 2026-09-29 晚 Ⅲ 接上的
    `fetch_adjust_factor` 那条通道）。

    例外同 `ingest_daily_bars`。**没有 `get_dividend` 的对应物** —— 分红缺口（
    `DbDataFeed.get_dividend()` 恒 `0.0`）不在这里关。
    """
    return _ingest_batch(
        adapter, channel=FACTOR_CHANNEL, row_class=AdjustFactorPoint,
        fetch=adapter.fetch_adjust_factor, symbols=symbols, start=start,
        end=end, data_version=data_version, conn=conn, priority=priority,
        write=lambda rows: PgFactorIngestor(conn).upsert_adjust_factors(rows),
    )
