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
    ADJUST_FACTOR_COLUMNS,
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
    TUSHARE_ADJ_FACTOR,
    TUSHARE_ADJ_FACTOR_API,
    TUSHARE_AUTH_CODE,
    AKShareAdapter,
    BaostockAdapter,
    EastMoneyAdapter,
    SourceAdapter,
    TencentAdapter,
    TushareAdapter,
    classify_source_failure,
    normalize_adjust_factor,
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
    # tushare 只有复权因子这一条通道（附录 B21），另外三个面是**本类自己写出来的**
    # raise（不是继承来的）—— 2026-09-29 实测：不写这三条，`TushareAdapter()` 会
    # 当场 `TypeError: Can't instantiate abstract class`。
    ('tushare-daily-bar',
     TushareAdapter(),
     lambda adapter: adapter.fetch_daily_bar(
         ['600000.SH'], date(2026, 9, 1), date(2026, 9, 30))),
    ('tushare-financial',
     TushareAdapter(),
     lambda adapter: adapter.fetch_financial(['600000.SH'], date(2026, 9, 30))),
    ('tushare-index-members',
     TushareAdapter(),
     lambda adapter: adapter.fetch_index_members('000300.SH', date(2026, 9, 30))),
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
    """最小本机 HTTP 服务：每个请求都喂同一份预设响应，并记下请求方法、路径、请求头与请求体。

    GET 与 POST 走**同一段**处理：两条通道的差别只在请求体，而请求体被记在 `bodies`
    里（GET 没有体 ⇒ `b''`）。这样 tushare 那条 POST 通道能验的与东财 GET 一样多。
    """

    def __init__(self, status: int = 200, body: str = '{}',
                 content_type: str = 'application/json') -> None:
        self.requests = []
        self.methods = []
        self.bodies = []
        owner = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _respond(self) -> None:
                length = int(self.headers.get('Content-Length') or 0)
                owner.requests.append((self.path, dict(self.headers)))
                owner.methods.append(self.command)
                owner.bodies.append(self.rfile.read(length) if length else b'')
                payload = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _respond  # noqa: N815 —— 方法名由 BaseHTTPRequestHandler 约定
            do_POST = _respond

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


def _documented_request_params(report_name, date_key, period_end):
    """生产实现该发的参数。写成一份期望值，是为了让「悄悄少发一个参数」变红。

    过滤表达式是**照实测语法逐字写的**（附录 B14），不由实现算出来 —— 从实现算出来的
    期望值只能证明「实现等于它自己」。
    """
    return {'reportName': report_name, 'columns': 'ALL',
            'filter': '(SECURITY_CODE="600000")(%s=\'%s\')' % (date_key, period_end),
            'pageSize': str(ds.EASTMONEY_PAGE_SIZE),
            'source': 'WEB', 'client': 'WEB'}


def test_default_fetch_pages_through_the_envelope_and_normalises_the_code(
        monkeypatch) -> None:
    """逐页取回、按页号顺序拼起来；发出去的过滤条件用的是**裸代码 + 报告期**。

    `sh.600000` → `(SECURITY_CODE="600000")` 这一条是重点：源只认裸代码，把带后缀
    的写法发过去会得到一个「查无此股」的正常响应，然后这份数据就静默地缺失了。
    带报告期（附录 B14 实测的单引号写法）只为**少传数据** —— 一张报表整段历史
    有上百行，而调用方只要一期。它不是正确性依据：筛选在归一化之后**还会**做一遍。
    """
    rows = [{'SECURITY_CODE': '600000', 'REPORTDATE': '2026-06-30'}]
    with _LocalHTTP(body=_eastmoney_envelope(rows, pages=2)) as server:
        _open_against(monkeypatch, server)
        frame = EastMoneyAdapter._default_fetch(
            symbol='sh.600000', report_name='RPT_LICO_FN_CPD',
            period_end=date(2026, 6, 30), date_key='REPORTDATE')
        requests = list(server.requests)

    assert len(requests) == 2, 'pages=2 必须真的走两页；只取第一页会静默丢数据'
    assert list(frame['SECURITY_CODE']) == ['600000', '600000'], '两页都要进结果'
    documented = _documented_request_params('RPT_LICO_FN_CPD', 'REPORTDATE', '2026-06-30')
    for index, (path, headers) in enumerate(requests, start=1):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        for key, value in documented.items():
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
            EastMoneyAdapter._default_fetch(
                symbol='600000', report_name='RPT_LICO_FN_CPD',
                period_end=date(2026, 6, 30), date_key='REPORTDATE')
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
            EastMoneyAdapter._default_fetch(
                symbol='600000', report_name='RPT_LICO_FN_CPD',
                period_end=date(2026, 6, 30), date_key='REPORTDATE')

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH', \
        '带内拒绝不是网络故障，更不是「本适配器不覆盖」'
    assert caught.value.retryable is False
    assert '9501' in str(caught.value), '错误码不许丢：那是给人去查源文档的唯一线索'


def test_default_fetch_refuses_an_unregistered_report_name_before_any_request(
        monkeypatch) -> None:
    """未登记的 `reportName` 直接 `UNSUPPORTED`，且**一个请求都不发**。

    取回来也归一化不了（`report_type` 是主键的一部分，猜一个值比不取更坏），
    所以这里在发请求之前就拦住 —— 白跑一趟网络只会让人以为「是源那边没数据」。

    顺带钉住**守卫次序**：本用例没给 `period_end` / `date_key`，却必须报 `UNSUPPORTED`
    而不是「缺参数」—— 报表名这一步在前。反过来的话，人会去补那两个参数，然后继续
    等一个永远不会来的报表。
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
        frame = EastMoneyAdapter._default_fetch(
            symbol='600000', report_name='RPT_DMSK_FN_BALANCE',
            period_end=date(2026, 6, 30), date_key='REPORT_DATE')

    assert frame.shape[0] == 0


def test_default_fetch_puts_the_period_into_the_filter_with_the_reports_own_key(
        monkeypatch) -> None:
    """请求里的日期过滤用**各自报表的**键名，单引号（附录 B14 实测的语法）。

    两个键名**必须不一样**：写死一个，另一张表就会被源当场拒（`success=false`  +
    `REPORT_DATE列不存在`），而「被拒」与「这张报表本来就没有数据」在调用方看是两种
    完全不同的结论。离线这里量不到源的反应，能钉住的是「键名真的分开了、真的走到了
    URL 上」。
    """
    sent = {}
    for report_name, date_key in (('RPT_LICO_FN_CPD', 'REPORTDATE'),
                                  ('RPT_DMSK_FN_BALANCE', 'REPORT_DATE')):
        with _LocalHTTP(body=_eastmoney_envelope([])) as server:
            _open_against(monkeypatch, server)
            EastMoneyAdapter._default_fetch(
                symbol='600000', report_name=report_name,
                period_end=date(2026, 6, 30), date_key=date_key)
            (path, _headers) = server.requests[0]
        sent[report_name] = urllib.parse.parse_qs(
            urllib.parse.urlsplit(path).query)['filter'][0]

    assert sent == {
        'RPT_LICO_FN_CPD': '(SECURITY_CODE="600000")(REPORTDATE=\'2026-06-30\')',
        'RPT_DMSK_FN_BALANCE': '(SECURITY_CODE="600000")(REPORT_DATE=\'2026-06-30\')',
    }, '每张报表一个键名（附录 B14）：写死一个，另一张表永远取不到数'


def test_default_fetch_refuses_a_missing_period_before_any_request(monkeypatch) -> None:
    """缺 `period_end` / `date_key` ⇒ `ValueError`，且**零请求**。

    这两个参数不可省：少了它们，`filter` 会退化成「整表全取」—— 一个**能用但悄悄
    变慢**的形态，静默降级正是本模块一直在防的（宁可当场报错）。
    用 `ValueError` 而不是 `SourceAdapterError`：这是**调用方**写错，不是源不可用。
    """
    cases = [
        ({'symbol': '600000', 'report_name': 'RPT_LICO_FN_CPD'}, 'period_end'),
        ({'symbol': '600000', 'report_name': 'RPT_LICO_FN_CPD',
          'period_end': date(2026, 6, 30)}, 'date_key'),
        ({'symbol': '600000', 'report_name': 'RPT_LICO_FN_CPD',
          'date_key': 'REPORTDATE'}, 'period_end'),
    ]
    with _LocalHTTP(body=_eastmoney_envelope([])) as server:
        _open_against(monkeypatch, server)
        for kwargs, missing in cases:
            with pytest.raises(ValueError) as caught:
                EastMoneyAdapter._default_fetch(**kwargs)
            assert missing in str(caught.value), '报错要点名缺的是哪一个参数：%r' % (kwargs,)
        requests = list(server.requests)

    assert requests == [], '参数不齐时一个请求都不该发：发了就是拿默认行为去猜'


def test_period_column_takes_the_reports_own_period_source_column() -> None:
    """`_period_column` 取列名表里**唯一**那个 `period_end` 的源列，不猜也不挑。

    两张真实表给出的键名不同，正是「不能写死一个」的来源；而 0 个 / 2 个都是**猜**的
    入口（随便挑一个就是 B12/B13 的老毛病），所以必须拒。
    """
    assert ds._period_column(EASTMONEY_INCOME_FINANCIAL,
                             'RPT_LICO_FN_CPD') == 'REPORTDATE'
    assert ds._period_column(EASTMONEY_BALANCE_FINANCIAL,
                             'RPT_DMSK_FN_BALANCE') == 'REPORT_DATE'

    for bad_map, why in (
            ({'SECURITY_CODE': 'symbol'}, '一个都没有 ⇒ 筛不了'),
            ({'A': 'period_end', 'B': 'period_end'}, '两个 ⇒ 筛哪一列没有唯一答案')):
        with pytest.raises(SourceAdapterError) as caught:
            ds._period_column(bad_map, 'RPT_X')
        assert caught.value.kind == 'UNSUPPORTED', why
        assert caught.value.retryable is False


def test_fetch_financial_returns_one_row_per_report_for_the_same_period() -> None:
    """一个报告期出**两行**（`INCOME` + `BALANCE`），不是把两张报表拼成一行。

    `report_type` 是 `dc_financial` 主键的一部分：拼成一行的话，资产负债类科目会
    挂在一行 `report_type='INCOME'` 上 —— 那行不属于任何真实报表，而账面上
    「字段都填满了」，看起来比两行还完整。
    """
    seen = []

    def fetch(**kwargs):
        seen.append(kwargs)
        if kwargs['report_name'] == 'RPT_LICO_FN_CPD':
            return _eastmoney_income_raw()
        return _eastmoney_balance_raw()

    adapter = EastMoneyAdapter(fetch=fetch)
    frame = adapter.fetch_financial(['600000.SH'], date(2026, 6, 30))

    assert [(kw['report_name'], kw['date_key']) for kw in seen] == [
        ('RPT_LICO_FN_CPD', 'REPORTDATE'),
        ('RPT_DMSK_FN_BALANCE', 'REPORT_DATE')], \
        '两张报表都要取（顺序固定），且各自的日期键名从自己的列名表里取（附录 B14）'
    assert [kw['period_end'] for kw in seen] == [date(2026, 6, 30)] * 2, \
        '请求侧也要带上报告期：只带键名不带值等于没筛'
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


# ── 腾讯日线：位置数组 + 从 end 回溯分页（离线，本机 HTTP 服务） ──────────────
# 腾讯**不在契约 §3.2 那张表里**（那张表只有三行：akshare / baostock / 东财），
# 它是实测出来的第四条通道（附录 B16）：东财的 kline 端点在两个 opener 下都拒连、
# 新浪只有 6 列**没有成交额**，而 `dc_daily_bar.amount` 是 `NOT NULL` —— 没有成交额的
# 源补不上这一列（合成 `amount = volume × close` 是造数据，不叫接入）。
#
# 它与另外三个源在结构上有一处**本质不同**：响应里没有列名，行是**位置数组**。
# 于是「位置映射表写的下标对不对」不能靠肉眼核对，只能靠两件事兜住：
#   ① 单位/口径用**实测过的那一行**做算术复现（下面 `_tencent_row` 就是探针打印出来的
#      那一行，逐字抄的）；
#   ② 行宽守卫（位置 8 一旦漂移，会静默读到别的字段而不报错）。
#
# 实测到的响应形状（`.rounds/_i2b1-probe-report.txt`，判据 X-amount单位 / X-成交额列位 /
# X-复权口径 三条全 OK）：
#   {'code': 0, 'msg': 'ok', 'data': {'sh600000': {'day': [[...11 段...], ...]}}}


class _ScriptedHTTP:
    """本机 HTTP 服务，但**按请求次序换响应体**（分页测试要第二页给出更早的日期）。

    与 `_LocalHTTP` 只差这一处。固定响应的服务测不了翻页：每一页回同一批日期，
    适配器会判「源没在前进」然后停在原处 —— 那正好是另一条要测的分支，但不是这条。
    最后一个响应体会被重复使用，免得「多发了一次请求」变成 IndexError 而不是断言失败。
    """

    def __init__(self, bodies, status: int = 200) -> None:
        self.requests = []
        self.methods = []
        self.bodies = []
        self._bodies = list(bodies)
        owner = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _respond(self) -> None:
                length = int(self.headers.get('Content-Length') or 0)
                owner.requests.append((self.path, dict(self.headers)))
                owner.methods.append(self.command)
                owner.bodies.append(self.rfile.read(length) if length else b'')
                body = owner._bodies.pop(0) if len(owner._bodies) > 1 else owner._bodies[0]
                payload = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _respond  # noqa: N815 —— 方法名由 BaseHTTPRequestHandler 约定
            do_POST = _respond  # tushare 那条通道，同样的按序换体

            def log_message(self, *args):  # 别把每个请求都打进测试输出
                pass

        self._server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)

    def __enter__(self) -> "_ScriptedHTTP":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d/ifzqgtimg/appstock/app/newfqkline/get' \
                   % self._server.server_address[1]
        return self

    def __exit__(self, *exc) -> bool:
        self._server.shutdown()
        self._server.server_close()
        return False


def _tencent_row(trade_date, segments: int = 11) -> list:
    """探针打印出来的那一行（`2024-01-10`，11 段），只换日期。

    逐字抄实测值而不是自己编一组，是为了让**单位换算**这件事在离线测试里也能复现：
    222409.00（手）× 100 = 22,240,900 股；14669.59（万元）× 10000 = 146,695,900 元；
    两者相除 = 6.5958 ∈ [low=6.57, high=6.63] —— 探针的判据就是这个算式。
    自己编一组数就复现不出来了，而「换算系数写反」只表现为**金额差 100 倍**。

    位置 6 实测是 `{}`、7 是 `'0.08'`、9/10 是 `'0.00'`：**四段都落不进契约 schema**，
    所以映射表里没有它们（多出来的列不允许跟着产出）。留下它们是刻意的 —— 它们
    就是「位置 6 被误映射成 amount」时会当场暴露的那批值。
    """
    row = [trade_date, '6.61', '6.57', '6.63', '6.57', '222409.00', {}, '0.08',
           '14669.59', '0.00', '0.00']
    return row[:segments]


def _tencent_envelope(rows, code: str = 'sh600000') -> str:
    return json.dumps({'code': 0, 'msg': 'ok', 'data': {code: {'day': list(rows)}}})


def _open_kline_against(monkeypatch, server) -> str:
    """把模块级端点指到本机服务上 —— 生产 URL 只在一处，替换也只做一处。"""
    monkeypatch.setattr(ds, 'TENCENT_KLINE_API', server.url)
    return server.url


def _tencent_params(server):
    """每个请求的 `param` 值，按请求顺序。"""
    return [urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['param'][0]
            for path, _ in server.requests]


def test_tencent_param_is_end_anchored_and_keeps_the_trailing_segment() -> None:
    """请求串的形状：`sh600000,day,<anchor>,<anchor>,320,` —— **必须是 6 段**。

    实测（`_tencent-shape-probe.txt`）：把 `start` 段省掉的 5 段写法被带内拒
    （`{'code': 0, 'msg': 'param error'}`、`data=[]`）—— 「少一段」不是少一点数据，
    是根本取不到东西。**这条断言曾经写成 `== 'sh600000,day,2024-01-10,320,'`
    并且通过了**：那是我按自己的意图压缩了形状，再用断言把我的字符串验一遍，
    不是测量。真实取数冒烟把它抓出来了。

    尾段空着 = 不复权（`qfq` ⇒ 同交易日 `5.42` 而不是 `6.57`，照抄进不复权列就是
    前视偏差），第 3、4 段都写 anchor：`start` 实测被忽略（V1/V4/V5/V6 四种写法回
    逐字节相同的 321 行），但万一源开始认它，`start == end == anchor` 的语义仍然
    与翻页一致，而填窗口起点会在翻页时变成反向区间（V3 实测被接受、回窗口外的行）。
    """
    param = ds._tencent_param('sh600000', date(2024, 1, 10), ds.TENCENT_PAGE_ROWS)

    assert param == 'sh600000,day,2024-01-10,2024-01-10,320,'
    assert param.count(',') == 5, '段数变了：源只认 6 段的位置串'
    assert param.split(',')[:2] == ['sh600000', 'day']
    assert param.split(',')[5] == '', '尾段不能写成 qfq（那是复权价）'


def test_tencent_adapter_declares_itself_as_a_fallback_source() -> None:
    """契约表里没有它，但它必须是**同一套接口**上的一个可替换实现。

    `priority` 复用 `FALLBACK` 是因为枚举只有 PRIMARY / FALLBACK 两值 —— 它显然
    不是主源（主源是契约表里那三个）。写成 PRIMARY 会让「同一天该信哪个源」出现
    两个答案，而这类冲突最终表现为「同一天的价格在两块屏幕上不一样」。
    """
    adapter = TencentAdapter()

    assert adapter.source_name() == 'tencent'
    assert adapter.priority is SourcePriority.FALLBACK
    assert isinstance(adapter, SourceAdapter)


def test_tencent_default_fetch_sends_exactly_one_param_and_the_documented_ua(
        monkeypatch) -> None:
    """真实取数请求：只有 `param` 一个查询参数，且带上 UA。"""
    with _ScriptedHTTP([_tencent_envelope([_tencent_row('2024-01-10')])]) as server:
        _open_kline_against(monkeypatch, server)
        frame = TencentAdapter._default_fetch(symbol='sh.600000',
                                              start=date(2024, 1, 10),
                                              end=date(2024, 1, 10))
        requests = list(server.requests)

    assert len(requests) == 1, '起止同一天 ⇒ 一页就覆盖了，不该多翻'
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0][0]).query)
    assert set(query) == {'param'}, '多发的参数没人验过：%r' % (sorted(query),)
    assert _tencent_params(server) == ['sh600000,day,2024-01-10,2024-01-10,320,']
    assert requests[0][1].get('User-Agent') == ds.HTTP_USER_AGENT
    assert list(frame.columns) == [str(index) for index in range(11)], (
        '`_default_fetch` 交出去的是**位置数组帧**（列名是下标），归一化在 '
        '`fetch_daily_bar` 里做 —— 这里写死 11 就是「行宽由源决定」那一件事')


def test_tencent_default_fetch_applies_both_measured_unit_conversions(
        monkeypatch) -> None:
    """两个换算系数各自 ×100 / ×10000，用探针那一行的算术复现。

    这两条**必须能分别失效**：合成一个开关的话，「换错一个」和「两个都没换」
    在结果上再也分不出来（一个差 100 倍，一个差 1e6 倍，但都只是「数不对」）。
    断言里带上与 low/high 的关系，是因为单看体积/金额的绝对值说不出哪个系数错了，
    而 `amount ÷ volume` 落在当日价格区间内这一条**同时**钉住了两个系数。
    """
    with _ScriptedHTTP([_tencent_envelope([_tencent_row('2024-01-10')])]) as server:
        _open_kline_against(monkeypatch, server)
        frame = TencentAdapter().fetch_daily_bar(['600000.SH'], date(2024, 1, 10),
                                                 date(2024, 1, 10))

    row = frame.iloc[0]
    assert row['volume'] == pytest.approx(222409.00 * 100), '成交量单位是「手」⇒ ×100'
    assert row['amount'] == pytest.approx(14669.59 * 10000), '成交额单位是「万元」⇒ ×10000'
    vwap = row['amount'] / row['volume']
    assert row['low'] <= vwap <= row['high'], (
        '两个系数只要有一个错，反推均价就会落到当日区间外（实测 6.5958）')
    assert row['close'] == pytest.approx(6.57), '要的是不复权原始价（qfq 那份是 5.42）'
    assert row['symbol'] == '600000.SH', '源不返回标的列 ⇒ 用请求参数回填归一化后的代码'
    assert row['trade_date'] == date(2024, 1, 10)


def test_tencent_default_fetch_truncates_locally_because_start_is_ignored(
        monkeypatch) -> None:
    """源把 `start` 之前的行也回了 ⇒ 适配器**自己裁**，且不把 `start` 发出去。

    这是「`start` 不在请求串里」的代价与它的补偿：少了那条参数，就多了一步本地裁剪。
    少了这一步，入库的数据里会出现**契约区间之外**的交易日 —— 它看起来完全正常。
    """
    rows = [_tencent_row('2024-01-08'), _tencent_row('2024-01-09'),
            _tencent_row('2024-01-10'), _tencent_row('2024-01-11')]
    with _ScriptedHTTP([_tencent_envelope(rows)]) as server:
        _open_kline_against(monkeypatch, server)
        frame = TencentAdapter().fetch_daily_bar(['600000.SH'], date(2024, 1, 9),
                                                 date(2024, 1, 10))
        requests = list(server.requests)

    assert len(requests) == 1
    assert list(frame['trade_date']) == [date(2024, 1, 9), date(2024, 1, 10)], (
        '区间外的 01-08 / 01-11 必须被裁掉')


def test_tencent_default_fetch_pages_backwards_until_the_window_is_covered(
        monkeypatch) -> None:
    """`start` 早于第一页的最早日期 ⇒ 从「最早日期 - 1」继续往回翻，直到覆盖。

    同时验去重：第二页刻意回了一行第一页给过的日期。不去重的话同一 (symbol,
    trade_date) 会出现两行，而 `dc_daily_bar` 的主键是 (symbol, trade_date) ——
    重复行在入库那一步才会炸，离这里很远。
    """
    first = [_tencent_row('2024-01-11'), _tencent_row('2024-01-10')]
    second = [_tencent_row('2024-01-05'), _tencent_row('2024-01-04'),
              _tencent_row('2024-01-10')]
    with _ScriptedHTTP([_tencent_envelope(first),
                        _tencent_envelope(second)]) as server:
        _open_kline_against(monkeypatch, server)
        frame = TencentAdapter().fetch_daily_bar(['600000.SH'], date(2024, 1, 4),
                                                 date(2024, 1, 11))

    assert _tencent_params(server) == ['sh600000,day,2024-01-11,2024-01-11,320,',
                                       'sh600000,day,2024-01-09,2024-01-09,320,'], (
        '第二页的 end 必须是「上一页最早日期 - 1」，而不是原样重发')
    assert list(frame['trade_date']) == [date(2024, 1, 4), date(2024, 1, 5),
                                         date(2024, 1, 10), date(2024, 1, 11)], (
        '重复的那一行只能出现一次，且结果按日期有序')


def test_tencent_default_fetch_stops_when_the_source_stops_advancing(
        monkeypatch) -> None:
    """源不再给新日期 ⇒ 立刻停，**不空转 40 页**。

    没有这个闸门，一个「永远回同一批日期」的源会被翻 `TENCENT_MAX_PAGES` 次，
    而每次都是真实网络请求 —— 限流就是这么来的。停下来之后仍然走「没覆盖到
    start ⇒ 抛」的判据，所以停下来不等于静默接受。
    """
    body = _tencent_envelope([_tencent_row('2024-01-10')])
    with _ScriptedHTTP([body]) as server:
        _open_kline_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TencentAdapter._default_fetch(symbol='600000.SH',
                                          start=date(2024, 1, 1),
                                          end=date(2024, 1, 10))
        requests = list(server.requests)

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH'
    assert caught.value.retryable is False
    assert len(requests) == 2, '第 2 页发现没有新行就该停（实际发了 %d 次）' % len(requests)
    assert '2024-01-01' in str(caught.value), '报错要说清是哪个边界没被覆盖'


def test_tencent_default_fetch_raises_instead_of_truncating_past_the_page_cap(
        monkeypatch) -> None:
    """页数撞上限而左边界仍未覆盖 ⇒ **抛**，不截断。

    数据静默缺失是最坏的一种失败：少掉的交易日看起来和「那几天没开市」一模一样，
    而回测里的表现是「收益曲线更漂亮」。
    """
    end = date(2024, 1, 10)
    bodies = [_tencent_envelope([_tencent_row((pd.Timestamp(end)
                                               - pd.Timedelta(days=index)).date()
                                              .isoformat())])
              for index in range(ds.TENCENT_MAX_PAGES)]
    with _ScriptedHTTP(bodies) as server:
        _open_kline_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TencentAdapter._default_fetch(symbol='600000.SH',
                                          start=date(1990, 1, 1), end=end)
        requests = list(server.requests)

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH'
    assert str(ds.TENCENT_MAX_PAGES) in str(caught.value)
    assert len(requests) == ds.TENCENT_MAX_PAGES, (
        '上限就是上限：不能少翻（那是静默截断）也不能多翻（那是没闸门）')
    assert _tencent_params(server)[0] == 'sh600000,day,2024-01-10,2024-01-10,320,'


def test_tencent_default_fetch_refuses_a_qfq_only_response(monkeypatch) -> None:
    """只给 `qfqday`（复权）时必须炸，不能「有数据就拿」。

    带 fq 的那份实测是 `close=5.42`、不复权是 `6.57` —— 差 20%。把它写进不复权列
    是日线级别的前视偏差，而且从库里**看不出来**（列名一样、类型一样、区间合法）。
    """
    body = json.dumps({'code': 0, 'msg': 'ok',
                       'data': {'sh600000': {'qfqday': [_tencent_row('2024-01-10', 10)]}}})
    with _ScriptedHTTP([body]) as server:
        _open_kline_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TencentAdapter._default_fetch(symbol='600000.SH',
                                          start=date(2024, 1, 10),
                                          end=date(2024, 1, 10))
        requests = list(server.requests)

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH'
    assert caught.value.retryable is False
    assert len(requests) == 1


@pytest.mark.parametrize("label,body,needle", [
    ('in-band-bad-params', json.dumps({'code': 1, 'msg': 'bad params'}), 'bad params'),
    ('data-missing', json.dumps({'code': 0, 'msg': 'ok'}), 'data'),
    ('no-day-key', json.dumps({'code': 0, 'msg': 'ok',
                               'data': {'sh600000': {'version': '11.0'}}}), 'day'),
    ('day-not-a-list', json.dumps({'code': 0, 'msg': 'ok',
                                   'data': {'sh600000': {'day': {}}}}), 'day'),
])
def test_tencent_schema_mismatches_are_not_retryable(label, body, needle,
                                                     monkeypatch) -> None:
    """四类带内拒绝先各自触发一次：**一项一个样本**。

    合在一个样本里不行：第一项 `raise` 之后后面的分支根本没跑，于是「四类都拦住了」
    这个结论只对第一类成立。四类都落在「调用方该改请求」那一侧 —— 重试只会把
    同一个错误再问一遍，而在重试策略里它会被当成网络抖动。

    `no-day-key` 那一类值得单独看一眼：实测到的**版本号信封**（`data` 里有键、
    但没有 `day`）外面看毫无异样，所以「先看 `data` 在不在」这个判据在这里是错的 ——
    必须一路走到 `day`。
    """
    with _ScriptedHTTP([body]) as server:
        _open_kline_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TencentAdapter._default_fetch(symbol='600000.SH',
                                          start=date(2024, 1, 10),
                                          end=date(2024, 1, 10))
        requests = list(server.requests)

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH', label
    assert caught.value.retryable is False, label
    assert needle in str(caught.value), label
    assert len(requests) == 1, '带内拒绝要当场停，别翻页'


def test_tencent_default_fetch_rejects_a_row_shorter_than_the_position_map(
        monkeypatch) -> None:
    """行宽不足 ⇒ 抛。位置数组最危险的失败就是**这一条**：

    源少给/换了位置之后，位置 8 会指向另一个字段，而 `_to_numbers` 照样能解析出
    一个数 —— 于是「成交额」变成了别的东西，全程不报错。实测的两种长度（11 段 /
    10 段）都在 9 以上，所以门槛取映射表里最大下标 + 1，而不是「看着差不多」。
    """
    short = _tencent_row('2024-01-10')[:5]
    with _ScriptedHTTP([_tencent_envelope([short])]) as server:
        _open_kline_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TencentAdapter._default_fetch(symbol='600000.SH',
                                          start=date(2024, 1, 10),
                                          end=date(2024, 1, 10))

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH'
    assert str(ds._TENCENT_MIN_WIDTH) in str(caught.value)


def test_tencent_default_fetch_accepts_the_measured_shorter_row(monkeypatch) -> None:
    """10 段的行（带复权那份的长度）必须**被接受**。

    只测「短了就炸」会把门槛推向「越长越好」，而实测过 10 段是正常形状 ——
    把正常形状判成坏形状，等于这条通道在某些股票上直接不可用。
    """
    with _ScriptedHTTP([_tencent_envelope([_tencent_row('2024-01-10', 10)])]) as server:
        _open_kline_against(monkeypatch, server)
        frame = TencentAdapter().fetch_daily_bar(['600000.SH'], date(2024, 1, 10),
                                                 date(2024, 1, 10))

    assert len(frame) == 1
    assert frame['amount'].iloc[0] == pytest.approx(14669.59 * 10000)


def test_tencent_default_fetch_rejects_a_reversed_window_before_any_request() -> None:
    """区间反了 ⇒ 抛，且**一个请求都不发**。没有本机服务就是断言本身。"""
    with pytest.raises(SourceAdapterError) as caught:
        TencentAdapter()._default_fetch(symbol='600000.SH',
                                        start=date(2024, 1, 10),
                                        end=date(2024, 1, 1))

    assert caught.value.kind == 'UNSUPPORTED'
    assert caught.value.retryable is False


def test_tencent_fetch_daily_bar_returns_the_standard_columns_for_every_symbol(
        monkeypatch) -> None:
    """多标的：逐个取数、拼成一帧，且**每个标的的响应键都不同**。

    响应键是**带前缀的代码**（`sh600000`），而请求参数里也是它 —— 两者只要有一处
    写错，源回的就是「没有这个键」，而那条错误长得很像「这段区间没数据」。
    """
    bodies = [_tencent_envelope([_tencent_row('2024-01-10')], code='sh600000'),
              _tencent_envelope([_tencent_row('2024-01-10')], code='sz000001')]
    with _ScriptedHTTP(bodies) as server:
        _open_kline_against(monkeypatch, server)
        frame = TencentAdapter().fetch_daily_bar(['600000.SH', '000001.SZ'],
                                                 date(2024, 1, 10),
                                                 date(2024, 1, 10))

    assert _tencent_params(server) == ['sh600000,day,2024-01-10,2024-01-10,320,',
                                       'sz000001,day,2024-01-10,2024-01-10,320,']
    assert list(frame.columns) == list(DAILY_BAR_COLUMNS), '列的集合与顺序都是契约的'
    assert list(frame['symbol']) == ['600000.SH', '000001.SZ']


def test_tencent_fetch_daily_bar_returns_an_empty_frame_when_the_source_has_no_rows(
        monkeypatch) -> None:
    """`day: []` ⇒ 空帧、**不抛**（与东财那条同口径：空结果不是失败）。

    同时钉住一件容易漏的事：空帧也必须带标准列。少了列的下游会报一个与「没数据」
    毫不相干的 `KeyError`，排查方向会被彻底带偏。
    """
    with _ScriptedHTTP([_tencent_envelope([])]) as server:
        _open_kline_against(monkeypatch, server)
        frame = TencentAdapter().fetch_daily_bar(['600000.SH'], date(2024, 1, 1),
                                                 date(2024, 1, 10))

    assert frame.shape[0] == 0
    assert list(frame.columns) == list(DAILY_BAR_COLUMNS)


def test_tencent_capabilities_outside_daily_bar_are_unsupported() -> None:
    """财务 / 指数成分股：这一源没有，`UNSUPPORTED` 且不可重试。"""
    adapter = TencentAdapter()
    with pytest.raises(SourceAdapterError) as financial:
        adapter.fetch_financial(['600000.SH'], date(2024, 6, 30))
    with pytest.raises(SourceAdapterError) as members:
        adapter.fetch_index_members('000300.SH', date(2024, 6, 28))

    assert financial.value.kind == 'UNSUPPORTED'
    assert financial.value.retryable is False
    assert members.value.kind == 'UNSUPPORTED'
    assert members.value.retryable is False


# ── I2c：tushare 复权因子通道（2026-09-29 接；离线整条验，不装 SDK）──────────
# 本机**刻意不装** `tushare`：这条通道走 stdlib 的 POST，所以能像东财/腾讯一样起一个
# 本机 HTTP 服务，把「JSON 体 → Content-Type → 解帧 → 分类 → 归一化」整条验完。
# 装 SDK 反而会多出一种「本机没装 ⇒ SOURCE_SDK_MISSING」的失败。
#
# 它与另外四个源有一处**本质不同**：成功与失败**都是 HTTP 200**，错误码在 body 里。
# 于是 `classify_source_failure`（它读 `HTTPError.code`）在这里永远看不到鉴权失败 ——
# 判据只能写在解帧之后，这也是它没有复用东财那条 GET 出口的原因。
#
# 凭证纪律：下面这个令牌是**合成的**，断言只检查「它有没有进请求体的 `token` 键」，
# **不打印令牌值**；真令牌只存在于环境变量 / `.env`（本文件不读它，也不该读它）。

#: 合成令牌 —— 长得不像任何真令牌，且只在本文件里出现。
_SYNTHETIC_TOKEN = 'SYNTHETIC-NOT-A-REAL-TOKEN'

#: tushare `adj_factor` 的响应列（实测）。顺序由源决定，实现按名字映射。
#: 与 `TUSHARE_ADJ_FACTOR` 的**键序**必须一致 —— 用例拼行时按它写，
#: 所以有一处断言把两者钉在一起（否则「帧的列名」与「映射表」会各说各话）。
_TUSHARE_FIELDS = ['ts_code', 'trade_date', 'adj_factor']

#: 契约 §3.2 的 7 个抽象方法（2026-09-29 实测 `SourceAdapter.__abstractmethods__`；
#: 2026-10-01 追加 `fetch_dividend`，附录 B22）。必须保持**字典序**：
#: 下面的断言用 `tuple(sorted(...))` 与它比，写成别的顺序只会报一句看不懂的差异。
_ABSTRACT_METHODS = ('fetch_adjust_factor', 'fetch_daily_bar', 'fetch_dividend',
                     'fetch_financial', 'fetch_index_members', 'source_name', 'validate')


@pytest.fixture
def tushare_token_env(monkeypatch) -> str:
    """每个 tushare 用例都先塞一个**合成**令牌。

    不加这个，`tushare_token()` 会去读**项目根**的 `.env` —— 于是「这台机器上有没有
    真凭证」会决定用例走哪条分支：没配凭证的机器上，所有 tushare 用例都会在第一行
    就抛 `SOURCE_AUTH`，而它们看起来仍然像「验过了」。
    """
    monkeypatch.setenv(ds.TUSHARE_TOKEN_ENV, _SYNTHETIC_TOKEN)
    return _SYNTHETIC_TOKEN


def _tushare_envelope(items, *, fields=None, code=0, msg='ok', has_more=False) -> str:
    """tushare 信封。**成功与失败都是 HTTP 200**，所以 `code` 由这里给。"""
    return json.dumps({'code': code, 'msg': msg, 'request_id': 'req-1', 'detail': '',
                       'data': {'count': len(items),
                                'fields': list(_TUSHARE_FIELDS if fields is None else fields),
                                'items': list(items), 'has_more': has_more}})


def _tushare_rows(*factors, ts_code: str = '600000.SH') -> list:
    """按 `_TUSHARE_FIELDS` 顺序的整行：`[ts_code, trade_date, adj_factor]`。

    参数名刻意叫 `ts_code` 而不是 `code`：`_tushare_envelope(code=...)` 那个 `code`
    是**tushare 的状态码**，同名会让「造一只股票的行」静默变成「造一个字符串状态码」
    （实测踩过一次：信封的 code 变成 `'600000.SH'` ⇒ 解帧报 UNKNOWN）。
    """
    return [[ts_code, '202401%02d' % (2 + index), factor]
            for index, factor in enumerate(factors)]


def _open_tushare_against(monkeypatch, server) -> str:
    """把模块级端点指到本机服务上 —— 生产 URL 只在一处，替换也只做一处。"""
    monkeypatch.setattr(ds, 'TUSHARE_API', server.url)
    return server.url


def _tushare_body(server, index: int = 0) -> dict:
    """第 `index` 个请求的 JSON 体。**只看键与形状，不打印令牌值。**"""
    return json.loads(server.bodies[index].decode('utf-8'))


def test_tushare_posts_the_documented_body_and_takes_the_token_from_the_environment(
        monkeypatch, tushare_token_env) -> None:
    """请求形状：**POST** 一份 JSON，四个键 `api_name` / `token` / `params` / `fields`。

    这里钉住的每一条，发错了都是**静默**的：用 GET 会得到 405 或一个空信封；把参数
    塞进 query string 会得到「参数不全」；`start_date` 写成 `2024-01-02` 会得到一个
    **HTTP 200 的失败响应**（code 非 0，msg 说日期格式错）。所以形状只能逐条对实测。

    端点本身也是一条实测事实：它不是配置项，也不是从文档抄的（下面那条断言在
    替换之前读它）。
    """
    assert ds.TUSHARE_API == 'https://api.tushare.pro', '端点不是实测值就该有人重测'
    with _LocalHTTP(body=_tushare_envelope(_tushare_rows(1.0, 1.1))) as server:
        _open_tushare_against(monkeypatch, server)
        frame = TushareAdapter().fetch_adjust_factor(['600000.SH'], date(2024, 1, 2),
                                                     date(2024, 1, 10))

    assert server.methods == ['POST'], 'tushare 不认 GET'
    _, headers = server.requests[0]
    assert headers.get('Content-Type') == 'application/json', (
        '不带 Content-Type 的 POST 会被源当成表单；错误信息离真因很远')
    assert headers.get('User-Agent') == ds.HTTP_USER_AGENT

    body = _tushare_body(server)
    assert body['api_name'] == TUSHARE_ADJ_FACTOR_API
    assert body['params'] == {'ts_code': '600000.SH', 'start_date': '20240102',
                              'end_date': '20240110'}, (
        'tushare 要 `YYYYMMDD` 的无分隔日期；写成 ISO 会得到一个 code 非 0 的"成功响应"')
    assert body['fields'] == ''
    assert body['token'] == _SYNTHETIC_TOKEN, 'tushare 没有 Authorization 头，令牌只在请求体里'

    assert list(frame.columns) == list(ADJUST_FACTOR_COLUMNS), '列的集合与顺序都是契约的'
    assert frame['adjust_factor'].tolist() == [1.0, 1.1]
    assert frame['trade_date'].tolist() == [date(2024, 1, 2), date(2024, 1, 3)]
    assert frame['symbol'].tolist() == ['600000.SH', '600000.SH']


def test_tushare_rejected_credentials_are_auth_failures_and_never_echo_the_token(
        monkeypatch, tushare_token_env) -> None:
    """`code=40101`（实测的空/错令牌）⇒ `SOURCE_AUTH`，且报错里**只有变量名**。

    这一条正是「不能套 `classify_source_failure`」的落地：鉴权失败是 HTTP 200，
    分类器那条路看不到它，所以判据写在解帧之后。报错文本会被贴进日志 / issue，
    因此它**不能**带令牌值 —— 而「不带」这件事只能靠一条断言守着。
    """
    with _LocalHTTP(body=_tushare_envelope([], code=int(TUSHARE_AUTH_CODE),
                                           msg='抱歉，您没有访问该接口的权限')) as server:
        _open_tushare_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TushareAdapter().fetch_adjust_factor(['600000.SH'], date(2024, 1, 2),
                                                 date(2024, 1, 10))

    assert caught.value.kind == 'SOURCE_AUTH'
    assert caught.value.retryable is False, '换凭证不是重试能解决的事'
    assert ds.TUSHARE_TOKEN_ENV in str(caught.value), '报错要说清该配哪个变量'
    assert _SYNTHETIC_TOKEN not in str(caught.value), '令牌值不许出现在报错里'


def test_tushare_an_unmapped_code_is_unknown_rather_than_guessed(
        monkeypatch, tushare_token_env) -> None:
    """其余非 0 码 ⇒ `UNKNOWN`，并把 `msg` 带出来，**不猜**一个映射。

    「积分不足 / 无权限 / 接口下线」这些码本机都没观察到 —— 编一个映射比报 UNKNOWN
    更坏：一个猜出来的类别会让人按错误的动作去修（去重试，或者去换凭证）。
    """
    with _LocalHTTP(body=_tushare_envelope([], code=2002, msg='积分不足')) as server:
        _open_tushare_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TushareAdapter().fetch_adjust_factor(['600000.SH'], date(2024, 1, 2),
                                                 date(2024, 1, 10))

    assert caught.value.kind == 'UNKNOWN'
    assert '积分不足' in str(caught.value), '码不认识时 msg 是唯一的线索，必须带出来'


def test_tushare_has_more_is_refused_instead_of_truncated(
        monkeypatch, tushare_token_env) -> None:
    """`has_more=true` ⇒ 抛。**宁可报错也不能只留第一页。**

    静默截断会让「这段区间的因子少了一截」在库里长得像「这段时间就是没数据」 ——
    前者的后果是用错的复权价算收益，后者只是不复权。本迭代不实现翻页（tushare 的
    翻页口径与腾讯的倒序翻页不同，没实测过就不写），所以这是一个响亮的未收口项。
    """
    with _LocalHTTP(body=_tushare_envelope(_tushare_rows(1.0), has_more=True)) as server:
        _open_tushare_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TushareAdapter().fetch_adjust_factor(['600000.SH'], date(2024, 1, 2),
                                                 date(2024, 1, 10))

    assert caught.value.kind == 'UNKNOWN'
    assert 'has_more' in str(caught.value)
    assert len(server.requests) == 1, '报了 has_more 就该当场停，不是再试一次'


def test_tushare_empty_items_keeps_the_source_column_names(
        monkeypatch, tushare_token_env) -> None:
    """空区间 ⇒ 空帧**且列名完整**，**不抛**（与东财/腾讯同一口径：空结果不是失败）。

    列名不能省：`normalize_adjust_factor` 先按源列名选列，一个连列名都没有的空帧会撞
    「缺必需源列」那条分支，报出一句与「这段区间没有因子」毫不相干的话。
    """
    with _LocalHTTP(body=_tushare_envelope([])) as server:
        _open_tushare_against(monkeypatch, server)
        frame = TushareAdapter().fetch_adjust_factor(['600000.SH'], date(2024, 1, 2),
                                                     date(2024, 1, 10))

    assert frame.shape[0] == 0
    assert list(frame.columns) == list(ADJUST_FACTOR_COLUMNS)


@pytest.mark.parametrize("label,body,needle", [
    ('not-an-object', '[1, 2, 3]', 'list'),
    ('data-not-an-object', json.dumps({'code': 0, 'data': []}), 'list'),
    ('fields-not-a-list', json.dumps({'code': 0, 'data': {'fields': {}, 'items': []}}),
     'fields'),
    ('items-not-a-list', json.dumps({'code': 0, 'data': {'fields': [], 'items': {}}}),
     'items'),
])
def test_tushare_envelope_shape_mismatches_are_not_retryable(
        label, body, needle, monkeypatch, tushare_token_env) -> None:
    """四类信封变形**一项一个样本**。

    合在一个样本里不行：第一项 `raise` 之后后面的分支根本没跑，于是「四类都拦住了」
    这个结论只对第一类成立。四类都落在「调用方该改解析/改请求」那一侧 ——
    重试只会把同一个错误再问一遍，而在重试策略里它会被当成网络抖动。
    """
    with _LocalHTTP(body=body) as server:
        _open_tushare_against(monkeypatch, server)
        with pytest.raises(SourceAdapterError) as caught:
            TushareAdapter().fetch_adjust_factor(['600000.SH'], date(2024, 1, 2),
                                                 date(2024, 1, 10))

    assert caught.value.kind == 'SOURCE_SCHEMA_MISMATCH', label
    assert caught.value.retryable is False, label
    assert needle in str(caught.value), label
    assert len(server.requests) == 1, '带内拒绝要当场停'


def test_tushare_rejects_a_reversed_window_before_any_request() -> None:
    """区间反了 ⇒ 抛，且**一个请求都不发**（没有本机服务就是断言本身）。

    不是「多此一举的入参校验」：反区间发出去会得到一个 `code=0`、`items=[]` 的
    正常响应，然后被当成「这段区间没有因子」写进库里。
    """
    with pytest.raises(SourceAdapterError) as caught:
        TushareAdapter()._default_fetch(symbol='600000.SH', start=date(2024, 1, 10),
                                        end=date(2024, 1, 2))

    assert caught.value.kind == 'UNSUPPORTED'
    assert caught.value.retryable is False


def test_tushare_fetch_adjust_factor_posts_once_per_symbol_and_concatenates(
        monkeypatch, tushare_token_env) -> None:
    """多标的：**每个标的一次 POST**，拼成一帧，且每次的参数带自己的代码。

    `params.ts_code` 写错（比如把后缀丢掉）不会报错 —— 源回一个 `code=0` 的空信封，
    于是那只股票的因子安静地缺失。所以两只股票的 `ts_code` 都要断出来。
    """
    bodies = [_tushare_envelope(_tushare_rows(1.0, ts_code='600000.SH')),
              _tushare_envelope(_tushare_rows(2.0, ts_code='000001.SZ'))]
    with _ScriptedHTTP(bodies) as server:
        _open_tushare_against(monkeypatch, server)
        frame = TushareAdapter().fetch_adjust_factor(['600000.SH', '000001.SZ'],
                                                     date(2024, 1, 2), date(2024, 1, 10))

    assert server.methods == ['POST', 'POST'], '一个标的一次请求'
    assert [_tushare_body(server, index)['params']['ts_code'] for index in (0, 1)] \
        == ['600000.SH', '000001.SZ']
    assert list(frame.columns) == list(ADJUST_FACTOR_COLUMNS)
    assert frame['symbol'].tolist() == ['600000.SH', '000001.SZ']
    assert frame['adjust_factor'].tolist() == [1.0, 2.0]


def test_tushare_fetch_adjust_factor_without_symbols_returns_the_standard_columns() -> None:
    """空标的清单 ⇒ 空帧 + 标准列，**一个请求都不发**（没有本机服务就是断言本身）。"""
    frame = TushareAdapter().fetch_adjust_factor([], date(2024, 1, 2), date(2024, 1, 10))

    assert frame.shape[0] == 0
    assert list(frame.columns) == list(ADJUST_FACTOR_COLUMNS)


@pytest.mark.parametrize("adapter", [AKShareAdapter, BaostockAdapter, EastMoneyAdapter,
                                     TencentAdapter, TushareAdapter])
def test_every_concrete_adapter_can_be_constructed(adapter) -> None:
    """**每一个**具体适配器都必须能被构造出来 —— 抽象方法一个都不许漏。

    `_AdapterBase` 给了 `fetch_adjust_factor` 与 `fetch_dividend` 两个默认实现，
    另外三个留在抽象层
    （那是「一个源适配器至少要能做的是什么」这条纪律）。于是**只覆盖部分数据面的源
    必须自己把三条写出来** —— 不写，这个类就实例化不了，而不是「运行时抛 UNSUPPORTED」。

    这条用例是 2026-09-29 **实测补的**：`TushareAdapter` 第一版只写了 `_default_fetch`
    与 `fetch_adjust_factor`，而它此前从未被导入、从未被实例化（门禁只 `ast` 解析源码，
    没有任何用例 import 它）⇒ 门禁全绿、用例全绿，直到手工 `TushareAdapter()` 当场
    `TypeError: Can't instantiate abstract class`。「复权因子采集通道已接」当时在**运行时
    是不成立的**，而报告里看不出来。

    静态门禁看不到这件事：`tools/verify_data_center_adapter.py` 的 A0~A11 读的是
    **源码形状**，「这个类能不能被构造」是运行时的属性。所以这条用例留在这里。
    """
    instance = adapter()
    assert instance.__class__.__abstractmethods__ == frozenset(), (
        '这些抽象方法没有实现（类实例化不了，不是"运行时会抛异常"）：%r'
        % sorted(instance.__class__.__abstractmethods__))
    assert instance.source_name(), 'source_name() 返回空串会让报告里分不清是哪个源'
    for name in _ABSTRACT_METHODS:
        assert callable(getattr(instance, name)), '%s 不可调用' % name
    assert tuple(sorted(SourceAdapter.__abstractmethods__)) == _ABSTRACT_METHODS, (
        '契约 §3.2 的抽象方法清单变了（实测 %r）—— 这条用例要跟着重读契约，'
        '并确认每个具体适配器都实现了新方法'
        # ⚠️ 2026-10-01 实测修正：这里原来写的是 `% tuple(sorted(...))`，
        # 而**断言消息只在断言失败时求值** ⇒ 这个格式化 bug 一直潜伏着，
        # 直到抽象方法从 6 个变 7 个、这条断言第一次真的失败时才炸：
        # 报出来的是 `TypeError: not all arguments converted`，把真正的原因
        # （清单少了一项）藏在一条看起来像「用例自己写错了」的异常里。
        # 多元素元组要整个当**一个**参数传，所以外面再套一层元组。
        % (tuple(sorted(SourceAdapter.__abstractmethods__)),))


# ── `normalize_adjust_factor`：源帧 → 标准帧（列对、类型对）────────────────────
# 这一层只保证「列对、类型对」。**值域（因子必须 > 0）不在这里** —— 那是
# `dc_adjust_factor` 的 `ck_dc_factor_positive`（附录 B21.5），执行点是
# `validate_frame` 的 `factor-positive` 判据（附录 B21.6，2026-09-29 晩 Ⅳ 接上）。
# 三句话是三件事，别混着说。


def _adj_factor_frame(*factors, ts_code: str = '600000.SH') -> pd.DataFrame:
    """造一个「像 tushare 那样」的源帧：`data.fields` 当列名，`data.items` 当行。"""
    assert tuple(TUSHARE_ADJ_FACTOR) == tuple(_TUSHARE_FIELDS), (
        '源字段名清单与映射表的键序对不上了：%r vs %r'
        % (tuple(_TUSHARE_FIELDS), tuple(TUSHARE_ADJ_FACTOR)))
    return pd.DataFrame(_tushare_rows(*factors, ts_code=ts_code),
                        columns=list(_TUSHARE_FIELDS))


def test_normalize_adjust_factor_maps_the_source_columns_and_types() -> None:
    """`ts_code/trade_date/adj_factor` → `symbol/trade_date/adjust_factor`，列序是契约的。

    列序也要断：`ADJUST_FACTOR_COLUMNS` 同时是 `dc_adjust_factor` 的插入列序，
    下游按位置对齐（`pgstore` 的 INSERT 与 smoke 的断言）。
    """
    frame = normalize_adjust_factor(_adj_factor_frame(1.0, 1.2), TUSHARE_ADJ_FACTOR)

    assert list(frame.columns) == list(ADJUST_FACTOR_COLUMNS)
    assert frame['symbol'].tolist() == ['600000.SH', '600000.SH']
    assert frame['trade_date'].tolist() == [date(2024, 1, 2), date(2024, 1, 3)]
    assert all(type(day) is date for day in frame['trade_date'].tolist()), (
        '契约要 `date`，不是 `Timestamp`（后者带时刻，落库会被当成本地时间）')
    assert frame['adjust_factor'].tolist() == [1.0, 1.2]


def test_normalize_adjust_factor_names_both_the_source_and_standard_column() -> None:
    """少一列 ⇒ 报错里**同时**有源列名与标准列名。

    只给源列名，读的人不知道它对应契约里的哪一列；只给标准列名，读的人不知道要去
    源接口的哪个字段里找。源接口改名与「这段区间没数据」必须能分辨。
    """
    with pytest.raises(SourceAdapterError) as caught:
        normalize_adjust_factor(_adj_factor_frame(1.0).drop(columns=['adj_factor']),
                                TUSHARE_ADJ_FACTOR)

    message = str(caught.value)
    assert 'adj_factor' in message and 'adjust_factor' in message, message


def test_normalize_adjust_factor_backfills_symbol_only_when_the_frame_has_none() -> None:
    """源帧没有 symbol 列时用请求参数回填；**没传就报错**，不静默留空。"""
    without_symbol = {'trade_date': 'trade_date', 'adj_factor': 'adjust_factor'}
    frame = _adj_factor_frame(1.0).drop(columns=['ts_code'])

    assert normalize_adjust_factor(frame, without_symbol,
                                  symbol='600000.SH')['symbol'].tolist() == ['600000.SH']
    with pytest.raises(SourceAdapterError) as caught:
        normalize_adjust_factor(frame, without_symbol)
    assert 'symbol' in str(caught.value)


def test_normalize_adjust_factor_rejects_a_non_numeric_factor() -> None:
    """因子不是数值 ⇒ 抛。

    这里拒绝的是「类型不对」，不是「值不对」—— `errors='coerce'` 那种写法会把脏值
    变成 NaN，NaN 落库变 NULL，NULL 又被下游读成「这一格没有数据」：一次格式错误
    就变成数据空洞，报告里什么都看不出来。
    """
    frame = _adj_factor_frame(1.0)
    frame.loc[0, 'adjust_factor'] = '不是数'

    with pytest.raises(SourceAdapterError) as caught:
        normalize_adjust_factor(frame, TUSHARE_ADJ_FACTOR)
    assert 'adjust_factor' in str(caught.value)


def test_normalize_adjust_factor_rejects_an_unparsable_or_missing_trade_date() -> None:
    """`trade_date` 解析不了、或解析成空 ⇒ 两个**分开**的样本（不许只测一个）。

    两个探针是分开写的：解析失败走 `_to_dates`，解析成空走 `_require_present`
    （对应 DDL 的 NOT NULL）。只测前者的话，后者一次都没跑过。
    """
    unparsable = _adj_factor_frame(1.0)
    unparsable.loc[0, 'trade_date'] = '不是日期'
    with pytest.raises(SourceAdapterError) as caught:
        normalize_adjust_factor(unparsable, TUSHARE_ADJ_FACTOR)
    assert 'trade_date' in str(caught.value)

    blank = _adj_factor_frame(1.0)
    blank.loc[0, 'trade_date'] = ''
    with pytest.raises(SourceAdapterError) as caught_blank:
        normalize_adjust_factor(blank, TUSHARE_ADJ_FACTOR)
    assert 'trade_date' in str(caught_blank.value)


def test_normalize_adjust_factor_passes_positivity_through_to_the_next_layer() -> None:
    """0 / 负值在这里**能过** —— 值域不是这一层的事。

    这条钉住的是**分工**，不是「随便测一下」：如果归一化也顺手拒了 `<= 0`，
    那么「库以 `ck_dc_factor_positive` 拒绝」（I3b 那条 `__cause__` 追链的用武之地）
    就永远走不到，取而代之的是一个更早、语义不同的错误码 —— 两条链会变成一条半。
    真正的判据在 `validate_frame` / DDL，写在这里就是第二个口径。
    """
    frame = normalize_adjust_factor(_adj_factor_frame(0.0, -1.0), TUSHARE_ADJ_FACTOR)

    assert frame['adjust_factor'].tolist() == [0.0, -1.0]


def test_validate_frame_judges_the_adjust_factor_schema() -> None:
    """复权因子帧现在**真的被判过**：认得出 schema，干净帧通过。

    本条原名 `test_validate_frame_refuses_the_adjust_factor_schema_loudly`
    （2026-09-29 晩 Ⅳ 判据接上前，它钉的是「必须响亮地拒绝」）。判据接上后那个名字
    陈述的事已经不存在了 —— 但**它盯的两个方向一点没变**，改到这里继续盯：
    ① 不许悄悄把坏数据判成 `is_valid=True`（见下面两条负样本）；
    ② 不许退回那两句误诊。第二句的背景：2026-09-29 实测（接 tushare 通道时发现）
    这一帧原先掉进通用分支，报出来的是「非标准列 `adjust_factor`：源字段名不得泄漏
    到输出列」+「列集合不匹配任何标准 schema」——**两句都是误诊**，读的人会跑去查
    源接口，而不是来看那张未收口清单。

    改名而不是删掉：它盯的那件事（别把这一帧判错）**还在**，只是判定方式从
    「响亮地拒绝」变成了「真的判过」。
    """
    report = validate_frame(normalize_adjust_factor(_adj_factor_frame(1.0),
                                                    TUSHARE_ADJ_FACTOR))

    assert report.is_valid is True, report.errors
    assert report.errors == [], report.errors

    stray = normalize_adjust_factor(_adj_factor_frame(1.0), TUSHARE_ADJ_FACTOR)
    stray['symbol_typo'] = '600000.SH'
    stray_report = validate_frame(stray)
    joined = ' '.join(stray_report.errors)
    assert '非标准列 symbol_typo' in joined, stray_report.errors
    assert 'adjust_factor' not in joined, (
        '误诊：`adjust_factor` 是契约 §3.2 的标准列 —— 它不该出现在「非标准列」里')
    assert '不匹配任何标准 schema' not in joined, (
        '误诊：复权因子是第四张 schema —— 判据接上后它必须被认出来')


def test_validate_rejects_a_non_positive_adjust_factor() -> None:
    """`adjust_factor <= 0`（含 NaN）⇒ 拒，且证据点出对应的是哪条库约束。

    **一个值一个样本**：判据只报**第一条**违规，把 0 / 负数 / NaN 塞进同一帧里
    等于只在测第一个。三种值各调一次 `validate_frame`。

    断言里必须出现 `ck_dc_factor_positive`：这条判据的全部意义就是「适配器拦下的」
    与「库会拒掉的」是**同一条规则**（契约 §3.9）。只断言「有个错误」的话，
    判据可以换成任何别的东西而用例照样绿。
    """
    for value in (0.0, -1.0, float('nan')):
        frame = normalize_adjust_factor(_adj_factor_frame(value), TUSHARE_ADJ_FACTOR)
        report = validate_frame(frame)

        joined = ' '.join(report.errors)
        assert report.is_valid is False, (value, report.errors)
        assert 'ck_dc_factor_positive' in joined, (value, report.errors)
        assert 'adjust_factor' in joined, (value, report.errors)


def test_validate_rejects_duplicate_adjust_factor_primary_key() -> None:
    """同一 `(symbol, trade_date)` 出现两次 ⇒ 拒（帧级自然键重复）。

    DDL 的主键是**三元组** `(symbol, trade_date, data_version)`，而 `data_version`
    由写入侧盖戳、源帧里根本没有这一列 ⇒ 帧级能判的只有前两列；这与 `_PRIMARY_KEYS`
    对 `pk_dc_daily_bar` / `pk_dc_financial_report` 的处理是同一个约定。
    两行同自然键落进**同一批**是重复（后一行会覆盖前一行，且覆盖哪行取决于插入
    顺序），不是历史。
    """
    frame = normalize_adjust_factor(_adj_factor_frame(1.0, 1.0), TUSHARE_ADJ_FACTOR)
    frame.loc[1, 'trade_date'] = frame.loc[0, 'trade_date']

    report = validate_frame(frame)

    joined = ' '.join(report.errors)
    assert report.is_valid is False, report.errors
    assert 'factor' in joined and '重复' in joined, report.errors


def test_validate_frame_still_reports_a_stray_column_on_an_adjust_factor_frame() -> None:
    """复权因子帧上多一列 ⇒ 仍然报出来（新判据不是「一律只报值域」的挡箭牌）。"""
    frame = normalize_adjust_factor(_adj_factor_frame(1.0), TUSHARE_ADJ_FACTOR)
    frame['adj_factor'] = 1.0  # 源字段名，模拟一次泄漏

    report = validate_frame(frame)

    assert report.is_valid is False
    assert any('非标准列 adj_factor' in error for error in report.errors), report.errors


def test_the_adapter_validate_delegates_to_validate_frame_for_this_schema() -> None:
    """类上的 `validate()` 真的走到 `validate_frame`（不只是「函数对了」）。

    契约 §3.2 的 `validate` 是适配器的**能力**之一：`DataCenter` 按它的结论决定写不写。
    只测裸函数、不测类，就可能出现「函数修好了、类还走老路」。

    样本用**违规**帧而不是干净帧：干净帧在「委托丢了、返回一个默认全绿的
    `ValidationReport`」那种假绿下也会通过 —— 那正是这条用例要拦的东西。
    """
    frame = normalize_adjust_factor(_adj_factor_frame(0.0), TUSHARE_ADJ_FACTOR)

    report = TushareAdapter().validate(frame)

    assert report.is_valid is False
    assert any('ck_dc_factor_positive' in error for error in report.errors), report.errors


def test_validate_frame_still_wins_the_empty_check_over_the_schema_branch() -> None:
    """空帧仍然走「帧为空」那条（更具体），不被任何 schema 分支盖住。

    顺序是有意的：空帧的判据对**所有** schema 都成立，而值域 / 主键只在有行时才
    谈得上；先报后者会让人以为「有数据就能过」，或者反过来以为空帧只是「值域违规」。
    """
    empty = TushareAdapter().fetch_adjust_factor([], date(2024, 1, 2), date(2024, 1, 10))

    report = validate_frame(empty)

    assert report.is_valid is False
    assert any('帧为空' in error for error in report.errors), report.errors
