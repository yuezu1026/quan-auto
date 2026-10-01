#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""platform-text-parity —— 两侧「定点显示规则」的对拍**夹具**，以及它是不是还新鲜。

为什么需要它
------------
平台层读取侧切片（`platform/`，附录C）把 `quanauto/dashboard.py` 的**显示规则**也抄了
一份：`ReportProjection.formatMetric` / `fixed`。于是同一条规则**两侧各有一份实现**，
而抄完之后**没有任何东西逐位对拍过它们** —— `ReportProjection.fixed` 的 Javadoc 自己
把这条登记成「已知边界」，附录C §C.5 第 2/3 条也登记着；唯一的对拍工具
`platform/check_text_parity.py` 需要一个活着的 JVM ⇒ **它不是门禁**，也就不构成常驻证据。
（那两条登记的现状：第 2 条已由 `platform-spec-parity` 关掉、第 3 条**就是本判据**，
逐条订正写在附录C §C.8 与 §C.11。）

2026-10-01 实测：这两份实现**真的会分歧**。用 73929 组样本（十进制平局点、精确值落在
平局点、真实报告值、随机值）对拍，**6887 组（9.3%）打印出不同的字符串**，两类成因：

  * **丢了负号**（4161 组）：`-0.005` 这类「负的、但舍入后落到零上」的值。Python 打印
    `-0.01` / `-0.00`，Java 的 `BigDecimal` **零没有符号**，`setScale` 之后减号就没了
    （旧实现只补了「原值恰好是 `-0.0`」那一种，见 `fixed` 里那句补号分支）。
  * **平局落错边**（2726 组）：`BigDecimal.valueOf(double)` 走的是 `Double.toString` 的
    **最短往返表示**，它是「能唯一还原这个 double 的十进制里最短的那个」，**不等于**这个
    double 的精确值。当那个最短表示恰好是一个十进制平局点（`-29.95`、`2.675`、
    `182.745`）时，两侧就会分道扬镳：Python 的 `"%.*f"` 是**按精确值**正确舍入的
    （`-29.95` 的精确值在 `-29.95` 略小的一侧 ⇒ `-29.9`），而 Java 拿 `-29.95` 这个
    十进制串去做 HALF_EVEN ⇒ `-30.0`。

本判据就是那次实测的**常驻形式**：一份**由 Python 侧现算出来的**夹具
（`platform/text-parity-cases.json`），加上两条腿 ——

  ① **Python 侧**（本脚本）：夹具里的 `text` 必须等于 `quanauto.dashboard.format_metric`
     **此刻**的输出。
  ② **Java 侧**（`platform/api/src/test/java/.../FormatParityTest.java`，由
     `platform-runtime` 门禁真跑 `mvn test`）：每条用例喂进
     `ReportProjection.formatMetric`，输出必须逐字节等于夹具里的 `text`。

两侧各自都绿**不算**接起来能跑 —— 这条判据钉的正是那条缝。任何一侧的规则被改动，
另一侧就会红。

比什么
------
`METRIC_SPECS` 里出现的每一个 `(unit, digits)` 组合，以及每个组合上的：
十进制平局点、精确值落在平局点、负零、`-0.0` 之外的「舍入到零的负值」、极大值
（`1e20`、`Double.MAX_VALUE`）、次正规数、以及三份**真实报告**里 14 个指标的原值。

**边界（它证明不了什么）**
--------------------------
* 它**不比对「服务端算好的 `text` 有没有真的印到页面上」**。那一段（REST 信封、字段名、
  前端有没有偷偷 `toFixed`）仍然只有手动脚本 `platform/check_text_parity.py`，而那个
  脚本需要一个活着的 JVM ⇒ 不能当门禁。本判据把「展示**规则**」钉住了，「展示**链路**」
  没有。
* 它**不启动 JVM、不渲染页面、不跑 `mvn test`**（那是 `platform-runtime` 的活）。它自己
  只做一件事：**证明夹具是新鲜的**。夹具新不新鲜与 Java 侧对不对是两件事，报告里必须分得开
  —— 所以本脚本的所有检查码都以 `PARITY-` 开头，Java 侧红在 `PB-MVN-TESTS-RED` 上。
* 它**不判断显示规则本身好不好**（`-0` 这种输出好不好看不是它能回答的）。`quanauto/dashboard.py`
  在这里是**参照物**：分歧就是 Java 侧要改，改判据去迁就它是本仓库明令禁止的动作。
* 它**不覆盖非有限输入**：`percent` 量纲的入参在 ×100 之后溢出成 `inf` 时，Python 的
  `format_metric` 会打印 `inf%`（`_require_number` 只在**换算前**查有限性 —— 这是上游
  `dashboard.py` 的一个缺口），而 Java 侧**故意拒绝**（打印 `inf%` 比报错更糟）。这一条
  **不同口径是故意的**，登记在本判据的 `UNMATCHABLE` 里（`why` 会随夹具一同落盘，
  见夹具的 `why` 最后一条）。

判据（每条都有样本，见 `--selftest`）
--------------------------------------
* `PARITY-NO-PY`              `quanauto.dashboard` 导不进来 ⇒ 无法现算，拒绝通过
* `PARITY-NO-FIXTURE`         夹具读不出来 / 不是合法 JSON / 没有 `cases`
* `PARITY-NO-CASES`           夹具里 0 条用例（空转守卫：后面每条比对都会在空转）
* `PARITY-THIN-CASES`         用例数低于 `MIN_CASES`（生成器/提取器坏了的报警器）
* `PARITY-CASE-SHAPE`         某条用例字段缺失、`value` 解不成浮点、`unit` 不认识
* `PARITY-STALE-CASES`        夹具与「生成器现在会产出的东西」**逐条不等**
                              （**这一条是防单向判据的**：某个用例的 `value` 过期了 ——
                              例如报告 JSON 变了 —— 它的 `text` 仍然自洽，`PARITY-DRIFT`
                              一声不响，夹具会**悄悄老去**）
* `PARITY-DRIFT`              夹具里的 `text` ≠ Python 此刻现算的值
* `PARITY-SPEC-COVERAGE`      `METRIC_SPECS` 里有 key 一条 `report-*` 用例都没有
* `PARITY-UNITDIGITS-COVERAGE` `METRIC_SPECS` 用到的某个 `(unit, digits)` 组合没有用例
* `PARITY-SPEC-MISMATCH`      某条 `report-*` 用例的 `(unit, digits)` 与该 key 的规范行不一致
* `PARITY-EDGE-MISSING`       声明在 `EDGE_CASES` 里的样本没进夹具
* `PARITY-EDGE-THIN`          `EDGE_CASES` 本身被削到 `MIN_EDGE_CASES` 以下
* `PARITY-EDGE-DUPLICATE`     `EDGE_CASES` 里有重复的 `(unit, digits, value)`（会静默少一条）
* `PARITY-UNMATCHABLE-OK`     `UNMATCHABLE` 里登记为「Java 侧无法对拍」的样本，Python 侧其实
                              打印的是**正常定点数** ⇒ 排除理由不成立，别再排除它

夹具怎么再生
------------
    python tools/verify_platform_text_parity.py --write

**顺序要紧**：改完 `quanauto/dashboard.py` 的显示规则（或报告 JSON）之后，`--write` 会让
`PARITY-STALE-CASES` 熄灭，但 Java 侧的 `FormatParityTest` 会**变红** —— 那是判据在工作，
正确的反应是去改 `ReportProjection`，**不是**删夹具、也不是放宽判据。

用法
----
    python tools/verify_platform_text_parity.py             # 真跑（判夹具新不新鲜）
    python tools/verify_platform_text_parity.py --write      # 重新生成夹具
    python tools/verify_platform_text_parity.py --selftest   # 每个探测器一个样本
    python tools/verify_platform_text_parity.py --root=PATH  # 对着别的树跑（自测用）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXTURE_REL = os.path.join('platform', 'text-parity-cases.json')
REPORTS_REL = os.path.join('.rounds', 'i1')
FIXTURE_SCHEMA = 'quanauto.text-parity-cases/1'

# 用例总数的下限。它是「生成器/夹具整体变薄」的**报警器**，不是「该有几条」的裁决
# （那由 `METRIC_SPECS` 的规模与 `EDGE_CASES` 的声明决定，两条各有自己的判据）。
MIN_CASES = 60
# `EDGE_CASES` 声明条数的下限。实测（2026-10-01）挑了 59 条，其中 **30 条**在旧实现下
# 真的会分歧 —— 这个门槛比实测值低，理由同 `verify_platform_specs.MIN_ROWS`：
# 它防的是「清单被悄悄削光」，不是钉住当前条数。
MIN_EDGE_CASES = 45

UNITS = ('percent', 'ratio', 'count', 'money', 'days')

# ── 边角样本清单 ─────────────────────────────────────────────────────────
# 每一项 = (name, unit, digits, value 的十进制串, 为什么挑它)。
#
# `value` 存**十进制串**而不是二进制：两侧都从它解析（Python 用 `float()`，Java 用
# `Double.parseDouble`），于是「夹具里写的数」与「两侧看到的数」逐位相同。若存成 JSON
# 数字，读写两侧各带一轮自己的最短表示转换，夹具本身就成了一处可漂移的中间层。
#
# **name 必须唯一**（重复会静默少一条用例，由 `PARITY-EDGE-DUPLICATE` 盯着）。
EDGE_CASES = (
    # ── 十进制平局点：最短往返表示恰好落在平局上，旧实现会落错边 ──
    ('tie-ratio1-neg2995', 'ratio', 1, '-29.95',
     '平局点。精确值在 -29.95 略小的一侧 ⇒ Python -29.9；旧 Java 拿十进制串做 HALF_EVEN ⇒ -30.0'),
    ('tie-ratio1-neg2985', 'ratio', 1, '-29.85',
     '同上。进位还会改整数部分（-29.8 vs -29.9），所以两个成因必须分开验'),
    ('tie-ratio1-neg2915', 'ratio', 1, '-29.15', '平局点，负向'),
    ('tie-ratio1-neg2895', 'ratio', 1, '-28.95', '平局点，负向'),
    ('tie-ratio1-pos2995', 'ratio', 1, '29.95',
     '平局点的**正**值也要有一条：否则修法可能变成「负值特判」而不是「改用精确值」'),
    ('tie-ratio1-neg2825', 'ratio', 1, '-28.25',
     '同一组合上的**不分歧**样本（精确值就在 -28.2 那一侧）：防「为了对齐而过度修正」'),
    ('tie-ratio2-2675', 'ratio', 2, '2.675',
     '教科书样本：最短表示 "2.675" 是平局，精确值在下方 ⇒ Python 2.67，旧 Java 2.68'),
    ('tie-ratio2-neg2675', 'ratio', 2, '-2.675', '上一行的负号版'),
    ('tie-ratio2-1005', 'ratio', 2, '1.005', '同类但两侧碰巧一致：防过度修正'),
    ('tie-ratio2-0615', 'ratio', 2, '0.615', '同类，两侧**不一致**'),
    ('tie-ratio2-8835', 'ratio', 2, '8.835', '同类，两侧一致（进位的方向相反）'),
    ('tie-money2-182745', 'money', 2, '182.745', '真实佣金量级上的平局点'),
    ('tie-money2-neg182745', 'money', 2, '-182.745', '上一行的负号版'),
    ('tie-money2-0005', 'money', 2, '0.005', '极小值的平局点：0.01 vs 0.00'),
    ('tie-money2-neg0005', 'money', 2, '-0.005',
     '同时踩两个成因：既落在平局上、又是「舍入到零的负值」⇒ Python -0.01，旧 Java 0.00'),
    ('tie-money2-96795655', 'money', 2, '96795.655', '期末净值量级上的平局点'),
    ('tie-money2-neg96795655', 'money', 2, '-96795.655', '上一行的负号版'),
    ('tie-money1-005', 'money', 1, '0.05', '1 位小数上的平局：Python 0.1，旧 Java 0.0'),
    ('tie-money1-neg005', 'money', 1, '-0.05', '上一行的负号版（旧 Java 连减号一起丢）'),
    ('tie-money1-015', 'money', 1, '0.15', '平局，进位改整数部分 ⇒ 0.1 vs 0.2'),
    ('tie-money1-025', 'money', 1, '0.25', '平局，两侧一致（HALF_EVEN 取偶）'),
    ('tie-percent4-00000125', 'percent', 4, '0.0000125',
     'percent 量纲先乘 100 ⇒ 0.00125，再在 4 位小数上取平局。两侧不一致'),
    ('tie-percent4-neg00000125', 'percent', 4, '-0.0000125', '上一行的负号版'),
    ('tie-percent4-neg00000245', 'percent', 4, '-0.0000245', '同类，两侧一致'),
    ('tie-ratio4-000005', 'ratio', 4, '0.00005', '4 位小数上的平局：0.0001 vs 0.0000'),
    ('tie-days2-1005', 'days', 2, '1.005', 'days 量纲也要有一条平局样本'),

    # ── 舍入到零的负值：BigDecimal 的零没有符号 ──
    ('negzero-money2-minus00', 'money', 2, '-0.0',
     '旧实现专门补过的那一条（原值恰好是负零）—— 它绿不代表整族都对'),
    ('negzero-money2-zero', 'money', 2, '0.0', '正零对照'),
    ('negzero-money2-minus1e-9', 'money', 2, '-1e-09',
     '比负零更常见的那一种：负的、小的、舍入后落到零上'),
    ('negzero-money2-1e-9', 'money', 2, '1e-09', '正的小值对照'),
    ('negzero-money2-minus5e-324', 'money', 2, '-5e-324',
     '最小的负次正规数：任何定点位数下都舍入到零，唯一的区别就是那个减号'),
    ('negzero-ratio4-minus1e-5', 'ratio', 4, '-0.00001', '4 位小数上的「舍入到零的负值」'),
    ('negzero-ratio4-minus5e-6', 'ratio', 4, '-0.000005', '恰好落在平局上的极小负值'),
    ('negzero-ratio4-minus49e-6', 'ratio', 4, '-0.000049', '平局点略下方'),
    ('negzero-ratio4-49e-6', 'ratio', 4, '0.000049', '正数对照'),
    ('negzero-percent4-minus1e-8', 'percent', 4, '-1e-08',
     'percent 量纲：乘 100 之后是 -1e-6 ⇒ Python "-0.0000%"，旧 Java "0.0000%"'),
    ('negzero-percent4-1e-8', 'percent', 4, '1e-08', '上一行的正号版'),
    ('negzero-percent4-minus00', 'percent', 4, '-0.0', 'percent 量纲上的负零'),
    ('negzero-percent4-zero', 'percent', 4, '0.0', 'percent 量纲上的正零'),

    # ── 0 位小数的定点（含 `-0`）──
    ('zero-digit-neg05', 'ratio', 0, '-0.5',
     '0 位小数：Python 打印 "-0"，旧 Java 打印 "0"（丢减号）'),
    ('zero-digit-pos05', 'ratio', 0, '0.5', '正数对照：两侧都是 "0"'),
    ('zero-digit-neg15', 'ratio', 0, '-1.5', '非平局的负值：两侧都是 "-2"'),
    ('zero-digit-25', 'ratio', 0, '2.5', '平局取偶：两侧都是 "2"'),
    ('zero-digit-neg25', 'ratio', 0, '-2.5', '负的平局取偶'),
    ('zero-digit-neg005', 'ratio', 0, '-0.05', '0 位小数 + 舍入到零 + 负号，三条叠在一起'),

    # ── 极大值：不许走科学计数，也不许被 `(long)` 截断 ──
    ('huge-money2-1e20', 'money', 2, '1e+20', '定点大数必须逐位展开（`toPlainString` 的职责）'),
    ('huge-money2-minus1e20', 'money', 2, '-1e+20', '上一行的负号版'),
    ('huge-money2-maxdouble', 'money', 2, '1.7976931348623157e+308',
     '双精度最大值：309 位的十进制展开，两侧必须逐位一样'),
    ('huge-count0-1e20', 'count', 0, '1e+20',
     '计数类的极大值 —— `(long)` 在这里会溢出并静默给出一个错的整数，所以 Java 侧走 BigDecimal'),
    ('huge-count0-1e23', 'count', 0, '1e+23',
     '**非可表示**的极大整数：`1e23` 的精确值是 99999999999999991611392（`int(1e23)` 就是这个），'
     '而 `BigDecimal.valueOf(1e23)` 按最短往返表示给出 1e23 ⇒ 两侧打印出不同的整数。'
     '`new BigDecimal(double)` 才是精确值，这条钉的就是那个差别'),

    # ── 真实报告里的值（人工从 .rounds/i1 抄出来的那一批由下面自动生成）──
    ('real-ratio4-sharpe7a', 'ratio', 4, '1.5102148302585512', '真实夏普量级，两侧一致'),
    ('real-ratio4-neg-sharpe7a', 'ratio', 4, '-1.5102148302585512', '上一行的负号版'),
    ('real-money2-commission7a', 'money', 2, '182.7465397', '真实佣金，两侧一致'),
    ('real-percent4-totalreturn7a', 'percent', 4, '-0.032043452542899906',
     '真实累计收益：显示成 -3.2043%，是 Java 侧 `readsRealReportFromRounds` 的锚'),
    ('real-days2-holdperiod7a', 'days', 2, '2.0', '真实平均持仓天数'),
    ('real-count0-trades7a', 'count', 0, '1.0', '真实成交笔数（整数，不给小数）'),
    ('real-count0-zero', 'count', 0, '0.0', '计数类的零'),
    ('real-count0-negative', 'count', 0, '-1.0', '计数类的负整数'),
    ('real-count0-negzero', 'count', 0, '-0.0',
     '计数类的负零：两侧都走「必须是整数」分支 ⇒ "0"（这条钉的是分支，不是补号）'),
)

# ── 登记在册的**故意**分歧 ───────────────────────────────────────────────
# (unit, digits, value, 为什么两侧**不该**一致)
#
# 这一批**不进夹具**：夹具是「两侧必须逐字节相等」的清单，把故意分歧塞进去会让 Java 侧
# 的用例变成「有时抛有时不抛」的怪物。它们在这里登记，`PARITY-UNMATCHABLE-OK` 反过来
# 检查「排除理由还成立吗」—— 若哪天 Python 侧也拒绝非数了，这条会红，提醒把它们收进夹具。
UNMATCHABLE = (
    ('percent', 4, '1.8e+306',
     'percent 量纲先乘 100 ⇒ 溢出成 +inf，Python 的 "%.4f" % inf 打印 "inf%"；'
     'Java 侧**故意拒绝**（打印 "inf%" 比报 422 更糟）。上游缺口：'
     '`format_metric` 只在换算**前**检查有限性（`_require_number`），换算后没有再查一次'),
    ('percent', 4, '-1.8e+306',
     '同上，负向 ⇒ Python "-inf%"'),
)


class FixtureMissing(Exception):
    """夹具读不出来 —— 不是「0 条用例」，是「没有夹具」。两者必须分开报。"""


def harden_stdout():
    """控制台是 cp936：一个 GBK 之外的字符就能让整份报告在打印途中抛
    UnicodeEncodeError。降级成 '?' 而不是崩掉，否则结论会整个丢掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def read_text(path):
    """统一按 UTF-8 + LF 读。CRLF 会让裸 \\n 的正则**静默**失配（提取 0 却全绿）。"""
    with open(path, 'r', encoding='utf-8-sig') as handle:
        return handle.read().replace('\r\n', '\n')


def write_text(path, text):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def load_dashboard():
    """导入 `quanauto.dashboard` —— **参照物**。

    这里**必须** import（而不是像 `verify_platform_specs.py` 那样只用 AST）：本判据的
    等价物定义就是「Python 侧**此刻**算出什么」，把 `format_metric` 再实现一遍等于拿
    自己的复制品核对自己。代价是门禁依赖 `quanauto` 可导入 —— 但它是本仓库自己的包、
    且只依赖标准库（`math` / `dataclasses` / `typing`），所以在门禁的解释器里必然可导入；
    真的导不进来会响亮地报 `PARITY-NO-PY`，不会静默跳过。

    读常量一律走这里的返回值，**不许**再用 AST 抄一份（那才是会漂的第二份副本）。
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from quanauto import dashboard  # noqa: PLC0415  (刻意的延迟导入)
    return dashboard


def float_or_none(raw):
    """十进制串 → float；解不出、或解出 NaN/inf 都返回 None。

    `inf` 必须一起拒：`"1e999"` 会被 `float()` 悄悄吃成 `inf`，于是坏样本变成「能解析的
    数」，而 Java 侧 `Double.parseDouble` 拿到 `inf` 之后行为完全不同。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return value


def make_case(source, key, unit, digits, raw, text, why=''):
    return {'source': source, 'key': key, 'unit': unit, 'digits': digits,
            'value': raw, 'text': text, 'why': why}


def build_cases(root):
    """生成夹具的 `cases`（**确定性**：同样的输入必然给出逐字节相同的输出）。

    顺序 = `EDGE_CASES` 的声明顺序，然后是三份真实报告 × `METRIC_SPECS` 的行序。
    报告读不出来**不在这里报错**：那是 `PARITY-NO-FIXTURE` 那一路的兄弟问题，
    由调用方把异常转成 FINDING。
    """
    dashboard = load_dashboard()
    cases = []

    for name, unit, digits, raw, why in EDGE_CASES:
        spec = dashboard.MetricSpec('edge:' + name, '', '', unit, digits)
        value = float_or_none(raw)
        if value is None:
            raise FixtureMissing('EDGE_CASES 里的 %s 值 %r 解不成浮点' % (name, raw))
        cases.append(make_case('edge', 'edge:' + name, unit, digits, raw,
                               dashboard.format_metric(spec, value), why))

    directory = os.path.join(root, REPORTS_REL)
    try:
        names = sorted(f for f in os.listdir(directory) if f.endswith('.json'))
    except OSError as exc:
        raise FixtureMissing('读不了报告目录 %s（%s）' % (REPORTS_REL, exc))
    if not names:
        raise FixtureMissing('%s 里没有 .json 报告 —— 夹具会缺掉「真实值」那一半'
                             % REPORTS_REL)
    for name in names:
        with open(os.path.join(directory, name), 'r', encoding='utf-8-sig') as handle:
            payload = json.load(handle)
        try:
            performance = payload['deterministic']['performance']
        except (KeyError, TypeError) as exc:
            raise FixtureMissing('%s 里取不到 deterministic.performance（%s）' % (name, exc))
        report_id = name[:-len('.json')]
        for spec in dashboard.METRIC_SPECS:
            if spec.key not in performance:
                raise FixtureMissing('%s 的 performance 段缺 %s' % (report_id, spec.key))
            value = float(performance[spec.key])
            # 存 `repr(value)` 而不是 JSON 数字：写出去是「最短往返表示」，读回来
            # `float()` / `Double.parseDouble` 逐位还原同一个 double。
            cases.append(make_case('report:' + report_id, spec.key, spec.unit,
                                   spec.digits, repr(value),
                                   dashboard.format_metric(spec, value)))
    return cases


def fixture_payload(cases):
    return {
        'schema': FIXTURE_SCHEMA,
        'generated_by': 'tools/verify_platform_text_parity.py --write',
        'why': [
            '两侧的定点显示规则各有一份实现（Python 的 quanauto.dashboard.format_metric '
            '与 Java 的 ReportProjection.fixed）。',
            '2026-10-01 实测：旧 Java 实现在 73929 组样本里有 6887 组（9.3%）打印出'
            '与 Python 不同的字符串（丢负号 4161 / 平局落错边 2726）⇒ 两侧的展示文本'
            '真的会不一致。',
            '本文件是那次实测的常驻形式：每条用例的 text 是 **Python 侧现算** 的值，'
            '由 tools/verify_platform_text_parity.py 核它是否新鲜（PARITY-DRIFT / '
            'PARITY-STALE-CASES），并由 Java 侧的 FormatParityTest 逐条对拍'
            '（platform-runtime 门禁真跑 mvn test）。',
            '手改本文件必红：它是生成物。再生命令：python tools/verify_platform_text_parity.py --write',
            '故意不同口径的那批（percent 换算后溢出）**不在**本文件里，登记在生成脚本的'
            'UNMATCHABLE 中。',
        ],
        'cases': cases,
    }


# ── 审计 ─────────────────────────────────────────────────────────────────
def audit(root):
    """返回 (summary, findings)。findings = [(code, message)]，已排序去重。"""
    findings = []
    summary = {'root': root, 'cases': 0, 'edge': 0, 'report': 0, 'compared': 0,
               'reports': [], 'units': 0}

    try:
        dashboard = load_dashboard()
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都要变成 FINDING，不能静默
        return summary, [('PARITY-NO-PY',
                          '导不进来 quanauto.dashboard（%s: %s）—— 无法现算参照值，'
                          '后面每条比对都会在空转，拒绝通过'
                          % (type(exc).__name__, exc))]

    # ── ① 读夹具（失败与「0 条用例」必须分开报）──
    path = os.path.join(root, FIXTURE_REL)
    data = None
    try:
        data = json.loads(read_text(path))
    except OSError:
        findings.append(('PARITY-NO-FIXTURE', '读不出来 %s' % FIXTURE_REL))
    except (ValueError, UnicodeDecodeError) as exc:
        findings.append(('PARITY-NO-FIXTURE', '%s 不是合法 JSON（%s）' % (FIXTURE_REL, exc)))
    if data is not None:
        if not isinstance(data, dict) or data.get('schema') != FIXTURE_SCHEMA:
            findings.append(('PARITY-NO-FIXTURE',
                             '%s 的 schema 不是 %r' % (FIXTURE_REL, FIXTURE_SCHEMA)))
        elif not isinstance(data.get('cases'), list):
            findings.append(('PARITY-NO-FIXTURE', '%s 里没有 cases 数组' % FIXTURE_REL))

    cases = data.get('cases') if isinstance(data, dict) and isinstance(
        data.get('cases'), list) else []
    summary['cases'] = len(cases)

    # ── ② 空转守卫：为空 / 变薄 ⇒ 拒绝通过，绝不打印「0 issue(s) PASS」──
    if not cases:
        findings.append(('PARITY-NO-CASES',
                         '%s 里 0 条用例 —— 后面每一条比对都会在空转，拒绝通过'
                         % FIXTURE_REL))
    elif len(cases) < MIN_CASES:
        findings.append(('PARITY-THIN-CASES',
                         '夹具只有 %d 条用例（门槛 %d）—— 生成器或夹具被削过，拒绝通过'
                         % (len(cases), MIN_CASES)))

    # ── ③ 逐条：形状 + 现算对拍 ──
    specs = {spec.key: spec for spec in dashboard.METRIC_SPECS}
    combos = sorted({(spec.unit, spec.digits) for spec in dashboard.METRIC_SPECS})
    summary['units'] = len(combos)
    seen = {}
    for index, case in enumerate(cases):
        label = '%s[%d]' % (FIXTURE_REL, index)
        if not isinstance(case, dict):
            findings.append(('PARITY-CASE-SHAPE', '%s 不是对象' % label))
            continue
        missing = [f for f in ('source', 'key', 'unit', 'digits', 'value', 'text')
                   if f not in case]
        if missing:
            findings.append(('PARITY-CASE-SHAPE',
                             '%s 缺字段 %s' % (label, ', '.join(missing))))
            continue
        unit, digits, raw, text = case['unit'], case['digits'], case['value'], case['text']
        if unit not in UNITS:
            findings.append(('PARITY-CASE-SHAPE', '%s 的 unit=%r 不认识' % (label, unit)))
            continue
        if not isinstance(digits, int) or isinstance(digits, bool):
            findings.append(('PARITY-CASE-SHAPE', '%s 的 digits=%r 不是整数'
                             % (label, digits)))
            continue
        if not isinstance(text, str):
            findings.append(('PARITY-CASE-SHAPE', '%s 的 text 不是字符串' % label))
            continue
        if not isinstance(raw, str) or float_or_none(raw) is None:
            findings.append(('PARITY-CASE-SHAPE',
                             '%s 的 value=%r 不是能解析的十进制串（存串是为了让两侧'
                             '看到同一个数，见 EDGE_CASES 的说明）' % (label, raw)))
            continue
        summary['compared'] += 1
        if case['source'] == 'edge':
            summary['edge'] += 1
            # 只记**边角**用例：真实报告用例的 (unit, digits, raw) 可能碰巧与某条声明
            # 相同，一旦它把 `seen` 填上，那条声明就算「没进夹具」也查不出来。
            seen[(unit, digits, raw)] = label
        elif str(case['source']).startswith('report:'):
            summary['report'] += 1

        spec = dashboard.MetricSpec(str(case['key']), '', '', unit, digits)
        try:
            actual = dashboard.format_metric(spec, float(raw))
        except Exception as exc:  # noqa: BLE001 - 上游拒绝了这个输入，本身就是不一致
            findings.append(('PARITY-DRIFT',
                             '%s（%s / %s / %s）Python 侧**抛了** %s：%s（夹具里写的 text '
                             '是 %r）' % (label, case['key'], unit, raw,
                                          type(exc).__name__, exc, text)))
            continue
        if actual != text:
            findings.append(('PARITY-DRIFT',
                             '%s（%s / %s / %s）夹具 text=%r，Python 现在算出来是 %r —— '
                             '夹具老了，先确认 Java 侧跟上了再 --write 再生'
                             % (label, case['key'], unit, raw, text, actual)))

        if str(case['source']).startswith('report:'):
            spec_row = specs.get(case['key'])
            if spec_row is None:
                findings.append(('PARITY-SPEC-MISMATCH',
                                 '%s 用了 %r 这个 key，而 METRIC_SPECS 里没有它'
                                 % (label, case['key'])))
            elif (spec_row.unit, spec_row.digits) != (unit, digits):
                findings.append(('PARITY-SPEC-MISMATCH',
                                 '%s 的 (unit, digits)=(%s, %d)，而 METRIC_SPECS 里 %s 是 '
                                 '(%s, %d)' % (label, unit, digits, case['key'],
                                               spec_row.unit, spec_row.digits)))

    # ── ④ 逐条与「生成器此刻会产出的东西」比：防夹具**悄悄老去** ──
    # 这一条是防单向的：若某个用例的 `value` 过期（报告 JSON 变了），它的 `text`
    # 仍然自洽 ⇒ PARITY-DRIFT 一声不响。只查「text 对不对」的判据看不见这种漂移。
    try:
        expected = build_cases(root)
    except Exception as exc:  # noqa: BLE001
        findings.append(('PARITY-STALE-CASES',
                         '生成器现在跑不出来（%s: %s）—— 无法判断夹具是否新鲜'
                         % (type(exc).__name__, exc)))
        expected = None
    if expected is not None and cases:
        if len(expected) != len(cases):
            findings.append(('PARITY-STALE-CASES',
                             '夹具 %d 条，生成器现在会产出 %d 条（差 %+d）—— 跑 '
                             '--write 再生；若 Java 侧还没跟上，先改 Java 侧'
                             % (len(cases), len(expected), len(expected) - len(cases))))
        else:
            drifted = [(i, a, b) for i, (a, b) in enumerate(zip(cases, expected))
                       if any(a.get(f) != b.get(f) for f in
                              ('source', 'key', 'unit', 'digits', 'value', 'text'))]
            if drifted:
                index, a, b = drifted[0]
                diff = [f for f in ('source', 'key', 'unit', 'digits', 'value', 'text')
                        if a.get(f) != b.get(f)]
                findings.append(('PARITY-STALE-CASES',
                                 '夹具第 %d 条与生成器现在会产出的东西不同（字段 %s：'
                                 '夹具 %s → 生成器 %s）。共 %d 条不同 —— 跑 --write 再生'
                                 % (index, '/'.join(diff),
                                    [a.get(f) for f in diff], [b.get(f) for f in diff],
                                    len(drifted))))

    # ── ⑤ 覆盖：每个 key 都要有一条真实报告用例，每个 (unit, digits) 组合都要有用例 ──
    if cases:
        report_keys = {c.get('key') for c in cases
                       if str(c.get('source', '')).startswith('report:')}
        for key in specs:
            if key not in report_keys:
                findings.append(('PARITY-SPEC-COVERAGE',
                                 'METRIC_SPECS 里的 %s 一条 report-* 用例都没有 ⇒ '
                                 '它的 (unit, digits) 组合没有真实值上的样本' % key))
        for unit, digits in combos:
            if (unit, digits) not in {(c.get('unit'), c.get('digits')) for c in cases
                                      if isinstance(c, dict)}:
                findings.append(('PARITY-UNITDIGITS-COVERAGE',
                                 'METRIC_SPECS 用到的 (%s, %d) 组合在夹具里没有用例 '
                                 '⇒ 这个组合的定点规则没被对拍过' % (unit, digits)))
        summary['reports'] = sorted({str(c.get('source'))[len('report:'):]
                                     for c in cases if isinstance(c, dict)
                                     and str(c.get('source', '')).startswith('report:')})

    # ── ⑥ 声明清单自己：条数下限、重复、以及每条都真的进了夹具 ──
    if len(EDGE_CASES) < MIN_EDGE_CASES:
        findings.append(('PARITY-EDGE-THIN',
                         'EDGE_CASES 只剩 %d 条（门槛 %d）—— 边角清单被削过'
                         % (len(EDGE_CASES), MIN_EDGE_CASES)))
    declared = [(unit, digits, raw) for _, unit, digits, raw, _ in EDGE_CASES]
    duplicates = sorted({key for key in declared if declared.count(key) > 1})
    for unit, digits, raw in duplicates:
        findings.append(('PARITY-EDGE-DUPLICATE',
                         'EDGE_CASES 里 (%s, %d, %s) 声明了不止一次 —— 后一条会覆盖前一条，'
                         '清单看着一大串、实际样本少一条' % (unit, digits, raw)))
    if cases:
        for name, unit, digits, raw, _ in EDGE_CASES:
            if (unit, digits, raw) not in seen:
                findings.append(('PARITY-EDGE-MISSING',
                                 'EDGE_CASES 里的 %s（%s / %s / %s）没进夹具 —— '
                                 '声明了却没被对拍' % (name, unit, digits, raw)))

    # ── ⑦ 登记在册的故意分歧：排除理由还成立吗 ──
    for unit, digits, raw, _ in UNMATCHABLE:
        value = float_or_none(raw)
        if value is None:
            findings.append(('PARITY-UNMATCHABLE-OK', 'UNMATCHABLE 里的 %r 解不成浮点' % raw))
            continue
        spec = dashboard.MetricSpec('unmatchable', '', '', unit, digits)
        try:
            text = dashboard.format_metric(spec, value)
        except Exception:  # 上游也拒绝了 ⇒ 那一侧已经收口，可以收进夹具了
            findings.append(('PARITY-UNMATCHABLE-OK',
                             'UNMATCHABLE 登记的 (%s, %d, %s)：Python 侧现在也**拒绝**了 ⇒ '
                             '两侧已经同口径，把它从 UNMATCHABLE 移到 EDGE_CASES'
                             % (unit, digits, raw)))
            continue
        if 'inf' not in text and 'nan' not in text:
            findings.append(('PARITY-UNMATCHABLE-OK',
                             'UNMATCHABLE 登记的 (%s, %d, %s) 现在算出来是 %r（正常的定点数）'
                             '⇒ 「Java 侧无法对拍」这个理由不成立了，别再排除它'
                             % (unit, digits, raw, text)))

    return summary, sorted(set(findings))


def report(summary, findings):
    print('root:            %s' % summary['root'])
    print('fixture:         %s' % FIXTURE_REL)
    print('  cases=%d (edge=%d, report=%d)  reports=%s  unit/digits combos=%d'
          % (summary['cases'], summary['edge'], summary['report'],
             ','.join(summary['reports']) or '-', summary['units']))
    print('compared:        %d 条按 (unit, digits, value) 现算过（参照物 = '
          'quanauto.dashboard.format_metric）' % summary['compared'])
    print('java side:       platform/api/src/test/java/com/quanauto/dashboard/'
          'FormatParityTest.java（由 platform-runtime 门禁真跑 mvn test）')
    for code, message in findings:
        print('FINDING [%s] %s' % (code, message))
    print('verdict: %s (%d issue(s))'
          % ('PASS' if not findings else 'DIRTY', len(findings)))
    return 1 if findings else 0


def write_fixture(root):
    """按当前 Python 侧重新生成夹具，然后**立刻审一遍**。"""
    try:
        cases = build_cases(root)
    except Exception as exc:  # noqa: BLE001
        print('FINDING [PARITY-NO-FIXTURE] 生成夹具失败（%s: %s）'
              % (type(exc).__name__, exc))
        print('verdict: DIRTY (1 issue(s))')
        return 1
    if len(cases) < MIN_CASES:
        print('FINDING [PARITY-THIN-CASES] 只生成了 %d 条用例（门槛 %d）—— 拒绝覆盖夹具'
              % (len(cases), MIN_CASES))
        print('verdict: DIRTY (1 issue(s))')
        return 1
    path = os.path.join(root, FIXTURE_REL)
    write_text(path, json.dumps(fixture_payload(cases), ensure_ascii=False,
                                indent=2, sort_keys=False) + '\n')
    print('wrote: %s（%d 条用例）' % (FIXTURE_REL, len(cases)))
    summary, findings = audit(root)
    return report(summary, findings)


# ── 自测 ─────────────────────────────────────────────────────────────────
class HarnessError(Exception):
    """变异没打中 ⇒ 这是**工具**的问题，不许静默变成「干净样本」。"""


def sample(name, expect, mutate=None, raw=None, absent=False, root=None, exact=False,
           marker='', want=None, advisory=False):
    """一个样本：造沙箱 → 跑 audit → 断言「报出来的正是那个检查码」。

    只断言「有 FINDING」会让「变异没打到分支」与「探测器不存在」长得一模一样，
    所以 `marker=` 用来指定**那条 FINDING 的消息里必须出现**的串。

    样本的底稿**不取自真夹具**，而是就地用同一支生成器造一份：那样 `--selftest` 的结论
    不依赖真夹具现状，于是「夹具坏了」与「自测坏了」在报告里不会合成一个词。
    真夹具另有一次 `advisory` 运行（只印结论，不参与自测裁决）。

    `absent=True` ⇒ 故意不写夹具文件；`raw=<串>` ⇒ 夹具内容就是这串（用来造非法 JSON）。
    """
    if root is None:
        root = os.path.join(SANDBOX['root'], name)
        # 报告**总是**要有的：报告缺了会让生成器跑不出来，于是样本会多报一条
        # PARITY-STALE-CASES，看起来像「期望的那个码没触发」。
        copy_reports(ROOT, root)
        try:
            if absent:
                pass
            elif raw is not None:
                write_text(os.path.join(root, FIXTURE_REL), raw)
            else:
                data = fixture_payload(build_cases(ROOT))
                if mutate is not None:
                    data = mutate(data)
                    if not isinstance(data, dict):
                        raise HarnessError('变异没返回对象（%s）'
                                           % type(data).__name__)
                write_text(os.path.join(root, FIXTURE_REL),
                           json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        except HarnessError as exc:
            if advisory:
                print('  SAMPLE NOTE (advisory): %s' % exc)
                return True
            print('  SAMPLE FAIL (harness): %s' % exc)
            return False

    summary, findings = audit(root)
    codes = sorted({code for code, _ in findings})
    problems = []
    if expect is None or expect == ():
        if findings:
            problems.append('期望 0 报错（干净样本防误报），实际 %s' % codes)
    elif expect not in codes:
        problems.append('期望命中 %s，实际 %s' % (expect, codes))
    elif marker and not any(marker in msg for code, msg in findings if code == expect):
        problems.append('期望 %s 的消息里含 %r，实际 %s'
                        % (expect, marker, [m for c, m in findings if code == expect]))
    if exact and codes != [expect]:
        problems.append('期望**只有** %s 报出来，实际 %s' % (expect, codes))
    for key, value in (want or {}).items():
        if summary.get(key) != value:
            problems.append('期望 summary[%s] == %r，实际 %r'
                            % (key, value, summary.get(key)))
    for code, message in findings[:3]:
        print('    [%s] %s %s' % (name, code, message[:110]))
    if problems:
        if advisory:
            print('  SAMPLE NOTE (advisory, 不参与自测裁决): %s' % '; '.join(problems))
            return True
        print('  SAMPLE FAIL: %s' % '; '.join(problems))
        return False
    print('  sample ok: %s (expect=%s)' % (name, expect or 'clean'))
    return True


def copy_path(src, dst):
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            copy_path(os.path.join(src, name), os.path.join(dst, name))
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def copy_reports(src_root, dst_root):
    copy_path(os.path.join(src_root, REPORTS_REL), os.path.join(dst_root, REPORTS_REL))


SANDBOX = {'root': None}


def _without(cases, predicate):
    """删掉一批用例；**一条都没删掉就是 HARNESS 级失败** —— 变异没打到分支与探测器不存在
    在报告里长得一模一样，所以这里必须响。"""
    kept = [c for c in cases if not predicate(c)]
    if len(kept) == len(cases):
        raise HarnessError('变异一条都没删掉（谓词没命中任何用例）')
    return kept


def selftest():
    """每个探测器至少一个会失败的样本；外加一个必须 0 报错的干净样本。"""
    SANDBOX['root'] = tempfile.mkdtemp(prefix='platform-text-parity-selftest-')
    print('sandbox: %s' % SANDBOX['root'])
    ok = True

    # ① 干净对照：就地生成的夹具必须 0 报错（防误报）。期望的条数是**数出来的**，不是
    #    写死的：写死的计数会在报告数/规范行数变化时静默漂成假绿（记忆里 `control=2` 那坑）。
    base = fixture_payload(build_cases(ROOT))
    want = {'cases': len(base['cases']), 'edge': len(EDGE_CASES),
            'report': len(base['cases']) - len(EDGE_CASES),
            'compared': len(base['cases'])}
    ok &= sample('clean-control', (), want=want)

    # ② PARITY-DRIFT：把一条用例的 text 改掉（判据的**核心**）
    def drift(data):
        data['cases'][0]['text'] = data['cases'][0]['text'] + 'X'
        return data

    ok &= sample('drift-text-mutated', 'PARITY-DRIFT', mutate=drift, marker='夹具 text')

    # ②b PARITY-DRIFT 的另一条路：把 value 换成**上游会拒绝**的数（Python 侧抛异常）
    #     也是 drift，只是走的 `except` 分支 —— 一条样本盖不住两个分支。
    def drift_raises(data):
        for case in data['cases']:
            if case['unit'] == 'count':
                case['value'] = '1.5'
                return data
        raise HarnessError('没找到 count 用例')

    ok &= sample('drift-python-raises', 'PARITY-DRIFT', mutate=drift_raises,
                 marker='Python 侧**抛了**')

    # ③ PARITY-STALE-CASES：把一条用例的 **value** 改掉而 text 保持自洽
    #    （这条是防单向判据的：只查 text 的判据看不见这种漂移）
    def stale(data):
        for case in data['cases']:
            if case['source'] == 'edge' and case['unit'] == 'ratio' and case['digits'] == 4:
                case['value'] = '1.2345'
                case['text'] = '1.2345'
                return data
        raise HarnessError('没找到可用的 ratio/4 边角用例')

    ok &= sample('stale-value-changed', 'PARITY-STALE-CASES', mutate=stale)

    # ④ PARITY-NO-CASES / PARITY-THIN-CASES：空转守卫
    ok &= sample('no-cases', 'PARITY-NO-CASES', mutate=lambda d: dict(d, cases=[]))
    ok &= sample('thin-cases', 'PARITY-THIN-CASES',
                 mutate=lambda d: dict(d, cases=d['cases'][:3]))

    # ⑤ PARITY-NO-FIXTURE：文件不在 / 内容不是 JSON / schema 不对 / 没有 cases / cases 不是数组
    ok &= sample('no-fixture', 'PARITY-NO-FIXTURE', absent=True)
    ok &= sample('broken-json', 'PARITY-NO-FIXTURE', raw='{oops')
    ok &= sample('wrong-schema', 'PARITY-NO-FIXTURE',
                 mutate=lambda d: dict(d, schema='quanauto.something-else/9'))
    ok &= sample('no-cases-key', 'PARITY-NO-FIXTURE',
                 mutate=lambda d: {k: v for k, v in d.items() if k != 'cases'})
    ok &= sample('cases-not-a-list', 'PARITY-NO-FIXTURE',
                 mutate=lambda d: dict(d, cases={'nope': 1}))

    # ⑥ PARITY-CASE-SHAPE：五类形状坏法各一条
    def shape(change):
        def apply(data):
            change(data['cases'][0])
            return data
        return apply

    ok &= sample('shape-missing-field', 'PARITY-CASE-SHAPE',
                 mutate=shape(lambda c: c.pop('text')))
    ok &= sample('shape-bad-digits', 'PARITY-CASE-SHAPE',
                 mutate=shape(lambda c: c.__setitem__('digits', '4')))
    ok &= sample('shape-bad-unit', 'PARITY-CASE-SHAPE',
                 mutate=shape(lambda c: c.__setitem__('unit', 'yuan')))
    ok &= sample('shape-bad-value', 'PARITY-CASE-SHAPE',
                 mutate=shape(lambda c: c.__setitem__('value', 'abc')),
                 marker='不是能解析的十进制串')
    ok &= sample('shape-infinite-value', 'PARITY-CASE-SHAPE',
                 mutate=shape(lambda c: c.__setitem__('value', '1e999')),
                 marker='不是能解析的十进制串')

    # ⑦ PARITY-SPEC-COVERAGE：删掉某个 key 的全部 report-* 用例
    ok &= sample('spec-coverage-missing',
                 'PARITY-SPEC-COVERAGE',
                 mutate=lambda d: dict(d, cases=_without(
                     d['cases'], lambda c: c['key'] == 'total_commission')),
                 marker='total_commission')
    # ⑧ PARITY-UNITDIGITS-COVERAGE：删掉 count/0 组合的全部用例
    ok &= sample('unit-digits-coverage-missing',
                 'PARITY-UNITDIGITS-COVERAGE',
                 mutate=lambda d: dict(d, cases=_without(
                     d['cases'], lambda c: c['unit'] == 'count')),
                 marker='count')
    # ⑨ PARITY-SPEC-MISMATCH：把某条 report-* 用例的 digits 改掉（text 也跟着改 ⇒
    #    只有 SPEC-MISMATCH 能抓）；以及用一个规范里没有的 key
    def spec_mismatch(data):
        for case in data['cases']:
            if case['key'] == 'total_return':
                case['digits'] = 2
                case['text'] = '%.2f' % (float(case['value']) * 100.0) + '%'
                return data
        raise HarnessError('没找到 total_return 的用例')

    def spec_unknown_key(data):
        for case in data['cases']:
            if case['key'] == 'total_return':
                case['key'] = 'no_such_metric'
                return data
        raise HarnessError('没找到 total_return 的用例')

    ok &= sample('spec-mismatch-digits', 'PARITY-SPEC-MISMATCH', mutate=spec_mismatch)
    ok &= sample('spec-mismatch-unknown-key', 'PARITY-SPEC-MISMATCH',
                 mutate=spec_unknown_key, marker='METRIC_SPECS 里没有它')

    # ⑩ PARITY-EDGE-MISSING：删掉一条**边角**用例（它没有 key 覆盖可依赖）
    ok &= sample('edge-missing', 'PARITY-EDGE-MISSING',
                 mutate=lambda d: dict(d, cases=_without(
                     d['cases'], lambda c: c['key'] == 'edge:tie-ratio2-2675')),
                 marker='tie-ratio2-2675')

    # ⑪ 下面这几条**改的是模块级清单本身**（不是夹具），所以它们对着一个**干净沙箱**
    #    跑：夹具就地生成 ⇒ 基线是 0 报错，于是任何多出来的码都必然是这次变异造成的。
    #    （若对着真夹具跑，夹具自己的陈年问题会冒充「自测失败」。）
    reg_root = os.path.join(SANDBOX['root'], 'registry-samples')
    copy_reports(ROOT, reg_root)
    write_text(os.path.join(reg_root, FIXTURE_REL),
               json.dumps(fixture_payload(build_cases(ROOT)), ensure_ascii=False,
                          indent=2) + '\n')
    ok &= _expect_codes('registry-baseline', (), _audit_codes(reg_root))

    # ⑫ PARITY-NO-PY：参照物缺席（提前 return，所以连夹具都不用摆）
    ok &= _sample_no_python_reference(reg_root)

    # ⑬ PARITY-EDGE-THIN / PARITY-EDGE-DUPLICATE：声明清单自己被削 / 被塞重复项
    saved_cases, saved_unmatchable = EDGE_CASES, UNMATCHABLE
    try:
        globals()['EDGE_CASES'] = EDGE_CASES[:3]
        ok &= _expect_codes('edge-thin', ('PARITY-EDGE-THIN', 'PARITY-STALE-CASES'),
                            _audit_codes(reg_root))

        # 注意这里用的是 saved_cases（上一条样本刚把模块级 EDGE_CASES 削到 3 条）——
        # 直接写 `EDGE_CASES + (EDGE_CASES[0],)` 会构出 4 条，于是 EDGE-THIN 也一起响，
        # 「变异打偏」与「判据多报」在只断言包含时长得一模一样。
        globals()['EDGE_CASES'] = saved_cases + (saved_cases[0],)
        ok &= _expect_codes('edge-duplicate',
                            ('PARITY-EDGE-DUPLICATE', 'PARITY-STALE-CASES'),
                            _audit_codes(reg_root))

        # ⑭ PARITY-UNMATCHABLE-OK：把一条**可对拍**的用例塞进排除表（理由不成立）
        globals()['EDGE_CASES'] = saved_cases
        globals()['UNMATCHABLE'] = (('money', 2, '182.745', '假的排除理由'),)
        ok &= _expect_codes('unmatchable-reason-broken', ('PARITY-UNMATCHABLE-OK',),
                            _audit_codes(reg_root))

        # ⑭b 反向：真登记的排除项**必须不报**这条 —— 否则这条判据会常红，而常红的判据
        #     最后一定会被人用「放宽」的方式关掉。
        globals()['UNMATCHABLE'] = saved_unmatchable
        ok &= _expect_codes('unmatchable-reason-live', (), _audit_codes(reg_root))
    finally:
        globals()['EDGE_CASES'] = saved_cases
        globals()['UNMATCHABLE'] = saved_unmatchable

    # ⑮ 顺带在真产物上跑一遍（**advisory**：只印结论，不参与自测裁决）。
    #    「夹具现在绿不绿」由不带 --selftest 的那次运行负责判。
    print('  -- 真产物现状（advisory，本行不决定自测结论）--')
    sample('real-fixture-now', (), root=ROOT, advisory=True)

    print('note: 真夹具的 PASS/FAIL 由不带 --selftest 的那次运行负责判（见统一入口的 real 列）')
    print('SELFTEST %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


def _audit_codes(root):
    _summary, findings = audit(root)
    return tuple(sorted({code for code, _ in findings}))


def _expect_codes(name, want, got):
    """`want` / `got` 都是「必须**恰好**等于」的码集合（不是「包含」）。

    用**恰好**而不是包含：多报一个码往往正是「变异打偏了、报的是另一个检查码」的签名，
    而那个签名与「探测器不存在」在只看包含时长得一模一样。
    """
    if tuple(sorted(want)) != tuple(sorted(got)):
        print('  SAMPLE FAIL: %s 期望 %s，实际 %s' % (name, sorted(want), sorted(got)))
        return False
    print('  sample ok: %s (expect=%s)' % (name, '/'.join(sorted(want)) or 'clean'))
    return True


def _sample_no_python_reference(root):
    """参照物缺席。`sys.modules['quanauto'] = None` 会让 `import quanauto.*` 抛
    ImportError，且与 `sys.path` / 是否 editable 安装无关 —— 比改 `sys.path` 可靠。

    `PARITY-NO-PY` 是**唯一**能在「参照物缺席」时拒绝通过的那条守卫，它必须被真的触发过
    一次：否则「导不进来」与「一切正常」在报告里长得一模一样。
    """
    saved = {name: sys.modules.get(name, _ABSENT)
             for name in ('quanauto', 'quanauto.dashboard')}
    sys.modules['quanauto'] = None
    try:
        summary, findings = audit(root)
    finally:
        for name, module in saved.items():
            if module is _ABSENT:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    codes = tuple(sorted({code for code, _ in findings}))
    ok = _expect_codes('no-python-reference', ('PARITY-NO-PY',), codes)
    if summary['compared'] != 0:
        print('  SAMPLE FAIL: no-python-reference 的 compared 应为 0（没参照物就不该现算），'
              '实际 %r' % summary['compared'])
        return False
    return ok


class _Absent:
    pass


_ABSENT = _Absent()


def main(argv):
    harden_stdout()
    if '--selftest' in argv:
        return selftest()
    root = None
    write = False
    for arg in argv:
        if arg == '--write':
            write = True
        elif arg.startswith('--root='):
            root = arg.split('=', 1)[1]
        elif not arg.startswith('--'):
            root = arg
    root = os.path.abspath(root or ROOT)
    if write:
        return write_fixture(root)
    summary, findings = audit(root)
    return report(summary, findings)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
