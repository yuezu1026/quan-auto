"""数据中心落库侧（I2 S3）—— `dc_daily_bar` / `dc_adjust_factor` / `dc_dividend` 的读写实现。

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

**默认安装与 CI 环境都没有 psycopg**（本机 `.venv` 有 —— `psycopg` 3.3.6，
`psycopg-binary` 同版本；驱动自 2026-09-25 起声明在 `pyproject.toml` 的 `[postgres]`
extra 里，**但 extra 是 opt-in，CI 只装 `.[dev]`**），所以这条「没有驱动」的话仍然成立，
只是**换了个范围**：不是「哪里都不能装」，而是「默认装的依赖里没有」。要复现本机口径就
`pip install -e ".[postgres]"`；没装驱动时跑 `tools/pytest_mutation_check.py`，
驱动相关那几条会退回 `ENV-LIMIT`，见它的出口分类）。
驱动必须满足两条：**导入本模块不拉驱动**、
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

## 约定 7：分红表的「事件日 / 可见性」是两个日期，落库层两个都存、只用一个做窗口

`dc_dividend`（2026-10-01，附录 B22）是本仓库第一张**可见性依据 ≠ 事件日**的表（D4）：
`ex_date`（除权除息日）是**事件日**，`announce_date`（公告日）是**可见性依据**。落库层的分工：

* **读窗口打在 `ex_date`**：`select_dividends(symbol, start, end)` 的闭区间说的是
  「哪几天发生了事件」—— 与契约 §3.2 `fetch_dividend` 的过滤键同一列
  （⚠️ **不是** `announce_date`：拿公告日当窗口，会让「除权除息日 = T 的那条分红」
  在 T 之前就落进窗口，`get_dividend(T)` 于是拿到一条还没到账的现金）；
* **`announce_date` 原样存下来、并交给 `PITGuard`**（`DbDataFeed._select_dividends` 把每一行的
  它当 `record_access` 的日期）：可见性是**读侧判据**，SQL 里一个字都不判 —— 在 SQL 里判
  就得知道 `as_of_date`，而那正是 S1/S3 分工要避免的（本模块一点 PIT 逻辑都没有）。

⚠️ 两列都进 `INSERT`，谁都不许省：只存 `ex_date` 会让「这条信息当时公不公开」无从判定
（D4 全部的意义就在这一列），只存 `announce_date` 则让「现金哪天到账」无从判定。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterator, List, Mapping, Protocol, Sequence, runtime_checkable

from .datacenter import (
    AdjustFactorPoint,
    BarStore,
    DailyBar,
    DividendPoint,
    DividendStore,
    FactorStore,
)
from .errors import (
    DataStoreError,
    DataVersionError,
    IngestConflictError,
    InvalidConfigError,
    QuanAutoError,
)

__all__ = [
    "BAR_COLUMNS",
    "DIVIDEND_COLUMNS",
    "FACTOR_COLUMNS",
    "VALUE_FIELDS",
    "SQL_SELECT_BARS",
    "SQL_SELECT_DIVIDENDS",
    "SQL_SELECT_DIVIDEND_EXISTING",
    "SQL_SELECT_EXISTING",
    "SQL_SELECT_FACTORS",
    "SQL_SELECT_FACTOR_EXISTING",
    "SQL_SELECT_SYMBOLS",
    "SQL_UPSERT_BAR",
    "SQL_UPSERT_DIVIDEND",
    "SQL_UPSERT_FACTOR",
    "IngestReport",
    "PgBarIngestor",
    "PgBarStore",
    "PgDividendIngestor",
    "PgDividendStore",
    "PgFactorIngestor",
    "PgFactorStore",
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

#: `dc_adjust_factor` 被本模块读写的列（顺序 = `INSERT` 的列序 = `%s` 的绑定顺序）。
#: 比 `BAR_COLUMNS` 少的是 OHLC/成交量/成交额，多的是那一个因子列。
FACTOR_COLUMNS = (
    "symbol",
    "trade_date",
    "adjust_factor",
    "source",
    "data_version",
)

# 因子表**没有** `VALUE_FIELDS` 的兄弟常量：它只有一个数值列，判等直接指向
# `adjust_factor`（见 `_raise_if_factor_divergent`）。一个元素的清单只会让人
# 以为它可以随便加 —— 而这里每加一列都要同时改 DDL、SQL 与标度。

#: `dc_dividend` 被本模块读写的列（顺序 = `INSERT` 的列序 = `%s` 的绑定顺序）。
#: 三日期模型（D4）里**可见性依据 ≠ 事件日**的那一张表：`ex_date` 是事件日（读窗口的键），
#: `announce_date` 是可见性依据（`DbDataFeed` 把它交给 `PITGuard`）。两列都在这里，见约定 7。
DIVIDEND_COLUMNS = (
    "symbol",
    "ex_date",
    "announce_date",
    "cash_per_share",
    "source",
    "data_version",
)

# 分红表同样**没有** `VALUE_FIELDS` 的兄弟常量，理由与因子表那段相同：判等的列是
# `announce_date` + `cash_per_share` 两列（见 `_raise_if_dividend_divergent`），写成常量
# 只会让人以为「再加一列判等」是改一个清单的事 —— 那要同时改 DDL、SQL 与这里。

#: `dc_daily_bar` 六个数值列在库里的标度（`numeric(18,4)` / `numeric(20,4)`，**都是 4**）。
#: 写入/判等前把值量化到这个标度，是 D10「同一批次重跑结果必须与跑一次相同」的前提：
#: 库会按 `numeric(_,4)` 四舍五入后再存，若本批带子标度的浮点尾巴，两侧就不是同一个数。
#: 改 `db/data_center.sql` 的标度必须同时改这里（`.rounds/_i2b3_db_roundtrip.py`
#: 会从 DDL 正则量出标度并与实际行为对拍，实测记录见契约附录 B18.3）。
_VALUE_SCALE = 4
_VALUE_QUANTUM = Decimal(1).scaleb(-_VALUE_SCALE)

#: `dc_adjust_factor.adjust_factor` 的库内标度（`numeric(18,8)`）—— **比日线那六列多 4 位**，
#: 所以另立一个量子，而不是复用 `_VALUE_QUANTUM`。
#: 拿 4 位量子去量 8 位的列，`1.00000005` 会被抹成 `1.0000`，而与库里存下的
#: `1.00000005` 永久不同值 ⇒ 每一轮重采都在第 1 行报 DATA_007，而数据其实一个字没变。
#: （这不是假设：`dc_daily_bar` 的 `amount` 就是同一种尾巴咬过一次，见附录 B18.3。）
_FACTOR_SCALE = 8
_FACTOR_QUANTUM = Decimal(1).scaleb(-_FACTOR_SCALE)

#: `dc_dividend.cash_per_share` 的库内标度（DDL 里是 `numeric(18,4)` —— 与日线价格同标度，
#: 与因子的 8 位**不同**）。为什么不复用一个 `_VALUE_QUANTUM` 了事：那是**两列各自的决定**
#: （日线六列 vs 分红一列），共用会让「改日线标度」顺手改掉分红，而 DDL 里两者是各写各的。
#: 标度传错的后果是静默的 —— 拿 8 位量子去量 4 位列，`1.23456000` 会与库里存的
#: `1.2346` 永久不等 ⇒ 每轮重采都在第 1 行报 DATA_007，而数据其实一个字没变（同 `_FACTOR_SCALE`）。
_DIVIDEND_SCALE = 4
_DIVIDEND_QUANTUM = Decimal(1).scaleb(-_DIVIDEND_SCALE)

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

#: 读一个标的在一个版本里的因子窗口（闭区间），按交易日升序。
SQL_SELECT_FACTORS = (
    "SELECT symbol, trade_date, adjust_factor, source, data_version "
    "FROM dc_adjust_factor "
    "WHERE symbol = %s AND data_version = %s "
    "AND trade_date >= %s AND trade_date <= %s "
    "ORDER BY trade_date"
)

#: 按主键取现有因子行 —— 「同值不写」与「异值报冲突」都靠它（只取判等用到的那一列）。
SQL_SELECT_FACTOR_EXISTING = (
    "SELECT adjust_factor FROM dc_adjust_factor "
    "WHERE symbol = %s AND trade_date = %s AND data_version = %s"
)

#: 与 `SQL_UPSERT_BAR` 同形（约定 1/6），只是表、列、判等字段不一样。
#: `IS DISTINCT FROM` 里库侧写成 `dc_adjust_factor.adjust_factor` 而不是裸列名：
#: 别名化会让 `RETURNING` 与 `WHERE` 指向的表在 SQL 文本上不可见，而这条语句
#: 的真实语义（"**库里的**因子与本批不同才更新"）必须能被人读出来。
SQL_UPSERT_FACTOR = (
    "INSERT INTO dc_adjust_factor "
    "(symbol, trade_date, adjust_factor, source, data_version) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (symbol, trade_date, data_version) DO UPDATE "
    "SET source = EXCLUDED.source, ingested_at = CURRENT_TIMESTAMP(3) "
    "WHERE dc_adjust_factor.adjust_factor IS DISTINCT FROM EXCLUDED.adjust_factor "
    "RETURNING symbol"
)

#: 读一个标的在一个版本里的分红窗口（闭区间，打在**除权除息日**上，见约定 7），
#: 按 `ex_date` 升序。与 `SQL_SELECT_FACTORS` 同形，换的是表与「窗口那一列」。
SQL_SELECT_DIVIDENDS = (
    "SELECT symbol, ex_date, announce_date, cash_per_share, source, data_version "
    "FROM dc_dividend "
    "WHERE symbol = %s AND data_version = %s "
    "AND ex_date >= %s AND ex_date <= %s "
    "ORDER BY ex_date"
)

#: 按主键取现有分红行 —— 「同值不写」与「异值报冲突」都靠它。
#: 只取**参与判等的那两列**（约定 3）：`ex_date`/`symbol`/`data_version` 在主键里，
#: `source` 是溯源信息不参与判等。`cash_per_share` 是数值列、`announce_date` 是日期列。
SQL_SELECT_DIVIDEND_EXISTING = (
    "SELECT announce_date, cash_per_share FROM dc_dividend "
    "WHERE symbol = %s AND ex_date = %s AND data_version = %s"
)

#: 与 `SQL_UPSERT_FACTOR` 同形（约定 1/6），表/列/判等字段换成分红。
#: `ON CONFLICT` 的主键列与 DDL 的 `pk_dc_dividend (symbol, ex_date, data_version)` 逐字对应；
#: `IS DISTINCT FROM` 里库侧写成 `dc_dividend.announce_date` 而不是裸列名，理由同因子那条
#: （「库里的值与本批不同才更新」必须能被人从 SQL 文本上读出来）。
#: ⚠️ 公告日在判等里**不是凑数**：改公告日（「公告日写错了」）恰好是最常见的一次重采，
#: 漏掉它会让新值被静默丢掉，库里留下一行与源不再一致的记录。
SQL_UPSERT_DIVIDEND = (
    "INSERT INTO dc_dividend "
    "(symbol, ex_date, announce_date, cash_per_share, source, data_version) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (symbol, ex_date, data_version) DO UPDATE "
    "SET source = EXCLUDED.source, ingested_at = CURRENT_TIMESTAMP(3) "
    "WHERE (dc_dividend.announce_date, dc_dividend.cash_per_share) IS DISTINCT FROM "
    "(EXCLUDED.announce_date, EXCLUDED.cash_per_share) "
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


def _to_decimal(value, quantum: Decimal, scale: int, what: str) -> Decimal:
    """把一列数值量化到**该列在库里的标度** —— 日线六列与因子一列共用这一处。

    用 `Decimal(str(value))` 而不是 `Decimal(value)`：`Decimal(0.1)` 会把二进制浮点的
    误差原样带进来（`0.1000000000000000055511151231257827`），于是「同一个 0.1」在
    两条路径上判不相等 —— 判等和 CHECK 约束都会跟着出错。`bool` 先挡掉，因为它是
    `int` 的子类，会被 `str()` 变成 `'True'`。

    `quantum` / `scale` / `what` 由调用方给：标度是**每一列**的属性（DDL 决定），不是
    全局常数 —— `dc_daily_bar` 的六列是 `numeric(_,4)`，`dc_adjust_factor.adjust_factor`
    是 `numeric(18,8)`。做成两个各自完整的函数等于把这段逻辑放两份；做成一个带参数的本函数，
    则要求调用点每次说清自己在量哪一列（`what` 会进错误信息，传错也能被看见）。

    末尾的 `quantize` 是 2026-09-24 补的，依据是 D10 幂等：库里这些列都是 `numeric`，
    PostgreSQL 入库时会四舍五入到该列标度；若本批带更细的浮点尾巴（源侧 2 位小数的「万元」
    × 10000 走 float64 就会，实测 `842270399.9999999`，数学上是 `842270400`），
    就会与库内的 `842270400.0000` 判成「异值」⇒ **同一批次连跑两次**抛 DATA_007，
    D10 的「重跑不产生重复」当场破裂。量化后两侧都等于库真正会存下的那个数，判等才有意义。

    量化只放在这一个函数里 ⇒ 读、写、比**三条路径同时收敛**（读侧本来就是该列标度，
    量化为恒等），而且 `INSERT` 绑定的参数也一并带标度，库端 `IS DISTINCT FROM`
    那道闸同样判成「同值」。实测见契约附录 B18.3（日线）与 B22（因子，标度 8）。
    """
    if isinstance(value, bool) or value is None:
        raise DataStoreError("%s列收到 %r，无法转成 Decimal" % (what, value))
    try:
        if isinstance(value, Decimal):
            number = value
        else:
            number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise DataStoreError("%s列收到 %r，无法转成 Decimal" % (what, value)) from exc
    try:
        # `ROUND_HALF_UP` 在 `Decimal` 里的定义是「半数远离零」，与 PostgreSQL `numeric`
        # 的四舍五入同义 —— 用错模式（如默认的 `ROUND_HALF_EVEN`）会让两侧在 `.00005`
        # 这类值上悄悄分家。
        return number.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise DataStoreError(
            "%s列 %r 量化到 %d 位小数失败（超出 decimal 上下文精度）" % (what, value, scale)
        ) from exc


def _as_decimal(value) -> Decimal:
    """`dc_daily_bar` 的六个数值列 → `Decimal`，标度 `_VALUE_SCALE`（4 位）。"""
    return _to_decimal(value, _VALUE_QUANTUM, _VALUE_SCALE, "数值")


def _as_factor_decimal(value) -> Decimal:
    """`dc_adjust_factor.adjust_factor` → `Decimal`，标度 `_FACTOR_SCALE`（8 位）。

    单独立一个名字而不是让调用点自己传量子：因子列的标度与日线不同，而**传错的后果
    是静默的**（见 `_FACTOR_SCALE` 那段）。名字里带 `factor`，写侧读侧一眼能对上 DDL。
    """
    return _to_decimal(value, _FACTOR_QUANTUM, _FACTOR_SCALE, "复权因子")


def _as_dividend_decimal(value) -> Decimal:
    """`dc_dividend.cash_per_share` → `Decimal`，标度 `_DIVIDEND_SCALE`（4 位）。

    与 `_as_factor_decimal` 同样的理由单独立名：**标度传错的后果是静默的**，而分红列的
    标度（`numeric(18,4)`）与因子的 8 位不同、与日线价格的 4 位**碰巧**相同。「碰巧相同」
    正是最不该靠复用表达的东西：DDL 改了日线那一列时，分红这一列会不会跟着改是一个
    **决定**，而共用一个常量会让这个决定消失。
    """
    return _to_decimal(value, _DIVIDEND_QUANTUM, _DIVIDEND_SCALE, "每股派息")


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


# ── 复权因子：与日线同一套判据，但**标度不同**（`numeric(18,8)`）─────────────

def _row_to_factor(row: Mapping[str, Any]) -> AdjustFactorPoint:
    """因子行 → dataclass。与 `_row_to_bar` 同形状：缺列 ⇒ DATA_008，不静默补默认值。"""
    try:
        return AdjustFactorPoint(
            symbol=str(row["symbol"]),
            trade_date=_as_date_value(row["trade_date"]),
            adjust_factor=_as_factor_decimal(row["adjust_factor"]),
            source="" if row["source"] is None else str(row["source"]),
            data_version=str(row["data_version"]),
        )
    except KeyError as exc:
        raise DataStoreError(
            "按主键取回的因子行缺少列 %s —— 读的列清单与 db/data_center.sql 不一致" % (exc,)
        ) from exc


def _factor_insert_params(point: AdjustFactorPoint) -> tuple:
    """按 `FACTOR_COLUMNS` 的顺序绑定 —— 列序与值序必须同源。"""
    return (
        point.symbol,
        _as_date_value(point.trade_date),
        _as_factor_decimal(point.adjust_factor),
        "" if point.source is None else str(point.source),
        _require_version(
            point.data_version, "因子行 %s/%s" % (point.symbol, point.trade_date)
        ),
    )


def _factor_primary_key(point: AdjustFactorPoint) -> tuple:
    return (
        point.symbol,
        _as_date_value(point.trade_date),
        _require_version(
            point.data_version, "因子行 %s/%s" % (point.symbol, point.trade_date)
        ),
    )


def _raise_if_factor_divergent(
    point: AdjustFactorPoint, row: Mapping[str, Any], concurrent: bool = False
) -> None:
    """同主键异因子 ⇒ DATA_007（约定 3 / D8）。**只有一列参与判等**（见 `FACTOR_COLUMNS`）。

    不写成「六列判等」的泛化版：因子表就一列，而多出来的循环只会把「到底比了什么」
    推到一个常量里，读的人还得回头查 `FACTOR_COLUMNS` 才知道判等是不是真的发生了。
    """
    mine = _as_factor_decimal(point.adjust_factor)
    try:
        theirs = _as_factor_decimal(row["adjust_factor"])
    except KeyError as exc:
        raise DataStoreError(
            "按主键取回的因子行缺少列 %s —— 列清单与 db/data_center.sql 不一致" % (exc,)
        ) from exc
    if mine == theirs:
        return
    symbol, trade_date, data_version = _factor_primary_key(point)
    raise IngestConflictError(
        "主键 (symbol=%s, trade_date=%s, data_version=%s) 已存在且复权因子不同："
        "库内 %s / 本批 %s%s —— "
        "D8：同一版本的行不可变，DATA_007 提示可能并发跑了两份采集。"
        "要改写历史请换一个新的 data_version 重采，不要用 upsert 把冲突盖掉。"
        % (symbol, trade_date, data_version, theirs, mine,
           "（并发写入后复查发现）" if concurrent else "")
    )


# ── 分红：两日期模型（D4），判等两列、窗口打在事件日上（约定 7）─────────────────

def _row_to_dividend(row: Mapping[str, Any]) -> DividendPoint:
    """分红行 → dataclass。与 `_row_to_factor` 同形状：缺列 ⇒ DATA_008，不静默补默认值。

    `announce_date` **原样**转 `date`，不做「其后首个交易日」之类的推导：本表没有
    `available_date` 列（D4：`announce_date` 本身就是可见性依据），而「公告日是不是交易日」
    是数据质量的问题，不是这一层该猜的 —— 猜了就会与库里存的那一列分家，
    下游的 `record_access` 也就跟着变成另一条时间线。
    """
    try:
        return DividendPoint(
            symbol=str(row["symbol"]),
            ex_date=_as_date_value(row["ex_date"]),
            announce_date=_as_date_value(row["announce_date"]),
            cash_per_share=_as_dividend_decimal(row["cash_per_share"]),
            source="" if row["source"] is None else str(row["source"]),
            data_version=str(row["data_version"]),
        )
    except KeyError as exc:
        raise DataStoreError(
            "按主键取回的分红行缺少列 %s —— 读的列清单与 db/data_center.sql 不一致" % (exc,)
        ) from exc


def _dividend_insert_params(point: DividendPoint) -> tuple:
    """按 `DIVIDEND_COLUMNS` 的顺序绑定 —— 列序与值序必须同源（同 `_insert_params`）。"""
    return (
        point.symbol,
        _as_date_value(point.ex_date),
        _as_date_value(point.announce_date),
        _as_dividend_decimal(point.cash_per_share),
        "" if point.source is None else str(point.source),
        _require_version(
            point.data_version, "分红行 %s/%s" % (point.symbol, point.ex_date)
        ),
    )


def _dividend_primary_key(point: DividendPoint) -> tuple:
    """主键 = `pk_dc_dividend (symbol, ex_date, data_version)` —— **事件日**进键，不是公告日。

    与 DDL 逐字对应：同一标的、同一除权除息日、同一版本只有一行。用公告日当键的一局部
    会让「同一除权日的两个公告」变成两行（而现金只能到账一次）。
    """
    return (
        point.symbol,
        _as_date_value(point.ex_date),
        _require_version(
            point.data_version, "分红行 %s/%s" % (point.symbol, point.ex_date)
        ),
    )


def _raise_if_dividend_divergent(
    point: DividendPoint, row: Mapping[str, Any], concurrent: bool = False
) -> None:
    """同主键异值 ⇒ DATA_007（约定 3 / D8）。**两列参与判等** —— 见 `DIVIDEND_COLUMNS`。

    `source` 不参与（与日线/因子同一条理由）；`ex_date`/`symbol`/`data_version` 在主键里。
    公告日参与判等是本表与因子表在判等形状上的**唯一**区别，理由写在 `SQL_UPSERT_DIVIDEND` 上。
    """
    mine_date = _as_date_value(point.announce_date)
    mine_cash = _as_dividend_decimal(point.cash_per_share)
    try:
        theirs_date = _as_date_value(row["announce_date"])
        theirs_cash = _as_dividend_decimal(row["cash_per_share"])
    except KeyError as exc:
        raise DataStoreError(
            "按主键取回的分红行缺少列 %s —— 列清单与 db/data_center.sql 不一致" % (exc,)
        ) from exc
    if mine_date == theirs_date and mine_cash == theirs_cash:
        return
    symbol, ex_date, data_version = _dividend_primary_key(point)
    raise IngestConflictError(
        "主键 (symbol=%s, ex_date=%s, data_version=%s) 已存在且值不同："
        "库内 announce_date=%s / cash_per_share=%s，本批 %s / %s%s —— "
        "D8：同一版本的行不可变，DATA_007 提示可能并发跑了两份采集。"
        "要改写历史请换一个新的 data_version 重采，不要用 upsert 把冲突盖掉。"
        % (symbol, ex_date, data_version, theirs_date, theirs_cash, mine_date, mine_cash,
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


class PgFactorStore(FactorStore):
    """PostgreSQL 支撑的 `FactorStore`：按窗口 + 版本读复权因子。

    与 `PgBarStore` 一样**必须绑 `data_version`**（约定 2 / D8）：不绑的话同一
    `(symbol, trade_date)` 会按版本数重复出现，而调用方（`DbDataFeed.get_adjustment_factor`）
    取的是 `rows[0]` —— 它拿到的是**哪一个版本**就变成执行顺序的函数。
    """

    def __init__(self, conn: SqlConnection, data_version: str):
        self.conn = conn
        self.data_version = _require_version(data_version, "PgFactorStore")

    def select_factors(
        self, symbol: str, start: date, end: date
    ) -> List[AdjustFactorPoint]:
        rows = _run(
            self.conn,
            SQL_SELECT_FACTORS,
            (symbol, self.data_version, _as_date_value(start), _as_date_value(end)),
            "读复权因子窗口",
        )
        return [_row_to_factor(row) for row in rows]


class PgFactorIngestor:
    """按 D10 幂等写复权因子：同值不写、异值抛 `IngestConflictError`。

    形状与 `PgBarIngestor` 逐条对齐（先过形状、整批一个事务、`_write_one` 返回布尔、
    `_run` 收口异常），但**刻意不抽成一个「通用 upsert 引擎」**：两张表的判等列集合
    不同（六列 vs 一列）、标度不同（4 vs 8）、主键的语义也可能分家（因子将来可能按
    源再分）。抽早了会把两边都不需要的约束焊死；真正该共用的部分（`_to_decimal`、
    `_run`、`_require_version`）已经共用了。
    """

    def __init__(self, conn: SqlConnection):
        self.conn = conn

    def upsert_adjust_factors(
        self, rows: Sequence[AdjustFactorPoint]
    ) -> IngestReport:
        points = list(rows)
        # 与日线同序：先一次过掉形状问题（不写一行、不开事务），再整批一个事务。
        for point in points:
            _require_version(
                point.data_version, "因子行 %s/%s" % (point.symbol, point.trade_date)
            )
        inserted = 0
        skipped = 0
        with self.conn.transaction():
            for point in points:
                if self._write_one(point):
                    inserted += 1
                else:
                    skipped += 1
        return IngestReport(inserted=inserted, skipped=skipped)

    def _write_one(self, point: AdjustFactorPoint) -> bool:
        """返回 True 表示这一行是新写入的，False 表示本来就在库里且值相同。"""
        key = _factor_primary_key(point)
        existing = _run(self.conn, SQL_SELECT_FACTOR_EXISTING, key, "按主键查现有因子行")
        if existing:
            _raise_if_factor_divergent(point, existing[0])
            return False
        written = _run(
            self.conn, SQL_UPSERT_FACTOR, _factor_insert_params(point), "写入复权因子"
        )
        if written:
            return True
        # 与日线同一条推理（见 `PgBarIngestor._write_one`）：
        # upsert 没返回行 ⇒ 只能是并发写者在这一瞬间插进去的同一主键。
        again = _run(self.conn, SQL_SELECT_FACTOR_EXISTING, key, "并发写入后复查（因子）")
        if not again:
            raise DataStoreError(
                "主键 %s 的因子 upsert 既没返回行、复查也查不到 —— 驱动/事务语义与预期不符，"
                "不能当作成功" % (key,)
            )
        _raise_if_factor_divergent(point, again[0], concurrent=True)
        return False


class PgDividendStore(DividendStore):
    """PostgreSQL 支撑的 `DividendStore`：按**除权除息日**窗口 + 版本读分红（约定 7）。

    与 `PgBarStore` / `PgFactorStore` 一样**必须绑 `data_version`**（约定 2 / D8）：不绑的话
    同一 `(symbol, ex_date)` 会按版本数重复出现，而 `DbDataFeed.get_dividend` 取的是
    `rows[0]` —— 它拿到的是**哪一个版本**就成了执行顺序的函数（更糟：两个版本的值会被
    当成两笔现金，而报告里分不出来）。

    ⚠️ 窗口打在 `ex_date` 上而**不是** `announce_date` 上，两条对不上的口径别抄错：
    「哪几天发生了事件」由本方法回答；「这条当时公不公开」由 `PITGuard` 回答
    （`DbDataFeed._select_dividends` 把每行的 `announce_date` 交给它）。
    """

    def __init__(self, conn: SqlConnection, data_version: str):
        self.conn = conn
        self.data_version = _require_version(data_version, "PgDividendStore")

    def select_dividends(self, symbol: str, start: date, end: date) -> List[DividendPoint]:
        rows = _run(
            self.conn,
            SQL_SELECT_DIVIDENDS,
            (symbol, self.data_version, _as_date_value(start), _as_date_value(end)),
            "读分红窗口",
        )
        return [_row_to_dividend(row) for row in rows]


class PgDividendIngestor:
    """按 D10 幂等写分红：同值不写、异值抛 `IngestConflictError`。

    形状与 `PgBarIngestor` / `PgFactorIngestor` 逐条对齐（先过形状、整批一个事务、
    `_write_one` 返回布尔、`_run` 收口异常），同样**刻意不抽成「通用 upsert 引擎」**：
    三张表的判等列集合不同（六列 / 一列 / 两列含一列日期）、标度不同（4 / 8 / 4）、
    主键的语义也不一样（分红的主键里是**事件日**）。真正该共用的部分
    （`_to_decimal` / `_run` / `_require_version`）已经共用了。

    ⚠️ 本类**不**自己判 `announce_date <= ex_date`（D4 那条规则）：那是
    `ck_dc_dividend_announce_not_after_ex` 的职责，库会以 CHECK 拒绝；在这里再判一次
    会让「谁拒的」变模糊，而库拒绝的判据还要在 `quanauto/ingest.py` 里被翻译成我们的错误码。
    """

    def __init__(self, conn: SqlConnection):
        self.conn = conn

    def upsert_dividends(self, rows: Sequence[DividendPoint]) -> IngestReport:
        points = list(rows)
        # 与另两张表同序：先一次过掉形状问题（不写一行、不开事务），再整批一个事务。
        for point in points:
            _require_version(
                point.data_version, "分红行 %s/%s" % (point.symbol, point.ex_date)
            )
        inserted = 0
        skipped = 0
        with self.conn.transaction():
            for point in points:
                if self._write_one(point):
                    inserted += 1
                else:
                    skipped += 1
        return IngestReport(inserted=inserted, skipped=skipped)

    def _write_one(self, point: DividendPoint) -> bool:
        """返回 True 表示这一行是新写入的，False 表示本来就在库里且值相同。"""
        key = _dividend_primary_key(point)
        existing = _run(self.conn, SQL_SELECT_DIVIDEND_EXISTING, key, "按主键查现有分红行")
        if existing:
            _raise_if_dividend_divergent(point, existing[0])
            return False
        written = _run(
            self.conn, SQL_UPSERT_DIVIDEND, _dividend_insert_params(point), "写入分红"
        )
        if written:
            return True
        # 与另两张表同一条推理（见 `PgBarIngestor._write_one`）：
        # upsert 没返回行 ⇒ 只能是并发写者在这一瞬间插进去的同一主键。
        again = _run(self.conn, SQL_SELECT_DIVIDEND_EXISTING, key, "并发写入后复查（分红）")
        if not again:
            raise DataStoreError(
                "主键 %s 的分红 upsert 既没返回行、复查也查不到 —— 驱动/事务语义与预期不符，"
                "不能当作成功" % (key,)
            )
        _raise_if_dividend_divergent(point, again[0], concurrent=True)
        return False


def _import_psycopg():
    """惰性 import 驱动（约定 4）。`import psycopg` 必须在函数体里。"""
    try:
        import psycopg  # noqa: PLC0415 —— 惰性是本函数的全部意义
    except ImportError as exc:
        raise InvalidConfigError(
            "落库需要 psycopg（psycopg 3）：pip install 'psycopg>=3.1'，"
            "或装本项目时带上 extra：pip install -e \".[postgres]\"。"
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
            #
            # `autocommit=True`：这不是图省事，它是本类正确性的前提，理由已实测。
            # psycopg 3.3.6 `_connection_base.py::_start_query`：
            #
            #     if self._autocommit: return
            #     if self.pgconn.transaction_status != IDLE: return
            #     yield from self._exec_command(self._get_tx_start_command())  # ⇒ b"BEGIN"
            #
            # 即默认的 `autocommit=False` 下，**任何一条裸 `execute()` 都会先显式发 `BEGIN`**
            # 并把连接永久钉在 INTRANS；再叠加 `transaction.py::_push_savepoint` 的
            #
            #     self._outer_transaction = self.pgconn.transaction_status == IDLE
            #
            # ⇒ 之前只要裸读过一次，之后的 `transaction()` 就退化成
            # `SAVEPOINT _pg3_1` / `RELEASE _pg3_1`，**永不 COMMIT**，`close()` 时整批被
            # 服务器回滚。这就是 B20：「先看看库里有什么、再决定写什么」这种最常见的用法
            # 会**静默丢数据**，而同一条连接上「写完读回对拍」恒真、全绿。
            #
            # `autocommit=True` 下 `Connection.transaction()` **仍然**是显式的 BEGIN…COMMIT
            # （见 `transaction()` 的文档串），所以「整批一个事务」的语义不变；
            # 变的只是「事务之外的一条语句立即生效」。
            self._conn = psycopg.connect(
                self.dsn, row_factory=psycopg.rows.dict_row, autocommit=True
            )
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[Mapping[str, Any]]:
        try:
            cursor = self._connect().execute(sql, params)
            # 无结果集的语句（不带 `RETURNING` 的 INSERT/DELETE、任何 DDL）在这里
            # `description` 是 `None`。实测 psycopg 3.3.6（`_cursor_base.py`）：
            # `Cursor.description` 无结果集时**返回 `None`**（不抛），而 `fetchall()`
            # 会**抛** `ProgrammingError: the last operation didn't produce records`
            # ⇒ 无条件 `fetchall()` 会把这类语句全变成 `DATA_008`，看起来像「驱动坏了」。
            # 判据取 `description`（DBAPI 标准，且实测语义就是「返回 None」），
            # 不去猜 SQL 文本 —— 猜文本会把判据绑死在调用方的写法上。
            if cursor.description is None:
                return []
            return list(cursor.fetchall())
        except QuanAutoError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 与 `_run` 同源，收口在出口
            raise DataStoreError("执行 SQL 失败（%s）：%s" % (type(exc).__name__, exc)) from exc

    @contextmanager
    def transaction(self) -> Iterator["PsycopgConnection"]:
        """psycopg 3 的事务语义：正常退出提交，异常退出回滚。

        ⚠️ 这里**必须**是 `conn.transaction()`，**绝不能**写成 `with conn:`。
        实测（psycopg 3.3.6，`psycopg/connection.py` 的 `Connection.__exit__`）：

            if exc_type: self.rollback()
            else:        self.commit()
            # Close the connection only if it doesn't belong to a pool.
            if not getattr(self, "_pool", None):
                self.close()

        `psycopg.connect()` 不带 pool ⇒ 出块时**连连接一起关掉**。而本类是一个可以反复用的
        连接对象（`_connect()` 里有 `if self._conn is None` 的复用），于是「先入库、再读回」
        这种最自然的用法会在第二次调用上炸成 `DATA_008 ... the connection is closed`。
        `Connection.transaction()` 才是「在既有连接上开一个事务」的 API：提交/回滚，不关连接。

        这一条曾经是错的，而**离线测试全绿**：测试里的假连接把 `__exit__` 写成「只记
        commit/rollback，不关连接」—— 那是照**作者的假设**写的，不是照驱动实测写的。
        抓到它的是容器通道那条探针（契约附录 B18）。教训与 B16.1/B17.1 同源：
        **对驱动行为的假设属于外部事实，只能测，不能想**；假连接必须照实测重写，
        否则它证明的只是「实现等于它自己」。

        另有一条与 `autocommit=True` 配对、同样实测过的语义（psycopg 3.3.6
        `transaction.py::_push_savepoint`）：

            self._outer_transaction = self.pgconn.transaction_status == IDLE

        而 `_get_commit_commands()` 只在 `_outer_transaction` 为真时才 `yield b"COMMIT"`。
        也就是说「这个事务块会不会真提交」取决于**进入那一刻连接的 libpq 状态**，而不是
        「我们调了 `transaction()`」。连接若已被前一条裸 `execute()` 钉在 INTRANS（默认
        `autocommit=False` 下必然如此），这里就退化成 `SAVEPOINT _pg3_1` / `RELEASE _pg3_1`
        —— **一条 COMMIT 都没有**，`close()` 时整段被回滚。这就是 B20：真库上「520 行入库、
        同连接读回 520 行、幂等重跑 `skipped=520`」全绿，另起一个客户端（`psql`）查出 0 行。
        `_connect()` 里的 `autocommit=True` 是它的修法，两处必须一起读。
        """
        conn = self._connect()
        with conn.transaction():
            yield self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
