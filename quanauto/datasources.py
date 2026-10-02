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

`akshare` / `baostock` 仍**没有安装**（它们在 `[datasources]` extra 里，装法
`pip install 'akshare>=1.18'` 或 `pip install 'baostock>=0.9.4'`，也可
`pip install -e ".[datasources]"`），也**从未联网核对**
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

**仍然没有实测过的**（不许读成「已验证」）：`pageSize` 的**上限**（实测过的是「这个取值被
遵守」）、东财的**日线端点**（`push2his` 间歇性拒连），以及 akshare / baostock 两源的一切。

**东财的日期过滤语法已于 2026-09-24 实测**（附录 B14）：`(SECURITY_CODE="600000")(REPORTDATE='2024-12-31')`
—— 单引号，且**每张报表用自己的日期键名**（利润表 `REPORTDATE`、资产负债表 `REPORT_DATE`），
键名写错会被源**当场拒**（`success=false` + `REPORT_DATE列不存在`）。但这份证据只有
**1 标的 × 2 报表 × 1 天**（换标的、换 `reportName` 都没测）⇒ 它在本模块里**只当省流量用，
不做正确性依赖**：`period_end` 在归一化之后**仍然**筛一遍（见 `fetch_financial`），
请求侧那份过滤哪天被源静默忽略，结果也不会多一行。

**tushare 的两张映射表（`TUSHARE_ADJ_FACTOR` / `TUSHARE_DIVIDEND`）同样没被真调用过**
（本机没有 `TUSHARE_TOKEN`，也不装 SDK）⇒ 它们的**键名照源文档写**，
`TUSHARE_DIVIDEND` 里 `ann_date` → `announce_date`、`cash_div_tax` → `cash_per_share`
两格尤其只是**读文档得出的口径**（税前/税后两列长得像，取错不报错）。这条与
`TUSHARE_ADJ_FACTOR` 的地位**完全一样**（B21 也没在真实券源上跑过，见附录 B21.3 末段），
额度不足/无凭证这件事在 B21/B22 里是同一条边界。

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
from datetime import date, timedelta
import json
import os
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

# 复权因子：契约 §3.2 的 `fetch_adjust_factor` 把 3 个列名写全了。逐字相等。
# `adjust_factor` 是**累乘因子**（无量纲、恒 > 0），不是「当日比例差」，也不是
# 「复权后价 / 复权前价」的某一日比值 —— 与 `dc_adjust_factor.adjust_factor`
# （`numeric(18,8)`、`ck_dc_factor_positive`）同义（契约 §2.3 / D6）。
ADJUST_FACTOR_COLUMNS = ('symbol', 'trade_date', 'adjust_factor')

# 分红：契约 §3.2 的 `fetch_dividend` 把 4 个列名写全了（2026-10-01 追加，附录 B22）。逐字相等。
# `cash_per_share` 是**每股税前现金分红（元/股）**，不是总额、不是比例、不是股息率；
# `0` 合法（只送转不派现的预案，`ck_dc_dividend_cash_nonneg` 允许 `>= 0`）。
# `ex_date` 是**事件日**（也是本条通道的过滤键），`announce_date` 是**唯一合法的可见性依据**
# （D4）—— 两者不能互相替代，也不能不去区分（见 `dc_dividend` 的三日期形状）。
DIVIDEND_COLUMNS = ('symbol', 'ex_date', 'announce_date', 'cash_per_share')

# 全量标准列：用来判「泄漏」——任何不在此集合里的列都是源字段名。
#
# `ADJUST_FACTOR_COLUMNS` 曾是**刻意排除**在外的（2026-09-29 随附录 B21 落地时）：
# 那时 `validate_frame` 对复权帧一条判据都没有，把一条没判据的 schema 混进来只会
# 让「校验通过」看起来比实际覆盖的多。**2026-09-29 晩 Ⅳ 判据接上后，排除它的理由
# 就没了** —— 而继续排除会**把那个误诊重新造出来**：`adjust_factor` 是契约 §3.2
# 写明的标准列，不在集合里就会被报成「源字段名泄漏到输出列」。
#
# `DIVIDEND_COLUMNS` 落地时（2026-10-01）**一次就把判据和它写在一起**（`_CHECK_SPECS`
# 的 `dividend` 那一支），所以它没有经历过那段「先排除、后补回」的过程。
STANDARD_COLUMNS = (DAILY_BAR_COLUMNS + FINANCIAL_COLUMNS + INDEX_MEMBER_COLUMNS
                    + ADJUST_FACTOR_COLUMNS + DIVIDEND_COLUMNS)

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

# ── 腾讯行情（`proxy.finance.qq.com`）日线 ─────────────────────────────────────
# 端点与全部单位、口径都是 **2026-09-24 实测**（数据中心契约附录 B16，探针
# `.rounds/_i2b1_probe.py`）：320 个交易日逐日与新浪对账，四价 0 处不一致。
#
# **这个源是契约 §3.2 那张表之外的第 4 个**，理由是实测的：东财 kline 端点在本机
# 两个 opener 下都拒连（`AKShareAdapter` 走的正是它），而新浪日线只有 6 列、**没有
# 成交额** —— `dc_daily_bar.amount` 是 `NOT NULL`，拿不到就只能合成一个，那是造数据。
# 腾讯这条是实测里唯一天然给出「契约 8 列」且价格是**不复权原始价**的一条。
TENCENT_KLINE_API = ('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get')

# 行是**字符串数组、没有列名** ⇒ 映射表的键是下标字符串。位置 2/3 的顺序反直觉
# （2 是收盘、不是最高），所以这里逐条写出下标而不是写「OHLC」。
# 位置 6 是 `{}`、7 是涨跌幅、9/10 是 `'0.00'` —— **没有一列能落进契约 schema**，
# 不登记即不映射（多余的列不允许跟着产出，见门禁 A8）。
TENCENT_DAILY_BAR = {
    '0': 'trade_date',
    '1': 'open',
    '2': 'close',
    '3': 'high',
    '4': 'low',
    '5': 'volume',
    '8': 'amount',
}
# 实测：与新浪「股」的比值 = 100.000099 / 99.999990 / 100.000207 ⇒ 单位是「手」。
TENCENT_VOLUME_IN_LOTS = True
# 实测：`成交额 ÷ (volume手 × 100)` 反推均价 6.5958 ∈ 新浪当日 [6.57, 6.63] ⇒ 单位是
# 「万元」；按「元」读会得到 0.0007 的均价。**两个假设恰好一个成立**才算数（探针的
# `pick_amount_unit`），所以这一条不是「看着像万元」。
TENCENT_AMOUNT_IN_WAN = True

# 单次请求要几个交易日。**取 320 是因为实测过 `count=320` 被接受**；
# 上限**没有实测**，所以刻意不往上试探 —— 真需要更长的区间就多翻几页（下面的
# `_default_fetch` 按 `end` 回溯分页），而不是去猜一个更大的 count 能不能被接受。
# 实测（2026-09-24，`_tencent-shape-probe.txt`）：`count=n` 回的是 **n+1 行**
# （`count=3` ⇒ 4 行、`count=320` ⇒ 321 行）。下面那「回满 320 行」的旧说法已按
# 实测改成 n+1；分页逻辑只依赖「最早那行的日期」，所以这个 off-by-one 不影响它。
TENCENT_PAGE_ROWS = 320
# 翻页上限：321 × 40 ≈ 12840 个交易日（约 50 年）。同东财那一条 —— 它是防跑飞的
# 闸门，不是数据口径；撞到它**报错**，不截断。
TENCENT_MAX_PAGES = 40

# 映射表用到的最大下标 + 1。**这是一个防「源换了布局」的守卫**：行是位置数组，
# 一旦源多/少一列，位置 8 会**静默变成别的字段**而不是报错 —— 探针实测到的两种
# 长度（不带复权 11 段 / 带复权 10 段）本身就说明长度是会变的，所以宽度要显式要求，
# 不能靠「反正测试时是对的」。由映射表算出，不手写数字（手写的那个会和表慢慢分家）。
_TENCENT_MIN_WIDTH = max(int(key) for key in TENCENT_DAILY_BAR) + 1

# tushare pro（`api.tushare.pro`）—— 本仓库第一个**要凭证**的源，本迭代接**两条**通道：
# 复权因子（`adj_factor`）与分红（`dividend`，2026-10-01 追加）。它是**表外源**（契约 §3.2
# 那张表上是三个子类，理由见 `TushareAdapter` 的类注释与 DC 契约附录 B21 / B22）。
# `ts_code` 的形状与标准符号**恰好相同**（`600000.SH`）⇒ 这一格是「改名字」，
# 不是「换口径」；也正因为它不是标准列名，A2 不会把它当恒等映射放行。
TUSHARE_API = 'https://api.tushare.pro'
TUSHARE_ADJ_FACTOR_API = 'adj_factor'
#: tushare 用 **body.code** 报告失败（HTTP 状态永远 200）—— 实测到的两个非 0 码
#: 都是 `40101`（空令牌 / 错令牌），见 DC 契约附录 B21.4（「失败形状」表）。
#: 所以只认这一个：
#: 其余非 0 码一律 `UNKNOWN` 并原样带上 code/msg，**不猜** —— 「积分不足 / 无权限」
#: 这类码本机观察不到（真令牌下两个接口都是 `code=0`），猜一个映射等于造规则。
TUSHARE_AUTH_CODE = '40101'
TUSHARE_ADJ_FACTOR = {
    'ts_code': 'symbol',
    'trade_date': 'trade_date',
    'adj_factor': 'adjust_factor',
}
# `dividend` 接口（2026-10-01 追加，附录 B22）。
#
# ⚠️ `ann_date` → `announce_date` 与 `cash_div_tax` → `cash_per_share` 两格是**口径**，
# 不是改名字：
#   · tushare 同时给 `cash_div`（**税后**）与 `cash_div_tax`（**税前**），契约 §2.3 定的
#     是**税前** ⇒ 取后者。拿错那一列不会报错，只会让每股派息整体偏小。
#   · `ex_date` / `ann_date` 都不是标准列名 ⇒ A2 不会把它们当恒等映射放行。
#
# ⚠️ **过滤键**：本接口的参数是**按日**的（`ex_date` / `ann_date` / `end_date` 各自是
# 单个日期），**表达不了区间** ⇒ `start` / `end` 只能落在**本地**（`normalize_dividend`
# 的区间筛选）。这与 `fetch_financial` 对 `period_end` 的态度是同一条理由（附录 B12：
# 请求侧过滤只当省流量用，规范化之后**仍然筛一遍**）—— 区别是这里连「请求侧那份」都
# 不存在，所以本地这一遍是**唯一**的过滤点，不是第二道保险。
TUSHARE_DIVIDEND_API = 'dividend'
TUSHARE_DIVIDEND = {
    'ts_code': 'symbol',
    'ex_date': 'ex_date',
    'ann_date': 'announce_date',
    'cash_div_tax': 'cash_per_share',
}

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
    amount_in_wan: bool = False,
    symbol: Optional[str] = None,
) -> pd.DataFrame:
    """源日线帧 → 标准日线帧（只含 `DAILY_BAR_COLUMNS`，顺序固定）。

    参数:
        raw: 源返回的原始帧。
        column_map: 源列名 → 标准列名的映射（模块级常量，见「源字段名不得泄漏」）。
        volume_in_lots: 源的成交量单位是否为「手」（是则 ×100）。
        amount_in_wan: 源的成交额单位是否为「万元」（是则 ×10000）。**刻意是独立开关**，
            不并入 `volume_in_lots`：两者是**两个源**、两个理由、两个换算系数，
            合并成一个看似更省的参数之后，「换错一个」与「两个都没换」再也分不出来。
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
    if amount_in_wan:
        # 契约 §2.3 / `dc_daily_bar.amount` 注释：单位是元；源给万元 ⇒ ×10000。
        # 单位是实测的（DC 契约附录 B16），不是按「看起来像」定的。
        frame['amount'] = frame['amount'] * 10000
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


def normalize_adjust_factor(
    raw: pd.DataFrame,
    column_map: Mapping[str, str],
    *,
    symbol: Optional[str] = None,
) -> pd.DataFrame:
    """源复权因子帧 → 标准帧（只含 `ADJUST_FACTOR_COLUMNS`，顺序固定）。

    参数:
        raw: 源返回的原始帧（tushare 是 `data.fields` + `data.items` 拼出来的）。
        column_map: 源列名 → 标准列名的映射。
        symbol: 源不返回 symbol 列（或返回的列名无法识别）时用请求参数回填的标的代码。

    **刻意没有单位/口径开关**（对比 `normalize_daily_bar` 的 `volume_in_lots` /
    `amount_in_wan`，`normalize_index_members` 的 `weight_is_percent`）：那三个开关的
    存在理由是「同一个量在两个源上有两种口径」；而复权因子只有一个口径（契约 §2.3
    的累乘因子）。多一个开关就多一种「两边都以为自己是对的」的可能。

    异常:
        SourceAdapterError: 缺少必需的源列、因子不是数值、`trade_date` 解析不了或为空、
            或 `symbol` 既不在帧里也没作为参数给出。
    """
    context = '复权因子归一化'
    source_required = [src for src, std in column_map.items()
                       if std in ADJUST_FACTOR_COLUMNS]
    frame = _select(raw, column_map, ADJUST_FACTOR_COLUMNS, source_required, context)
    if frame.shape[0] == 0:
        return _empty(*ADJUST_FACTOR_COLUMNS)
    if 'symbol' not in frame.columns:
        if symbol is None:
            raise SourceAdapterError('%s：帧里没有 symbol 列，也没有传 symbol= 回填参数'
                                     % context)
        frame['symbol'] = symbol
    frame['symbol'] = _symbols(frame['symbol'], context)
    frame['trade_date'] = _to_dates(frame['trade_date'], 'trade_date', context)
    _require_present(frame['trade_date'], 'trade_date', context)
    frame['adjust_factor'] = _to_numbers(frame['adjust_factor'], 'adjust_factor', context)
    # 不做 > 0 的检查：那是**值域判断**，属 `validate_frame` / DDL 的
    # `ck_dc_factor_positive`，不是机械换算。归一化这一层只保证「类型对、列对」。
    return frame.loc[:, list(ADJUST_FACTOR_COLUMNS)]


def normalize_dividend(
    raw: pd.DataFrame,
    column_map: Mapping[str, str],
    *,
    symbol: Optional[str] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> pd.DataFrame:
    """源分红帧 → 标准帧（只含 `DIVIDEND_COLUMNS`，顺序固定）。2026-10-01 追加（附录 B22）。

    参数:
        raw: 源返回的原始帧。
        column_map: 源列名 → 标准列名的映射。
        symbol: 源不返回 symbol 列时用请求参数回填的标的代码。
        start / end: 按 **`ex_date`** 的区间（契约 §3.2）。两端都不给 ⇒ **不筛**。

    与 `normalize_adjust_factor` 的关系：同样是「选列 → 转类型」，同样**不做值域判断**
    （`cash_per_share >= 0` 与 `announce_date <= ex_date` 归 `validate_frame` 与 DDL）。
    多出来的只有两件，且都不是口径换算：

    * **区间筛选**（`_window_by_ex_date`）：本通道的源接口都是「按单个日期查」，
      表达不了区间 ⇒ 这一遍是**唯一**的过滤点（见 `TUSHARE_DIVIDEND` 的注释）。
    * **三个 NOT NULL 列的显式失败**：`ex_date` / `announce_date` / `cash_per_share`
      在 DDL 里都是 `NOT NULL`。其中 `ex_date` 为空的行由区间筛选天然排除（没有事件日
      的分红不属于任何 `ex_date` 区间）；**另两列空了就抛** —— 用 `ex_date` 冒充
      `announce_date`会让未来信息漏进 PIT（D4），而把空的派息当成 `0` 会把一个数据空洞
      变成「这只票当天没派钱」，两者都是安静错。

    异常:
        SourceAdapterError: 缺少必需的源列、日期/数值解析不了、或三个 NOT NULL 列
            里除 `ex_date` 外有空值。
    """
    context = '分红归一化'
    source_required = [src for src, std in column_map.items() if std in DIVIDEND_COLUMNS]
    frame = _select(raw, column_map, DIVIDEND_COLUMNS, source_required, context)
    if frame.shape[0] == 0:
        return _empty(*DIVIDEND_COLUMNS)
    if 'symbol' not in frame.columns:
        if symbol is None:
            raise SourceAdapterError('%s：帧里没有 symbol 列，也没有传 symbol= 回填参数'
                                     % context)
        frame['symbol'] = symbol
    frame['symbol'] = _symbols(frame['symbol'], context)
    frame['ex_date'] = _to_dates(frame['ex_date'], 'ex_date', context)
    frame['announce_date'] = _to_dates(frame['announce_date'], 'announce_date', context)
    frame = _window_by_ex_date(frame, start, end)
    if frame.shape[0] == 0:
        return _empty(*DIVIDEND_COLUMNS)
    _require_present(frame['ex_date'], 'ex_date', context)
    _require_present(frame['announce_date'], 'announce_date', context)
    frame['cash_per_share'] = _to_numbers(frame['cash_per_share'], 'cash_per_share', context)
    _require_present(frame['cash_per_share'], 'cash_per_share', context)
    return frame.loc[:, list(DIVIDEND_COLUMNS)].reset_index(drop=True)


def _window_by_ex_date(frame: pd.DataFrame, start: Optional[date],
                       end: Optional[date]) -> pd.DataFrame:
    """把分红帧收到 `[start, end]`（按 **`ex_date`**，契约 §3.2 的过滤键）。

    * 两端都不给 ⇒ **原样返回**（不筛）：调用方没要区间，就不许在这里替它发明一个
      「默认最近一年」—— 那会让「我没给区间」与「这段真的没有分红」长得一样。
    * `ex_date` 为空的行**必然落在区间之外**，所以天然被筛掉：源会把**还没实施**的
      预案也带回来，而那些行没有除权日。⚠️ 这不是「顺手清脏数据」，是同一件事的另一面：
      一条没有事件日的分红不属于任何 `ex_date` 区间。
    * 只带一端也行（只要 `start` 或只要 `end`）—— 另一端为 `None` 就那一侧不设限。
    """
    if start is None and end is None:
        return frame
    mask = []
    for value in frame['ex_date']:
        if value is None:
            mask.append(False)
        elif start is not None and value < start:
            mask.append(False)
        elif end is not None and value > end:
            mask.append(False)
        else:
            mask.append(True)
    return frame.loc[pd.Series(mask, index=frame.index), :].reset_index(drop=True)


# ── 五个 normalize_* 共用的机械步骤 ────────────────────────────────────────────
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

    认**五张** schema（日线 / 财务 / 指数成分 / **复权因子** / **分红**），每一张的判据都写在
    `_CHECK_SPECS` 里 —— 判据是**数据**，所以「哪些规则真的被检查过」可以被打印出来、
    被触发测试逐一打中。复权因子这一张是 2026-09-29 晩 Ⅳ 补上的（DC 契约附录 B21.6）：
    在那之前它走一支显式拒绝的专支（报「判据尚未实现」），**从没被任何判据判过**。
    分红这一张（2026-10-01）**落地时就带判据**，不是又一个「登记了但没有判据」的 schema。

    **空帧不是通过**：0 行意味着没有可校验的东西，`is_valid=False` 并带一条硬错误。
    契约 §2.4 总原则写明「所有『找不到数据』的分支都必须显式失败，不允许返回空集」，
    而「提取为空却打印 PASS」正是本项目反复踩过的那类假绿。

    **覆盖率也是判据**（2026-10-02 收口 DC 契约附录 J 的 J-12）：每一列都必须在
    `_COVERAGE_SPECS` 里有一份**显式的空值决定**，而且「`NOT NULL` 且没有别的判据判它」
    的那些列（各 schema 的键列与日期列）**一格为空就 `is_valid=False`**。
    在这之前只有一条「缺失率 > 5%」的 warning，于是**判据说「通过」而库的 `NOT NULL`
    说「不」** —— 同一条规则在适配器与数据库两个执行点上口径不同，而漏判的代价是白跑
    一趟落库。⚠️ 那条 5% 的 warning **保留**：契约 §3.9 把「缺失率偏高」定义为
    「**不阻断入库**的告警」，覆盖率成为判据**不是**把这条告警升级成 error，两者判的
    是两件事（一条说「这一列有一点空」，一条说「这一列根本不许空」）。

    本函数**不因缺失值抛异常**：它产出报告。`is_valid=False` 的含义是「`DataCenter`
    不得写入」，调用方按此决定是重试、降级备源，还是把这批数据标成 `PARTIAL`。
    「缺失值」是**明写的**范围而不是随口一说：`_COVERAGE_SPECS` 整套设计都以
    「一格为空」为一等输入，所以**每一条**判据都必须对 NaN / `None` 有明确答复
    （「算违规」或「跳过」），不许在比较处崩掉。
    （2026-10-02 补：`daily-ohlc-order` 原本四列直接比大小，一个 `None` 会让
    `TypeError` 逃出本函数 —— 是覆盖率那批用例逐列置空时咬出来的。
    ⚠️ 值的**类型**不对（例如整列是字符串）仍会在比较处抛 `TypeError`：那是另一件事
    ——「这帧根本没归一化」应该由上游的 `normalize_*` 负责，本轮不动，也**不假装**
    本函数兜住了它。）
    判据表与 schema 脱节（覆盖率表漏了一列、登记了一个不存在的标签）时也**不崩**，
    而是把「判据缺失」当成一条硬错误 —— 唯一的例外是 `_violation` 遇到未知标签时抛的
    `AssertionError`，那是「判据表与实现脱节」的另一种形态（实现的 if 链少了一支）。
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
        errors.append('列集合不匹配任何标准 schema（日线 %s / 财务 %s / 成分股 %s / '
                      '复权因子 %s / 分红 %s）：实际 %s'
                      % (list(DAILY_BAR_COLUMNS), list(FINANCIAL_REQUIRED_COLUMNS),
                         list(INDEX_MEMBER_COLUMNS), list(ADJUST_FACTOR_COLUMNS),
                         list(DIVIDEND_COLUMNS),
                         [str(name) for name in frame.columns]))
        return ValidationReport(is_valid=False, row_count=frame.shape[0], errors=errors,
                                warnings=warnings, missing_ratio=missing_ratio)

    for column in required:
        if column not in frame.columns:
            errors.append('%s schema 缺少必需列 %s' % (kind, column))
    for message in _coverage_spec_errors(kind):
        errors.append(message)
    for message in _coverage_violations(frame, kind):
        errors.append(message)
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


#: 复权因子的**帧级自然键**。注意它**不等于** DDL 主键：`db/data_center.sql` 里是
#: `pk_dc_adjust_factor PRIMARY KEY (symbol, trade_date, data_version)`，多出来的
#: `data_version` 由**写入侧**盖戳，源帧里根本没有这一列 ⇒ 帧里能判的只有前两列。
#: 这与 `_PRIMARY_KEYS['daily']` 对 `pk_dc_daily_bar` 的处理是同一个约定。
#:
#: 2026-09-29 晩 Ⅳ 之前它**只被登记、不参与任何判断**（那时复权帧走的是
#: 「判据尚未实现」那一支）；现在它是 `_PRIMARY_KEYS['factor']` 的取值，同一
#: `(symbol, trade_date)` 的重复行真的会被报出来。
_ADJUST_FACTOR_PRIMARY_KEY = ('symbol', 'trade_date')

#: 分红的**帧级自然键**。同一种形状：`db/data_center.sql` 里是
#: `pk_dc_dividend PRIMARY KEY (symbol, ex_date, data_version)`，多出来的
#: `data_version` 由**写入侧**盖戳 ⇒ 帧里能判的只有前两列。
#: ⚠️ 这里用的是 `ex_date`（**事件日**）而不是 `announce_date`（可见性依据）：
#: 自然键回答的是「哪一行是同一件事」，同一事件重复公告两次不会变成两条分红。
_DIVIDEND_PRIMARY_KEY = ('symbol', 'ex_date')

#: 各 schema 的**帧级自然键** = DDL 主键去掉写入侧盖戳的那一列（见上面那条注释）。
#: 这张表同时是「`validate_frame` **真的判过**哪几类帧」的登记处 —— 每加一项，
#: `_CHECK_SPECS` 与 `_match_schema` 必须同步加，否则新 schema 会以「无判据」
#: 的样子通过（这正是复权因子在 B21.6 里挂了两天的原因）。
_PRIMARY_KEYS = {
    'daily': ('symbol', 'trade_date'),
    'financial': ('symbol', 'report_type', 'period_end'),
    'index': ('index_code', 'symbol', 'effective_from'),
    'factor': _ADJUST_FACTOR_PRIMARY_KEY,
    'dividend': _DIVIDEND_PRIMARY_KEY,
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
    'factor': (
        ('factor-positive', 'adjust_factor 必须 > 0（`ck_dc_factor_positive`）'),
    ),
    'dividend': (
        ('dividend-cash-nonneg',
         'cash_per_share 必须 >= 0（`ck_dc_dividend_cash_nonneg`）'),
        ('dividend-announce-not-after-ex',
         'announce_date 必须 <= ex_date（`ck_dc_dividend_announce_not_after_ex`）'),
    ),
}

#: 每个 schema 的**完整列集合**（= 该类帧归一化之后的列形状）。它与 `_COVERAGE_SPECS`
#: **双向**核对，所以「覆盖率判据漏了一列」会当场炸出来。
#:
#: ⚠️ 它**不是** `_match_schema` 那张识别表的副本，两者的差是**故意**的：识别表用的是
#: 「认出这类帧所需的最少列」，对 financial 只列 4 个必需列；这里是 9 列。契约 §3.2 对
#: 「各财务科目」只给了子集判据（源多给一个科目是好事，不该判红）。
_SCHEMA_COLUMNS = {
    'daily': DAILY_BAR_COLUMNS,
    'financial': FINANCIAL_COLUMNS,
    'index': INDEX_MEMBER_COLUMNS,
    'factor': ADJUST_FACTOR_COLUMNS,
    'dividend': DIVIDEND_COLUMNS,
}

#: **覆盖率判据**（2026-10-02 收口 DC 契约附录 J 的 **J-12**：「覆盖率还不是判据」）。
#:
#: 表项 = `(列, 允许缺失率上限, 判据标签或 None, 空为什么合法 / 谁判它的空)`。
#: 上限**只有两个取值**，所以这张表里没有一处手写的自由裁量：
#:
#: * `0.0` —— 这一列在 DDL 里是 `NOT NULL`，而 `_CHECK_SPECS` 里**没有任何一条**能判它的
#:   空值（日期列与键列的空值会被每条判据 `continue` 掉，见 `_violation` 里那两处
#:   「NULL 不在这里冒充区间违规」）⇒ **一格为空即 `is_valid=False`**。
#:   这就是「覆盖率成为判据」：在此之前，一个 `trade_date` 整列是 NaN 的帧会带着
#:   `is_valid=True` 走向 `INSERT`，再被库的 `NOT NULL` 拒掉 —— 判据说「通过」而库说
#:   「不」，两个执行点口径不同，而契约 §3.9 把 `is_valid=False` 定义为「`DataCenter`
#:   不得写入」⇒ 判据漏判的代价是白跑一趟落库。
#: * `1.0` —— 空不是缺失，且有**两种**，理由那栏必须写清是哪一种：
#:   ① 判据标签非 None：空值已经由那条判据判成硬错误（`daily-price-positive` 等把 NaN
#:      算违规，注释见 `_violation`），覆盖率只登记「谁判这一列」，**不重复判** ——
#:      两条并列判据判同一件事只会让同一处缺陷报两次。
#:   ② 标签为 None：空是**语义**（DDL 注释：`effective_to IS NULL` = 至今仍在成分内）
#:      或契约明写允许稀疏（契约 §2.3：源缺科目留 NaN）。
#:
#: 四条形状由 `_coverage_spec_errors()` 自动核对（列集合双向相等 / 上限只能是 0.0 或 1.0 /
#: 标签非 None ⇒ 上限必须是 1.0 且必须是该 schema 真判过的标签 / 上限 1.0 且无标签 ⇒
#: 理由不得为空）。**第 ⑤ 条**——这张表的 NULL 性与 `db/data_center.sql` 的 `NOT NULL`
#: 必须**双向一致**——由用例盯着（`tests/test_data_center_adapter.py` 的
#: `test_coverage_specs_match_the_ddl_nullability`）：谁往 DDL 加一列 NOT NULL 而没在这里
#: 决定它的空值，那条用例就红。这张表的**取值**由用例与变异盯着（与 `_PRIMARY_KEYS` /
#: `_CHECK_SPECS` 同一约定，见 DC 契约 B21.6 末段）。
_COVERAGE_SPECS = {
    'daily': (
        ('symbol', 0.0, None, 'DDL NOT NULL 的键列，没有别的判据判它的空值'),
        ('trade_date', 0.0, None, 'DDL NOT NULL 的日期列，没有别的判据判它的空值'),
        ('open', 1.0, 'daily-price-positive', '空值已由 daily-price-positive 判成硬错误'),
        ('high', 1.0, 'daily-price-positive', '空值已由 daily-price-positive 判成硬错误'),
        ('low', 1.0, 'daily-price-positive', '空值已由 daily-price-positive 判成硬错误'),
        ('close', 1.0, 'daily-price-positive', '空值已由 daily-price-positive 判成硬错误'),
        ('volume', 1.0, 'daily-nonneg', '空值已由 daily-nonneg 判成硬错误'),
        ('amount', 1.0, 'daily-nonneg', '空值已由 daily-nonneg 判成硬错误'),
    ),
    'financial': (
        ('symbol', 0.0, None, 'DDL NOT NULL 的键列，没有别的判据判它的空值'),
        ('report_type', 1.0, 'financial-report-type',
         '空值不是 REPORT_TYPES 的成员 ⇒ 已由 financial-report-type 判成硬错误'),
        ('period_end', 0.0, None, 'DDL NOT NULL 的日期列，没有别的判据判它的空值'),
        ('announce_date', 0.0, None,
         'DDL NOT NULL 的可见性依据列（D4），financial-announce-after-period 会 continue 掉它'),
        ('revenue', 1.0, None, 'DDL 允许 NULL；契约 §2.3：源缺科目留 NaN，空是允许的'),
        ('net_profit', 1.0, None, 'DDL 允许 NULL；契约 §2.3：源缺科目留 NaN，空是允许的'),
        ('total_assets', 1.0, None, 'DDL 允许 NULL；契约 §2.3：源缺科目留 NaN，空是允许的'),
        ('total_equity', 1.0, None, 'DDL 允许 NULL；契约 §2.3：源缺科目留 NaN，空是允许的'),
        ('roe', 1.0, None,
         'DDL 允许 NULL；financial-roe-range 明写 NaN 就 continue（与 factor 那一列的写法相反，'
         '差别是契约的差别）'),
    ),
    'index': (
        ('index_code', 0.0, None, 'DDL NOT NULL 的键列，没有别的判据判它的空值'),
        ('symbol', 0.0, None, 'DDL NOT NULL 的键列，没有别的判据判它的空值'),
        ('effective_from', 0.0, None,
         'DDL NOT NULL；index-effective-range 只判区间方向，不判起点是否为空'),
        ('effective_to', 1.0, None, 'DDL 允许 NULL 且 NULL 有语义：effective_to IS NULL = 至今仍在成分内'),
        ('weight', 1.0, None, 'DDL 允许 NULL；index-weight-range 明写 NaN 就 continue'),
    ),
    'factor': (
        ('symbol', 0.0, None, 'DDL NOT NULL 的键列，没有别的判据判它的空值'),
        ('trade_date', 0.0, None, 'DDL NOT NULL 的日期列，没有别的判据判它的空值'),
        ('adjust_factor', 1.0, 'factor-positive', '空值已由 factor-positive 判成硬错误'),
    ),
    'dividend': (
        ('symbol', 0.0, None, 'DDL NOT NULL 的键列，没有别的判据判它的空值'),
        ('ex_date', 0.0, None,
         'DDL NOT NULL 的事件日列，没有别的判据判它的空值（自然键去重对 NULL 也不生效）'),
        ('announce_date', 0.0, None,
         'DDL NOT NULL 的可见性依据列（D4），dividend-announce-not-after-ex 会 continue 掉它'),
        ('cash_per_share', 1.0, 'dividend-cash-nonneg', '空值已由 dividend-cash-nonneg 判成硬错误'),
    ),
}


def _match_schema(columns: Any):
    """按列集合认出这是哪一类帧。返回 (kind, 必需列, 判据表)。

    顺序有意如此：日线的 8 列最具体，先判它不会被财务帧误命中。复权因子放最后 ——
    它的 3 列里有 `adjust_factor`，与前三类的必需列**互不包含**，所以位置不改变结论。
    分红接在复权因子之后（2026-10-01）：它的 4 列里既无 `report_type` 也无
    `index_code`、也**不要求** `adjust_factor` ⇒ 与前四类**互不包含**，位置同样不改变结论。
    「互不包含」这件事是判据，不是感觉：`tools/verify_data_center_adapter.py` 的 A8/A9
    把三张表（契约列名 / `*_COLUMNS` 常量 / `_PRIMARY_KEYS`）对起来查。
    """
    have = set(map(str, columns))
    for kind, required in (('daily', DAILY_BAR_COLUMNS),
                           ('financial', FINANCIAL_REQUIRED_COLUMNS),
                           ('index', INDEX_MEMBER_COLUMNS),
                           ('factor', ADJUST_FACTOR_COLUMNS),
                           ('dividend', DIVIDEND_COLUMNS)):
        if set(required) <= have:
            return kind, required, _CHECK_SPECS[kind]
    return None, (), ()


def _coverage_spec_errors(kind: str) -> List[str]:
    """核对 `_COVERAGE_SPECS[kind]` 的**形状**（不是数据），返回硬错误清单。

    这是「覆盖率判据自己有没有被漏掉」的那一层：`_SCHEMA_COLUMNS[kind]` 与表里的列集合
    **双向**比，谁往 schema 加一列而没在这里决定它，那张帧一律 `is_valid=False`，
    理由写明是「覆盖率判据缺失」而不是「数据脏」——**这正是复权因子在 B21.6 里挂了两天
    的形态**（一张 schema 以「无判据」的样子通过）。

    形状不对时**不崩**：`validate_frame` 的契约是「产出报告」，一个不肯出报告的校验器
    在下游看起来和崩掉一样，而当下正确的动作是「不得写入」。
    """
    specs = _COVERAGE_SPECS.get(kind, ())
    declared = [column for column, _, _, _ in specs]
    expected = list(_SCHEMA_COLUMNS[kind])
    errors: List[str] = []
    missing = [name for name in expected if name not in declared]
    extra = [name for name in declared if name not in expected]
    duplicated = sorted(set(name for name in declared if declared.count(name) > 1))
    if missing or extra or duplicated:
        errors.append(
            '覆盖率判据表与 %s schema 的列集合不一致（漏了 %s / 多了 %s / 重复 %s）：'
            '没被决定过的列不许以「看起来通过」的样子过检（DC 契约附录 J 的 J-12）'
            % (kind, missing, extra, duplicated))
    tags = set(tag for tag, _ in _CHECK_SPECS[kind])
    for column, limit, tag, why in specs:
        if limit not in (0.0, 1.0):
            errors.append('覆盖率上限 %r 不在 {0.0, 1.0} 里（%s.%s）：中间值没有出处，'
                          '写一个「看起来合理」的阈值就是又一处手写自由裁量'
                          % (limit, kind, column))
            continue
        if tag is not None:
            if limit != 1.0:
                errors.append('%s.%s 登记了判据 %r 却把上限压到 %r：两条判据的深度不一致，'
                              '深的那条会让浅的那条形同虚设' % (kind, column, tag, limit))
            if tag not in tags:
                errors.append('%s.%s 登记了判据 %r，但 %s schema 的 `_CHECK_SPECS` 里没有这个'
                              '标签：登记了一条不存在的判据，与「没有判据」在报告里长得一样'
                              % (kind, column, tag, kind))
        elif limit == 1.0 and not str(why).strip():
            errors.append('%s.%s 的上限是 1.0（空合法）却没写理由：'
                          '「空是语义」和「我放弃了」必须能被区分'
                          % (kind, column))
    return errors


def _coverage_violations(frame: pd.DataFrame, kind: str) -> List[str]:
    """覆盖率判据的执行点：按 `_COVERAGE_SPECS[kind]` 逐列核对实际缺失率。

    缺失率的分母是**帧的行数**。

    两条 `continue` 各自有理由，而且都**不会**把一处真缺陷静默掉：

    * `tag is not None`：这一列的空值已经由那条判据判成硬错误，这里再判一次只会让同一处
      缺陷报两次 —— 两条并列判据判同一件事，读报告的人会去找一处不存在的第二问题。
    * `column not in frame.columns`：列不在帧里 = 归一化没造出这一列（契约 §2.3 允许源缺
      科目）。这类列的上限**一律是 1.0**（`financial` 的五个科目），所以「跳过」与「按
      缺失率 1.0 算」**等价**；而 `0.0` 的那些列按定义是**必需列**（用例
      `test_every_zero_limit_coverage_column_is_a_required_column` 拿 `_match_schema`
      的返回值钉着这一点）⇒ 它们一定在帧里，「整列缺失」到不了这里。既然跳过与计算等价，
      就不写一个「算得出 1.0 却永远不触发」的分支 —— 那种分支在下一次读代码时会被当成
      有牙的守卫。
    """
    violations: List[str] = []
    for column, limit, tag, _ in _COVERAGE_SPECS.get(kind, ()):
        if tag is not None or column not in frame.columns:
            continue
        ratio = float(frame[column].isna().mean())
        if ratio > limit:
            violations.append(
                '列 %s 缺失率 %.1f%% 超过该列的覆盖率上限 %.1f%%：这一列在 DDL 里是 `NOT NULL`，'
                '而 `_CHECK_SPECS` 里没有一条判它的空值 ⇒ 空值落库会被库拒掉，'
                '必须在写库前就判失败（DC 契约附录 J 的 J-12）'
                % (column, ratio * 100.0, limit * 100.0))
    return violations


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
                'trade_date', 'period_end', 'announce_date', 'ex_date',
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
            # 四列里任一为空 ⇒ 这一行答不了「顺序对不对」：空值归 `daily-price-positive`
            #（那四列 DDL 全是 NOT NULL，那条判据把 NaN 也算违规）。一条判据只答一个问题。
            # （2026-10-02 补：在此之前这里直接比大小，「一格空」在没有守卫的写法下
            # 会以两种方式坏掉 —— 浮点 NaN 时 `nan >= nan` 为假 ⇒ 报一条**假的**
            # 「顺序违规」；`object` 列里的 `None` 时直接 `TypeError` 逃出
            # `validate_frame`。两种都是「空值被当成了值」。）
            if any(pd.isna(row[name]) for name in ('open', 'high', 'low', 'close')):
                continue
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
                continue    # 空值不在这里冒充「区间违规」：它归覆盖率判据（`_COVERAGE_SPECS`
                            # 里这两列的上限是 0.0）—— 一条判据只答一个问题
            if announce < period:
                return ('第 %d 行 announce_date=%s < period_end=%s'
                        % (position, announce, period))
        return None
    if tag == 'financial-roe-range':
        # ⚠️ 这一列**可能整列不在帧里**：帧级 schema 只要求 4 个必需列（`_match_schema` 的
        # financial 那一项 = `FINANCIAL_REQUIRED_COLUMNS`），而契约 §2.3 允许源缺科目
        # ⇒「整列不在」与「整列 NaN」同义，两者都归 `continue`。
        # （2026-10-02 补：在此之前这里直接取 `frame['roe']`，一个只给必需列的 financial 帧
        # 会让 `KeyError` 逃出 `validate_frame`，与本函数「不因缺失值抛异常」的契约相悖。
        # 五张 schema 里只有这一列会缺：别处的列都是各自 schema 的必需列。）
        if 'roe' not in frame.columns:
            return None
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
    if tag == 'factor-positive':
        # 与 `daily-price-positive` / `daily-nonneg` 同一种写法：**NaN 也算违规**，不是
        # 「跳过」—— `dc_adjust_factor.adjust_factor` 是 NOT NULL，帧里的 NaN 落库就是
        # NULL，一样会被库拒掉。对照组：`financial-roe-range` / `index-weight-range`
        # 写的是「NaN 就 continue」，因为那两列 DDL 允许 NULL。
        # 两处写法的差别是契约的差别，不是笔误；抄判据时别把它们抄成一种。
        for position, value in enumerate(frame['adjust_factor']):
            if pd.isna(value) or value <= 0:
                return '第 %d 行 adjust_factor=%r' % (position, value)
        return None
    if tag == 'dividend-cash-nonneg':
        # 与 `daily-price-positive` / `factor-positive` 同一种写法：**NaN 也算违规**。
        # `dc_dividend.cash_per_share` 是 NOT NULL，帧里的 NaN 落库就是 NULL，一样会被库拒。
        # ⚠️ 但 `0` **合法**（只送转不派现的预案）—— 所以这里是 `<= 0` 而不是 `< 0` 的**否定**：
        # 判据是 `value < 0` 才违规，写成「`value <= 0` 算违规」会把契约 §2.3 明写合法的 `0` 误杀。
        for position, value in enumerate(frame['cash_per_share']):
            if pd.isna(value) or value < 0:
                return '第 %d 行 cash_per_share=%r' % (position, value)
        return None
    if tag == 'dividend-announce-not-after-ex':
        for position, (announce, ex_date) in enumerate(
                zip(frame['announce_date'], frame['ex_date'])):
            if announce is None or ex_date is None:
                continue    # 空值归覆盖率判据（`_COVERAGE_SPECS` 里这两列的上限是 0.0）：
                            # 这里只答「顺序对不对」
            if announce > ex_date:
                return ('第 %d 行 announce_date=%s > ex_date=%s'
                        % (position, announce, ex_date))
        return None
    raise AssertionError('未知的校验标签 %r：判据表与实现脱节，沉默的 None 会伪造出绿'
                         % tag)



# ── 源凭证：`.env`（本模块第一次遇到「要鉴权的源」）──────────────────────────
# 契约 §3.2 的既有源（腾讯 / 新浪 / 东财 / akshare / baostock）**全部免鉴权**，
# 所以「凭证从哪来」这件事在这里第一次要定规矩。三条：
#
# * 令牌**只从环境读**。绝不写进源码、绝不写进测试、绝不打印、绝不进 git。
# * 本机供值通道 = 项目根的 `.env`（`.gitignore` **忽略**它，入库的是模板
#   `.env.example`）。**没有这份文件是正常状态** —— CI 就是没有。
# * **真实环境变量优先于文件**。反过来会让 CI / 容器里注入的凭证被一份遗留的
#   本地文件悄悄盖掉，而症状只是「鉴权失败」—— 最难查的那一种。
#
# 解析器**只用标准库**，与 `tools/` 同口径：为二十行代码拖进 `python-dotenv`
# 违背「依赖由真正用它的模块带进来」—— 没有任何模块真的需要那个库。
ENV_FILE_NAME = '.env'

#: tushare 令牌的环境变量名。同一个名字出现在三处（`.env.example`、这里、
#: `tests/test_datasources_env.py`），改一处要三处一起改。
TUSHARE_TOKEN_ENV = 'TUSHARE_TOKEN'


def project_root() -> str:
    """仓库根目录（`quanauto/` 的上一级）。

    `.env` 在**项目根**而不是包目录里：包安装后可能在 `site-packages`，而凭证
    属于**这台机器上的这份工作树**，不属于包。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _credentials_error(message: str) -> SourceAdapterError:
    """凭证类失败的**唯一**构造点 —— 这样「报错里不许出现令牌值」只有一个地方要守。

    类别用 `SOURCE_AUTH`（闭集里的已有值，动作是「去配/换凭证」）：本模块的
    异常类规矩是「只抛 `SourceAdapterError`，别让 `ImportError` / `ValueError`
    逃上去冒充『上层代码写错了』」，一个坏掉的 `.env` 正属于「源这一侧用不了」。
    """
    return SourceAdapterError(message, source='env', kind='SOURCE_AUTH')


def read_env_file(path: Optional[str] = None) -> Dict[str, str]:
    """读一份 `.env`（每行 `KEY=VALUE`），返回 `{键: 值}`。

    **刻意做得很笨**：不认 `${VAR}` 展开、不认多行值、不认行尾注释。一个「聪明」的
    解析器会让人以为 `.env` 里有 shell 语义，而这里的值是要**原样**喂给 HTTP 的凭证
    —— 猜错的代价是发一个坏请求，而不是当场报错。

    规则（逐条都有用例）：

    * 空行、以及去掉前导空白后以 `#` 开头的行，跳过；
    * 行首可选的 `export ` 去掉（兼容 shell 习惯）；
    * 以**第一个** `=` 切分 —— 值里含 `=` 是合法的（base64 令牌很常见）；
    * 键与值两端空白去掉；
    * 值若被**成对**的 `'` 或 `"` 包住，去掉这对外引号；不配对的引号保持原样。

    文件不存在 ⇒ 返回 `{}`，**不抛**：没有本机 `.env` 是正常状态（CI 就没有），
    不是一个要上层处理的错误。「凭证到底有没有」由调用方判。

    **两处编码坑当红牌打出来**（本仓库在编码上吃过两次亏，这里不静默处理）：

    * UTF-8 BOM：按 `utf-8-sig` 读，BOM 不会混进键名；
    * **UTF-16**：PS 5.1 的 `>` 重定向写的是 UTF-16LE（不是 UTF-8）。它按 UTF-8 能
      解出来，只是一堆夹着 NUL 的「键」⇒ 查找落到「没有凭证」那条路，让人去怀疑
      令牌的值。所以这里显式检出 NUL 并拒绝，把「查错方向」这件事挡住。
    """
    path = path or os.path.join(project_root(), ENV_FILE_NAME)
    try:
        with open(path, 'r', encoding='utf-8-sig') as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise _credentials_error(
            '%s 不是 UTF-8（%s）。请把凭证文件另存为 UTF-8 —— '
            'PS 5.1 的 `>` 重定向写的不是 UTF-8。' % (path, type(exc).__name__)) from exc
    if '\x00' in raw:
        raise _credentials_error(
            '%s 内容里有 NUL 字节，多半是 **UTF-16** —— PS 5.1 的 `>` 重定向写的正是 '
            'UTF-16LE。按 UTF-8 硬读会得到一堆带 NUL 的键名，症状是「查不到凭证」，'
            '让人去错怪令牌的值。请改用编辑器另存为 UTF-8，'
            '或 `Copy-Item %s.example %s` 之后再改。'
            % (path, ENV_FILE_NAME, ENV_FILE_NAME))
    values: Dict[str, str] = {}
    for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        key, separator, value = line.partition('=')
        if not separator:
            # 没有 `=` 的行（比如误写成 shell 赋值）忽略 —— 不当成「键的值是空」
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key] = value
    return values


def tushare_token(environ: Optional[Mapping[str, str]] = None,
                  env_file: Optional[str] = None) -> str:
    """tushare 令牌：**真实环境变量优先**，其次项目根 `.env`；取不到就抛。

    不返回 `None`：一个「有时候是 None」的凭证会在第一次发请求时变成一个**没有
    令牌的请求** —— 那是**发到源上**的一次失败，比在这里当场报错贵得多，而且
    现场离原因很远。

    报错文本里**不含令牌值**（含的是它该去哪配）—— 报错会被贴进日志 / issue。
    """
    env = os.environ if environ is None else environ
    token = (env.get(TUSHARE_TOKEN_ENV) or '').strip()
    if not token:
        # 空串按「没有」处理：`.env.example` 原样复制过来就是 `TUSHARE_TOKEN=`。
        token = (read_env_file(env_file).get(TUSHARE_TOKEN_ENV) or '').strip()
    if not token:
        raise _credentials_error(
            'tushare 令牌为空（环境变量 %s 与项目根 %s 都没有）。把 %s 写进 %s：'
            '`Copy-Item %s.example %s` 后再填值（`.env` 已被 .gitignore 忽略，'
            '不会进仓库），或设为真实环境变量。'
            % (TUSHARE_TOKEN_ENV, ENV_FILE_NAME, TUSHARE_TOKEN_ENV, ENV_FILE_NAME,
               ENV_FILE_NAME, ENV_FILE_NAME))
    return token


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
    def fetch_adjust_factor(self, symbols: List[str], start: date,
                            end: date) -> pd.DataFrame:
        """拉取复权因子，列名必须是 `ADJUST_FACTOR_COLUMNS`（见附录 B21）。"""
        raise NotImplementedError

    @abstractmethod
    def fetch_dividend(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        """拉取分红（每股税前派息），列名必须是 `DIVIDEND_COLUMNS`（见附录 B22）。

        与 `fetch_adjust_factor` 同一种形状，但**区间键不同**：这里按 `ex_date`
        （除权除息日 = 事件日）过滤，不是按 `announce_date`；`announce_date`
        （公告日）单独一列，是 D4 的可见性依据 —— 两者不能互相代替。

        契约 §3.2（2026-10-01 追加）把签名与 4 个列名写全了；为什么它不是
        `_AdapterBase` 里又一条默认实现，见基类 `fetch_adjust_factor` 的注释。
        """
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


# ── 网络出口（全模块只有这两个函数碰网络：一个 GET、一个 POST）─────────────────
# 为什么是两个函数而不是一个带 `method=` 的：两条通道的**失败形状**不同 —— GET 那边
# 的鉴权失败是 HTTP 401/403（`classify_source_failure` 看得见状态码），而 tushare 的
# 鉴权失败是 **HTTP 200 + body.code != 0**（状态码永远 200）⇒ 判据只能写在解帧之后。
# 塞进同一个函数，会让「取数失败」在两处各有一套判据却写在一个地方。
def _http_get_json(url: str, params: Mapping[str, Any], *,
                   timeout: float = HTTP_TIMEOUT_SECONDS,
                   opener: Optional[Callable[..., Any]] = None) -> Any:
    """GET `url?params` 并解成 JSON。**全模块两个网络出口之一**（另一个是
    `_http_post_json` —— tushare 只吃 POST）。

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


def _http_post_json(url: str, payload: Mapping[str, Any], *,
                    timeout: float = HTTP_TIMEOUT_SECONDS,
                    opener: Optional[Callable[..., Any]] = None) -> Any:
    """POST 一份 JSON 并解成 JSON。**tushare 只吃 POST**（GET 不认这些参数）。

    与 `_http_get_json` 一样**不在这里 try/except**：异常原样上抛，交给唯一的翻译点
    `_AdapterBase._call` 去分类。不一样的是 `payload` 里**装着凭证**，所以这个函数
    （以及调用它的 `_default_fetch`）**绝不把 payload 打进任何消息、日志或异常**。

    参数:
    opener: 替换用的请求函数（签名同 `urllib.request.urlopen` 且被当作上下文管理器
        用）。测试传本机实现就能把「JSON 体 → Content-Type → 解帧 → 分类」整条链路
        验完，**不碰外网**。
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload)).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': HTTP_USER_AGENT},
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


def _tencent_param(symbol: str, anchor: date, count: int) -> str:
    """拼腾讯的 `param` —— 它是**逗号分隔的位置串**，不是键值对，且**必须是 6 段**。

    实测（2026-09-24，`_tencent-shape-probe.txt`）：

    * 5 段（把 `start` 省掉，`symbol,day,<end>,<count>,`）⇒ 带内拒：
      `{'code': 0, 'msg': 'param error'}`、`data=[]`。**这不是「少一段就少一点
      数据」，是根本取不到东西**，而冒烟就是在这里红的。
    * 6 段的 6 种写法全部被接受（完整区间 / `count=3` / 反区间 / 空 `start` /
      `start == end` / 换一个 `start`），且 `start` 段**实测被忽略** ——
      V1/V4/V5/V6 四种写法回的是**逐字节相同**的 321 行（同为 `2025-06-06`
      至 `2026-09-24`）。

    尾随那个空段是第 6 段的 `fq`：空 ⇒ 不复权（实测 close=6.57 = 新浪不复权值），
    `qfq` ⇒ 复权（同交易日 close=5.42）。要入库的是不复权原始价（契约 §2.3）。

    第 3 段写 `anchor` 而**不是**请求区间的 `start`：它反正被忽略，而万一源哪天开始
    认它，`start == end == anchor` 的语义（「以 anchor 结尾往前 count+1 行」）与
    翻页的含义一致；填窗口起点则会在翻页时变成**反向区间** —— 而反向区间实测被
    接受（V3）却回出 `max(start, end)` 之前的行，也就是窗口外的数据。左边界仍然
    完全由调用方按 `end` 回溯 + 事后裁剪保证。
    """
    return '%s,day,%s,%s,%d,' % (symbol, anchor.isoformat(), anchor.isoformat(), count)


def _tencent_rows(payload: Any, code: str) -> List[Any]:
    """解腾讯信封 → 行列表。信封形状是**实测**的，不是照文档写的。

    实测到的两种形状（附录 B16）：

    * 正常：`{'code': 0, 'msg': '', 'data': {'sh600000': {'day': [[...], ...]}}}`
    * 拒绝：`{'code': 1, 'msg': 'bad params', 'data': {'sh600000': {'version': '16'}}}`

    拒绝那个形状值得盯住：**`data` 照样在，外面看毫无异样**，进去才发现是版本号。
    所以「先看 `data` 在不在」这个判据在这里是错的 —— 必须一路走到 `day` 并检查
    它是不是列表，否则「参数写错」会伪装成「这段区间没有数据」。

    三个判定，理由与 `_eastmoney_page` 完全一致：

    * 路径上任何一层缺失或类型不对 ⇒ 源在带内拒绝了这次请求（HTTP 200 不代表
      这次查询合法），或返回了别的东西。重试不会让参数变合法 ⇒
      `SOURCE_SCHEMA_MISMATCH`，并把源自己的 `code`/`msg` 抄进消息里。
    * `day` 在、但为空 ⇒ 源正常回复、只是这段区间没有数据 ⇒ 空帧。**不抛**：
      `errors.py` 的分类表刻意没有「源没给数据」这一类。
    * 只给 `qfqday` 而不给 `day` ⇒ **抛**。那是**复权**价；契约 §2.3 要的是不复权
      原始价，静默把复权价写进不复权列，是日线级别的前视偏差，而且从库里看不出来。
    """
    if not isinstance(payload, Mapping):
        raise SourceAdapterError(
            '腾讯 %s 的响应不是 JSON 对象（收到 %s）'
            % (code, type(payload).__name__),
            source='tencent', kind='SOURCE_SCHEMA_MISMATCH')
    # 源自己说的话。带进消息里，比我们替它总结一句准。
    tag = 'code=%r msg=%r' % (payload.get('code'), payload.get('msg'))
    data = payload.get('data')
    if not isinstance(data, Mapping):
        raise SourceAdapterError(
            '腾讯 %s 的响应里 data 不是 JSON 对象（收到 %s，%s）—— 拒绝是带内的，'
            '动作是改请求参数，不是重试' % (code, type(data).__name__, tag),
            source='tencent', kind='SOURCE_SCHEMA_MISMATCH')
    node = data.get(code)
    if not isinstance(node, Mapping):
        raise SourceAdapterError(
            '腾讯 %s 的响应里 data.%s 不是 JSON 对象（收到 %s，%s）'
            % (code, code, type(node).__name__, tag),
            source='tencent', kind='SOURCE_SCHEMA_MISMATCH')
    if 'day' not in node and 'qfqday' in node:
        raise SourceAdapterError(
            '腾讯 %s 只给了复权行（qfqday），没有 day —— 契约 §2.3 要的是不复权原始价，'
            '复权价写进不复权列是前视偏差（%s）' % (code, tag),
            source='tencent', kind='SOURCE_SCHEMA_MISMATCH')
    rows = node.get('day')
    if rows is None:
        raise SourceAdapterError(
            '腾讯 %s 的响应里没有 day 键（%s）' % (code, tag),
            source='tencent', kind='SOURCE_SCHEMA_MISMATCH')
    if not isinstance(rows, list):
        raise SourceAdapterError(
            '腾讯 %s 的 day 不是数组（收到 %s，%s）'
            % (code, type(rows).__name__, tag),
            source='tencent', kind='SOURCE_SCHEMA_MISMATCH')
    return rows


class _AdapterBase(SourceAdapter):
    """各子类共用的管道。**不是契约类** —— 契约 §3.2 只画了基类与三个子类。

    放这里而不复制进每个子类的东西只有四样：`validate` 的委托、
    「源这一侧的任何异常都翻译成 `SourceAdapterError`」的包装、
    `fetch_adjust_factor` 的默认实现（= 这一源没有这条通道），以及
    `fetch_dividend` 的默认实现（同一种理由，2026-10-01 追加）。第二样尤其不该复制 ——
    漏掉一处，那一处就会开始向上层抛 `ModuleNotFoundError` / `KeyError` / `ValueError`，
    而那些类型在上层读起来是「调用方写错了」，会被当成 bug 去查错地方。

    ⚠️ 上面那句里的「四样」是**现在的**数目，不是契约的一部分：`fetch_dividend`
    落地时这行从「三样」改成「四样」。下一个只覆盖单个数据面的源落地时，它会再变 ——
    所以**别把这里的数字当判据用**，要看的是「默认实现都有哪几个」这件事本身。
    而这件事的可核对形式不在注释里，在**继承结构**里：谁的 `fetch_*` 直接继承
    `_AdapterBase` 那一版，谁就是「这个源没有这条通道」。

    **子类不止三个**：契约表上的三个之外还有 `TencentAdapter`（它为何存在见自己的
    类注释与附录 B16）与 `TushareAdapter`（附录 B21）。子类数量与契约那张表
    **不是一回事**，所以「照表实现了三个」这句话在本模块里不能当成兜底理由。
    """

    #: 子类覆盖。契约给 `source_name` 写的是抽象方法而不是类属性，所以这里保留
    #: 一个私有常量，由子类的 `source_name()` 返回 —— 形状对契约负责，取值只写一处。
    _SOURCE = ''

    def __init__(self, fetch: Optional[Callable[..., pd.DataFrame]] = None,
                 dividend_fetch: Optional[Callable[..., pd.DataFrame]] = None) -> None:
        """参数:
        fetch: 真实取数函数。**注入**而不是在方法体里写死网络调用，是这一层能被
            离线测试的唯一原因（测试注入假函数，永不联网）。传 None 时用模块里
            那个未经调用验证的默认实现。
        dividend_fetch: 分红通道的取数函数，与 `fetch` **分开**。
            为什么不复用同一个可调用对象：`fetch_daily_bar` / `fetch_adjust_factor` /
            `fetch_dividend` 在同一源上是**不同接口**（tushare 是 `daily`/`adj_factor`/
            `dividend` 三个 api_name），签名里的 `symbol=` 一样、返回的列完全不同 ⇒
            挤进同一个函数只能靠 `**kwargs` 里的开关分派，而那个开关就是「两条通道
            其实共用一套判据」的假象来源。
            不传时用 `_default_dividend_fetch`（基类那一版**显式抛 UNSUPPORTED**）。
        """
        self._fetch = fetch if fetch is not None else self._default_fetch
        self._fetch_dividend = (dividend_fetch if dividend_fetch is not None
                                else self._default_dividend_fetch)

    def source_name(self) -> str:
        return self._SOURCE

    def validate(self, frame: pd.DataFrame) -> ValidationReport:
        """委托给 `validate_frame`。三个子类的判据是**同一套**（标准 schema 与 DDL
        的 CHECK 是同一条规则），所以没有理由让它们各自实现一份。"""
        return validate_frame(frame)

    def fetch_adjust_factor(self, symbols: List[str], start: date,
                            end: date) -> pd.DataFrame:
        """默认 = **这一源没有复权因子这条通道**。

        为什么默认实现放在基类，而 `fetch_daily_bar` / `fetch_financial` /
        `fetch_index_members` 留在 `SourceAdapter` 里当抽象方法：那三条是「一个源适配器
        至少要能做的是什么」，而这条目前**只有 tushare 有**（附录 B21.1）。留成抽象
        方法，会让另外四个源各写一份一模一样的 `raise`，而重复的 `raise` 在下次有人
        加源时不会提醒他任何事。

        **必须显式抛，绝不返回空帧**：空帧会被下游读成「这段区间没有因子」（= 不复权），
        而真相是「这个源不给因子」—— 两件事在回测里的后果相反，而报告里长得一样。
        """
        raise SourceAdapterError(
            '%s 这条通道没有复权因子（本迭代只接了 tushare 的 adj_factor，见 DC 契约附录 B21）'
            % self._SOURCE,
            source=self._SOURCE, kind='UNSUPPORTED')

    def _default_fetch(self, **kwargs: Any) -> pd.DataFrame:
        """子类覆盖为真实的源调用（惰性导入）。"""
        raise SourceAdapterError(
            '%s 适配器没有注入 fetch 函数，也没有默认实现' % type(self).__name__,
            source=self._SOURCE, kind='UNSUPPORTED')

    def _call(self, _fetch: Optional[Callable[..., pd.DataFrame]] = None,
              **kwargs: Any) -> pd.DataFrame:
        """调用注入的取数函数，并把源侧异常统一翻译成 `SourceAdapterError`。

        参数:
        _fetch: 覆盖要调用的取数函数（分红通道传 `self._fetch_dividend`）。
            **刻意做成参数而不是第二个方法**：本模块只有**一处** try/except 在做
            异常翻译（`tools/verify_data_center_adapter.py` 的 A11 就盯这一处），
            拆成两个方法会让「两条通道的失败分类由同一个函数算」变成一句注释里的承诺，
            而哪天其中一份被改动，两份的措辞仍然都读得通。名字带下划线是为了让它
            不可能与源侧参数撞车（源侧 kwarg 都是列名/日期，不会以 `_` 开头）。
        **kwargs: 传给取数函数的参数（`symbol=` / `start=` / `end=` / `opener=` / `token=`）。

        `SourceAdapterError` 原样透传（它已经是这个类型的语义，而且带着适配器
        自己判定好的类别）；其余一律包一层，并在消息里保留原异常类型名 ——
        丢掉类型名会让「源库没装」和「源限流」在日志里长得一模一样。

        类别由 `classify_source_failure` 判定，`retryable` 由类别推出来，
        `source` 由适配器自己填。这三样加起来才写得成重试策略。
        """
        fetch = self._fetch if _fetch is None else _fetch
        try:
            return fetch(**kwargs)
        except SourceAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 这一层的职责就是兜住源侧的一切
            raise SourceAdapterError(
                '%s 取数失败（%s）：%s' % (self._SOURCE, type(exc).__name__, exc),
                source=self._SOURCE,
                kind=classify_source_failure(exc),
            ) from exc

    def _default_dividend_fetch(self, **kwargs: Any) -> pd.DataFrame:
        """基类这一版 = **没有分红通道**：直接复用 `fetch_dividend` 的那条拒绝。

        为什么不在这里再写一遍 `raise`：两条只差一句措辞的拒绝会在某次改动后分歧，
        而调用方看到哪一条取决于「注入点走对了没有」—— 那是两个不同的缺陷却给同一个症状。
        参数三个位都是占位（`fetch_dividend` 的默认实现不看参数就抛）。
        """
        return self.fetch_dividend([], date(1970, 1, 1), date(1970, 1, 1))

    def fetch_dividend(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        """默认 = **这一源没有分红数据这条通道**（2026-10-01 追加，附录 B22）。

        理由与 `fetch_adjust_factor` 的默认实现逐字相同：那三条抽象方法
        （日线 / 财务 / 成分股）是「一个源适配器至少要能做的是什么」，而复权因子与
        分红目前**只有 tushare 有**（B21.1 / B22.1）。留成抽象方法，会让另外四个源
        各写一份一模一样的 `raise`，而重复的 `raise` 在下次有人加源时不会提醒他任何事。

        **必须显式抛，绝不返回空帧**：空帧会被下游读成「这段区间没有分红事件」
        （= `get_dividend()` 返回 `0.0`），而真相是「这个源不给分红数据」——
        两件事在回测里的后果相反，而报告里长得一样。
        """
        raise SourceAdapterError(
            '%s 这条通道没有分红数据（本迭代只接了 tushare 的 dividend，见 DC 契约附录 B22）'
            % self._SOURCE,
            source=self._SOURCE, kind='UNSUPPORTED')


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


def _period_column(column_map: Mapping[str, str], report_name: str) -> str:
    """某张报表列名表里「哪个源列是报告期」—— 恰一个才返回，否则拒。

    为什么要函数而不是就地在 `fetch_financial` 里取一个就算：日期过滤的键名
    **每张报表不同**（利润表 `REPORTDATE`、资产负债表 `REPORT_DATE`，附录 B14 实测），
    而「列名表里映射到 `period_end` 的源列」是这层对应关系的唯一真源。多一个就是
    「筛哪一列」没有唯一答案，少一个就是筛不了 —— 两种都是**猜**的入口，所以这里
    直接拒，而不是随便挑一个。
    """
    keys = sorted(key for key, value in column_map.items() if value == 'period_end')
    if len(keys) != 1:
        raise SourceAdapterError(
            '东财 %s 的列名表里映射到 period_end 的源列有 %d 个（%s）：恰一个才能拿去'
            '当日期过滤的键名' % (report_name, len(keys), keys),
            source='eastmoney', kind='UNSUPPORTED')
    return keys[0]


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
            period_end: 报告期，拼进过滤表达式（源侧只回这一期，**只为省流量**）。
            date_key: 该报表**自己的**报告期列名，由调用方从列名表里取
                （`_period_column`）。本函数不猜键名：猜错会被源当场拒（附录 B14）。
            opener: 仅测试传（替换 `urllib.request.urlopen`）。生产路径不传。

        **请求侧的日期过滤只省流量，不做正确性依赖**：实测证据只有 1 标的 × 2 报表 ×
        1 天（附录 B14），而本地筛是能用本地帧测出来的那一种。所以 `fetch_financial`
        在归一化之后**仍然**筛一遍 —— 这里发出去的过滤哪天被源静默忽略，结果也不会多一行。
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

        period_end = kwargs.get('period_end')
        date_key = str(kwargs.get('date_key') or '')
        if not isinstance(period_end, date):
            # 和 symbol 同理：缺/错参数是**调用方**写错了，用 ValueError 表达，免得被
            # `_call` 翻译成 SourceAdapterError 之后读成「这个源不可用」。
            raise ValueError(
                'EastMoneyAdapter._default_fetch 需要 date 类型的 period_end 参数')
        if not date_key:
            raise ValueError(
                'EastMoneyAdapter._default_fetch 需要 date_key 参数（取该报表列名表里'
                ' period_end 的那一列，见 `_period_column`）')

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
                 # 带后缀的写法没有实测过，不猜。日期那一项是实测语法（附录 B14）：
                 # **单引号**，键名由调用方给（每张报表不同）。
                 # ⚠️ 日期这里**必须**是单引号：B14 实测的是 `(REPORTDATE='2024-12-31')`。
                 # 本行曾写成双引号 —— 单元测试没拦住（当时的期望值是照实现对齐的），
                 # 是「期望值照实测逐字写、不由实现算」这条纪律把它抓出来的。
                 'filter': '(SECURITY_CODE="%s")(%s=\'%s\')'
                           % (code, date_key, period_end.isoformat()),
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
        * **按报告期筛在归一化之后仍然做一遍**：请求里也带了日期过滤（附录 B14 实测的
          语法，少传数据），但那份证据只有 1 标的 × 2 报表 × 1 天，而且注入了自定义
          `fetch=` 时请求根本不存在 —— 本地这一遍才是**契约保证的那一遍**。
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
                raw = self._call(
                    symbol=symbol, report_name=report_name, period_end=period_end,
                    date_key=_period_column(column_map, report_name))
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


class TencentAdapter(_AdapterBase):
    """腾讯行情（`proxy.finance.qq.com`）—— **只覆盖日线**，`SourcePriority.FALLBACK`。

    **它不在契约 §3.2 那张表里**（表上是 AKShare / Baostock / 东财三个子类），这件事
    必须说清楚，不能靠「反正能跑」蒙过去。加它的理由是**实测**的（附录 B16）：

    * 东财的 kline 端点在本机两个 opener 下都拒连（`RemoteDisconnected` / `HTTP 502`），
      而 `AKShareAdapter` 走的正是它 ⇒ 主源**当前取不到数**。
    * 新浪日线可达，但只有 6 列、**没有成交额**，而 `dc_daily_bar.amount` 是 `NOT NULL`
      ⇒ 填不满契约。合成一个（`volume × close`）是造数据，不是取数。
    * 腾讯这条是实测里唯一天然给出**契约 8 列**、且价格是**不复权原始价**的一条。

    所以它是个**补充源**，不是第四个「必须实现」的子类：契约表不改，它的覆盖面也只有
    日线（财务报表与指数成分股在这里是 `UNSUPPORTED`），`priority` 复用 `FALLBACK` ——
    枚举只有 PRIMARY / FALLBACK 两值，而它显然不是主源。
    """

    _SOURCE = 'tencent'
    priority = SourcePriority.FALLBACK

    # 不带 `self` 的 staticmethod，**形状是约束不是风格**：另外三个源的 `_default_fetch`
    # 都是这样，且这个形状被 `tools/contract-signature-manifest.json` 的 `impl_params`
    # 逐字登记（`['**kwargs']`，没有 self）。取数函数是**注入**进 `_AdapterBase` 的
    # （`self._fetch = self._default_fetch`），它不需要实例；给它加一个 `self` 会被
    # S4 判成参数漂移 —— 而那条发现是对的：形状一改，注入路径上的所有人都得重看。
    # 于是 `source=` 在这里只能写字面量（`self` 不可用），与东财那份写法一致。
    @staticmethod
    def _default_fetch(**kwargs: Any) -> pd.DataFrame:
        """真实取数：**单标的**一段区间，从 `end` 往回翻页，再裁到 `[start, end]`。

        为什么要自己翻页 + 自己裁（都是实测的，附录 B16）：源只认「末端日期 + 往回取
        几个交易日」，区间左边界被忽略（`count=3` 时回的是 **4 行**：`end` 往前 3 天
        到 `end`；`start` 段换什么值都回同一批行）⇒ 「把 `start` 发过去就算划定了区间」
        是错的 —— `start` 早于 `end - 320` 时会**静默少数据**。

        撞到 `TENCENT_MAX_PAGES` 而左边界仍未覆盖 ⇒ **抛**，不截断：少取的交易日会变成
        回测里的静默缺口，而契约 §2.4 的总原则是宁可跑不起来。

        行宽按观测到的最大值补齐再建帧；**被映射的 0~5、8 号位在实测的两种长度里都在**，
        补出来的是尾部空位、不在映射表里，因此不会有一列凭空进到标准帧。
        """
        symbol = code_glued(kwargs['symbol'])
        start = pd.Timestamp(kwargs['start']).date()
        end = pd.Timestamp(kwargs['end']).date()
        if start > end:
            raise SourceAdapterError(
                '腾讯 %s 的区间是反的：start=%s > end=%s' % (symbol, start, end),
                source='tencent', kind='UNSUPPORTED')
        opener = kwargs.get('opener')

        rows: List[Any] = []
        seen = set()
        earliest = ''
        covered = False
        cursor = end
        for _page in range(TENCENT_MAX_PAGES):
            page_rows = _tencent_rows(_http_get_json(
                TENCENT_KLINE_API,
                {'param': _tencent_param(symbol, cursor, TENCENT_PAGE_ROWS)},
                opener=opener), symbol)
            fresh = []
            for row in page_rows:
                # 行宽守卫：位置数组一旦换了布局，位置 8 会变成别的字段而**不报错**。
                if not isinstance(row, list) or len(row) < _TENCENT_MIN_WIDTH:
                    raise SourceAdapterError(
                        '腾讯 %s 的 day 里有一行不是长度 ≥ %d 的数组（实测 11 段）：%r —— '
                        '位置映射表按位置写死，宽度变了就必须重测，不能猜'
                        % (symbol, _TENCENT_MIN_WIDTH, row),
                        source='tencent', kind='SOURCE_SCHEMA_MISMATCH')
                if row[0] not in seen:
                    seen.add(row[0])
                    fresh.append(row)
            if not fresh:
                # 源没在前进（或本页为空）。**不再翻**：再翻就是死循环。
                break
            rows.extend(fresh)
            earliest = min(str(row[0]) for row in fresh)
            if earliest <= start.isoformat():
                covered = True
                break
            cursor = date.fromisoformat(earliest) - timedelta(days=1)
        if rows and not covered:
            raise SourceAdapterError(
                '腾讯 %s 翻了 %d 页仍然没覆盖到 start=%s（最早只到 %s）：**不截断**，'
                '宁可报错 —— 少取的交易日会变成回测里的静默缺口'
                % (symbol, TENCENT_MAX_PAGES, start.isoformat(), earliest or None),
                source='tencent', kind='SOURCE_SCHEMA_MISMATCH')

        kept = [row for row in rows if start.isoformat() <= str(row[0]) <= end.isoformat()]
        if not kept:
            # 空帧也要带上映射表里的列：`normalize_daily_bar` 是**先选列、后看行数**，
            # 交一张没有这些列的帧过去，会在它那儿变成一个和「没数据」毫不相干的 KeyError。
            return pd.DataFrame(columns=sorted(TENCENT_DAILY_BAR))
        kept.sort(key=lambda row: str(row[0]))
        width = max(len(row) for row in kept)
        padded = [list(row) + [None] * (width - len(row)) for row in kept]
        return pd.DataFrame(padded, columns=[str(index) for index in range(width)])

    def fetch_daily_bar(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        """逐标的取数后拼接。接口给的是列表、源吃的是单标的，差值在这里抹平。"""
        frames = []
        for symbol in symbols:
            raw = self._call(symbol=symbol, start=start, end=end)
            frames.append(normalize_daily_bar(
                raw, TENCENT_DAILY_BAR,
                volume_in_lots=TENCENT_VOLUME_IN_LOTS,
                amount_in_wan=TENCENT_AMOUNT_IN_WAN,
                symbol=symbol))
        if not frames:
            return _empty(*DAILY_BAR_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    def fetch_financial(self, symbols: List[str], period_end: date) -> pd.DataFrame:
        raise SourceAdapterError(
            '腾讯这条通道只有日线（实测过的也只有日线；财务走东财，见契约 §3.2）',
            source=self._SOURCE, kind='UNSUPPORTED')

    def fetch_index_members(self, index_code: str, as_of_date: date) -> pd.DataFrame:
        raise SourceAdapterError(
            '腾讯这条通道只有日线（实测过的也只有日线；指数成分股走东财，见契约 §3.2）',
            source=self._SOURCE, kind='UNSUPPORTED')


def _tushare_rows(doc: Any) -> Tuple[List[str], List[Any]]:
    """tushare 信封 → `(fields, items)`。**成功与失败都是 HTTP 200**。

    这是「tushare 为什么不能直接套 `classify_source_failure`」的落地：那个分类器看的是
    `HTTPError.code`，而 tushare 的鉴权失败根本不产生 `HTTPError`。所以判据必须读 body。

    三种结果三种 `kind`，**不合并**：
      * `code == 0` ⇒ 正常，返回 `fields` / `items`；
      * `code == TUSHARE_AUTH_CODE` ⇒ `SOURCE_AUTH`（动作 = 换/补凭证）；
      * 其余非 0 码 ⇒ `UNKNOWN`（动作 = **先看 msg**，因为「积分不足 / 无权限 / 接口
        下线」这些码本机都没观察到，编一个映射比报 UNKNOWN 更坏）。
    """
    if not isinstance(doc, dict):
        raise SourceAdapterError(
            'tushare 返回的不是 JSON 对象，而是 %s' % type(doc).__name__,
            source='tushare', kind='SOURCE_SCHEMA_MISMATCH')
    code = doc.get('code')
    if code != 0:
        # `str(code)`：实测是数字，但不赌版本 —— 字符串形态要同样认得出来。
        if str(code) == TUSHARE_AUTH_CODE:
            raise SourceAdapterError(
                'tushare 拒绝了凭证（code=%s，msg=%s）—— 检查环境变量 %s 或项目根的 %s'
                % (code, doc.get('msg'), TUSHARE_TOKEN_ENV, ENV_FILE_NAME),
                source='tushare', kind='SOURCE_AUTH')
        raise SourceAdapterError(
            'tushare 返回 code=%s（msg=%s）—— 本仓库只认 0（成功）与 %s（凭证被拒），'
            '其余码不猜，先照着 msg 去查文档'
            % (code, doc.get('msg'), TUSHARE_AUTH_CODE),
            source='tushare', kind='UNKNOWN')
    data = doc.get('data')
    if not isinstance(data, dict):
        raise SourceAdapterError(
            'tushare code=0 但 data 不是对象（%s）—— 信封形状变了'
            % type(data).__name__,
            source='tushare', kind='SOURCE_SCHEMA_MISMATCH')
    if data.get('has_more'):
        # 宁可报错也不能只留第一页：静默截断会让「这段区间的因子少了一截」在库里
        # 长得像「这段时间就是没数据」。本迭代**不实现翻页**（tushare 的分页口径与
        # 腾讯的倒序翻页不同，没实测过就不写），所以这是个响亮的未收口项。
        raise SourceAdapterError(
            'tushare 报了 has_more=true，请求的区间一次拿不完，而本迭代没有实现翻页 —— '
            '拆小区间再取，不要拿截断的结果当全量（附录 B21.6）',
            source='tushare', kind='UNKNOWN')
    fields, items = data.get('fields'), data.get('items')
    if not isinstance(fields, list) or not isinstance(items, list):
        raise SourceAdapterError(
            'tushare 的 data.fields / data.items 不是列表（%s / %s）—— 信封形状变了'
            % (type(fields).__name__, type(items).__name__),
            source='tushare', kind='SOURCE_SCHEMA_MISMATCH')
    return [str(name) for name in fields], items


class TushareAdapter(_AdapterBase):
    """tushare pro（`api.tushare.pro`）—— 本仓库第一个**要凭证**的源。

    **它不在契约 §3.2 那张表里**（与 `TencentAdapter` 同一种情况：表上写的是「MVP 阶段
    必须实现的三个子类」，表外的源是接进来的事实，不能从表里读出来）。本迭代接**两条**
    通道：复权因子（`adj_factor`）与**分红**（`dividend`，2026-10-01 追加，附录 B22）。
    复权因子这条通道是 `dc_adjust_factor` 到目前为止**唯一的**采集路径（我看到的采集路径
    只有它；写入侧与读数侧在 **2026-09-29 晚**也接上了 ——
    因子从 `dc_adjust_factor` 读得出来、写入口是 `quanauto/pgstore.py` 的 `PgFactorIngestor`。
    订正（2026-09-29 晚 Ⅱ）：这里原写「但**复权价仍未实施**：`BarData` 的 OHLC 仍是不复权价」，
    **那句已作废** —— `DbDataFeed._row_to_bar` 现在会乘 `datacenter._price_scale()` 给的倍数
    （唯一一处算式在 `quanauto/datafeed.py`：`_rescale_price`，全仓库**两个**调用点 ——
    `DbDataFeed._row_to_bar` 与 `CsvDataFeed._load_data`，2026-09-29 晩 Ⅲ 补上第二条），`volume`/`amount` 刻意不乘。
    逐条见 DC 契约附录 B21.3 / A6 的订正块）。分红这条同理，是 `dc_dividend` 目前**唯一的**
    采集路径（写入侧 `PgDividendIngestor` 与读数侧 `DbDataFeed.get_dividend` 在 2026-10-01 同批接上）。

    ⚠️ **两条通道的区间键不同**：复权因子按 `trade_date`，分红按 `ex_date`（除权除息日）。
    这不是命名差异 —— 分红多一个 `announce_date`（公告日）承载 D4 的可见性，而因子帧里
    没有这一列（因子按交易日可见）。两者共用一个 `start`/`end` 签名是契约的统一入口形状，
    不意味着它们筛的是同一列。

    它是本模块**第一个只覆盖部分数据面的源**（最初只覆盖一个面，2026-10-01 起两个），
    所以其余的面必须在类里显式写出来（不能靠基类兜 —— 基类只给
    `fetch_adjust_factor` / `fetch_dividend` 两个默认实现，那三条仍是抽象方法，
    不写就**实例化不了**；见下面那三条前的注释）。

    凭证**不从签名传入**（契约 §3.2 的签名里没有凭证参数，也不打算加 —— 加上去每一处
    调用都得跟着改，而「换源/加源不改下游签名」正是这一层存在的理由）。它在取数那一刻
    由 `tushare_token()` 从环境变量 `TUSHARE_TOKEN` 或项目根的 `.env` 读；读不到就抛
    `SourceAdapterError(kind='SOURCE_AUTH')`，消息里**只有变量名**。

    为什么用 stdlib 而不是 tushare SDK（附录 B21.1）：这条 API 就是「POST 一份 JSON，
    读 `data.fields` + `data.items`」，SDK 是一层没有加信息的包装；而少一个运行时依赖
    就少一种「本机没装 ⇒ SOURCE_SDK_MISSING」的失败，同时这条适配器能与东财/腾讯一样
    被**离线**整条验完（本机没装 tushare，它也不会被装）。
    """

    _SOURCE = 'tushare'
    priority = SourcePriority.FALLBACK

    @staticmethod
    def _default_fetch(**kwargs: Any) -> pd.DataFrame:
        """真实取数：**单标的**一段区间（`api_name='adj_factor'`）。

        `symbols` 的展开在 `fetch_adjust_factor` 里做（与其它源同一分工）。
        `opener` 只给测试用（同 `_http_get_json`）；生产路径不传。
        """
        symbol = normalize_symbol(kwargs['symbol'])
        start = pd.Timestamp(kwargs['start']).date()
        end = pd.Timestamp(kwargs['end']).date()
        if start > end:
            raise SourceAdapterError(
                'tushare %s 的区间是反的：start=%s > end=%s' % (symbol, start, end),
                source='tushare', kind='UNSUPPORTED')
        fields, items = _tushare_rows(_http_post_json(
            TUSHARE_API,
            {'api_name': TUSHARE_ADJ_FACTOR_API,
             # 凭证只出现在这一行；它不进消息、不进日志。
             'token': kwargs.get('token') or tushare_token(),
             'params': {'ts_code': symbol,
                        # tushare 要 `YYYYMMDD` 的无分隔形式。
                        'start_date': start.strftime('%Y%m%d'),
                        'end_date': end.strftime('%Y%m%d')},
             'fields': ''},
            opener=kwargs.get('opener')))
        if not items:
            # 空区间：把**列名**带上。不带的话 `normalize_adjust_factor` 会先撞
            # 「源返回的不是 DataFrame」那条分支，报出一句与真因无关的话。
            return pd.DataFrame(columns=fields or sorted(TUSHARE_ADJ_FACTOR))
        return pd.DataFrame(items, columns=fields)

    def fetch_adjust_factor(self, symbols: List[str], start: date,
                            end: date) -> pd.DataFrame:
        """逐标的取数后拼接（与其它源同一形状）。"""
        frames = [normalize_adjust_factor(self._call(symbol=symbol, start=start, end=end),
                                          TUSHARE_ADJ_FACTOR, symbol=symbol)
                  for symbol in symbols]
        if not frames:
            return _empty(*ADJUST_FACTOR_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    @staticmethod
    def _default_dividend_fetch(**kwargs: Any) -> pd.DataFrame:
        """真实取数：**单标的**全部历史（`api_name='dividend'`）。

        2026-10-01 追加（附录 B22）。

        ⚠️ **这里刻意不传区间参数**：`dividend` 接口的日期参数是**按日**的
        （`ex_date` / `ann_date` / `end_date` 各是单个日期），**表达不了 `[start, end]`**。
        传一个它不识别的参数名（比如照抄 `adj_factor` 的 `start_date`）会得到两种结果：
        要么被忽略、要么报参数错 —— 而这两种在报告里长得一样，且都不是我们要的东西。
        ⇒ 区间筛选**全部**落在 `normalize_dividend` 的 `start=`/`end=` 上（那才是
        契约 §3.2 说的「按 `ex_date` 过滤」）。代价：`ts_code` 单独一个参数的返回
        可能很长（含未实施的预案），分页由 `has_more` 那套兜底（见 `_tushare_rows`）。

        `symbols` 的展开在 `fetch_dividend` 里做（与其它源同一分工）。
        `opener` 只给测试用；生产路径不传。
        """
        symbol = normalize_symbol(kwargs['symbol'])
        fields, items = _tushare_rows(_http_post_json(
            TUSHARE_API,
            {'api_name': TUSHARE_DIVIDEND_API,
             # 凭证只出现在这一行；它不进消息、不进日志。
             'token': kwargs.get('token') or tushare_token(),
             'params': {'ts_code': symbol},
             'fields': ''},
            opener=kwargs.get('opener')))
        if not items:
            # 空区间：把**列名**带上（同 `_default_fetch` 那条注释的理由）。
            return pd.DataFrame(columns=fields or sorted(TUSHARE_DIVIDEND))
        return pd.DataFrame(items, columns=fields)

    def fetch_dividend(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        """逐标的取数后拼接（与其它源同一形状）。

        与 `fetch_adjust_factor` 的两处差别都来自「分红是**事件流**」：

        * 区间交给 `normalize_dividend(start=, end=)` —— 见 `_default_dividend_fetch`
          那段注释（接口给不了区间，所以本地这一遍是唯一的过滤点）。
        * 某一段区间没有分红**不是失败**：这是一个**合法的空结果**（那只票那段时间就是
          没分红），与「这个源不给分红数据」不同。区别在于后者由基类的
          `fetch_dividend` 显式抛 `UNSUPPORTED`，而这里返回一个**列名齐全的空帧**。
          ⚠️ 这两种空在上层读出来都是「没有分红」，所以**分辨它们只能靠「有没有异常」**——
          这也是为什么基类那条默认实现绝不返回空帧。
        """
        frames = [normalize_dividend(self._call(self._fetch_dividend, symbol=symbol,
                                                start=start, end=end),
                                     TUSHARE_DIVIDEND, symbol=symbol,
                                     start=start, end=end)
                  for symbol in symbols]
        if not frames:
            return _empty(*DIVIDEND_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    # ── 本类**不覆盖**的三个数据面 ───────────────────────────────────────────
    # 这三段不是「照抄基类」：`_AdapterBase` **故意**只给了 `fetch_adjust_factor`
    # 与 `fetch_dividend` 两个默认实现，另外三个留在 `SourceAdapter` 里当抽象方法
    # （理由见基类注释：那三条是「一个源适配器至少要能做的是什么」）。于是
    # **一个只覆盖部分数据面的源必须自己把这三条写出来** —— 不写就实例化不了。
    #
    # 2026-09-29 实测：本类第一版**没写**这三条，而 `TushareAdapter` 此前从未被
    # 导入、从未被实例化（只被 `ast` 解析过），所以门禁全绿、用例全绿，直到实测
    # `TushareAdapter()` 才当场 `TypeError: Can't instantiate abstract class`。
    # ⇒ 「采集通道已接」当时在运行时是不成立的。守这件事的用例是
    # `tests/test_data_center_adapter.py::test_every_concrete_adapter_can_be_constructed`。
    def fetch_daily_bar(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        raise SourceAdapterError(
            'tushare 这条通道只有复权因子与分红（附录 B21.1 / B22.1）—— '
            '日线走腾讯或东财，见契约 §3.2',
            source=self._SOURCE, kind='UNSUPPORTED')

    def fetch_financial(self, symbols: List[str], period_end: date) -> pd.DataFrame:
        raise SourceAdapterError(
            'tushare 有财务接口，但本迭代没接（附录 B21.1）—— 财务走东财，见契约 §3.2',
            source=self._SOURCE, kind='UNSUPPORTED')

    def fetch_index_members(self, index_code: str, as_of_date: date) -> pd.DataFrame:
        raise SourceAdapterError(
            'tushare 的指数成分股接口本迭代没接（附录 B21.1）—— 指数成分股走东财，见契约 §3.2',
            source=self._SOURCE, kind='UNSUPPORTED')


def code_digits(symbol: Any) -> str:
    """`600000.SH` → `600000`。给「只要裸代码」的源用（akshare）。

    先过 `normalize_symbol` 再切，是为了让 `sh.600000` 这种写法也能被接受；
    直接 `split('.')` 会把它切成 `sh` 和 `600000` 两半，然后静默地取错那半。
    """
    return normalize_symbol(symbol).split('.')[0]


def code_glued(symbol: Any) -> str:
    """`600000.SH` → `sh600000`（腾讯的写法：小写前缀、无分隔符）。

    同 `code_digits` / `_baostock_code`：**先生成后缀归一化再切**。这根字符串会直接
    拼进 `param` 的位置串里，拼错的表现是源回一句 `bad params`，而不是抛异常。
    """
    normalized = normalize_symbol(symbol)
    digits, exchange = normalized.split('.')
    return '%s%s' % (exchange.lower(), digits)


def _baostock_code(symbol: Any) -> str:
    """`600000.SH` → `sh.600000`（baostock 的写法：小写前缀，点分隔）。"""
    normalized = normalize_symbol(symbol)
    digits, exchange = normalized.split('.')
    return '%s.%s' % (exchange.lower(), digits)
