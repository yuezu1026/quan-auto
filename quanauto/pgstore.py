"""数据中心落库侧（I2 S3）—— `dc_daily_bar` 的读写实现。

## 与读侧的分工

* `quanauto/datacenter.py`（S1）管「**看到什么**」：`as_of` 冻结 + `PITGuard` 第二层，
  **一行 SQL 都没有**；
* 本模块（S3）管「**怎么读写**」：SQL 文本、参数绑定、幂等、异常包装，
  **一点 PIT 逻辑都没有**。

分开的理由：PIT 是判据，SQL 是手段。手段换了（换库、换驱动、加分区）不该动判据；
判据改了不该动手段。

## 约定 1：SQL 一律参数化，且以模块级常量出现

`%s` 占位 + 参数元组是**唯一**允许的形式。理由不只是注入：SQL 文本一旦是拼接出来的，
「窗口条件在不在」就变成运行期才知道的事，静态门禁再也核不了 —— 而「窗口条件丢了」
正是 S1 里那条最重要注释要防的 bug。

SQL 写成模块级常量而不是内联在方法里，是为了让门禁**不执行代码**就能核到文本形状；
也让「读侧和写侧用的是不是同一套列名」变成可 diff 的东西。

## 约定 2：读必须绑定 `data_version`（D8）

`dc_daily_bar` 的主键是 `(symbol, trade_date, data_version)` —— 多个版本**共存**。
所以不绑定版本地读，同一个 `(symbol, trade_date)` 会按版本数重复出现，回测看到的是
叠影（而且叠影看起来像「成交量变大了」，不像 bug）。
`PgBarStore` 因此把 `data_version` 做成**必填位置参数**：忘传是 `TypeError`，
不是静默多读几行。

## 约定 3：幂等的读法是「同值不写」

约定 6（`INSERT ... ON CONFLICT DO UPDATE`）与 DATA_007（同版本同主键出现不同值要报冲突）
合起来只剩一种自洽的读法：**值相同 ⇒ 一个字节都不写**（连 `ingested_at` 都不动），
**值不同 ⇒ 抛 `IngestConflictError`**（D8：版本不可变）。

判等只看 `VALUE_FIELDS` 六个数值列。`source` 是溯源信息不是行情内容：同一版本的同一行
被备源复核过是正常事件，不该被当成数据变更，更不该为此反复写库（那会让「重跑 == 跑一次」
变成假的）。

## 约定 4：驱动是注入进来的，且只在用到时才 import

`tools/` 与 CI 环境都没有 psycopg。驱动必须满足两条：**导入本模块不拉驱动**、
**没装驱动时报的是我们的异常而不是 `ImportError`**。所以真实驱动包在
`PsycopgConnection` 里惰性 import，异常映射成 `InvalidConfigError`（部署问题）
或 `DataStoreError`（库操作失败），不裸逃。

## 约定 5：驱动异常只在出口收一次

`conn.execute` 外面套一层 `_run`，把任何非本项目的异常包装成 `DataStoreError`
并挂 `__cause__`。本项目的异常（`IngestConflictError` …）**原样穿过** ——
否则调用方再也分不出「数据版本不可变，要人来处理」和「库挂了，要重试」。

## 约定 6：先按主键查、再决定写（并应对并发）

每行写入前的两次往返：
1. `SELECT` 现有行 ⇒ 有则判等（同值跳过 / 异值抛冲突），无则继续；
2. `INSERT ... ON CONFLICT DO UPDATE ... RETURNING symbol` ⇒ 返回了行说明写成功，
   **没返回行**说明有并发写者抢先插了同一主键（约定 6 的 `DO UPDATE` 带 `WHERE`，
   值相同时它不会更新、也就不会返回行）⇒ 再查一次判等。

代价写在这里而不是留着让人猜：每行 1~2 个往返。批量化（多值 `VALUES` / `COPY`）留到
数据量真的成问题时再做 —— 现在做的幂等语义，换实现时不必改。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, List, Mapping, Protocol, Sequence, runtime_checkable

from .datacenter import BarStore, DailyBar
from .errors import (
    DataStoreError,
    DataVersionError,
    IngestConflictError,
    InvalidConfigError,
    QuanAutoError,
)

__all__ = [
    "BAR_COLUMNS",
    "VALUE_FIELDS",
    "SQL_SELECT_BARS",
    "SQL_SELECT_EXISTING",
    "SQL_SELECT_SYMBOLS",
    "SQL_UPSERT_BAR",
    "IngestReport",
    "PgBarIngestor",
    "PgBarStore",
    "PsycopgConnection",
    "SqlConnection",
]

# ── 形状常量：列清单只有一份，读写共用 ────────────────────────────────────
#: `dc_daily_bar` 被本模块读写的列（顺序 = `INSERT` 的列序 = `%s` 的绑定顺序）。
BAR_COLUMNS = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "data_version",
)

#: 参与「同值/异值」判定的字段（见约定 3）。
VALUE_FIELDS = ("open", "high", "low", "close", "volume", "amount")

# ── SQL ──────────────────────────────────────────────────────────────────
#: 读一个 (标的, 版本, 闭区间窗口) 的日线，按交易日升序。
SQL_SELECT_BARS = (
    "SELECT symbol, trade_date, open, high, low, close, volume, amount, source, data_version "
    "FROM dc_daily_bar "
    "WHERE symbol = %s AND data_version = %s "
    "AND trade_date >= %s AND trade_date <= %s "
    "ORDER BY trade_date"
)

#: 一个版本里的标的清单（去重：同一标的的不同版本各占一行）。
SQL_SELECT_SYMBOLS = (
    "SELECT DISTINCT symbol FROM dc_daily_bar WHERE data_version = %s ORDER BY symbol"
)

#: 按主键取现有行的内容 —— 「同值不写」与「异值报冲突」都靠它。
SQL_SELECT_EXISTING = (
    "SELECT open, high, low, close, volume, amount FROM dc_daily_bar "
    "WHERE symbol = %s AND trade_date = %s AND data_version = %s"
)

#: 约定 6：按业务主键 upsert。`DO UPDATE ... WHERE` 只在**值真的不同**时才更新，
#: 于是「没有行返回」本身就是一个信号（见约定 6 第 2 步）。
SQL_UPSERT_BAR = (
    "INSERT INTO dc_daily_bar "
    "(symbol, trade_date, open, high, low, close, volume, amount, source, data_version) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (symbol, trade_date, data_version) DO UPDATE "
    "SET source = EXCLUDED.source, ingested_at = CURRENT_TIMESTAMP(3) "
    "WHERE (dc_daily_bar.open, dc_daily_bar.high, dc_daily_bar.low, dc_daily_bar.close, "
    "dc_daily_bar.volume, dc_daily_bar.amount) IS DISTINCT FROM "
    "(EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, "
    "EXCLUDED.volume, EXCLUDED.amount) "
    "RETURNING symbol"
)


@runtime_checkable
class SqlConnection(Protocol):
    """执行 SQL 的最小缝（seam）。

    只要求两件事：`execute(sql, params) -> 行序列` 与 `transaction()` 上下文。
    `PgBarStore` / `PgBarIngestor` 只认这个协议，不认 psycopg ——
    于是用例可以注入假连接，真实驱动只是它的一种实现。
    """

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[Mapping[str, Any]]:
        """执行一条语句；返回行（映射）。参数一律走 `params`，不许拼进 `sql`。"""
        ...

    def transaction(self) -> Any:
        """一个事务上下文：正常退出提交，异常退出回滚。"""
        ...


@dataclass(frozen=True)
class IngestReport:
    """一批写入的结果。

    `inserted` 与 `skipped` 之和必须等于输入行数 —— 中间任何一行出问题都不返回本对象，
    而是抛异常。所以「报告拿到了」本身就意味着「整批都落定了」。
    """

    inserted: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.skipped


# ── 取值归一：库里出来的可能是字符串 ─────────────────────────────────────
def _as_date_value(value) -> date:
    """`date` / `datetime` / ISO 字符串 → `date`。

    为什么不信「驱动会给 `date`」：psycopg 会给，但文本协议、导出文件、一次性 `psql`
    查询给的是字符串。落库层若假定类型，「换成字符通道」就会让日期比较静默变成
    字符串比较（`'2026-01-05' <= date(...)` 直接 `TypeError`，或者更糟：两个字符串比大小）。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError as exc:
            raise DataStoreError("交易日不是合法的 ISO 日期：%r" % (value,)) from exc
    raise DataStoreError("交易日类型无法识别：%r（%s）" % (value, type(value).__name__))


def _as_decimal(value) -> Decimal:
    """数值列一律走 `Decimal`。

    用 `Decimal(str(value))` 而不是 `Decimal(value)`：`Decimal(0.1)` 会把二进制浮点的
    误差原样带进来（`0.1000000000000000055511151231257827`），于是「同一个 0.1」在
    两条路径上判不相等 —— 判等和 CHECK 约束都会跟着出错。`bool` 先挡掉，因为它是
    `int` 的子类，会被 `str()` 变成 `'True'`。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise DataStoreError("数值列收到 %r，无法转成 Decimal" % (value,))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise DataStoreError("数值列收到 %r，无法转成 Decimal" % (value,)) from exc


def _row_to_bar(row: Mapping[str, Any]) -> DailyBar:
    try:
        return DailyBar(
            symbol=str(row["symbol"]),
            trade_date=_as_date_value(row["trade_date"]),
            open=_as_decimal(row["open"]),
            high=_as_decimal(row["high"]),
            low=_as_decimal(row["low"]),
            close=_as_decimal(row["close"]),
            volume=_as_decimal(row["volume"]),
            amount=_as_decimal(row["amount"]),
            source="" if row["source"] is None else str(row["source"]),
            data_version=str(row["data_version"]),
        )
    except KeyError as exc:
        raise DataStoreError(
            "按主键取回的行缺少列 %s —— 读的列清单与 db/data_center.sql 不一致" % (exc,)
        ) from exc


def _run(conn: SqlConnection, sql: str, params: Sequence[Any], what: str) -> List[Mapping[str, Any]]:
    """执行一条语句；驱动异常一律在**这里**收口（约定 5）。"""
    try:
        return list(conn.execute(sql, params))
    except QuanAutoError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 收口就是本函数存在的理由
        raise DataStoreError(
            "%s 失败（%s）：%s" % (what, type(exc).__name__, exc)
        ) from exc


def _require_version(value, what: str) -> str:
    """D8：没有 data_version 的行/查询不该存在。"""
    if not isinstance(value, str) or not value.strip():
        raise DataVersionError(
            "%s 的 data_version 是 %r —— D8：版本缺失的行不该落库（写进去，那段历史就再也"
            "取不回来）；读也必须绑定版本，否则同一 (symbol, trade_date) 会按版本数重复出现"
            % (what, value)
        )
    return value.strip()


def _insert_params(bar: DailyBar) -> tuple:
    """按 `BAR_COLUMNS` 的顺序绑定 —— 列序与值序必须同源，否则会「写反了也成功」。"""
    return (
        bar.symbol,
        _as_date_value(bar.trade_date),
        _as_decimal(bar.open),
        _as_decimal(bar.high),
        _as_decimal(bar.low),
        _as_decimal(bar.close),
        _as_decimal(bar.volume),
        _as_decimal(bar.amount),
        "" if bar.source is None else str(bar.source),
        _require_version(bar.data_version, "行 %s/%s" % (bar.symbol, bar.trade_date)),
    )


def _primary_key(bar: DailyBar) -> tuple:
    return (bar.symbol, _as_date_value(bar.trade_date), _require_version(
        bar.data_version, "行 %s/%s" % (bar.symbol, bar.trade_date)))


def _diff_fields(bar: DailyBar, row: Mapping[str, Any]) -> List[str]:
    try:
        return [
            name for name in VALUE_FIELDS
            if _as_decimal(getattr(bar, name)) != _as_decimal(row[name])
        ]
    except KeyError as exc:
        raise DataStoreError(
            "按主键取回的行缺少列 %s —— 列清单与 db/data_center.sql 不一致" % (exc,)
        ) from exc


def _raise_if_divergent(bar: DailyBar, row: Mapping[str, Any], concurrent: bool = False) -> None:
    """同主键异值 ⇒ DATA_007。这里抛，调用方（以及事务上下文）负责回滚。"""
    diffs = _diff_fields(bar, row)
    if not diffs:
        return
    details = "; ".join(
        "%s 库内 %s / 本批 %s" % (name, _as_decimal(row[name]), _as_decimal(getattr(bar, name)))
        for name in diffs
    )
    symbol, trade_date, data_version = _primary_key(bar)
    raise IngestConflictError(
        "主键 (symbol=%s, trade_date=%s, data_version=%s) 已存在且值不同：%s%s —— "
        "D8：同一版本的行不可变，DATA_007 提示可能并发跑了两份采集。"
        "要改写历史请换一个新的 data_version 重采，不要用 upsert 把冲突盖掉。"
        % (symbol, trade_date, data_version, details,
           "（并发写入后复查发现）" if concurrent else "")
    )


class PgBarStore(BarStore):
    """PostgreSQL 支撑的 `BarStore`：按窗口 + 版本读日线。"""

    def __init__(self, conn: SqlConnection, data_version: str):
        self.conn = conn
        self.data_version = _require_version(data_version, "PgBarStore")

    def select_bars(self, symbol: str, start: date, end: date) -> List[DailyBar]:
        rows = _run(
            self.conn,
            SQL_SELECT_BARS,
            (symbol, self.data_version, _as_date_value(start), _as_date_value(end)),
            "读日线窗口",
        )
        return [_row_to_bar(row) for row in rows]

    def select_symbols(self) -> List[str]:
        rows = _run(self.conn, SQL_SELECT_SYMBOLS, (self.data_version,), "读标的清单")
        return [str(row["symbol"]) for row in rows]


class PgBarIngestor:
    """按 D10 幂等写日线：同值不写、异值抛 `IngestConflictError`。

    整批一个事务：中途任何一行判成冲突，前面已写的行随事务一起回滚 ——
    「半批落库」比「整批失败」难查得多（要对着 `ingested_at` 逐行找）。
    """

    def __init__(self, conn: SqlConnection):
        self.conn = conn

    def upsert_daily_bars(self, rows: Sequence[DailyBar]) -> IngestReport:
        bars = list(rows)
        # 先把「没有版本号」这类形状问题一次过掉：不写一行、不开事务。
        for bar in bars:
            _require_version(bar.data_version, "行 %s/%s" % (bar.symbol, bar.trade_date))
        inserted = 0
        skipped = 0
        with self.conn.transaction():
            for bar in bars:
                if self._write_one(bar):
                    inserted += 1
                else:
                    skipped += 1
        return IngestReport(inserted=inserted, skipped=skipped)

    def _write_one(self, bar: DailyBar) -> bool:
        """返回 True 表示这一行是新写入的，False 表示本来就在库里且值相同。"""
        key = _primary_key(bar)
        existing = _run(self.conn, SQL_SELECT_EXISTING, key, "按主键查现有行")
        if existing:
            _raise_if_divergent(bar, existing[0])
            return False
        written = _run(self.conn, SQL_UPSERT_BAR, _insert_params(bar), "写入日线")
        if written:
            return True
        # upsert 没返回行：`DO UPDATE ... WHERE` 说明值相同，但那要「行已存在」才可能。
        # 预查没看到行 ⇒ 只能是并发写者在这一瞬间插进去的同一主键。
        again = _run(self.conn, SQL_SELECT_EXISTING, key, "并发写入后复查")
        if not again:
            raise DataStoreError(
                "主键 %s 的 upsert 既没返回行、复查也查不到 —— 驱动/事务语义与预期不符，"
                "不能当作成功" % (key,)
            )
        _raise_if_divergent(bar, again[0], concurrent=True)
        return False


def _import_psycopg():
    """惰性 import 驱动（约定 4）。`import psycopg` 必须在函数体里。"""
    try:
        import psycopg  # noqa: PLC0415 —— 惰性是本函数的全部意义
    except ImportError as exc:
        raise InvalidConfigError(
            "落库需要 psycopg（psycopg 3）：pip install 'psycopg>=3.1'。"
            "本模块在导入期**不**拉驱动，所以这个错误只会在真正要用库时出现。"
        ) from exc
    return psycopg


class PsycopgConnection:
    """`SqlConnection` 的真实实现（psycopg 3，惰性 import）。"""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise InvalidConfigError("落库 DSN 不能为空：PostgreSQL 连接串如 "
                                     "'postgresql://user:pw@host:5432/quanauto'")
        self.dsn = dsn
        self._conn = None

    def _connect(self):
        if self._conn is None:
            psycopg = _import_psycopg()
            # `row_factory=dict_row`：本模块按**列名**取字段（`row["close"]`）。
            # 用默认的元组行会让每个取值点依赖列序 —— 列序一改就静默错位。
            self._conn = psycopg.connect(self.dsn, row_factory=psycopg.rows.dict_row)
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[Mapping[str, Any]]:
        try:
            cursor = self._connect().execute(sql, params)
            return list(cursor.fetchall())
        except QuanAutoError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 与 `_run` 同源，收口在出口
            raise DataStoreError("执行 SQL 失败（%s）：%s" % (type(exc).__name__, exc)) from exc

    @contextmanager
    def transaction(self) -> Iterator["PsycopgConnection"]:
        """psycopg 3 的连接上下文语义：正常退出提交，异常退出回滚。"""
        conn = self._connect()
        with conn:
            yield self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
