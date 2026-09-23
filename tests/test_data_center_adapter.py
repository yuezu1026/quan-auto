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

import pandas as pd
import pytest

from quanauto.datasources import (
    AKSHARE_DAILY_BAR,
    DAILY_BAR_COLUMNS,
    EASTMONEY_FINANCIAL,
    EASTMONEY_INDEX_MEMBER,
    EASTMONEY_REPORT_TYPE,
    FINANCIAL_COLUMNS,
    FINANCIAL_REQUIRED_COLUMNS,
    INDEX_MEMBER_COLUMNS,
    REPORT_TYPES,
    AKShareAdapter,
    BaostockAdapter,
    EastMoneyAdapter,
    SourceAdapter,
    normalize_daily_bar,
    normalize_financial,
    normalize_index_members,
    normalize_symbol,
    validate_frame,
)
from quanauto.enums import SourcePriority
from quanauto.errors import SourceAdapterError
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


def _eastmoney_financial_raw(**overrides):
    frame = {
        "SECURITY_CODE": ["600000"],
        "REPORT_DATE": ["2026-06-30"],
        "NOTICE_DATE": ["2026-08-28"],
        "REPORT_TYPE": ["利润表"],
        "TOTAL_OPERATE_INCOME": [1.0e10],
        "PARENT_NETPROFIT": [2.0e9],
        "TOTAL_ASSETS": [3.0e11],
        "TOTAL_EQUITY": [1.5e11],
        "WEIGHTAVG_ROE": [12.5],   # 单位：百分数
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
    raw = _eastmoney_financial_raw().drop(columns=["NOTICE_DATE"])
    with pytest.raises(SourceAdapterError) as caught:
        normalize_financial(raw, EASTMONEY_FINANCIAL, symbol="600000",
                            report_type_map=EASTMONEY_REPORT_TYPE,
                            roe_is_percent=True)
    assert "announce_date" in str(caught.value)


def test_normalize_financial_maps_report_type_and_rescales_roe() -> None:
    """报表名要映射进 `ck_dc_fin_report_type` 的取值域；ROE 要从小数比率口径对齐。

    契约 §2.3 的 `roe` 是**小数比率、可为负、区间 [-1, 5]**，不是百分数；
    源给 12.5 表示 12.5%，不换算就是 12.5 —— 越过 `ck_dc_fin_roe_range` 的上界，
    入库会直接被 CHECK 拒掉（届时错误地点跑到数据库那一层）。
    """
    frame = normalize_financial(_eastmoney_financial_raw(), EASTMONEY_FINANCIAL,
                                symbol="600000", report_type_map=EASTMONEY_REPORT_TYPE,
                                roe_is_percent=True)
    assert set(FINANCIAL_REQUIRED_COLUMNS) <= set(frame.columns)
    assert "SECURITY_CODE" not in frame.columns, "源字段名泄漏"
    assert frame["report_type"].iloc[0] in REPORT_TYPES
    assert frame["report_type"].iloc[0] == "INCOME"
    assert frame["roe"].iloc[0] == pytest.approx(0.125)
    assert frame["period_end"].iloc[0] == date(2026, 6, 30)
    assert frame["announce_date"].iloc[0] == date(2026, 8, 28)


def test_normalize_financial_keeps_all_standard_subject_columns() -> None:
    """源缺科目的补 NaN 而不是消失：列形状稳定，缺失由 `missing_ratio` 显式报出来。"""
    raw = _eastmoney_financial_raw().drop(columns=["TOTAL_EQUITY"])
    frame = normalize_financial(raw, EASTMONEY_FINANCIAL, symbol="600000",
                                report_type_map=EASTMONEY_REPORT_TYPE,
                                roe_is_percent=True)
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
    raw = _eastmoney_financial_raw()
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
