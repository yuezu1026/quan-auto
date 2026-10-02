# -*- coding: utf-8 -*-
"""probe_ingest_commit.py -- 三条落库写入路径在容器 PostgreSQL 上真跑一次，并用**新连接**读回。

为什么需要它
------------
DC 契约附录 J 的 J.1 有两行自称「可离线关」，而它们在 2026-10-02 时都还没关：

  * **J-6「真的入过库」** —— B18 那次「520 行入库 / 幂等重跑 `skipped=520`」全绿，但 B20.5 的
    重演证明那是**同一条连接自读**（恒真），另起连接数到 **0** 行。也就是说「测过的那个结论
    比它写的少」。B20.5 只证了**缺陷**（`autocommit=False` 下真的丢数据），**没有**证修法
    （`autocommit=True`）在真库上成立 —— 缺的正是那条**对照组**。
  * **J-8「`ROUND_HALF_UP` 与 PG `numeric` 在半值上同向」** —— `quanauto/pgstore.py::_to_decimal`
    的注释照**文档语义**写了「与 PostgreSQL `numeric` 的四舍五入同义」，而 `.00005` 这类
    半值样本**一个都没有**。文档语义不是实测出来的结论。

本探针把这两条都变成有证据的结论，分三段：

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
结论**会**因为改了 `db/data_center.sql` 或 `quanauto/pgstore.py` 而作废（本报告头部记着
这两个文件的 sha256）。

退出码
------
    0  A/B/C 三段全部按预期
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
PINNED_FILES = (
    'db/data_center.sql',
    'quanauto/pgstore.py',
    'quanauto/datacenter.py',
)

TABLES = ('dc_daily_bar', 'dc_adjust_factor', 'dc_dividend')

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
    body = ['SCOPE: probe_ingest_commit（A 段对照 / B 段真跑 / C 段舍入）-- 探针，不是门禁。',
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
    log('probe-ingest-commit: 三条落库写入路径在容器 PostgreSQL 上真跑一次，')
    log('                     并用**另一个客户端**读回（对照 DC 契约附录 J 的 J-6 / J-8）。')
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

    # --- 5. 三段 ----------------------------------------------------------
    for section in (section_a, section_b, section_c):
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
    log('')
    log('boundary: 本次证明的是「`quanauto/pgstore.py` 的三条**落库写入**路径在真 PostgreSQL')
    log('          上真的提交了」，**不是**「`quanauto/ingest.py` 那层编排在真库上跑过」')
    log('          （那一层的产品调用点仍然只在假连接用例里跑过，见 DC 契约 B21.6 与附录 J 的 J-11）。')
    log('boundary: 本次证明只对 image=%s (digest=%s) 与上面记的那几个文件 hash 成立。'
        % (env.get('image') or '?', env.get('digest') or '?'))
    log('boundary: 输入是**本探针自己构造的行**，不是任何真实券源取回来的数据 ——')
    log('          「真源取数」仍然没有跑过，也不该被要求跑（真实取数天然不可复现）。')
    return 'PROBE PASS（A/B/C 三段都按预期）', EXIT_OK


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

    # `run` 的「命令不存在」出口：不能崩、要返回 127。
    rc, _ = run(['definitely-not-a-real-binary-xyz'], timeout=30)
    case('run-NEG-missing-binary-rc', rc, 127)

    print('SELFTEST %s：as_int / teeth_ok / compare_rounding / quote_literal / run 的'
          '正负样本都有覆盖' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
