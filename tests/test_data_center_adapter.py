"""I2 S2 —— 数据源适配器层（`quanauto/datasources.py`）的用例。

本文件的纪律（与 `test_data_center_pit.py` 相同）：

* **一律不联网**。适配器对 AKShare / Baostock 的调用通过注入的「取数函数」发生，
  测试注入假函数；真实调用只能落成一份带日期 + 源名 + 标的 + 行数的快照，
  诚实度与 `tools/sql-smoke-report.txt` 同级（那份也只在标题里写「在哪个镜像上跑过」）。
* 这些用例是**门禁**（`tools/verify_data_center_adapter.py`）测不到的那一半：
  门禁只解析源码形状，行为由这里负责。
"""

from __future__ import annotations

import dataclasses
from datetime import date
import http.server
import json
import socket
import threading
import urllib.error
import urllib.parse

import pandas as pd
import pytest

from quanauto import datasources as ds
from quanauto.datasources import (
    AKSHARE_DAILY_BAR,
    DAILY_BAR_COLUMNS,
    EASTMONEY_BALANCE_FINANCIAL,
    EASTMONEY_INDEX_MEMBER,
    EASTMONEY_INCOME_FINANCIAL,
    EASTMONEY_REPORT_TYPE,
    FINANCIAL_COLUMNS,
    FINANCIAL_REQUIRED_COLUMNS,
    INDEX_MEMBER_COLUMNS,
    REPORT_TYPES,
    AKShareAdapter,
    BaostockAdapter,
    EastMoneyAdapter,
    SourceAdapter,
    classify_source_failure,
    normalize_daily_bar,
    normalize_financial,
    normalize_index_members,
    normalize_symbol,
    validate_frame,
)
from quanauto.enums import SourcePriority
from quanauto.errors import (
    RETRYABLE_SOURCE_FAILURES,
    SOURCE_FAILURE_KINDS,
    SourceAdapterError,
)
from quanauto.models import ValidationReport


def _daily(**overrides):
    """一帧「已经是标准 schema」的日线，用于校验用例。"""
    frame = {
        "symbol": ["600000.SH"],
        "trade_date": [date(2026, 9, 22)],
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [1000.0],
        "amount": [10200.0],
    }
    frame.update(overrides)
    return pd.DataFrame(frame)


def _akshare_raw(**overrides):
    """一帧「东财系源」的原始日线：中文列名 + 一个标准 schema 装不下的列。"""
    frame = {
        "日期": ["2026-09-22"],
        "开盘": [10.0],
        "收盘": [10.2],
        "最高": [10.5],
        "最低": [9.8],
        "成交量": [1234],          # 单位：手
        "成交额": [10200.0],
        "涨跌幅": [2.0],           # 源特有 —— 归一化后必须消失
    }
    frame.update(overrides)
    return pd.DataFrame(frame)


def _eastmoney_income_raw(**overrides):
    """东财**利润表**：列名照 2026-09-24 实测拄，关键是 `REPORTDATE` 没有下划线。

    两张表分开做夹具是刻意的：实测没有任何单一 `reportName` 同时含收入类与资产
    负债类科目，而两者的日期列名还不一样（附录 B12）。夹具若不分开，就会把一个
    实测中不存在的响应形状当成基线。
    """
    frame = {
        "SECURITY_CODE": ["600000"],
        "REPORTDATE": ["2026-06-30"],
        "NOTICE_DATE": ["2026-08-28"],
        "TOTAL_OPERATE_INCOME": [1.0e10],
        "PARENT_NETPROFIT": [2.0e9],
        "WEIGHTAVG_ROE": [12.5],   # 单位：百分数
    }
    frame.update(overrides)
    return pd.DataFrame(frame)


def _eastmoney_balance_raw(**overrides):
    """东财**资产负债表**：另一张报表，日期列名是 `REPORT_DATE`（带下划线）。"""
    frame = {
        "SECURITY_CODE": ["600000"],
        "REPORT_DATE": ["2026-06-30"],
        "NOTICE_DATE": ["2026-08-28"],
        "TOTAL_ASSETS": [3.0e11],
        "TOTAL_EQUITY": [1.5e11],
    }
    frame.update(overrides)
    return pd.DataFrame(frame)


# ── 符号格式：后缀由适配器补，且不许猜 ────────────────────────────────────────
def test_normalize_symbol_covers_the_spellings_sources_actually_use() -> None:
    """`db/data_center.sql` 的 `dc_symbol.symbol` 注释：统一格式，**禁止混用裸代码**。

    源给的写法就这几种（akshare 要裸代码、baostock 要 `sh.600000`、人手工粘的时候
    各种大小写），所以归一化点必须能吃下它们。带上这个用例是因为下游
    （`dc_symbol` 主键、`dc_daily_bar` 主键）都是按这个字符串做的。
    """
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300750") == "300750.SZ"
    assert normalize_symbol("830799") == "830799.BJ"
    assert normalize_symbol("sh.600000") == "600000.SH"
    assert normalize_symbol("600000.sh") == "600000.SH"
    assert normalize_symbol("SH600000") == "600000.SH"
    assert normalize_symbol("600000.SH") == "600000.SH"


@pytest.mark.parametrize("bad", ["60000", "6000000", "600000.XX", "ABCDEF", "", None, 600000])
def test_normalize_symbol_refuses_to_guess(bad) -> None:
    """猜错的代价：`600000` 被归到另一个交易所，代码一样、肉眼看不出来。"""
    with pytest.raises(SourceAdapterError):
        normalize_symbol(bad)


# ── 日线归一化 ───────────────────────────────────────────────────────────────
def test_normalize_daily_bar_outputs_only_the_standard_columns() -> None:
    """契约 §3.2 验收表：「源字段名不得泄漏到输出列」。"""
    frame = normalize_daily_bar(_akshare_raw(), AKSHARE_DAILY_BAR,
                                volume_in_lots=True, symbol="600000")
    assert list(frame.columns) == list(DAILY_BAR_COLUMNS)
    assert "涨跌幅" not in frame.columns
    assert frame["open"].iloc[0] == 10.0
    assert frame["close"].iloc[0] == 10.2
    assert frame["symbol"].iloc[0] == "600000.SH"
    assert type(frame["trade_date"].iloc[0]) is date, "trade_date 必须是 date 类型"


def test_normalize_daily_bar_converts_lots_to_shares() -> None:
    """成交量单位：源给「手」，标准 schema 要「股」，必须 ×100（契约 §2.3）。

    这条不报错、只让成交量类因子整体缩小 100 倍，所以它必须由一条用例钉住。
    同时用 `volume_in_lots=False` 做**控制**：证明差异确实来自这个开关，
    而不是归一化顺手干了别的换算。
    """
    lots = normalize_daily_bar(_akshare_raw(), AKSHARE_DAILY_BAR,
                               volume_in_lots=True, symbol="600000")
    shares = normalize_daily_bar(_akshare_raw(), AKSHARE_DAILY_BAR,
                                 volume_in_lots=False, symbol="600000")
    assert lots["volume"].iloc[0] == 123400.0
    assert shares["volume"].iloc[0] == 1234.0
    assert lots["amount"].iloc[0] == shares["amount"].iloc[0], "成交额不是 手/股 口径，不该被换算"


def test_normalize_daily_bar_requires_a_symbol_source() -> None:
    """源不返回 symbol 列时，**必须**由请求参数回填；两者都没有就是格式不合格。"""
    with pytest.raises(SourceAdapterError) as caught:
        normalize_daily_bar(_akshare_raw(), AKSHARE_DAILY_BAR, volume_in_lots=True)
    assert "symbol" in str(caught.value)


def test_normalize_daily_bar_reports_missing_source_column() -> None:
    """源少给一列（接口改版、限流返回了残帧）时，异常必须点名是哪一列。"""
    raw = _akshare_raw().drop(columns=["成交额"])
    with pytest.raises(SourceAdapterError) as caught:
        normalize_daily_bar(raw, AKSHARE_DAILY_BAR, volume_in_lots=True, symbol="600000")
    assert "成交额" in str(caught.value)


def test_normalize_daily_bar_rejects_non_numeric_volume() -> None:
    """`pd.to_numeric(errors='coerce')` 会把脏值变成 NaN 然后静默写入 —— 这里不许。"""
    raw = _akshare_raw(成交量=["--"])
    with pytest.raises(SourceAdapterError):
        normalize_daily_bar(raw, AKSHARE_DAILY_BAR, volume_in_lots=True, symbol="600000")


# ── 财务归一化：D4 的硬要求 ──────────────────────────────────────────────────
def test_normalize_financial_refuses_to_substitute_period_end_for_announce_date() -> None:
    """契约 §3.2 / 验收表：源不给披露日期时必须抛，**禁止**用 `period_end` 冒充。

    这是最经典的一类未来函数：一份 3 个月后才公开的报表，会在报告期当天就"可见"。
    它不报错，只让回测收益变漂亮。
    """
    raw = _eastmoney_income_raw().drop(columns=["NOTICE_DATE"])
    with pytest.raises(SourceAdapterError) as caught:
        normalize_financial(raw, EASTMONEY_INCOME_FINANCIAL, symbol="600000",
                            report_type="INCOME", roe_is_percent=True)
    assert "announce_date" in str(caught.value)


def test_normalize_financial_takes_report_type_from_the_request() -> None:
    """`report_type` 由**请求参数**给（东财响应里根本没有这一列，实测只给 `REPORT_TYPE_CODE`）。

    它同时是 `dc_financial` 主键的一部分 —— “没给且帧里也没有”必须抛，不许猜一个默认值：
    猜错不会报错，只会让一批数据挂到错误的报表名上。
    """
    frame = normalize_financial(_eastmoney_income_raw(), EASTMONEY_INCOME_FINANCIAL,
                                symbol="600000", report_type="INCOME",
                                roe_is_percent=True)
    assert frame["report_type"].iloc[0] == "INCOME"
    assert "SECURITY_CODE" not in frame.columns, "源字段名泄漏"

    with pytest.raises(SourceAdapterError) as caught:
        normalize_financial(_eastmoney_income_raw(), EASTMONEY_INCOME_FINANCIAL,
                            symbol="600000")
    assert "report_type" in str(caught.value)


def test_normalize_financial_rejects_a_report_type_outside_the_contract_enum() -> None:
    """`ck_dc_fin_report_type` 只认 4 个取值。传错取值必须在归一化这层就炸。

    放行的话，入库时才被 CHECK 拒 —— 错误地点从「调用方写了一个字符串」
    变成「数据中心的写入失败」，排查方向完全不同。
    """
    with pytest.raises(SourceAdapterError) as caught:
        normalize_financial(_eastmoney_income_raw(), EASTMONEY_INCOME_FINANCIAL,
                            symbol="600000", report_type="INCOME_STATEMENT")
    assert "INCOME_STATEMENT" in str(caught.value)


def test_normalize_financial_rescales_roe_from_percent_to_ratio() -> None:
    """ROE 要从小数比率口径对齐。

    契约 §2.3 的 `roe` 是**小数比率、可为负、区间 [-1, 5]**，不是百分数；
    源给 12.5 表示 12.5%，不换算就是 12.5 —— 越过 `ck_dc_fin_roe_range` 的上界，
    入库会直接被 CHECK 拒掉（届时错误地点跑到数据库那一层）。
    """
    frame = normalize_financial(_eastmoney_income_raw(), EASTMONEY_INCOME_FINANCIAL,
                                symbol="600000", report_type="INCOME",
                                roe_is_percent=True)
    assert set(FINANCIAL_REQUIRED_COLUMNS) <= set(frame.columns)
    assert frame["report_type"].iloc[0] in REPORT_TYPES
    assert frame["roe"].iloc[0] == pytest.approx(0.125)
    assert frame["period_end"].iloc[0] == date(2026, 6, 30)
    assert frame["announce_date"].iloc[0] == date(2026, 8, 28)


def test_normalize_financial_keeps_all_standard_subject_columns() -> None:
    """源缺科目的补 NaN 而不是消失：列形状稳定，缺失由 `missing_ratio` 显式报出来。"""
    raw = _eastmoney_balance_raw().drop(columns=["TOTAL_EQUITY"])
    frame = normalize_financial(raw, EASTMONEY_BALANCE_FINANCIAL, symbol="600000",
                                report_type="BALANCE")
    assert list(frame.columns) == list(FINANCIAL_COLUMNS)
    assert pd.isna(frame["total_equity"].iloc[0])


# ── 指数成分股：D5 的区间表 ──────────────────────────────────────────────────
def test_normalize_index_members_keeps_null_effective_to() -> None:
    """`effective_to IS NULL` = 至今仍在成分内，**不能**用哨兵日期填掉。

    `db/data_center.sql` 注释：「查询必须显式处理 NULL，否则 a <= x < NULL 恒为 FALSE」。
    填一个 `9999-12-31` 会让所有"至今仍在成分内"的票在区间查询里被静默排除 ——
    也就是把现成成分股整批判成非成分股，而列里看着"没有空值"，很干净。
    """
    raw = pd.DataFrame({
        "INDEX_CODE": ["000300.SH", "000300.SH"],
        "SECURITY_CODE": ["600000", "000001"],
        "START_DATE": ["2026-01-01", "2026-07-01"],
        "END_DATE": ["2026-06-30", None],
        "WEIGHT": [1.5, 10.0],
    })
    frame = normalize_index_members(raw, EASTMONEY_INDEX_MEMBER,
                                    index_code="000300.SH", weight_is_percent=True)
    assert list(frame.columns) == list(INDEX_MEMBER_COLUMNS)
    assert frame["effective_to"].iloc[1] is None
    assert frame["effective_to"].iloc[0] == date(2026, 6, 30)
    assert frame["weight"].iloc[1] == pytest.approx(0.1)
    assert frame["symbol"].iloc[0] == "600000.SH"


# ── validate：判据与 DDL 的具名 CHECK 一一对应 ───────────────────────────────
def test_validate_accepts_a_clean_daily_bar_frame() -> None:
    """**干净样本**：所有 NEG 用例旁边必须有一条期望 0 报错的用例，
    否则一个「永远红灯」的校验器会让每个 NEG 都看起来完美。"""
    report = validate_frame(_daily())
    assert report.errors == []
    assert report.is_valid is True
    assert report.row_count == 1
    assert set(report.missing_ratio) == set(DAILY_BAR_COLUMNS)
    assert all(value == 0.0 for value in report.missing_ratio.values())


def test_validate_empty_frame_is_not_a_pass() -> None:
    """0 行不是"通过"（契约 §2.4：找不到数据必须显式失败，不得返回空集）。

    这正是本项目反复踩过的假绿：提取为空 ⇒ 所有检查空转 ⇒ 打印「0 问题 PASS」。
    """
    report = validate_frame(_daily().iloc[0:0])
    assert report.is_valid is False
    assert report.row_count == 0
    assert report.errors, "空帧必须带一条硬错误，否则 is_valid=False 说不清为什么"


def test_validate_rejects_duplicate_primary_key() -> None:
    """`pk_dc_daily_bar = (symbol, trade_date, data_version)`。"""
    frame = pd.concat([_daily(), _daily()], ignore_index=True)
    report = validate_frame(frame)
    assert report.is_valid is False
    assert any("主键" in message or "重复" in message for message in report.errors)


def test_validate_rejects_non_positive_price() -> None:
    """`ck_dc_bar_price_positive`：open/high/low/close 均 > 0。"""
    report = validate_frame(_daily(open=[0.0]))
    assert report.is_valid is False
    assert any("> 0" in message or "正" in message for message in report.errors)


def test_validate_rejects_ohlc_order_violation() -> None:
    """`ck_dc_bar_ohlc_order`。这类脏数据不报错就会在策略里变成"低吸高抛"的假信号。"""
    report = validate_frame(_daily(high=[9.0]))
    assert report.is_valid is False
    # 不只要「报了一个错」，还要**能定位**：哪一行、哪个列、什么值。校验跑在成千上万行上，
    # 一条没有定位信息的「OHLC 顺序错了」在工程上等于没报。
    assert any("ck_dc_bar_ohlc_order" in message and "high" in message
               and "9.0" in message for message in report.errors)


def test_validate_rejects_source_field_names_leaking_into_the_frame() -> None:
    """源字段名出现在归一化后的帧里，说明有人绕过了映射表直接 `rename` 或拼接。"""
    frame = _daily()
    frame["TRADE_DATE"] = frame["trade_date"]
    report = validate_frame(frame)
    assert report.is_valid is False
    assert any("TRADE_DATE" in message for message in report.errors)


def test_validate_rejects_roe_outside_the_contract_range() -> None:
    """`ck_dc_fin_roe_range`：`roe` 可为负，区间 [-1, 5]，且**不受** 0~1 通用比率约束。

    12.5 = 忘了把百分数换算成小数比率的典型后果。
    """
    raw = _eastmoney_income_raw()
    frame = pd.DataFrame({
        "symbol": ["600000.SH"],
        "report_type": ["INCOME"],
        "period_end": [date(2026, 6, 30)],
        "announce_date": [date(2026, 8, 28)],
        "revenue": [1.0e10],
        "net_profit": [2.0e9],
        "total_assets": [3.0e11],
        "total_equity": [1.5e11],
        "roe": [12.5],
    })
    assert len(raw) == 1
    assert validate_frame(frame).is_valid is False
    # 控制样本：同一帧只把 roe 改成契约允许的负值，就必须干净通过。
    assert validate_frame(frame.assign(roe=-0.35)).is_valid is True


def test_validate_rejects_index_weight_left_as_a_percentage() -> None:
    """`ck_dc_member_weight_range`：weight 是 0~1 小数，10.0 表示有人忘了 ÷100。"""
    frame = pd.DataFrame({
        "index_code": ["000300.SH"],
        "symbol": ["600000.SH"],
        "effective_from": [date(2026, 1, 1)],
        "effective_to": [None],
        "weight": [10.0],
    })
    report = validate_frame(frame)
    assert report.is_valid is False
    assert any("weight" in message for message in report.errors)


def test_validate_rejects_index_member_with_inverted_effective_range() -> None:
    """`ck_dc_member_effective_range`：`effective_to > effective_from`（NULL 除外）。"""
    frame = pd.DataFrame({
        "index_code": ["000300.SH"],
        "symbol": ["600000.SH"],
        "effective_from": [date(2026, 6, 30)],
        "effective_to": [date(2026, 1, 1)],
        "weight": [0.1],
    })
    assert validate_frame(frame).is_valid is False


# ── 适配器形状与「源侧异常不许逃逸」 ────────────────────────────────────────
@pytest.mark.parametrize("adapter,expected,priority", [
    (AKShareAdapter, "akshare", SourcePriority.PRIMARY),
    (BaostockAdapter, "baostock", SourcePriority.FALLBACK),
    (EastMoneyAdapter, "eastmoney", SourcePriority.FALLBACK),
])
def test_the_three_required_subclasses_exist_with_the_contract_values(
        adapter, expected, priority) -> None:
    """契约 §3.2 的表：三个必做子类、各自的 `source_name` 与 `SourcePriority`。"""
    assert issubclass(adapter, SourceAdapter)
    instance = adapter(fetch=lambda **kwargs: pd.DataFrame())
    assert instance.source_name() == expected
    assert instance.priority is priority


def test_source_side_failure_is_translated_into_source_adapter_error() -> None:
    """源库没装 / 网络炸 / 限流，都必须以 `SourceAdapterError`（DATA_005）出现。

    裸 `ModuleNotFoundError` 逃到上层会被读成"调用方写错了"，于是去查错地方。
    这里用**注入**的假函数造这件事，因此不联网、不依赖源库是否安装。
    """
    def boom(**kwargs):
        raise ModuleNotFoundError("No module named 'akshare'")

    adapter = AKShareAdapter(fetch=boom)
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch_daily_bar(["600000.SH"], date(2026, 9, 1), date(2026, 9, 22))
    assert "akshare" in str(caught.value)
    assert "ModuleNotFoundError" in str(caught.value), "原异常类型名不许丢，否则日志里分不清原因"


def test_validate_is_wired_on_every_adapter() -> None:
    """`validate()` 是契约 §3.2 的抽象方法之一；三个子类都必须真的接上。"""
    empty = pd.DataFrame({name: [] for name in DAILY_BAR_COLUMNS})
    for adapter in (AKShareAdapter, BaostockAdapter, EastMoneyAdapter):
        report = adapter(fetch=lambda **kwargs: pd.DataFrame()).validate(empty)
        assert isinstance(report, ValidationReport)
        assert report.is_valid is False, "空帧不是通过"


# ── ValidationReport 的字段形状（S2 的第一项裁决）─────────────────────────────
def test_validation_report_matches_data_center_contract_fields() -> None:
    """`ValidationReport` 只能是数据中心契约 §3.9 那一版（5 字段）。

    背景：主契约**只引用不定义**它（`validate_no_leakage(self) -> ValidationReport`、
    `validation_report: Optional[ValidationReport]`，都没有类块），
    `开工前缺口清单.md` 把它列进「被引用但从未定义的类型」，并注明由数据中心契约补齐。
    I1 当时塞了一个 3 字段占位版（`is_valid` / `warnings` / `issues`）进去 ——
    本用例把它钉死在契约那一版上：字段名与**顺序**都要对得上，
    否则 `errors` 会被写成 `issues` 这类"看着像、其实不是"的漂移。
    """
    fields = [f.name for f in dataclasses.fields(ValidationReport)]
    assert fields == ["is_valid", "row_count", "errors", "warnings", "missing_ratio"], (
        "ValidationReport 的字段与数据中心契约 §3.9 不一致：实际 %r" % fields
    )


# ── I2a-1：取数失败**可分类**（2026-09-24）────────────────────────────────────
# 背景：契约 §3.9 只规定「源这一侧出问题就抛 SourceAdapterError（DATA_005）」，
# 没规定**怎么区分**。而调用方要做的动作恰恰取决于类别：「源库没装」要去装库、
# 「超时」要退避重试、「映射表对不上」要改代码。三者在旧实现里都是一句字符串，
# 只能靠人读中文 —— 日志能读，重试策略读不了。
#
# 这一组的纪律：**一个类别一个样本**。分类器是一条「从具体到笼统」的 if 链，
# 只测一个坏样本会让后面的分支根本没跑到，却看起来全绿。

#: 分类器的样本表：(异常实例, 期望类别)。**每一类都要有样本**，兜底那档也要 ——
#: 否则「兜底」只是个没被证明过的假设。
_SOURCE_FAILURE_SAMPLES = (
    # 源库没装 —— 动作是「装库」。重试一百次也不会因此装上。
    (ModuleNotFoundError("No module named 'akshare'"), 'SDK_MISSING'),
    # 鉴权被拒（401/403）：换凭证，不是重试。
    (urllib.error.HTTPError('u', 401, 'Unauthorized', {}, None), 'SOURCE_AUTH'),
    (urllib.error.HTTPError('u', 403, 'Forbidden', {}, None), 'SOURCE_AUTH'),
    # 限流（429）：退避后重试**值得**。
    (urllib.error.HTTPError('u', 429, 'Too Many Requests', {}, None),
     'SOURCE_RATE_LIMITED'),
    # 其它 HTTP 状态（5xx 等）：站点自己有问题，重试值得。
    (urllib.error.HTTPError('u', 503, 'Service Unavailable', {}, None),
     'SOURCE_UNREACHABLE'),
    # 超时。注意 `socket.timeout` 就是 `TimeoutError`，且它是 `OSError` 的子类 ——
    # 判定顺序写反的话，这一行会掉进「连不上」那档。
    (socket.timeout('timed out'), 'SOURCE_TIMEOUT'),
    (TimeoutError('timed out'), 'SOURCE_TIMEOUT'),
    (urllib.error.URLError('name resolution failed'), 'SOURCE_UNREACHABLE'),
    (ConnectionResetError('connection reset by peer'), 'SOURCE_UNREACHABLE'),
    # 通了但解析不了：源改了字段名、返回了 HTML 错误页。改映射表，不是重试。
    (KeyError('日期'), 'SOURCE_SCHEMA_MISMATCH'),
    (TypeError('unsupported operand type(s)'), 'SOURCE_SCHEMA_MISMATCH'),
    (ValueError('could not convert string to float'), 'SOURCE_SCHEMA_MISMATCH'),
    (json.JSONDecodeError('Expecting value', '', 0), 'SOURCE_SCHEMA_MISMATCH'),
    # 兜底：没归类的一律 UNKNOWN，**不猜**。原始类型名保留在消息里给人看。
    (RuntimeError('something else entirely'), 'UNKNOWN'),
)

_SAMPLE_IDS = ['%02d-%s-%s' % (index, type(exc).__name__, kind)
               for index, (exc, kind) in enumerate(_SOURCE_FAILURE_SAMPLES)]

#: 「本适配器明说不覆盖这个数据面」的调用 —— `UNSUPPORTED` 类别的**生产者**。
#: 它与上面每一类的区别在动作：不是装库、不是重试、不是改映射，是**换源**。
_UNSUPPORTED_CALLS = (
    ('akshare-financial',
     AKShareAdapter(fetch=lambda **kwargs: pd.DataFrame()),
     lambda adapter: adapter.fetch_financial(['600000.SH'], date(2026, 9, 30))),
    ('akshare-index-members',
     AKShareAdapter(fetch=lambda **kwargs: pd.DataFrame()),
     lambda adapter: adapter.fetch_index_members('000300.SH', date(2026, 9, 30))),
    ('baostock-financial',
     BaostockAdapter(fetch=lambda **kwargs: pd.DataFrame()),
     lambda adapter: adapter.fetch_financial(['600000.SH'], date(2026, 9, 30))),
    ('baostock-index-members',
     BaostockAdapter(fetch=lambda **kwargs: pd.DataFrame()),
     lambda adapter: adapter.fetch_index_members('000300.SH', date(2026, 9, 30))),
    ('eastmoney-daily-bar',
     EastMoneyAdapter(fetch=lambda **kwargs: pd.DataFrame()),
     lambda adapter: adapter.fetch_daily_bar(
         ['600000.SH'], date(2026, 9, 1), date(2026, 9, 30))),
)


def test_failure_taxonomy_is_a_closed_set_of_uppercase_strings() -> None:
    """分类表是闭集，且必须非空 —— 空表的每一条断言都会变成空转。"""
    assert SOURCE_FAILURE_KINDS, "分类表是空的，下面每条断言都在空转"
    for kind in SOURCE_FAILURE_KINDS:
        assert isinstance(kind, str) and kind and kind == kind.upper(), (
            "类别名必须是大写字符串常量：%r" % (kind,))
    assert len(set(SOURCE_FAILURE_KINDS)) == len(SOURCE_FAILURE_KINDS), "类别名重复"


def test_retryable_categories_name_only_known_categories() -> None:
    """「可重试」表里若出现分类表没有的名字，那条规则永远匹配不上 —— 静默失配。"""
    assert RETRYABLE_SOURCE_FAILURES, "可重试表为空 —— 分类存在的全部理由就没了"
    unknown = sorted(set(RETRYABLE_SOURCE_FAILURES) - set(SOURCE_FAILURE_KINDS))
    assert not unknown, "可重试表引用了分类表里没有的类别：%r" % (unknown,)


@pytest.mark.parametrize("exc,expected", _SOURCE_FAILURE_SAMPLES, ids=_SAMPLE_IDS)
def test_classifier_maps_each_failure_to_its_category(exc, expected) -> None:
    assert classify_source_failure(exc) == expected


def test_the_classifier_tests_specific_types_before_general_ones() -> None:
    """判定顺序是这张表的**全部风险**：`socket.timeout` ⊂ `OSError`，
    `HTTPError` ⊂ `URLError` ⊂ `OSError`。顺序反了，最该区分的两类
    （超时 / 限流）会一起掉进「连不上」，而分类表看上去仍然「有那么多类别」。
    """
    assert classify_source_failure(socket.timeout('t')) == 'SOURCE_TIMEOUT', \
        'socket.timeout 被更笼统的 OSError 分支截走了'
    assert classify_source_failure(
        urllib.error.HTTPError('u', 429, 'x', {}, None)) == 'SOURCE_RATE_LIMITED', \
        'HTTPError 被更笼统的 URLError 分支截走了'
    assert classify_source_failure(
        urllib.error.HTTPError('u', 401, 'x', {}, None)) == 'SOURCE_AUTH', \
        '401 与 429 被混成了一类 —— 一个该换凭证，一个该退避'


@pytest.mark.parametrize("kind", SOURCE_FAILURE_KINDS)
def test_retryable_flag_is_derived_from_the_category(kind) -> None:
    """`retryable` 不是自由字段：不传就由类别推出来，推不出来说明类别没登记。"""
    assert SourceAdapterError('x', kind=kind).retryable is (
        kind in RETRYABLE_SOURCE_FAILURES)


def test_an_unknown_category_is_rejected_instead_of_silently_downgraded() -> None:
    """写错的类别必须**当场红**。降级成 UNKNOWN 会让「新加的类别根本没生效」
    和「归类成功」长得一模一样 —— 那正是分类表最容易失效的方式。
    """
    with pytest.raises(ValueError) as caught:
        SourceAdapterError('x', kind='SOURCE_FLUX_CAPACITOR')
    assert 'SOURCE_FLUX_CAPACITOR' in str(caught.value)


def test_call_attaches_source_category_and_retryable_to_the_translated_error() -> None:
    """`_call` 是分类的**唯一接线点**：不接在这里，分类表就是个装饰品。"""
    def boom(**kwargs):
        raise TimeoutError('read timed out')

    adapter = AKShareAdapter(fetch=boom)
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch_daily_bar(['600000.SH'], date(2026, 9, 1), date(2026, 9, 30))
    err = caught.value
    assert err.source == 'akshare', '不知道是哪个源失败，多源并跑时无法定位'
    assert err.kind == 'SOURCE_TIMEOUT'
    assert err.retryable is True
    assert 'TimeoutError' in str(err), \
        '原异常类型名不许丢：类别是粗分类，类型名才是排查线索'


def test_a_source_adapter_error_from_the_fetch_keeps_its_own_category() -> None:
    """`_call` 对 `SourceAdapterError` 是**原样透传**，不是重新分类。

    重新分类会把适配器自己判定好的类别压成 UNKNOWN，于是「该换源」这个
    动作在日志里消失了 —— 而它恰恰是唯一能解释「已经装了库为什么还失败」的线索。
    """
    def boom(**kwargs):
        raise SourceAdapterError('源明说不覆盖这个数据面', source='akshare',
                                 kind='UNSUPPORTED')

    adapter = AKShareAdapter(fetch=boom)
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch_daily_bar(['600000.SH'], date(2026, 9, 1), date(2026, 9, 30))
    assert caught.value.kind == 'UNSUPPORTED'


@pytest.mark.parametrize("label,adapter,call", _UNSUPPORTED_CALLS,
                         ids=[label for label, _, _ in _UNSUPPORTED_CALLS])
def test_capability_the_source_does_not_cover_is_unsupported_and_not_retryable(
        label, adapter, call) -> None:
    """「这一源不覆盖这个数据面」是**装库和重试都解决不了**的失败。

    旧实现把它和「网络炸了」写成同一句字符串，调用方的重试策略于是会对着一个
    永远不会变好的失败反复重试。
    """
    with pytest.raises(SourceAdapterError) as caught:
        call(adapter)
    assert caught.value.kind == 'UNSUPPORTED'
    assert caught.value.retryable is False, '不覆盖的能力重试一万次也不会变成覆盖'


def test_every_declared_category_has_a_producer() -> None:
    """**不许预支类别。** 声明了却没有任何地方会产出的类别是死代码，而且会让
    「已分类」看起来比实际更完整 —— 本仓库对「预支的异常」有同一条纪律。

    这一条同时也是一张**完成度自检表**：以后新增类别时，它要么有分类器分支，
    要么有显式 raise，否则本用例红。
    """
    produced = {classify_source_failure(exc) for exc, _ in _SOURCE_FAILURE_SAMPLES}
    for label, adapter, call in _UNSUPPORTED_CALLS:
        with pytest.raises(SourceAdapterError) as caught:
            call(adapter)
        produced.add(caught.value.kind)
    assert produced == set(SOURCE_FAILURE_KINDS), (
        '分类表与生产者对不上：声明了没人抛的有 %r；抛了没声明的有 %r'
        % (sorted(set(SOURCE_FAILURE_KINDS) - produced),
           sorted(produced - set(SOURCE_FAILURE_KINDS))))


# ── 传输层：唯一网络出口（离线可测，不碰外网） ────────────────────────────────
# 东财没有官方 Python SDK，「怎么发请求」这件事因此落在适配器里。它的正确性不能靠
# 「跑一次线上看看报不报错」来证明 —— 那既不可重复，也会把「本机断网」变成红。
# 做法是：起一个**本机** HTTP 服务，用真实 `urllib` 打过去，把
# urlencode → 请求头 → 状态码 → 解帧 → 分类 整条链路验完。
#
# 这里刻意不 mock `urllib.request.urlopen`：被替换成假函数的传输层验不出
# 「参数拼错了」「UA 没带」这类错误，而那正是这一段代码的**全部内容**。


class _LocalHTTP:
    """最小本机 HTTP 服务：每个请求都喂同一份预设响应，并记下收到的路径与请求头。"""

    def __init__(self, status: int = 200, body: str = '{}',
                 content_type: str = 'application/json') -> None:
        self.requests = []
        owner = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 —— 方法名由 BaseHTTPRequestHandler 约定
                owner.requests.append((self.path, dict(self.headers)))
                payload = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # 别把每个请求都打进测试输出
                pass

        self._server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)

    def __enter__(self) -> "_LocalHTTP":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d/api/data/v1/get' % self._server.server_address[1]
        return self

    def __exit__(self, *exc) -> bool:
        self._server.shutdown()
        self._server.server_close()
        return False


def test_http_get_json_encodes_the_query_and_parses_the_envelope() -> None:
    """请求形状与解帧：参数进 query string，响应的 `result.data` 直接可用。

    响应形状是 **2026-09-24 实测**的（`success` / `result.data` / `result.pages`），
    见 `tools/eastmoney-transport-smoke-report.txt`。
    """
    payload = json.dumps({'success': True, 'result': {'data': [{'A': 1}], 'pages': 2}})
    with _LocalHTTP(body=payload) as server:
        got = ds._http_get_json(server.url, {'reportName': 'RPT_X', 'pageNumber': 2,
                                             'pageSize': 100, 'columns': 'ALL'})
    assert got['result']['data'] == [{'A': 1}]
    path, headers = server.requests[0]
    assert path.startswith('/api/data/v1/get?'), '路径被吃掉了：url + "?" + query 拼错'
    assert 'reportName=RPT_X' in path and 'pageNumber=2' in path
    assert headers.get('User-Agent') == ds.HTTP_USER_AGENT, (
        '不带 UA 的请求会被源当爬虫挡掉 —— 返回的是 HTML，解帧报的是「JSON 解析失败」，'
        '排查方向会被彻底带偏')


@pytest.mark.parametrize("status,expected", [
    (429, 'SOURCE_RATE_LIMITED'),
    (401, 'SOURCE_AUTH'),
    (403, 'SOURCE_AUTH'),
    (503, 'SOURCE_UNREACHABLE'),
])
def test_transport_failures_reach_the_right_category(status, expected) -> None:
    """真传输 → 真适配器 → 真分类器，全程不碰外网。

    「限流」和「凭证错」都会返回非 200，如果传输层就地吞掉状态码写成一句人话，
    这两类就再也分不开了 —— 而调用方对它们的动作**完全相反**（等一会儿 vs 换凭证）。
    """
    with _LocalHTTP(status=status, body='{}') as server:
        url = server.url
        adapter = EastMoneyAdapter(
            fetch=lambda **kwargs: ds._http_get_json(url, {'reportName': 'RPT_X'}))
        with pytest.raises(SourceAdapterError) as caught:
            adapter.fetch_index_members('000300.SH', date(2026, 9, 24))
    assert caught.value.kind == expected
    assert caught.value.source == 'eastmoney'
    assert caught.value.retryable is (expected in RETRYABLE_SOURCE_FAILURES)


def test_a_non_json_body_is_a_schema_mismatch_not_a_retryable_failure() -> None:
    """源返回公告页 / 风控页时给的是 HTML，不是 JSON。

    把它当网络故障去重试，永远也等不到 JSON；类别必须落在「调用方该改请求」那一侧。
    """
    with _LocalHTTP(body='<html>系统繁忙</html>', content_type='text/html') as server:
        url = server.url
        adapter = EastMoneyAdapter(fetch=lambda **kwargs: ds._http_get_json(url, {}))
        with pytest.raises(SourceAdapterError) as caught:
            adapter.fetch_index_members('000300.SH', date(2026, 9, 24))
    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH'
    assert caught.value.retryable is False


def test_a_closed_port_is_unreachable_and_retryable() -> None:
    """上面几条都是「连上了但被打回」，这条补上「根本没连上」。

    端口取法是 bind 到 0 再关掉 —— 拿到的端口号必然没人监听，且**不需要外网**。
    """
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()

    adapter = EastMoneyAdapter(
        fetch=lambda **kwargs: ds._http_get_json('http://127.0.0.1:%d/x' % port, {}))
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch_index_members('000300.SH', date(2026, 9, 24))
    assert caught.value.kind == 'SOURCE_UNREACHABLE'
    assert caught.value.retryable is True


# ── 东财取数：两报表 × 逐页（离线，本机 HTTP 服务） ───────────────────────────
# `_default_fetch` 是**真实会被跑的默认实现**（不像 akshare/baostock 那两个从没被调用
# 验证过），所以它的形状必须有行为测试兜着。这里刻意用本机 HTTP 服务而不是打真接口：
# 「本机断网就变红」的测试最后一定会被跳过，然后就没有人再看它了。
#
# 实测到的响应形状（`tools/eastmoney-transport-smoke-report.txt`）：
#   {'success': bool, 'code': int, 'message': str, 'result': {'data': [...], 'pages': int}}


def _eastmoney_envelope(rows, pages=1, **overrides):
    envelope = {'success': True, 'code': 0, 'message': 'ok',
                'result': {'data': list(rows), 'pages': pages}}
    envelope.update(overrides)
    return json.dumps(envelope)


def _open_against(monkeypatch, server):
    """把模块级端点指到本机服务上 —— 生产 URL 只在一处，替换也只做一处。"""
    monkeypatch.setattr(ds, 'EASTMONEY_DATA_API', server.url)
    return server.url


def _documented_request_params(report_name):
    """生产实现该发的参数。写成一份期望值，是为了让「悄悄少发一个参数」变红。"""
    return {'reportName': report_name, 'columns': 'ALL',
            'filter': '(SECURITY_CODE="600000")',
            'pageSize': str(ds.EASTMONEY_PAGE_SIZE),
            'source': 'WEB', 'client': 'WEB'}


def test_default_fetch_pages_through_the_envelope_and_normalises_the_code(
        monkeypatch) -> None:
    """逐页取回、按页号顺序拼起来；发出去的过滤条件用的是**裸代码**。

    `sh.600000` → `(SECURITY_CODE="600000")` 这一条是重点：源只认裸代码，把带后缀
    的写法发过去会得到一个「查无此股」的正常响应，然后这份数据就静默地缺失了。
    """
    rows = [{'SECURITY_CODE': '600000', 'REPORTDATE': '2026-06-30'}]
    with _LocalHTTP(body=_eastmoney_envelope(rows, pages=2)) as server:
        _open_against(monkeypatch, server)
        frame = EastMoneyAdapter._default_fetch(
            symbol='sh.600000', report_name='RPT_LICO_FN_CPD')
        requests = list(server.requests)

    assert len(requests) == 2, 'pages=2 必须真的走两页；只取第一页会静默丢数据'
    assert list(frame['SECURITY_CODE']) == ['600000', '600000'], '两页都要进结果'
    for index, (path, headers) in enumerate(requests, start=1):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        for key, value in _documented_request_params('RPT_LICO_FN_CPD').items():
            assert query.get(key) == [value], '请求参数 %s 发出去了没：%r' % (key, query)
        assert query.get('pageNumber') == [str(index)], '页号必须递增：%r' % (query,)
        assert headers.get('User-Agent') == ds.HTTP_USER_AGENT


def test_default_fetch_raises_instead_of_truncating_past_the_page_cap(
        monkeypatch) -> None:
    """页数超过上限时**报错**，不是「取满 200 页就收手」。

    截断的后果是数据静默缺失（少掉的那部分看起来和「源本来就没有」一模一样），
    而这类缺失在回测里表现为「某些票在某些报告期没有财报」，查不出来。
    """
    body = _eastmoney_envelope([{'SECURITY_CODE': '600000'}],
                               pages=ds.EASTMONEY_MAX_PAGES + 1)
    with _LocalHTTP(body=body) as server:
        _open_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            EastMoneyAdapter._default_fetch(symbol='600000',
                                            report_name='RPT_LICO_FN_CPD')
        requests = list(server.requests)

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH'
    assert str(ds.EASTMONEY_MAX_PAGES) in str(caught.value)
    assert len(requests) == 1, '发现超限就该停在第 1 页，而不是先乖乖取 200 页'


def test_default_fetch_treats_an_in_band_rejection_as_a_schema_mismatch(
        monkeypatch) -> None:
    """HTTP 200 + `success=false`（错误码在信封里）—— 这是**参数不合法**，不是网络故障。

    重试它一万次也不会让参数变合法；类别必须不可重试，动作才写得成「改请求」。
    这也是唯一能解释「接口通着、状态码 200，却一直失败」的线索 —— 当成网络问题查，
    会一路查到怀疑自己的网线。
    """
    body = _eastmoney_envelope([], success=False, code=9501, message='报表不存在')
    with _LocalHTTP(body=body) as server:
        _open_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            EastMoneyAdapter._default_fetch(symbol='600000',
                                            report_name='RPT_LICO_FN_CPD')

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH', \
        '带内拒绝不是网络故障，更不是「本适配器不覆盖」'
    assert caught.value.retryable is False
    assert '9501' in str(caught.value), '错误码不许丢：那是给人去查源文档的唯一线索'


def test_default_fetch_refuses_an_unregistered_report_name_before_any_request(
        monkeypatch) -> None:
    """未登记的 `reportName` 直接 `UNSUPPORTED`，且**一个请求都不发**。

    取回来也归一化不了（`report_type` 是主键的一部分，猜一个值比不取更坏），
    所以这里在发请求之前就拦住 —— 白跑一趟网络只会让人以为「是源那边没数据」。
    """
    with _LocalHTTP(body=_eastmoney_envelope([])) as server:
        _open_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            EastMoneyAdapter._default_fetch(symbol='600000', report_name='RPT_NOPE')
        requests = list(server.requests)

    assert caught.value.kind == 'UNSUPPORTED'
    assert caught.value.retryable is False
    assert requests == [], '未登记的报表名不该产生任何网络调用'


def test_default_fetch_returns_an_empty_frame_when_the_source_has_no_rows(
        monkeypatch) -> None:
    """源正常回复但没有数据 ⇒ 空帧，**不抛**。

    `errors.py` 的分类表刻意没有「源没给数据」这一类（见 `SOURCE_FAILURE_KINDS`
    上方的注释）：空结果不是失败，是「这一期确实没有」。把它做成异常，调用方就
    分不出「没有数据」和「取数坏了」。
    """
    with _LocalHTTP(body=_eastmoney_envelope([], pages=1)) as server:
        _open_against(monkeypatch, server)
        frame = EastMoneyAdapter._default_fetch(symbol='600000',
                                                report_name='RPT_DMSK_FN_BALANCE')

    assert frame.shape[0] == 0


def test_fetch_financial_returns_one_row_per_report_for_the_same_period() -> None:
    """一个报告期出**两行**（`INCOME` + `BALANCE`），不是把两张报表拼成一行。

    `report_type` 是 `dc_financial` 主键的一部分：拼成一行的话，资产负债类科目会
    挂在一行 `report_type='INCOME'` 上 —— 那行不属于任何真实报表，而账面上
    「字段都填满了」，看起来比两行还完整。
    """
    seen = []

    def fetch(**kwargs):
        seen.append(kwargs['report_name'])
        if kwargs['report_name'] == 'RPT_LICO_FN_CPD':
            return _eastmoney_income_raw()
        return _eastmoney_balance_raw()

    adapter = EastMoneyAdapter(fetch=fetch)
    frame = adapter.fetch_financial(['600000.SH'], date(2026, 6, 30))

    assert seen == ['RPT_LICO_FN_CPD', 'RPT_DMSK_FN_BALANCE'], \
        '两张报表都要取，且顺序固定（页面/日志里的行顺序才可复现）'
    assert len(frame) == 2
    assert sorted(frame['report_type']) == ['BALANCE', 'INCOME']
    assert frame['revenue'].notna().sum() == 1, '收入类科目只该出现在 INCOME 行上'
    assert frame['total_assets'].notna().sum() == 1, '资产负债类科目只该出现在 BALANCE 行上'
    assert frame['roe'].iloc[0] == pytest.approx(0.125), 'ROE 只在利润表那张表上'

    # 报告期筛在**归一化之后**做：别的报告期不是「报错」，是这一期没有数据。
    other = adapter.fetch_financial(['600000.SH'], date(2026, 3, 31))
    assert other.shape[0] == 0
    assert list(other.columns) == list(FINANCIAL_COLUMNS), '空帧也得带标准列'


def test_fetch_financial_refuses_a_report_whose_type_is_not_registered(
        monkeypatch) -> None:
    """`_FINANCIAL_REPORTS` 与 `EASTMONEY_REPORT_TYPE` 漂移时必须炸，且不发请求。

    这两处是同一件事的两半（哪张报表 / 归到哪个 `report_type`），漂移的后果是
    取回来的数据没有 `report_type` 可用 —— 于是要么猜一个值，要么整批丢掉。
    两条都是静默的，所以这里必须硬拦。
    """
    called = []

    def fetch(**kwargs):
        called.append(kwargs)
        return _eastmoney_income_raw()

    adapter = EastMoneyAdapter(fetch=fetch)
    monkeypatch.setattr(EastMoneyAdapter, '_FINANCIAL_REPORTS',
                        (('RPT_NOPE', EASTMONEY_INCOME_FINANCIAL),))
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch_financial(['600000.SH'], date(2026, 6, 30))

    assert caught.value.kind == 'UNSUPPORTED'
    assert called == [], '报表类型登记不上时不该去取数'
