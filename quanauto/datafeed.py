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

另有一条**口径**（不是签名偏差，所以不在附录 E，登记在数据中心契约附录 A6）：契约示例把
CSV 的列**原样**搬进 `BarData`，实现把四列价格各乘一次该行的 `adjust_factor` —— 与
`DbDataFeed._row_to_bar` 同一条口径、同一份算式（模块级 `_rescale_price`）。缺列或为空
⇒ `1.0`，走恒等快路径。**不这么做的后果是量纲分裂**：同一个 `DataFeed` 协议下两条实现体
交出「已复权」与「未复权」两种价格而都不报错 —— 而 `engine.py` 又把
`feed.get_adjustment_factor()` 原样写进 `ReportBundle.adjust_factor`，于是 CSV 侧那句因子
会与它自己那根 bar 的价格**互相矛盾**。`volume` / `amount` 与 DB 侧一样**刻意不乘**
（复权复的是价格）。

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
        """该 (标的, 交易日) 的**累计**复权因子。

        口径（DC 契约 §2.3 / D6，订正于 2026-09-29）："没有复权信息"**不是一个可以
        静默等价于 1.0 的状态**。

        * 调用方要的就是不复权（`AdjustType.NONE`）⇒ 返回 1.0 —— 这是**计算**的结果，
          不是"查不到"的退路；
        * 调用方要后复权/前复权（`HFQ`/`QFQ`）而库/源里没有这一天的因子 ⇒
          抛 `DataNotAvailableError`（DATA_001）。§2.4：「所有『找不到数据』的分支都必须
          **显式失败**，不允许返回空集或默认值」。

        本方法先前写的是"没有复权信息时返回 1.0"——那句话把两种情况当成一种，而它
        的后果是**整段回测静默变成不复权**：曲线照样画得出来，报告里一个字都不提。
        实现层（`DbDataFeed`）现在按上面两条分开走。
        """
        raise NotImplementedError

    @abstractmethod
    def get_dividend(self, symbol: str, datetime: datetime) -> float:
        """该 (标的, **除权除息日**) 的每股现金分红（元/股，税前）；没有分红信息时返回 `0.0`。

        ⚠️ 这里的"没有"包含**三种情形**，它们**都返回 `0.0`、都不抛**（订正于 2026-10-01，
        数据中心契约附录 **B22.3**）：① 这个标的没有分红记录；② 这一天不是除权除息日；
        ③ 数据源根本没有分红这一列。主契约 §2.2.1 的参考实现就是这么写的（缺 symbol /
        缺日 / 缺 `dividend` 列一律 `return 0.0`），本仓库的 `CsvDataFeed` 逐字同形。

        **与 `get_adjustment_factor` 刻意不对称**（两者挨着，所以写在这里）：因子那边
        "缺行"要抛 `DataNotAvailableError`（DATA_001）。理由两条，缺一不可：

        * **分红是稀疏事件流，因子是连续序列。** 绝大多数交易日没有分红，所以"没有"的
          默认答案是 `0.0`；而"该有因子却没有"是数据异常（序列不该有洞），默认答案是抛。
        * **同一协议的两条实现体必须同口径**（D9）。CSV 侧根本没有 `AdjustType` 这个概念
          （见 `CsvDataFeed` 的已登记偏差），它面对"文件里没有分红"只能给 `0.0`；
          若库侧的同一个方法因为"没接源"而抛，调用方就得按实现类分支写代码。

        ⚠️ 代价：**"没有分红"与"没接分红源"在返回值上不可区分**，而"静默补 0.0"会让
        整段回测悄悄变成**不含分红**（与因子侧"静默补 1.0 ⇒ 悄悄不复权"同族）。所以
        本方法的返回值**不**能当作"分红口径在绩效里生效"的证据 —— 引擎侧是否消费它
        要看 `quanauto/engine.py`（当前**一行都没动**，附录 B22.2）。
        """
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


def _rescale_price(value: float, factor: float) -> float:
    """原始价 → 复权价：“全仓库唯一一处复权算术”（数据中心契约 D6 第二句「读取时现算」）。

    为什么是独立函数而不是内联的 `value * factor`：两条实现体都要乘（`CsvDataFeed`
    上面四列、`DbDataFeed._row_to_bar` 四列），内联就是八份同样的乘法；更要紧的是这条口径
    要**一眼可见地只有一份**（附录 B21 那句「归一化只发生在 `_row_to_bar` 一处」在这里扩成
    「归一化 + 复权都只在这一份算式」）。

    为什么住在 `datafeed.py` 而不是 `datacenter.py`（2026-09-29 晚 ⅲ 下移）：本模块是
    `DataFeed` 协议的所在地，两条实现体（`CsvDataFeed` 在这里、`DbDataFeed` 在
    `datacenter.py`）都依赖它；反方向放会让 `datafeed` → `datacenter` 变成**循环导入**。
    同族的先例就是上面的 `epoch_seconds()` —— 一样是模块级、一样住在 `datafeed.py`、
    一样被 `DbDataFeed` 使用。`datacenter.py` 里 import 进来只是复用，算术本体只有这里一份。

    `factor == 1.0` 时**原样返回**、不做乘法：

    * 不复权路径（`AdjustType.NONE`，以及 CSV 没有 `adjust_factor` 列 / 该列为空的日子）
      与引入复权之前的实现**逐字节一致**，不会被一次 `× 1.0` 引入本不存在的浮点尾巴；
    * 判据是**值相等**而不是 `is`：这里只 claim「`1.0` 走恒等路径」，
      **不** claim「不复权价与复权价一定逐位相等」—— 后者挡不住也不该假装能挡住。
    """
    if factor == 1.0:
        return value
    return value * factor


class CsvDataFeed(DataFeed):
    """CSV 数据源。只读、全量载入内存、按 `(symbol, datetime)` 建哈希索引。

    列的约定：`datetime`、`symbol`、`open`、`high`、`low`、`close`、`volume` 必填；
    `amount`（缺则 `close * volume`）、`timestamp`（缺则按 UTC 纪元秒自算）、
    `adjust_factor`、`dividend` 选填。

    `dividend` 列的语义是**该行那个日期（= 除权除息日）的每股现金分红**（元/股、税前）。
    它只被 `get_dividend` 消费，**不参与**任何价格换算（复权算术只认 `adjust_factor`）——
    两件事刻意分开："复权"是乘法，"分红"是现金流，混在一起会得到一个既不是前复权
    也不是后复权的价。

    **价格量纲**：`adjust_factor` 列被当成**累计**复权因子，四列价格各乘一次它就是
    `BarData` 里的价格，与 `DbDataFeed._row_to_bar` 同一条口径（同一份 `_rescale_price`）。
    缺列、或该列为空 ⇒ `1.0`，价格就是文件里的原值。这一条登记在数据中心契约附录 A6。

    ⚠️ **已登记的偏差**：`get_adjustment_factor` **不接** `AdjustType`（契约 §2.2.1 的签名里
    没有这个参数），所以本类无法区分「用户要 HFQ」与「用户要 QFQ」—— 它只回答「文件里这个
    时点的累计因子是多少」。因此「缺该列/该列为空 ⇒ 1.0」是**恒等的**，不需要也不应该像
    `DbDataFeed._factor_for` 那样抛 `DataNotAvailableError`；这里没有「找不到数据」这个状态，
    只有「文件作者没提供」这一个。重要的一条：**回报的因子与乘上去的因子是同一个值**（
    两者都来自同一个 `factor` 局部变量），所以不存在 bundle 与价格互相矛盾的情形。
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

            # 该行的累计复权因子。缺列/为空 ⇒ 1.0（走恒等快路径，见 `_rescale_price`）。
            # 先解析再建 bar：解析失败要抛在**建对象之前**，免得留下半截状态。
            factor = parse_float(row.get("adjust_factor") or "1.0", "%s 的 adjust_factor 列" % where)
            bar = BarData(
                symbol=symbol,
                # 四列价格各乘一次因子 —— 只乘 `close` 是最容易漏的一种（见 `_rescale_price`）。
                # `amount` 刻意**不乘**：上面那句 `close * volume` 用的也是未复权的 close。
                open=_rescale_price(values["open"], factor),
                high=_rescale_price(values["high"], factor),
                low=_rescale_price(values["low"], factor),
                close=_rescale_price(values["close"], factor),
                volume=int(values["volume"]),
                amount=amount,
                datetime=stamp,
                timestamp=epoch_seconds(stamp),
            )
            index.setdefault(symbol, {})[stamp] = bar
            adjust.setdefault(symbol, {})[stamp] = factor
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
        """该时点的累计复权因子；文件没提供时 `1.0`（与价格乘的是**同一个**默认值）。

        这个返回值与 `_load_data` 里乘到四列价格上的因子**必然相等**：两者是同一个
        局部变量 `factor` 的两条出口。`engine.py` 把它写进 `ReportBundle.adjust_factor`，
        所以「策略按声明的因子把价格除回去」必须能拿到文件里的原值 —— 控制组在
        `tests/test_backtest_slice.py`。
        """
        return self._adjust.get(symbol, {}).get(datetime, 1.0)

    def get_dividend(self, symbol: str, datetime: datetime) -> float:
        """该时点（**除权除息日**）的每股现金分红；文件没提供时 `0.0`。

        这是 B22.3 那张"两条读侧口径"表的 **CSV 那一半**：缺 symbol / 缺日 / 缺
        `dividend` 列**一律** `0.0`，从不抛。`DbDataFeed.get_dividend` 与它同口径
        —— 这两条实现体不一致的话，同一个 `DataFeed` 协议下"没有分红数据"就有两种行为。
        """
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
