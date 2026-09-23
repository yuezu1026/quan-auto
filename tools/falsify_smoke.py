# -*- coding: utf-8 -*-
"""falsify_smoke.py -- 证伪 db/*.smoke.sql：故意削弱一条约束，看触发测试是否会变红。

为什么必须有这个工具
--------------------
只跑「全绿」等于假门禁。一份 SMOKE PASS 有两种可能：

    (a) 约束真的会拒绝非法值（我们想要的结论）
    (b) 触发测试根本没在测（DO 块的异常分支从没进过、计数加错了、样本值其实合法……）

从输出上这两种**长得一模一样**。唯一能把它们分开的办法，是构造一个**已知有缺陷**
的 schema，确认触发测试会因此变红。这就是本工具。

手法：**放宽表达式，而不是删掉约束**
------------------------------------
删掉约束会让 smoke 的 A 段（存在性核对）先报 FAIL，于是「红了」这个结果分不清是
A 段还是 B 段抓到的 —— 一个探测器一个样本，这里要的是 B 段。所以本工具只把区间放宽到
刚好能让 B 段那个样本被接受（例如 roe 上界 5 -> 1000，样本 roe=600 便由拒绝变接受），
约束名保持不变 ⇒ A 段照旧通过 ⇒ 红的只可能是 B 段那一条。期望签名：
`n_pass = 总数-1, n_fail = 1`。

四个自 assert（少一个，结论就不可信）
------------------------------------
  1. 每个编辑的目标片段必须**恰好出现一次**，否则 exit 2
  2. 变异后的文件必须**真的变了**（逐字节比较），否则 exit 3
     —— 否则「全绿」是在**未经修改的文件**上得出的结论
  3. 每个案例开跑前 public schema 必须是**空的**，否则 exit 2
  4. 变异必须**真的落在数据库那个约束上**：跑完变异 DDL 后取回
     pg_get_constraintdef()，定义里必须出现该案例声明的 expect 片段，否则 exit 2

为什么要每案例清 schema（2026-09-23 实测撞到的坑）
--------------------------------------------------
db/*.sql 全部用 `CREATE TABLE IF NOT EXISTS`，种子行也 `ON CONFLICT DO NOTHING`。
第一个案例建好表之后，第二个案例再跑一份**改过约束的** DDL，会因为表已存在而
**整条语句静默空转** —— 表上还是旧约束。于是那个案例的 B 段样本照旧被拒、
触发测试照旧全绿 ⇒ 判定 MISSED。**这不是「触发测试没用」，是工具自己没把变异
送进数据库。** 之前只有 2 个案例、且分属两份 DDL（各建各的表名），所以侥幸没
暴露；扩到全量（现 31 个案例 / 30 条命名 CHECK）立刻会撞上 —— 而撞上的方式恰好是
「多报 MISSED」，方向安全但会让整个覆盖矩阵变成噪声。修法：每案例前
`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` 并断言表数为 0。

第 4 条自 assert 是第二道保险：清 schema 只能证明「DDL 被执行了」，不能证明
「改的正好是那个约束表达式」（比如片段命中了注释、或改错了表）。

expect 的比对先把定义归一化：PostgreSQL 会把它展开成 `'ACCOUNTS'::text`、
`(0)::numeric` 这种形式，拿源码原文去匹配**注定失败**（已实测）。归一化规则见
`_bare()`。这只是让断言能说人话；「变异真的改了文件」靠第 2 条的逐字节比较。

用法
----
    python tools/falsify_smoke.py            # 跑 CASES 里的全部案例
    python tools/falsify_smoke.py --list

当前覆盖
--------
31 个案例 / 30 条命名 CHECK（风控 14 + 数据中心 16；ck_risk_rule_ratio_range 上下界各一个案例）。
**不覆盖**的 3 个守门（靠 tools/verify_data_center.py 的 C7 核对，不靠本工具）：
risk_rule.threshold 的 NOT NULL、两个单例表的 PK、uq_dc_data_version_active 部分唯一索引。
它证明的是「放宽哪条就红哪条」（靠 `n_fail == 1` 这个签名），**不是**逐值证明
「每个样本值都真的落在拒绝区间内」。结果写 tools/falsify-report.txt（快照）。

退出码：0 每个案例都被抓到变红 / 1 有案例没被抓到（触发测试是摆设）/ 2 自 assert 失败
"""

import io
import os
import re
import shutil
import sys
import time

# 必须放在 import run_sql_smoke 之前：否则 tools/ 下会多出一个 __pycache__，
# 那是仓库里不该有的构建产物（实测踩过）。
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_sql_smoke import (                                    # noqa: E402
    EXIT_GUARD, EXIT_NO_DOCKER, EXIT_OK, EXIT_SMOKE_FAIL,
    ROOT, SUMMARY_RE, compose, psql, run, scalar,)

TMP_DIR = os.path.join(ROOT, 'tools', '_falsify_tmp')
REPORT = os.path.join(ROOT, 'tools', 'falsify-report.txt')


class SelfAssert(Exception):
    """自 assert 失败。与「案例没抓到」是两回事，退出码也不同（2 vs 1）。"""


# (tag, DDL, smoke, cname, edits, expect)
#
#   edits  : [(old, new), ...]。一个案例可能需要放宽**多处**：像
#            ck_dc_bar_ohlc_order 那样五个子句都会被同一个样本违反，
#            只放宽一个子句样本照样被拒 ⇒ 会误判成 MISSED。
#            每个 old 都必须单行、且在该 DDL 里**恰好出现一次**。
#   expect : 变异后 pg_get_constraintdef(cname) 里必须出现的片段。
#            这是「变异真的落到约束上」的凭据 —— 清 schema 只证明 DDL 跑了，
#            不证明改的是那个表达式（片段可能命中注释，或改错了表）。
#   cname  : 必须是 pg_constraint.conname 里的字面名字（不是 CREATE TABLE 里的别名）。
#
# 覆盖统计：31 个案例 / 30 条命名 CHECK（ck_risk_rule_ratio_range 上下界各一例）。
# 没覆盖的：PRIMARY KEY、uq_dc_data_version_active、uq_dc_quality_issue、
#           risk_rule.threshold 的 NOT NULL —— 本工具的手法只对 CHECK 表达式成立。
RISK_DDL = 'db/risk_control.sql'
RISK_SMOKE = 'db/risk_control.smoke.sql'
DC_DDL = 'db/data_center.sql'
DC_SMOKE = 'db/data_center.smoke.sql'

CASES = [
    # ---------------- 数据中心：16 条 CHECK，16 个案例 ----------------
    # 放宽 ck_dc_version_format 的锚定：去掉开头的 v 也可。B1 样本 '2026.09.23'
    # （上游把 v-号写成了点号）本该被拒。
    ('dc-version-format-anchor', DC_DDL, DC_SMOKE,
     'ck_dc_version_format',
     [(r"data_version ~ '^v[0-9]{4}\.[0-9]{2}\.[0-9]{2}$'",
       r"data_version ~ '^v?[0-9]{4}\.[0-9]{2}\.[0-9]{2}$'")],
     '^v?'),

    # 放宽退市日必须严格晚于上市日：相等也放行。B2 样本（同日上市退市）本该被拒。
    ('dc-symbol-delist-after-list', DC_DDL, DC_SMOKE,
     'ck_dc_symbol_delist_after_list',
     [('delist_date > list_date', 'delist_date >= list_date')],
     'delist_date >= list_date'),

    # 放宽价格必须为正：0 也放行。B3 样本（全 0 价）本该被拒。
    ('dc-bar-price-positive', DC_DDL, DC_SMOKE,
     'ck_dc_bar_price_positive',
     [('open > 0 AND high > 0 AND low > 0 AND close > 0',
       'open >= 0 AND high >= 0 AND low >= 0 AND close >= 0')],
     'open >= 0'),

    # 放宽 OHLC 序关系。B4 样本把 high/low 整个倒过来，五个子句同时被违反，
    # 所以必须**逐子句**给足容差：每个方向各留 1，倒置 0.5 元的行情即可通过。
    ('dc-bar-ohlc-order', DC_DDL, DC_SMOKE,
     'ck_dc_bar_ohlc_order',
     [('high >= low AND high >= open AND high >= close',
       'high >= low - 1 AND high >= open - 1 AND high >= close - 1'),
      ('AND low <= open AND low <= close',
       'AND low <= open + 1 AND low <= close + 1')],
     'low - 1'),

    # 放宽成交量/额非负：-1 也放行。B5 样本（volume = -1）本该被拒。
    ('dc-bar-volume-nonneg', DC_DDL, DC_SMOKE,
     'ck_dc_bar_volume_nonneg',
     [('volume >= 0 AND amount >= 0', 'volume >= -1 AND amount >= -1')],
     'volume >= -1'),

    # 放宽复权因子必须为正：0 也放行。B6 样本（factor = 0）本该被拒。
    ('dc-factor-positive', DC_DDL, DC_SMOKE,
     'ck_dc_factor_positive',
     [('adjust_factor > 0', 'adjust_factor >= 0')],
     'adjust_factor >= 0'),

    # 放宽财报类型枚举，多收一个 'QUARTERLY'。B7 样本本该被拒。
    ('dc-fin-report-type-enum', DC_DDL, DC_SMOKE,
     'ck_dc_fin_report_type',
     [("report_type IN ('BALANCE', 'INCOME', 'CASHFLOW', 'INDICATOR')",
       "report_type IN ('BALANCE', 'INCOME', 'CASHFLOW', 'INDICATOR', 'QUARTERLY')")],
     'QUARTERLY'),

    # 放宽公告日必须不早于报告期末。B8 样本把公告日写在期末前 30 天，
    # 因此容差必须 > 30 才可能被接受 ⇒ 给 60。（C2 的等号样本不受影响。）
    ('dc-fin-announce-after-period', DC_DDL, DC_SMOKE,
     'ck_dc_fin_announce_after_period',
     [('announce_date >= period_end', 'announce_date >= period_end - 60')],
     'period_end - 60'),

    # 放宽可得日必须不早于公告日。B9 样本提前 27 天可得 ⇒ 容差给 60。
    # （这条也是「一个探测器一个样本」的典型：B8/B9 是两条不同约束，不能互相代替。）
    ('dc-fin-available-ge-announce', DC_DDL, DC_SMOKE,
     'ck_dc_fin_available_ge_announce',
     [('available_date >= announce_date', 'available_date >= announce_date - 60')],
     'announce_date - 60'),

    # 放宽 ROE 上界（本工具最初的 2 个案例之一，保留作回归基线）。
    # B10 样本 roe = 600 本该被拒；C1a/b/c/d 四个边界接受样本不受影响。
    ('dc-roe-upper-bound', DC_DDL, DC_SMOKE,
     'ck_dc_fin_roe_range',
     [('roe >= -1 AND roe <= 5', 'roe >= -1 AND roe <= 1000')],
     '<= 1000'),

    # 放宽成分股区间必须严格递增：相等也放行。B11 样本本该被拒。
    ('dc-member-effective-range', DC_DDL, DC_SMOKE,
     'ck_dc_member_effective_range',
     [('effective_to > effective_from', 'effective_to >= effective_from')],
     'effective_to >= effective_from'),

    # 放宽权重上界到 100。B12 样本 weight = 1.5 本该被拒。
    ('dc-member-weight-range', DC_DDL, DC_SMOKE,
     'ck_dc_member_weight_range',
     [('weight >= 0 AND weight <= 1', 'weight >= 0 AND weight <= 100')],
     '<= 100'),

    # 放宽采集状态枚举，多收一个 'DONE'。B13 样本本该被拒。
    ('dc-ingest-status-enum', DC_DDL, DC_SMOKE,
     'ck_dc_ingest_status',
     [("status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')",
       "status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'DONE')")],
     'DONE'),

    # 放宽数据源优先级枚举，多收一个 'SECONDARY'。B14 样本本该被拒。
    ('dc-ingest-priority-enum', DC_DDL, DC_SMOKE,
     'ck_dc_ingest_priority',
     [("priority IN ('PRIMARY', 'FALLBACK')",
       "priority IN ('PRIMARY', 'FALLBACK', 'SECONDARY')")],
     'SECONDARY'),

    # 放宽质检标签枚举，多收一个 'HALTED'。B15 样本本该被拒。
    # 该枚举在 DDL 里跨两行，所以只改第一行的尾项（仍在同一行内，满足单行约束）。
    ('dc-quality-flag-enum', DC_DDL, DC_SMOKE,
     'ck_dc_quality_flag',
     [("'SUSPENDED', 'LIMIT_UP', 'LIMIT_DOWN', 'ST',",
       "'SUSPENDED', 'LIMIT_UP', 'LIMIT_DOWN', 'ST', 'HALTED',")],
     'HALTED'),

    # 放宽质检严重度枚举，多收一个 'ERROR'。B16 样本本该被拒。
    ('dc-quality-severity-enum', DC_DDL, DC_SMOKE,
     'ck_dc_quality_severity',
     [("severity IN ('CRITICAL', 'WARNING', 'INFO')",
       "severity IN ('CRITICAL', 'WARNING', 'INFO', 'ERROR')")],
     'ERROR'),

    # ---------------- 风控：14 条 CHECK，15 个案例 ----------------
    # 放宽 scope 枚举，多收一个 'ACCOUNTS'。B18 样本本该被拒。
    ('risk-scope-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_scope',
     [("scope IN ('GLOBAL', 'ACCOUNT', 'STRATEGY', 'SYMBOL')",
       "scope IN ('GLOBAL', 'ACCOUNT', 'STRATEGY', 'SYMBOL', 'ACCOUNTS')")],
     'ACCOUNTS'),

    # 放宽 unit 枚举，多收一个 'PERCENT'。B6 样本本该被拒。
    ('risk-unit-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_unit',
     [("unit IN ('RATIO', 'COUNT', 'ABSOLUTE')",
       "unit IN ('RATIO', 'COUNT', 'ABSOLUTE', 'PERCENT')")],
     'PERCENT'),

    # 放宽 comparison 枚举，多收一个 'LT'。B7 样本本该被拒。
    ('risk-comparison-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_comparison',
     [("comparison IN ('LTE', 'GTE')", "comparison IN ('LTE', 'GTE', 'LT')")],
     'LT'),

    # 放宽 RATIO 上界（本工具最初的 2 个案例之一，保留作回归基线）。
    # B1 样本 RATIO = 10 本该被拒 ⇒ 必须从 23/0 变成 22/1。
    ('risk-ratio-upper-bound', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_ratio_range',
     [('threshold > 0 AND threshold <= 1', 'threshold > 0 AND threshold <= 100')],
     '<= 100'),

    # 同一个约束的**下界**，另起一个案例：上界与下界是两个独立判据，
    # 只测一个等于没测另一半（B2 样本 threshold = 0 本该被拒）。
    ('risk-ratio-lower-bound', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_ratio_range',
     [('threshold > 0 AND threshold <= 1', 'threshold >= 0 AND threshold <= 1')],
     'threshold >= 0'),

    # 放宽「GLOBAL 的 scope_key 必须是 '*'」：直接置为恒真。
    # B4 样本（scope=GLOBAL 且 scope_key='SMOKE-A'）本该被拒。
    # 这条没法用「放宽到某个数」表达（判据是字符串相等），恒真是最窄的放宽手段。
    ('risk-global-key', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_global_key',
     [("scope <> 'GLOBAL' OR scope_key = '*'", 'TRUE')],
     'true'),

    # 放宽阈值非负到 -100。B10 样本（threshold = -1）本该被拒。
    ('risk-threshold-nonneg', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_nonneg',
     [('threshold >= 0', 'threshold >= -100')],
     '>= -100'),

    # 放宽「COUNT 单位必须是整数」：恒真。B8 样本（COUNT 且 3.5）本该被拒。
    ('risk-count-int', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_count_int',
     [("unit <> 'COUNT' OR threshold = trunc(threshold)", 'TRUE')],
     'true'),

    # 放宽配置版本单行守卫（id = 1）：B17 样本 id = 2 本该被拒。
    # 用完整约束名定位，因为另一张表有长得一样的 `CHECK (id = 1)`。
    ('risk-config-version-singleton', RISK_DDL, RISK_SMOKE,
     'ck_risk_config_version_singleton',
     [('CONSTRAINT ck_risk_config_version_singleton CHECK (id = 1)',
       'CONSTRAINT ck_risk_config_version_singleton CHECK (id >= 1)')],
     'id >= 1'),

    # 放宽审计方向枚举，多收一个 'WIDEN'。B19 样本本该被拒。
    ('risk-audit-direction-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_rule_audit_direction',
     [("change_direction IN ('', 'TIGHTEN', 'RELAX')",
       "change_direction IN ('', 'TIGHTEN', 'RELAX', 'WIDEN')")],
     'WIDEN'),

    # 放宽拦截动作枚举，多收一个 'SELL'。B12 样本本该被拒。
    ('risk-intercept-action-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_intercept_action',
     [("action IN ('REDUCE', 'REJECT', 'HALT')",
       "action IN ('REDUCE', 'REJECT', 'HALT', 'SELL')")],
     'SELL'),

    # 放宽决策动作枚举，多收一个 'SKIP'。B13 样本本该被拒。
    ('risk-decision-action-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_decision_action',
     [("action IN ('PASS', 'REDUCE', 'REJECT', 'HALT')",
       "action IN ('PASS', 'REDUCE', 'REJECT', 'HALT', 'SKIP')")],
     'SKIP'),

    # 放宽决策运行态枚举，多收一个 'HALF_OPEN'。B14 样本本该被拒。
    ('risk-decision-run-state-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_decision_run_state',
     [("run_state IN ('NORMAL', 'DEGRADED', 'BREAKER_TRIPPED', 'KILLED')",
       "run_state IN ('NORMAL', 'DEGRADED', 'BREAKER_TRIPPED', 'KILLED', 'HALF_OPEN')")],
     'HALF_OPEN'),

    # 放宽熔断器态枚举，多收一个 'HALF_OPEN'。B15 样本本该被拒。
    ('risk-breaker-state-enum', RISK_DDL, RISK_SMOKE,
     'ck_risk_breaker_state',
     [("state IN ('NORMAL', 'TRIPPED')", "state IN ('NORMAL', 'TRIPPED', 'HALF_OPEN')")],
     'HALF_OPEN'),

    # 放宽总开关单行守卫（id = 1）：B16 样本 id = 2 本该被拒。
    ('risk-switch-state-singleton', RISK_DDL, RISK_SMOKE,
     'ck_risk_switch_state_singleton',
     [('CONSTRAINT ck_risk_switch_state_singleton CHECK (id = 1)',
       'CONSTRAINT ck_risk_switch_state_singleton CHECK (id >= 1)')],
     'id >= 1'),
]


def _line_of(text, needle):
    """needle 首次出现的 1-based 行号（回显给肉眼核对，免得改错位置还看不出来）。"""
    idx = text.find(needle)
    return text.count('\n', 0, idx) + 1 if idx >= 0 else -1


def _bare(text):
    """把 PostgreSQL 重新排版出来的约束定义归一化后返回。

    pg_get_constraintdef() 会把简单表达式“展开”：枚举成员变成 `'SELL'::text`，
    数值字面量变成 `(0)::numeric`，多字类型名带空格。拿源码原文（`open >= 0`）
    去匹配是**注定失败**的 —— 2026-09-23 实测如此：变异确实入库了
    （`(open >= (0)::numeric)`），却报「变异没落到约束表达式上」。
    两侧同规则归一化后才可比较；这不是提高检出率，只是让断言说人话。

    “真的变了”这件事**不靠**这个弱断言 —— 靠 mutate() 里的逐字节比较。
    """
    text = text.replace("'", '')
    # 多字类型名必须先处理，否则 ::character varying 会被下一个正则切成两截
    text = re.sub(r'::(?:character varying|timestamp with(?:out)? time zone|'
                  r'time with(?:out)? time zone|double precision|bit varying)\b', '',
                  text)
    text = re.sub(r'::[\w\[\]]+(?:\(\d+(?:,\s*\d+)?\))?', '', text)
    text = re.sub(r'\((-?\d+(?:\.\d+)?)\)', r'\1', text)   # (0) -> 0
    return re.sub(r'\s+', ' ', text)


def mutate(rel_ddl, edits, tag):
    """把变异后的 DDL 写到 tools/_falsify_tmp/ 下（必须在仓库内：compose 只挂了 ./）。"""
    src = os.path.join(ROOT, rel_ddl.replace('/', os.sep))
    with open(src, 'r', encoding='utf-8-sig') as fh:
        raw = fh.read().replace('\r\n', '\n')

    mutated = raw
    for n, (old, new) in enumerate(edits, 1):
        if '\n' in old:
            raise SelfAssert('edits[%d] 的 old 跨行 —— 行号回显与「恰好一次」都失去意义' % n)
        hits = mutated.count(old)
        if hits != 1:
            raise SelfAssert(
                'edits[%d] 的目标片段在 %s 中出现 %d 次（要求恰好 1 次）\n'
                '      片段: %r' % (n, rel_ddl, hits, old))
        print('  放宽[%d] %s:%d' % (n, rel_ddl, _line_of(raw, old)))
        print('          %s' % old)
        print('       -> %s' % new)
        mutated = mutated.replace(old, new)

    if mutated == raw:
        raise SelfAssert('全部 edits 跑完，内容逐字节没变 —— 变异根本没生效')

    os.makedirs(TMP_DIR, exist_ok=True)
    dst = os.path.join(TMP_DIR, tag + '.sql')
    with open(dst, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(mutated)

    # 再验一次落盘结果。注意不能用 `old in disk`：old 常常是 new 的前缀
    # （`... <= 1` 是 `... <= 100` 的前缀），那样写恒为真 —— 实测踩过。
    # 逐字节相等是这里唯一没有歧义的判据。
    with open(dst, 'r', encoding='utf-8') as fh:
        disk = fh.read()
    if disk == raw:
        raise SelfAssert('落盘文件与原文逐字节相同 —— 变异没写下去')
    if disk != mutated:
        raise SelfAssert('落盘文件与内存中的变异不一致')
    return dst, len(mutated) - len(raw)


def reset_schema():
    """把 public 清空。

    这不是洁癖：db/*.sql 用 `CREATE TABLE IF NOT EXISTS`，种子行用
    `ON CONFLICT DO NOTHING`。在已有表上再跑一份**改过约束的** DDL 会
    **整条静默空转** —— 库里还是旧约束，而触发测试是在旧约束下跑的，
    于是「变异没送进数据库」会被读成「触发测试没用」⇒ 多报一堆 MISSED。
    清完必须断言为空，否则后面每个案例的结论都是空的。
    """
    rc, out = psql(sql='DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;',
                   timeout=300)
    if rc != 0:
        raise SelfAssert('清 schema 失败（exit=%d）:\n%s' % (rc, out[-1500:]))
    rc, n, out = scalar(
        'SELECT count(*) FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace '
        "WHERE ns.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')")
    if rc != 0:
        raise SelfAssert('清 schema 后数不出对象数（exit=%d）:\n%s' % (rc, out[-1500:]))
    if n != '0':
        raise SelfAssert('清 schema 后 public 里还剩 %s 个对象 —— 变异后的 DDL 会在'
                         '已存在的表上静默空转，本次结论不可信' % (n or '<空>'))


def constraintdef(cname):
    """取回约束的真实定义（PostgreSQL 会把它重新排版并加 ::text 之类转型）。"""
    rc, last, out = scalar("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                           "WHERE conname = '%s'" % cname)
    if rc != 0:
        raise SelfAssert('查 %s 的定义失败（exit=%d）:\n%s' % (cname, rc, out[-800:]))
    return last


def case(tag, rel_ddl, rel_smoke, cname, edits, expect):
    print('')
    print('=== %s ===' % tag)
    print('  约束: %s   （变异后定义里必须出现 %r）' % (cname, expect))

    reset_schema()

    dst, delta = mutate(rel_ddl, edits, tag)
    rel_dst = 'tools/_falsify_tmp/%s.sql' % tag
    print('  变异已落盘: %s  (delta=%+d bytes)' % (rel_dst, delta))

    rc, out = psql(path='/work/' + rel_dst, timeout=600)
    if rc != 0:
        print('  !! 变异后的 DDL 自己没跑通（exit=%d）—— 本次无法判定，不计入结果' % rc)
        for ln in out.splitlines():
            if 'ERROR' in ln:
                print('     | ' + ln.strip())
        return None

    # 自 assert 4：变异必须真的落到这个约束上。清 schema 只证明 DDL 被执行了，
    # 不证明改的是那个表达式（片段可能命中注释，或改错了表）。
    ddef = constraintdef(cname)
    if not ddef:
        raise SelfAssert('变异后的 DDL 落库了，但 pg_constraint 里找不到 %s —— '
                         '约束名写错，或那份 DDL 没建这张表' % cname)
    if _bare(expect) not in _bare(ddef):
        raise SelfAssert('约束 %s 的定义里没有 %r —— 变异没落到约束表达式上\n'
                         '      实际定义: %s' % (cname, expect, ddef))
    print('  变异已入库: %s' % ddef)

    rc, out = psql(path='/work/' + rel_smoke, timeout=600)
    m = SUMMARY_RE.search(out)
    counts = ('%s passed, %s failed' % m.groups()) if m else 'no summary line'
    red = 'SMOKE FAIL' in out

    print('  触发测试输出: %s  (psql exit=%d)' % (counts, rc))
    for ln in out.splitlines():
        s = ln.strip()
        if 'FAIL' in s and 'NOTICE' in s:
            print('     | ' + s)

    if not red:
        print('  RESULT: 没抓到 —— 约束被放宽了，触发测试还是 SMOKE PASS。')
        print('          ⇒ 这条触发用例是摆设，本轮「全绿」不可信。')
        return False

    n_fail = int(m.group(2)) if m else -1
    if n_fail == 1:
        print('  RESULT: 抓到，且正好 1 条变红 —— 与期望签名一致。')
        return True
    print('  RESULT: 抓到，但红了 %d 条（期望 1 条）—— 变异波及了别的用例，' % n_fail)
    print('          需要手工分辨，本次不算强证据。')
    return False


def main():
    """既打印到控制台，又留一份报告。一跑就是 ~90 次 docker exec，
    不该为了看结论重跑 —— 与 gates-report.txt / sql-smoke-report.txt 同一约定。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass

    if '--list' in sys.argv:
        return _main()

    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = _Tee(real, buf)
    try:
        code = _main()
    finally:
        sys.stdout = real

    verdict = {EXIT_OK: 'OK', EXIT_SMOKE_FAIL: 'FAIL',
               EXIT_GUARD: 'SELF-ASSERT FAILED',
               EXIT_NO_DOCKER: 'NO DOCKER'}.get(code, 'exit %s' % code)
    with open(REPORT, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('# falsify-report.txt —— 触发测试的证伪记录\n')
        fh.write('# 生成: %s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
        fh.write('# 工具: tools/falsify_smoke.py（每次运行把 db/*.sql 逐条放宽后重跑 smoke，\n')
        fh.write('#       每个案例都必须正好 1 条样本变红）\n')
        fh.write('# 判定: %s (exit=%d)\n' % (verdict, code))
        fh.write('#\n\n')
        fh.write(buf.getvalue())
    print('报告: tools/falsify-report.txt  (%s)' % verdict)
    return code


class _Tee(object):
    """同时写往多个流；只为了把 main() 的输出留存一份，不改变任何判定。"""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, text):
        for s in self._streams:
            try:
                s.write(text)
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def _main():
    if '--list' in sys.argv:
        print('%d 个案例' % len(CASES))
        for tag, ddl, smoke, cname, edits, expect in CASES:
            print('%-32s %-34s %s' % (tag, cname, ddl))
        return EXIT_OK

    rc, out = run(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=120)
    if rc != 0:
        print('DOCKER UNAVAILABLE: docker daemon 没有应答 —— 什么都没测，不是通过。')
        return EXIT_NO_DOCKER

    compose('down', '-v', '--remove-orphans', timeout=300)
    rc, out = compose('up', '-d', '--wait', 'db', timeout=1200)
    if rc != 0:
        print('GATE FAIL: 数据库没起来。')
        print(out[:2000])
        return EXIT_GUARD

    results = []
    try:
        try:
            for tag, ddl, smoke, cname, edits, expect in CASES:
                results.append((tag, case(tag, ddl, smoke, cname, edits, expect)))
        except SelfAssert as exc:
            print('')
            print('SELF-ASSERT FAILED: %s' % exc)
            print('什么都没证明 —— 这既不是「通过」，也不是「发现缺陷」。')
            print('先修工具/夹具，再重跑；不要把这轮的结论写进任何报告。')
            return EXIT_GUARD
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        compose('down', '-v', '--remove-orphans', timeout=300)

    print('')
    print('=== 汇总 ===')
    bad = 0
    for tag, ok in results:
        state = {True: 'CAUGHT', False: 'MISSED', None: 'INCONCLUSIVE'}[ok]
        if ok is not True:
            bad += 1
        print('  %-26s %s' % (tag, state))

    if bad:
        print('')
        print('verdict: FAIL -- 有触发用例没抓住人为放宽的约束，那些 SMOKE PASS 不能信。')
        return EXIT_SMOKE_FAIL
    print('')
    print('verdict: OK -- 每个被放宽的约束都被触发测试抓到了；触发测试确有效力。')
    print('         覆盖: %d 个案例 / 30 条命名 CHECK 约束' % len(CASES))
    print('               （ck_risk_rule_ratio_range 上下界各一例，所以案例数比约束数多 1）')
    print('         未覆盖: PRIMARY KEY、uq_dc_data_version_active、uq_dc_quality_issue、')
    print('                 risk_rule.threshold 的 NOT NULL —— 它们不是 CHECK 表达式，')
    print('                 而本工具的手法只对 CHECK 成立。')
    print('         另一半边界: 本工具只证明「被放宽的那一条会红」，不证明「除它以外全是绿」——')
    print('                 后者靠 n_fail == 1 这个签名间接兜住（变异波及多例会判 MISSED）。')
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
