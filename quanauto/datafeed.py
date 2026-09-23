"""数据源 —— `DataFeed` 抽象类（契约 §2.2.1）与 `CsvDataFeed`（§2.2.2）。

关于契约 §2.2.2 的三处**必须偏离**（都写在契约附录 E 与 manifest 里，不是暗改）：

1. 契约的示例用 `pd.read_csv`，并且把列存成 `pd.DataFrame`。但 `DataFeed` 的 9 个方法
   的返回注解全是 `BarData` / `List[BarData]`，DataFrame 一步都没露到接口上。引 pandas
   只会让研究层的 import 变重（`tests/test_skeleton.py` 有一条测试专门守这个），
   所以 I1 用标准库 `csv` 实现。
2. 契约示例构造 `BarData` 时只填了 7 个字段，漏了 `amount` 和 `timestamp` —— 而按
   §2.1.2，这两个字段没有默认值，照抄示例会 `TypeError`。这里补齐：
   `amount` 缺列时用 `close * volume` 折算，`timestamp` 缺列时按 **UTC 纪元秒**自算
   （刻意不用 `datetime.timestamp()`：它按本机时区解释 naive datetime，换台机器结果就变，
   那是「同一条命令跑两次结果不一致」的隐蔽来源）。
3. 契约示例的 `get_market_status` 只看时间戳在不在索引里。`is_symbol_available` 却额外
   要求 `volume > 0`。两者口径不同是契约的原样，这里**照抄**（不自作主张统一），
   差异记在 docstring 上。

另外：契约的示例把 `self._data` 声明成 `Dict[str, pd.DataFrame]`。这里改成
`Dict[str, List[BarData]]` 并按时间排好序，`get_bar` 另配一个哈希索引 —— 逐根 K 线取数的
内层循环是 O(1) 而不是扫全表。
"""

from __future__ import annotations

import csv
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional

from .enums import MarketStatus
from .errors import DataFeedError, DataValidationError
from .models import BarData

REQUIRED_COLUMNS = ("datetime", "symbol", "open", "high", "low", "close", "volume")
NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume")
EPOCH = datetime(1970, 1, 1)


class DataFeed(ABC):
    """数据源抽象基类。9 个抽象方法全部要在子类里实现，一个都不能少。

    参数名 `datetime` 会**遮蔽**模块里的 `datetime` 类型 —— 这是契约原文的写法
    （`def get_bar(self, symbol: str, datetime: datetime)`），签名门禁按契约逐字比对，
    所以不能顺手改名成 `dt`。因为文件开了 `from __future__ import annotations`，
    注解不会被求值，遮蔽只影响函数体内，没有实际危害。
    """

    @abstractmethod
    def get_bar(self, symbol: str, datetime: datetime) -> Optional[BarData]:
        """取单根 K 线；标的或时间点不存在时返回 `None`。"""
        raise NotImplementedError

    @abstractmethod
    def get_bars(self, symbol: str, start: datetime, end: datetime) -> List[BarData]:
        """批量取 K 线，闭区间 `[start, end]`；标的不存在返回空列表。"""
        raise NotImplementedError

    @abstractmethod
    def get_available_symbols(self) -> List[str]:
        """全部可用标的。"""
        raise NotImplementedError

    @abstractmethod
    def get_available_dates(self, symbol: str) -> List[datetime]:
        """某标的的全部可用时间点。"""
        raise NotImplementedError

    @abstractmethod
    def is_symbol_available(self, symbol: str, datetime: datetime) -> bool:
        """该时点是否可交易（考虑停牌 / 涨跌停）。"""
        raise NotImplementedError

    @abstractmethod
    def get_adjustment_factor(self, symbol: str, datetime: datetime) -> float:
        """复权因子；没有复权信息时返回 1.0。"""
        raise NotImplementedError

    @abstractmethod
    def get_dividend(self, symbol: str, datetime: datetime) -> float:
        """每股分红；没有分红信息时返回 0.0。"""
        raise NotImplementedError

    @abstractmethod
    def get_trading_calendar(self, start: datetime, end: datetime) -> List[datetime]:
        """交易日历（所有标的的并集，去重升序）。"""
        raise NotImplementedError

    @abstractmethod
    def get_market_status(self, datetime: datetime) -> MarketStatus:
        """该时点的市场状态。"""
        raise NotImplementedError


# ── 模块级辅助函数 ────────────────────────────────────────────────────────
# 刻意**不**做成 `CsvDataFeed` 的私有方法：契约 §2.2.2 的块把该类的方法列全了，
# 多一个私有方法就意味着 manifest 里多一条 `members_extra` 登记。放到模块级，
# 类的成员集合与契约逐字一致，门禁不需要开口子。
def epoch_seconds(value: datetime) -> int:
    """UTC 纪元秒。刻意避开 `datetime.timestamp()`（按本机时区解释 naive datetime）。"""
    return int((value - EPOCH).total_seconds())


def parse_datetime(text: str, where: str) -> datetime:
    """解析 CSV 里的时间列。接受 `YYYY-MM-DD` 与 `YYYY-MM-DD HH:MM:SS` 两种写法。"""
    raw = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            # 需要 `datetime.strptime`，而模块级 `datetime` 名字没被遮蔽（遮蔽只在方法体内）。
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise DataValidationError("时间列无法解析: %r（位置：%s）" % (raw, where))


def parse_float(text: str, where: str) -> float:
    """解析数值列，失败时抛 `DataValidationError` 而不是返回 0 —— 静默补 0 会污染回测。"""
    raw = text.strip()
    if raw == "":
        raise DataValidationError("数值列为空（位置：%s）" % where)
    try:
        return float(raw)
    except ValueError:
        raise DataValidationError("数值列无法解析: %r（位置：%s）" % (raw, where))


class CsvDataFeed(DataFeed):
    """CSV 数据源。只读、全量载入内存、按 `(symbol, datetime)` 建哈希索引。

    列的约定：`datetime`、`symbol`、`open`、`high`、`low`、`close`、`volume` 必填；
    `amount`（缺则 `close * volume`）、`timestamp`（缺则按 UTC 纪元秒自算）、
    `adjust_factor`、`dividend` 选填。
    """

    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self._data: Dict[str, List[BarData]] = {}
        self._index: Dict[str, Dict[datetime, BarData]] = {}
        self._adjust: Dict[str, Dict[datetime, float]] = {}
        self._dividend: Dict[str, Dict[datetime, float]] = {}
        self._calendar: List[datetime] = []
        self._load_data()

    def _load_data(self):
        """读文件、校验、建索引。任何一步出问题都在这里抛，绝不留半截状态：

        先把整个文件读进临时结构，全部校验通过之后才写 `self._data` —— 否则
        `__init__` 抛异常时对象已处于「一半有数据」的状态，被人 `except` 掉再复用
        就会得到静默错误的回测。
        """
        if not os.path.isfile(self.csv_path):
            raise DataFeedError("CSV 文件不存在: %s" % self.csv_path)

        try:
            with open(self.csv_path, "r", encoding="utf-8-sig", newline="") as fp:
                reader = csv.DictReader(fp)
                columns = tuple(reader.fieldnames or ())
                rows = list(reader)
        except OSError as exc:
            raise DataFeedError("CSV 文件读取失败: %s (%s)" % (self.csv_path, exc)) from exc

        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise DataValidationError(
                "CSV 缺少必需列 %s（实际列：%s）" % (", ".join(missing), ", ".join(columns))
            )
        if not rows:
            raise DataValidationError("CSV 没有任何数据行: %s" % self.csv_path)

        data: Dict[str, List[BarData]] = {}
        index: Dict[str, Dict[datetime, BarData]] = {}
        adjust: Dict[str, Dict[datetime, float]] = {}
        dividend: Dict[str, Dict[datetime, float]] = {}
        seen: Dict[str, Dict[datetime, int]] = {}

        for lineno, row in enumerate(rows, start=2):  # 1 是表头
            where = "第 %d 行" % lineno
            symbol = (row.get("symbol") or "").strip()
            if not symbol:
                raise DataValidationError("symbol 为空（位置：%s）" % where)
            stamp = parse_datetime(row.get("datetime") or "", where)
            values = {c: parse_float(row.get(c) or "", "%s 的 %s 列" % (where, c)) for c in NUMERIC_COLUMNS}
            if values["volume"] < 0:
                raise DataValidationError("volume 为负（位置：%s）" % where)
            amount_text = (row.get("amount") or "").strip()
            amount = float(amount_text) if amount_text else values["close"] * values["volume"]

            per_symbol_seen = seen.setdefault(symbol, {})
            if stamp in per_symbol_seen:
                raise DataValidationError(
                    "同一标的的时间戳重复: %s @ %s（第 %d 行与第 %d 行）"
                    % (symbol, stamp, per_symbol_seen[stamp], lineno)
                )
            per_symbol_seen[stamp] = lineno

            bar = BarData(
                symbol=symbol,
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=int(values["volume"]),
                amount=amount,
                datetime=stamp,
                timestamp=epoch_seconds(stamp),
            )
            index.setdefault(symbol, {})[stamp] = bar
            adjust.setdefault(symbol, {})[stamp] = parse_float(row.get("adjust_factor") or "1.0", where)
            dividend.setdefault(symbol, {})[stamp] = parse_float(row.get("dividend") or "0.0", where)

        for symbol, bars in index.items():
            data[symbol] = sorted(bars.values(), key=lambda b: b.datetime)

        # 全部校验通过，现在才提交
        self._data = data
        self._index = index
        self._adjust = adjust
        self._dividend = dividend
        self._calendar = sorted({b.datetime for bars in data.values() for b in bars})

    def get_bar(self, symbol: str, datetime: datetime) -> Optional[BarData]:
        return self._index.get(symbol, {}).get(datetime)

    def get_bars(self, symbol: str, start: datetime, end: datetime) -> List[BarData]:
        if start > end:
            return []
        return [b for b in self._data.get(symbol, []) if start <= b.datetime <= end]

    def get_available_symbols(self) -> List[str]:
        """按字典序返回。契约示例返回的是插入序（= 文件里首次出现的顺序）——
        字典序是刻意的：文件行序一变、回测结果就变，那是最难查的一类不确定。
        """
        return sorted(self._data)

    def get_available_dates(self, symbol: str) -> List[datetime]:
        return [b.datetime for b in self._data.get(symbol, [])]

    def is_symbol_available(self, symbol: str, datetime: datetime) -> bool:
        bar = self.get_bar(symbol, datetime)
        return bar is not None and bar.volume > 0

    def get_adjustment_factor(self, symbol: str, datetime: datetime) -> float:
        return self._adjust.get(symbol, {}).get(datetime, 1.0)

    def get_dividend(self, symbol: str, datetime: datetime) -> float:
        return self._dividend.get(symbol, {}).get(datetime, 0.0)

    def get_trading_calendar(self, start: datetime, end: datetime) -> List[datetime]:
        if start > end:
            return []
        return [d for d in self._calendar if start <= d <= end]

    def get_market_status(self, datetime: datetime) -> MarketStatus:
        """口径与 `is_symbol_available` **不同**（照抄契约示例）：这里只看时间戳在不在
        某个标的的索引里，不看 `volume`。一个时点全部标的都停牌时它会说 `OPEN`。
        这是契约的原样，不是这里的笔误；要改得先改契约。
        """
        for bars in self._index.values():
            if datetime in bars:
                return MarketStatus.OPEN
        return MarketStatus.CLOSED
