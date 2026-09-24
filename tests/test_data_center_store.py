"""I2 S3 —— 落库侧（`quanauto/pgstore.py`）的用例。

纪律（与 S1 / S2 两条同源）：

* **一律不连库**。所有 SQL 都通过注入的 `SqlConnection` 发出。假连接能证明「我发了哪些
  语句、绑了哪些参数」，**证明不了**「PostgreSQL 认不认这些语句」—— 后者的证据只能来自
  容器通道那份报告，两者的效力不同，不能互相冒充，也不能互相替代。
* 这里守的是**形状与分支**：SQL 是否参数化、约定 6 的 upsert 在不在、三个分支
  （插入 / 同值重跑 / 同主键异值）是否各走各路、驱动异常有没有被包装。

TDD：本文件与 `quanauto/pgstore.py` 的红步骤同批提交 —— 先有失败的断言，再有实现。
复现红状态：

```powershell
git log --oneline -- quanauto/pgstore.py   # 取红提交的 hash
git checkout <红提交> -- quanauto/pgstore.py
.venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_data_center_store.py -q
git checkout HEAD -- quanauto/pgstore.py
```
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal

import pytest

from quanauto.datacenter import DailyBar
from quanauto.errors import (
    DataStoreError,
    DataVersionError,
    IngestConflictError,
    InvalidConfigError,
)
from quanauto.pgstore import (
    BAR_COLUMNS,
    VALUE_FIELDS,
    IngestReport,
    PgBarIngestor,
    PgBarStore,
    PsycopgConnection,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYMBOL = "600000.SH"
VERSION = "v2026.01.05"
DAY = date(2026, 1, 5)


def _bar(symbol: str = SYMBOL, trade_date: date = DAY, close: str = "10.5",
         data_version: str = VERSION, source: str = "akshare") -> DailyBar:
    """一条标准日线。`close` 收字符串，方便造「同值但写法不同」（`10.5` vs `10.5000`）。"""
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open=Decimal("10"),
        high=Decimal("10.8"),
        low=Decimal("9.9"),
        close=Decimal(close),
        volume=Decimal("1000"),
        amount=Decimal("10500"),
        source=source,
        data_version=data_version,
    )


def _row(close: str = "10.5") -> dict:
    """库里的那一行 —— **故意全是字符串**。

    psycopg 会把 `date` / `numeric` 转成 Python 类型，但并不是所有通道都这样
    （文本协议、`COPY` 导出、psql 的一次性查询都会给字符串）。落库层若假定
    「库里出来的已经是 `Decimal`」，换成字符通道就会静默拿到字符串，
    而字符串比较 `'10.0000' == 10.5` 永远为假 —— 那是「冲突天天报」或「同值也写库」
    两种假结论的共同来源。所以这里坚持用字符串喂进去。
    """
    return {
        "symbol": SYMBOL,
        "trade_date": "2026-01-05",
        "open": "10.0000",
        "high": "10.8000",
        "low": "9.9000",
        "close": close,
        "volume": "1000.0000",
        "amount": "10500.0000",
        "source": "akshare",
        "data_version": VERSION,
    }


class FakeConn:
    """假连接：按脚本吐行，并记下每次 `(sql, params)` 与事务事件。

    `script` 的一项对应一次 `execute` 的返回值；脚本用尽后再来的 `execute` 返回空行集
    —— 不抛异常，因为「多查了一次」这件事由用例断言 `len(conn.calls)` 来抓，
    让它变成一条清晰的断言失败，而不是一条被包装进 `DataStoreError` 的怪异常。
    """

    def __init__(self, script=None, error=None):
        self.script = list(script or [])
        self.error = error
        self.calls = []
        self.events = []

    def execute(self, sql, params=()):
        if self.error is not None:
            raise self.error
        self.calls.append((sql, tuple(params)))
        return self.script.pop(0) if self.script else []

    @contextlib.contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield self
        except BaseException:
            self.events.append("rollback")
            raise
        self.events.append("commit")


def _insert_calls(conn) -> list:
    return [c for c in conn.calls if "INSERT" in c[0].upper()]


def _select_calls(conn) -> list:
    return [c for c in conn.calls if c[0].upper().lstrip().startswith("SELECT")]


# ── 读侧 ─────────────────────────────────────────────────────────────────
def test_select_bars_sql_is_parameterized_not_interpolated():
    """标的、版本、窗口端点一共四个值，一个都不许出现在 SQL 文本里。"""
    trap = "600000.SH' OR '1'='1"
    conn = FakeConn()
    PgBarStore(conn, VERSION).select_bars(trap, date(2026, 1, 1), date(2026, 1, 31))
    assert len(conn.calls) == 1, conn.calls
    sql, params = conn.calls[0]
    assert trap not in sql, "标的被拼进了 SQL 文本 —— 注入与「过滤条件悄悄消失」都从这里开始"
    assert sql.count("%s") == 4, "四个绑定值各占一个占位符，实际 %d 个：%r" % (sql.count("%s"), sql)
    assert params == (trap, VERSION, date(2026, 1, 1), date(2026, 1, 31)), params


def test_select_bars_window_is_closed_and_ordered_in_sql():
    """窗口必须是闭区间（端点含当日），升序由 SQL 保证而不是 Python 再排一遍。"""
    conn = FakeConn()
    PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)
    sql, params = conn.calls[0]
    assert ">= %s" in sql and "<= %s" in sql, "窗口必须是闭区间，实际：%r" % sql
    assert "ORDER BY trade_date" in sql, "升序交给 SQL —— 换 store 时靠它，不靠调用方补排序"
    assert params[2] == params[3] == DAY, params


def test_select_bars_binds_the_data_version():
    """D8：多版本共存 ⇒ 不绑定版本地读，同一个 (symbol, trade_date) 会按版本数重复出现。"""
    conn = FakeConn()
    PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)
    sql, params = conn.calls[0]
    assert "data_version = %s" in sql, "读必须按版本过滤，否则回测看到的是叠影：%r" % sql
    assert VERSION in params, params


def test_store_requires_a_data_version():
    """必填位置参数：忘传是 TypeError，不是静默多读几行。"""
    with pytest.raises(TypeError):
        PgBarStore(FakeConn())  # type: ignore[call-arg]


def test_select_bars_converts_text_columns_to_daily_bar():
    conn = FakeConn(script=[[_row()]])
    bars = PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)
    assert len(bars) == 1, bars
    bar = bars[0]
    assert isinstance(bar, DailyBar)
    assert bar.trade_date == DAY, "文本日期必须归一成 date，否则 PIT 的日期比较会静默失效"
    assert isinstance(bar.close, Decimal) and bar.close == Decimal("10.5"), repr(bar.close)
    assert isinstance(bar.volume, Decimal), repr(bar.volume)
    assert bar.symbol == SYMBOL and bar.source == "akshare" and bar.data_version == VERSION


def test_select_bars_empty_result_is_an_empty_list():
    """控制样本：查不到就是空表，不是异常 —— 「没有数据」与「库出问题」必须分得开。"""
    conn = FakeConn(script=[[]])
    assert PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY) == []


def test_select_symbols_is_distinct_and_versioned():
    conn = FakeConn(script=[[{"symbol": "600000.SH"}, {"symbol": "000001.SZ"}]])
    assert PgBarStore(conn, VERSION).select_symbols() == ["600000.SH", "000001.SZ"]
    sql, params = conn.calls[0]
    assert "DISTINCT" in sql, "同一标的的多个版本会各占一行，必须去重：%r" % sql
    assert "data_version = %s" in sql, sql
    assert params == (VERSION,), params


# ── 写侧：SQL 形状 ───────────────────────────────────────────────────────
def test_upsert_sql_follows_contract_rule_6():
    conn = FakeConn(script=[[]])
    PgBarIngestor(conn).upsert_daily_bars([_bar()])
    ins = _insert_calls(conn)
    assert len(ins) == 1, [c[0] for c in conn.calls]
    sql = ins[0][0]
    assert "ON CONFLICT (symbol, trade_date, data_version)" in sql, \
        "约定 6：写入必须是按业务主键的 upsert（幂等，见 D10）：%r" % sql
    assert "DO UPDATE" in sql, sql
    assert ".sql" not in sql and "db/" not in sql, "写侧不读 DDL 文件，表结构由 db/data_center.sql 单点维护"


def test_upsert_binds_values_in_column_list_order():
    conn = FakeConn(script=[[]])
    PgBarIngestor(conn).upsert_daily_bars([_bar()])
    sql, params = _insert_calls(conn)[0]
    assert params == (
        SYMBOL, DAY, Decimal("10"), Decimal("10.8"), Decimal("9.9"), Decimal("10.5"),
        Decimal("1000"), Decimal("10500"), "akshare", VERSION,
    ), params
    assert sql.count("%s") == len(BAR_COLUMNS), \
        "绑定顺序必须与列清单一一对应，实际 %d 个占位符 vs %d 列" % (sql.count("%s"), len(BAR_COLUMNS))
    for column in BAR_COLUMNS:
        assert column in sql, "%s 不在 INSERT 的列清单里：%r" % (column, sql)


def test_upsert_looks_before_it_writes():
    """先按主键取现有行，再决定写不写 —— 顺序本身是判据（同值不写的依据就在这一次查询里）。"""
    conn = FakeConn(script=[[], [{"symbol": SYMBOL}]])
    PgBarIngestor(conn).upsert_daily_bars([_bar()])
    assert len(conn.calls) == 2, [c[0][:40] for c in conn.calls]
    assert _select_calls(conn), conn.calls
    assert "INSERT" in conn.calls[1][0].upper(), conn.calls


# ── 写侧：三个分支 ───────────────────────────────────────────────────────
def test_upsert_new_row_counts_as_inserted():
    conn = FakeConn(script=[[], [{"symbol": SYMBOL}]])
    report = PgBarIngestor(conn).upsert_daily_bars([_bar()])
    assert (report.inserted, report.skipped) == (1, 0), report
    assert conn.events == ["begin", "commit"], conn.events


def test_upsert_identical_rerun_writes_nothing():
    """D10 的最强读法：重跑 == 跑一次，连 `ingested_at` 都不动。"""
    conn = FakeConn(script=[[_row()]])
    report = PgBarIngestor(conn).upsert_daily_bars([_bar()])
    assert (report.inserted, report.skipped) == (0, 1), report
    assert conn.calls and not _insert_calls(conn), \
        "同值重跑不该产生任何写入，实际发了：%s" % [c[0][:40] for c in conn.calls]
    assert conn.events == ["begin", "commit"], conn.events


def test_upsert_identical_under_trailing_zeros_is_still_identical():
    """`10.5` 与 `10.5000` 是同一个数 —— 按数值判等，不按文本判等。"""
    conn = FakeConn(script=[[_row(close="10.5000")]])
    report = PgBarIngestor(conn).upsert_daily_bars([_bar(close="10.5")])
    assert (report.inserted, report.skipped) == (0, 1), report
    assert not _insert_calls(conn), conn.calls


def test_upsert_divergent_value_raises_ingest_conflict():
    conn = FakeConn(script=[[_row(close="10.0000")]])
    with pytest.raises(IngestConflictError) as caught:
        PgBarIngestor(conn).upsert_daily_bars([_bar(close="10.5")])
    msg = str(caught.value)
    assert SYMBOL in msg and VERSION in msg and "2026-01-05" in msg, msg
    assert "close" in msg, "冲突信息必须点名是哪个字段不同，否则排障要自己重算一遍：%r" % msg
    assert "10.0000" in msg and "10.5" in msg, "库内值与本批值都要给出来：%r" % msg
    assert conn.events == ["begin", "rollback"], conn.events
    assert not _insert_calls(conn), "判成冲突就不该再发 INSERT：%s" % [c[0][:40] for c in conn.calls]


def test_upsert_concurrent_identical_insert_is_skipped():
    """预查没看到行 ⇒ 发 INSERT，但 upsert 没返回行 ⇒ 有并发写者抢先插了同一主键。
    复查发现值相同 ⇒ 算跳过（幂等），不算冲突。"""
    conn = FakeConn(script=[[], [], [_row()]])
    report = PgBarIngestor(conn).upsert_daily_bars([_bar()])
    assert (report.inserted, report.skipped) == (0, 1), report
    assert conn.events == ["begin", "commit"], conn.events


def test_upsert_concurrent_divergent_insert_raises_ingest_conflict():
    """DATA_007 描述的场景正是「并发跑了两份采集」：两个写者都以为自己是第一个。"""
    conn = FakeConn(script=[[], [], [_row(close="10.0000")]])
    with pytest.raises(IngestConflictError):
        PgBarIngestor(conn).upsert_daily_bars([_bar(close="10.5")])
    assert conn.events == ["begin", "rollback"], conn.events


def test_upsert_stops_the_batch_at_the_conflict():
    """冲突之后不许继续写后面的行：半批落库比整批失败更难查（要对着 ingested_at 逐行找）。"""
    bars = [_bar(symbol="600000.SH"), _bar(symbol="000001.SZ", close="11"), _bar(symbol="600519.SH")]
    conn = FakeConn(script=[[], [{"symbol": "600000.SH"}], [_row(close="99")]])
    with pytest.raises(IngestConflictError) as caught:
        PgBarIngestor(conn).upsert_daily_bars(bars)
    assert "000001.SZ" in str(caught.value), "报错必须指向真正冲突的那一行：%s" % caught.value
    assert len(conn.calls) == 3, \
        "第三个标的不该被碰过，实际发了 %d 条语句：%s" % (len(conn.calls), [c[0][:30] for c in conn.calls])
    assert conn.events == ["begin", "rollback"], conn.events


def test_upsert_batch_mixes_branches_per_row():
    """一个批次里三条行各走各的分支：插入 / 跳过 / 插入 —— 计数不能串。"""
    bars = [_bar(symbol="600000.SH"), _bar(symbol="000001.SZ"), _bar(symbol="600519.SH")]
    conn = FakeConn(script=[[], [{"symbol": "600000.SH"}], [_row()], [], [{"symbol": "600519.SH"}]])
    report = PgBarIngestor(conn).upsert_daily_bars(bars)
    assert (report.inserted, report.skipped) == (2, 1), report
    assert report.total == len(bars), report
    assert conn.events == ["begin", "commit"], conn.events


def test_upsert_rejects_a_blank_data_version_before_any_sql():
    """D8：没有版本号的行不该「先写进去再说」—— 写进去了，它的版本就是空的，那段历史再也取不回来。"""
    conn = FakeConn()
    with pytest.raises(DataVersionError):
        PgBarIngestor(conn).upsert_daily_bars([_bar(data_version="   ")])
    assert conn.calls == [], conn.calls
    assert conn.events == [], conn.events


# ── 异常包装 ─────────────────────────────────────────────────────────────
def test_read_side_driver_error_is_wrapped_with_cause():
    boom = RuntimeError("connection reset by peer")
    conn = FakeConn(error=boom)
    with pytest.raises(DataStoreError) as caught:
        PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)
    assert caught.value.__cause__ is boom, "原始驱动异常必须挂在 __cause__ 上，否则排障线索断了"


def test_write_side_driver_error_is_wrapped_with_cause():
    boom = RuntimeError('new row violates check constraint "ck_dc_bar_ohlc_order"')
    conn = FakeConn(error=boom)
    with pytest.raises(DataStoreError) as caught:
        PgBarIngestor(conn).upsert_daily_bars([_bar()])
    assert caught.value.__cause__ is boom, caught.value.__cause__


def test_our_own_errors_are_not_double_wrapped():
    """冲突是自己的判据，不该被包装成 `DataStoreError` —— 否则调用方再也分不出
    「数据版本不可变」（要人处理）和「库挂了」（要重试）。"""
    conn = FakeConn(script=[[_row(close="10.0000")]])
    with pytest.raises(IngestConflictError):
        PgBarIngestor(conn).upsert_daily_bars([_bar(close="10.5")])


# ── 驱动惰性 ─────────────────────────────────────────────────────────────
def test_import_pgstore_does_not_import_the_driver():
    """子进程里真跑一次 `import`：CI 与本机都没装 psycopg，导入期拉驱动就全红。"""
    code = "import sys; import quanauto.pgstore; print('psycopg' in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", "导入期就把驱动拉进来了：%r" % proc.stdout


def test_missing_driver_fails_loudly_and_lazily(monkeypatch):
    """没装驱动时抛的是我们的异常，不是 `ImportError`；而且构造时不连库。"""
    monkeypatch.setitem(sys.modules, "psycopg", None)  # ⇒ `import psycopg` 必抛 ImportError
    conn = PsycopgConnection("postgresql://127.0.0.1:1/quanauto")  # 构造不许有副作用
    with pytest.raises(InvalidConfigError) as caught:
        conn.execute("SELECT 1")
    assert "psycopg" in str(caught.value), caught.value


def test_value_fields_are_the_six_numeric_columns():
    """判等字段清单是公开常量：溯源信息（source）不参与判等，这件事必须看得见。"""
    assert VALUE_FIELDS == ("open", "high", "low", "close", "volume", "amount")
    assert "source" in BAR_COLUMNS and "source" not in VALUE_FIELDS
    assert set(VALUE_FIELDS) < set(BAR_COLUMNS)
