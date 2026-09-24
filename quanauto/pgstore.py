"""数据中心落库侧（I2 S3）—— `dc_daily_bar` 的读写实现。

⚠️ **本文件此刻处于 TDD 的红步骤**：公开面（类名 / 方法名 / SQL 常量名 / 列清单）
先定死，行为**故意留空**，让 `tests/test_data_center_store.py` 先在**断言**上失败
（不是 `ImportError`、不是 `AttributeError`、不是语法错）。实现由紧随其后的那一次
提交补齐 —— 红在先、绿在后，两次都在 git 里，可用
`git log --oneline -- quanauto/pgstore.py` 复核。

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
合起来只剩一种自洽的读法：**值相同 ⇒ 一个字节都不写**，**值不同 ⇒ 抛
`IngestConflictError`**（D8：版本不可变）。见 `PgBarIngestor.upsert_daily_bars`。

## 约定 4：驱动是注入进来的，且只在用到时才 import

`tools/` 与 CI 环境都没有 psycopg。驱动必须满足两条：**导入本模块不拉驱动**、
**没装驱动时报的是我们的异常而不是 `ImportError`**。所以真实驱动包在
`PsycopgConnection` 里惰性 import，异常映射成 `InvalidConfigError`（部署问题）
或 `DataStoreError`（库操作失败），不裸逃。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterator, List, Mapping, Protocol, Sequence, runtime_checkable

from .datacenter import BarStore, DailyBar
from .errors import DataVersionError, InvalidConfigError  # noqa: F401  (红步骤：实现里会用到)

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

#: 参与「同值/异值」判定的字段。`source` / `ingested_at` 是溯源信息，不是行情内容，
#: 不参与判等（备源复核过同一行是正常事件，不该被当成数据变更，更不该为此反复写库）。
VALUE_FIELDS = ("open", "high", "low", "close", "volume", "amount")

# ── SQL：红步骤里全是占位符文本，让形状断言先红 ──────────────────────────
SQL_SELECT_BARS = "-- TDD 红步骤：读窗口 SQL 尚未写出"
SQL_SELECT_SYMBOLS = "-- TDD 红步骤：标的清单 SQL 尚未写出"
SQL_SELECT_EXISTING = "-- TDD 红步骤：按主键取现有行的 SQL 尚未写出"
SQL_UPSERT_BAR = "-- TDD 红步骤：upsert SQL 尚未写出"


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


class PgBarStore(BarStore):
    """PostgreSQL 支撑的 `BarStore`：按窗口 + 版本读日线。"""

    def __init__(self, conn: SqlConnection, data_version: str):
        # 红步骤：只记下来，不判断、不执行。
        self.conn = conn
        self.data_version = data_version

    def select_bars(self, symbol: str, start: date, end: date) -> List[DailyBar]:
        # TDD 红步骤：故意返回空 —— 让用例在**断言**上失败。
        return []

    def select_symbols(self) -> List[str]:
        # TDD 红步骤：故意返回空。
        return []


class PgBarIngestor:
    """按 D10 幂等写日线：同值不写、异值抛 `IngestConflictError`。"""

    def __init__(self, conn: SqlConnection):
        self.conn = conn

    def upsert_daily_bars(self, rows: Sequence[DailyBar]) -> IngestReport:
        # TDD 红步骤：故意什么都不做、什么都不记。
        return IngestReport()


class PsycopgConnection:
    """`SqlConnection` 的真实实现（psycopg 驱动，惰性 import）。"""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[Mapping[str, Any]]:
        # TDD 红步骤：这里抛的是**普通** `RuntimeError`，所以用例里那条
        # `pytest.raises(InvalidConfigError)` 会以断言不匹配的方式失败 ——
        # 这是故意的：红步骤必须在断言上红，不能靠 NotImplementedError 蒙过去。
        raise RuntimeError("TDD 红步骤：驱动适配尚未实现")

    def transaction(self) -> Iterator["PsycopgConnection"]:
        raise RuntimeError("TDD 红步骤：事务上下文尚未实现")


# 红步骤里这些名字只为了「实现里会用到」的意图可见，避免实现时忘了它们的存在。
_UNUSED_IN_RED_STEP = (Decimal, DataVersionError, InvalidConfigError, Iterator, date)
