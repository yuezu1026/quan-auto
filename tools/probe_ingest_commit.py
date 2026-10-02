# -*- coding: utf-8 -*-
"""probe_ingest_commit.py -- 三条落库写入路径 + 编排层在容器 PostgreSQL 上真跑一次，并用**新连接**读回。

为什么需要它
------------
DC 契约附录 J 的 J.1 有几行自称「可离线关」，而它们在 2026-10-02 时都还没关：

  * **J-6「真的入过库」** —— B18 那次「520 行入库 / 幂等重跑 `skipped=520`」全绿，但 B20.5 的
    重演证明那是**同一条连接自读**（恒真），另起连接数到 **0** 行。也就是说「测过的那个结论
    比它写的少」。B20.5 只证了**缺陷**（`autocommit=False` 下真的丢数据），**没有**证修法
    （`autocommit=True`）在真库上成立 —— 缺的正是那条**对照组**。
  * **J-8「`ROUND_HALF_UP` 与 PG `numeric` 在半值上同向」** —— `quanauto/pgstore.py::_to_decimal`
    的注释照**文档语义**写了「与 PostgreSQL `numeric` 的四舍五入同义」，而 `.00005` 这类
    半值样本**一个都没有**。文档语义不是实测出来的结论。
  * **J-11「编排层只在假连接上跑过」**（2026-10-02 追加，D 段）—— `quanauto/ingest.py` 那条
    串（取数 → `validate_frame` → 盖 `source`/`data_version` → 落库 → 写 `dc_ingest_run`）
    此前**只有** `tests/test_ingest.py` 的假连接用例跑过：真库上的 `dc_ingest_run` 一行、
    那两条 `ck_dc_ingest_status` / `ck_dc_ingest_priority` 一次都没被这条路径执行过。
    `pgstore` 的写入路径有自己的真库证据（B 段），**不等于**编排层有 —— 「下层的每个零件
    都测过」与「这一串接起来跑过」在本项目一直是两件事（B20 那次的三个真缺陷全在缝上）。
    ⇒ **2026-10-02（R29）** D 段把这一半补上了，见下。

本探针把这三条都变成有证据的结论，分四段：

  **A 段（有牙的对照）** 故意把连接降回 `autocommit=False`，跑**两次**：
      A1 没有前置裸读（进 `transaction()` 时连接是 IDLE）；
      A2 照 B18 的调用次序**先裸读一次**（进 `transaction()` 时连接已在 INTRANS）。
      A2 必须让新连接看到 **0** 行。若 A2 看到非 0，说明本探针**分辨不出**那条缺陷 ⇒
      B 段的绿毫无意义 ⇒ 直接 FAIL（`PROBE-TEETH-MISSING`）。

  **B 段（真跑）** 用**没有改过**的 `PsycopgConnection`（`autocommit=True`）跑三条写入路径
      （日线 / 复权因子 / 分红），每一条都：① 由容器里的 `psql`（**另一个进程、另一个会话**）
      数行；② 由**另一条 psycopg 连接**走 Store 读回并逐字段对拍；③ 幂等重跑必须
      `inserted=0`；④ 异值冲突必须抛错**且整批回滚**（前面已写入的行一起消失）。

  **C 段（J-8）** 半值样本同时喂给 `_as_decimal` / `_as_factor_decimal` 与
      `SELECT (x::numeric)::numeric(18,s)`，逐例对拍；半值再真的写进列、由新连接读回。

  **D 段（J-11）** 调**产品调用点**（`quanauto/ingest.py` 的 `ingest_daily_bars` /
      `ingest_adjust_factors` / `ingest_dividends`），而不是 B 段那层的 `pgstore` 写入路径。
      D0 是同一形状的**对照组**（`autocommit=False` + 前置裸读 ⇒ 内存里 `SUCCESS`、库里 0 行）；
      D1~D4 覆盖三条通道各一次、幂等重跑、异值冲突（整批回滚）、质检拒绝（一行不落库）；
      D5 覆盖 `PARTIAL`；D6 覆盖「前置检查失败**不留批次行**」。每一条都拿容器里的 `psql`
      （另一个进程、另一个会话）读 `dc_ingest_run` 的**那一行**逐字段对拍。

为什么**不**复用 `tools/run_sql_smoke.py` 那条 compose 通道
----------------------------------------------------------
它把 psql 全部走 `docker compose exec` 的 unix socket，**不对宿主发布端口**；而本探针必须在
**宿主**上用 psycopg 连库 —— `quanauto/pgstore.py` 的驱动只能在宿主跑（`tools/` 下没有任何
脚本 import psycopg，容器里也没有本仓库的 Python 环境）。所以本探针自带一个 `docker run`
的临时容器，端口**只绑 `127.0.0.1`**，跑完 `docker rm -f`。

为什么**不**注册进门禁
----------------------
与 `tools/run_sql_smoke.py` 同一条理由：`tools/run_all_gates.py` 必须能在没有 docker 的环境
里跑完（本仓库的现状就是如此）。本脚本在 docker / 镜像 / 宿主 psycopg 缺一不可时报
`NO ENV` 并退 **3**，**绝不算绿**。它是一支探针，不是判据 —— 与 B12/B14~B20 同级：
结论**会**因为改了下面 `PINNED_FILES` 里**任何一个文件**而作废（本报告头部记着那五个文件
的 sha256，重跑一次即可自己发现有没有失效）。

退出码
------
    0  A/B/C/D 四段全部按预期
    1  有 FAIL（发现了真问题，或者本探针自己分辨不出缺陷）
    2  前置守卫失败或**无法判定**（报告落点 / 空库守卫 / 容器起不来 / 输出解析不出数字）
    3  NO ENV：docker 不可用 / 镜像不在本机 / 宿主没装 psycopg —— 什么都没执行

用法
----
    python tools/probe_ingest_commit.py
    python tools/probe_ingest_commit.py --selftest        # 只测纯函数，不需要 docker
    python tools/probe_ingest_commit.py --pg-image=postgres:16
    python tools/probe_ingest_commit.py --keep            # 失败也留容器，便于人工排查
    python tools/probe_ingest_commit.py --report=tools/xxx.txt
"""

import hashlib
import os
import re
import socket
import subprocess
import sys
import time
from datetime import date
from decimal import Decimal

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
for _p in (ROOT, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用 `run_sql_smoke.py` 里**同一条**规则，不抄第二份实现。
# 本项目已经踩过「抄副本漂移 ⇒ 6 个样本全红、看起来像探测器坏了」那一次。
# `run_sql_smoke` 在导入期不执行任何东西（有 `if __name__ == '__main__':` 守卫）。
from run_sql_smoke import Log, harden_stdout, resolve_report_path  # noqa: E402

DEFAULT_IMAGE = 'postgres:17'
DEFAULT_REPORT_PATH = os.path.join(ROOT, 'tools', 'ingest-commit-probe-report.txt')
REPORT_PATH = DEFAULT_REPORT_PATH

CONTAINER = 'quan-ingest-probe'
DB_NAME = 'quan'
DB_USER = 'postgres'
# 本地一次性容器**仅**用：端口只绑 127.0.0.1，容器跑完即 rm -f，不写进任何配置。
DB_PASSWORD = 'quan_probe_local_only'

SYMBOL = '600000.SH'
VERSION = 'probe-r24'
START = date(2026, 3, 1)
END = date(2026, 3, 31)
DIV_START = date(2026, 1, 1)
DIV_END = date(2026, 12, 31)

# 本报告的结论**只对这几个文件的这些字节**成立；它们一改，本报告即作废，重跑。
# D 段（编排层）把后两个也牵进来了：它的结论同时依赖「适配器怎么归一化/校验」与
# 「编排层怎么留痕」，所以这两个文件与 DDL 同级 —— 改了任何一个，D 段的话都要重测。
PINNED_FILES = (
    'db/data_center.sql',
    'quanauto/pgstore.py',
    'quanauto/datacenter.py',
    'quanauto/ingest.py',
    'quanauto/datasources.py',
)

TABLES = ('dc_daily_bar', 'dc_adjust_factor', 'dc_dividend')
#: 批次日志表。D 段要清它，且要**单独**能数它（「前置失败不留痕」判的就是它的行数不变）。
RUN_TABLE = 'dc_ingest_run'

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_GUARD = 2
EXIT_NO_ENV = 3

# ---------------------------------------------------------------------------
# 样本
#
# 全用**字面量字符串**而不是 float（除了一条故意留着的真实 float 尾巴）。
# 半值（第 5 / 第 9 位小数是 5 且后面全 0）是 J-8 的核心样本：十进制平局落哪一边，
# Python 的 `ROUND_HALF_UP` 与 PG `numeric` 的「四舍五入」最容易在这里分家。
# ---------------------------------------------------------------------------

VALUE_SAMPLES = (
    # (标签, 字面量)  —— 走 `_as_decimal`，标度 4
    ('tie-up', '1.00005'),
    ('tie-up-small', '0.00005'),
    ('just-below-tie', '1.000049'),
    ('tie-up-large', '842270399.00005'),
    ('plain-4dp', '10.2500'),
    ('float-tail', '842270399.9999999'),
)

VALUE_SAMPLES_NEG = (
    ('neg-tie', '-1.00005'),
    ('neg-tie-small', '-0.00005'),
    ('neg-just-below-tie', '-1.000049'),
)

FACTOR_SAMPLES = (
    ('factor-tie', '1.000000005'),
    ('factor-plain', '1.00000000'),
)
FACTOR_SAMPLES_NEG = (
    ('factor-neg-tie', '-1.000000005'),
)


def daily_rows():
    """三行日线，各带一个不同的「会考到舍入」的值。

    * `2026-03-02` 普通四位数小数值
    * `2026-03-03` `close` 是**半值**（4 位标度上的平局）⇒ `_as_decimal` 应给 `1.0001`
    * `2026-03-04` `close` 是**真实 float 尾巴** `842270399.9999999`
      （`quanauto/pgstore.py::_to_decimal` 的文档串里点名的那个值）⇒ 应给 `842270400.0000`
    """
    from quanauto.datacenter import DailyBar

    def bar(day, o, h, low, c, vol, amt):
        return DailyBar(
            symbol=SYMBOL, trade_date=day,
            open=o, high=h, low=low, close=c,
            volume=vol, amount=amt,
            source='probe', data_version=VERSION,
        )

    return [
        bar(date(2026, 3, 2), Decimal('10.1250'), Decimal('10.5000'),
            Decimal('10.0000'), Decimal('10.2500'),
            Decimal('1000000.0000'), Decimal('10250000.0000')),
        bar(date(2026, 3, 3), Decimal('1.0000'), Decimal('1.0002'),
            Decimal('1.0000'), Decimal('1.00005'),
            Decimal('2000000.0000'), Decimal('2000400.0000')),
        bar(date(2026, 3, 4), Decimal('842270399.9999999'), Decimal('842270400.0000001'),
            Decimal('842270399.9999999'), 842270399.9999999,
            Decimal('3000000.0000'), Decimal('2526811200.0000')),
    ]


def factor_rows():
    from quanauto.datacenter import AdjustFactorPoint
    return [
        AdjustFactorPoint(symbol=SYMBOL, trade_date=date(2026, 3, 2),
                          adjust_factor=Decimal('1.00000000'),
                          source='probe', data_version=VERSION),
        AdjustFactorPoint(symbol=SYMBOL, trade_date=date(2026, 3, 3),
                          adjust_factor=Decimal('1.000000005'),
                          source='probe', data_version=VERSION),
    ]


def dividend_rows():
    from quanauto.datacenter import DividendPoint
    return [
        DividendPoint(symbol=SYMBOL, ex_date=date(2026, 6, 10),
                      announce_date=date(2026, 6, 3),
                      cash_per_share=Decimal('0.3000'),
                      source='probe', data_version=VERSION),
        DividendPoint(symbol=SYMBOL, ex_date=date(2026, 9, 10),
                      announce_date=date(2026, 9, 3),
                      cash_per_share=Decimal('0.00005'),
                      source='probe', data_version=VERSION),
    ]


# ---------------------------------------------------------------------------
# D 段（J-11）的样本
#
# 与 A/B/C 三段的根本差别是**被测层**：B 段调 `pgstore` 的三条写入路径，D 段调产品
# 调用点 `quanauto/ingest.py` 的三个公开函数。后者把「取数 → `validate_frame` →
# 盖 `source`/`data_version` → 落库 → 写 `dc_ingest_run`」串成一条，B 段一行都没覆盖。
#
# 取数侧**不联网**：`_AdapterBase.__init__(fetch=…)` 就是为这件事留的口子（它的类注释
# 写着「注入而不是在方法体里写死网络调用，是这一层能被离线测试的唯一原因」），所以这里
# 用**真的** `TencentAdapter` / `TushareAdapter` 配**注入**的取数函数回**源形状**的帧
# —— 归一化、校验、盖章、落库、留痕全部走产品代码，只有「网」是假的。
# ⚠️ 这条通道与 §21.4 的禁令不冲突：**真源取数**天然不可复现，本探针要证的从来不是它。
# ---------------------------------------------------------------------------

ORCH_SYMBOL = '600000.SH'
#: 第二个标的，只出现在 D5（「要了两个、只回来一个 ⇒ PARTIAL」那一条）。
ORCH_OTHER = '000001.SZ'
ORCH_START = date(2026, 3, 2)
ORCH_END = date(2026, 3, 4)
#: D1c（异值冲突）要给新那一行留出日子，否则它落在窗口外就与冲突无关了。
ORCH_END2 = date(2026, 3, 5)
ORCH_VERSION = 'probe-r29'

#: 腾讯那条通道的原始帧是**位置数组、没有列名** ⇒ 列名就是下标字符串
#: （`tencent/_default_fetch` 就是这么建帧的：`pd.DataFrame(rows, columns=[str(i)…])`）。
TENCENT_COLUMNS = tuple(str(index) for index in range(9))

#: 三行**源形状**的原始行。位置含义见腾讯适配器的 `_default_fetch` 里的 `column_map`：
#: 0 日期 / 1 开 / 2 **收** / 3 高 / 4 低 / 5 量（手）/ 8 额（万元）；6、7 不在映射表里
#: ⇒ 原样留着、不参与归一化。列名与位置写错的话下面每一条判据都会红，这是故意的。
TENCENT_BASE_ROWS = (
    ('2026-03-02', '10.1000', '10.2500', '10.3000', '10.0000', '10000', '{}', '1.50', '102.5'),
    ('2026-03-03', '10.2500', '10.1000', '10.4000', '10.0500', '20000', '{}', '-1.50', '202.0'),
    ('2026-03-04', '10.1000', '10.2000', '10.2200', '10.0900', '30000', '{}', '1.00', '306.0'),
)


def tencent_fetch(rows=None, only=None):
    """给 `TencentAdapter` 用的取数函数：回**源形状**的帧（位置数组、无列名）。

    `rows=None` ⇒ 用 `TENCENT_BASE_ROWS`（三行）；`only=None` ⇒ 每个标的都回同一批行，
    否则不在 `only` 里的标回**空帧**（空帧是 D5「这个标的没取到」的输入；「空帧本身
    过不了校验」是另一条路，由 D4 单独覆盖 —— 两者不是一件事）。
    """

    def fetch(symbol='', **kwargs):   # noqa: ARG001 - `_call` 会把 symbol/start/end 都传进来
        import pandas as pd

        body = [list(row) for row in (TENCENT_BASE_ROWS if rows is None else rows)]
        if only is not None:
            from quanauto.datasources import normalize_symbol

            if normalize_symbol(symbol) not in only:
                return pd.DataFrame(columns=list(TENCENT_COLUMNS))
        return pd.DataFrame(body, columns=list(TENCENT_COLUMNS))

    return fetch


def tushare_factor_fetch():
    """给 `TushareAdapter` 用的 `adj_factor` 取数函数（源形状：表内源列名）。"""

    def fetch(**kwargs):   # noqa: ARG001
        import pandas as pd

        return pd.DataFrame({
            'ts_code': [ORCH_SYMBOL, ORCH_SYMBOL],
            'trade_date': ['20260302', '20260303'],
            'adj_factor': ['1.00000000', '1.23456789'],
        })

    return fetch


def tushare_dividend_fetch():
    """给 `TushareAdapter` 用的 `dividend` 取数函数。

    用的列是 `cash_div_tax`（**税前**）而不是 `cash_div`（税后）—— 契约要的是前者，
    换错列不会报错、只会写进另一个数（所以 D3 对拍的是 `0.3000` / `0.00005`）。
    `ex_date` 必须落在调用窗口里（`ingest_dividends` 的窗口比的是除权除息日），
    `ann_date <= ex_date`（否则 DDL 那条 `ck_dc_dividend_announce` 会拦）。
    """

    def fetch(**kwargs):   # noqa: ARG001
        import pandas as pd

        return pd.DataFrame({
            'ts_code': [ORCH_SYMBOL, ORCH_SYMBOL],
            'ex_date': ['20260610', '20260910'],
            'ann_date': ['20260603', '20260903'],
            'cash_div_tax': ['0.3000', '0.00005'],
        })

    return fetch


# ---------------------------------------------------------------------------
# 纯函数（所以能自测）
# ---------------------------------------------------------------------------

def as_int(text):
    """把 psql 的标量输出读成 int；读不出来返回 `None`。

    **绝不允许**把读不出来折算成 0：那会把「解析失败」与「数到 0 行」这两个完全不同的
    结论合成一个数字，而这支探针有一处的**正确**预期恰好就是 0（A2 段）⇒ 合成之后
    「探针有牙」与「探针坏了」在报告里长得一模一样。
    """
    if text is None:
        return None
    s = str(text).strip()
    if not re.fullmatch(r'-?\d+', s):
        return None
    return int(s)


def teeth_ok(new_client_rows):
    """A2 段的唯一用途 = 新连接看到 **0** 行。

    看到非 0 ⇒ 本探针分辨不出「没提交」这条缺陷 ⇒ B 段的绿没有意义。
    解析失败（`None`）同样必须判 False：那说明这条判据根本没跑起来。
    """
    return new_client_rows == 0


def compare_rounding(cases, py_round, pg_round):
    """逐例对拍两侧的舍入。`py_round(literal)` / `pg_round(literal)` 都是**注入**的。

    返回 `(rows, n_mismatch)`，`rows` 每项是 `(label, literal, py, pg, same)`。
    注入是为了能自测：本函数不碰数据库、不 import 任何东西。
    """
    rows = []
    bad = 0
    for label, literal in cases:
        py = py_round(literal)
        pg = pg_round(literal)
        same = (py is not None) and (pg is not None) and (Decimal(py) == Decimal(pg))
        if not same:
            bad += 1
        rows.append((label, literal, py, pg, same))
    return rows, bad


def quote_literal(literal):
    """把一个字面量包成 SQL 字符串常量。样本里没有单引号，但**不假设**这一点。"""
    return "'" + str(literal).replace("'", "''") + "'"


#: `dc_ingest_run` 那一行**判哪些列**，以及读它的顺序。`error_message` 排在**最后**：
#: 它是唯一可能含任意文本的列（`_record_failure` 会把异常消息写进去），排最后才能用
#: `split(sep, 字段数-1)` 把多余的 `sep` 都留给它 —— 排在中间的话，消息里一个 `|`
#: 就会让字段整体错位，而错位看起来像「值不符」。
RUN_COLUMNS = ('adapter', 'priority', 'status', 'symbol_count', 'row_count',
               'start_date', 'end_date', 'data_version', 'finished_at', 'error_message')

#: 与 `RUN_COLUMNS` **一一对应、同序**。两处一错位，判据就整体错位 ⇒ 见 `run_row_of`。
RUN_SELECT = ("SELECT adapter, priority, status, symbol_count, row_count, "
              "start_date, end_date, data_version, coalesce(finished_at::text, ''), "
              "error_message FROM dc_ingest_run WHERE run_id = %s")


def split_row(text, count):
    """psql 的一行（`-tA -F '|'`）→ `count` 个字段；**形状不对返回 None**。

    返回 None 而不是「补齐 / 截断」：补齐会让「psql 没按我要的格式回」看起来像「字段值
    不符」，而这两件事要修的地方完全不同（前者改读法，后者改产物）。
    """
    if text is None:
        return None
    parts = str(text).split('|', count - 1)
    if len(parts) != count:
        return None
    return tuple(part.strip() for part in parts)


def _matches(actual, spec):
    """`spec` 要么是字符串（**精确相等**），要么是一个算子元组。

    算子只有两个，多一个都要在这里显式加 —— 写不出来的算子直接炸，**不静默判过**：
    `('nonempty',)` / `('contains', 子串)`。后两个是给「消息内容」用的，因为那几列
    的正确值由产品代码拼出来，钉死全文等于把探针与文案耦死（改个标点就要重跑四个镜像）。
    """
    if isinstance(spec, tuple):
        op = spec[0]
        if op == 'nonempty':
            return actual != ''
        if op == 'contains':
            return spec[1] in actual
        raise AssertionError('未知的判据算子 %r（判据表与实现脱节，不是产物的问题）' % (op,))
    return actual == spec


def judge_run_row(cells, expect):
    """把**另一个客户端**读回来的那一行与期望逐字段比。

    返回 `(mismatches, seen)`：`mismatches` 每项 `(字段, 实际, 期望)`；`seen` 是读到的
    全部字段（进报告，让「没被判的那几列」也肉眼可见）。

    `cells is None`（读不出来 / 字段数不对）**必须产生一条不符** —— 否则「读不出来」会
    静默变成「都对」，正是本项目反复踩的那一类假绿。
    """
    if cells is None:
        return [('(整行)', '(读不出来 / 字段数不对)', '一行 %d 字段' % len(RUN_COLUMNS))], ()
    bad = []
    for index, name in enumerate(RUN_COLUMNS):
        spec = expect.get(name)
        if spec is None:
            continue
        actual = cells[index]
        if not _matches(actual, spec):
            bad.append((name, actual, spec))
    return bad, cells


def sha256_of(rel_path):
    path = os.path.join(ROOT, rel_path)
    try:
        with open(path, 'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return '(读不到)'


# ---------------------------------------------------------------------------
# 进程封装（自带 `docker run` 临时容器，不是 compose）
# ---------------------------------------------------------------------------

def run(argv, timeout=600, stdin_text=None):
    """返回 `(rc, 输出)`。stderr **合并**进 stdout（psql 的 NOTICE 走 stderr）。"""
    kw = dict(cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if stdin_text is None:
        kw['stdin'] = subprocess.DEVNULL
    try:
        p = subprocess.run(
            argv,
            input=(stdin_text.encode('utf-8') if stdin_text is not None else None),
            **kw
        )
    except FileNotFoundError:
        return 127, 'executable not found: %s' % argv[0]
    except subprocess.TimeoutExpired:
        return 124, 'TIMEOUT after %ds: %s' % (timeout, ' '.join(argv))
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def dexec(args, timeout=600, stdin_text=None):
    argv = ['docker', 'exec']
    if stdin_text is not None:
        argv.append('-i')
    return run(argv + [CONTAINER] + args, timeout=timeout, stdin_text=stdin_text)


def psql_scalar(sql, timeout=180):
    rc, out = dexec(['psql', '-v', 'ON_ERROR_STOP=1', '-U', DB_USER, '-d', DB_NAME,
                     '-tAc', sql], timeout=timeout)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return rc, (lines[-1] if lines else ''), out


def psql_expect(sql, timeout=180):
    """取一个标量并**当场**要求它是整数，否则返回 None（调用方必须判 FAIL/GUARD）。"""
    rc, val, out = psql_scalar(sql, timeout=timeout)
    if rc != 0:
        return None, out
    n = as_int(val)
    if n is None:
        return None, 'psql 的输出不是整数：%r' % val
    return n, out


def psql_stdin(text, timeout=900):
    return dexec(['psql', '-v', 'ON_ERROR_STOP=1', '-U', DB_USER, '-d', DB_NAME, '-f', '-'],
                 timeout=timeout, stdin_text=text)


def free_port(preferred, candidates):
    """找一个宿主上**真的**能 bind 的端口。docker 的 `-p` 绑定失败会让容器起不来，
    而「起不来」与「镜像没拉」在报告里长得一样 ⇒ 先挑好端口，把两类原因分开。"""
    for port in [preferred] + [p for p in candidates if p != preferred]:
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                continue
            return port
    return None


# ---------------------------------------------------------------------------
# 被测侧的小工具
# ---------------------------------------------------------------------------

def reset_tables(log):
    """把三张表清空。每一段开始前都清，否则上一段的残留会让本段的计数说不出话。"""
    rc, out = psql_stdin('TRUNCATE %s;' % ', '.join(TABLES), timeout=180)
    if rc != 0:
        log('GATE FAIL: TRUNCATE 失败。')
        log.indent(out.strip()[:600])
        return False
    return True


def count_of(table):
    n, err = psql_expect('SELECT count(*) FROM %s' % table)
    return n, err


NUMERIC_TEXT_RE = re.compile(r'-?\d+(\.\d+)?')


def no_autocommit_class():
    """复刻 B20 之前的行为：连上来的会话**不是** autocommit。

    为什么用**子类**而不是打补丁 / mock：`_connect()` 是 `PsycopgConnection` 里唯一决定
    autocommit 的地方，覆盖它一个方法就够，而且**不改被测代码**（没有补丁、没有 mock，
    也就没有「测的其实是自己被替换过的行为」这个问题）。
    """
    from quanauto import pgstore

    class _NoAutoCommitConnection(pgstore.PsycopgConnection):
        def _connect(self):
            if self._conn is None:
                psycopg = pgstore._import_psycopg()
                self._conn = psycopg.connect(self.dsn, row_factory=psycopg.rows.dict_row,
                                             autocommit=False)
            return self._conn

    return _NoAutoCommitConnection


# ---------------------------------------------------------------------------
# D 段的小工具：清四张表 + 读 `dc_ingest_run` 的那一行
# ---------------------------------------------------------------------------

def reset_orch(log):
    """清**四张**表（三张业务表 + 批次日志）。

    批次日志必须一起清：D 段有几条判据是「这次操作**不许**留下任何行」/「恰好多了 1 行」，
    拿上一段的残留去数这些，判据会变得说不清。
    `TRUNCATE a, b, c` 是单条语句，要么全成要么全不成 —— 与「清了两张、第三张没清」分得开。
    """
    rc, out = psql_stdin('TRUNCATE %s;' % ', '.join(TABLES + (RUN_TABLE,)), timeout=180)
    if rc != 0:
        log('GATE FAIL: TRUNCATE %s 失败。' % (RUN_TABLE,))
        log.indent(out.strip()[:600])
        return False
    return True


def run_row_of(run_id):
    """读 `dc_ingest_run` 里 `run_id` 那一行，返回 `(cells|None, note)`。

    走容器里的 `psql`（**另一个进程、另一个会话**）而不是本进程那条 psycopg 连接 ——
    这正是 J-6 那次假绿缺的东西：同一条连接自读是恒真的。
    """
    rc, val, out = psql_scalar(RUN_SELECT % int(run_id))
    if rc != 0:
        return None, 'psql rc=%d：%s' % (rc, out.strip()[:200])
    cells = split_row(val, len(RUN_COLUMNS))
    if cells is None:
        return None, ('psql 的输出切不出 %d 个字段（`RUN_SELECT` 与 `RUN_COLUMNS` 脱节？）：%r'
                      % (len(RUN_COLUMNS), val))
    return cells, ''


def last_run_id():
    """最新的 `run_id`。用于「调用抛异常、拿不到返回值」那几条（D1c / D4）。"""
    n, err = psql_expect('SELECT max(run_id) FROM %s' % RUN_TABLE)
    if n is None:
        return None, err
    if n <= 0:
        return None, '批次日志是空的（这次调用一行都没留下）'
    return n, ''


def show_run_row(log, tag, run_id, expect):
    """读 → 判 → 打。返回不符条数（读不出来**算 1 条**）。"""
    cells, note = run_row_of(run_id)
    if cells is None:
        log('      %s MISMATCH %s' % (tag, note))
        return 1
    bad, seen = judge_run_row(cells, expect)
    shown = ' | '.join('%s=%s' % (name, text[:60] if len(text) > 60 else text)
                       for name, text in zip(RUN_COLUMNS, seen))
    log('      %s 另一个客户端的读数：%s' % (tag, shown))
    for name, actual, spec in bad:
        if isinstance(spec, tuple):
            spec_text = '%s %r' % (spec[0], spec[1]) if len(spec) > 1 else spec[0]
        else:
            spec_text = repr(spec)
        log('          MISMATCH %s: 实际 %r / 期望 %s' % (name, actual, spec_text))
    if not bad:
        log('          OK（%d 个字段全部相符；未列的字段本次不判）' % len(expect))
    return len(bad)


def check_counts(log, tag, want):
    """逐表数行并与期望比。返回不符条数。

    `want` 里没写的表**默认期望 0**（不是「跳过」）：漏写一张表就等于那张表没人看，
    而「没人看」在报告里与「看过了、恰好是 0」长得一样。
    """
    bad = 0
    for table in TABLES:
        got, err = count_of(table)
        if got is None:
            log('      %s GATE FAIL: 数不了 %s 的行数：%s' % (tag, table, err))
            bad += 1
            continue
        expected = want.get(table, 0)
        if got != expected:
            log('      %s MISMATCH %s = %d 行（期望 %d）' % (tag, table, got, expected))
            bad += 1
    return bad


# ---------------------------------------------------------------------------
# A 段：把连接降回 autocommit=False，跑两次，第二次必须被新连接看见「0 行」
# ---------------------------------------------------------------------------

def section_a(log, dsn):
    """返回 `(verdict, exit_code)`，verdict ∈ {OK, FAIL, GUARD_FAIL}。"""
    from quanauto import pgstore

    NoAutoCommit = no_autocommit_class()
    rows = daily_rows()
    want = len(rows)

    log('')
    log('--- A 段：把连接降回 autocommit=False（复刻 B20 之前的驱动语义）---')
    log('  A1 = 进 `transaction()` 之前**没有**裸读（那一刻连接是 IDLE）')
    log('  A2 = 照 B18 的调用次序**先裸读一次**（那一刻连接已在 INTRANS）')
    log('  只有 A2 是「有牙」的那一半：它必须让新连接看到 0 行。')

    observed = {}
    for tag, pre_read in (('A1', False), ('A2', True)):
        if not reset_tables(log):
            return 'GUARD_FAIL', EXIT_GUARD

        conn = NoAutoCommit(dsn)
        try:
            if pre_read:
                # B18 的探针就是这个形状：「先看看库里有什么、再决定写什么」。
                before = conn.execute('SELECT count(*) AS n FROM dc_daily_bar')
                log('  %s 前置裸读：库里原有 %s 行' % (tag, before[0]['n']))
            report = pgstore.PgBarIngestor(conn).upsert_daily_bars(rows)
            same_conn = pgstore.PgBarStore(conn, VERSION).select_bars(SYMBOL, START, END)
        finally:
            conn.close()
        new_rows, err = count_of('dc_daily_bar')
        if new_rows is None:
            log('GATE FAIL: %s 段读不出行数：%s' % (tag, err))
            return 'GUARD_FAIL', EXIT_GUARD
        observed[tag] = (report.inserted, report.skipped, len(same_conn), new_rows)
        log('  %s upsert -> inserted=%d skipped=%d；**同一条连接**读回 %d 行；'
            '**新客户端**（容器里的 psql）数到 %d 行'
            % (tag, report.inserted, report.skipped, len(same_conn), new_rows))

    a2_new = observed['A2'][3]
    if not teeth_ok(a2_new):
        log('')
        log('PROBE-TEETH-MISSING: A2 段期望新客户端看到 0 行，实际看到 %d 行。' % a2_new)
        log('  含义：本探针**分辨不出**「没提交」这条缺陷 ⇒ B 段的绿证明不了任何东西，')
        log('        所以拒绝判定（不是「通过了」）。不要去调这个期望值 —— 先查为什么')
        log('        A2 的前置裸读没能把连接钉在 INTRANS。')
        # 退 **2**（GUARD）而不是 1（FAIL）：这是**本探针自己**失去了判定能力，
        # 不是「在产物里发现了问题」。两种红必须分开写 —— 合成一个退出码之后，
        # 「psycopg 换了语义」与「pgstore 真的丢数据」在报告里会长得一模一样。
        return 'GUARD_FAIL', EXIT_GUARD

    if observed['A1'][3] == 0:
        # A1 的预期是「真提交了」。它不成立时**不**判 FAIL（那是驱动语义的问题，不是本探针
        # 的判据），但必须响亮地说出来：它意味着「只差一次前置裸读」这个因果说法要重写。
        log('  NOTE: A1 段新客户端也只看到 0 行 —— 「只差一次前置裸读」这个因果说法')
        log('        需要重写（见 DC 契约 B20.1 与 J.6）。这不是本探针的 FAIL。')

    log('  A 段 OK：同连接读到 %d 行、新客户端读到 0 行 —— 这条读法的恒真性被实测钉住了。'
        % observed['A2'][2])
    return 'OK', EXIT_OK


# ---------------------------------------------------------------------------
# B 段：用没改过的 PsycopgConnection（autocommit=True）真跑三条写入路径
# ---------------------------------------------------------------------------

def section_b(log, dsn):
    from quanauto import pgstore
    from quanauto.errors import QuanAutoError

    bars = daily_rows()
    factors = factor_rows()
    dividends = dividend_rows()

    log('')
    log('--- B 段：真跑（`autocommit=True`，即 B20.3 的修法）---')
    if not reset_tables(log):
        return 'GUARD_FAIL', EXIT_GUARD

    conn = pgstore.PsycopgConnection(dsn)
    try:
        rep_bar = pgstore.PgBarIngestor(conn).upsert_daily_bars(bars)
        rep_fac = pgstore.PgFactorIngestor(conn).upsert_adjust_factors(factors)
        rep_div = pgstore.PgDividendIngestor(conn).upsert_dividends(dividends)
    finally:
        conn.close()
    log('  写入：日线 inserted=%d skipped=%d；因子 inserted=%d skipped=%d；'
        '分红 inserted=%d skipped=%d'
        % (rep_bar.inserted, rep_bar.skipped, rep_fac.inserted, rep_fac.skipped,
           rep_div.inserted, rep_div.skipped))

    want = {'dc_daily_bar': len(bars), 'dc_adjust_factor': len(factors),
            'dc_dividend': len(dividends)}
    bad = 0

    # B1：另**一个进程 + 另一个会话**（容器里的 psql）数行。
    log('  B1 另一个客户端数行（容器里的 psql，独立会话，不是同连接自读）：')
    seen = {}
    for table in TABLES:
        got, err = count_of(table)
        if got is None:
            log('GATE FAIL: 读 %s 的行数失败：%s' % (table, err))
            return 'GUARD_FAIL', EXIT_GUARD
        seen[table] = got
        flag = 'OK' if got == want[table] else 'MISMATCH'
        if got != want[table]:
            bad += 1
        log('      %-18s 库里 %d 行 / 期望 %d 行  %s' % (table, got, want[table], flag))

    # B2：另**一条 psycopg 连接**走 Store 读回，逐字段对拍。
    log('  B2 另一条 psycopg 连接走 Store 读回并逐字段对拍：')
    conn2 = pgstore.PsycopgConnection(dsn)
    try:
        back = {b.trade_date: b for b in
                pgstore.PgBarStore(conn2, VERSION).select_bars(SYMBOL, START, END)}
        for bar in bars:
            got = back.get(bar.trade_date)
            if got is None:
                log('      MISSING 日线 %s' % bar.trade_date)
                bad += 1
                continue
            for field in ('open', 'high', 'low', 'close', 'volume', 'amount'):
                # `_to_decimal` 就是**写进去的那个值**；读回必须等于它，而不是等于原始输入
                # —— 半值与那条 float 尾巴本来就会被舍入，拿原始输入当期望是错的期望。
                expect = pgstore._as_decimal(getattr(bar, field))
                if getattr(got, field) != expect:
                    log('      MISMATCH 日线 %s.%s 读回 %s / 期望 %s'
                        % (bar.trade_date, field, getattr(got, field), expect))
                    bad += 1
            log('      OK 日线 %s close 写 %s -> 读回 %s'
                % (bar.trade_date, bar.close, got.close))

        fac_back = {f.trade_date: f for f in
                    pgstore.PgFactorStore(conn2, VERSION).select_factors(SYMBOL, START, END)}
        for point in factors:
            got = fac_back.get(point.trade_date)
            if got is None:
                log('      MISSING 因子 %s' % point.trade_date)
                bad += 1
                continue
            expect = pgstore._as_factor_decimal(point.adjust_factor)
            if got.adjust_factor != expect:
                log('      MISMATCH 因子 %s 读回 %s / 期望 %s'
                    % (point.trade_date, got.adjust_factor, expect))
                bad += 1
            else:
                log('      OK 因子 %s 写 %s -> 读回 %s'
                    % (point.trade_date, point.adjust_factor, got.adjust_factor))

        div_back = {d.ex_date: d for d in
                    pgstore.PgDividendStore(conn2, VERSION).select_dividends(
                        SYMBOL, DIV_START, DIV_END)}
        for point in dividends:
            got = div_back.get(point.ex_date)
            if got is None:
                log('      MISSING 分红 %s' % point.ex_date)
                bad += 1
                continue
            expect = pgstore._as_dividend_decimal(point.cash_per_share)
            if got.cash_per_share != expect or got.announce_date != point.announce_date:
                log('      MISMATCH 分红 %s 读回 %s/%s / 期望 %s/%s'
                    % (point.ex_date, got.announce_date, got.cash_per_share,
                       point.announce_date, expect))
                bad += 1
            else:
                log('      OK 分红 %s（announce %s）写 %s -> 读回 %s'
                    % (point.ex_date, got.announce_date, point.cash_per_share,
                       got.cash_per_share))
    finally:
        conn2.close()

    # B3：幂等重跑 —— **新连接**，同样的行，必须 inserted=0 且行数不变。
    log('  B3 幂等重跑（另一条连接，同样的行）：')
    conn3 = pgstore.PsycopgConnection(dsn)
    try:
        again_bar = pgstore.PgBarIngestor(conn3).upsert_daily_bars(bars)
        again_fac = pgstore.PgFactorIngestor(conn3).upsert_adjust_factors(factors)
        again_div = pgstore.PgDividendIngestor(conn3).upsert_dividends(dividends)
    finally:
        conn3.close()
    for label, rep in (('日线', again_bar), ('因子', again_fac), ('分红', again_div)):
        ok = (rep.inserted == 0)
        if not ok:
            bad += 1
        log('      %s inserted=%d skipped=%d  %s'
            % (label, rep.inserted, rep.skipped, 'OK' if ok else 'MISMATCH'))
    for table in TABLES:
        got, err = count_of(table)
        if got is None:
            log('GATE FAIL: 读 %s 的行数失败：%s' % (table, err))
            return 'GUARD_FAIL', EXIT_GUARD
        if got != want[table]:
            log('      MISMATCH %s 重跑后变成 %d 行（期望 %d）' % (table, got, want[table]))
            bad += 1

    # B4：异值冲突 —— 必须抛错**且整批回滚**（前面已写入的新行一起消失）。
    log('  B4 异值冲突（整批一个事务，前一行也必须一起回滚）：')
    fresh = pgstore.DailyBar(
        symbol=SYMBOL, trade_date=date(2026, 3, 5),
        open=Decimal('5.0000'), high=Decimal('5.0000'), low=Decimal('5.0000'),
        close=Decimal('5.0000'), volume=Decimal('1000.0000'), amount=Decimal('5000.0000'),
        source='probe', data_version=VERSION)
    conflict = pgstore.DailyBar(
        symbol=bars[0].symbol, trade_date=bars[0].trade_date,
        open=bars[0].open, high=bars[0].high, low=bars[0].low,
        close=Decimal('99.9900'),  # 与库里那行不同 ⇒ D10 的异值分支
        volume=bars[0].volume, amount=bars[0].amount,
        source='probe', data_version=VERSION)
    conn4 = pgstore.PsycopgConnection(dsn)
    raised = None
    try:
        pgstore.PgBarIngestor(conn4).upsert_daily_bars([fresh, conflict])
    except QuanAutoError as exc:
        raised = exc
    except Exception as exc:                                    # noqa: BLE001
        raised = exc
        log('      NOTE: 抛出的不是本项目异常族：%r' % (exc,))
    finally:
        conn4.close()
    if raised is None:
        log('      MISMATCH 异值批次没有抛错')
        bad += 1
    else:
        want_name = 'IngestConflictError'
        hit = type(raised).__name__ == want_name
        if not hit:
            bad += 1
        log('      抛出 %s  %s' % (type(raised).__name__, 'OK' if hit else
                                   'MISMATCH（期望 %s）' % want_name))
    got, err = count_of('dc_daily_bar')
    if got is None:
        log('GATE FAIL: 读 dc_daily_bar 的行数失败：%s' % err)
        return 'GUARD_FAIL', EXIT_GUARD
    if got != want['dc_daily_bar']:
        log('      MISMATCH 冲突批次之后 dc_daily_bar 变成 %d 行（期望 %d）'
            '—— 事务没有整批回滚' % (got, want['dc_daily_bar']))
        bad += 1
    else:
        log('      OK 冲突批次之后 dc_daily_bar 仍是 %d 行 —— 那一行新数据也回滚了' % got)

    if bad:
        log('')
        log('B 段 FAIL：%d 项不符（明细见上）。' % bad)
        return 'FAIL', EXIT_FAIL
    log('  B 段 OK：三条写入路径都由**另一个客户端**读到了，且幂等重跑与冲突回滚都对。')
    return 'OK', EXIT_OK


# ---------------------------------------------------------------------------
# C 段：J-8 —— `ROUND_HALF_UP` 与 PG `numeric` 在半值上是否同向
# ---------------------------------------------------------------------------

def section_c(log, dsn):
    from quanauto import pgstore
    bad = 0

    log('')
    log('--- C 段：`ROUND_HALF_UP` 与 PG `numeric` 的舍入对拍（J-8）---')

    def pg_round(literal, scale):
        sql = 'SELECT ((%s::numeric)::numeric(18,%d))::text' % (quote_literal(literal), scale)
        rc, val, out = psql_scalar(sql)
        text = (val or '').strip()
        if rc != 0 or not NUMERIC_TEXT_RE.fullmatch(text):
            return None
        return text

    cases4 = VALUE_SAMPLES + VALUE_SAMPLES_NEG
    rows, n = compare_rounding(cases4, pgstore._as_decimal,
                               lambda lit: pg_round(lit, 4))
    log('  标度 4（`_as_decimal` vs `numeric(18,4)`），%d 例：' % len(rows))
    for label, literal, py, pg, same in rows:
        if not same:
            bad += 1
        log('      %-18s %-20s python=%-16s pg=%-16s %s'
            % (label, literal, py, pg, 'OK' if same else 'MISMATCH'))

    cases8 = FACTOR_SAMPLES + FACTOR_SAMPLES_NEG
    rows8, n8 = compare_rounding(cases8, pgstore._as_factor_decimal,
                                 lambda lit: pg_round(lit, 8))
    log('  标度 8（`_as_factor_decimal` vs `numeric(18,8)`），%d 例：' % len(rows8))
    for label, literal, py, pg, same in rows8:
        if not same:
            bad += 1
        log('      %-18s %-20s python=%-16s pg=%-16s %s'
            % (label, literal, py, pg, 'OK' if same else 'MISMATCH'))

    # 半值真的写进列、由另一个客户端读回：证明「两侧同向」不只是 `SELECT` 里的巧合。
    # 注意这条要**读列**而不是再算一次 SELECT —— 它检验的是「列里存下的那个十进制串」。
    log('  半值落列往返（B 段写进去的 `1.00005`，列里必须存 `_as_decimal` 的那个结果）：')
    written = pgstore._as_decimal('1.00005')
    rc, text, out = psql_scalar(
        'SELECT close::text FROM dc_daily_bar WHERE symbol = %s AND trade_date = %s '
        'AND data_version = %s'
        % (quote_literal(SYMBOL), quote_literal('2026-03-03'), quote_literal(VERSION)))
    text = (text or '').strip()
    if rc != 0 or not NUMERIC_TEXT_RE.fullmatch(text):
        log('      MISMATCH 读不回那一列（rc=%d）：%s' % (rc, out.strip()[:200]))
        bad += 1
    elif Decimal(text) != written:
        log('      MISMATCH 列里是 %s / `_as_decimal` 给的是 %s' % (text, written))
        bad += 1
    else:
        log("      OK 列里是 %s == `_as_decimal('1.00005')` = %s" % (text, written))

    if bad:
        log('')
        log('C 段 FAIL：%d 例两侧不同向。`quanauto/pgstore.py::_to_decimal` 的注释里那句')
        log('  「与 PostgreSQL `numeric` 的四舍五入同义」就要改写成实测出来的边界。')
        return 'FAIL', EXIT_FAIL
    log('  C 段 OK：所有半值样本两侧同向（`ROUND_HALF_UP` = 半数远离零 = PG numeric 的语义）。')
    return 'OK', EXIT_OK


# ---------------------------------------------------------------------------
# D 段：编排层（`quanauto/ingest.py`）在真库上真跑 —— J-11 的那一半
# ---------------------------------------------------------------------------

def caught(fn, want):
    """跑 `fn()`，返回 `(ok, actual, exc)`：`ok` = 抛出来的**正是** `want` 那个类名。

    不用 `pytest.raises` 那一套：本脚本不属于 `tests/`，而「抛了别的异常」与「没抛」
    必须能分开报（前者是产物的问题，后者往往更严重 —— 静默写进去了）。
    """
    try:
        fn()
    except Exception as exc:                                    # noqa: BLE001
        return type(exc).__name__ == want, type(exc).__name__, exc
    return False, '(没抛)', None


def section_d(log, dsn):
    """**编排层**在真库上真跑（J-11）。返回 `(verdict, exit_code)`。

    与 B 段的差别只有**被测层**：这里调的是产品调用点 `ingest_daily_bars` /
    `ingest_adjust_factors` / `ingest_dividends`，它们把「取数 → 校验 → 盖章 → 落库 →
    留痕」串成一条。每条通道都用**真的**适配器 + **注入的**取数函数（不联网、不需凭证）。
    """
    from quanauto import datasources, ingest, pgstore

    bad = 0
    log('')
    log('--- D 段：编排层（`quanauto/ingest.py`）在真库上真跑（J-11）---')
    log('  被测：三条公开通道 + `dc_ingest_run` 的开/收留痕。B 段那一层是 `pgstore` 的写入')
    log('        路径，**没有**覆盖这条串本身（「零件都测过」与「接起来跑过」是两件事）。')
    log('  取数：真的 `TencentAdapter` / `TushareAdapter` 配**注入**的取数函数回源形状的帧，')
    log('        归一化 / `validate_frame` / 盖 source·data_version / 落库 / 留痕全走产品代码。')
    log('        本次不证的也写在这里：**真实券源取过数**（那天然不可复现，见 §21.4）。')

    # ── D0 对照组 ─────────────────────────────────────────────────────────
    log('')
    log('  D0 对照组：`autocommit=False` + 前置裸读（= B18 的调用次序，A2 的编排层版本）')
    if not reset_orch(log):
        return 'GUARD_FAIL', EXIT_GUARD
    NoAutoCommit = no_autocommit_class()
    d0_exc = None
    d0_run = None
    conn = NoAutoCommit(dsn)
    try:
        # 先裸读一次 ⇒ 会话进 INTRANS ⇒ 后面所有语句都在这个（永不提交的）事务里。
        conn.execute('SELECT count(*) AS n FROM %s' % RUN_TABLE)
        d0_run = ingest.ingest_daily_bars(datasources.TencentAdapter(fetch=tencent_fetch()),
                                         [ORCH_SYMBOL], ORCH_START, ORCH_END,
                                         conn=conn, data_version=ORCH_VERSION)
    except Exception as exc:                                    # noqa: BLE001
        d0_exc = exc
    finally:
        conn.close()
    if d0_exc is not None or d0_run is None:
        log('  GATE FAIL: D0 对照组自己抛了 %r —— 前提不成立，D 段的绿无从谈起。' % (d0_exc,))
        return 'GUARD_FAIL', EXIT_GUARD
    seen_run, err_run = count_of(RUN_TABLE)
    seen_bar, err_bar = count_of('dc_daily_bar')
    if seen_run is None or seen_bar is None:
        log('  GATE FAIL: D0 数行数失败：%s / %s' % (err_run, err_bar))
        return 'GUARD_FAIL', EXIT_GUARD
    log('      内存里的返回值：status=%s row_count=%d（run_id=%s）'
        % (d0_run.status, d0_run.inserted + d0_run.skipped, d0_run.run_id))
    log('      另一个客户端数到：%s %d 行 / dc_daily_bar %d 行'
        % (RUN_TABLE, seen_run, seen_bar))
    if not (teeth_ok(seen_run) and teeth_ok(seen_bar)):
        log('  PROBE-TEETH-MISSING: 期望另一个客户端在**两张表**上都数到 0 行，实际 %d / %d。'
            % (seen_run, seen_bar))
        log('    含义：本探针分辨不出「编排层跑完了、但一行都没提交」这条缺陷 ⇒ D 段的绿')
        log('          什么也证明不了，故拒绝判定（**不是**「通过了」）。')
        log('    不要去调这两个期望值 —— 先查为什么 D0 的前置裸读没能把会话钉在 INTRANS。')
        return 'GUARD_FAIL', EXIT_GUARD
    log('      OK 内存里是 SUCCESS、库里两张表都是 0 行 ⇒ 这条读法的分辨力被钉住了')

    # ── D1 日线通道 ───────────────────────────────────────────────────────
    log('')
    log('  D1 日线通道（`ingest_daily_bars` → `dc_daily_bar`）')
    if not reset_orch(log):
        return 'GUARD_FAIL', EXIT_GUARD
    daily = datasources.TencentAdapter(fetch=tencent_fetch())
    conn = pgstore.PsycopgConnection(dsn)
    try:
        r1a = ingest.ingest_daily_bars(daily, [ORCH_SYMBOL], ORCH_START, ORCH_END,
                                      conn=conn, data_version=ORCH_VERSION)
        r1b = ingest.ingest_daily_bars(daily, [ORCH_SYMBOL], ORCH_START, ORCH_END,
                                      conn=conn, data_version=ORCH_VERSION,
                                      priority=daily.priority)
    finally:
        conn.close()
    log('      D1a 不传 priority（用**默认值**）：status=%s inserted=%d skipped=%d run_id=%s'
        % (r1a.status, r1a.inserted, r1a.skipped, r1a.run_id))
    log('          注：适配器自己声明的优先级是 %s，而默认落进日志的是 `PRIMARY`。'
        % daily.priority.value)
    log('          两者不同是**产品行为**（调用方要自己传 `priority=adapter.priority`）、')
    log('          不是缺陷；本探针把两个值都记下来，免得下一个读的人以为是同一个数。')
    log('      D1b 传 priority=adapter.priority（%s）：status=%s inserted=%d skipped=%d run_id=%s'
        % (daily.priority.value, r1b.status, r1b.inserted, r1b.skipped, r1b.run_id))
    bad += show_run_row(log, 'D1a', r1a.run_id, {
        'adapter': 'tencent', 'priority': 'PRIMARY', 'status': 'SUCCESS',
        'symbol_count': '1', 'row_count': '3', 'start_date': '2026-03-02',
        'end_date': '2026-03-04', 'data_version': ORCH_VERSION,
        'finished_at': ('nonempty',), 'error_message': ''})
    bad += show_run_row(log, 'D1b', r1b.run_id, {
        'adapter': 'tencent', 'priority': 'FALLBACK', 'status': 'SUCCESS',
        'symbol_count': '1', 'row_count': '3', 'finished_at': ('nonempty',),
        'error_message': ''})
    if (r1a.inserted, r1a.skipped) != (3, 0) or (r1b.inserted, r1b.skipped) != (0, 3):
        log('      MISMATCH 幂等那一步：首次应 inserted=3 skipped=0、重跑应 inserted=0 skipped=3，'
            '实际首次 %s / 重跑 %s' % ((r1a.inserted, r1a.skipped), (r1b.inserted, r1b.skipped)))
        bad += 1
    bad += check_counts(log, 'D1a+b', {'dc_daily_bar': 3})

    # D1c：同键异值 ⇒ 抛 `IngestConflictError`，且**整批回滚**（新那一行也不许留下）。
    conflict_rows = [
        ('2026-03-02', '10.1000', '99.9900', '99.9900', '10.0000', '10000', '{}', '1.50', '980.0'),
        ('2026-03-05', '10.1000', '10.2000', '10.2200', '10.0900', '10000', '{}', '1.00', '102.0'),
    ]
    conn = pgstore.PsycopgConnection(dsn)
    try:
        ok_c, actual_c, exc_c = caught(
            lambda: ingest.ingest_daily_bars(
                datasources.TencentAdapter(fetch=tencent_fetch(rows=conflict_rows)),
                [ORCH_SYMBOL], ORCH_START, ORCH_END2,
                conn=conn, data_version=ORCH_VERSION), 'IngestConflictError')
    finally:
        conn.close()
    log('      D1c 异值冲突（03-02 改了 close，并新增 03-05 一行）：抛的是 %s' % actual_c)
    if not ok_c:
        log('          MISMATCH 期望 `IngestConflictError`，实际 %s（%r）' % (actual_c, exc_c))
        bad += 1
    c_run, c_err = last_run_id()
    if c_run is None:
        log('          MISMATCH 拿不到那一行的 run_id：%s' % c_err)
        bad += 1
    else:
        bad += show_run_row(log, 'D1c', c_run, {
            'adapter': 'tencent', 'status': 'FAILED', 'row_count': '0',
            'symbol_count': '1', 'finished_at': ('nonempty',),
            'error_message': ('nonempty',)})
    bad += check_counts(log, 'D1c 回滚后', {'dc_daily_bar': 3})

    # ── D2/D3 复权因子与分红通道 ──────────────────────────────────────────
    log('')
    log('  D2 复权因子通道（`ingest_adjust_factors` → `dc_adjust_factor`）')
    log('  D3 分红通道（`ingest_dividends` → `dc_dividend`，窗口比的是 ex_date）')
    ts = datasources.TushareAdapter(fetch=tushare_factor_fetch(),
                                    dividend_fetch=tushare_dividend_fetch())
    conn = pgstore.PsycopgConnection(dsn)
    try:
        r2 = ingest.ingest_adjust_factors(ts, [ORCH_SYMBOL], ORCH_START, ORCH_END,
                                         conn=conn, data_version=ORCH_VERSION,
                                         priority=ts.priority)
        r3 = ingest.ingest_dividends(ts, [ORCH_SYMBOL], DIV_START, DIV_END,
                                     conn=conn, data_version=ORCH_VERSION,
                                     priority=ts.priority)
    finally:
        conn.close()
    log('      D2 status=%s inserted=%d skipped=%d run_id=%s'
        % (r2.status, r2.inserted, r2.skipped, r2.run_id))
    log('      D3 status=%s inserted=%d skipped=%d run_id=%s'
        % (r3.status, r3.inserted, r3.skipped, r3.run_id))
    bad += show_run_row(log, 'D2', r2.run_id, {
        'adapter': 'tushare', 'priority': 'FALLBACK', 'status': 'SUCCESS',
        'symbol_count': '1', 'row_count': '2', 'data_version': ORCH_VERSION,
        'start_date': '2026-03-02', 'end_date': '2026-03-04',
        'finished_at': ('nonempty',), 'error_message': ''})
    bad += show_run_row(log, 'D3', r3.run_id, {
        'adapter': 'tushare', 'priority': 'FALLBACK', 'status': 'SUCCESS',
        'symbol_count': '1', 'row_count': '2', 'data_version': ORCH_VERSION,
        'start_date': '2026-01-01', 'end_date': '2026-12-31',
        'finished_at': ('nonempty',), 'error_message': ''})
    if (r2.inserted, r3.inserted) != (2, 2):
        log('      MISMATCH 因子/分红的首跑 inserted 应为 2 / 2，实际 %s / %s'
            % (r2.inserted, r3.inserted))
        bad += 1
    bad += check_counts(log, 'D2+D3', {'dc_daily_bar': 3, 'dc_adjust_factor': 2,
                                       'dc_dividend': 2})
    # 分红是按 ex_date 过滤的：窗口写错（比如用 announce_date 比）时这两行会不在帧里，
    # 上面那条 row_count=2 立刻会红 —— 这里再单独把**值**读一次，因为「行数对」不等于
    # 「每股现金对」（`cash_div` 税后列与 `cash_div_tax` 税前列差的就是这个数）。
    for ex_date, want_cash in (('2026-06-10', '0.3000'), ('2026-09-10', '0.0001')):
        rc, val, out = psql_scalar(
            "SELECT cash_per_share::text FROM dc_dividend WHERE symbol = %s AND ex_date = %s "
            "AND data_version = %s"
            % (quote_literal(ORCH_SYMBOL), quote_literal(ex_date), quote_literal(ORCH_VERSION)))
        if rc != 0 or (val or '').strip() != want_cash:
            log('      MISMATCH ex_date=%s 的 cash_per_share：实际 %r / 期望 %s'
                % (ex_date, (val or '').strip(), want_cash))
            bad += 1

    # ── D4 质检拒绝 ⇒ 一行都不落库 ────────────────────────────────────────
    log('')
    log('  D4 质检拒绝（同一自然键在同一帧里出现两次）⇒ `DataQualityError`、一行都不落库')
    dup_rows = list(TENCENT_BASE_ROWS) + [TENCENT_BASE_ROWS[2]]
    conn = pgstore.PsycopgConnection(dsn)
    try:
        ok_d, actual_d, exc_d = caught(
            lambda: ingest.ingest_daily_bars(
                datasources.TencentAdapter(fetch=tencent_fetch(rows=dup_rows)),
                [ORCH_SYMBOL], ORCH_START, ORCH_END,
                conn=conn, data_version=ORCH_VERSION), 'DataQualityError')
    finally:
        conn.close()
    log('      D4 抛的是 %s' % actual_d)
    if not ok_d:
        log('          MISMATCH 期望 `DataQualityError`，实际 %s（%r）' % (actual_d, exc_d))
        bad += 1
    d_run, d_err = last_run_id()
    if d_run is None:
        log('          MISMATCH 拿不到那一行的 run_id：%s' % d_err)
        bad += 1
    else:
        bad += show_run_row(log, 'D4', d_run, {
            'status': 'FAILED', 'row_count': '0', 'finished_at': ('nonempty',),
            'error_message': ('contains', '没通过校验')})
    bad += check_counts(log, 'D4', {'dc_daily_bar': 3, 'dc_adjust_factor': 2,
                                    'dc_dividend': 2})

    # ── D5 要了两个标的、只回来一个 ⇒ PARTIAL ────────────────────────────
    log('')
    log('  D5 要了两个标的、只回来一个（`%s` 空帧）⇒ `PARTIAL` + error_message' % ORCH_OTHER)
    conn = pgstore.PsycopgConnection(dsn)
    try:
        r5 = ingest.ingest_daily_bars(
            datasources.TencentAdapter(fetch=tencent_fetch(only=(ORCH_SYMBOL,))),
            [ORCH_SYMBOL, ORCH_OTHER], ORCH_START, ORCH_END,
            conn=conn, data_version=ORCH_VERSION)
    finally:
        conn.close()
    log('      D5 status=%s missing=%s inserted=%d' % (r5.status, r5.missing_symbols, r5.inserted))
    if r5.status != 'PARTIAL' or r5.missing_symbols != (ORCH_OTHER,):
        log('          MISMATCH 期望 status=PARTIAL 且 missing=(%s,)，实际 %s / %s'
            % (ORCH_OTHER, r5.status, r5.missing_symbols))
        bad += 1
    if r5.complete:
        log('          MISMATCH `IngestRun.complete` 在缺标的时不应为 True')
        bad += 1
    bad += show_run_row(log, 'D5', r5.run_id, {
        'adapter': 'tencent', 'status': 'PARTIAL', 'symbol_count': '2', 'row_count': '3',
        'finished_at': ('nonempty',), 'error_message': ('contains', ORCH_OTHER)})
    # ⚠️ 期望**仍是 3**而不是 6：D5 要回来的那一个标的与 D1 是**同一批行**（同自然键
    # 同 `data_version`）⇒ 它们是 skipped 不是 inserted（日志里 `inserted=0` 就是这件事）。
    # 想让它变成 6，得另换一个 `data_version`，那测的就不是「PARTIAL 时缺的那个标的不落库」了。
    bad += check_counts(log, 'D5', {'dc_daily_bar': 3, 'dc_adjust_factor': 2,
                                    'dc_dividend': 2})
    # 上一条只能证「总数没涨」—— 总数不变也可能是「缺的那个标的的行**覆盖了**已有的行」。
    # 所以再**按标的**数一次：没取到的那个必须 0 行，取到的那个必须还是 3 行。
    n_other, err_o = psql_expect('SELECT count(*) FROM dc_daily_bar WHERE symbol = %s'
                                 % quote_literal(ORCH_OTHER))
    n_main, err_m = psql_expect('SELECT count(*) FROM dc_daily_bar WHERE symbol = %s'
                                % quote_literal(ORCH_SYMBOL))
    if n_other is None or n_main is None:
        log('      GATE FAIL: 按标的数行数失败：%s / %s' % (err_o, err_m))
        return 'GUARD_FAIL', EXIT_GUARD
    if n_other != 0:
        log('          MISMATCH 没取到的标的 %s 在 dc_daily_bar 里是 %d 行（应为 0）'
            % (ORCH_OTHER, n_other))
        bad += 1
    if n_main != 3:
        log('          MISMATCH 取到的标的 %s 在 dc_daily_bar 里是 %d 行（应为 3）'
            % (ORCH_SYMBOL, n_main))
        bad += 1
    if n_other == 0 and n_main == 3:
        log('          OK 按标的数：%s 3 行、%s 0 行（缺的那一个真的没落库）'
            % (ORCH_SYMBOL, ORCH_OTHER))

    # ── D6 前置检查失败 ⇒ **不留**批次行 ──────────────────────────────────
    log('')
    log('  D6 前置检查失败（`data_version` 空白 / `symbols` 为空）⇒ 连批次行都不该留')
    before, err_b = count_of(RUN_TABLE)
    if before is None:
        log('      GATE FAIL: 数不了 %s：%s' % (RUN_TABLE, err_b))
        return 'GUARD_FAIL', EXIT_GUARD
    conn = pgstore.PsycopgConnection(dsn)
    try:
        ok_v, actual_v, _ = caught(
            lambda: ingest.ingest_daily_bars(daily, [ORCH_SYMBOL], ORCH_START, ORCH_END,
                                             conn=conn, data_version='   '),
            'DataVersionError')
        ok_s, actual_s, _ = caught(
            lambda: ingest.ingest_daily_bars(daily, [], ORCH_START, ORCH_END,
                                             conn=conn, data_version=ORCH_VERSION),
            'InvalidParamError')
    finally:
        conn.close()
    log('      D6 空白 data_version 抛 %s；空 symbols 抛 %s' % (actual_v, actual_s))
    if not ok_v:
        log('          MISMATCH 期望 `DataVersionError`，实际 %s' % actual_v)
        bad += 1
    if not ok_s:
        log('          MISMATCH 期望 `InvalidParamError`，实际 %s' % actual_s)
        bad += 1
    after, err_a = count_of(RUN_TABLE)
    if after is None:
        log('      GATE FAIL: 数不了 %s：%s' % (RUN_TABLE, err_a))
        return 'GUARD_FAIL', EXIT_GUARD
    if after != before:
        log('          MISMATCH %s 从 %d 行变成 %d 行 —— 前置检查失败不该留批次行'
            % (RUN_TABLE, before, after))
        bad += 1
    else:
        log('          OK %s 仍是 %d 行（前置检查在开批次行之前就拦住了）'
            % (RUN_TABLE, after))

    log('')
    if bad:
        log('D 段 FAIL：%d 处不符。编排层在真库上的行为与它写在文档里的不一样，' % bad)
        log('  先看上面每一条 MISMATCH 的实际值，再决定改产物还是改这条判据的期望。')
        log('  （**不许**为了让本段变绿去调期望值 —— 那等于把这条证据删掉。）')
        return 'FAIL', EXIT_FAIL
    log('D 段 OK：三条通道各真跑一次 + 幂等 + 冲突回滚 + 质检拒绝 + PARTIAL + 前置不留痕，')
    log('  全部由**另一个客户端**读 `dc_ingest_run` 与三张业务表核对。')
    log("  这一层因此拿到了真库证据 —— 但「谁定时调它」仍然不存在（`quanauto/cli.py` 没有")
    log('  采集子命令，本仓库里唯一的调用者就是本探针与 `tests/test_ingest.py`）。')
    return 'OK', EXIT_OK


# ---------------------------------------------------------------------------
# 容器生命周期
# ---------------------------------------------------------------------------

def start_container(log, image, digest):
    rc, out = run(['docker', 'rm', '-f', CONTAINER], timeout=120)
    log('pre-clean: docker rm -f %s -> exit %d（没东西可删时非 0 属正常）' % (CONTAINER, rc))

    port = free_port(55432, (55433, 55434, 55435))
    if port is None:
        log('GATE FAIL: 宿主上找不到可用的本地端口。')
        return None
    log('宿主端口：127.0.0.1:%d -> 容器 5432（**只绑回环**，不对外）' % port)

    argv = ['docker', 'run', '-d', '--name', CONTAINER,
            '-e', 'POSTGRES_DB=%s' % DB_NAME,
            '-e', 'POSTGRES_USER=%s' % DB_USER,
            '-e', 'POSTGRES_PASSWORD=%s' % DB_PASSWORD,
            '-e', 'POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C.UTF-8',
            '-e', 'PGCLIENTENCODING=UTF8',
            '-p', '127.0.0.1:%d:5432' % port,
            image]
    rc, out = run(argv, timeout=600)
    if rc != 0:
        log('GATE FAIL: 容器没起来（docker run 退出码 %d）。' % rc)
        log.indent(out.strip()[:1500])
        return None
    log('容器已创建：%s' % out.strip()[:20])

    # 就绪探测用 **TCP**（`-h 127.0.0.1`）：initdb 那个临时服务器只监听 socket，
    # 用 socket 探会「太早报健康」。这条与 docker-compose.smoke.yml 的 healthcheck 同源。
    deadline = time.time() + 180
    while time.time() < deadline:
        rc, _ = dexec(['pg_isready', '-h', '127.0.0.1', '-U', DB_USER, '-d', DB_NAME],
                      timeout=30)
        if rc == 0:
            log('数据库已就绪（pg_isready -h 127.0.0.1 返回 0）')
            return {'port': port, 'image': image, 'digest': digest,
                    'dsn': 'postgresql://%s:%s@127.0.0.1:%d/%s'
                           % (DB_USER, DB_PASSWORD, port, DB_NAME)}
        time.sleep(2)
    log('GATE FAIL: 等了 180s 还没就绪。')
    return None


def teardown(log, keep):
    if keep:
        log('')
        log('--keep: 容器保留。人工排查：')
        log('    docker exec -it %s psql -U %s -d %s' % (CONTAINER, DB_USER, DB_NAME))
        log('    排查完请务必手工 docker rm -f %s' % CONTAINER)
        return
    rc, out = run(['docker', 'rm', '-f', CONTAINER], timeout=300)
    log('')
    log('teardown: docker rm -f %s -> exit %d' % (CONTAINER, rc))
    if rc != 0:
        log('  注意：容器没删掉。残留的容器会占着 55432 附近的端口与名字，')
        log('        下一次运行要么起不来、要么连到上一个容器上。届时手工清理。')
        log.indent(out.strip()[:400])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def write_report(lines, verdict, exit_code):
    body = ['SCOPE: probe_ingest_commit（A 段对照 / B 段真跑 / C 段舍入 / D 段编排层）-- 探针，不是门禁。',
            '']
    body += list(lines)
    body.append('')
    body.append('verdict: %s' % verdict)
    body.append('exit code: %d' % exit_code)
    body.append('NOTE: 本报告的结论只对上面记录的镜像 digest + 下面这些文件的 sha256 成立。')
    for rel in PINNED_FILES:
        body.append('      %s  sha256=%s' % (rel, sha256_of(rel)))
    body.append('      上面任何一个 hash 变了，本快照即作废 —— 重跑本探针，不要只改结论。')
    body.append('probe: %s  sha256=%s（出这份报告的**工具**版本；改了它也该重跑）'
                % (os.path.relpath(os.path.abspath(__file__), ROOT).replace('\\', '/'),
                   hashlib.sha256(open(os.path.abspath(__file__), 'rb').read()).hexdigest()))
    body.append('      （报告里**不**钉 git commit：它记录的是输入文件与镜像，不是某次提交，')
    body.append('        所以它可以和代码同批提交。）')
    with open(REPORT_PATH, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(body) + '\n')


def finish(log, verdict, code):
    try:
        write_report(log.lines, verdict, code)
    except Exception as exc:                                    # noqa: BLE001
        log('')
        log('GATE FAIL: 报告写不出去（%r）。target: %s' % (exc, REPORT_PATH))
        log('          结论没落盘 = 本次不产生任何结论。本来是 PASS 的必须降级。')
        return code if code != EXIT_OK else EXIT_GUARD
    log('')
    log('report: %s -- 看它，不要为了看结果重跑（重跑要起容器）。' % REPORT_PATH)
    log('verdict: %s' % verdict)
    return code


def parse_args(argv):
    opts = {'image': DEFAULT_IMAGE, 'keep': False, 'selftest': False, 'report': None}
    for arg in argv[1:]:
        if arg == '--selftest':
            opts['selftest'] = True
        elif arg == '--keep':
            opts['keep'] = True
        elif arg.startswith('--pg-image='):
            opts['image'] = arg.split('=', 1)[1] or DEFAULT_IMAGE
        elif arg.startswith('--report='):
            opts['report'] = arg.split('=', 1)[1]
    return opts


def main():
    harden_stdout()
    opts = parse_args(sys.argv)
    if opts['selftest']:
        return selftest()

    log = Log()
    log('probe-ingest-commit: 三条落库写入路径 + 编排层三条产品通道，在容器 PostgreSQL 上')
    log('                     真跑一次，并用**另一个客户端**读回')
    log('                     （对照 DC 契约附录 J 的 J-6 / J-8 / J-11）。')
    log('')

    # --- 0. 报告落点 ------------------------------------------------------
    # 与 `run_sql_smoke.py` 同一条守卫：参数写错时**不**退回默认路径。
    # 退回会把上一轮那份证据覆盖掉，而且覆盖它的还是一份「本次没跑成」的报告。
    if opts['report'] is not None:
        new_path, why = resolve_report_path(opts['report'])
        if new_path is None:
            log('GATE FAIL: --report 不可用 -- %s' % why)
            log('          报告写不到指定位置时**不会**退回默认路径（那会覆盖上一轮证据）。')
            log('          故本次不写任何报告。')
            return EXIT_GUARD
        globals()['REPORT_PATH'] = new_path
        log('report target: %s' % new_path)
        log('              （默认落点是 %s；本次是另存，不会动它）' % DEFAULT_REPORT_PATH)
        log('')

    # --- 1. 环境 ----------------------------------------------------------
    rc, out = run(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=120)
    if rc != 0:
        log('NO ENV: docker daemon 没有应答。')
        log.indent(out.strip()[:600])
        log('')
        log('verdict: NO ENV -- 本次什么都没执行，所以本次不产生任何结论。')
        log('         注意：不要把这句话读成「写入路径未经验证」。已经跑过的那一轮写在')
        log('         %s（快照）；本句只说「这一次没跑」。' % os.path.basename(REPORT_PATH))
        return finish(log, 'NO ENV：docker 不可用', EXIT_NO_ENV)
    log('docker server version: %s' % out.strip())

    rc, out = run(['docker', 'image', 'inspect', '--format', '{{.Id}} {{index .RepoDigests 0}}',
                   opts['image']], timeout=120)
    if rc != 0:
        log('NO ENV: 本机没有镜像 %s（拉镜像不是本探针的事）。%s'
            % (opts['image'], out.strip()[:200]))
        return finish(log, 'NO ENV：镜像不在本机', EXIT_NO_ENV)
    image_id, _, repo_digest = out.strip().partition(' ')
    log('image  : %s' % opts['image'])
    log('imageid: %s' % image_id)
    log('digest : %s' % (repo_digest or '(未取到)'))

    try:
        import psycopg                                          # noqa: F401
    except ImportError as exc:
        log('NO ENV: 宿主没有 psycopg（%s）⇒ 没法在本进程里走 quanauto/pgstore.py。' % exc)
        log('        装法：pip install "psycopg[binary]>=3.1" 或 pip install -e ".[postgres]"')
        return finish(log, 'NO ENV：宿主没有 psycopg', EXIT_NO_ENV)

    # --- 2. 起容器 --------------------------------------------------------
    log('')
    log('--- 起临时容器 ---')
    started = start_container(log, opts['image'], repo_digest)
    if started is None:
        return finish(log, 'GUARD_FAIL：容器起不来，本次无任何结论', EXIT_GUARD)

    result = ('GUARD_FAIL：脚本自身异常', EXIT_GUARD)
    try:
        result = body(log, started)
    except Exception as exc:                                    # noqa: BLE001
        log('UNEXPECTED ERROR: %r' % (exc,))
        log('（脚本自身出错，本次无任何结论 —— 不要把它读成「写入路径有问题」。）')
        result = ('GUARD_FAIL：脚本自身异常', EXIT_GUARD)
    finally:
        # 拆容器放在这里：成功路径同样要拆，否则残留容器会占着端口与名字，
        # 让下一次运行要么起不来、要么连到上一个容器上。
        teardown(log, opts['keep'])

    return finish(log, result[0], result[1])


def body(log, env):
    dsn = env['dsn']

    # --- 3. 前置守卫 ------------------------------------------------------
    rc, out = psql_stdin('SELECT 1;', timeout=120)
    if rc != 0:
        log('GATE FAIL: 起来了但连不进去。')
        log.indent(out.strip()[:600])
        return 'GUARD_FAIL：连不上刚起来的库', EXIT_GUARD

    log('')
    log('--- 前置守卫 ---')
    for var in ('server_encoding', 'client_encoding'):
        rc, val, out = psql_scalar('SHOW %s' % var)
        if rc != 0 or val.upper() != 'UTF8':
            log('GATE FAIL: %s = %r，期望 UTF8。DDL 里有中文注释，编码不对时结果不可信。'
                % (var, val))
            return 'GUARD_FAIL：编码不是 UTF8', EXIT_GUARD
        log('  %s = %s  OK' % (var, val))

    n, err = psql_expect(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')")
    if n is None:
        log('GATE FAIL: 数不了 public schema 的表数：%s' % err)
        log.indent(str(err)[:400])
        return 'GUARD_FAIL：空库守卫无法执行', EXIT_GUARD
    if n != 0:
        log('GATE FAIL: 库不是空的 -- public schema 里已有 %d 张表。' % n)
        log('         `db/data_center.sql` 全用 CREATE TABLE IF NOT EXISTS：在**已有** schema 上')
        log('         重跑会静默什么都不做，于是可能拿旧 schema 跑出「全绿」。拒绝判定。')
        return 'GUARD_FAIL：库不是空的', EXIT_GUARD
    log('  public schema 表数 = 0（全新库）  OK')

    # --- 4. 应用真 DDL ----------------------------------------------------
    log('')
    log('--- 应用 db/data_center.sql（**真** DDL，不是 smoke 里的副本）---')
    ddl_rel = 'db/data_center.sql'
    try:
        with open(os.path.join(ROOT, ddl_rel), 'r', encoding='utf-8') as fh:
            ddl_text = fh.read()
    except OSError as exc:
        log('GATE FAIL: 读不到 %s：%r' % (ddl_rel, exc))
        return 'GUARD_FAIL：DDL 读不到', EXIT_GUARD
    rc, out = psql_stdin(ddl_text)
    if rc != 0:
        log('GATE FAIL: DDL 本身没干净跑完（psql exit=%d）。' % rc)
        log.indent(out.strip()[-1500:])
        return 'GUARD_FAIL：DDL 未通过', EXIT_GUARD
    log('ddl applied: %s' % ddl_rel)

    _, version, _ = psql_scalar('SHOW server_version')
    log('')
    log('--- 实测环境 ---')
    log('  server : PostgreSQL %s' % (version or '(未取到)'))

    # --- 5. 四段 ----------------------------------------------------------
    for section in (section_a, section_b, section_c, section_d):
        verdict, code = section(log, dsn)
        if verdict != 'OK':
            log('')
            log('boundary: %s 段没通过 ⇒ 后面的段结论不完整，本次不报 PASS。'
                % section.__name__.replace('section_', '').upper())
            return '%s 段 %s' % (section.__name__.replace('section_', '').upper(), verdict), code

    # --- 6. 总判据 --------------------------------------------------------
    log('')
    log('--- 汇总 ---')
    log('  A 段  对照（autocommit=False 下新客户端看到 0 行）  OK')
    log('  B 段  真跑（三条写入路径都由另一个客户端读到）      OK')
    log('  C 段  舍入（半值两侧同向）                          OK')
    log('  D 段  编排（`quanauto/ingest.py` 三条通道 + 留痕）  OK')
    log('')
    log('boundary: 本次证明的是「`quanauto/pgstore.py` 的三条**落库写入**路径（B 段）与')
    log('          `quanauto/ingest.py` 的三条**产品通道**（D 段）在真 PostgreSQL 上真的')
    log('          提交了、且留痕行由另一个客户端读到」，**不是**「真实券源取过数」。')
    log('boundary: D 段**没有**证明的：① 「真源取数」（源侧是注入的取数函数，是假源 ——')
    log('          真实取数天然不可复现，也不该被要求跑，见 迭代计划 §21.4）；② 「谁定时调它」')
    log('          （`quanauto/cli.py` 没有采集子命令，本仓库里只有本探针与 `tests/test_ingest.py`')
    log('          调过这三个函数）；③ 并发 / 连接池语义（本段全用短命连接）。')
    log('boundary: 本次证明只对 image=%s (digest=%s) 与上面记的那几个文件 hash 成立。'
        % (env.get('image') or '?', env.get('digest') or '?'))
    log('boundary: 输入是**本探针自己构造的行**（B 段是行对象、D 段的源侧是注入的取数函数），')
    log('          不是任何真实券源取回来的数据 —— 「真源取数」仍然没有跑过。')
    return 'PROBE PASS（A/B/C/D 四段都按预期）', EXIT_OK


# ---------------------------------------------------------------------------
# 自测：纯函数各构造正/负样本
# ---------------------------------------------------------------------------

def selftest():
    ok = True

    def case(tag, got, want):
        nonlocal ok
        hit = (got == want)
        print('  [%s] got=%r %s' % (tag, got, 'OK' if hit else 'MISSED (want %r)' % (want,)))
        ok = ok and hit

    # `as_int`：**最关键**的一条是「读不出来」绝不能变成 0 —— 本探针有一处的正确预期
    # 恰好就是 0（A2 段），折算进去会让「有牙」与「坏了」长得一模一样。
    case('as_int-POS-zero', as_int('0'), 0)
    case('as_int-POS-plain', as_int(' 42 '), 42)
    case('as_int-POS-negative', as_int('-1'), -1)
    case('as_int-NEG-empty', as_int(''), None)
    case('as_int-NEG-none', as_int(None), None)
    case('as_int-NEG-prose', as_int('0 rows'), None)
    case('as_int-NEG-error', as_int('ERROR:  relation "x" does not exist'), None)
    case('as_int-NEG-float', as_int('1.0001'), None)

    # `teeth_ok`：解析失败必须与「看到非 0 行」一样判 False。
    case('teeth-POS-sees-zero', teeth_ok(0), True)
    case('teeth-NEG-sees-rows', teeth_ok(3), False)
    case('teeth-NEG-parse-failed', teeth_ok(None), False)

    # `compare_rounding`：干净样本（两侧一致）必须 0 不符；坏样本必须被抓到。
    same = lambda lit: Decimal(lit)                                 # noqa: E731
    rows, bad = compare_rounding([('a', '1.0000'), ('b', '2.0000')], same, same)
    case('round-POS-all-match-bad', bad, 0)
    case('round-POS-all-match-len', len(rows), 2)

    differ = lambda lit: Decimal('9.9999')                          # noqa: E731
    rows, bad = compare_rounding([('a', '1.0000')], same, differ)
    case('round-NEG-mismatch-count', bad, 1)
    case('round-NEG-mismatch-flagged', rows[0][4], False)

    nothing = lambda lit: None                                      # noqa: E731
    rows, bad = compare_rounding([('a', '1.0000')], same, nothing)
    case('round-NEG-none-is-mismatch', bad, 1)

    # `quote_literal`：样本里没有单引号，但**不假设**这一点。
    case('quote-POS-plain', quote_literal('1.00005'), "'1.00005'")
    case('quote-POS-escape', quote_literal("a'b"), "'a''b'")

    # `split_row`：D 段每一行都先经过它 ⇒ 「切不出来」必须返回 None，不许补齐/截断成
    # 一个「看起来像值不符」的形状（那两种故障要修的地方完全不同）。
    row = split_row('tencent|PRIMARY|SUCCESS|1|3|2026-03-02|2026-03-04|probe-r29|'
                    '2026-10-02 09:00:00.1+00|', len(RUN_COLUMNS))
    case('split-POS-width', len(row), len(RUN_COLUMNS))
    case('split-POS-last-empty', row[-1], '')
    case('split-POS-status', row[2], 'SUCCESS')
    # error_message 里含 `|` 时必须留在**最后那个字段**里（所以它必须排最后一列）。
    piped = split_row('t|PRIMARY|FAILED|1|0|2026-03-02|2026-03-04|v|ts|a|b|c',
                      len(RUN_COLUMNS))
    case('split-POS-pipe-in-message', piped[-1], 'a|b|c')
    case('split-NEG-too-few', split_row('a|b', len(RUN_COLUMNS)), None)
    case('split-NEG-none', split_row(None, len(RUN_COLUMNS)), None)

    # `judge_run_row`：三类样本 —— 干净样本（0 不符，防误报）、坏样本（抓到那一个字段）、
    # 「读不出来」（**必须**产生 1 条不符，否则会静默变成「都对」）。
    good = tuple(['tencent', 'PRIMARY', 'SUCCESS', '1', '3', '2026-03-02', '2026-03-04',
                  'probe-r29', '2026-10-02 09:00:00.1+00', ''])
    want = {'adapter': 'tencent', 'status': 'SUCCESS', 'row_count': '3',
            'finished_at': ('nonempty',), 'error_message': ''}
    bad_list, seen = judge_run_row(good, want)
    case('judge-POS-clean-bad', len(bad_list), 0)
    case('judge-POS-clean-seen', len(seen), len(RUN_COLUMNS))
    case('judge-NEG-field-count', len(judge_run_row(good[:4] + ('7',) + good[5:], want)[0]), 1)
    case('judge-NEG-unreadable-bad', len(judge_run_row(None, want)[0]), 1)
    case('judge-NEG-unreadable-seen', judge_run_row(None, want)[1], ())
    case('judge-NEG-contains-miss', len(judge_run_row(good, {'error_message': ('contains', 'x')})[0]), 1)
    case('judge-POS-nonempty-blank',
         len(judge_run_row(good[:8] + ('',) + good[9:], {'finished_at': ('nonempty',)})[0]), 1)

    # `caught`：命中、抛别的、没抛 —— 三种必须分得开（「没抛」往往比「抛错」更严重）。
    case('caught-POS-hit', caught(lambda: (_ for _ in ()).throw(ValueError('x')), 'ValueError')[0], True)
    case('caught-NEG-other', caught(lambda: (_ for _ in ()).throw(KeyError('x')), 'ValueError')[1:2],
         ('KeyError',))
    case('caught-NEG-none', caught(lambda: None, 'ValueError')[0:2], (False, '(没抛)'))

    # `run` 的「命令不存在」出口：不能崩、要返回 127。
    rc, _ = run(['definitely-not-a-real-binary-xyz'], timeout=30)
    case('run-NEG-missing-binary-rc', rc, 127)

    print('SELFTEST %s：as_int / teeth_ok / compare_rounding / quote_literal / '
          'split_row / judge_run_row / caught / run 的正负样本都有覆盖'
          % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
