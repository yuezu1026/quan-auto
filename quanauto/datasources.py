"""数据中心采集侧适配器（契约 §3.2）—— 把源特有格式归一化到标准 schema。

本模块只做一件事：**把「源给我们什么」变成「契约要什么」**，并且让源字段名没有机会
出现在下游 DataFrame 里。契约 §3.2 把职责边界划得很死，这里逐条落实：

* **不写数据库**（D1 / D10：写入由 `DataCenter` 统一负责）。本模块不导入任何数据库
  驱动，只返回 DataFrame。加一个 `psycopg` 进来是很容易的事，所以
  `tools/verify_data_center_adapter.py` 专门盯这条。
* **不判断 `as_of_date`**（采集关心「现在取得到什么」，不是「过去看得到什么」）。
  `fetch_index_members` 的 `as_of_date` 按契约原文是「截至该日已知的调仓记录」，
  是**取数范围**，不是可见性过滤。可见性由读侧的 `PITGuard` 负责 —— 采集侧没有
  交易日历，硬做 PIT 判断只会做错，而且错得很安静。
* **只抛 `SourceAdapterError`（DATA_005）** 表示「源这一侧的问题」。网络、限流、
  源格式解析不了、归一化缺少必需列，全走这一个类型；`ImportError` / `KeyError` /
  `ValueError` 逃到上层就是「上层代码写错了」的假信号。

## 两个最容易出事故的归一化（契约 §2.3 点名）

1. **`volume` 的单位是「股」，不是「手」。** 东方财富系接口（AKShare 的
   `stock_zh_a_hist`）给的成交量是**手**，必须 ×100。`db/data_center.sql` 的
   `dc_daily_bar.volume` 注释把这条写死了（`SourceAdapter` 的验收表也点名了
   「`volume` 手/股换算正确」）。这个 bug 不报错，只让成交量类因子整体缩小 100 倍 ——
   回测曲线看着完全正常。
2. **`weight` 是 0~1 小数，不是百分数。** 源给 10.0 表示 10%，必须 ÷100。
   同理 `roe`，但**方向相反**：契约 §2.3 明确 `roe` 是小数比率、可为负、区间 [-1, 5]，
   不受 0~1 约束，所以它只在源确实给百分数时才 ÷100。

## 符号格式（`db/data_center.sql` 的 `dc_symbol.symbol` 注释）

「统一格式 600000.SH / 000001.SZ，**禁止混用裸代码**」。源几乎都只给 6 位裸代码
（akshare 要 `600000`，baostock 要 `sh.600000`），所以**交易所后缀由适配器补**，
而不是留给下游猜。`normalize_symbol` 是那条规则的唯一实现点。

## 源字段名不得泄漏（契约 §3.2 验收表）

实现方式是**显式的列映射表**（`AKSHARE_DAILY_BAR` 等），而不是把 `rename` 散在
代码里。映射表因此有两条可以被**静态检查**（不需要跑代码）的性质：

* 值域 ⊆ 标准 schema —— 否则就是把源名字写进了输出列；
* 键域 ∩ 标准 schema = ∅ —— 标准名不需要映射，出现交集说明映射表写反了。

`tools/verify_data_center_adapter.py` 检查这两条。它只解析源码，不导入 pandas：
门禁不许把「装不装第三方库」变成能不能跑的条件（C4）。

## 未验证的部分（诚实声明，不在代码里假装已验证）

`akshare` / `baostock` 仍**没有安装**（它们在 `[datasources]` extra 里），也**从未联网核对**
⇒ 这两张映射表的**键名**仍是照源文档的字段名写的，`_default_*` 真实取数函数仍未被真实调用。
测试覆盖的是映射**机制**（喂进带源字段名的帧，输出必须是标准 schema），不是映射**内容**。

**东财一源已经不同了（2026-09-24）**：传输层已落地（就是下面的 `_http_get_json`），并对真实
端点跑过一次**人工**冒烟（`tools/eastmoney_transport_smoke.py`，证据
`tools/eastmoney-transport-smoke-report.txt` —— **它是工具，不是门禁**）。实测结论不是
「未验证」，而是**部分证伪**：9 列**没有任何单一 `reportName` 能喂满**
（`RPT_LICO_FN_CPD` 5/9、`RPT_DMSK_FN_BALANCE` 5/9，并集 8/9），且 `REPORT_TYPE`
在真实返回里**没有生产者**（只有 `REPORT_TYPE_CODE`）—— 而 `EASTMONEY_REPORT_TYPE` 是按
**中文报表名**建的，真实的对应关系却是「一个 `reportName` 就是一种报表」。

据此**改了取数形状**（这是产品决策，逐条证据与边界登记在数据中心契约**附录 B12**）：

* 一个 `reportName` 一张映射表（`EASTMONEY_INCOME_FINANCIAL` /
  `EASTMONEY_BALANCE_FINANCIAL`）——「一张大表」在真实返回里不存在；
* `report_type` 改由**请求参数** `reportName` 决定（`EASTMONEY_REPORT_TYPE` 的键因此从
  中文报表名换成 `reportName`）—— 响应里没有这一列，按响应列建的表没有生产者；
* `fetch_financial` 一个报告期出**两行**（利润表一行、资产负债表一行），不把两张报表拼成
  一行：`report_type` 是 `dc_financial` 主键的一部分，拼成一行等于让资产负债类科目挂在
  一行不属于任何真实报表的数据上。缺的科目按契约留 NaN，`missing_ratio` 会报出来。

**仍然没有实测过的**（不许读成「已验证」）：`pageSize` 的取值（实测过的只是「这个参数被
接受」）、东财的**日期过滤语法**（所以 `period_end` 在归一化之后筛，不拼进请求）、日线端点
（`push2his` 间歇性拒连），以及 akshare / baostock 两源的一切。

## 一处契约缺口（本切片不擅自补）

契约 §3.2 的表说 `BaostockAdapter` 覆盖「行情（**含停牌/涨跌停标记**）」「质量标记更全」，
但同一节规定的日线标准 schema 只有 8 列，**没有任何一列能装停牌/涨跌停标记**。
本切片的选择：归一化时**丢弃**这些列（留着就是源字段名泄漏，而且会让
`DAILY_BAR_COLUMNS` 逐字相等的检查失效），并在附录 B 登记为待补 —— 要么给
`dc_daily_bar` 加列，要么给 `SourceAdapter` 加一个 `fetch_quality_flags`。
不在这里自己发明第 9 列：那会让「标准 schema」变成实现说了算。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
import json
import re
import socket
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from .enums import SourcePriority
from .errors import SourceAdapterError
from .models import ValidationReport

# ── 标准 schema（唯一真源是契约 §3.2；列名逐字抄写，不发明）────────────────────
# 日线：契约把 8 个列名写全了，所以这里是**逐字相等**的判据。
DAILY_BAR_COLUMNS = (
    'symbol', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount',
)

# 财务：契约只说「必须包含 —— symbol / report_type / period_end / announce_date /
# 各财务科目」，「各财务科目」是开放的 ⇒ 这里只能要求**子集**，不能要求相等。
# 等式会把「源多给了一个科目」判成红灯，那是在惩罚一件好事。
FINANCIAL_REQUIRED_COLUMNS = ('symbol', 'report_type', 'period_end', 'announce_date')
FINANCIAL_SUBJECT_COLUMNS = (
    'revenue', 'net_profit', 'total_assets', 'total_equity', 'roe',
)
FINANCIAL_COLUMNS = FINANCIAL_REQUIRED_COLUMNS + FINANCIAL_SUBJECT_COLUMNS

# 指数成分股：契约把 5 个列名写全了（`effective_to` 可空）。逐字相等。
INDEX_MEMBER_COLUMNS = (
    'index_code', 'symbol', 'effective_from', 'effective_to', 'weight',
)

# 全量标准列：用来判「泄漏」——任何不在此集合里的列都是源字段名。
STANDARD_COLUMNS = DAILY_BAR_COLUMNS + FINANCIAL_COLUMNS + INDEX_MEMBER_COLUMNS

# 契约 §3.6.1 的 `ck_dc_fin_report_type`。适配器必须在**写库前**就把源的口径
# 映射到这个枚举，否则那条第 5 个取值出现时，报错地点会跑到数据库那一层。
REPORT_TYPES = ('BALANCE', 'INCOME', 'CASHFLOW', 'INDICATOR')

# 契约 §2.3：价格保留 4 位小数（`numeric(18,4)`）。
PRICE_DECIMALS = 4

# ── 源 → 标准 schema 的映射表 ─────────────────────────────────────────────────
# 「东财系」列名（AKShare 的 `stock_zh_a_hist` 底子是东方财富，列名是中文）。
# 注意这里**没有** symbol：该接口按 symbol 取数，返回的帧不带 symbol 列，
# 所以 symbol 由请求参数回填（`symbol=` 参数），这是归一化的一部分，不是补丁。
AKSHARE_DAILY_BAR = {
    '日期': 'trade_date',
    '开盘': 'open',
    '收盘': 'close',
    '最高': 'high',
    '最低': 'low',
    '成交量': 'volume',
    '成交额': 'amount',
}
# 该源给的成交量单位是「手」（契约 §2.3 / `dc_daily_bar.volume` 注释）。
AKSHARE_VOLUME_IN_LOTS = True

# Baostock `query_history_k_data_plus` 的列名。它给 `tradestatus` / `isST`
# （停牌、ST 标记）与 `preclose` / `pctChg` / `turn` —— 标准日线 schema 装不下，
# 在本切片里被**丢弃**（见模块 docstring 末节）。
BAOSTOCK_DAILY_BAR = {
    'date': 'trade_date',
    'open': 'open',
    'high': 'high',
    'low': 'low',
    'close': 'close',
    'volume': 'volume',
    'amount': 'amount',
}
# 该源给的成交量单位也是「手」（契约 §2.3 点名的那处换算）。
BAOSTOCK_VOLUME_IN_LOTS = True
# 「质量标记更全」的那些列：明确列在这里，是为了让「为什么它们不见了」有据可查，
# 而不是让人以为适配器漏了。
BAOSTOCK_DROPPED_MARKERS = ('tradestatus', 'isST', 'preclose', 'pctChg', 'turn', 'adjustflag')

# 东方财富数据中心接口（财务）的列名。全大写是源自己的风格 —— 正是 D9 要防的那种
# 「源字段名泄漏」的典型样本，所以它必须只出现在这些表的**键**里。
#
# **一张报表一张表（2026-09-24 实测，契约附录 B12）**：东财的「财务」不是一次请求，
# 一个 `reportName` 就是一种报表，两张报表的列名**互不相同** —— 而它们在 `dc_financial`
# 里本来就是**两行**（`report_type` 是主键的一部分），不是一行。
# 连报告期的**键名**都不一样，这是「不能把两张报表当成一张」最直接的证据：
EASTMONEY_INCOME_FINANCIAL = {
    'SECURITY_CODE': 'symbol',
    'REPORTDATE': 'period_end',       # 没有下划线，与下面资产负债表的写法不同
    'NOTICE_DATE': 'announce_date',
    'TOTAL_OPERATE_INCOME': 'revenue',
    'PARENT_NETPROFIT': 'net_profit',
    'WEIGHTAVG_ROE': 'roe',
}
EASTMONEY_BALANCE_FINANCIAL = {
    'SECURITY_CODE': 'symbol',
    'REPORT_DATE': 'period_end',
    'NOTICE_DATE': 'announce_date',
    'TOTAL_ASSETS': 'total_assets',
    'TOTAL_EQUITY': 'total_equity',
}
# 报表类型：真实返回里**没有这一列**（只有 `REPORT_TYPE_CODE`），它是**请求参数**
# `reportName` 的属性 ⇒ 这张表的键是 `reportName`，值是 `ck_dc_fin_report_type` 的取值。
# 键从「中文报表名」换成 `reportName` 是**实测结论**：按响应列建的表在真实返回里找不到
# 生产者，那张表就是死代码 —— 而不是「改一改让冒烟好看点」。
EASTMONEY_REPORT_TYPE = {
    'RPT_LICO_FN_CPD': 'INCOME',
    'RPT_DMSK_FN_BALANCE': 'BALANCE',
}
# 该源的 ROE 是百分数（12.34 表示 12.34%），而契约 §2.3 要小数比率。
EASTMONEY_ROE_IS_PERCENT = True

# 东方财富指数成分股（区间表）的列名。
EASTMONEY_INDEX_MEMBER = {
    'INDEX_CODE': 'index_code',
    'SECURITY_CODE': 'symbol',
    'START_DATE': 'effective_from',
    'END_DATE': 'effective_to',
    'WEIGHT': 'weight',
}
# 该源的权重是百分数。
EASTMONEY_WEIGHT_IS_PERCENT = True

# ── 真实取数的传输层（只用标准库） ─────────────────────────────────────────────
# 契约 §3.2 的三个源里，东财**没有官方 Python SDK** —— 于是「怎么发请求」这件事落在
# 本模块里，而不是某个 SDK 里。这里只用标准库：`[datasources]` extra 装的是源 SDK，
# 而 CI 只装 `[dev]`，任何一处 import 了第三方 HTTP 库都会让本模块在 CI 里直接不可导入。
#
# **网络出口只有 `_http_get_json` 一处**。这不是洁癖：出口唯一，测试把它换成假函数
# 就能离线验完 URL、参数、分页与解帧，而被测代码一行也不用改。
HTTP_TIMEOUT_SECONDS = 10.0
HTTP_USER_AGENT = 'quan-auto/0.1 (+https://github.com/yuezu1026/quan-auto)'

# 东财数据中心（财务 / 指数成分股）端点。**端点自身是实测可达的**（2026-09-24：直连 200，
# 证据 `tools/eastmoney-transport-smoke-report.txt`）；待验证的是各 `reportName` 的**列名**，
# 那部分登记在数据中心契约附录 B10，**不要**把「端点可达」读成「映射表是对的」。
EASTMONEY_DATA_API = 'https://datacenter-web.eastmoney.com/api/data/v1/get'

# 分页：实测响应带 `result.pages`，而**一次请求只回一页** —— 不翻页就是静默截断
# （B12 的探针用 `pageSize=2` 时看到 `pages=54`）。`pageSize` 的**取值**没有实测过，
# 实测过的只是「这个参数存在且被接受」。
EASTMONEY_PAGE_SIZE = 100
# 翻页上限：源报出一个荒唐的页数时**宁可报错也不截断**。它是个防跑飞的闸门，不是数据
# 口径 —— 调到多少都不改变「有没有取全」的判定。
EASTMONEY_MAX_PAGES = 200

EXCHANGES = ('SH', 'SZ', 'BJ')

# 归一化入口要接受的四种写法。**`re` 模块级编译而不是每次调用现编**：这类函数在
# 逐标的循环里被调用上万次，而且编译期能立刻暴露正则写错。
_SUFFIXED_RE = re.compile(r'^(\d{6})\.(SH|SZ|BJ)$')      # 600000.SH
_PREFIXED_RE = re.compile(r'^(SH|SZ|BJ)\.?(\d{6})$')     # sh.600000 / SH600000
_BARE_RE = re.compile(r'^(\d{6})$')                      # 600000


def normalize_symbol(code: Any) -> str:
    """把各种写法的标的代码归一成契约要求的 `600000.SH` / `000001.SZ`。

    `db/data_center.sql` 的 `dc_symbol.symbol` 注释：统一格式，**禁止混用裸代码**。
    规则：

    * `600000` / `sh.600000` / `SH600000` / `600000.SH` → `600000.SH`（大小写无关）；
    * 6 位裸代码按首位推断交易所 —— `6`/`9` → SH，`0`/`2`/`3` → SZ，`4`/`8` → BJ；
    * 其余一律 `SourceAdapterError`，**不猜、不补默认值**。猜错的后果是这只票的
      行情被安静地归到另一个交易所，而代码（`600000`）还是一样的，肉眼看不出来。

    非字符串（`None`、数字）也走异常：`600000` 作为 int 会被当 600000 处理，
    但 `1` 就不是代码了，与其在后面某处变成 `'1.SZ'`，不如在这里炸。

    **指数代码必须带后缀，裸代码不接**：`000001` 既是平安银行（SZ）又是上证指数（SH），
    首位推断对「标的」成立、对「指数」不成立 —— `000300` 会被推成 `000300.SZ`（错），
    而 `000300.SH`（沪深 300，契约 §3.2 的样例）原样通过。这个歧义不靠猜补，
    靠要求调用方写后缀：写不出后缀，说明它自己也不知道那是哪只票。
    """
    if not isinstance(code, str):
        raise SourceAdapterError('标的代码必须是字符串，收到 %r（%s）'
                                 % (code, type(code).__name__))
    text = code.strip().upper()
    if not text:
        raise SourceAdapterError('标的代码是空字符串')
    match = _SUFFIXED_RE.match(text)
    if match:
        return '%s.%s' % (match.group(1), match.group(2))
    match = _PREFIXED_RE.match(text)
    if match:
        return '%s.%s' % (match.group(2), match.group(1))
    if _BARE_RE.match(text):
        return '%s.%s' % (text, _infer_exchange(text))
    raise SourceAdapterError(
        '无法归一化的标的代码 %r：接受 `600000.SH` / `sh.600000` / `SH600000` / `600000` 四种写法'
        % code)


def _infer_exchange(digits: str) -> str:
    """裸 6 位代码 → 交易所。**只对股票成立**（见 `normalize_symbol` 的指数说明）。"""
    head = digits[0]
    if head in ('6', '9'):          # 6xxxxx 沪A；9xxxxx 沪B
        return 'SH'
    if head in ('0', '2', '3'):     # 000/002/300 深A；200xxx 深B
        return 'SZ'
    if head in ('4', '8'):          # 430xxx / 8xxxxx 北交所
        return 'BJ'
    raise SourceAdapterError('裸代码 %s 的首位无法确定交易所（只支持 6/9=SH、0/2/3=SZ、4/8=BJ）'
                             % digits)


def normalize_daily_bar(
    raw: pd.DataFrame,
    column_map: Mapping[str, str],
    *,
    volume_in_lots: bool = False,
    symbol: Optional[str] = None,
) -> pd.DataFrame:
    """源日线帧 → 标准日线帧（只含 `DAILY_BAR_COLUMNS`，顺序固定）。

    参数:
        raw: 源返回的原始帧。
        column_map: 源列名 → 标准列名的映射（模块级常量，见「源字段名不得泄漏」）。
        volume_in_lots: 源的成交量单位是否为「手」（是则 ×100）。
        symbol: 源不返回 symbol 列时，用请求参数回填的标的代码。

    异常:
        SourceAdapterError: 缺少必需的源列、列不是数值、日期解析不了、或
            `symbol` 既不在帧里也没作为参数给出、或 `trade_date` 里有空值。
    """
    context = '日线归一化'
    source_required = [src for src, std in column_map.items() if std in DAILY_BAR_COLUMNS]
    frame = _select(raw, column_map, DAILY_BAR_COLUMNS, source_required, context)
    if frame.shape[0] == 0:
        return _empty(*DAILY_BAR_COLUMNS)
    if 'symbol' not in frame.columns:
        if symbol is None:
            raise SourceAdapterError('%s：帧里没有 symbol 列，也没有传 symbol= 回填参数'
                                     % context)
        frame['symbol'] = symbol
    frame['symbol'] = _symbols(frame['symbol'], context)
    frame['trade_date'] = _to_dates(frame['trade_date'], 'trade_date', context)
    _require_present(frame['trade_date'], 'trade_date', context)
    for column in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        frame[column] = _to_numbers(frame[column], column, context)
    for column in ('open', 'high', 'low', 'close'):
        # 契约 §2.3：不复权原始价，保留 4 位小数（`numeric(18,4)`）。
        frame[column] = frame[column].round(PRICE_DECIMALS)
    if volume_in_lots:
        # 契约 §2.3 / `dc_daily_bar.volume` 注释：单位是股；源给手 ⇒ ×100。
        frame['volume'] = frame['volume'] * 100
    return frame.loc[:, list(DAILY_BAR_COLUMNS)]


def normalize_financial(
    raw: pd.DataFrame,
    column_map: Mapping[str, str],
    *,
    symbol: Optional[str] = None,
    report_type: Optional[str] = None,
    roe_is_percent: bool = False,
) -> pd.DataFrame:
    """源财务帧 → 标准财务帧。

    **`announce_date` 缺失必须抛异常**（契约 §3.2 与验收表都把这条写成硬要求）：
    源只给报告期就给不出「这份报表是哪天公开的」，而 PIT 可见性完全建立在这个日期上。
    用 `period_end` 冒充 `announce_date` 会让一份 3 个月后才公布的报表在报告期当天
    就「可见」—— 那是最经典的一类未来函数，而且它只表现为回测收益变漂亮。

    参数:
        report_type: 报表类型**由请求参数决定**时直接给（东财就是这样：一个
            `reportName` 一种报表，响应里没有报表名列，见契约附录 B12）。
            这里**没有**「源报表名 → `REPORT_TYPES` 取值」的映射表参数：本切片的
            三个源没有任何一家在响应里给报表名（B12 实测东财只给
            `REPORT_TYPE_CODE`、没有 `REPORT_TYPE`），为一张没有生产者的映射表
            留参数就是留一条走不到的分支。真遇到这样的源再加。
        roe_is_percent: 源的 ROE 是百分数时置 True（÷100）。契约 §2.3 的 `roe`
            是小数比率、可为负、区间 [-1, 5]，**不是**百分数。
    """
    context = '财务归一化'
    source_required = [src for src, std in column_map.items()
                       if std in FINANCIAL_REQUIRED_COLUMNS]
    frame = _select(raw, column_map, FINANCIAL_COLUMNS, source_required, context)
    if frame.shape[0] == 0:
        return _empty(*FINANCIAL_COLUMNS)
    if 'symbol' not in frame.columns:
        if symbol is None:
            raise SourceAdapterError('%s：帧里没有 symbol 列，也没有传 symbol= 回填参数'
                                     % context)
        frame['symbol'] = symbol
    frame['symbol'] = _symbols(frame['symbol'], context)
    for column in ('period_end', 'announce_date'):
        frame[column] = _to_dates(frame[column], column, context)
        _require_present(frame[column], column, context)
    for column in FINANCIAL_SUBJECT_COLUMNS:
        if column not in frame.columns:
            # 科目缺失补 NaN 而不是让列消失：列形状稳定，缺失由 `missing_ratio` 报出来。
            frame[column] = pd.Series([None] * frame.shape[0], index=frame.index, dtype=object)
        frame[column] = _to_numbers(frame[column], column, context)
    if roe_is_percent:
        # 契约 §2.3：roe 是小数比率（可为负，区间 [-1, 5]），源给百分数时 ÷100。
        frame['roe'] = frame['roe'] / 100.0
    if report_type is not None:
        # 整列一个值：报表类型来自**请求**（东财），所以它不可能是「有的行有、有的行没有」。
        frame['report_type'] = report_type
    elif 'report_type' not in frame.columns:
        raise SourceAdapterError(
            '%s：帧里没有 report_type 列，也没有传 report_type= —— 它是 '
            '`dc_financial` 主键的一部分，缺了它这一批数据不知道该归到哪张报表'
            % context)
    unmapped = sorted(set(value for value in frame['report_type'] if value not in REPORT_TYPES))
    if unmapped:
        # `ck_dc_fin_report_type` 只认 4 个取值，而 `report_type` 是主键的一部分。
        # 不校验就写进去，等于把一批行归到「未知报表类型」这一类里 —— 它不会报错，
        # 只会让「同一报告期的三张表」少一张。所以这里必须炸，而不是放行让 CHECK 拒。
        # 这道守卫现在最可能的触发方式是**调用方把 report_type= 传错**（比如把
        # `INCOME_STATEMENT` 当成取值），所以在归一化这一步就拦，别让它跑到数据库那层。
        raise SourceAdapterError('%s：report_type 取值 %s 不在 %s 里' % (
            context, unmapped, list(REPORT_TYPES)))
    return frame.loc[:, list(FINANCIAL_COLUMNS)]


def normalize_index_members(
    raw: pd.DataFrame,
    column_map: Mapping[str, str],
    *,
    index_code: Optional[str] = None,
    weight_is_percent: bool = False,
) -> pd.DataFrame:
    """源指数成分股帧 → 标准区间帧（D5 的区间表，不是逐日快照）。

    `effective_to` 的**空值必须保持为空**：`db/data_center.sql` 的注释写着
    「NULL 表示至今仍在成分内 —— 查询必须显式处理 NULL」。用一个哨兵日期
    （`9999-12-31` 之类）去填它，会让查询里的 `a <= x < effective_to` 静默地
    开始对「至今仍在成分内」的票返回 FALSE，也就是把现成成分股整批判成非成分股。
    """
    context = '指数成分股归一化'
    source_required = [src for src, std in column_map.items()
                       if std in ('index_code', 'symbol', 'effective_from')]
    frame = _select(raw, column_map, INDEX_MEMBER_COLUMNS, source_required, context)
    if frame.shape[0] == 0:
        return _empty(*INDEX_MEMBER_COLUMNS)
    if 'index_code' not in frame.columns:
        if index_code is None:
            raise SourceAdapterError('%s：帧里没有 index_code 列，也没有传 index_code= 回填参数'
                                     % context)
        frame['index_code'] = index_code
    frame['index_code'] = _symbols(frame['index_code'], context)
    frame['symbol'] = _symbols(frame['symbol'], context)
    frame['effective_from'] = _to_dates(frame['effective_from'], 'effective_from', context)
    _require_present(frame['effective_from'], 'effective_from', context)
    if 'effective_to' not in frame.columns:
        frame['effective_to'] = pd.Series([None] * frame.shape[0], index=frame.index, dtype=object)
    else:
        # 空串 / `--` / `N/A` 都要变成 None，**不能**变成某个日期。
        frame['effective_to'] = _to_dates(frame['effective_to'], 'effective_to', context)
    if 'weight' not in frame.columns:
        frame['weight'] = pd.Series([None] * frame.shape[0], index=frame.index, dtype=object)
    frame['weight'] = _to_numbers(frame['weight'], 'weight', context)
    if weight_is_percent:
        # 契约 §2.3 / `ck_dc_member_weight_range`：0~1 小数，禁止存百分数。
        frame['weight'] = frame['weight'] / 100.0
    return frame.loc[:, list(INDEX_MEMBER_COLUMNS)]


# ── 三个 normalize_* 共用的机械步骤 ────────────────────────────────────────────
# 这些函数只做「机械」的事（选列、转类型、换算），**不做判断**。判断（缺列怎么办、
# 越界算不算失败）留在调用它的函数里，或者留在 `validate_frame` 里。


def _select(raw: Any, column_map: Mapping[str, str], output_columns: Sequence[str],
            required_source: Sequence[str], context: str) -> pd.DataFrame:
    """源帧 → 只含标准列的帧。**源列名在这里消失**，后面没有任何一步再碰源列名。

    * 必需源列缺失 ⇒ `SourceAdapterError` 并**点名是哪一列**：接口改版/限流残帧
      与「这段区间本来就没数据」是两件事，必须能分辨，否则一个字段改名会让整条
      生产链路安静地写进一批空值。
    * 两个源列映射到同一个标准列 ⇒ 异常。静默取其中一个，等于让下游拿到一半数据。
    * 源特有列直接不在 `keep` 里 ⇒ 丢弃是结构性的，不靠谁记得去 drop。
    """
    if raw is None or not isinstance(raw, pd.DataFrame):
        raise SourceAdapterError('%s：源返回的不是 DataFrame，而是 %s'
                                 % (context, type(raw).__name__))
    if raw.shape[0] == 0 and len(raw.columns) == 0:
        return _empty(*output_columns)
    missing = [name for name in required_source if name not in raw.columns]
    if missing:
        # 报错里**同时**给源列名和它对应的标准列名：只看源列名，读的人不知道缺的是
        # 哪个 PIT 字段（`NOTICE_DATE` 缺了意味着「这份报表的可见日期没了」）；
        # 只看标准列名，读的人不知道要去源那边加哪一列。两个都要。
        described = ', '.join('%s（→ %s）' % (name, column_map[name]) for name in missing)
        raise SourceAdapterError('%s：源返回的帧缺少必需列 %s（实际列 %s）—— '
                                 '接口改版或被限流截断，不猜、不补'
                                 % (context, described, ', '.join(map(str, raw.columns))))
    renamed = raw.rename(columns=dict(column_map))
    duplicated = sorted(set(name for name in renamed.columns
                            if list(renamed.columns).count(name) > 1))
    if duplicated:
        raise SourceAdapterError('%s：多个源列映射到了同一标准列 %s —— 映射表写错了'
                                 % (context, ', '.join(map(str, duplicated))))
    keep = [name for name in output_columns if name in renamed.columns]
    return renamed.loc[:, keep].copy()


#: 源用来表示「这一格没有值」的写法。要在 `to_datetime` **之前**替换掉：
#: `pd.to_datetime(['--'])` 是抛错，而在源那边 `--` 的意思是「没有」，不是「格式坏了」。
_BLANK_TOKENS = ('', '-', '--', '/', 'n/a', 'na', 'nan', 'none', 'null', 'nat')


def _blank_to_null(series: pd.Series) -> pd.Series:
    return series.where(~series.map(_is_blank), None)


def _is_blank(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _BLANK_TOKENS


def _to_dates(series: pd.Series, column: str, context: str) -> pd.Series:
    """→ 由 `datetime.date` / `None` 组成的 object 序列。

    为什么不用 `datetime64`：契约 §3.2 要求「`trade_date` 为 `date` 类型」，而
    `Timestamp` 带时刻，会在落库/比较时被隐式当成本地时间。`db/data_center.sql`
    那边也是 `date` 列 —— 边界上少一个类型歧义，就少一类只能靠对答案才发现的 bug。
    """
    try:
        parsed = pd.to_datetime(_blank_to_null(series), errors='raise')
    except (ValueError, TypeError) as exc:
        raise SourceAdapterError('%s：列 %s 解析不成日期（%s）' % (context, column, exc)) from exc
    return pd.Series([None if pd.isna(value) else value.date() for value in parsed],
                     index=series.index, dtype=object, name=column)


def _to_numbers(series: pd.Series, column: str, context: str) -> pd.Series:
    """→ 数值列。`errors='raise'` 是刻意的。

    `errors='coerce'` 会把脏值变成 NaN，NaN 落库变 NULL，NULL 又被下游读成
    「这一格没有数据」—— 一次格式错误就这样变成数据空洞，而报告里什么都看不出来。
    """
    try:
        return pd.to_numeric(series, errors='raise')
    except (ValueError, TypeError) as exc:
        raise SourceAdapterError('%s：列 %s 不是数值（%s）' % (context, column, exc)) from exc


def _require_present(series: pd.Series, column: str, context: str) -> None:
    """对应 DDL 里的 `NOT NULL`：主键/可见性依赖的日期列必须有值。"""
    if bool(series.isna().any()):
        raise SourceAdapterError('%s：列 %s 有 %d 行是空的，但它是 NOT NULL（DDL）'
                                 % (context, column, int(series.isna().sum())))


def _symbols(series: pd.Series, context: str) -> pd.Series:
    try:
        values = [normalize_symbol(value) for value in series]
    except SourceAdapterError as exc:
        raise SourceAdapterError('%s：%s' % (context, exc)) from exc
    return pd.Series(values, index=series.index, dtype=object, name=series.name)


def validate_frame(frame: Any) -> ValidationReport:
    """校验归一化后的帧，返回契约 §3.9 的 `ValidationReport`。

    这是适配器 `validate()` 的唯一实现点。判据逐条对应 `db/data_center.sql` 里的
    **具名 CHECK 约束**（`ck_dc_bar_price_positive` 等），这样「适配器拦下了什么」
    和「数据库会拒掉什么」是同一份规则的两个执行点，而不是两套口径。

    **空帧不是通过**：0 行意味着没有可校验的东西，`is_valid=False` 并带一条硬错误。
    契约 §2.4 总原则写明「所有『找不到数据』的分支都必须显式失败，不允许返回空集」，
    而「提取为空却打印 PASS」正是本项目反复踩过的那类假绿。

    本函数**不抛异常**：它产出报告。`is_valid=False` 的含义是「`DataCenter` 不得写入」，
    调用方按此决定是重试、降级备源，还是把这批数据标成 `PARTIAL`。
    """
    if not isinstance(frame, pd.DataFrame):
        return ValidationReport(is_valid=False, row_count=0,
                                errors=['不是 DataFrame 而是 %s：校验无从进行'
                                        % type(frame).__name__])
    if frame.shape[0] == 0:
        return ValidationReport(
            is_valid=False, row_count=0,
            errors=['帧为空：0 行不构成校验通过（契约 §2.4 不允许用空集表示「找不到数据」，'
                    '空集必须由调用方翻译成显式失败）'])

    errors: List[str] = []
    warnings: List[str] = []
    present = [name for name in frame.columns if name in STANDARD_COLUMNS]
    leaked = sorted(set(name for name in frame.columns if name not in STANDARD_COLUMNS))
    if leaked:
        errors.append('非标准列 %s：源字段名不得泄漏到输出列（契约 §3.2 验收表）'
                      % ', '.join(map(str, leaked)))
    missing_ratio = dict((name, float(frame[name].isna().mean())) for name in present)
    for name, ratio in sorted(missing_ratio.items()):
        if ratio > 0.05:
            warnings.append('列 %s 缺失率 %.1f%%' % (name, ratio * 100.0))

    kind, required, checks = _match_schema(frame.columns)
    if kind is None:
        errors.append('列集合不匹配任何标准 schema（日线 %s / 财务 %s / 成分股 %s）：实际 %s'
                      % (list(DAILY_BAR_COLUMNS), list(FINANCIAL_REQUIRED_COLUMNS),
                         list(INDEX_MEMBER_COLUMNS), [str(name) for name in frame.columns]))
        return ValidationReport(is_valid=False, row_count=frame.shape[0], errors=errors,
                                warnings=warnings, missing_ratio=missing_ratio)

    for column in required:
        if column not in frame.columns:
            errors.append('%s schema 缺少必需列 %s' % (kind, column))
    _check_dates(frame, kind, errors)
    for tag, message in checks:
        detail = _violation(frame, tag)
        if detail:
            errors.append('%s —— %s' % (message, detail))
    duplicates = frame.duplicated(subset=[c for c in _PRIMARY_KEYS[kind] if c in frame.columns])
    if bool(duplicates.any()):
        errors.append('%s 主键 %s 有 %d 行重复'
                      % (kind, list(_PRIMARY_KEYS[kind]), int(duplicates.sum())))
    if kind == 'daily' and 'volume' in frame.columns:
        zero = int((frame['volume'] == 0).sum())
        if zero:
            warnings.append('%d 行 volume=0（停牌或源未给成交量）' % zero)
    return ValidationReport(is_valid=not errors, row_count=frame.shape[0], errors=errors,
                            warnings=warnings, missing_ratio=missing_ratio)


_PRIMARY_KEYS = {
    'daily': ('symbol', 'trade_date'),
    'financial': ('symbol', 'report_type', 'period_end'),
    'index': ('index_code', 'symbol', 'effective_from'),
}

#: 校验项写成一列 `(标签, 判据)` 而不是一串 if：判据是**数据**，于是「哪些规则被检查过」
#: 可以被打印出来、被触发测试逐一打中。`tools/verify_data_center_adapter.py` 的
#: 非空转守卫要能看出「规则表空了」，写成一串 if 就没法看。
_CHECK_SPECS = {
    'daily': (
        ('daily-price-positive', '价格必须 > 0（`ck_dc_bar_price_positive`）'),
        ('daily-ohlc-order', 'OHLC 顺序必须满足 high >= open/low/close 且 low <= open/close'
                             '（`ck_dc_bar_ohlc_order`）'),
        ('daily-nonneg', 'volume / amount 必须 >= 0（`ck_dc_bar_volume_nonneg`）'),
    ),
    'financial': (
        ('financial-report-type', 'report_type 必须是 %s 之一（`ck_dc_fin_report_type`）'
                                  % (list(REPORT_TYPES),)),
        ('financial-announce-after-period',
         'announce_date 必须 >= period_end（`ck_dc_fin_announce_after_period`）'),
        ('financial-roe-range', 'roe 必须为 NULL 或落在 [-1, 5]（`ck_dc_fin_roe_range`）'),
    ),
    'index': (
        ('index-effective-range',
         'effective_to 必须为 NULL 或 > effective_from（`ck_dc_member_effective_range`）'),
        ('index-weight-range', 'weight 必须为 NULL 或落在 [0, 1]（`ck_dc_member_weight_range`）'),
    ),
}


def _match_schema(columns: Any):
    """按列集合认出这是哪一类帧。返回 (kind, 必需列, 判据表)。

    顺序有意如此：日线的 8 列最具体，先判它不会被财务帧误命中。
    """
    have = set(map(str, columns))
    for kind, required in (('daily', DAILY_BAR_COLUMNS),
                           ('financial', FINANCIAL_REQUIRED_COLUMNS),
                           ('index', INDEX_MEMBER_COLUMNS)):
        if set(required) <= have:
            return kind, required, _CHECK_SPECS[kind]
    return None, (), ()


def _check_dates(frame: pd.DataFrame, kind: str, errors: List[str]) -> None:
    """契约 §3.2 要求日期是 `date` 类型。

    这里查的是**元素类型**而不是 `dtype`：`datetime64` 列里的 `Timestamp` 也是
    「日期」，但它带时刻，落库时会被隐式当成本地时间。契约要的是 `date`。
    """
    fields = _PRIMARY_KEYS[kind]
    for column in frame.columns:
        if column not in fields or column not in STANDARD_COLUMNS:
            continue
        if not any(name == column for name in (
                'trade_date', 'period_end', 'announce_date',
                'effective_from', 'effective_to')):
            continue
        bad = [value for value in frame[column] if value is not None and type(value) is not date]
        if bad:
            errors.append('列 %s 不是 date 类型（样例 %r）' % (column, bad[0]))


def _violation(frame: pd.DataFrame, tag: str) -> Optional[str]:
    """判据实现的唯一位置。`tag` 与 `_CHECK_SPECS` 的标签一一对应。

    返回**违规定位的证据串**（第几行、哪个列、什么值）而不是 bool：一条「OHLC 顺序错了」
    的报告没有可操作性 —— 校验会跑在成千上万行上，只有告诉人「哪一行、哪两列」，它才
    是一份能拿去改的东西。返回 `None` 表示通过。

    注意这里**逐行迭代**而不是向量化：向量化快，但它只能回答「有没有违规」，回答不了
    「哪一行违规」；而这些校验的输入是「一个标的 × 一段区间」，行数量级在千以内，
    用一丁点性能换「报告能直接定位」是划算的。
    """
    if tag == 'daily-price-positive':
        for name in ('open', 'high', 'low', 'close'):
            for position, value in enumerate(frame[name]):
                if pd.isna(value) or value <= 0:
                    return '第 %d 行 %s=%r' % (position, name, value)
        return None
    if tag == 'daily-ohlc-order':
        for position in range(frame.shape[0]):
            row = frame.iloc[position]
            if (row['high'] >= row['low'] and row['high'] >= row['open']
                    and row['high'] >= row['close']
                    and row['low'] <= row['open'] and row['low'] <= row['close']):
                continue
            return ('第 %d 行 open=%r high=%r low=%r close=%r'
                    % (position, row['open'], row['high'], row['low'], row['close']))
        return None
    if tag == 'daily-nonneg':
        for name in ('volume', 'amount'):
            for position, value in enumerate(frame[name]):
                if pd.isna(value) or value < 0:
                    return '第 %d 行 %s=%r' % (position, name, value)
        return None
    if tag == 'financial-report-type':
        for position, value in enumerate(frame['report_type']):
            if value not in REPORT_TYPES:
                return '第 %d 行 report_type=%r' % (position, value)
        return None
    if tag == 'financial-announce-after-period':
        for position, (announce, period) in enumerate(
                zip(frame['announce_date'], frame['period_end'])):
            if announce is None or period is None:
                continue    # NULL 由 DDL 的 NOT NULL 管，不在这里冒充「区间违规」
            if announce < period:
                return ('第 %d 行 announce_date=%s < period_end=%s'
                        % (position, announce, period))
        return None
    if tag == 'financial-roe-range':
        for position, value in enumerate(frame['roe']):
            if pd.isna(value) or -1 <= value <= 5:
                continue
            return '第 %d 行 roe=%r' % (position, value)
        return None
    if tag == 'index-effective-range':
        for position, (start, end) in enumerate(
                zip(frame['effective_from'], frame['effective_to'])):
            if start is None or end is None or pd.isna(end):
                continue    # effective_to IS NULL = 至今仍在成分内，这是合法值
            if not end > start:
                return '第 %d 行 effective_from=%s effective_to=%s' % (position, start, end)
        return None
    if tag == 'index-weight-range':
        for position, value in enumerate(frame['weight']):
            if pd.isna(value) or 0 <= value <= 1:
                continue
            return '第 %d 行 weight=%r' % (position, value)
        return None
    raise AssertionError('未知的校验标签 %r：判据表与实现脱节，沉默的 None 会伪造出绿'
                         % tag)



def classify_source_failure(exc: BaseException) -> str:
    """把源侧的任意异常映射到 `SOURCE_FAILURE_KINDS` 里的**一个**类别。

    判定顺序**从具体到笼统**，这条顺序就是本函数的全部风险，不能凭感觉调：
    `socket.timeout` 就是 `TimeoutError`、是 `OSError` 的子类；`HTTPError` 是
    `URLError` 的子类、`URLError` 又是 `OSError` 的子类。顺序写反的话，
    「超时」「限流」「鉴权被拒」会一起被最笼统的那一档截走，而返回值看上去
    仍然是个**合法**类别 —— 静默降级，没有报错。所以
    `tests/test_data_center_adapter.py` 里有一条专门盯这条顺序的用例。

    `UNKNOWN` 是**真的兜底**：这里不猜。猜错的类别比没有类别更坏 —— 它会让
    调用方对着一类它其实不认识的东西执行重试或放弃。

    本函数体内**不出现类别名以外的字符串字面量**，这是刻意的：
    `tools/verify_data_center_adapter.py` 的 A11 靠这条性质静态判定
    「返回值 ⊆ 声明表」，混进一句提示语就会让那条判定失去意义。
    """
    if isinstance(exc, ImportError):
        return 'SDK_MISSING'
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return 'SOURCE_RATE_LIMITED'
        if exc.code in (401, 403):
            return 'SOURCE_AUTH'
        return 'SOURCE_UNREACHABLE'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return 'SOURCE_TIMEOUT'
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return 'SOURCE_UNREACHABLE'
    if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
        return 'SOURCE_SCHEMA_MISMATCH'
    return 'UNKNOWN'


class SourceAdapter(ABC):
    """数据源适配器基类。一个数据源一个子类，负责把源特有格式归一化到本契约的标准 schema。

    职责:
        - 拉取（网络/文件 IO）
        - 字段名、单位、空值约定的归一化
        - 最低限度的格式校验（不是业务校验）

    不做:
        - 不写数据库（由 DataCenter 统一写入，见 D1/D10）
        - 不判断 as_of_date（采集关心「现在取得到什么」，不是「过去看得到什么」）
    """

    @abstractmethod
    def source_name(self) -> str:
        """返回数据源标识（写入每行的 `source` 列）。"""
        raise NotImplementedError

    @abstractmethod
    def fetch_daily_bar(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        """拉取日线行情（不复权），列名必须是 `DAILY_BAR_COLUMNS`。"""
        raise NotImplementedError

    @abstractmethod
    def fetch_financial(self, symbols: List[str], period_end: date) -> pd.DataFrame:
        """拉取财务数据，必须包含 `announce_date`（缺失即不合格，见 D4）。"""
        raise NotImplementedError

    @abstractmethod
    def fetch_index_members(self, index_code: str, as_of_date: date) -> pd.DataFrame:
        """拉取指数成分股的**历史调仓记录**（只提供当前名单的源必须抛异常，见 D5）。"""
        raise NotImplementedError

    @abstractmethod
    def validate(self, frame: pd.DataFrame) -> ValidationReport:
        """校验归一化后的数据帧；`is_valid=False` 时 `DataCenter` 不得写入。"""
        raise NotImplementedError


# ── 唯一网络出口 ─────────────────────────────────────────────────────────────
def _http_get_json(url: str, params: Mapping[str, Any], *,
                   timeout: float = HTTP_TIMEOUT_SECONDS,
                   opener: Optional[Callable[..., Any]] = None) -> Any:
    """GET `url?params` 并解成 JSON。**全模块只有这里碰网络。**

    参数:
    opener: 替换用的请求函数（签名同 `urllib.request.urlopen` 且被当作上下文管理器用）。
        默认 None 即真实请求。测试传自己的实现就能对着本地 HTTP 服务跑完
        整条链路（urlencode → 头 → 状态码 → 解帧），**不碰外网**。

    **不在这里 try/except**。把 `HTTPError` / `socket.timeout` 就地译成一句人话，
    会让 `classify_source_failure` 失去分类依据 —— 401 与 503 会长得一样，于是
    「换凭证」和「过一会儿重试」这两种完全不同的动作就分不出来了。异常原样上抛，
    由 `_AdapterBase._call` 这个唯一的翻译点分类。

    JSON 解不出来时抛的是 `json.JSONDecodeError`（`ValueError` 的子类）——
    它会落到 `SOURCE_SCHEMA_MISMATCH`：源返回了网页/公告而不是数据时，调用方
    该做的是改请求或改解析，不是重试。
    """
    request = urllib.request.Request(
        url + '?' + urllib.parse.urlencode(dict(params)),
        headers={'User-Agent': HTTP_USER_AGENT},
    )
    real_opener = urllib.request.urlopen if opener is None else opener
    with real_opener(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode('utf-8'))


def _eastmoney_page(payload: Any, report_name: str) -> Tuple[List[Any], int]:
    """解东财数据中心的统一信封 → `(rows, pages)`。

    信封形状 `{'success': bool, 'code': int, 'message': str,
    'result': {'data': [...], 'pages': int}}` 是 **2026-09-24 实测**的
    （证据 `tools/eastmoney-transport-smoke-report.txt`），不是照文档写的。

    两处刻意的判定：

    * `success` 不为真 ⇒ 源**在带内**拒绝了这次请求（HTTP 200，而错误码和人话都在信封
      里）。这和「源返回了一张网页」是同一类问题：重试一万次也不会让参数变合法 ⇒
      归 `SOURCE_SCHEMA_MISMATCH`（动作是改请求/改映射表），**不是**可重试类。
    * `result` 为空 ⇒ 源正常回复但没有数据。**不抛**：`errors.py` 的分类表刻意没有
      「源没给数据」这一类（见 `SOURCE_FAILURE_KINDS` 上方的注释），空结果就是空帧。
    """
    if not isinstance(payload, Mapping):
        raise SourceAdapterError(
            '东财 %s 的响应不是 JSON 对象（收到 %s）'
            % (report_name, type(payload).__name__),
            source='eastmoney', kind='SOURCE_SCHEMA_MISMATCH')
    if not payload.get('success'):
        raise SourceAdapterError(
            '东财 %s 在信封里拒绝了这次请求：code=%r message=%r —— 动作是改请求参数，'
            '不是重试（HTTP 200 不代表这次查询合法）'
            % (report_name, payload.get('code'), payload.get('message')),
            source='eastmoney', kind='SOURCE_SCHEMA_MISMATCH')
    result = payload.get('result') or {}
    if not isinstance(result, Mapping):
        raise SourceAdapterError(
            '东财 %s 的 result 不是 JSON 对象（收到 %s）'
            % (report_name, type(result).__name__),
            source='eastmoney', kind='SOURCE_SCHEMA_MISMATCH')
    rows = result.get('data') or ()
    try:
        pages = int(result.get('pages') or 0)
    except (TypeError, ValueError) as exc:
        raise SourceAdapterError(
            '东财 %s 的 result.pages 不是整数：%r'
            % (report_name, result.get('pages')),
            source='eastmoney', kind='SOURCE_SCHEMA_MISMATCH') from exc
    return list(rows), pages


class _AdapterBase(SourceAdapter):
    """三个子类共用的管道。**不是契约类** —— 契约 §3.2 只画了基类与三个子类。

    放在这里而不复制进三个子类的东西只有两样：`validate` 的委托，和
    「源这一侧的任何异常都翻译成 `SourceAdapterError`」的包装。后者尤其不该复制 ——
    漏掉一处，那一处就会开始向上层抛 `ModuleNotFoundError` / `KeyError` / `ValueError`，
    而那些类型在上层读起来是「调用方写错了」，会被当成 bug 去查错地方。
    """

    #: 子类覆盖。契约给 `source_name` 写的是抽象方法而不是类属性，所以这里保留
    #: 一个私有常量，由子类的 `source_name()` 返回 —— 形状对契约负责，取值只写一处。
    _SOURCE = ''

    def __init__(self, fetch: Optional[Callable[..., pd.DataFrame]] = None) -> None:
        """参数:
        fetch: 真实取数函数。**注入**而不是在方法体里写死网络调用，是这一层能被
            离线测试的唯一原因（测试注入假函数，永不联网）。传 None 时用模块里
            那个未经调用验证的默认实现。
        """
        self._fetch = fetch if fetch is not None else self._default_fetch

    def source_name(self) -> str:
        return self._SOURCE

    def validate(self, frame: pd.DataFrame) -> ValidationReport:
        """委托给 `validate_frame`。三个子类的判据是**同一套**（标准 schema 与 DDL
        的 CHECK 是同一条规则），所以没有理由让它们各自实现一份。"""
        return validate_frame(frame)

    def _default_fetch(self, **kwargs: Any) -> pd.DataFrame:
        """子类覆盖为真实的源调用（惰性导入）。"""
        raise SourceAdapterError(
            '%s 适配器没有注入 fetch 函数，也没有默认实现' % type(self).__name__,
            source=self._SOURCE, kind='UNSUPPORTED')

    def _call(self, **kwargs: Any) -> pd.DataFrame:
        """调用注入的取数函数，并把源侧异常统一翻译成 `SourceAdapterError`。

        `SourceAdapterError` 原样透传（它已经是这个类型的语义，而且带着适配器
        自己判定好的类别）；其余一律包一层，并在消息里保留原异常类型名 ——
        丢掉类型名会让「源库没装」和「源限流」在日志里长得一模一样。

        类别由 `classify_source_failure` 判定，`retryable` 由类别推出来，
        `source` 由适配器自己填。这三样加起来才写得成重试策略。
        """
        try:
            return self._fetch(**kwargs)
        except SourceAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 这一层的职责就是兜住源侧的一切
            raise SourceAdapterError(
                '%s 取数失败（%s）：%s' % (self._SOURCE, type(exc).__name__, exc),
                source=self._SOURCE,
                kind=classify_source_failure(exc),
            ) from exc


def _empty(*columns: str) -> pd.DataFrame:
    """空帧也必须带标准列。

    返回一个「什么都没有、列名齐全」的帧，是为了让下游不必区分「没有数据」和
    「格式不对」：格式不对在归一化时就已经抛了，能走到下游的空帧一定是合规形状。
    """
    return pd.DataFrame({name: [] for name in columns})


class AKShareAdapter(_AdapterBase):
    """AKShare（东方财富系接口）—— MVP 主源（`SourcePriority.PRIMARY`）。

    覆盖行情 + 财务 + 指数成分股 + 龙虎榜（契约 §3.2 的表）。龙虎榜没有对应的
    标准 schema，不在本切片内。
    """

    _SOURCE = 'akshare'
    priority = SourcePriority.PRIMARY

    @staticmethod
    def _default_fetch(symbol: str = '', start: date = None, end: date = None,
                       **kwargs: Any) -> pd.DataFrame:
        """**未经调用验证**：见模块 docstring 的「未验证的部分」。

        惰性导入 akshare 是刻意为之：源库缺失时必须由 `_call` 翻译成
        `SourceAdapterError`（DATA_005），而不是让裸 `ImportError` 逃出去。
        """
        import akshare  # noqa: PLC0415 —— 惰性导入，见上文
        return akshare.stock_zh_a_hist(
            symbol=code_digits(symbol),
            period='daily',
            start_date=start.strftime('%Y%m%d'),
            end_date=end.strftime('%Y%m%d'),
            adjust='',  # 不复权（D6：库中存原始价，复权是读取时的视图行为）
        )

    def fetch_daily_bar(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        """逐标的取数后拼接。契约给的入参是 `symbols` 列表，而 akshare 的日线接口
        一次只吃一个代码，所以循环是接口形状逼出来的，不是设计选择。"""
        frames = []
        for symbol in symbols:
            raw = self._call(symbol=symbol, start=start, end=end)
            frames.append(normalize_daily_bar(raw, AKSHARE_DAILY_BAR,
                                              volume_in_lots=AKSHARE_VOLUME_IN_LOTS,
                                              symbol=symbol))
        if not frames:
            return _empty(*DAILY_BAR_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    def fetch_financial(self, symbols: List[str], period_end: date) -> pd.DataFrame:
        raise SourceAdapterError(
            'akshare 财务接口（`stock_financial_abstract`）不含披露日期，'
            '契约 §3.2 要求 `announce_date` 缺失即不合格 —— 本适配器不提供该能力，'
            '财务请走 EastMoneyAdapter（DR 见数据中心契约附录 B）',
            source=self._SOURCE, kind='UNSUPPORTED')

    def fetch_index_members(self, index_code: str, as_of_date: date) -> pd.DataFrame:
        raise SourceAdapterError(
            'akshare 指数成分股接口只给**当前**名单，契约 D5 禁止用当前名单回溯历史 '
            '（幸存者偏差）；本适配器不提供该能力，指数成分股请走 EastMoneyAdapter'
            '（DR 见数据中心契约附录 B）',
            source=self._SOURCE, kind='UNSUPPORTED')


class BaostockAdapter(_AdapterBase):
    """Baostock —— MVP 备源（`SourcePriority.FALLBACK`）。

    覆盖行情。契约说它「质量标记更全」，但标准日线 schema 装不下那些标记列，
    本切片选择**丢弃**（见模块 docstring 末节与 `BAOSTOCK_DROPPED_MARKERS`）。
    """

    _SOURCE = 'baostock'
    priority = SourcePriority.FALLBACK

    @staticmethod
    def _default_fetch(symbol: str = '', start: date = None, end: date = None,
                       **kwargs: Any) -> pd.DataFrame:
        """**未经调用验证**（未安装 baostock、未联网）。

        baostock 是「登录 → 查询 → 登出」的有状态 SDK，比另外两个源多一层会话；
        这段代码的价值是证明惰性导入与 `_call` 包装的形状，不是证明它跑得通。
        """
        import baostock as bs  # noqa: PLC0415 —— 惰性导入，见上文
        login = bs.login()
        if getattr(login, 'error_code', '0') != '0':
            # baostock 的登录是**匿名**的、不收凭证，所以登录失败只可能是服务端
            # 不可用而不是凭证错 —— 归到「连不上」（可重试）而不是「鉴权」。
            # 这个判断未经联网验证（本机未装 baostock），所以只是分类不是结论。
            raise SourceAdapterError('baostock 登录失败：%s' % getattr(login, 'error_msg', ''),
                                     source='baostock', kind='SOURCE_UNREACHABLE')
        try:
            result = bs.query_history_k_data_plus(
                _baostock_code(symbol),
                'date,open,high,low,close,volume,amount',
                start_date=start.isoformat(), end_date=end.isoformat(),
                frequency='d', adjustflag='3')  # 3 = 不复权
            rows = []
            while result.error_code == '0' and result.next():
                rows.append(result.get_row_data())
            return pd.DataFrame(rows, columns=result.fields)
        finally:
            bs.logout()

    def fetch_daily_bar(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        frames = []
        for symbol in symbols:
            raw = self._call(symbol=symbol, start=start, end=end)
            frames.append(normalize_daily_bar(raw, BAOSTOCK_DAILY_BAR,
                                              volume_in_lots=BAOSTOCK_VOLUME_IN_LOTS,
                                              symbol=symbol))
        if not frames:
            return _empty(*DAILY_BAR_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    def fetch_financial(self, symbols: List[str], period_end: date) -> pd.DataFrame:
        raise SourceAdapterError('契约 §3.2 的表里 baostock 不覆盖财务，见附录 B',
                                 source=self._SOURCE, kind='UNSUPPORTED')

    def fetch_index_members(self, index_code: str, as_of_date: date) -> pd.DataFrame:
        raise SourceAdapterError('契约 §3.2 的表里 baostock 不覆盖指数成分股，见附录 B',
                                 source=self._SOURCE, kind='UNSUPPORTED')


class EastMoneyAdapter(_AdapterBase):
    """东方财富数据中心接口 —— 备源（`SourcePriority.FALLBACK`）。

    覆盖财务 + 指数成分股 + 资金流向（契约 §3.2 的表）。本切片只做前两项：
    资金流向没有对应的标准 schema。
    """

    _SOURCE = 'eastmoney'
    priority = SourcePriority.FALLBACK

    #: 「本源的一次财务取数」= 两张报表（契约附录 B12：没有任何单一 `reportName` 能同时
    #: 喂满收入类与资产负债类科目）。放类属性而不是模块级，是为了让它**不是一张映射表**：
    #: 真正被静态检查的映射表是 `EASTMONEY_INCOME_FINANCIAL` /
    #: `EASTMONEY_BALANCE_FINANCIAL`（两张都登记在门禁的 `COLUMN_MAPS` 里），报表类型取自
    #: `EASTMONEY_REPORT_TYPE`（唯一真源，这里不复制一份）。
    _FINANCIAL_REPORTS = (
        ('RPT_LICO_FN_CPD', EASTMONEY_INCOME_FINANCIAL),
        ('RPT_DMSK_FN_BALANCE', EASTMONEY_BALANCE_FINANCIAL),
    )

    @staticmethod
    def _default_fetch(**kwargs: Any) -> pd.DataFrame:
        """东财数据中心取数：**一个 `reportName` 一次请求，逐页取回拼成一帧**。

        为什么签名是 `**kwargs` 而不是位置参数（`symbol, report_name, ...`）：
        `tools/contract-signature-manifest.json` 把本函数的 `impl_params` 登记成
        `**kwargs`，理由是取数函数的参数形状是**实现细节**（契约只规定
        `fetch_financial(symbols, period_end)`）—— 写成位置参数会把这个测试注入点
        （`opener=`，与 `_http_get_json` 同款）挤到无处安放，而那个点正是整条链路
        能离线跑通的原因。

        参数（全部走 kwargs）：
            symbol: 标的，接受 `normalize_symbol` 的四种写法；发给源的是裸代码。
            report_name: 东财报表名，必须登记在 `EASTMONEY_REPORT_TYPE` 里。
            opener: 仅测试传（替换 `urllib.request.urlopen`）。生产路径不传。

        `period_end` **刻意不作为请求参数**：东财的日期过滤语法没有实测过，不猜。
        「只要哪一期」由调用方在归一化之后筛（见 `fetch_financial`）。
        """
        symbol = str(kwargs.get('symbol') or '')
        report_name = str(kwargs.get('report_name') or '')
        if not symbol:
            # 缺参数是**调用方**写错了，不是源的问题 —— 用 ValueError 表达，免得被
            # `_call` 翻译成 SourceAdapterError 之后被读成「这个源不可用」。
            raise ValueError('EastMoneyAdapter._default_fetch 需要 symbol 参数')
        if report_name not in EASTMONEY_REPORT_TYPE:
            raise SourceAdapterError(
                '东财 reportName %r 没有登记：本适配器只覆盖 %s —— 未登记的报表取回来也'
                '归一化不了（`report_type` 是 `dc_financial` 主键的一部分，猜一个值'
                '比不取更坏）'
                % (report_name, sorted(EASTMONEY_REPORT_TYPE)),
                source='eastmoney', kind='UNSUPPORTED')

        code = code_digits(symbol)
        opener = kwargs.get('opener')
        rows: List[Any] = []
        page = 1
        while True:
            payload = _http_get_json(
                EASTMONEY_DATA_API,
                {'reportName': report_name,
                 'columns': 'ALL',
                 # 过滤表达式用**裸代码**：实测 `(SECURITY_CODE="600000")` 有返回；
                 # 带后缀的写法没有实测过，不猜。
                 'filter': '(SECURITY_CODE="%s")' % code,
                 'pageNumber': page,
                 'pageSize': EASTMONEY_PAGE_SIZE,
                 'source': 'WEB',
                 'client': 'WEB'},
                opener=opener)
            page_rows, pages = _eastmoney_page(payload, report_name)
            if pages > EASTMONEY_MAX_PAGES:
                raise SourceAdapterError(
                    '东财 %s 报出 %d 页，超过上限 %d：**不截断**，宁可报错 —— '
                    '少取的页会变成静默缺失的数据'
                    % (report_name, pages, EASTMONEY_MAX_PAGES),
                    source='eastmoney', kind='SOURCE_SCHEMA_MISMATCH')
            rows.extend(page_rows)
            if page >= pages:
                break
            page += 1
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    def fetch_daily_bar(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        raise SourceAdapterError('契约 §3.2 的表里东财不覆盖行情（行情走 AKShare/Baostock）',
                                 source=self._SOURCE, kind='UNSUPPORTED')

    def fetch_financial(self, symbols: List[str], period_end: date) -> pd.DataFrame:
        """逐标的 × 逐报表取数，再按报告期筛。

        三个「为什么」：

        * **循环是接口形状逼出来的**：契约给的是 `(symbols, period_end)`，而源一次只吃
          一个代码、一张报表。
        * **一个报告期出两行，不是一行**：`report_type` 是 `dc_financial` 主键的一部分。
          把利润表与资产负债表拼成一行，会让资产负债类科目挂在一行 `report_type='INCOME'`
          的数据上 —— 那行不属于任何真实报表。
        * **按报告期筛在归一化之后做**：东财的日期过滤语法没实测过（见 `_default_fetch`），
          而在这里筛是能用本地帧测出来的行为。
        """
        frames = []
        for symbol in symbols:
            for report_name, column_map in self._FINANCIAL_REPORTS:
                # `_default_fetch` 里也拦同一个条件，但那是**取数函数**的守卫：
                # 注入了自定义 fetch 时它不生效，所以这里在取数之前再拦一次。
                report_type = EASTMONEY_REPORT_TYPE.get(report_name)
                if report_type is None:
                    raise SourceAdapterError(
                        '东财报表 %r 没有登记在 EASTMONEY_REPORT_TYPE 里' % report_name,
                        source=self._SOURCE, kind='UNSUPPORTED')
                raw = self._call(symbol=symbol, report_name=report_name)
                frames.append(normalize_financial(
                    raw, column_map, symbol=symbol, report_type=report_type,
                    # ROE 是百分数这个开关跟着**表**走，不跟着 reportName 走 ——
                    # 换一张表时不会漏改（只有利润表有 roe）。
                    roe_is_percent=(EASTMONEY_ROE_IS_PERCENT
                                    and 'roe' in column_map.values())))
        if not frames:
            return _empty(*FINANCIAL_COLUMNS)
        combined = pd.concat(frames, ignore_index=True)
        if combined.shape[0] == 0:
            # 空 concat 结果的列形状不好赖，统一回标准空帧。
            return _empty(*FINANCIAL_COLUMNS)
        return combined.loc[combined['period_end'] == period_end].reset_index(drop=True)

    def fetch_index_members(self, index_code: str, as_of_date: date) -> pd.DataFrame:
        raw = self._call(index_code=index_code, as_of_date=as_of_date)
        return normalize_index_members(raw, EASTMONEY_INDEX_MEMBER, index_code=index_code,
                                       weight_is_percent=EASTMONEY_WEIGHT_IS_PERCENT)


def code_digits(symbol: Any) -> str:
    """`600000.SH` → `600000`。给「只要裸代码」的源用（akshare）。

    先过 `normalize_symbol` 再切，是为了让 `sh.600000` 这种写法也能被接受；
    直接 `split('.')` 会把它切成 `sh` 和 `600000` 两半，然后静默地取错那半。
    """
    return normalize_symbol(symbol).split('.')[0]


def _baostock_code(symbol: Any) -> str:
    """`600000.SH` → `sh.600000`（baostock 的写法：小写前缀，点分隔）。"""
    normalized = normalize_symbol(symbol)
    digits, exchange = normalized.split('.')
    return '%s.%s' % (exchange.lower(), digits)
