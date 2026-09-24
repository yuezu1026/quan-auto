# -*- coding: utf-8 -*-
"""run_sql_smoke.py -- 在真实 PostgreSQL 上执行 db/*.sql 的 DDL + 触发测试。

为什么需要它
------------
tools/verify_risk_config.py 与 tools/verify_data_center.py 都是**纯静态**的：它们能证明
「约束语句还在、触发用例还在、覆盖齐、且用例断言了是哪条约束拒绝的」，但**证明不了约束真的
会拒绝非法值**，也证明不了某个样本值真的落在拒绝区间内。后者只能由真实执行给出。
截至本脚本诞生，30 条 CHECK 约束（风控 14 + 数据中心 16）全部处于 UNPROVEN 状态；
**2026-09-23 起这句话不再成立**（两份触发测试已跑过，且每一条命名 CHECK 都被
 tools/falsify_smoke.py 逐条证伪）—— 当前结论一律以 tools/sql-smoke-report.txt 与
tools/falsify-report.txt 这两份**快照**为准，改过任何 db/*.sql 或 *.smoke.sql 后即作废。

用法
----
    python tools/run_sql_smoke.py                 # 起临时库、跑两份 DDL+smoke、拆库、写报告
    python tools/run_sql_smoke.py --selftest       # 只测本脚本的判定逻辑，不需要 docker
    python tools/run_sql_smoke.py --keep           # 失败也保留容器，便于人工进容器排查
    python tools/run_sql_smoke.py --pg-image=postgres:16
    python tools/run_sql_smoke.py --pg-image=postgres:14 --report=tools/sql-smoke-report-pg14.txt

`--report=<相对仓库根的路径>` 用来**另存**证据。为什么需要它：本脚本的结论**只对跑过的那个
镜像成立**，所以要支撑「PostgreSQL 14+」这类跨版本声称，必须**逐版本各留一份快照**；
没有这个开关时第二次运行会原地**覆盖**第一次的证据，于是「14 通过了吗」这个问题永远
答不上来，而报告看起来依旧是绿的。不带 `--report` 时行为与以前**完全一致**
（默认仍写 tools/sql-smoke-report.txt）。

退出码
------
    0  全部 SMOKE PASS
    1  有 SMOKE FAIL（发现了纸面防线）
    2  前置守卫失败，或**无法判定**（空库守卫 / 编码断言 / 容器起不来 / 输出里没有判定标记）
    3  docker 不可用 —— 什么都没执行

    （注意：psql 在 ON_ERROR_STOP=1 下自己报 SQL 错也返回 3。那是 psql 的语义，本脚本
      不直接冒泡 psql 的退出码，只把它作为判定输入记录进日志。）

三种结局严格分开 —— 这是本脚本存在的意义
------------------------------------------
「没跑成」不等于「跑过了」，「跑过了」不等于「通过了」。所以 docker 没起时本脚本报
DOCKER UNAVAILABLE 并以非零退出，**绝不**返回 0；输出里读不到 SMOKE PASS / SMOKE FAIL 时
报 GUARD_FAIL，**绝不**当成通过（提取为空 = 拒绝判通过，与静态门禁同一条纪律）。

本脚本**故意不注册进 tools/run_all_gates.py**：那个 harness 必须能在没有 docker 的环境里
跑完（本仓库的现状就是如此）。把它塞进去只会得到两个烂结局 —— 要么因环境原因整条 FAIL，
要么被静默 SKIP 而报绿，后者正是本项目反复踩的假门禁。要集成的话，规则必须是
「不起 docker ⇒ 显式 SKIPPED，且总判据里带 skipped 计数」，那是一件单独的事。
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_FILE = os.path.join(ROOT, 'docker-compose.smoke.yml')
DEFAULT_REPORT_PATH = os.path.join(ROOT, 'tools', 'sql-smoke-report.txt')
# 可变：--report= 可以改它。默认值**一个字都没动**，所以不带该开关时与以前逐字节一致。
REPORT_PATH = DEFAULT_REPORT_PATH

DB_NAME = 'quan'
DB_USER = 'postgres'

# (标签, DDL 相对路径, 触发测试相对路径)。顺序有意义：DDL 必须先跑通，触发测试的
# A 段才有前置可查。
PAIRS = [
    ('risk-control', 'db/risk_control.sql', 'db/risk_control.smoke.sql'),
    ('data-center', 'db/data_center.sql', 'db/data_center.smoke.sql'),
]

EXIT_OK = 0
EXIT_SMOKE_FAIL = 1
EXIT_GUARD = 2
EXIT_NO_DOCKER = 3

SUMMARY_RE = re.compile(r'----\s*smoke summary:\s*(\d+)\s*passed,\s*(\d+)\s*failed\s*----')

ENV = os.environ.copy()


# ---------------------------------------------------------------------------
# 判定逻辑（纯函数，所以能自测）
# ---------------------------------------------------------------------------

def judge(rc, out):
    """把一次 psql 调用的 (退出码, 合并输出) 判成一个结论。

    返回 (verdict, why)，verdict ∈ {PASS, SMOKE_FAIL, GUARD_FAIL}。
    """
    has_pass = 'SMOKE PASS' in out
    has_fail = 'SMOKE FAIL' in out
    if has_pass and has_fail:
        return 'GUARD_FAIL', '输出里同时出现 SMOKE PASS 与 SMOKE FAIL，矛盾，拒绝判定'
    if not has_pass and not has_fail:
        return 'GUARD_FAIL', ('输出里既没有 SMOKE PASS 也没有 SMOKE FAIL -- 提取为空，'
                              '后续判断形同虚设，拒绝判通过')
    if has_fail:
        return 'SMOKE_FAIL', '触发测试报告存在纸面防线'
    if rc != 0:
        return 'GUARD_FAIL', '报了 SMOKE PASS 但 psql 退出码非 0（%d），两者矛盾' % rc
    return 'PASS', 'ok'


# ---------------------------------------------------------------------------
# 进程封装
# ---------------------------------------------------------------------------

def harden_stdout():
    """控制台是 cp936：输出里出现 GBK 之外的字符会让 Python 抛 UnicodeEncodeError，
    整份结论一起丢。与 run_all_gates.py 的 harden_stdout 同一条理由。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass


def run(argv, timeout=900):
    """返回 (rc, 输出)。stderr **合并**进 stdout：psql 的 NOTICE 走 stderr，
    不合并就会丢掉 smoke 的计数行与 PASS/FAIL 行。"""
    try:
        p = subprocess.run(argv, cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
    except FileNotFoundError:
        return 127, 'executable not found: %s' % argv[0]
    except subprocess.TimeoutExpired:
        return 124, 'TIMEOUT after %ds: %s' % (timeout, ' '.join(argv))
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def compose(*args, **kw):
    return run(['docker', 'compose', '-f', COMPOSE_FILE] + list(args), **kw)


def psql(sql=None, path=None, timeout=600):
    """全部走 `exec -T` 在容器内用 unix socket 连接：官方镜像 pg_hba 对 local 是 trust，
    因此不需要密码，也**不需要把 5432 发布到 host**。"""
    argv = ['docker', 'compose', '-f', COMPOSE_FILE, 'exec', '-T', 'db',
            'psql', '-v', 'ON_ERROR_STOP=1', '-U', DB_USER, '-d', DB_NAME]
    if sql is not None:
        argv += ['-tAc', sql]
    if path is not None:
        argv += ['-f', path]
    return run(argv, timeout=timeout)


def scalar(sql):
    """取标量查询的最后一行（psql 可能先吐几条 NOTICE）。"""
    rc, out = psql(sql=sql, timeout=180)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return rc, (lines[-1] if lines else ''), out


class Log(object):
    """既打印又留存，最后写成报告文件（与 gates-report.txt 同一约定：看报告，别重跑）。"""

    def __init__(self):
        self.lines = []

    def __call__(self, text=''):
        text = '' if text is None else str(text)
        try:
            print(text)
        except Exception:
            print(text.encode('ascii', 'replace').decode('ascii'))
        self.lines.append(text)

    def indent(self, text):
        for ln in str(text).splitlines():
            self('    ' + ln)

    def echo_markers(self, out, limit=40):
        """把判定相关的行回显出来，免得只看到一个结论而不知道它是怎么来的。"""
        shown = 0
        for ln in out.splitlines():
            s = ln.strip()
            if not s:
                continue
            if ('SMOKE' in s) or ('smoke summary' in s) or ('ERROR' in s):
                self('    | ' + s)
                shown += 1
                if shown >= limit:
                    self('    | ...(更多省略)')
                    break


def write_report(lines, verdict, exit_code):
    body = list(lines)
    body.append('')
    body.append('verdict: %s' % verdict)
    body.append('exit code: %d' % exit_code)
    body.append('NOTE: 本报告的结论只对上面记录的镜像版本 + digest 成立。')
    body.append('      只在一个版本上通过，不等于 DDL 声称的 PostgreSQL 14+ 全都通过。')
    with open(REPORT_PATH, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(body) + '\n')


def set_report_path(value):
    """解析 --report=<相对仓库根的路径>。返回 (path, None) 或 (None, 原因)。

    只做三件事，全部是「写不出去就别装作写出了」：① 相对路径按仓库根解析；
    ② 上级目录必须已存在（**不**自动 mkdir：路径打错时自动建目录会让人以为写对了）；
    ③ 不能指向目录。
    """
    global REPORT_PATH
    raw = (value or '').strip()
    if not raw:
        return None, '--report 后面是空的'
    path = raw if os.path.isabs(raw) else os.path.join(ROOT, raw)
    path = os.path.normpath(path)
    if os.path.isdir(path):
        return None, '--report 指向的是一个目录：%s' % path
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return None, '--report 的上级目录不存在：%s' % parent
    REPORT_PATH = path
    return path, None


def teardown(log, keep):
    if keep:
        log('')
        log('--keep: 容器保留。人工排查：')
        log('    docker compose -f docker-compose.smoke.yml exec db psql -U postgres -d %s' % DB_NAME)
        log('    排查完请务必手工 docker compose -f docker-compose.smoke.yml down -v')
        return
    rc, out = compose('down', '-v', '--remove-orphans', timeout=300)
    log('teardown: docker compose down -v -> exit %d' % rc)
    if rc != 0:
        log('  注意：拆库失败。残留的容器/卷会让下次运行的「空库守卫」报 FAIL，届时手工清理。')
        log.indent(out.strip()[:500])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    harden_stdout()
    if '--selftest' in sys.argv:
        return selftest()

    keep = '--keep' in sys.argv
    report_arg = None
    for arg in sys.argv[1:]:
        if arg.startswith('--pg-image='):
            ENV['PG_IMAGE'] = arg.split('=', 1)[1]
        elif arg.startswith('--report='):
            report_arg = arg.split('=', 1)[1]

    log = Log()
    log('sql-smoke: 在临时 PostgreSQL 上执行 db/*.sql，把 UNPROVEN 变成有证据的结论。')
    log('理由：静态门禁只能证明约束**还写着**，只有真实数据库能证明约束**会拒绝**。')
    log('')

    # --- 0. 报告落点 ------------------------------------------------------
    # 这一步**故意**在 write_report 之前失败时直接 return，不调用 finish()：
    # 参数写错时退回默认路径去写，会把上一轮（例如 postgres:17 那份）证据**覆盖掉**，
    # 而且覆盖它的还是一份「本次没跑成」的报告。宁可什么都不写。
    if report_arg is not None:
        new_path, why = set_report_path(report_arg)
        if new_path is None:
            log('GATE FAIL: --report 不可用 -- %s' % why)
            log('          报告写不到指定位置时**不会**退回默认路径：那会覆盖上一轮的证据，')
            log('          而且覆盖它的还是一份「本次没跑成」的报告。故本次不写任何报告。')
            return EXIT_GUARD
        log('report target: %s' % new_path)
        log('              （默认落点是 %s；本次是另存，不会动它）' % DEFAULT_REPORT_PATH)
        log('')

    if not os.path.exists(COMPOSE_FILE):
        log('GATE FAIL: 缺少 %s' % COMPOSE_FILE)
        return finish(log, 'GUARD_FAIL：缺少 compose 文件', EXIT_GUARD)

    # --- 1. docker 可用性 -------------------------------------------------
    rc, out = run(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=120)
    if rc != 0:
        log('DOCKER UNAVAILABLE: docker daemon 没有应答。')
        log.indent(out.strip()[:800])
        log('')
        log('verdict: DOCKER UNAVAILABLE -- 本次什么都没执行，所以本次不产生任何结论。')
        log('         注意：不要把这句话读成「约束未经验证」。已经跑过的那一轮写在')
        log('         tools/sql-smoke-report.txt（快照，含镜像 digest）；本句只说「这一次没跑」。')
        return finish(log, 'DOCKER UNAVAILABLE', EXIT_NO_DOCKER)
    log('docker server version: %s' % out.strip())

    # --- 2. 起全新库 ------------------------------------------------------
    log('')
    log('--- 起全新库 ---')
    rc, out = compose('down', '-v', '--remove-orphans', timeout=300)
    log('pre-clean: down -v -> exit %d（没东西可拆时非 0 属正常，不判失败）' % rc)

    started = False
    result = ('GUARD_FAIL：脚本自身异常', EXIT_GUARD)
    try:
        rc, out = compose('up', '-d', '--wait', 'db', timeout=1200)
        if rc != 0:
            log('GATE FAIL: 数据库没起来（up -d --wait 退出码 %d）。' % rc)
            log.indent(out.strip()[:2000])
            result = ('GUARD_FAIL：容器起不来，本次无任何结论', EXIT_GUARD)
        else:
            started = True
            log('数据库已就绪（compose up -d --wait 返回 0）')
            result = body(log)
    except Exception as exc:                                    # noqa: BLE001
        log('UNEXPECTED ERROR: %r' % (exc,))
        log('（脚本自身出错，本次无任何结论 -- 不要把它读成「约束有问题」。）')
        result = ('GUARD_FAIL：脚本自身异常', EXIT_GUARD)
    finally:
        # 拆库必须放在这里：成功路径同样要拆，否则残留容器/卷会让下次运行的
        # 「空库守卫」报 FAIL，而且 PGDATA 会一直在盘上堆着。
        if started:
            teardown(log, keep)

    # finish 必须在这之后：报告要包含拆库那几行，否则报告缺一段过程。
    return finish(log, result[0], result[1])


def body(log):
    """起库之后的全部检查。返回 (verdict, exit_code)，不在这里拆库。"""
    # --- 3. 连接 + 编码 + 空库守卫 ---------------------------------------
    rc, val, out = scalar('SELECT 1')
    if rc != 0 or val != '1':
        log('GATE FAIL: 起来了但连不进去（SELECT 1 返回 %r）。' % val)
        log.indent(out.strip()[:800])
        return 'GUARD_FAIL：连不上刚起来的库', EXIT_GUARD

    log('')
    log('--- 前置守卫 ---')
    for var in ('server_encoding', 'client_encoding'):
        rc, val, out = scalar('SHOW %s' % var)
        if rc != 0 or val.upper() != 'UTF8':
            log('GATE FAIL: %s = %r，期望 UTF8。' % (var, val))
            log('         两份 SQL 都含中文注释；会话编码不对时中文可能被写坏或直接报')
            log('         invalid byte sequence，本次结果不可信，拒绝继续。')
            return 'GUARD_FAIL：编码不是 UTF8', EXIT_GUARD
        log('  %s = %s  OK' % (var, val))

    rc, val, out = scalar(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')")
    if rc != 0:
        log('GATE FAIL: 数不了 public schema 的表数。')
        log.indent(out.strip()[:500])
        return 'GUARD_FAIL：空库守卫无法执行', EXIT_GUARD
    if val != '0':
        log('GATE FAIL: 库不是空的 -- public schema 里已有 %s 张表。' % val)
        log('         两份 DDL 全用 CREATE TABLE IF NOT EXISTS：在**已有**的库上重跑会静默')
        log('         什么都不做，于是可能拿旧 schema 跑出「全绿」。这正是要防的假门禁，')
        log('         所以拒绝在非空库上判定。')
        log('         清理：docker compose -f docker-compose.smoke.yml down -v')
        return 'GUARD_FAIL：库不是空的', EXIT_GUARD
    log('  public schema 表数 = 0（全新库）  OK')

    # --- 4. 记录实测环境（证据，不是估算）--------------------------------
    # run() 只返回 (rc, 输出) 两个值；scalar() 返回三个。混用过一次，被 main 的
    # except 兜成 GUARD_FAIL —— 那是正确行为：宁可响一声，也不要静默出个绿。
    _, version, _ = scalar('SHOW server_version')
    image = digest = ''
    rc, cid, _ = scalar_cid()
    if rc == 0 and cid:
        rc2, image = run(['docker', 'inspect', '--format', '{{.Config.Image}}', cid])
        image = image.strip() if rc2 == 0 else ''
        if image:
            rc3, digest = run(['docker', 'image', 'inspect',
                               '--format', '{{index .RepoDigests 0}}', image])
            digest = digest.strip() if rc3 == 0 else ''
    if not image or not digest:
        log('WARNING: 取不到 image/digest —— 本次结论无法标注「在哪个镜像上验证过」。')
        log('         结论本身仍然有效（smoke 是 psql 真跑出来的），但缺口清单里只能写')
        log('         「已在 PostgreSQL %s 上验证」，不能写镜像摘要。' % (version or '?'))
    log('')
    log('--- 实测环境 ---')
    log('  image  : %s' % (image or '(未取到)'))
    log('  digest : %s' % (digest or '(未取到)'))
    log('  server : PostgreSQL %s' % (version or '(未取到)'))

    # --- 5. 逐对执行 ------------------------------------------------------
    results = []
    for tag, ddl, smoke in PAIRS:
        log('')
        log('--- %s ---' % tag)
        rc, out = psql(path='/work/' + ddl, timeout=900)
        if rc != 0:
            log('GATE FAIL: DDL 本身没干净跑完（%s，psql exit=%d）。' % (ddl, rc))
            log('          DDL 都没建好，触发测试无从谈起，故不继续跑它。')
            log.echo_markers(out)
            results.append((tag, 'GUARD_FAIL', 'DDL 未通过'))
            continue
        log('ddl applied: %s' % ddl)

        rc, out = psql(path='/work/' + smoke, timeout=900)
        verdict, why = judge(rc, out)
        m = SUMMARY_RE.search(out)
        counts = ('%s passed, %s failed' % m.groups()) if m else 'no summary line'
        log('smoke: %s -> %s (%s, psql exit=%d)' % (smoke, verdict, counts, rc))
        if why != 'ok':
            log('      判定依据: %s' % why)
        log.echo_markers(out)
        results.append((tag, verdict, counts))

    # --- 6. 总判据 --------------------------------------------------------
    log('')
    log('--- 汇总 ---')
    for tag, verdict, detail in results:
        log('  %-16s %-11s %s' % (tag, verdict, detail))

    log('')
    log('boundary: 本次证明只对 image=%s (digest=%s) 成立。' % (image or '?', digest or '?'))
    log('          它**不**证明「PostgreSQL 14+」都成立 -- 只测了上面这一个版本。')

    if any(v == 'GUARD_FAIL' for _, v, _ in results):
        return 'GUARD_FAIL（有前置失败或无法判定）', EXIT_GUARD
    if any(v == 'SMOKE_FAIL' for _, v, _ in results):
        log('boundary: 现在去分辨每个 FAIL 是「DDL 写错了」还是「样本写错了」。')
        log('          样本错了就改样本；**绝不为了让它过而削弱 CHECK**。')
        return 'SMOKE FAIL（存在纸面防线）', EXIT_SMOKE_FAIL

    log('boundary: 本次证明的是**约束的行为**，不是**契约的正确性**。若某条约束本身语义')
    log('          就写错了（例如区间定错），它一样会「正确地拒绝」了一个错误的样本。')
    return 'SMOKE PASS（全部数据库级守卫都真的会拒绝）', EXIT_OK


def scalar_cid():
    # 必须拆成两个 argv 元素：写成 compose('ps -q', 'db') 会让 docker 把 'ps -q' 当成
    # 一个子命令名而报错，于是 image/digest 静默变成空 —— 实测踩过（首次运行报告里
    # 就是 image=(未取到)）。
    rc, out = compose('ps', '-q', 'db', timeout=120)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return rc, (lines[-1] if lines else ''), out


def finish(log, verdict, code):
    try:
        write_report(log.lines, verdict, code)
    except Exception as exc:                                    # noqa: BLE001
        log('')
        log('GATE FAIL: 报告写不出去（%r）。' % (exc,))
        log('          target: %s' % REPORT_PATH)
        log('          结论没落盘 = 本次不产生任何结论。绝不能因为「进程跑完了」就当它通过。')
        # 本来就非 0 的结局保持原码；本来是 PASS 的必须**降级**，否则会留下一个
        # 「绿了但没证据」的结局 —— 那正是本项目反复要防的假绿。
        return code if code != EXIT_OK else EXIT_GUARD
    log('')
    log('report: %s -- 看它，不要为了看结果重跑（重跑要起容器）。' % REPORT_PATH)
    log('verdict: %s' % verdict)
    return code


# ---------------------------------------------------------------------------
# 自测：把判定逻辑的三种结局各构造一个样本（尤其是「无法判定」绝不能落进 PASS）
# ---------------------------------------------------------------------------

def selftest():
    ok = True

    def case(tag, rc, out, want):
        nonlocal ok
        got, why = judge(rc, out)
        hit = (got == want)
        print('  [%s] verdict=%s %s' % (tag, got, 'OK' if hit else 'MISSED (want %s)' % want))
        if not hit:
            print('        why=%s' % why)
        ok = ok and hit

    case('POS-real-shape', 0,
         'NOTICE:  ---- smoke summary: 24 passed, 0 failed ----\n'
         "NOTICE:  SMOKE PASS: all 24 database-level checks are effective\n", 'PASS')

    case('NEG-fail-marker', 3,
         'ERROR:  SMOKE FAIL: 2 of 24 database-level checks are ineffective\n', 'SMOKE_FAIL')

    # 最关键的一条：退出码非 0 但输出里没有任何判定标记 —— 必须拒绝判定，不能当成失败
    # 就完事、更不能当成通过（它可能只是连接失败，什么都没测）。
    case('NEG-rc-nonzero-no-marker', 1,
         'psql: error: connection to server on socket failed\n', 'GUARD_FAIL')

    # 退出码 0 但两个标记都没有 —— 空转。这是最隐蔽的假绿。
    case('NEG-rc-zero-no-marker', 0, 'NOTICE:  whatever\n', 'GUARD_FAIL')

    case('NEG-both-markers', 0, 'SMOKE PASS: x\nSMOKE FAIL: y\n', 'GUARD_FAIL')

    case('NEG-pass-but-rc-nonzero', 1,
         'NOTICE:  SMOKE PASS: all 24 database-level checks are effective\n', 'GUARD_FAIL')

    # --- --report 的解析守卫：坏样本 3 个 + 干净样本 2 个 --------------------
    # 只测「能解析出路径」是不够的：这个开关的**唯一**目的是别覆盖上一轮证据，
    # 所以重点是「坏参数必须拒绝，且**不得**动 REPORT_PATH」。全程不写任何文件。
    saved = REPORT_PATH

    def rcase(tag, value, want_ok):
        nonlocal ok
        path, why = set_report_path(value)
        got_ok = (path is not None)
        hit = (got_ok == want_ok)
        # 失败时全局必须原样不动 —— 否则「拒绝」只拒绝了一半。
        untouched = want_ok or (REPORT_PATH == saved)
        print('  [%s] ok=%s untouched=%s %s'
              % (tag, got_ok, untouched, 'OK' if (hit and untouched)
                 else 'MISSED (want_ok=%s)' % want_ok))
        if not hit:
            print('        why=%s' % why)
        globals()['REPORT_PATH'] = saved
        ok = ok and hit and untouched

    rcase('report-NEG-empty', '', False)
    rcase('report-NEG-is-a-directory', 'tools', False)
    rcase('report-NEG-parent-missing', 'tools/__selftest_no_such_dir__/x.txt', False)
    rcase('report-POS-relative', 'tools/__selftest_target__.txt', True)
    rcase('report-POS-absolute', os.path.join(ROOT, '__selftest_target__.txt'), True)
    assert REPORT_PATH == saved, '自测把 REPORT_PATH 弄脏了：%s' % REPORT_PATH

    print('SELFTEST %s: 判定逻辑的三种结局 + --report 落点守卫都有样本覆盖'
          % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
