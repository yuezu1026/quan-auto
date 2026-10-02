# -*- coding: utf-8 -*-
"""probe_pg_driver.py —— 连接与驱动的行为（无池 / 有池 / 换版本）：把 DC 契约附录 J 的 J-15 变成读数。

    python tools/probe_pg_driver.py                       # 默认 postgres:17，写 tools/pg-driver-probe-report.txt
    python tools/probe_pg_driver.py --pg-image=postgres:14 --report=tools/pg-driver-probe-report-pg14.txt
    python tools/probe_pg_driver.py --report=tools/pg-driver-probe-report-psycopg32.txt   # 用**另一个解释器**跑（见下）
    python tools/probe_pg_driver.py --selftest            # 只跑纯函数样本：不碰 docker、不碰网络、不碰真库
    python tools/probe_pg_driver.py --keep                # 留容器排查

为什么需要它（J-15 的现值，逐字）
----------------------------------
    连接与驱动行为 | B20.6 | `autocommit=True` 与连接池**未测**；`psycopg` **只测 3.3.6**（换版本要重测） | ⚠️ 半

「`autocommit=True`」那一半已由 `tools/probe_ingest_commit.py` 在真库上测掉（附录 J 的 J.4）。
剩下两半本探针量：

  * **有池时的行为**。`quanauto/pgstore.py::PsycopgConnection.transaction()` 的整段理由都建在
    这条源码事实（psycopg 3.3.6 `psycopg/connection.py::Connection.__exit__`）上：

        if not getattr(self, "_pool", None):
            self.close()

    即「无池 ⇒ 出块连连接一起关掉」。但它**只在无池条件下**被验过；池一进来，那条后果就不成立
    —— 于是「`transaction()` 不得不这么写」的理由**变了**（不变的是它仍然该这么写：它要的是
    「在既有连接上开一个事务」，不是「为了不被关掉」）。这件事在本仓库里还牵到一句更大的话：
    「本仓库不建池」。所以 B 段同时给出**行为读数**与**本仓库到底用没用池**的现数（D 段）。
  * **换一个 psycopg 版本**。上面所有行号与分支都是 3.3.6 的；`psycopg_pool` 的
    `close_returns=True` 还**另有**一条与版本绑定的行为，而且分叉点**不只在驱动上**：
    驱动 ≥3.3 时「归还入池」由 `Connection.close()` 自带那一支提供；驱动 <3.3 时由**池**
    顶上一个 `PoolConnection` 子类提供（`psycopg_pool/pool.py` 里 `close_returns and
    PSYCOPG_VERSION < (3, 3)` 那段嵌套 `if connection_class is Connection:`）⇒ 同一个池
    版本（3.3.3）配上 3.2.13 也照样归还。
    C 段把两个版本都写进读数，第二版本由**另一次运行**产生另一份快照（同一个探针字节，
    两个解释器）。
    ⚠️ 这条**第一版写错过**：当时把「3.3 之前驱动 `close()` 里没有池分支」直接推成
    「老驱动仍然真关」，漏读了那个 `if` 嵌套的一层 ⇒ 被 3.2.13 那次真跑当场证伪
    （读到 `closed=0`）。现在改成**直接读连接类的名字**（那才是机制本身）。
    ⚠️ 本仓库声明的下限是 `psycopg[binary]>=3.1`（`pyproject.toml` 的 `[postgres]` extra）——
    下限 ≠ 已验证到这个版本，本探针只主张**跑过的那些版本**。

分四段
------
  A 段（无池的源事实）  裸 `psycopg.connect()` ⇒ `with conn:` 出块即关（并先断这条连接真的**没有**
      池，否则 A 段的结论不成立）；再**复演**「默认 `autocommit=False` + 先裸读一次 ⇒
      `transaction()` 退化成 SAVEPOINT/RELEASE、整批不提交」这件事，缩到一张一列小表上，
      由**另一个进程**（容器内 `psql`）来数行 —— 它同时是 B 段的量具校准（见「空转守卫」）。
  B 段（真池）          用**真的** `psycopg_pool.ConnectionPool`：① `getconn()` 出来的连接带 `_pool`
      ⇒ `with conn:` 出块**不关**（归还）；② 同一个对象、只把 `_pool` 摘掉，出块**就**关（把读数
      归因到那个属性，而不是归因到「psycopg 从不关连接」）；③ 把真池化连接**注入**
      `PsycopgConnection._conn`，走**产品写入路径**（`PgBarIngestor.upsert_daily_bars`），由另一个
      进程读回 —— 池化连接上事务仍真提交、且 `transaction()` 不会关掉它；④ 池的 `close_returns`
      开关让 `conn.close()` 在「真关」与「归还」之间分岔 —— **直接读连接类的名字**判它是谁
      提供的（驱动 ≥3.3 是 `Connection` 自带，<3.3 是池顶上的 `PoolConnection`）。
  C 段（版本）          把 `python` / `psycopg` / `psycopg-pool` 版本与声明下限并排写进读数。
  D 段（本仓库用得到吗）  现数三件事：`quanauto/` 下零池引用（「本仓库不建池」不是靠记忆）；
      `quanauto/pgstore.py` 的实现约束**仍在**（`with conn:` 形状 0 次、`conn.transaction()` 形状 ≥1
      次 —— 用 **AST 形状**数，不数文本：那个文件的 docstring 里就**字面**写着「绝不能写成
      `with conn:`」，文本匹配会把解释性文字当成实现）；`pyproject.toml` 声明的驱动下限现取。

空转守卫（每一段的第一件事）
----------------------------
  * A 段：`psycopg.connect()` 出来的连接若竟然带 `_pool` ⇒ 整段结论无意义，`A0` FAIL。
  * A 段：`A3`（不前置裸读的对照）**必须**由另一个进程数到 **1** 行 —— 数到 0 说明「另一个进程
    读不到」而不是「退化」，量具坏了 ⇒ FAIL。这一条同时给 B 段当校准：同一个 psql 数法在
    「本来就该读到 1」的样本上读得到，B 段的 `0`/`1` 才有意义。
  * B 段：`getconn()` 出来的连接若没有 `_pool` ⇒ `B0` FAIL。
  * 所有标量读数经 `psql_expect()`：取不到（退出码非 0 / 最后一行不是整数）返回 `None`，
    调用方一律 FAIL —— 不把「读不到」当成 `0`。
  * D 段：文件读不到（`read_text` 返回 `None`）与「数到 0 次」必须分开；`pyproject.toml` 里提取
    不到声明下限也必须 FAIL（「没提到」不等于「一致」，与 `DRIVER-EXTRA` 门禁同一口径）。

本报告不主张什么
----------------
  * **不主张**「PostgreSQL 14+ 全都这样」（那是 J-7，四个 tag 的阶梯，不是区间）；
  * **不主张**产品侧用了连接池：`D1` 现数是零引用，B 段那条池化连接是**注入**进
    `PsycopgConnection._conn` 的，只用来在**池化条件下**复核既有写入路径；
  * **不主张**并发正确性（没有多线程压同一个连接，也没有池的等待/超时行为）、不主张长连接或
    生产场景（与 J-15 原来的边界一致：那两格本来就没测）；
  * 结论只对上面记录的**镜像 digest + 驱动版本 + `PINNED_FILES` 的字节**成立。
  * 报告里**不**钉 git commit（只钉被依赖文件的 sha256 与探针自身的 sha256）⇒ 它可以和代码
    同批提交；两个版本的快照里探针 sha 相同即「同一份量具」。

为什么不是门禁
--------------
与 `tools/probe_ingest_commit.py` 同源：它要 docker、要真库、要宿主装了 `psycopg`（B 段还要
`psycopg-pool`）。门禁必须在没有 docker 的机器上也能跑 ⇒ 这条只能当探针，缺环境时退
**3 = NO ENV**，那一档**不算绿**。要长期守它，得先有「证据新鲜度」那条路（现在没有）。

退出码：0 全 PASS / 1 FAIL / 2 GUARD（报告写不出去、空库守卫、容器起不来、输出解析不了）/
3 NO ENV（缺 docker / 缺镜像 / 缺驱动）。`--selftest` 的退出码**只**归自测所有。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import socket
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
for _p in (ROOT, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用 `run_sql_smoke.py` 里**同一条**规则，不抄第二份实现。
# 本项目已经踩过「抄副本漂移 ⇒ 样本全红、看起来像探测器坏了」那一次。
# `run_sql_smoke` 在导入期不执行任何东西（有 `if __name__ == '__main__':` 守卫）。
from run_sql_smoke import Log, harden_stdout, resolve_report_path, run  # noqa: E402

DEFAULT_IMAGE = 'postgres:17'
DEFAULT_REPORT_PATH = os.path.join(TOOLS, 'pg-driver-probe-report.txt')
REPORT_PATH = DEFAULT_REPORT_PATH

CONTAINER = 'quan-pg-driver-probe'
DB_NAME = 'quan'
DB_USER = 'postgres'
# 本地一次性容器**仅**用：端口只绑 127.0.0.1，容器跑完即 rm -f，不写进任何配置。
DB_PASSWORD = 'quan_probe_local_only'

SYMBOL = '600000.SH'
VERSION = 'probe-driver'
BAR_DATE = date(2026, 5, 6)
TXN_TABLE = 'probe_txn'
DDL_PATH = os.path.join(ROOT, 'db', 'data_center.sql')

# 本报告的结论**只对这几个文件的这些字节**成立；它们一改，本报告即作废，重跑。
#  * `db/data_center.sql`：B4 走的是产品写入路径，它的行形状与约束由 DDL 定。
#  * `quanauto/pgstore.py`：被注入连接的那个类、以及 B4 走的写入路径。
#  * `quanauto/datacenter.py`：`DailyBar` 的定义（B4 构造输入行用它）。
#  * `pyproject.toml`：D3/C 段读的声明下限。
PINNED_FILES = (
    'db/data_center.sql',
    'quanauto/pgstore.py',
    'quanauto/datacenter.py',
    'pyproject.toml',
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_GUARD = 2
EXIT_NO_ENV = 3


# ---------------------------------------------------------------------------
# 纯函数（自测只测这些：不碰 docker、不碰网络、不碰真库）
# ---------------------------------------------------------------------------

FLOOR_RE = re.compile(r'postgres\s*=\s*\[\s*"psycopg\[binary\]>=([0-9][0-9.]*)"\s*\]')


def count_occurrences(text, needle):
    """返回 `text` 里 `needle` 出现的次数；**读不到**（`text is None`）返回 `None`。

    `None` 与 `0` 必须分开：D1 的判据里「数到 0 次」是**预期**，而「文件读不到」也数到 0 的话，
    D1 就会在空转里报 OK —— 那正是「提取为空必须判 FAIL」那条。
    """
    if text is None:
        return None
    return text.count(needle)


def read_text(path):
    """读 UTF-8 文本；读不到（或不是 UTF-8）返回 `None` —— 与「空文件」区分开。"""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def sha256_file(path):
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def parse_version(text):
    """`'3.3.6'` -> `(3, 3)`；解析不出返回 `None`（⇒ 调用方判 FAIL，不许当成「未知但不影响」）。"""
    if not isinstance(text, str):
        return None
    m = re.match(r'^\s*(\d+)\.(\d+)', text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def expected_pool_connection_class(text):
    """池写了 `close_returns=True` 时，池**应当**拿哪个连接类去连（按**驱动**版本）。

    源码事实（本机 `.venv`，`psycopg_pool/pool.py` L65-70，池 3.3.3）::

        if close_returns and PSYCOPG_VERSION < (3, 3):
            if connection_class is Connection:
                connection_class = PoolConnection      # ← 池自己顶上一个子类
            else:
                raise TypeError("… requires psycopg 3.3 or newer …")

    也就是说「归还入池」有两个提供者：驱动 ≥3.3 由 `Connection.close()` 自带的那一支提供
    （`pool = getattr(self, "_pool", None)` + `if pool and … close_returns: pool.putconn(self); return`）；
    驱动 <3.3 时由**池**顶上的 `PoolConnection` 子类提供。

    ⚠️ 这条判据的第一版**写错了**：它按 `psycopg/connection.py::close()` 里「3.3 起才有池分支」
    直接推成「老驱动仍然真关」，**漏读了上面那个 `if connection_class is Connection:` —— 它嵌套在
    第一个 `if` 里**。于是 `close_returns 3.2.13 -> 真关` 被写成期望，被 3.2.13 那次真跑当场
    证伪（池换了 `PoolConnection`，`closed=0`）。⇒ 现在改为**直接读连接类的名字**
    （那是机制本身），不从驱动力版本推后果。

    返回 `'PoolConnection'` / `'Connection'`；版本号读不出返回 `None`（⇒ 调用方判 FAIL）。
    """
    ver = parse_version(text)
    if ver is None:
        return None
    return 'PoolConnection' if ver < (3, 3) else 'Connection'


def extract_driver_floor(text):
    """从 `pyproject.toml` 的文本里取 `[postgres]` extra 声明的驱动下限（如 `'3.1'`）。

    提取不到返回 `None` ⇒ 调用方判 FAIL。**不允许**把「没提到」当成「一致」——
    这是 `skeleton` 门禁的 `DRIVER-EXTRA` 已经立过的规矩，这里照抄口径。
    """
    if text is None:
        return None
    m = FLOOR_RE.search(text)
    return m.group(1) if m else None


def judge_exit_closes(closed_value):
    """`with conn:` 出块之后连接**是否**被关：`0` 视为没关（False），非 0 视为关了（True）。"""
    try:
        return int(closed_value) != 0
    except (TypeError, ValueError):
        raise ValueError('closed 读数不是整数：%r' % (closed_value,))


def judge_pool_attached(pool_attr, pool):
    """`getattr(conn, '_pool', None) is pool` —— B 段的空转守卫。"""
    return pool is not None and pool_attr is pool


TX_STATUS_NAMES = {0: 'IDLE', 1: 'ACTIVE', 2: 'INTRANS', 3: 'INERROR', 4: 'UNKNOWN'}


def tx_status_name(status):
    """`conn.info.transaction_status` -> `'IDLE'` / `'INTRANS'` 这种名字。

    ⚠️ 实测（2026-10-02，psycopg 3.3.6 / Python 3.13）：它的值是 `IntEnum`，而 **Python 3.11 起
    `IntEnum.__str__` 就是 `int.__str__`** ⇒ `str(status)` 出来是 `'2'`，根本不是一个名字。
    第一版按 `'TransactionStatus.INTRANS'` 取最后一段（那是 3.10 及以前的形状），于是在
    `IDLE -> INTRANS` 上读到 `0 -> 2`、报了一条**假 FAIL**。所以两种形状都要认：先取最后一个
    `.` 之后那段，纯数字就查表。这一步本身就是「判据依赖的是驱动的呈现细节」的一个样本 ——
    所以它有专门的样本（见 `--selftest`）。
    """
    text = str(status).rsplit('.', 1)[-1].strip()
    if text.isdigit():
        return TX_STATUS_NAMES.get(int(text), 'UNKNOWN(%s)' % text)
    return text


def count_ast_shapes(source):
    """数两类**形状**：`with <名字>:` 的 with 语句、`<名字>.transaction()` 的调用。

    为什么是 AST 而不是文本：`quanauto/pgstore.py` 的 docstring 里就**字面**写着
    「**绝不能**写成 `with conn:`」—— 拿文本匹配会把解释性文字数成实现（`'with conn:'` 命中 1 次），
    于是「实现约束还在不在」这件事**永远判红**。判据必须钉到形状，才不会被解释性文字满足。
    （同族教训见 e2e 规范：覆盖类判据要收窄到等式断言的形状，不能是「名字出现过」。）

    返回 `(with_names, tx_call_names)`；解析不了返回 `None`（⇒ 调用方 FAIL，不许当成「0 处」）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    with_names = []
    tx_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Name):
                    with_names.append(expr.id)
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == 'transaction'
                    and isinstance(func.value, ast.Name)):
                tx_names.append(func.value.id)
    return with_names, tx_names


# ---------------------------------------------------------------------------
# 容器与 psql
# ---------------------------------------------------------------------------

def psql_run(env, sql=None, path=None, timeout=300):
    """容器内 psql（unix socket，官方镜像 pg_hba 对 local 是 trust ⇒ 不需要密码）。"""
    argv = ['docker', 'exec', CONTAINER, 'psql', '-v', 'ON_ERROR_STOP=1',
            '-U', DB_USER, '-d', DB_NAME]
    if sql is not None:
        argv += ['-tAc', sql]
    if path is not None:
        argv += ['-f', path]
    return run(argv, timeout=timeout)


def psql_expect(env, sql):
    """取一个整数标量；取不到（退出码非 0 / 最后一行不是整数）返回 `None` ⇒ 调用方必须判 FAIL。"""
    rc, out = psql_run(env, sql=sql, timeout=180)
    if rc != 0:
        return None
    last = ''
    for line in out.splitlines():
        if line.strip():
            last = line.strip()
    if not re.match(r'^-?\d+$', last):
        return None
    return int(last)


def free_port(preferred, candidates):
    """挑一个能绑的宿主机端口。绑不上时逐个试备用 —— 「端口被占」与「镜像没有」必须分得开。"""
    import errno
    for port in (preferred,) + tuple(candidates):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                continue
            raise
        finally:
            sock.close()
        return port
    return None


def start_container(log, image):
    """起一次性容器。返回 (env, kind, why)：kind 为 None 表示成功。"""
    run(['docker', 'rm', '-f', CONTAINER], timeout=180)
    port = free_port(55436, (55437, 55438, 55439))
    if port is None:
        return None, 'GUARD', '宿主机 55436~55439 全被占；分不清是端口问题还是镜像问题，拒绝往下走'
    rc, out = run(['docker', 'run', '-d', '--name', CONTAINER,
                   '-e', 'POSTGRES_PASSWORD=' + DB_PASSWORD,
                   '-e', 'POSTGRES_DB=' + DB_NAME,
                   '-p', '127.0.0.1:%d:5432' % port, image], timeout=600)
    if rc != 0:
        return None, 'GUARD', 'docker run 失败（rc=%d）：%s' % (rc, out.strip()[-600:])
    deadline = time.time() + 180
    ready = False
    while time.time() < deadline:
        rc2, _ = run(['docker', 'exec', CONTAINER, 'pg_isready', '-h', '127.0.0.1'], timeout=60)
        if rc2 == 0:
            ready = True
            break
        time.sleep(2)
    if not ready:
        return None, 'GUARD', '容器 180s 内没有就绪（pg_isready 一直非 0）'
    digest = image_digest(log, image)
    env = {'port': port, 'image': image, 'digest': digest,
           'dsn': 'postgresql://%s:%s@127.0.0.1:%d/%s' % (DB_USER, DB_PASSWORD, port, DB_NAME),
           'readings': {}}
    return env, None, None


def image_digest(log, image):
    rc, out = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image], timeout=120)
    if rc != 0:
        log('    (image inspect 失败，digest 记为空：%s)' % out.strip()[:200])
        return ''
    return out.strip().splitlines()[-1].strip() if out.strip() else ''


def teardown(log, keep):
    if keep:
        log('--keep：容器 %s 留着（跑完请自己 docker rm -f %s）' % (CONTAINER, CONTAINER))
        return
    rc, out = run(['docker', 'rm', '-f', CONTAINER], timeout=180)
    log('容器已删：rc=%d %s' % (rc, out.strip()[:120]))


# ---------------------------------------------------------------------------
# 段
# ---------------------------------------------------------------------------

def section_a(log, env):
    """无池的源事实：`with conn:` 出块即关；默认 `autocommit=False` + 前置裸读 ⇒ 整批不提交（复演）。"""
    psycopg = env['psycopg']
    dsn = env['dsn']

    conn = psycopg.connect(dsn, autocommit=True)
    # A0：整段结论的前提 = 这条连接真的没有池
    if getattr(conn, '_pool', None) is not None:
        return ('A0：裸 psycopg.connect() 出来的连接竟然带 _pool ⇒ 「无池」这个前提不成立，'
                'A 段整套结论无意义')
    with conn:
        conn.execute('SELECT 1').fetchall()
    closed_a1 = int(conn.closed)
    conn.close()
    log('  A1 裸 connect（无池）+ `with conn:` 出块之后 closed=%d（期望非 0：出块即关连接）' % closed_a1)
    if not judge_exit_closes(closed_a1):
        return ('A1：无池时 `with conn:` 出块**没有**关连接 —— 与 pgstore 那条注释赖以成立的前提相反，'
                '先搞清驱动版本再谈别的')

    # A 段的小表：只为一件事存在 —— 让「整批提交了没有」由**另一个进程**来数
    rc, out = psql_run(env, sql='CREATE TABLE IF NOT EXISTS %s (tag text NOT NULL, n integer NOT NULL)'
                       % TXN_TABLE)
    if rc != 0:
        return 'A1 附加：建 %s 失败（rc=%d）：%s' % (TXN_TABLE, rc, out.strip()[-300:])

    # A2：默认 autocommit=False，**先裸读一次**（附录 B18 的调用次序）⇒ transaction() 退化
    c2 = psycopg.connect(dsn)
    before = tx_status_name(c2.info.transaction_status)
    c2.execute('SELECT 1').fetchall()
    after = tx_status_name(c2.info.transaction_status)
    log('  A2 默认 autocommit=False：裸读前后事务状态 %s -> %s（期望 IDLE -> INTRANS）'
        % (before, after))
    if before != 'IDLE' or after != 'INTRANS':
        return 'A2：事务状态不是 IDLE -> INTRANS（读到 %s -> %s）⇒ 复演的调用次序没复现出来' % (before, after)
    with c2.transaction():
        c2.execute("INSERT INTO %s (tag, n) VALUES ('A2', 1)" % TXN_TABLE)
    c2.close()
    n_a2 = psql_expect(env, "SELECT count(*) FROM %s WHERE tag = 'A2'" % TXN_TABLE)
    if n_a2 is None:
        return 'A2：另一个进程数不出 %s 的行数（读数取不到 ≠ 0）' % TXN_TABLE
    log('  A2 退化的事务块里那条 INSERT：另一个进程（容器内 psql）数到 %d 行（期望 0）' % n_a2)
    if n_a2 != 0:
        return 'A2：退化的事务块里的 INSERT 竟然被另一个进程读到了 %d 行 ⇒ 没复现出「整批不提交」' % n_a2

    # A3：同一个次序但**不**前置裸读 ⇒ 真 BEGIN…COMMIT。它同时是量具校准：
    #     「本来就该读到 1」的样本必须读到 1，否则 A2 的 0 只是「量具坏了」。
    c3 = psycopg.connect(dsn)
    enter = tx_status_name(c3.info.transaction_status)
    if enter != 'IDLE':
        return 'A3：进事务块前连接不在 IDLE（读到 %s）⇒ 对照条件不成立' % enter
    with c3.transaction():
        c3.execute("INSERT INTO %s (tag, n) VALUES ('A3', 1)" % TXN_TABLE)
    c3.close()
    n_a3 = psql_expect(env, "SELECT count(*) FROM %s WHERE tag = 'A3'" % TXN_TABLE)
    if n_a3 is None:
        return 'A3：另一个进程数不出行数（读数取不到 ≠ 0）'
    log('  A3 不前置裸读（进块时 IDLE）：同一个数法读到 %d 行（期望 1 —— 量具校准）' % n_a3)
    if n_a3 != 1:
        return ('A3：同一个数法在「本来就该读到 1」的样本上读到 %d ⇒ 量具本身有问题，'
                'A2 那个 0 不能当作「没提交」的证据' % n_a3)
    return ''


def section_b(log, env):
    """真池：出块不关（归还）· 摘掉 `_pool` 就关（归因）· 池化连接上写入路径仍真提交 · close_returns 分叉。"""
    psycopg = env['psycopg']
    pool_mod = env['pool']
    dsn = env['dsn']
    if pool_mod is None:
        return 'B：本解释器没有 psycopg_pool（这一档应先退 NO ENV，不该走到这里）'

    from quanauto.datacenter import DailyBar
    from quanauto.pgstore import PgBarIngestor, PsycopgConnection

    from psycopg.rows import dict_row
    pool_kwargs = {'row_factory': dict_row, 'autocommit': True}
    env['pool_kwargs'] = pool_kwargs

    pool = pool_mod.ConnectionPool(dsn, min_size=0, max_size=3, kwargs=pool_kwargs, open=True)
    pool.wait(timeout=30)
    try:
        c1 = pool.getconn()
        # B0：整段结论的前提 = 拿到的真是**池化**连接
        if not judge_pool_attached(getattr(c1, '_pool', None), pool):
            return ('B0：pool.getconn() 拿到的连接没有 `_pool` ⇒ 「池化连接」这个前提不成立，'
                    'B 段整套结论无意义')
        with c1:
            c1.execute('SELECT 1').fetchall()
        closed_b1 = int(c1.closed)
        log('  B1 池化连接 + `with conn:` 出块之后 closed=%d（期望 0：归还而不是关掉）' % closed_b1)
        if judge_exit_closes(closed_b1):
            return 'B1：池化连接被 `with conn:` 关掉了 —— 与 Connection.__exit__ 的源码分支相反'

        # B2：归还证据 —— 池自称手里又有一条可借的连接
        pool.putconn(c1)
        stats = pool.get_stats()
        available = stats.get('pool_available')
        log('  B2 归还后池自报 pool_size=%s pool_available=%s（期望 available >= 1）'
            % (stats.get('pool_size'), available))
        if available is None or int(available) < 1:
            return 'B2：归还后池自报手里没有可用连接（available=%r）⇒ 「归还」这一步没验到' % (available,)
        c1b = pool.getconn()
        log('    （同一对象再借出来：%s —— 观察，不作判据）' % (c1b is c1))

        # B3：对照 —— 同一个对象、只把 `_pool` 摘掉，出块**就**关
        c2 = pool.getconn()
        c2._pool = None
        with c2:
            c2.execute('SELECT 1').fetchall()
        closed_b3 = int(c2.closed)
        log('  B3 同一条池化连接、只摘掉 `_pool` 之后 closed=%d（期望非 0：把 B1 归因到那个属性）'
            % closed_b3)
        if not judge_exit_closes(closed_b3):
            return ('B3：摘掉 `_pool` 之后出块仍然不关 ⇒ B1 的「不关」另有原因，'
                    '读数不能归因到 `_pool`')

        # B4：把真池化连接**注入**产品连接对象，走产品写入路径
        pc = PsycopgConnection(dsn)
        pc._conn = c1b
        bar = DailyBar(symbol=SYMBOL, trade_date=BAR_DATE,
                       open=Decimal('10.0000'), high=Decimal('10.5000'),
                       low=Decimal('9.9000'), close=Decimal('10.2500'),
                       volume=Decimal('1000000.0000'), amount=Decimal('10250000.0000'),
                       source='probe-driver', data_version=VERSION)
        report = PgBarIngestor(pc).upsert_daily_bars([bar])
        closed_after = int(pc._conn.closed)
        n_b4 = psql_expect(env, "SELECT count(*) FROM dc_daily_bar WHERE data_version = '%s'"
                           % VERSION)
        log('  B4 池化连接上 upsert_daily_bars：inserted=%d skipped=%d；事务之后 closed=%d；'
            '另一个进程数到 %s 行（期望 nonzero=1、closed=0）'
            % (report.inserted, report.skipped, closed_after, n_b4))
        if n_b4 != 1:
            return ('B4：池化连接上写入路径提交的行数不是 1（读到 %s）⇒ '
                    '「池化条件下事务仍真提交」不成立' % (n_b4,))
        if report.inserted != 1 or report.skipped != 0:
            return 'B4：写入路径自报 inserted=%d skipped=%d，与期望 1/0 不符' % (report.inserted,
                                                                          report.skipped)
        if judge_exit_closes(closed_after):
            return 'B4：`transaction()` 之后池化连接被关掉了'
        pool.putconn(c1b)

        # B5：`close_returns=True` —— 「归还入池」到底是谁提供的（驱动还是池）
        ver = psycopg.__version__
        pool_ver = getattr(pool_mod, '__version__', None)
        want_cls = expected_pool_connection_class(ver)
        if want_cls is None:
            return 'B5：读不出 psycopg 版本号 ⇒ 「该用哪个连接类」无从判'
        try:
            pool_cr = pool_mod.ConnectionPool(dsn, min_size=0, max_size=2, kwargs=pool_kwargs,
                                              close_returns=True, open=True)
            pool_cr.wait(timeout=30)
        except (TypeError, ValueError) as exc:
            # 池 < 3.3 根本没这个开关（未知 kwarg ⇒ TypeError）；传了非标准 connection_class
            # 且驱动 < 3.3 时池也会 raise TypeError。这不是「测不了」，而是**本条支在这个池
            # 版本上不存在** ⇒ 记成读数、由 B6 那条默认池的读顶上。但若驱动已 >= 3.3 还拒，
            # 就与源码不符 ⇒ 必须 FAIL，否则「拒绝」会被人当成免检通道。
            if want_cls == 'Connection':
                return ('B5：psycopg %s 已经 >= 3.3，池却仍然拒绝 close_returns=True（%s：%s）'
                        '⇒ 与 psycopg_pool 的版本校验不符' % (ver, type(exc).__name__, exc))
            log('  B5 psycopg %s + psycopg-pool %s：池**不接受** close_returns（%s：%s）'
                '⇒ 本条支在这个池版本上不存在；本版本「池化连接 close()」由 B6 那条默认池读'
                % (ver, pool_ver, type(exc).__name__, exc))
        else:
            try:
                c3 = pool_cr.getconn()
                cls_name = type(c3).__name__
                c3.close()
                closed_b5 = int(c3.closed)
            finally:
                pool_cr.close()
            log('  B5 psycopg %s + psycopg-pool %s 的 `close_returns=True` 池：连接类 %s'
                '（按驱动版本期望 %s）；conn.close() 之后 closed=%d（期望 0：归还入池）'
                % (ver, pool_ver, cls_name, want_cls, closed_b5))
            if cls_name != want_cls:
                return ('B5：连接类与源码不符 —— psycopg %s 上期望 %s，拿到 %s'
                        % (ver, want_cls, cls_name))
            if judge_exit_closes(closed_b5):
                return ('B5：池写了 close_returns=True，close() 却把连接真关了（closed=%d）'
                        '⇒ 与 PoolConnection / Connection.close() 的源码不符' % closed_b5)

        # B6：默认池（close_returns=False）里 close() **真关**（即使它属于池）
        pool2 = pool_mod.ConnectionPool(dsn, min_size=0, max_size=2, kwargs=pool_kwargs, open=True)
        pool2.wait(timeout=30)
        try:
            c4 = pool2.getconn()
            c4.close()
            closed_b6 = int(c4.closed)
        finally:
            pool2.close()
        log('  B6 默认池（close_returns=False）里 conn.close() 之后 closed=%d'
            '（期望非 0：真关，即使它属于池）' % closed_b6)
        if not judge_exit_closes(closed_b6):
            return 'B6：默认池里 close() 竟然没关连接 ⇒ 与 Connection.close() 的源码不符'
    finally:
        pool.close()
    return ''


def section_c(log, env):
    """版本写进读数：本解释器 + 声明下限并排（下限 ≠ 已验证到这个版本）。"""
    psycopg = env['psycopg']
    pool_mod = env['pool']
    floor = extract_driver_floor(read_text(os.path.join(ROOT, 'pyproject.toml')))
    if floor is None:
        return ('C：从 pyproject.toml 里提取不到 [postgres] extra 的驱动下限'
                '（提取为空不许当成「一致」）')
    ver = psycopg.__version__
    if parse_version(ver) is None:
        return 'C：psycopg 版本号 %r 解析不出 ⇒ 「换版本要重测」这句话连锚点都没有' % (ver,)
    env['readings'].update({
        'python': '%d.%d.%d' % sys.version_info[:3],
        'psycopg': ver,
        'psycopg_pool': getattr(pool_mod, '__version__', None) if pool_mod else None,
        'floor': floor,
    })
    log('  C 本解释器：Python %s / psycopg %s / psycopg-pool %s；'
        'pyproject 声明的下限是 psycopg[binary]>=%s'
        % (env['readings']['python'], ver, env['readings']['psycopg_pool'], floor))
    log('    （B5 按**本版本**算：psycopg %s ⇒ close_returns=True 时池该拿连接类 %s；'
        '池版本 < 3.3 根本没这个开关时，B5 把它记成读数而不是失败）'
        % (ver, expected_pool_connection_class(ver)))
    return ''


def section_d(log, env):
    """本仓库用得到吗：零池引用 / 实现约束仍在 / 声明下限现取。"""
    names = sorted(p.name for p in Path(ROOT, 'quanauto').glob('*.py'))
    if not names:
        return 'D1：quanauto/ 下一个 .py 都数不到 ⇒ 这是在空转里报 OK，拒绝通过'
    hits = 0
    for name in names:
        text = read_text(os.path.join(ROOT, 'quanauto', name))
        got = count_occurrences(text, 'psycopg_pool')
        got2 = count_occurrences(text, 'ConnectionPool')
        if got is None or got2 is None:
            return 'D1：读不到 quanauto/%s ⇒ 「零池引用」无从判（读不到 ≠ 0 次）' % name
        hits += got + got2
    log('  D1 quanauto/ 下 %d 个 .py 里 `psycopg_pool` / `ConnectionPool` 共 %d 次'
        '（期望 0：本仓库不建池）' % (len(names), hits))
    if hits != 0:
        return ('D1：quanauto/ 里出现了池引用 %d 次 ⇒ 「本仓库不建池」这句话过期了，'
                'B 段的结论要按「产品侧也用池」重取' % hits)

    source = read_text(os.path.join(ROOT, 'quanauto', 'pgstore.py'))
    if source is None:
        return 'D2：读不到 quanauto/pgstore.py ⇒ 实现约束无从判'
    shapes = count_ast_shapes(source)
    if shapes is None:
        return 'D2：quanauto/pgstore.py 解析不了（SyntaxError）⇒ 形状判据无从下手'
    with_names, tx_names = shapes
    log('  D2 pgstore.py 的 AST 形状：`with <名字>:` %d 处 %s；`<名字>.transaction()` %d 处 %s'
        % (len(with_names), with_names, len(tx_names), tx_names))
    if 'conn' in with_names:
        return ('D2：pgstore.py 里出现了 `with conn:` 形状 ⇒ 注释点名的那条实现约束被破坏'
                '（而且有池/无池的后果不同 ⇒ 必须重测）')
    if 'conn' not in tx_names:
        return 'D2：pgstore.py 里没有 `conn.transaction()` 形状 ⇒ 注释点名的那条 API 不见了'
    if not with_names and not tx_names:
        return 'D2：两种形状都是 0 处 ⇒ 提取失配（不是「干净」），拒绝通过'

    floor = extract_driver_floor(read_text(os.path.join(ROOT, 'pyproject.toml')))
    if floor is None:
        return 'D3：从 pyproject.toml 提取不到 [postgres] 的驱动下限'
    log('  D3 pyproject.toml 的 [postgres] extra 明写 psycopg[binary]>=%s'
        '（那是**下限**，不是「已验证到这个版本」）' % floor)
    return ''


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def guard_empty_db(log, env):
    """空库守卫：DDL 全是 `CREATE TABLE IF NOT EXISTS` ⇒ 非空库上会静默跳过、与触测一起假绿。"""
    tables = psql_expect(env, "SELECT count(*) FROM information_schema.tables "
                              "WHERE table_schema = 'public'")
    if tables is None:
        return 'GUARD：数不出 public schema 的表数（读数取不到 ≠ 0）'
    if tables != 0:
        return 'GUARD：库不是空的（public 下 %d 张表）⇒ DDL 会静默跳过' % tables
    log('  空库守卫：public schema 下 0 张表 OK')
    return ''


def guard_encoding(log, env):
    rc, out = psql_run(env, sql="SELECT current_setting('server_encoding') || '/' || "
                                "current_setting('client_encoding')")
    text = out.strip().splitlines()[-1].strip() if out.strip() else ''
    log('  编码守卫：server/client = %s（期望 UTF8/UTF8）' % text)
    if rc != 0 or text != 'UTF8/UTF8':
        return 'GUARD：编码不是 UTF8/UTF8（读到 %r）' % text
    return ''


def apply_ddl(log, env):
    """把**真的** `db/data_center.sql` 拷进容器再执行（不喂 stdin：`run()` 固定 DEVNULL）。"""
    rc, out = run(['docker', 'cp', DDL_PATH, CONTAINER + ':/tmp/data_center.sql'], timeout=180)
    if rc != 0:
        return 'GUARD：docker cp db/data_center.sql 失败（rc=%d）：%s' % (rc, out.strip()[-300:])
    rc, out = psql_run(env, path='/tmp/data_center.sql', timeout=600)
    if rc != 0:
        return 'GUARD：应用 db/data_center.sql 失败（rc=%d）：%s' % (rc, out.strip()[-400:])
    log('  已应用 db/data_center.sql（容器内 /tmp/data_center.sql）')
    return ''


def body(log, env):
    """连接 + 三条守卫 + 四段。返回 (verdict, exit_code)。"""
    env['psycopg'] = env['psycopg_mod']
    rc, out = psql_run(env, sql='SELECT version()')
    if rc == 0 and out.strip():
        log('  %s' % out.strip().splitlines()[-1].strip())
    for guard in (guard_encoding, guard_empty_db, apply_ddl):
        why = guard(log, env)
        if why:
            log('  [GUARD] %s' % why)
            return 'GUARD_FAIL', EXIT_GUARD

    steps = (
        ('A 段（无池的源事实）', section_a),
        ('B 段（真池）', section_b),
        ('C 段（版本）', section_c),
        ('D 段（本仓库用得到吗）', section_d),
    )
    for name, fn in steps:
        why = fn(log, env)
        if why:
            log('  [%s] FAIL —— %s' % (name, why))
            return 'FAIL:%s' % name.split()[0], EXIT_FAIL
        log('  [%s] OK' % name)

    readings = env.get('readings') or {}
    log('  读数：Python %s / psycopg %s / psycopg-pool %s / 声明下限 psycopg[binary]>=%s'
        % (readings.get('python'), readings.get('psycopg'), readings.get('psycopg_pool'),
           readings.get('floor')))
    log('')
    log('边界（本报告不主张什么）：')
    log('  1. 这些读数只对上面那个镜像 digest + 上面那两个驱动版本成立；**不**主张')
    log('     「PostgreSQL 14+ 全都这样」（那是 J-7 的四个 tag 阶梯，不是区间）。')
    log('  2. 产品侧**没有**连接池：D1 现数是零引用。B4 那条池化连接是**注入**进')
    log('     PsycopgConnection._conn 的，用来在池化条件下复核既有写入路径，')
    log('     不等于「产品侧已经有池」。')
    log('  3. 没有测并发（多线程压同一个连接）、没有测池的等待/超时、没有测长连接或生产场景。')
    log('  4. 第二版本是**另一次运行**产生的另一份快照：同一个探针字节 + 另一个解释器，')
    log('     读法 = 比对两份报告里的版本行与同一批判据的结论。')
    return 'PASS', EXIT_OK


def build_report(log, env, verdict, exit_code):
    lines = ['SCOPE: probe_pg_driver（A 段无池源事实 / B 段真池 / C 段版本 / D 段本仓库用得到吗）'
             '-- 探针，不是门禁。',
             'GENERATED: %s' % time.strftime('%Y-%m-%d %H:%M:%S'),
             'IMAGE: %s digest=%s' % (env.get('image'), (env.get('digest') or '(未取到)')[:48])]
    readings = env.get('readings') or {}
    lines.append('HOST: python=%s psycopg=%s psycopg-pool=%s declared-floor=%s'
                 % (readings.get('python'), readings.get('psycopg'),
                    readings.get('psycopg_pool'), readings.get('floor')))
    if verdict != 'PASS':
        lines.append('（本次没跑完：上面的版本行可能是空的；下面正文里写到哪算哪）')
    lines.append('')
    lines.extend(log.lines)
    lines.append('')
    lines.append('verdict: %s' % verdict)
    lines.append('exit code: %d' % exit_code)
    lines.append('NOTE: 结论只对这个镜像 digest + 这个驱动版本 + 下面钉住的那些文件字节成立。')
    lines.append('      报告里**不**钉 git commit（只钉文件 sha256），所以它可以和代码同批提交。')
    lines.append('      判据有牙由**外部**变异证伪（不在仓库里，只在 %TEMP% 里跑过那一次）：把 '
                 'expected_pool_connection_class 压成常量 / 把 AST 形状判据换成文本匹配 / '
                 '把 count_occurrences 的 None 静默当 0 ⇒ 探针自测各 4/4/1 项变红、exit=1；'
                 '还原后 exit=0。变异脚本本身**不是**可复跑产物，这也是这条探针不是门禁的一部分。')
    for rel in PINNED_FILES:
        digest = sha256_file(os.path.join(ROOT, rel))
        lines.append('PINNED %s sha256=%s' % (rel, digest or '(读不到)'))
    self_path = os.path.abspath(__file__)
    lines.append('probe: tools/%s sha256=%s' % (os.path.basename(self_path),
                                                sha256_file(self_path) or '(读不到)'))
    return '\n'.join(lines) + '\n'


def write_report(text):
    try:
        with open(REPORT_PATH, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        return None
    except OSError as exc:
        return '报告写不出去（%s）：%s' % (REPORT_PATH, exc)


def finish(log, env, verdict, code):
    why = write_report(build_report(log, env, verdict, code))
    if why:
        log(why)
        if code == EXIT_OK:
            return 'GUARD_FAIL', EXIT_GUARD
    else:
        log('报告已写：%s' % REPORT_PATH)
    return verdict, code


def parse_args(argv):
    parser = argparse.ArgumentParser(description='连接与驱动的行为探针（J-15）')
    parser.add_argument('--pg-image', default=DEFAULT_IMAGE)
    parser.add_argument('--report', default='')
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--selftest', action='store_true')
    return parser.parse_args(argv)


def main(argv=None):
    harden_stdout()
    log = Log()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        log('probe_pg_driver --selftest（只跑纯函数样本：不碰 docker / 网络 / 真库）')
        code, bad = selftest(log)
        log('')
        log('note: 本次运行**只**判自测；探针在真库上的结论由不带 --selftest 的那次运行负责判。')
        return code

    global REPORT_PATH
    if args.report:
        path, why = resolve_report_path(args.report)
        if path is None:
            log('GATE FAIL: %s' % why)
            log('（--report 不合法时**不**回退到默认落点：那会覆盖上一轮的证据）')
            return EXIT_GUARD
        REPORT_PATH = path
    log('探针：连接与驱动行为（J-15 剩下两半）报告 -> %s' % REPORT_PATH)

    env = {}

    # 1) 环境：docker
    rc, out = run(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=120)
    if rc != 0:
        log('NO ENV: docker 不可用（rc=%d）：%s' % (rc, out.strip()[:200]))
        return EXIT_NO_ENV
    env['docker'] = out.strip().splitlines()[-1].strip() if out.strip() else ''
    log('docker server: %s' % env['docker'])

    # 2) 环境：镜像
    rc, out = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.pg_image], timeout=120)
    if rc != 0:
        log('NO ENV: 本机没有镜像 %s（先 docker pull %s）' % (args.pg_image, args.pg_image))
        return EXIT_NO_ENV

    # 3) 环境：驱动（宿主侧）。两个都要：B 段的主靶子是**有池**时的行为。
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:
        log('NO ENV: 宿主解释器 %s 没有 psycopg（%s）' % (sys.executable, exc))
        log("       装法：pip install \"psycopg[binary]>=3.1\"")
        return EXIT_NO_ENV
    env['psycopg_mod'] = psycopg
    try:
        import psycopg_pool
        env['pool'] = psycopg_pool
    except ImportError:
        env['pool'] = None
        log('NO ENV: 宿主解释器没有 psycopg-pool —— B 段是本报告的主靶子，缺它整份证据不成立。')
        log('       装法：pip install psycopg-pool（本仓库**不**声明这个依赖：产品侧不建池，'
            '它只被这条探针用到）')
        return EXIT_NO_ENV

    # 4) 容器
    started, kind, why = start_container(log, args.pg_image)
    if kind:
        log('%s: %s' % (kind, why))
        teardown(log, args.keep)
        return EXIT_GUARD if kind == 'GUARD' else EXIT_NO_ENV
    env.update(started)
    log('容器 %s 就绪：%s digest=%s' % (CONTAINER, env['dsn'], env['digest'][:24]))

    try:
        verdict, code = body(log, env)
    except Exception as exc:  # noqa: BLE001 —— 探针崩了也要留下一份说明它崩在哪的报告
        log('探针自身异常：%s: %s' % (type(exc).__name__, exc))
        verdict, code = 'GUARD_FAIL', EXIT_GUARD
    finally:
        teardown(log, args.keep)
    return finish(log, env, verdict, code)[1]


# ---------------------------------------------------------------------------
# 自测：只测纯函数（不碰 docker / 网络 / 真库）
# ---------------------------------------------------------------------------

def selftest(log):
    """纯函数样本。返回 (exit_code, n_bad)：**退出码只报自测结论**。

    样本分三类（缺一类这套自测就不算数）：① 目标断言的正样本（该判红的确实判红）；
    ② **边界与 None**（`0` 与「读不到」必须分开、`3.3` 是 `close_returns` 的分界、
    `with open(...)` 不算 `with conn:`）；③ **变异**（在内存里把真实现弄坏，
    确认判据真的变红 —— 见下面那两条 `变异：…` 与真文件样本）。
    """
    bad = []
    total = []

    def check(name, got, want):
        total.append(name)
        if got == want:
            log('  ok   %s' % name)
        else:
            bad.append(name)
            log('  FAIL %s: got=%r want=%r' % (name, got, want))

    # count_occurrences：读不到（None）与数到 0 必须分开
    check('count_occurrences POS two', count_occurrences('a-b-a', 'a'), 2)
    check('count_occurrences POS zero', count_occurrences('abc', 'z'), 0)
    check('count_occurrences NEG none', count_occurrences(None, 'a'), None)
    check('count_occurrences POS empty-text', count_occurrences('', 'a'), 0)

    # parse_version
    check('parse_version 3.3.6', parse_version('3.3.6'), (3, 3))
    check('parse_version 3.2.13', parse_version('3.2.13'), (3, 2))
    check('parse_version leading-space', parse_version('  3.14'), (3, 14))
    check('parse_version one-part', parse_version('3'), None)
    check('parse_version empty', parse_version(''), None)
    check('parse_version None', parse_version(None), None)
    check('parse_version junk', parse_version('x.y'), None)

    # expected_pool_connection_class：3.3 是分界，且**读不出就返回 None**
    check('close_returns 3.3.6 -> 驱动自带 Connection', expected_pool_connection_class('3.3.6'),
          'Connection')
    check('close_returns 3.3.0 -> 驱动自带 Connection', expected_pool_connection_class('3.3.0'),
          'Connection')
    check('close_returns 3.2.13 -> 池顶上 PoolConnection',
          expected_pool_connection_class('3.2.13'), 'PoolConnection')
    check('close_returns 3.2 -> 池顶上 PoolConnection',
          expected_pool_connection_class('3.2'), 'PoolConnection')
    check('close_returns 4.0 -> 驱动自带 Connection', expected_pool_connection_class('4.0'),
          'Connection')
    check('close_returns junk -> None', expected_pool_connection_class('junk'), None)
    check('close_returns None -> None', expected_pool_connection_class(None), None)

    # extract_driver_floor
    real = read_text(os.path.join(ROOT, 'pyproject.toml'))
    check('floor 真 pyproject', extract_driver_floor(real), '3.1')
    check('floor 少了那行', extract_driver_floor('[project]\nname="x"\n'), None)
    check('floor 空串', extract_driver_floor(''), None)
    check('floor None', extract_driver_floor(None), None)
    check('floor 换了下限', extract_driver_floor('postgres = ["psycopg[binary]>=4.2"]'), '4.2')

    # judge_exit_closes / judge_pool_attached
    check('judge_exit_closes 0 -> 没关', judge_exit_closes(0), False)
    check('judge_exit_closes 1 -> 关了', judge_exit_closes(1), True)
    check('judge_exit_closes True -> 关了', judge_exit_closes(True), True)
    try:
        judge_exit_closes('x')
        raised = False
    except ValueError:
        raised = True
    check('judge_exit_closes 非整数 -> ValueError（不静默当 0）', raised, True)
    sentinel = object()
    check('judge_pool_attached 同一个池', judge_pool_attached(sentinel, sentinel), True)
    check('judge_pool_attached None', judge_pool_attached(None, sentinel), False)
    check('judge_pool_attached 别的池', judge_pool_attached(object(), sentinel), False)
    check('judge_pool_attached 池为 None', judge_pool_attached(sentinel, None), False)

    # tx_status_name：两种呈现形状都要认（3.11+ 的 IntEnum 给的是数字）
    check('tx_status 枚举名', tx_status_name('TransactionStatus.INTRANS'), 'INTRANS')
    check('tx_status 裸名', tx_status_name('IDLE'), 'IDLE')
    check('tx_status 带空格', tx_status_name('  TransactionStatus.IDLE '), 'IDLE')
    check('tx_status IntEnum 数字 2 -> INTRANS', tx_status_name(2), 'INTRANS')
    check('tx_status IntEnum 数字 0 -> IDLE', tx_status_name(0), 'IDLE')
    check('tx_status 未知数字', tx_status_name(9), 'UNKNOWN(9)')
    check('tx_status 字符串数字', tx_status_name('2'), 'INTRANS')

    # count_ast_shapes：**这是本轮的关键判据**，样本要覆盖
    # ① 文本里有 `with conn:` 但只在 docstring 里（文本匹配会误报的那一类）
    src_doc = ('"""doc：绝不能写成 `with conn:`。"""\n'
               'def f(conn):\n'
               '    with conn.transaction():\n'
               '        pass\n')
    check('ast docstring 陷阱 -> with 0 处', count_ast_shapes(src_doc)[0], [])
    check('ast docstring 陷阱 -> tx 1 处', count_ast_shapes(src_doc)[1], ['conn'])
    # ② 真写了 `with conn:`
    check('ast with conn: 命中', count_ast_shapes('def f(conn):\n    with conn:\n        pass\n')[0],
          ['conn'])
    # ③ `with open(...)` 不算（context_expr 是 Call，不是名字）
    check('ast with open 不算', count_ast_shapes('with open("x") as fh:\n    pass\n')[0], [])
    # ④ 别的接收者的 transaction() 不算成 conn 的
    check('ast 别的接收者', count_ast_shapes('pool.transaction()\n')[1], ['pool'])
    # ⑤ 解析不了 -> None（不等于「0 处」）
    check('ast 语法错 -> None', count_ast_shapes('def f(:\n'), None)
    check('ast 空源码 -> 两处都 0', count_ast_shapes(''), ([], []))

    # 真文件上再核一次：这正是 D2 的形状判据
    real_pg = read_text(os.path.join(ROOT, 'quanauto', 'pgstore.py'))
    shapes = count_ast_shapes(real_pg or '')
    check('D2 真文件：with conn: 不出现', 'conn' in (shapes[0] if shapes else ['<解析不了>']), False)
    check('D2 真文件：conn.transaction() 在', 'conn' in (shapes[1] if shapes else []), True)
    check('D2 文本里有 docstring 那句（所以才必须用形状）',
          count_occurrences(real_pg, 'with conn:') > 0, True)

    # 变异：把真文件的实现改成 `with conn:`（在内存里改，不落盘）⇒ 形状判据必须变红
    mutated = (real_pg or '').replace('with conn.transaction():', 'with conn:')
    check('变异：改成 with conn: 之后命中', 'conn' in count_ast_shapes(mutated)[0], True)

    # free_port / 计数
    check('free_port 备用也能兜', free_port(0, ()) is not None, True)

    log('')
    log('自测：%d 项，%d 项 FAIL' % (len(total), len(bad)))
    return (EXIT_OK if not bad else EXIT_FAIL), bad


if __name__ == '__main__':
    sys.exit(main())
