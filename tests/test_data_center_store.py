"""I2 S3 —— 落库侧（`quanauto/pgstore.py`）的用例。

纪律（与 S1 / S2 两条同源）：

* **一律不连库**。所有 SQL 都通过注入的 `SqlConnection` 发出。假连接能证明「我发了哪些
  语句、绑了哪些参数」，**证明不了**「PostgreSQL 认不认这些语句」—— 后者的证据只能来自
  容器通道那份报告，两者的效力不同，不能互相冒充，也不能互相替代。
* 这里守的是**形状与分支**：SQL 是否参数化、约定 6 的 upsert 在不在、三个分支
  （插入 / 同值重跑 / 同主键异值）是否各走各路、驱动异常有没有被包装。

**2026-09-24 兑现**：上面那句「证据只能来自容器通道那份报告」不是修辞。报告（契约附录 B18）
真的来了，并且当场把本文件的一个假设判死：假驱动的 `__exit__` 原先「只记 commit/rollback、
不关连接」，是把作者的猜测当成了 psycopg 的行为；真驱动（3.3.6）在 `with conn:` 出块时
**提交/回滚之后还会关连接**。于是 `PsycopgConnection.transaction()` 里的 `with conn:` 让
「先入库、再读回」在真库上炸，而本文件全绿。假驱动现已照实测重写（关连接 + 关掉后再
`execute` 就抛），并另有一条用例专门钉住它的保真度。

**2026-09-25 兑现了同一句话的第二遍**：真库上「520 行入库、同连接读回 520 行、逐字段对拍
全过、幂等重跑 `skipped=520`」一片绿，而**另起一个客户端**（`psql`，python 之外的新连接）
查出 **0 行**。根因是 psycopg 的 `autocommit=False` 默认值：裸 `execute()` 会**显式发
`BEGIN`** 并把连接钉在 INTRANS，令之后的 `transaction()` 只发 savepoint、**永不 COMMIT**
⇒ 关连接时整批被服务器回滚。假驱动当时把「我们调了 `transaction()`」记成「提交了」，
于是又一次把「实现等于它自己」当成了证据。

因此本文件新增的两条用例断言的是**线上真的发生了什么**（`driver.tx` 里的 `COMMIT`），
而不是「我们调用了哪个方法」—— 后者在缺陷版本里同样成立。

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
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal

import pytest

from quanauto import pgstore
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
    SQL_SELECT_EXISTING,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYMBOL = "600000.SH"
VERSION = "v2026.01.05"
DAY = date(2026, 1, 5)


def _bar(symbol: str = SYMBOL, trade_date: date = DAY, close: str = "10.5",
         data_version: str = VERSION, source: str = "akshare",
         amount: str = "10500") -> DailyBar:
    """一条标准日线。`close`/`amount` 收字符串，方便造「同值但写法不同」（`10.5` vs `10.5000`）。"""
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open=Decimal("10"),
        high=Decimal("10.8"),
        low=Decimal("9.9"),
        close=Decimal(close),
        volume=Decimal("1000"),
        amount=Decimal(amount),
        source=source,
        data_version=data_version,
    )


def _row(close: str = "10.5", amount: str = "10500.0000") -> dict:
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
        "amount": amount,
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
    # 脚本要写全：`RETURNING` 得给一行才算「新插入」。空返回值在本实现里是「并发写者抢先」
    # 的信号（约定 6 第 2 步），不是「写成功」—— 省略它，形状用例会顺带变成分支用例。
    conn = FakeConn(script=[[], [{"symbol": SYMBOL}]])
    PgBarIngestor(conn).upsert_daily_bars([_bar()])
    ins = _insert_calls(conn)
    assert len(ins) == 1, [c[0] for c in conn.calls]
    sql = ins[0][0]
    assert "ON CONFLICT (symbol, trade_date, data_version)" in sql, \
        "约定 6：写入必须是按业务主键的 upsert（幂等，见 D10）：%r" % sql
    assert "DO UPDATE" in sql, sql
    assert ".sql" not in sql and "db/" not in sql, "写侧不读 DDL 文件，表结构由 db/data_center.sql 单点维护"


def test_upsert_binds_values_in_column_list_order():
    conn = FakeConn(script=[[], [{"symbol": SYMBOL}]])
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


def test_upsert_ignores_a_float_tail_below_the_table_scale():
    """D10 在**浮点尾巴**面前必须成立：库内 `842270400.0000` 与本批 `842270399.9999999` 是同一个值。

    尾巴不是笔误，是float64 算必然的产物：源侧「万元」保留 2 位小数，乘 10000 就落在
    整数下方一点点（实测，见契约附录 B16.7 / B18.3）。库里那列是 `numeric(20,4)`，
    PostgreSQL 写入时本就把它存成 `842270400.0000`；若判等前不量化到同一标度，就是拿两个
    不同的数去比 ⇒ 同一批次连跑两次抛 DATA_007，而 D10 要求的正是「重跑 == 跑一次」。

    这条用例就是真库上那次 DATA_007 的离线复现（样本编号 S12b）。
    """
    tail, stored = Decimal("842270399.9999999"), Decimal("842270400.0000")
    assert tail != stored, "前置：尾巴与库内值在 `Decimal` 上确实不等，否则本用例在空转"
    conn = FakeConn(script=[[_row(amount="842270400.0000")]])
    report = PgBarIngestor(conn).upsert_daily_bars([_bar(amount="842270399.9999999")])
    assert (report.inserted, report.skipped) == (0, 1), report
    assert not _insert_calls(conn), \
        "子标度的尾巴不算异值，不该发 INSERT：%s" % [c[0][:40] for c in conn.calls]
    assert conn.events == ["begin", "commit"], conn.events


def test_upsert_binds_values_already_quantized_to_the_table_scale():
    """绑给库的参数要**带标度**：库端 `IS DISTINCT FROM` 是拿字符串化的 numeric 比的。

    量化只放在 `_as_decimal` 一处，所以写侧参数顺带收敛；这条用例把「顺带」变成判据，
    免得日后有人把量化挪到读侧（那时写侧又会给库一个带尾巴的值，库端那道闸就判成异值）。
    """
    conn = FakeConn(script=[[], [{"symbol": SYMBOL}]])
    PgBarIngestor(conn).upsert_daily_bars([_bar(amount="842270399.9999999")])
    bound = _insert_calls(conn)[0][1][BAR_COLUMNS.index("amount")]
    assert bound == Decimal("842270400.0000"), bound
    assert str(bound) == "842270400.0000", "绑定的值必须带库内标度：%r" % (str(bound),)


def test_upsert_still_calls_a_one_cent_change_a_conflict():
    """反向用例：量化**没有**把真差异一起抹掉。一分钱 = 1e-2 = 100 个量化单位。"""
    conn = FakeConn(script=[[_row(close="10.0000")]])
    with pytest.raises(IngestConflictError) as caught:
        PgBarIngestor(conn).upsert_daily_bars([_bar(close="10.01")])
    assert "close" in str(caught.value), str(caught.value)


def test_value_scale_matches_the_ddl():
    """`_VALUE_SCALE` 是 `db/data_center.sql` 标度的镜像 —— 抄错就得再破一次幂等。

    离线套件不连库，所以只能把 DDL 当文本读来对账；**提取为空必须判失败**（正则失配时
    一个刻度都比不到，却会打印「全等」）。
    """
    ddl = open(os.path.join(REPO_ROOT, "db", "data_center.sql"), encoding="utf-8").read()
    table = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?dc_daily_bar\b(.*?);",
                      ddl, re.S | re.I)
    assert table, "没在 DDL 里找到 dc_daily_bar 的 CREATE TABLE —— 提取为空，本用例形同虚设"
    scales = {m.group(1).lower(): int(m.group(2)) for m in re.finditer(
        r"\b(open|high|low|close|volume|amount)\s+numeric\(\s*\d+\s*,\s*(\d+)\s*\)",
        table.group(1), re.I)}
    assert set(scales) == set(VALUE_FIELDS), \
        "DDL 里六个数值列的标度没全取到，取到的是 %r" % (scales,)
    assert set(scales.values()) == {pgstore._VALUE_SCALE}, \
        "DDL 标度 %r 与 `_VALUE_SCALE=%d` 不一致" % (scales, pgstore._VALUE_SCALE)


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


def test_already_classified_error_from_a_lower_layer_passes_through():
    """下层已经用我们的异常类报过的错，不许在出口再包一层。

    这条是**变异测试发现的缺口**：`_raise_if_divergent` 是在 `_run` **外面**抛的，
    所以上面那条用例根本没经过 `_run` 的 `except QuanAutoError: raise` 守卫 ——
    把守卫删掉套件依然 28 passed（实测）。这条用例便是那个变异（`S8-our-own-errors-get-wrapped`）
    的主人：删了它，`tools/pytest_mutation_check.py` 里的 S8 会立刻变成 CAUGHT=no。
    这里直接让下层抛我们的异常，才真正压住那条守卫。
    """
    boom = DataStoreError("下层已经判过：连接断了")
    conn = FakeConn(error=boom)
    with pytest.raises(DataStoreError) as caught:
        PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)
    assert caught.value is boom, \
        "本项目自己的异常必须原样穿过，不许套成 DataStoreError(DataStoreError)：%r" % (caught.value,)


# ── 驱动惰性 ─────────────────────────────────────────────────────────────
def test_import_pgstore_does_not_import_the_driver():
    """子进程里真跑一次 `import`：两边都要求「导入期不拉驱动」。

    环境事实（**会变，别照抄**）：本机 `.venv` 自 2026-09-25 起**装着** psycopg（手工装，
    `psycopg[binary] 3.3.6`；`pyproject.toml` 里**没有**声明它）⇒ 本机这条断言是**有牙的**
    （变异 `S9` 在本机真的 CAUGHT）。CI 只装 `.[dev]`（= pytest）⇒ 那边没装，这条退化成
    恒真。两边都跑同一条，所以「本机红」比「CI 绿」强。
    """
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


# ── 驱动适配层：装驱动的那条路径 ─────────────────────────────────────────
# 为什么用假驱动：「`PsycopgConnection` 究竟怎么调驱动」这条路径默认没人走 ——
# 它要**实连接**才走得到，而本套件一律不连库。用一个假驱动模块顶替 `sys.modules["psycopg"]`
# 就能把它走通，**且与「本机装没装 psycopg」无关**（2026-09-25 起本机 `.venv` 是装着的，
# CI 没装；这条注释原来写的是「本机与 CI 都没装」，已成假话）。
#
# 效力边界写清楚：这证明的是**我们怎么调驱动**（要字典行、要复用连接、要把事务上下文
# 原样传下去），**不是**「psycopg 会那样做」—— 后者只能来自容器通道那份报告。
# libpq 的 `TransactionStatus` 取值（照实测：裸 `execute()` 之后是 2/INTRANS，
# 一条语句都没跑的空闲连接是 0/IDLE）。`transaction.py::_push_savepoint` 就是拿它判
# `_outer_transaction` 的，所以假驱动必须维护这个数，否则「提交了没有」根本无从断言。
_IDLE = 0
_INTRANS = 2


class _DriverNoRecords(RuntimeError):
    """对没有结果集的语句调 `fetchall()` 会抛的东西 —— 照实测抄（psycopg 3.3.6
    `_cursor_base.py:629` 抛 `ProgrammingError: the last operation didn't produce
    records (command status: ...)`）。"""


# 脚本项哨兵：这条语句**不产生结果集**（不带 `RETURNING` 的 INSERT/DELETE、DDL 等）。
# 真实 psycopg 是按 `pgresult.status` 判的；此处用显式哨兵，而不是靠 SQL 文本去猜 ——
# 猜文本会把「驱动怎么表现」悄悄换成「我以为驱动怎么表现」，那正是本文件反复踩的坑。
_NO_RESULT = object()


class _DriverCursor:
    """假游标：**照实测**同时给出 `description` 与 `fetchall()` 两副面孔。

    实测（psycopg 3.3.6 `_cursor_base.py:115`）：无结果集时 `Cursor.description`
    **返回 `None`**（不抛）；而 `fetchall()`（同文件 L629）**抛**「没产生记录」。
    两者不对称，正是「`PsycopgConnection.execute()` 无条件 `fetchall()`」会炸的原因 ——
    也是为什么修法要判 `description` 而不是判异常。
    """

    def __init__(self, rows, has_result=True):
        self._rows = rows
        self.description = (("col",),) if has_result else None

    def fetchall(self):
        if self.description is None:
            raise _DriverNoRecords(
                "the last operation didn't produce records (command status: OK)"
            )
        return list(self._rows)


class _DriverClosed(RuntimeError):
    """关掉的连接上再 `execute` 会抛的东西 —— 照实测抄（psycopg 3 报
    `OperationalError: the connection is closed`）。"""

class _DriverConn:
    """假 psycopg 连接：只实现我们真正用到的那几个成员，并且**照实测抄语义**。

    ⚠️ 这个类是 2026-09-24 重写的，重写的原因是它**曾经把驱动写错、因此放跑了一个真缺陷**：

      旧版 `__exit__` 只记一句 `commit` / 异常名，**不关连接** —— 那是照「我以为 psycopg
      的 `with conn:` 是提交/回滚」写的。实测 psycopg 3.3.6 的 `Connection.__exit__`
      （`psycopg/connection.py`）在提交/回滚之后还有一句：

          if not getattr(self, "_pool", None):
              self.close()

      `psycopg.connect()` 不带 pool ⇒ **出块就关连接**。于是 `PsycopgConnection.transaction()`
      里写 `with conn:` 时，「先入库、再读回」在真库上炸成 `DATA_008 ... connection is closed`，
      而本文件里的每一条用例都是绿的 —— 假连接把「实现等于它自己」当成了证据。

      现在：`__exit__` 提交/回滚**并关连接**，`transaction()` 提交/回滚**且不关**，
      且关掉之后再 `execute` 会抛。于是「我们的 `transaction()` 必须在事务后让连接还能用」
      这件事，离线用例就能盯住，不必等到起容器。

    ⚠️ 2026-09-25 又重写了一次，补的是**事务状态**这一层，原因同源（假驱动又一次把驱动的
    行为写成了「我以为」）：

      旧版 `transaction()` 无条件记一句 `exits.append("commit")`，**不管线上到底有没有
      发过 COMMIT**。真驱动不是这样判的。实测 psycopg 3.3.6：

        * `_connection_base.py::_start_query` —— `autocommit=False` 时，第一条语句之前
          psycopg **自己显式发 `BEGIN`** ⇒ 连接从此停在 INTRANS；
        * `transaction.py::_push_savepoint` ——
          `self._outer_transaction = self.pgconn.transaction_status == IDLE`，
          而 `_get_commit_commands()` 只在 `_outer_transaction` 为真时才 `yield b"COMMIT"`。

      ⇒ 只要进事务块之前**裸 `execute()` 过一次**，`transaction()` 就退化成
      `SAVEPOINT _pg3_1` / `RELEASE _pg3_1`，**永远不发 COMMIT**；关连接时整段被服务器
      回滚。旧版假驱动把这个「假提交」记成 `commit` ⇒「先读一次、再写一批」在离线用例里
      全绿、在真库上静默丢数据。

      现在：`autocommit` 是挂在连接上的属性（`connect(**kwargs)` 照实传下来，就像真驱动），
      `transaction_status` 会被维护，`tx` 逐条记下**真正上线的事务控制语句**。用例因此可以
      断言 `"COMMIT" in driver.tx`，而不是断言「我们调了 `transaction()`」。

    两个台账分工写清楚，免得再混：`exits` 记**上下文出口**（旧断言在用，语义不变）；
    `tx` 记**真的发给服务器的语句** —— 「提交了没有」只认后者。
    """

    def __init__(self, script=None, autocommit=False):
        self.script = list(script or [])
        self.calls = []
        self.exits = []
        self.tx = []
        self.closed = False
        self.autocommit = autocommit
        self.transaction_status = _IDLE
        self.rolled_back_on_close = False

    def execute(self, sql, params=()):
        if self.closed:
            raise _DriverClosed("the connection is closed")
        # 照实测照抄 `_start_query`：autocommit ⇒ 不动；已在事务里 ⇒ 不动；
        # 否则**显式发 BEGIN**（这就是「裸 execute 一次之后再也提交不了」的源头）。
        if not self.autocommit and self.transaction_status == _IDLE:
            self.tx.append("BEGIN")
            self.transaction_status = _INTRANS
        self.calls.append((sql, tuple(params)))
        item = self.script.pop(0) if self.script else []
        if item is _NO_RESULT:
            return _DriverCursor([], has_result=False)
        return _DriverCursor(item)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # 照实测：提交或回滚，**然后关连接**。`dict`/`str` 记下来好断言是哪一种；
        # 返回 False ⇒ 异常继续往上抛。
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.exits.append(exc_type.__name__ if exc_type else "commit")
        self.close()
        return False

    def commit(self):
        if self.transaction_status != _IDLE:
            self.tx.append("COMMIT")
            self.transaction_status = _IDLE

    def rollback(self):
        if self.transaction_status != _IDLE:
            self.tx.append("ROLLBACK")
            self.transaction_status = _IDLE

    @contextlib.contextmanager
    def transaction(self):
        """照实测：`Connection.transaction()` 提交/回滚，**不**关连接。

        `_outer_transaction` 由**进入那一刻的状态**决定（照抄 `_push_savepoint`）：
        已经在事务里就只发 `SAVEPOINT` / `RELEASE`，**不发 COMMIT**。
        """
        outer = self.transaction_status == _IDLE
        self.tx.append("BEGIN" if outer else "SAVEPOINT _pg3_1")
        self.transaction_status = _INTRANS
        try:
            yield self
        except BaseException as exc:
            self.tx.append("ROLLBACK" if outer else "ROLLBACK TO _pg3_1")
            self.exits.append(type(exc).__name__)
            if outer:
                self.transaction_status = _IDLE
            raise
        self.tx.append("COMMIT" if outer else "RELEASE _pg3_1")
        if outer:
            self.transaction_status = _IDLE
        self.exits.append("commit")

    def close(self):
        # 照实测：关连接时若还有未结束的事务，服务器把它**回滚** —— 静默丢数据的那个动作。
        # 这里不照抄 `connection.py::commit()` 的异常路径，因为我们要的是能被离线用例观察到的
        # **事实**（多一条要照抄的异常路径，只会再多一个「照猜写」的机会）。
        if self.transaction_status != _IDLE:
            self.rolled_back_on_close = True
            self.tx.append("ROLLBACK (on close)")
            self.transaction_status = _IDLE
        self.closed = True
        self.exits.append("close")


class _DictRow:
    """`psycopg.rows.dict_row` 的替身 —— 按**身份**比较，防止实现里换成别的默认值。"""


_DICT_ROW = _DictRow()


class _FakePsycopgRows:
    dict_row = _DICT_ROW


class _FakePsycopg:
    """假驱动模块（塞进 `sys.modules["psycopg"]`）。"""

    def __init__(self, conn):
        self._conn = conn
        self.connect_calls = []
        self.rows = _FakePsycopgRows()

    def connect(self, dsn, **kwargs):
        self.connect_calls.append((dsn, kwargs))
        # 照实测：`connect()` 的 kwargs 会落到连接对象上 —— 我们依赖的正是 `autocommit`。
        # 少了这一句，`autocommit=True` 的修法在这套假驱动下就完全不可见。
        self._conn.autocommit = bool(kwargs.get("autocommit", False))
        return self._conn


def test_psycopg_connection_asks_the_driver_for_mapping_rows(monkeypatch):
    driver = _DriverConn([[_row()]])
    fake = _FakePsycopg(driver)
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    conn = PsycopgConnection("postgresql://u@127.0.0.1:1/quanauto")
    rows = conn.execute(SQL_SELECT_EXISTING, (SYMBOL, DAY, VERSION))

    assert rows == [_row()], "行的形态要原样交出去，别再自己转一遍"
    assert len(fake.connect_calls) == 1, "连接必须复用：每次 execute 重连＝每次丢事务"
    dsn, kwargs = fake.connect_calls[0]
    assert dsn.startswith("postgresql://"), dsn
    assert kwargs.get("row_factory") is _DICT_ROW, (
        "必须显式要字典行：本模块按列名取值（row['close']），默认的元组行会让取值依赖列序，"
        "列序一改就静默错位。实际 kwargs=%r" % (kwargs,)
    )
    assert driver.calls[0][1] == (SYMBOL, DAY, VERSION), driver.calls
    conn.close()
    assert driver.exits == ["close"], driver.exits


def test_psycopg_transaction_is_the_drivers_commit_and_rollback(monkeypatch):
    """`transaction()` 必须把驱动自己的提交/回滚语义原样传下去，**且事务后连接还能用**。

    这条不是吹毛求疵：若这里写成手工 `BEGIN`/`COMMIT`，异常路径漏掉回滚的后果是
    「库里的半批数据」，它不会自己冒泡成任何一条测试失败 —— 只能靠这条控制组盯住。

    「还能用」那半句是 2026-09-24 补的，补的原因是它真的坏过：`transaction()` 原先写的是
    `with conn:`，而 psycopg 3 的 `Connection.__exit__` 在提交/回滚之后**还会关连接**
    （`if not getattr(self, "_pool", None): self.close()`，实测 psycopg 3.3.6），
    于是「先入库、再读回」的第二次调用报 `the connection is closed`。修法与实测记录：
    `quanauto/pgstore.py` 的 `PsycopgConnection.transaction` 文档串 / 契约附录 B18。
    """
    driver = _DriverConn([[_row()]])
    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg(driver))
    conn = PsycopgConnection("postgresql://u@127.0.0.1:1/quanauto")

    with conn.transaction():
        pass
    with pytest.raises(ValueError):
        with conn.transaction():
            raise ValueError("boom")
    assert driver.exits == ["commit", "ValueError"], driver.exits
    assert driver.closed is False, "事务结束**不能**关连接：本类是可反复用的长命连接"
    assert conn.execute(SQL_SELECT_EXISTING, (SYMBOL, DAY, VERSION)) == [_row()], (
        "事务之后必须还能用同一条连接 —— 「入完一批、紧接着读回」靠的就是这件事"
    )


def test_the_fake_driver_mirrors_the_measured_connection_context_trap():
    """假驱动的**保真度**本身也要被盯住，否则它证明的只是「实现等于它自己」。

    实测（psycopg 3.3.6 `Connection.__exit__`）：`with conn:` 提交/回滚之后**会关连接**
    （不带 pool 时）。这条把该语义钉在假驱动上：哪天真驱动的行为变了、而我们照旧写假驱动，
    这里先红。它同时解释了为什么 `PsycopgConnection.transaction()` 不许写成 `with conn:`。
    """
    with _DriverConn() as driver:
        pass
    assert (driver.closed, driver.exits) == (True, ["commit", "close"]), driver.exits

    driver2 = _DriverConn()
    with pytest.raises(ValueError):
        with driver2:
            raise ValueError("boom")
    assert (driver2.closed, driver2.exits) == (True, ["ValueError", "close"]), driver2.exits

    driver3 = _DriverConn()
    with driver3.transaction():
        pass
    assert (driver3.closed, driver3.exits) == (False, ["commit"]), driver3.exits
    driver3.close()
    with pytest.raises(_DriverClosed):
        driver3.execute("SELECT 1")


def test_the_fake_driver_mirrors_the_measured_implicit_transaction_trap():
    """假驱动的**事务状态**保真度也要被盯住 —— 上一版它无条件记 `commit`，于是放跑了 B20。

    实测（psycopg 3.3.6）：`autocommit=False` 时第一条语句前显式发 `BEGIN`（`_start_query`）；
    进入事务块时若状态不是 IDLE，则 `_outer_transaction=False`，出块只发 `RELEASE`，
    **不发 COMMIT**（`_push_savepoint` + `_get_commit_commands`）。
    这条把该语义钉在假驱动上：哪天真驱动的行为变了而我们照旧，这里先红。
    """
    # ① autocommit=False：裸 execute 一次 ⇒ 真的发了 BEGIN，连接停在 INTRANS
    stuck = _DriverConn([[]])
    stuck.execute("SELECT 1")
    assert (stuck.tx, stuck.transaction_status) == (["BEGIN"], _INTRANS), stuck.tx
    stuck.close()
    assert stuck.rolled_back_on_close is True, (
        "关连接时还开着的事务必须被服务器回滚 —— 这就是静默丢数据的那一下"
    )

    # ② 同一连接：事务块退化成 savepoint，线上**没有** COMMIT，出块后事务仍然开着
    nested = _DriverConn([[], []], autocommit=False)
    nested.execute("SELECT 1")
    with nested.transaction():
        nested.execute("SELECT 2")
    assert nested.tx == ["BEGIN", "SAVEPOINT _pg3_1", "RELEASE _pg3_1"], nested.tx
    assert "COMMIT" not in nested.tx, "已在事务里却发了 COMMIT —— 那不是实测行为"
    assert nested.transaction_status == _INTRANS, "savepoint 分支出块后事务仍然开着"

    # ③ autocommit=True：裸 execute 不开事务，事务块是**真的** BEGIN/COMMIT
    clean = _DriverConn([[], []], autocommit=True)
    clean.execute("SELECT 1")
    assert (clean.tx, clean.transaction_status) == ([], _IDLE), clean.tx
    with clean.transaction():
        clean.execute("SELECT 2")
    assert (clean.tx, clean.transaction_status) == (["BEGIN", "COMMIT"], _IDLE), clean.tx


def test_a_bare_execute_does_not_swallow_the_next_transactions_commit(monkeypatch):
    """🔴 B20（2026-09-25）：先裸读一次、再入一批 —— 线上必须真的出现 COMMIT。

    「先看看库里有什么，再决定写什么」是本模块最常见的用法（读侧与写侧常共用一条连接）。
    psycopg 默认 `autocommit=False`，第一次 `execute()` 就发出 `BEGIN` 并把连接钉在
    INTRANS ⇒ 之后的 `transaction()` 只发 savepoint、**永不 COMMIT**。

    后果是**静默丢数据**：整批写入在 `close()` 时被服务器回滚，而同一条连接上「写完读回
    对拍」恒真且全绿 —— 这正是它在真库上躲过全部离线用例的原因。

    断言的是**线上真的发生了 COMMIT**（`driver.tx`），不是「我们调了 `transaction()`」：
    后者在缺陷版本里同样成立，拿它当判据等于没判。

    本用例是变异 `S13a-autocommit-dropped` 的**主人**（tools/pytest_mutation_check.py）：
    把 `_connect()` 里的 `autocommit=True` 拿掉，必须只有本用例变红。
    真驱动上的对照实验见 `.rounds/_b20_verify.py`（A 段数出 0 行 / B 段数出 1 行）。
    """
    driver = _DriverConn([[_row()], [], [_row()]])
    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg(driver))
    conn = PsycopgConnection("postgresql://u@127.0.0.1:1/quanauto")

    PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)   # 裸读（在任何事务之外）
    report = PgBarIngestor(conn).upsert_daily_bars([_bar()])  # 再写一批
    assert (report.inserted, report.skipped) == (1, 0), report

    assert "COMMIT" in driver.tx, (
        "事务块结束了，线上却没有一条真的 COMMIT —— 写入会在关连接时被静默回滚。"
        "线上实际发生的事务控制：%r" % (driver.tx,)
    )
    conn.close()
    assert driver.rolled_back_on_close is False, (
        "关连接时还有未结束的事务被回滚：%r" % (driver.tx,)
    )


def test_execute_tolerates_statements_without_a_result_set(monkeypatch):
    """🔴 B20 附带缺陷：`execute()` 不许无条件 `fetchall()`。

    `PsycopgConnection.execute()` 是无结果集语句（不带 `RETURNING` 的 INSERT/DELETE、
    任何 DDL）的唯一出口。实测 psycopg 3.3.6（`_cursor_base.py`）：无结果集时
    `Cursor.description` **返回 `None`**，而 `fetchall()` **抛**
    `ProgrammingError: the last operation didn't produce records` ⇒ 这类语句全被包成
    `DataStoreError [DATA_008]`，看起来像「驱动坏了」。

    判据取 `description`（DBAPI 标准，且实测是「返回 None」而不是「抛异常」），
    不是去猜 SQL 文本 —— 猜文本会把判据绑死在调用方的写法上。

    本用例是变异 `S13b-fetchall-unconditional` 的**主人**（tools/pytest_mutation_check.py）：
    把 `if cursor.description is None: return []` 删掉，必须只有本用例变红。
    """
    driver = _DriverConn([_NO_RESULT, [_row()]])
    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg(driver))
    conn = PsycopgConnection("postgresql://u@127.0.0.1:1/quanauto")

    assert conn.execute("DELETE FROM quan_daily_bar WHERE data_version = %s", ("v0",)) == []
    assert driver.calls[0][0].startswith("DELETE"), driver.calls
    assert conn.execute(SQL_SELECT_EXISTING, (SYMBOL, DAY, VERSION)) == [_row()], (
        "有结果集的语句必须照旧返回行 —— 别为了绕过崩溃把 fetchall() 整个删掉"
    )


def test_store_and_ingestor_over_the_driver_seam_end_to_end(monkeypatch):
    """端到端控制组：分支用例用 `FakeConn` 测过，但「`PgBarIngestor` / `PgBarStore` 与
    真实适配层接不接得上」它证明不了（键名对不上、事务没生效、行形态不对都可能发生）。
    这里把假驱动接到 `PsycopgConnection` 上，让读和写各走一遍完整链路。"""
    driver = _DriverConn([[_row()], [], [_row()], [_row()]])
    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg(driver))
    conn = PsycopgConnection("postgresql://u@127.0.0.1:1/quanauto")

    bars = PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)
    assert len(bars) == 1 and bars[0].close == Decimal("10.5"), bars

    report = PgBarIngestor(conn).upsert_daily_bars([_bar()])
    assert (report.inserted, report.skipped) == (1, 0), report
    assert len(driver.calls) == 3, [c[0][:40] for c in driver.calls]
    assert driver.exits == ["commit"], "整批一个事务，正常退出必须提交：%r" % driver.exits
    assert driver.closed is False, (
        "入库之后连接必须是活的 —— 这条端到端用例就是旧版 `with conn:` 缺陷的离线复现（B18）"
    )
    bars_again = PgBarStore(conn, VERSION).select_bars(SYMBOL, DAY, DAY)
    assert len(bars_again) == 1, "写完之后再读一次同一条连接：这就是真库上炸过的那一步"
