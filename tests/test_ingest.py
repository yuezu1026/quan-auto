"""`quanauto/ingest.py` 的离线测试 —— 采集 → 落库那条缝。

这个文件测的是**接缝**，不是两端：`tests/test_data_center_adapter.py` 已经测过
「帧怎么校验」（`validate_frame`），`tests/test_data_center_store.py` 已经测过
「行怎么幂等落库」（`PgBarIngestor`）。此前这两半各自全绿，**缝上一行都没跑过**
（`docs/智能量化交易平台.md` 附录 C.5 案例四，本项目第四次踩这个坑）。所以这里：

* 用**真的** `validate_frame`（不打桩）—— 唯一值得测的判据就是「校验不过 ⇒ 一行都不
  写」，把校验打桩掉就把它测成了同义反复；
* 用假的连接（本机没有本地 PostgreSQL，且这一层要断言的是**发了哪几条 SQL**，
  不是 SQL 在库里跑得对不对）；
* 用**真的** DDL 文本核对取值域 —— 见下。

## 为什么从 DDL 的文本里读数

`dc_ingest_run` 的 `status` / `priority` 取值域在 `quanauto/ingest.py` 里是常量
（`INGEST_STATUSES` / `INGEST_PRIORITIES`），在 `db/data_center.sql` 里是两条具名
CHECK。DDL **一个字都不能动**（动一次四份 SQL 冒烟快照全作废），所以两边不可能靠
「同一处定义」保持一致 —— 只能把 DDL 当**文本**读出来比对。抄一份常量而不钉住它，
就是下一个会漂的东西。

## 两处刻意的「不实现」

* `FakeConn` **不实现回滚**（`transaction()` 只记事件，不撤销已写入的行）。所以凡涉及
  「半批已写又回滚」的断言都留在 `tests/test_data_center_store.py`（那里测的是
  `PgBarIngestor` 的事务语义）；本文件里所有会抛异常的用例都构造成**第一行就冲突**，
  于是「回滚不回滚」不影响结论。
* `FakeConn` 不模拟驱动，只模拟**列语义**：从 SQL 文本里把 `INSERT ... (列清单)` 解析
  出来按列名装配，而不是按位置解包 `params`。位置解包会把「列顺序」这一层也测进来，
  而那一层是 `pgstore` 的事。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from datetime import date, datetime

import pandas as pd
import pytest

from quanauto.datacenter import AdjustFactorPoint, DailyBar
from quanauto.datasources import (
    ADJUST_FACTOR_COLUMNS,
    DAILY_BAR_COLUMNS,
    SourceAdapter,
    validate_frame,
)
from quanauto.enums import SourcePriority
from quanauto.errors import (
    DataQualityError,
    DataStoreError,
    DataVersionError,
    IngestConflictError,
    InvalidParamError,
    SourceAdapterError,
)
from quanauto.ingest import (
    DAILY_CHANNEL,
    FACTOR_CHANNEL,
    INGEST_PRIORITIES,
    INGEST_STATUSES,
    SQL_FINISH_RUN,
    SQL_START_RUN,
    IngestRun,
    IngestRunLog,
    ingest_adjust_factors,
    ingest_daily_bars,
)
from quanauto.pgstore import IngestReport, PgBarIngestor, PgFactorIngestor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DC_DDL_REL = os.path.join("db", "data_center.sql")

START = date(2026, 1, 5)
END = date(2026, 1, 6)
VERSION = "v2026-01-06"

INSERT_COLUMNS_RE = re.compile(r"INSERT INTO (\w+)\s*\(([^)]*)\)")


# ── 假连接 ───────────────────────────────────────────────────────────────────
def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def _insert_columns(sql: str) -> tuple:
    """从 `INSERT INTO t (a, b, c) VALUES ...` 里把列清单读出来。

    提取为空**当场炸**（`AssertionError`）：假连接读不到列清单，装配出来的行就会
    缺列，而缺列在 `_diff_fields` 那里表现为「主键取回的行少了列」——那是一句与
    真实原因无关的报错。提取器的失效必须响自己的名字。
    """
    match = INSERT_COLUMNS_RE.search(sql)
    assert match, "假连接从这条 SQL 里读不出 INSERT 的列清单：%r" % sql
    columns = tuple(name.strip() for name in match.group(2).split(","))
    assert columns and all(columns), "列清单里有空项：%r" % (columns,)
    return columns


class FakeConn:
    """按列语义记账的假连接：`dc_ingest_run` / `dc_daily_bar` / `dc_adjust_factor`。

    三张表都是「主键 → 本文」的字典，于是「重跑同一批 ⇒ 全部 skipped」与「同主键异值
    ⇒ 冲突」这两条不需要任何脚本就能自然发生 —— 它们本来就该由数据决定，而不是由
    「第几次调用该返回什么」决定。

    `break_sql` 里的任意子串命中当前 SQL 就抛一个**不是**本项目异常的异常（模拟驱动
    断连）。这样测的是「驱动异常被包成 DataStoreError」而不是「本项目异常被二次包装」。
    """

    def __init__(self, *, break_sql: tuple = ()) -> None:
        self.calls: list = []
        self.events: list = []
        self.runs: dict = {}
        self.bars: dict = {}
        self.factors: dict = {}
        self.break_sql = tuple(break_sql)
        self._next_run_id = 1

    # -- 记账 ---------------------------------------------------------------
    def execute(self, sql, params=()):
        flat = _normalize(sql)
        self.calls.append((flat, tuple(params)))
        for token in self.break_sql:
            if token in flat:
                raise RuntimeError("server closed the connection unexpectedly")

        if flat.startswith("INSERT INTO dc_ingest_run"):
            columns = _insert_columns(flat)
            values = dict(zip(columns, params))
            assert values["status"] == "RUNNING", \
                "开批次那一条应当写 RUNNING，实际 %r" % (values["status"],)
            run_id = self._next_run_id
            self._next_run_id += 1
            self.runs[run_id] = dict(values, run_id=run_id, row_count=0,
                                     error_message="", finished_at=None)
            return [{"run_id": run_id}]

        if flat.startswith("UPDATE dc_ingest_run"):
            status, row_count, error_message, run_id = params
            row = self.runs.get(run_id)
            if row is None:
                return []
            row.update(status=status, row_count=row_count,
                       error_message=error_message, finished_at="2026-01-06 09:00:00")
            return [{"run_id": run_id}]

        if flat.startswith("INSERT INTO dc_daily_bar"):
            values = dict(zip(_insert_columns(flat), params))
            key = (values["symbol"], values["trade_date"], values["data_version"])
            if key not in self.bars:
                self.bars[key] = dict(values)
            return [{"symbol": values["symbol"]}]

        if flat.startswith("INSERT INTO dc_adjust_factor"):
            values = dict(zip(_insert_columns(flat), params))
            key = (values["symbol"], values["trade_date"], values["data_version"])
            if key not in self.factors:
                self.factors[key] = dict(values)
            return [{"symbol": values["symbol"]}]

        if flat.startswith("SELECT open, high, low, close, volume, amount"):
            row = self.bars.get(tuple(params))
            return [dict(row)] if row else []

        if flat.startswith("SELECT adjust_factor"):
            row = self.factors.get(tuple(params))
            return [dict(row)] if row else []

        raise AssertionError("假连接收到没预期的 SQL：%s" % flat)

    @contextlib.contextmanager
    def transaction(self):
        """只记事件、**不实现回滚**（见模块 docstring 的第二条）。"""
        self.events.append("begin")
        try:
            yield self
        except Exception:
            self.events.append("rollback")
            raise
        self.events.append("commit")

    # -- 断言用的小工具 -----------------------------------------------------
    def sql_kinds(self) -> list:
        kinds = []
        for flat, _ in self.calls:
            if flat.startswith("INSERT INTO dc_ingest_run"):
                kinds.append("start")
            elif flat.startswith("UPDATE dc_ingest_run"):
                kinds.append("finish")
            elif flat.startswith("INSERT INTO dc_daily_bar"):
                kinds.append("bar")
            elif flat.startswith("INSERT INTO dc_adjust_factor"):
                kinds.append("factor")
            else:
                kinds.append("select")
        return kinds

    def only_run(self) -> dict:
        assert len(self.runs) == 1, "dc_ingest_run 里有 %d 行，应当恰好 1 行" % len(self.runs)
        return next(iter(self.runs.values()))


# ── 假适配器 ─────────────────────────────────────────────────────────────────
class FakeAdapter(SourceAdapter):
    """离线适配器：`fetch_*` 返回固定的帧，`validate` 走**真的** `validate_frame`。"""

    def __init__(self, frame=None, *, name="fake", daily_error=None, factor_error=None,
                 probe=None) -> None:
        self._frame = frame
        self._name = name
        self._daily_error = daily_error
        self._factor_error = factor_error
        self._probe = probe
        self.fetches: list = []
        self.validated: int = 0

    def source_name(self) -> str:
        return self._name

    def _check_probe(self):
        if self._probe is not None:
            self._probe()

    def fetch_daily_bar(self, symbols, start, end):
        self.fetches.append(("daily", tuple(symbols), start, end))
        self._check_probe()
        if self._daily_error is not None:
            raise self._daily_error
        return self._frame

    def fetch_adjust_factor(self, symbols, start, end):
        self.fetches.append(("factor", tuple(symbols), start, end))
        self._check_probe()
        if self._factor_error is not None:
            raise self._factor_error
        return self._frame

    def fetch_financial(self, symbols, period_end):
        raise AssertionError("本测试不该走到 fetch_financial")

    def fetch_index_members(self, index_code, as_of_date):
        raise AssertionError("本测试不该走到 fetch_index_members")

    def validate(self, frame):
        self.validated += 1
        return validate_frame(frame)


# ── 帧构造 ───────────────────────────────────────────────────────────────────
def daily_frame(symbols=("600000.SH",), days=(date(2026, 1, 5),), close=10.0):
    """合法日线帧（8 列标准列，**不含** source / data_version）。"""
    rows = []
    for symbol in symbols:
        for offset, day in enumerate(days):
            price = close + offset
            rows.append({
                "symbol": symbol, "trade_date": day,
                "open": price, "high": price + 1.0, "low": price - 1.0, "close": price,
                "volume": 1000.0, "amount": 10000.0,
            })
    return pd.DataFrame(rows, columns=list(DAILY_BAR_COLUMNS))


def factor_frame(symbols=("600000.SH",), days=(date(2026, 1, 5),), factor=1.0):
    rows = [{"symbol": symbol, "trade_date": day, "adjust_factor": factor}
            for symbol in symbols for day in days]
    return pd.DataFrame(rows, columns=list(ADJUST_FACTOR_COLUMNS))


def bad_price_frame():
    frame = daily_frame()
    frame.loc[0, "close"] = -1.0
    return frame


def leaky_frame():
    """源特有列名泄漏到输出列 —— 契约 §3.2 验收表点名要拒的那种。"""
    frame = daily_frame()
    frame["tradeStatus"] = 1
    return frame


# ── 1. 取值域钉在 DDL 文本上 ─────────────────────────────────────────────────
def ddl_text() -> str:
    path = os.path.join(ROOT, DC_DDL_REL)
    with open(path, encoding="utf-8") as handle:
        return handle.read().replace("\r\n", "\n")


def ddl_check_values(constraint: str) -> tuple:
    """从 DDL 文本里读出 `<constraint>` 的 `IN (...)` 取值列表。"""
    text = ddl_text()
    pattern = r"CONSTRAINT\s+%s\s+CHECK\s*\(\s*\w+\s+IN\s*\(([^)]*)\)" % constraint
    match = re.search(pattern, text)
    assert match, ("从 %s 里提取不到 %s 的 IN 列表 —— 提取为空的话下面的比对是在比两个"
                   "空东西，必须当失败报出来" % (DC_DDL_REL, constraint))
    values = tuple(re.findall(r"'([^']*)'", match.group(1)))
    assert values, "提取到了括号但里面没有字面量：%r" % (match.group(1),)
    return values


def ddl_columns(table: str) -> tuple:
    """从 DDL 文本里读出 `CREATE TABLE <table>` 的列名清单（跳过约束与注释行）。"""
    text = ddl_text()
    match = re.search(r"CREATE TABLE IF NOT EXISTS %s\s*\((.*?)\n\);" % table, text, re.S)
    assert match, "从 %s 里找不到 %s 的建表语句 —— 提取为空，列名比对形同虚设" % (DC_DDL_REL, table)
    names = []
    for line in match.group(1).split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if re.match(r"(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK)\b", stripped):
            continue
        first = stripped.split()[0]
        if re.match(r"^[a-z_][a-z0-9_]*$", first):
            names.append(first)
    assert names, "从 %s 的建表语句里一列都没读出来" % table
    return tuple(names)


def test_status_vocabulary_is_the_ddl_check_verbatim():
    assert INGEST_STATUSES == ddl_check_values("ck_dc_ingest_status")


def test_priority_vocabulary_is_the_ddl_check_verbatim():
    assert INGEST_PRIORITIES == ddl_check_values("ck_dc_ingest_priority")


def test_the_run_log_sql_only_touches_columns_the_ddl_declares():
    """SQL 里写到的列必须都在 DDL 里 —— 抄错一个列名在假连接上是看不见的。"""
    declared = set(ddl_columns("dc_ingest_run"))
    inserted = _insert_columns(_normalize(SQL_START_RUN))
    asserted = tuple(name.strip().split("=")[0].strip()
                     for name in _normalize(SQL_FINISH_RUN)
                     .split(" SET ", 1)[1].split(" WHERE ", 1)[0].split(","))
    assert set(inserted) <= declared, "start 里写了 DDL 没有的列：%s" % (set(inserted) - declared)
    assert set(asserted) <= declared, "finish 里写了 DDL 没有的列：%s" % (set(asserted) - declared)
    # `finished_at` 是 finish 里其实也会写的一列，但它不出现在 `SET` 的等号左边
    # （写的是 `CURRENT_TIMESTAMP(3)`，没有参数）—— 手工补进来一起核。
    assert "finished_at" in declared


def test_placeholder_count_matches_the_params_we_bind():
    """`%s` 个数与绑定参数个数不等的话，真库上必炸，而假连接上不会。"""
    conn = FakeConn()
    log = IngestRunLog(conn)
    run_id = log.start(adapter="fake", priority=INGEST_PRIORITIES[0], start_date=START,
                       end_date=END, symbol_count=1, data_version=VERSION)
    log.finish(run_id, status=INGEST_STATUSES[1], row_count=1)
    for flat, params in conn.calls:
        assert flat.count("%s") == len(params), \
            "SQL 里有 %d 个 %%s 而绑定了 %d 个参数：%s" % (flat.count("%s"), len(params), flat)


# ── 2. 前置检查：不留痕、不碰网 ──────────────────────────────────────────────
def test_no_symbols_is_rejected_and_leaves_no_run_row():
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame())
    with pytest.raises(InvalidParamError):
        ingest_daily_bars(adapter, [], START, END, conn=conn, data_version=VERSION)
    assert conn.calls == [], "前置检查失败却发了 SQL：%s" % (conn.calls,)
    assert adapter.fetches == [], "前置检查失败却去取数了"


def test_a_bare_string_is_not_a_symbol_list():
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame())
    with pytest.raises(InvalidParamError):
        ingest_daily_bars(adapter, "600000.SH", START, END, conn=conn, data_version=VERSION)
    assert conn.calls == []


@pytest.mark.parametrize("window", [
    (datetime(2026, 1, 5), date(2026, 1, 6)),
    (date(2026, 1, 5), datetime(2026, 1, 6)),
])
def test_datetime_window_is_rejected_not_silently_truncated(window):
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame())
    with pytest.raises(InvalidParamError) as excinfo:
        ingest_daily_bars(adapter, ["600000.SH"], window[0], window[1],
                          conn=conn, data_version=VERSION)
    assert "自然日" in str(excinfo.value)
    assert conn.calls == []


def test_a_reversed_window_is_rejected():
    conn = FakeConn()
    with pytest.raises(InvalidParamError):
        ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], END, START,
                          conn=conn, data_version=VERSION)
    assert conn.calls == []


@pytest.mark.parametrize("version", ["", "   ", None, 2026])
def test_blank_data_version_is_rejected_before_any_sql(version):
    conn = FakeConn()
    with pytest.raises(DataVersionError):
        ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], START, END,
                          conn=conn, data_version=version)
    assert conn.calls == [], "版本号没填却已经把批次行开出来了"


def test_data_version_is_keyword_only_so_it_cannot_be_forgotten():
    with pytest.raises(TypeError):
        ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], START, END,
                          FakeConn(), VERSION)  # type: ignore[misc]


def test_an_unknown_priority_is_rejected():
    conn = FakeConn()
    with pytest.raises(InvalidParamError):
        ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], START, END,
                          conn=conn, data_version=VERSION, priority="SECONDARY")
    assert conn.calls == []


# ── 3. 正常路径 ──────────────────────────────────────────────────────────────
def test_daily_happy_path_writes_bars_and_one_finished_run_row():
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame(symbols=("600000.SH", "000001.SZ"),
                                      days=(date(2026, 1, 5), date(2026, 1, 6))))
    run = ingest_daily_bars(adapter, ["600000.SH", "000001.SZ"], START, END,
                            conn=conn, data_version=VERSION)

    assert isinstance(run, IngestRun)
    assert (run.status, run.inserted, run.skipped, run.row_count) == ("SUCCESS", 4, 0, 4)
    assert run.missing_symbols == () and run.complete is True
    assert run.requested_symbols == ("600000.SH", "000001.SZ")

    # 两条批次 SQL：先 start 后 finish，且 finish 改的就是 start 开的那一行。
    assert conn.sql_kinds().count("start") == 1
    assert conn.sql_kinds().count("finish") == 1
    row = conn.only_run()
    assert row["run_id"] == run.run_id
    assert row["status"] == "SUCCESS"
    assert row["row_count"] == 4
    assert row["symbol_count"] == 2
    assert row["adapter"] == "fake"
    assert row["priority"] == "PRIMARY"
    assert row["data_version"] == VERSION
    assert row["finished_at"] is not None

    # 四条日线落库，且 `source` / `data_version` 是**本层**盖的戳。
    assert len(conn.bars) == 4
    for (symbol, trade_date, data_version), bar in conn.bars.items():
        assert data_version == VERSION
        assert bar["source"] == "fake"
        assert isinstance(symbol, str) and isinstance(trade_date, date)


def test_the_run_row_is_opened_before_the_frame_is_fetched():
    """「先开行、后取数」是刻意的：失败的批次才是日志最该留下的东西。"""
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame(), probe=lambda: _assert_run_open(conn))
    ingest_daily_bars(adapter, ["600000.SH"], START, END, conn=conn, data_version=VERSION)
    assert conn.sql_kinds()[0] == "start"
    assert conn.sql_kinds()[-1] == "finish"


def _assert_run_open(conn):
    assert conn.runs, "取数的时候 dc_ingest_run 里还没有行 —— 说明是先取数后开行"


def test_fallback_priority_is_carried_into_the_run_row():
    conn = FakeConn()
    run = ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], START, END,
                            conn=conn, data_version=VERSION,
                            priority=SourcePriority.FALLBACK)
    assert run.priority == "FALLBACK"
    assert conn.only_run()["priority"] == "FALLBACK"
    assert conn.only_run()["priority"] in INGEST_PRIORITIES


def test_adjust_factor_channel_writes_the_factor_table():
    conn = FakeConn()
    adapter = FakeAdapter(factor_frame(factor=1.00000005))
    run = ingest_adjust_factors(adapter, ["600000.SH"], START, END,
                                conn=conn, data_version=VERSION)

    assert run.status == "SUCCESS"
    assert run.row_count == 1
    assert conn.bars == {}, "复权因子不许顺手写日线表"
    assert len(conn.factors) == 1
    bar = next(iter(conn.factors.values()))
    assert bar["source"] == "fake" and bar["data_version"] == VERSION
    assert adapter.fetches[0][0] == "factor"
    assert conn.sql_kinds().count("factor") == 1


def test_the_row_objects_are_the_normalized_dataclasses_we_think_they_are(monkeypatch):
    """帧记录只有 8 列，行对象有 10 个字段（多出来的两列是本层盖的戳）。"""
    conn = FakeConn()
    seen = {}

    real = PgBarIngestor.upsert_daily_bars

    def spy(self, rows):
        seen["rows"] = list(rows)
        return real(self, rows)

    monkeypatch.setattr(PgBarIngestor, "upsert_daily_bars", spy)
    ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], START, END,
                      conn=conn, data_version=VERSION)

    assert len(seen["rows"]) == 1
    row = seen["rows"][0]
    assert isinstance(row, DailyBar)
    assert row.source == "fake" and row.data_version == VERSION
    assert (row.symbol, row.trade_date) == ("600000.SH", START)
    assert row.close == 10.0 and row.__class__ is DailyBar
    # 行对象的字段 = 「帧里的 8 个标准列」+「本层盖的两个戳」。少一个字段会 TypeError
    # ⇒ DataQualityError；多一个字段则说明标准列集合变了。
    assert {field.name for field in DailyBar.__dataclass_fields__.values()} == \
        set(DAILY_BAR_COLUMNS) | {"source", "data_version"}, \
        "DailyBar 的字段清单与帧的标准列不再一一对应"
    assert len(DAILY_BAR_COLUMNS) == 8, "标准日线列应当是 8 列，比对赖它成立"
    assert type(row.trade_date) is date, \
        "帧里的日期必须是 date（不能是 datetime 或 Timestamp），实际 %r" % (type(row.trade_date),)
    assert row.trade_date == START


# ── 4. 校验门：不通过 ⇒ 一行都不写 ──────────────────────────────────────────
def test_a_frame_that_fails_validation_writes_nothing_and_marks_the_run_failed():
    conn = FakeConn()
    adapter = FakeAdapter(bad_price_frame())
    with pytest.raises(DataQualityError) as excinfo:
        ingest_daily_bars(adapter, ["600000.SH"], START, END, conn=conn, data_version=VERSION)

    assert "ck_dc_bar_price_positive" in str(excinfo.value), \
        "异常里应当带上校验器给出的原因，实际：%s" % (excinfo.value,)
    assert conn.bars == {}, "校验没过却写了日线：%s" % (conn.bars,)
    row = conn.only_run()
    assert row["status"] == "FAILED"
    assert row["row_count"] == 0
    assert "ck_dc_bar_price_positive" in row["error_message"]
    assert adapter.validated == 1, "校验器根本没被调用，那这条判据是空转的"


def test_an_empty_frame_is_refused_not_treated_as_a_pass():
    """空帧不是「没有数据」而是「没有可校验的东西」—— 契约 §2.4 的防假绿。"""
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame(symbols=(), days=()))
    with pytest.raises(DataQualityError) as excinfo:
        ingest_daily_bars(adapter, ["600000.SH"], START, END, conn=conn, data_version=VERSION)
    assert "帧为空" in str(excinfo.value)
    assert conn.bars == {}
    assert conn.only_run()["status"] == "FAILED"


def test_a_leaked_source_column_is_refused():
    conn = FakeConn()
    adapter = FakeAdapter(leaky_frame())
    with pytest.raises(DataQualityError) as excinfo:
        ingest_daily_bars(adapter, ["600000.SH"], START, END, conn=conn, data_version=VERSION)
    assert "非标准列" in str(excinfo.value)
    assert conn.bars == {}


def test_a_frame_missing_a_requested_symbol_is_partial_not_success():
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame(symbols=("600000.SH",)))
    run = ingest_daily_bars(adapter, ["600000.SH", "000001.SZ"], START, END,
                            conn=conn, data_version=VERSION)
    assert run.status == "PARTIAL"
    assert run.missing_symbols == ("000001.SZ",)
    assert run.received_symbols == ("600000.SH",)
    assert run.complete is False
    row = conn.only_run()
    assert row["status"] == "PARTIAL"
    assert "000001.SZ" in row["error_message"], "PARTIAL 的批次要写明缺了谁"
    assert row["row_count"] == 1


def test_the_requested_symbols_are_normalized_so_partial_is_not_reported_falsely():
    """请求 `600000` 而帧里是 `600000.SH` —— 不归一就会每次都假报 PARTIAL。"""
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame(symbols=("600000.SH",)))
    run = ingest_daily_bars(adapter, ["600000"], START, END, conn=conn, data_version=VERSION)

    assert adapter.fetches[0][1] == ("600000.SH",), "交给适配器的应当是归一化后的代码"
    assert run.requested_symbols == ("600000.SH",)
    assert run.missing_symbols == (), "请求侧没归一 ⇒ 这里会假报缺一个标的"
    assert run.status == "SUCCESS"


def test_duplicate_requested_symbols_are_deduped_keeping_the_order():
    conn = FakeConn()
    adapter = FakeAdapter(daily_frame(symbols=("600000.SH", "000001.SZ")))
    run = ingest_daily_bars(adapter, ["600000", "000001", "600000"], START, END,
                            conn=conn, data_version=VERSION)
    assert run.requested_symbols == ("600000.SH", "000001.SZ")
    assert conn.only_run()["symbol_count"] == 2


def test_a_bad_symbol_is_reported_as_a_source_adapter_error():
    from quanauto.datasources import normalize_symbol
    conn = FakeConn()
    try:
        normalize_symbol("NOT-A-CODE")
    except SourceAdapterError:
        pass
    else:  # pragma: no cover - 若归一化不再抛异常，这条测试的前提变了
        pytest.skip("normalize_symbol 现在接受任意字符串，本用例前提已变")
    with pytest.raises(SourceAdapterError):
        ingest_daily_bars(FakeAdapter(daily_frame()), ["NOT-A-CODE"], START, END,
                          conn=conn, data_version=VERSION)
    assert conn.calls == [], "标的没归一出错不该开批次行"


# ── 5. 幂等（契约 §3.10）与冲突（D8 / DATA_007）──────────────────────────────
def test_the_same_batch_twice_is_idempotent_the_second_time_is_all_skipped():
    conn = FakeConn()
    frame = daily_frame(symbols=("600000.SH",), days=(date(2026, 1, 5), date(2026, 1, 6)))
    first = ingest_daily_bars(FakeAdapter(frame), ["600000.SH"], START, END,
                              conn=conn, data_version=VERSION)
    bars_after_first = dict(conn.bars)
    second = ingest_daily_bars(FakeAdapter(frame), ["600000.SH"], START, END,
                               conn=conn, data_version=VERSION)

    assert (first.inserted, first.skipped) == (2, 0)
    assert (second.inserted, second.skipped) == (0, 2)
    assert second.row_count == 2, "row_count 是「落定的行数」= inserted + skipped"
    assert second.status == "SUCCESS"
    assert conn.bars == bars_after_first, "重跑之后行数或内容变了 ⇒ 不幂等"
    assert len(conn.runs) == 2, "两次调用应当各留一行日志"


def test_a_changed_value_on_the_same_key_is_a_conflict_and_surfaces_as_failed():
    conn = FakeConn()
    ingest_daily_bars(FakeAdapter(daily_frame(close=10.0)), ["600000.SH"], START, END,
                      conn=conn, data_version=VERSION)
    before = dict(conn.bars)

    with pytest.raises(IngestConflictError) as excinfo:
        ingest_daily_bars(FakeAdapter(daily_frame(close=99.0)), ["600000.SH"], START, END,
                          conn=conn, data_version=VERSION)
    assert "DATA_007" in str(excinfo.value) or "不可变" in str(excinfo.value)
    assert conn.bars == before, "冲突批次不许改到已有行"
    assert max(conn.runs) == 2 and conn.runs[2]["status"] == "FAILED"
    assert conn.runs[2]["error_message"], "FAILED 的行要留下原因"


def test_a_different_data_version_is_not_a_conflict():
    conn = FakeConn()
    ingest_daily_bars(FakeAdapter(daily_frame(close=10.0)), ["600000.SH"], START, END,
                      conn=conn, data_version="v1")
    run = ingest_daily_bars(FakeAdapter(daily_frame(close=99.0)), ["600000.SH"], START, END,
                            conn=conn, data_version="v2")
    assert (run.inserted, run.skipped) == (1, 0), "换版本重采是 D8 允许的改历史方式"
    assert len(conn.bars) == 2


def test_factor_conflict_is_surfaced_too():
    conn = FakeConn()
    ingest_adjust_factors(FakeAdapter(factor_frame(factor=1.0)), ["600000.SH"], START, END,
                          conn=conn, data_version=VERSION)
    with pytest.raises(IngestConflictError):
        ingest_adjust_factors(FakeAdapter(factor_frame(factor=2.0)), ["600000.SH"], START, END,
                              conn=conn, data_version=VERSION)
    assert conn.runs[2]["status"] == "FAILED"


# ── 6. 源这一侧失败 / 库这一侧失败 ──────────────────────────────────────────
def test_a_source_failure_keeps_its_own_kind_and_retryable_and_marks_the_run_failed():
    conn = FakeConn()
    failure = SourceAdapterError("东财返回 503", source="eastmoney",
                                 kind="SOURCE_UNREACHABLE", retryable=True)
    adapter = FakeAdapter(daily_frame(), daily_error=failure)
    with pytest.raises(SourceAdapterError) as excinfo:
        ingest_daily_bars(adapter, ["600000.SH"], START, END, conn=conn, data_version=VERSION)

    assert excinfo.value is failure, "源异常被二次包装了：调用方拿不到 kind / retryable"
    assert excinfo.value.kind == "SOURCE_UNREACHABLE"
    assert excinfo.value.retryable is True
    assert conn.bars == {}
    row = conn.only_run()
    assert row["status"] == "FAILED"
    assert "503" in row["error_message"]


def test_a_raw_driver_error_becomes_a_datastore_error():
    conn = FakeConn(break_sql=("SELECT open, high",))
    with pytest.raises(DataStoreError) as excinfo:
        ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], START, END,
                          conn=conn, data_version=VERSION)
    assert "RuntimeError" in str(excinfo.value), "原始异常类型应当出现在消息里"
    assert excinfo.value.__cause__ is not None, "要用 from exc 保留因果链"
    assert conn.only_run()["status"] == "FAILED"


def test_a_break_while_opening_the_run_row_is_a_datastore_error_and_no_fetch_happens():
    conn = FakeConn(break_sql=("INSERT INTO dc_ingest_run",))
    adapter = FakeAdapter(daily_frame())
    with pytest.raises(DataStoreError):
        ingest_daily_bars(adapter, ["600000.SH"], START, END, conn=conn, data_version=VERSION)
    assert adapter.fetches == [], "批次行都没开出来就去取数了"
    assert conn.bars == {}


def test_opening_a_run_that_returns_no_row_is_refused():
    class SilentConn(FakeConn):
        def execute(self, sql, params=()):
            if _normalize(sql).startswith("INSERT INTO dc_ingest_run"):
                self.calls.append((_normalize(sql), tuple(params)))
                return []
            return super().execute(sql, params)

    conn = SilentConn()
    adapter = FakeAdapter(daily_frame())
    with pytest.raises(DataStoreError) as excinfo:
        ingest_daily_bars(adapter, ["600000.SH"], START, END, conn=conn, data_version=VERSION)
    assert "run_id" in str(excinfo.value)
    assert adapter.fetches == [], "拿不到 run_id 还继续取数 ⇒ 收尾会静默改 0 行"


def test_a_non_integer_run_id_is_refused():
    class BogusConn(FakeConn):
        def execute(self, sql, params=()):
            if _normalize(sql).startswith("INSERT INTO dc_ingest_run"):
                self.calls.append((_normalize(sql), tuple(params)))
                return [{"run_id": "7"}]
            return super().execute(sql, params)

    with pytest.raises(DataStoreError) as excinfo:
        ingest_daily_bars(FakeAdapter(daily_frame()), ["600000.SH"], START, END,
                          conn=BogusConn(), data_version=VERSION)
    assert "run_id" in str(excinfo.value)


def test_the_write_report_must_account_for_every_row(monkeypatch):
    """`inserted + skipped` 与帧行数不等 ⇒ 「账对不上」，宁可标 FAILED 也不许标 SUCCESS。

    样本必须是**多行**的：只有一行时 `inserted=1, skipped=0` 本来就对得上，
    变异会静默打空分支（看起来像探测器没写）。
    """
    conn = FakeConn()
    monkeypatch.setattr(PgBarIngestor, "upsert_daily_bars",
                        lambda self, rows: IngestReport(inserted=1, skipped=0))
    frame = daily_frame(symbols=("600000.SH", "000001.SZ"), days=(START,))
    assert frame.shape[0] == 2, "样本必须是 2 行，否则「账对不上」根本不会触发"
    with pytest.raises(DataStoreError) as excinfo:
        ingest_daily_bars(FakeAdapter(frame), ["600000.SH", "000001.SZ"], START, END,
                          conn=conn, data_version=VERSION)
    message = str(excinfo.value)
    assert "账对不上" in message
    assert "autocommit" in message, "消息要写清「前面的行已经落库了」，否则读者会以为一行没写"
    assert conn.only_run()["status"] == "FAILED"


def test_failure_recording_never_masks_the_original_error(caplog):
    conn = FakeConn(break_sql=("UPDATE dc_ingest_run",))
    adapter = FakeAdapter(bad_price_frame())
    with caplog.at_level(logging.WARNING, logger="quanauto.ingest"):
        with pytest.raises(DataQualityError) as excinfo:
            ingest_daily_bars(adapter, ["600000.SH"], START, END,
                              conn=conn, data_version=VERSION)
    assert "ck_dc_bar_price_positive" in str(excinfo.value), \
        "记失败时的异常把真正的失败盖掉了"
    assert any("run_id" in record.getMessage() for record in caplog.records), \
        "记不上失败状态时应当吼一声，实际日志：%r" % (caplog.text,)
    assert conn.runs[1]["status"] == "RUNNING", "连 UPDATE 都失败了，行只能停在 RUNNING"


# ── 7. IngestRunLog 自身的边界 ───────────────────────────────────────────────
def test_finish_refuses_running_and_unknown_statuses():
    conn = FakeConn()
    log = IngestRunLog(conn)
    run_id = log.start(adapter="fake", priority="PRIMARY", start_date=START, end_date=END,
                       symbol_count=1, data_version=VERSION)
    with pytest.raises(InvalidParamError):
        log.finish(run_id, status="RUNNING", row_count=0)
    with pytest.raises(InvalidParamError):
        log.finish(run_id, status="DONE", row_count=0)
    assert conn.only_run()["status"] == "RUNNING", "被拒绝的 finish 不该改到行"


def test_finish_on_a_missing_run_id_is_refused():
    conn = FakeConn()
    with pytest.raises(DataStoreError) as excinfo:
        IngestRunLog(conn).finish(4242, status="SUCCESS", row_count=0)
    assert "4242" in str(excinfo.value)


def test_start_always_writes_running():
    conn = FakeConn()
    IngestRunLog(conn).start(adapter="fake", priority="PRIMARY", start_date=START,
                             end_date=END, symbol_count=3, data_version=VERSION)
    assert conn.only_run()["status"] == "RUNNING"
    assert conn.only_run()["row_count"] == 0


# ── 8. 这一层不许把驱动拉进来 ────────────────────────────────────────────────
def test_importing_ingest_does_not_import_the_driver():
    """与 `test_import_pgstore_does_not_import_the_driver` 同一个前提。"""
    import subprocess
    code = ("import sys, quanauto.ingest; "
            "print('psycopg' in sys.modules or 'psycopg2' in sys.modules)")
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", \
        "import quanauto.ingest 把数据库驱动拉进来了（应当只有显式连库时才导入）"


# ── 9. IngestRun 的形状 ──────────────────────────────────────────────────────
def test_ingest_run_row_count_and_complete_are_derived_not_stored():
    run = IngestRun(run_id=1, status="PARTIAL", adapter="fake", priority="PRIMARY",
                    data_version=VERSION, start_date=START, end_date=END,
                    requested_symbols=("600000.SH",), received_symbols=(),
                    missing_symbols=("600000.SH",), inserted=3, skipped=2)
    assert run.row_count == 5
    assert run.complete is False
    assert run.warnings == ()
    assert run.status in INGEST_STATUSES


def test_the_channels_have_names_so_the_log_message_is_readable():
    assert DAILY_CHANNEL and FACTOR_CHANNEL
    assert DAILY_CHANNEL != FACTOR_CHANNEL


def test_adjust_factor_rows_are_the_factor_dataclass(monkeypatch):
    conn = FakeConn()
    seen = {}
    real = PgFactorIngestor.upsert_adjust_factors

    def spy(self, rows):
        seen["rows"] = list(rows)
        return real(self, rows)

    monkeypatch.setattr(PgFactorIngestor, "upsert_adjust_factors", spy)
    ingest_adjust_factors(FakeAdapter(factor_frame()), ["600000.SH"], START, END,
                          conn=conn, data_version=VERSION)

    row = seen["rows"][0]
    assert isinstance(row, AdjustFactorPoint)
    assert row.source == "fake" and row.data_version == VERSION
    assert row.adjust_factor == 1.0
