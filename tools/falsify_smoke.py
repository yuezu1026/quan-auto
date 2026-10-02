# -*- coding: utf-8 -*-
"""falsify_smoke.py -- 证伪 db/*.smoke.sql：故意削弱一条约束，看触发测试是否会变红。

为什么必须有这个工具
--------------------
只跑「全绿」等于假门禁。一份 SMOKE PASS 有两种可能：

    (a) 约束真的会拒绝非法值（我们想要的结论）
    (b) 触发测试根本没在测（DO 块的异常分支从没进过、计数加错了、样本值其实合法……）

从输出上这两种**长得一模一样**。唯一能把它们分开的办法，是构造一个**已知有缺陷**
的 schema，确认触发测试会因此变红。这就是本工具。

手法：**放宽守门，而不是删掉它**
------------------------------
删掉一条**命名 CHECK** 会让 smoke 的 A 段（存在性核对：A2 按 pg_constraint 点名要那
18 / 14 条）先报 FAIL，于是「红了」这个结果分不清是 A 段还是 B 段抓到的 —— 一个探测器
一个样本，这里要的是 B 段。所以对命名 CHECK，本工具只把区间放宽到刚好能让 B 段那个样本
被接受（例如 roe 上界 5 -> 1000，样本 roe=600 便由拒绝变接受），**约束名保持不变**
⇒ A 段照旧通过 ⇒ 红的只可能是 B 段那一条。期望签名：`n_pass = 总数-1, n_fail = 1`。

2026-10-02 起同一套手法扩到**非 CHECK 的守门**（附录 I 的 J-9：「只放宽 CHECK 表达式」，
于是 NOT NULL / 主键 / UNIQUE 全在范围外）。非 CHECK 那一支要按「A 段有没有一条存在性
核对会先红」分类 —— 混用会让两条样本一起红，看起来就像工具坏了：

  * `uq_dc_data_version_active`（部分唯一索引）：A5 按 pg_indexes **点名核对它存在**
    ⇒ 删掉索引会让 A5 与 A6 一起红（两条红，归因不清）⇒ 这里用的是「**同名**保留索引、
    只去掉 UNIQUE」。A5 照旧 PASS，A6 那条「第二行 active 被接受」变红 ⇒ 仍然恰好 1 条。
  * `dc_trading_calendar` 的主键：没有任何存在性核对 ⇒ 把主键子句换成**同名** CHECK
    （名字还在，contype 从 p 变 c），C8b 那条「重复日历日被接受」变红 ⇒ 恰好 1 条。
    落地断言按**表名**问「这张表还有没有一次主键」，不写约束名 —— 单例表的主键是匿名
    子句，名字由 PostgreSQL 现生成，钉它等于钉一个实现细节。
  * `risk_rule.threshold` 的 NOT NULL：这一个**只能真的拿掉**。换成同名 CHECK 的话
    SQLSTATE 会从 23502 变成 23514，而 B11 只捕 not_null_violation ⇒ 异常逃出子块、
    整个 DO 块中止、**连 SMOKE 汇总行都不会打出来**（那是 MISSED，不是「1 条红」）。
    拿掉 NOT NULL 之后 B11 的 INSERT **成功**，于是走到它自己那句 `n_fail := n_fail + 1`
    ⇒ 仍然恰好 1 条红。它上面也没有存在性核对（A 段点名核对的是 14 条 ck_）。

两张单例表（`risk_config_version` / `risk_switch_state`）的主键**没有**进 CASES，尽管它们与
`dc_trading_calendar` 的主键完全同类：它们的主键正是种子行 `INSERT ... ON CONFLICT (id) DO
NOTHING` 赖以成立的唯一性索引，去掉它之后**变异后的 DDL 自己跑不通**（种子行会报「no unique
or exclusion constraint matching the ON CONFLICT specification」）⇒ 案例判成 INCONCLUSIVE，
而那也是「没抓到」。要证伪它们得再改一处种子行，那已经是动种子数据、超出「只放宽一条守门」
的边界。它们连同够不着的那些一起登记在 `NOT_FALSIFIED` 里。

四个自 assert（少一个，结论就不可信）
------------------------------------
  1. 每个编辑的目标片段必须**恰好出现一次**；本来该出现多次的（例如 `PRIMARY KEY (id),`
     在风控那份 DDL 里出现 2 次），用三元组 `(old, new, 第几次)` 明确消歧 —— 那个次数
     必须真的存在，而且必须 > 1（消歧的理由消失了就要报错，否则它会悄悄退化成「改第一处」）。
     违反任一条即 exit 2
  2. 变异后的文件必须**真的变了**（逐字节比较），否则 exit 3
     —— 否则「全绿」是在**未经修改的文件**上得出的结论
  3. 每个案例开跑前 public schema 必须是**空的**，否则 exit 2
  4. 变异必须**真的落在数据库那个守门上**：跑完变异 DDL 后跑该 kind 的落地查询
     （见 LANDINGS：CHECK 取 pg_get_constraintdef()、NOT NULL 取 attnotnull、
     UNIQUE 看 pg_constraint.contype / pg_indexes.indexdef、主键看「这张表还有没有一次
     主键」），输出里必须出现该案例声明的 expect 片段，否则 exit 2
     （查询输出为空也算失败 —— 名字写错不能静默通过）

为什么要每案例清 schema（2026-09-23 实测撞到的坑）
--------------------------------------------------
db/*.sql 全部用 `CREATE TABLE IF NOT EXISTS`，种子行也 `ON CONFLICT DO NOTHING`。
第一个案例建好表之后，第二个案例再跑一份**改过约束的** DDL，会因为表已存在而
**整条语句静默空转** —— 表上还是旧约束。于是那个案例的 B 段样本照旧被拒、
触发测试照旧全绿 ⇒ 判定 MISSED。**这不是「触发测试没用」，是工具自己没把变异
送进数据库。** 之前只有 2 个案例、且分属两份 DDL（各建各的表名），所以侥幸没
暴露；扩到全量（现 38 个案例 / 32 条命名 CHECK）立刻会撞上 —— 而撞上的方式恰好是
「多报 MISSED」，方向安全但会让整个覆盖矩阵变成噪声。修法：每案例前
`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` 并断言表数为 0。

第 4 条自 assert 是第二道保险：清 schema 只能证明「DDL 被执行了」，不能证明
「改的正好是那个守门」（比如片段命中了注释、或改错了表）。

expect 的比对先把定义归一化：PostgreSQL 会把它展开成 `'ACCOUNTS'::text`、
`(0)::numeric` 这种形式，拿源码原文去匹配**注定失败**（已实测）。归一化规则见
`_bare()`。这只是让断言能说人话；「变异真的改了文件」靠第 2 条的逐字节比较。

用法
----
    python tools/falsify_smoke.py            # 跑 CASES 里的全部案例
    python tools/falsify_smoke.py --list
    python tools/falsify_smoke.py --selftest  # 只测参数/落点守卫，不需要 docker
    python tools/falsify_smoke.py --pg-image=postgres:14 \
        --report=tools/falsify-report-pg14.txt

`--pg-image=` 与 `--report=` 和 tools/run_sql_smoke.py 的那两个开关**同义**（落点守卫
直接 import 那边的 `resolve_report_path`，一份实现两个调用点）。为什么这两个开关必须
一起加（2026-09-24 就已登记，见附录 B19 第 2 条）：

* 报告里**没有镜像名**时，「在 14 上也逐条证伪过」这句话无法核对 —— 谁都能说，
  谁都不能证伪。所以本工具现在把 `image : postgres:N`（与 digest、服务端版本）写进报告，
  由 tools/verify_data_center.py 的 `C6` 与 tools/verify_risk_config.py 的 `C17` 拿它和
  冒烟那一侧的证据集**双向**核对（少一个镜像的证伪 ⇒ FAIL，多一个未被戳记引用的 ⇒ 也 FAIL）。
* 报告只有一条固定路径时，铺四个镜像 = 每次把上一版的证据**覆盖掉**，而快照没有撤销
  —— 于是「14 上到底跑过没有」永远答不上来。

当前覆盖
--------
覆盖统计：38 个案例 / 32 条命名 CHECK（数据中心 20 个案例、风控 18 个；ck_risk_rule_ratio_range
上下界各一例，所以命名 CHECK 的案例比约束多 1，其余 5 个案例打的是非 CHECK 守门）。这几个数由
CASES 数出来，`registry_problems()` 会拿**这句话**与 CASES 对账（说了数却能跟数据脱钩的散文
比没说更坏），而报告末尾那行「覆盖: N 个案例」也是**数出来**的 —— 两个门禁会拿它跟 CASES
的长度对账 ⇒ 加了案例却不重跑阶梯，门禁当场报 FAIL。

案例分两类，由 `KINDS` 里的 kind 决定「怎么证明守门真的没了」。非 CHECK 的那 5 条各有自己的
落地查询（`LANDINGS`），而它们能进 CASES 的前提是**有一条样本会因它变红**（括号里就是那条
样本被接受时的样子）：

    dc-version-active-unique     uq_dc_data_version_active 的 UNIQUE  （A6 被接受）
    dc-calendar-pk               dc_trading_calendar 的主键          （C8b 被接受）
    risk-threshold-notnull       risk_rule.threshold 的 NOT NULL     （B11 被接受）
    risk-config-version-pk       risk_config_version 的主键          （B21 被接受）
    risk-switch-state-pk         risk_switch_state 的主键            （B20b 被接受）

其中 `risk-config-version-pk` 要**两处**编辑：拿掉主键之后，种子行
`... ON CONFLICT (id) DO NOTHING` 的冲突目标也失效了（『no unique or exclusion constraint
matching the ON CONFLICT specification』）⇒ 变异后的 DDL 自己加载不动，那是 INCONCLUSIVE
而不是「没抓到」。所以第二处把那个冲突目标去掉（`ON CONFLICT DO NOTHING`），种子行照样幂等
地插进去。两处编辑打的是**同一个对象**（这张表上 id 的唯一性），不是顺手放宽第二条守门。

**够不着**的守门一律登记在 `NOT_FALSIFIED` 里（每条都写清为什么），报告末尾照样打出来 ——
「够不着」与「已经证伪」是两件事，混说就会把边界读成覆盖。tools/verify_data_center.py 的 C9
会**静态**核这张表（只读源码、从不 import）：DDL 里数出来的非 CHECK 守门（PRIMARY KEY /
UNIQUE / NOT NULL）既没有案例、也没有登记 ⇒ FAIL；登记项点名的守门在 DDL 里根本不存在 ⇒
也 FAIL；登记项格式坏掉（不是二元组 / 理由是空话 / `（除 …）`写坏）⇒ 也 FAIL。名字形如
`<类别>（除 <名字>、<名字>）` 的条目给**整个类别**兜底（理由必须对整个类别成立），而
「除」后面点名的那些**必须真的各有一条案例**（否则这条兜底会把它们悄悄吞掉，而报告里
看不出分别）；其余条目只对**它自己点名的那个守门**有效（`uq_dc_quality_issue` 就是后者：
只有一个对象，值得点名）。它证明的仍是「放宽哪条就红哪条」（靠 `n_fail == 1` 这个签名），
**不是**逐值证明「每个样本值都真的落在拒绝区间内」。结果写 tools/falsify-report.txt（快照）。

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
    ENV, EXIT_GUARD, EXIT_NO_DOCKER, EXIT_OK, EXIT_SMOKE_FAIL,
    ROOT, SUMMARY_RE, compose, psql, resolve_report_path, run, scalar, scalar_cid,)

TMP_DIR = os.path.join(ROOT, 'tools', '_falsify_tmp')
DEFAULT_REPORT = os.path.join(ROOT, 'tools', 'falsify-report.txt')
# 可变：--report= 可以改它。默认值**一个字都没动** ⇒ 不带该开关时落点与以前一致。
REPORT = DEFAULT_REPORT
# _main() 填、main() 写报告头部时读。用全局传值是刻意的：镜像是**结论的一部分**
# （「这句话只在哪个镜像上成立」），不值得为「干净」把它塞过 5 层参数。
OBSERVED = {'image': '', 'digest': '', 'version': ''}


class SelfAssert(Exception):
    """自 assert 失败。与「案例没抓到」是两回事，退出码也不同（2 vs 1）。"""


def set_report_path(value):
    """解析 --report=<相对仓库根的路径>。返回 (path, None) 或 (None, 原因)。

    规则本身在 `run_sql_smoke.resolve_report_path`（一份实现、两个调用点）。这里只负责
    把结果装进本模块的 REPORT。**失败时 REPORT 必须原样不动** —— 半途改掉落点，等于
    把「拒绝」执行成「换一个地方写」。
    """
    global REPORT
    path, why = resolve_report_path(value)
    if path is None:
        return None, why
    REPORT = path
    return path, None


def parse_args(argv):
    """把 argv 拆成 (pg_image, report_arg, flags)。纯函数 ⇒ 能自测。

    `flags` 里三个开关都只回答「出现过没有」，取值一律走 `--name=值` 的形式：这样
    `--report=` 后面留空**不**会被悄悄当成「没传」，而是走到守卫里报「后面是空的」。
    （把「参数写错了」和「没传参数」混成同一件事，是「什么都不写就退 0」那类假绿的起点。）
    """
    pg_image, report_arg = None, None
    flags = {'list': False, 'selftest': False, 'keep': False}
    for arg in argv:
        if arg.startswith('--pg-image='):
            pg_image = arg.split('=', 1)[1]
        elif arg.startswith('--report='):
            report_arg = arg.split('=', 1)[1]
        elif arg in ('--list', '--selftest', '--keep'):
            flags[arg[2:]] = True
    return pg_image, report_arg, flags


def _environment():
    """读回实测环境：服务端版本 + 容器用的镜像名 + 镜像 digest。

    `image  : postgres:N` 这一行是**判据**，不是注释：两个门禁按这个形状从报告里取镜像名。
    形状改了（缩进、空格、冒号）而没人报错，就是本仓库最怕的那种「报告看起来比真通过还
    干净」—— 所以取不到时必须吼出来，并让那一行留成 `(未取到)`（门禁会因此 FAIL）。
    """
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
    return version, image, digest


# (tag, DDL, smoke, guard, edits, expect)
#
#   edits  : [(old, new), ...]。一个案例可能需要放宽**多处**：像
#            ck_dc_bar_ohlc_order 那样五个子句都会被同一个样本违反，
#            只放宽一个子句样本照样被拒 ⇒ 会误判成 MISSED。
#            每个 old 都必须单行；它在该 DDL 里**恰好出现一次**，或者写成
#            (old, new, 第几次出现) 显式消歧（1-based；片段只出现 1 次却给了
#            次数 ⇒ 自 assert 报错，免得消歧理由消失后它悄悄退化成「改第一处」）。
#   expect : 落地查询的输出里必须出现的片段（见 LANDINGS）。这是「变异真的落到
#            那个守门上」的凭据 —— 清 schema 只证明 DDL 跑了，不证明改的是它
#            （片段可能命中注释，或改错了表）。空输出一律算失败。
#   guard  : kind 决定落地查询怎么吃它（LANDINGS）：kind='check' 时它是
#            pg_constraint.conname 里的字面名字（不是 CREATE TABLE 里的别名）；
#            'notnull' 时它是 (表名, 列名)；'unique' 时是约束名或索引名；
#            'pk' 时是**表名**。
#   kind   : 见 KINDS，缺省 'check'。
#
# 案例数与命名 CHECK 数的**权威声明**在模块 docstring 的「当前覆盖」一节
# （registry_problems() 会拿那句话跟 CASES 对账）。非 CHECK 守门一共 5 条（登记在 KINDS
# 里，不以 'ck_' 开头所以扫一眼就能数出来）：uq_dc_data_version_active、
# dc_trading_calendar 的主键、risk_rule.threshold 的 NOT NULL、以及两张单例表
# （risk_config_version / risk_switch_state）的主键。
# 够不着的那些不在这里列 —— 它们在 NOT_FALSIFIED 里逐条登记、由报告末尾打出来、
# 并由 tools/verify_data_center.py 的 C9 静态核对。
RISK_DDL = 'db/risk_control.sql'
RISK_SMOKE = 'db/risk_control.smoke.sql'
DC_DDL = 'db/data_center.sql'
DC_SMOKE = 'db/data_center.smoke.sql'

CASES = [
    # ---------------- 数据中心：18 条 CHECK，18 个案例 ----------------
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

    # 放宽每股派息必须非负：-1 及以上的负数也放行。B17 样本（cash = -0.5）本该被拒。
    # C9a 的 cash = 0 边界样本不受影响（0 >= -1 仍然成立）。
    ('dc-dividend-cash-nonneg', DC_DDL, DC_SMOKE,
     'ck_dc_dividend_cash_nonneg',
     [('cash_per_share >= 0', 'cash_per_share >= -1')],
     'cash_per_share >= -1'),

    # 放宽公告日不得晚于除权除息日：B18 样本把公告日写在除权日后 31 天，
    # 因此容差必须 > 31 ⇒ 给 60。（C9a 的 announce == ex 与 C9b 的 announce < ex 不受影响。）
    ('dc-dividend-announce-not-after-ex', DC_DDL, DC_SMOKE,
     'ck_dc_dividend_announce_not_after_ex',
     [('announce_date <= ex_date', 'announce_date <= ex_date + 60')],
     '+ 60'),

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
    # B1 样本 RATIO = 10 本该被拒 ⇒ 必须从 26/0 变成 25/1。
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

    # ---------------- 非 CHECK 守门（2026-10-02 追加两批，附录 I 的 J-9）----------------
    # 这五条**没有一条是靠放宽第二条守门来造效果的**：前三条的样本（A5/A6、C8b、B11）
    # 早就存在，要证的正是「它们真在看的那条守门一旦消失就会红」；后两条（两张单例表的
    # 主键）用的样本（B20a/B20b/B21）是同批新增的，因为主键之前**根本没开过火**。
    # 每条注释都点名它是被哪条样本抓的，而 n_fail == 1 那个签名负责核对「只有它一条红」。
    #
    # 部分唯一索引。A5 按 pg_indexes 点名核对它存在 ⇒ 不能删（删了 A5 与 A6 一起红）。
    # 同名保留索引、只去掉 UNIQUE ⇒ A5 照旧 PASS，A6「第二行 is_active=TRUE 被接受」变红。
    ('dc-version-active-unique', DC_DDL, DC_SMOKE,
     'uq_dc_data_version_active',
     [('CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_data_version_active',
       'CREATE INDEX IF NOT EXISTS uq_dc_data_version_active')],
     'unique=no'),
    # 主键。没有任何存在性核对点名它，但名字要留着（改完 contype 从 p 变 c）——
    # 落地断言按**表名**问「这张表还有没有一次主键」。C8b「重复日历日被接受」变红。
    ('dc-calendar-pk', DC_DDL, DC_SMOKE, 'dc_trading_calendar',
     [('    CONSTRAINT pk_dc_trading_calendar PRIMARY KEY (exchange, trade_date, data_version)',
       '    CONSTRAINT pk_dc_trading_calendar CHECK (TRUE)')],
     'pk=no'),
    # NOT NULL。只能真的拿掉：换成同名 CHECK 会让 SQLSTATE 从 23502 变 23514，而 B11
    # 只捕 not_null_violation ⇒ 异常逃出子块、整个 DO 块中止、连汇总行都打不出来（那是
    # MISSED）。拿掉之后 B11 的 INSERT **成功** ⇒ 走到它自己那句 n_fail + 1 ⇒ 恰好 1 条红。
    # 这个片段在风控那份 DDL 里出现 **2** 次（risk_rule 与 risk_intercept_log 各一次），
    # 所以必须显式说「第 1 次」—— 那正是 risk_rule 的那一处。
    ('risk-threshold-notnull', RISK_DDL, RISK_SMOKE, ('risk_rule', 'threshold'),
     [('threshold    numeric(18,8)  NOT NULL,', 'threshold    numeric(18,8),', 1)],
     'false'),
    # 主键（两张**单例表**）。单例 CHECK 只保证「只有 id = 1 那一行」，它管不了「第二行
    # 也叫 id = 1」—— B16/B17 打的一直是 ck_*_singleton，这两张表的主键一次都没开过火。
    # risk-config-version-pk 要**两处**编辑：这张表的种子行用 `ON CONFLICT (id) DO NOTHING`
    # 落库，而它赖以成立的唯一性索引正是刚被拿掉的那个主键 ⇒ 变异后的 DDL 直接加载失败
    # （INCONCLUSIVE，不是「没抓到」）。第二处把冲突目标去掉，种子行照样幂等。
    # 两处编辑打的是同一个对象（这张表上 id 的唯一性），不是顺手放宽第二条守门。
    # `  PRIMARY KEY (id),` 在两处出现（risk_config_version / risk_switch_state），
    # 所以必须显式说「第几次」；写错表不会静默通过 —— 落地断言会当场问这张表还有没有主键。
    ('risk-config-version-pk', RISK_DDL, RISK_SMOKE, 'risk_config_version',
     [('  PRIMARY KEY (id),', '  -- R28 mutation: primary key removed', 1),
      ('ON CONFLICT (id) DO NOTHING;', 'ON CONFLICT DO NOTHING;')],
     'pk=no'),
    ('risk-switch-state-pk', RISK_DDL, RISK_SMOKE, 'risk_switch_state',
     [('  PRIMARY KEY (id),', '  -- R28 mutation: primary key removed', 2)],
     'pk=no'),
]


# ---------------------------------------------------------------------------
# 非 CHECK 守门：kind、落地查询、以及够不着的那些
# ---------------------------------------------------------------------------
# 每个案例的 kind（不在表里就是 'check'）。kind 决定两件事：
#   ① 用哪条落地查询证明「守门真的没了」（LANDINGS[KINDS[tag]]）；
#   ② 覆盖行里「N 条命名 CHECK」那个计数只数 kind == 'check' 的**去重 guard**。
# 非 CHECK 案例忘了在这里登记 kind 不会静默通过：缺省 'check' ⇒ 会拿
# pg_get_constraintdef() 去问一个索引名 ⇒ 落地断言当场报「查询输出为空」。
KINDS = {
    'dc-version-active-unique': 'unique',
    'dc-calendar-pk': 'pk',
    'risk-threshold-notnull': 'notnull',
    'risk-config-version-pk': 'pk',
    'risk-switch-state-pk': 'pk',
}

# kind -> (SQL 模板, 它吃的参数长什么样)。
#
# 为什么主键按**表名**问、不按约束名问：dc_trading_calendar 用的是
# `CONSTRAINT pk_dc_trading_calendar PRIMARY KEY (...)`，但两张单例表用的是**匿名子句**
# `PRIMARY KEY (id)` —— 那个约束名（risk_config_version_pkey）是 PostgreSQL 现生成的，
# 写进案例就等于钉住一个实现细节。所以主键一律问「这张表还有没有一次 contype='p' 的主键」。
#
# 输出必须**归一化后**能拿 expect 片段匹配（与 CHECK 那一支同规则，见 _bare()）。
LANDINGS = {
    'check': ("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
              "WHERE conname = '%s'",
              '约束名（pg_constraint.conname 的字面名字）'),
    'notnull': ("SELECT attnotnull::text FROM pg_attribute "
                "WHERE attrelid = '%s'::regclass AND attname = '%s' "
                "AND NOT attisdropped",
                '(表名, 列名)'),
    'unique': ("SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_constraint c "
               "WHERE c.conname = '%s' AND c.contype = 'u') "
               "OR EXISTS (SELECT 1 FROM pg_indexes i WHERE i.indexname = '%s' "
               "AND i.indexdef ~* 'CREATE UNIQUE') "
               "THEN 'unique=yes' ELSE 'unique=no' END",
               '约束名或索引名（两种形态都得没了才算没了）'),
    'pk': ("SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_constraint c "
           "WHERE c.conrelid = '%s'::regclass AND c.contype = 'p') "
           "THEN 'pk=yes' ELSE 'pk=no' END",
           '表名'),
}

# 够不着的守门。**这不是「覆盖」**，是「本工具证明不了它」的登记：报告末尾照打，
# tools/verify_data_center.py 的 C9 静态核对（既无案例又无登记 ⇒ FAIL；登记项没理由 ⇒ FAIL；
# DDL 里数得出来、这里既无案例又无登记的守门 ⇒ FAIL）。
#
# 名字形如 `<类别>（除 <名字>、<名字>）` = 给**整个类别**兜底（理由必须对整个类别成立），
# 而「除」后面点名的那些**必须真的各有一条案例** —— `registry_problems()` 会拿 CASES 核这一条，
# 否则这条兜底会把它们一起吞掉，而报告里看不出分别。名字不是类别前缀时只对**它自己点名的
# 那个守门**有效 —— uq_dc_quality_issue 就是后一种（只有一个对象，值得点名）。
# 类别的字面名字（`CLASS_KIND` 的键）与案例 kind 的对应：PRIMARY KEY ↔ pk、UNIQUE ↔ unique、
# NOT NULL ↔ notnull。三个字面名都是本文件自己写的，所以 C9 拿它认类别不需要猜。
CLASS_KIND = {'PRIMARY KEY': 'pk', 'UNIQUE': 'unique', 'NOT NULL': 'notnull'}
NOT_FALSIFIED = [
    ('PRIMARY KEY（除 dc_trading_calendar、risk_config_version、risk_switch_state）',
     '其余各表的主键都没有样本会因它消失而变红：一条重复插入要先被别的守门拒绝、'
     '才轮到主键。要证伪就得为每张表各写一条重复插入样本，而那只是把同一个机制再证一次。'
     '上面点名的三张表各有样本（dc-calendar-pk / risk-config-version-pk / '
     'risk-switch-state-pk）—— 两张单例表之所以够得着，是因为它们的重复插入样本本来就'
     '该写：单例 CHECK 只保证「只有 id = 1 那一行」。'),
    ('uq_dc_quality_issue',
     '没有任何样本会因它消失而变红 —— 它在这份 smoke 里只是被**回避**的：别的样本要'
     '小心别撞上它，才让自己的拒绝归因清楚。要证伪得新写一条「重复质量问题被接受」的样本。'),
    ('NOT NULL（除 risk_rule.threshold）',
     '其余每一列都没有样本喂 NULL ⇒ 删掉 NOT NULL 也不会有样本变红。'
     'A 段点名核对的是命名 ck_，NOT NULL 不在里面。'),
]

# kind 的落地查询返回什么形态，expect 就必须是什么形态。少了这条，「expect 写错」
# 要等到真跑数据库（~90 次 docker exec）才会暴露，而它其实是纯文本错误。
EXPECT_SHAPE = {
    'unique': r'^unique=(yes|no)$',
    'pk': r'^pk=(yes|no)$',
    'notnull': r'^(true|false)$',
}


def guard_label(guard):
    """guard 的可打印形式（'check' 是约束名，'notnull' 是 (表名, 列名) 元组）。"""
    return guard if isinstance(guard, str) else '%s.%s' % guard


def registry_problems():
    """静态核对三张登记表（空列表 = 干净）。

    两个调用点：本工具的 --selftest（注入坏登记证明每条规则真的有牙），以及
    `registry_gate()` —— 后者被 `_main()` 在**起 docker 之前**调用，所以一份 falsify
    报告里的 `verdict: OK` 现在就蕴含「这些登记自洽」。这条接线是补上的：在此之前
    这套规则只在自测里跑过，真跑时没人调用它，而「没人调用」在一份报告上和「没问题」
    长得一模一样。

    门禁 tools/verify_data_center.py 的 C9 做的是**另一半**：把 CASES 与两份 DDL 对账
    （DDL 里数得出来的每条守门，要么有案例、要么在 NOT_FALSIFIED 里登记）。那部分只有
    门禁做 —— 工具不该再写一份 DDL 扫描器。C9 **不 import 本文件**（import 就会真去
    shell 出 docker 并重写一份报告），它用 ast.parse 读同样的登记表；两份实现靠样本
    而不是靠共享代码保持同步。
    """
    problems = []
    tags = [c[0] for c in CASES]
    dup = sorted({t for t in tags if tags.count(t) > 1})
    if dup:
        problems.append('CASES 里有重复的 tag: %s' % ', '.join(dup))
    unknown = sorted(set(KINDS) - set(tags))
    if unknown:
        problems.append('KINDS 登记了不存在的案例（tag 拼错？）: %s' % ', '.join(unknown))
    for tag in tags:
        kind = KINDS.get(tag, 'check')
        if kind not in LANDINGS:
            problems.append('案例 %s 的 kind=%r 在 LANDINGS 里没有落地查询' % (tag, kind))
    for c in CASES:
        tag, expect = c[0], c[5]
        kind = KINDS.get(tag, 'check')
        shape = EXPECT_SHAPE.get(kind)
        if shape and not re.match(shape, expect):
            problems.append('案例 %s（kind=%s）的 expect 应该是 %s 这个形态，实得 %r'
                            % (tag, kind, shape, expect))
    # 落地查询模板的**参数摊法**：`%s` 个数要与 guard 的形态配得上。这条是补上的 ——
    # 2026-10-02 实测在真跑数据库（~90 次 docker exec、数分钟）时才崩在
    # `TypeError: not enough arguments for format string`（unique 那条要点名两次：
    # pg_constraint 一次、pg_indexes 一次，而 guard 是 str），bug 本身却纯是文本的。
    # 两个方向都要报：占位符太少（模式串吃了类型不符的参数）与**一个都没有**
    # （`模板 % ()` 会安静地返回常量 ⇒ 落地查询对所有 guard 给同一个答案，看起来
    # 像「守门真的没了」，而它其实没在看守门）。
    for c in CASES:
        tag, guard = c[0], c[3]
        kind = KINDS.get(tag, 'check')
        if kind not in LANDINGS:
            continue
        n_ph = LANDINGS[kind][0].count('%s')
        if n_ph == 0:
            problems.append('案例 %s（kind=%s）的落地查询模板一个 %s 都没有 —— 它会对'
                            '所有 guard 返回同一个答案' % (tag, kind, '%s'))
        elif isinstance(guard, tuple) and n_ph != len(guard):
            problems.append('案例 %s（kind=%s）的 guard 是 %d 元组，落地查询模板却要 %d 个参数'
                            % (tag, kind, len(guard), n_ph))
    for i, entry in enumerate(NOT_FALSIFIED, 1):
        if len(entry) != 2:
            problems.append('NOT_FALSIFIED[%d] 不是 (名字, 理由) 两元组: %r' % (i, entry))
            continue
        name, why = entry
        if not name.strip():
            problems.append('NOT_FALSIFIED[%d] 没有名字' % i)
        if len(why.strip()) < 20:
            problems.append('NOT_FALSIFIED[%d]（%s）的理由太短，读的人没法判断它为什么够不着'
                            % (i, name))
    # 类别兜底条目「<类别>（除 A、B）」的**格式**。格式坏掉时它会静默退化成「只有它自己
    # 有效」（读起来像兜底失效），而报告里看不出来；「除」后面点名的那些必须真的各有一条
    # 案例（否则那句例外是空话，读者会以为它们已被证伪）。
    case_kind = {guard_label(c[3]): KINDS.get(c[0], 'check') for c in CASES}
    for i, entry in enumerate(NOT_FALSIFIED, 1):
        if len(entry) != 2 or not isinstance(entry[0], str):
            continue
        name = entry[0]
        # 裸的类别名（没有任何例外）= 给整个类别兜底，合法。
        if name in CLASS_KIND:
            continue
        # 「类别名 + 别的东西」但没写「（除 …）」：读的人会以为它只护一个叫这个名字的守门，
        # 而 DDL 里没有那个名字 ⇒ 兜底范围静默地变成空集。C9 也会报，但这里报得更早。
        guess = [tok for tok in CLASS_KIND if name.startswith(tok)]
        if guess and '（' not in name:
            problems.append('NOT_FALSIFIED[%d]（%s）看起来是给 %r 整个类别兜底，但没写'
                            '「（除 …）」：写成「%s（除 A、B）」，或者干脆只写类别名'
                            % (i, name, guess[0], guess[0]))
            continue
        if '（' not in name and '）' not in name:
            continue
        m = re.match(r'^([A-Z][A-Z ]*)（除 (.+)）$', name)
        if not m:
            problems.append('NOT_FALSIFIED[%d]（%s）的「（除 …）」写法不对：必须是'
                            '「<类别>（除 A、B）」且（）在最末尾' % (i, name))
            continue
        cls = m.group(1)
        if cls not in CLASS_KIND:
            problems.append('NOT_FALSIFIED[%d] 的类别前缀 %r 不在 CLASS_KIND 里（%s）—— 门槛'
                            '靠它认类别，自造一个前缀会让兜底范围读不出来'
                            % (i, cls, '、'.join(sorted(CLASS_KIND))))
            continue
        for exc in [x.strip() for x in m.group(2).split('、')]:
            if case_kind.get(exc) != CLASS_KIND[cls]:
                problems.append('NOT_FALSIFIED[%d]（%s）把 %r 当成本类别里「已有案例」的例外，'
                                '但 CASES 里没有 kind=%s 的案例 guard 是它'
                                % (i, name, exc, CLASS_KIND[cls]))
    # docstring 里那句「覆盖统计：N 个案例 / M 条命名 CHECK（数据中心 D 个案例、风控 R 个…）」
    # 是给人读的覆盖声明。没有它 ⇒ 边界只存在于数据里；跟 CASES 不接 ⇒ 它是一句会老去的话。
    # 四个数都核，包括每份 DDL 各占多少 —— 「总数改对了、拆分忘了改」是这里的常态。
    claim = re.search(r'覆盖统计：(\d+) 个案例 / (\d+) 条命名 CHECK'
                      r'（数据中心 (\d+) 个案例、风控 (\d+) 个', __doc__ or '')
    if not claim:
        problems.append('docstring 里没有「覆盖统计：N 个案例 / M 条命名 CHECK（数据中心 D 个案例、'
                        '风控 R 个…）」这句话 —— 它是这份文件对外声明的边界，删了它就该同批改这里')
    else:
        claim_cases, claim_checks, claim_dc, claim_risk = (int(g) for g in claim.groups())
        if claim_cases != len(CASES):
            problems.append('docstring 说 %d 个案例，CASES 里数出来 %d 个'
                            % (claim_cases, len(CASES)))
        if claim_dc + claim_risk != claim_cases:
            problems.append('docstring 自己的拆分就对不上：数据中心 %d + 风控 %d != %d 个案例'
                            % (claim_dc, claim_risk, claim_cases))
        n_check_cases = {c[3] for c in CASES if KINDS.get(c[0], 'check') == 'check'}
        if claim_checks != len(n_check_cases):
            problems.append('docstring 说 %d 条命名 CHECK，CASES 里数出来 %d 条'
                            % (claim_checks, len(n_check_cases)))
        for want, rel, label in ((claim_dc, DC_DDL, '数据中心'),
                                 (claim_risk, RISK_DDL, '风控')):
            have = len([c for c in CASES if c[1] == rel])
            if want != have:
                problems.append('docstring 说%s %d 个案例，CASES 里数出来 %d 个'
                                % (label, want, have))
    return problems


def registry_gate():
    """(退出码, 问题列表)。退出码非 0 时调用方**不许继续**。

    抽成函数有两个理由：① 它要被**真跑**调用（`_main()` 在起 docker 之前），而不是只
    活在 --selftest 的断言里；② 这样能给它造坏样本而不需要数据库（见 selftest 的
    registry_gate-NEG/POS 一对）。
    """
    problems = registry_problems()
    return (EXIT_GUARD if problems else EXIT_OK), problems


def _line_of(text, needle):
    """needle 首次出现的 1-based 行号（回显给肉眼核对，免得改错位置还看不出来）。"""
    idx = text.find(needle)
    return text.count('\n', 0, idx) + 1 if idx >= 0 else -1


def _nth_of(text, needle, k):
    """needle 第 k 次（1-based）出现的下标；不够 k 次就抛（消歧失败必须响亮）。"""
    idx, cursor = -1, 0
    for _ in range(k):
        idx = text.find(needle, cursor)
        if idx < 0:
            raise SelfAssert('找不到 %r 的第 %d 次出现 —— 消歧次数与实际不符' % (needle, k))
        cursor = idx + len(needle)
    return idx


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
    for n, edit in enumerate(edits, 1):
        if len(edit) == 3:
            old, new, nth = edit
        else:
            old, new, nth = edit[0], edit[1], None
        if '\n' in old:
            raise SelfAssert('edits[%d] 的 old 跨行 —— 行号回显与「恰好一次」都失去意义' % n)
        hits = mutated.count(old)
        if nth is None:
            if hits != 1:
                raise SelfAssert(
                    'edits[%d] 的目标片段在 %s 中出现 %d 次（要求恰好 1 次）\n'
                    '      片段: %r' % (n, rel_ddl, hits, old))
        else:
            if not isinstance(nth, int) or isinstance(nth, bool) or nth < 1:
                raise SelfAssert('edits[%d] 带消歧的次数，但它不是 >= 1 的整数（%r）' % (n, nth))
            if hits < 2:
                raise SelfAssert(
                    'edits[%d] 说「第 %d 次出现」，可这个片段在 %s 里只出现 %d 次 —— '
                    '消歧的理由已经不在了，把次数去掉，让它回到「恰好一次」的检查'
                    % (n, nth, rel_ddl, hits))
            if hits < nth:
                raise SelfAssert(
                    'edits[%d] 说「第 %d 次出现」，而 %s 里只有 %d 次\n'
                    '      片段: %r' % (n, nth, rel_ddl, hits, old))

        line = _line_of(raw, old)
        if nth is not None:
            at = _nth_of(raw, old, nth)
            line = raw.count('\n', 0, at) + 1
        print('  放宽[%d] %s:%d%s' % (n, rel_ddl, line,
                                   '' if nth is None else '（第 %d 次出现）' % nth))
        print('          %s' % old)
        print('       -> %s' % new)
        if nth is None:
            mutated = mutated.replace(old, new)
        else:
            at = _nth_of(mutated, old, nth)
            mutated = mutated[:at] + new + mutated[at + len(old):]

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


def landing_args(kind, guard):
    """把 guard 摊成落地查询模板要的参数。

    **这里是唯一知道「模板吃几个参数」的地方。** 两个模板的 `%s` 个数不一样：
    `unique` 要点名两次（pg_constraint 一次、pg_indexes 一次），其余各一次；
    而 guard 是 str（check/unique/pk 用名字或表名）或 2 元组（notnull 用表名+列名）。
    写错的表现是 `TypeError: not enough arguments for format string` —— 2026-10-02
    实测栽在这上面一次，而它要等到真跑数据库（~90 次 docker exec、数分钟）才暴露，
    其实是纯文本错误。所以既抽出这个纯函数，又在 --selftest 里对**每个 kind 各跑一次**。
    """
    tmpl = LANDINGS[kind][0]
    if isinstance(guard, tuple):
        return guard
    return (guard,) * tmpl.count('%s')


def landing(kind, guard):
    """跑该 kind 的落地查询，取回这个守门**现在**的样子。

    SQL 在 LANDINGS 里。三件事缺一不可：查询失败 ⇒ 抛；**输出为空 ⇒ 抛**（名字写错、
    表不存在都表现为空，静默通过等于根本没验）；返回值交给 _bare() 归一化后与 expect 比对
    （PostgreSQL 会把定义重新排版并加 ::text 之类转型，拿源码原文去匹配是注定失败的）。
    """
    sql_tmpl, what = LANDINGS[kind]
    rc, last, out = scalar(sql_tmpl % landing_args(kind, guard))
    if rc != 0:
        raise SelfAssert('落地查询失败（kind=%s, guard=%r, exit=%d）:\n%s'
                         % (kind, guard, rc, out[-800:]))
    if not last.strip():
        raise SelfAssert('落地查询返回空（kind=%s, guard=%r）：guard 该是 %s。'
                         '空输出不能算「守门真的没了」' % (kind, guard, what))
    return last


def case(tag, rel_ddl, rel_smoke, guard, edits, expect, kind='check'):
    print('')
    print('=== %s  [%s] ===' % (tag, kind))
    print('  守门: %r   （落地查询的输出里必须出现 %r）' % (guard, expect))

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

    # 自 assert 4：变异必须真的落到这个守门上。清 schema 只证明 DDL 被执行了，
    # 不证明改的是它（片段可能命中注释、或改错了表）。
    ddef = landing(kind, guard)
    if _bare(expect) not in _bare(ddef):
        raise SelfAssert('%r（kind=%s）落地查询的输出里没有 %r —— 变异没落到这个守门上\n'
                         '      实际输出: %s' % (guard, kind, expect, ddef))
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

    pg_image, report_arg, flags = parse_args(sys.argv[1:])
    if flags['selftest']:
        return selftest()
    if pg_image is not None:
        ENV['PG_IMAGE'] = pg_image
    if flags['list']:
        return _main()

    # --- 0. 报告落点 ------------------------------------------------------
    # 与 run_sql_smoke 同一条理由：参数写错时**绝不退回默认路径**。退回默认路径恰好会
    # 覆盖上一轮（例如 postgres:17）的证据，而覆盖它的还是一份「本次没跑成」的报告。
    # 宁可什么都不写 —— 快照没有撤销。
    if report_arg is not None:
        new_path, why = set_report_path(report_arg)
        if new_path is None:
            print('GATE FAIL: --report 不可用 -- %s' % why)
            print('          报告写不到指定位置时**不会**退回默认路径：那会覆盖上一轮的证据，')
            print('          而且覆盖它的还是一份「本次没跑成」的报告。故本次不写任何报告。')
            return EXIT_GUARD
        print('report target: %s' % new_path)
        print('              （默认落点是 %s；本次是另存，不会动它）' % DEFAULT_REPORT)
        print('')

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
        fh.write('# 镜像: %s   digest: %s   server: PostgreSQL %s\n'
                 % (OBSERVED['image'] or '(未取到)', OBSERVED['digest'] or '(未取到)',
                    OBSERVED['version'] or '(未取到)'))
        fh.write('# 判定: %s (exit=%d)\n' % (verdict, code))
        fh.write('#\n')
        fh.write('# 上面这行「镜像」与正文里那句 `image  : postgres:N` 是同一个值的两种写法。\n')
        fh.write('# 判据只认正文那种（tools/verify_data_center.py C6 / verify_risk_config.py C17\n')
        fh.write('# 按 `image  :` 取镜像名），所以**正文那句不能丢**：丢了会让本快照在证据集里\n')
        fh.write('# 贡献 0 个镜像，而那种「分量变轻」在报告上看起来比真通过还干净。\n')
        fh.write('#\n\n')
        fh.write(buf.getvalue())
    print('报告: %s  (%s)' % (REPORT, verdict))
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
        for tag, ddl, smoke, guard, edits, expect in CASES:
            print('%-32s %-9s %-26s %s'
                  % (tag, KINDS.get(tag, 'check'), guard_label(guard), ddl))
        return EXIT_OK

    # 登记表与 CASES 对不上 ⇒ 这份「覆盖 N 条守门」的声明自己说不通 ⇒ 不值得花 38 次
    # docker exec 去证明它。放在 docker 之前，所以一张报告里的 `verdict: OK` 现在就蕴含
    # 「几份登记自洽」；把 CASES 与两份 DDL 对账的那一半是门禁 C9 的事（工具不做：它不该
    # 再写一份 DDL 扫描器）。
    gate_rc, gate_problems = registry_gate()
    if gate_rc != EXIT_OK:
        print('REGISTRY INCONSISTENT: 登记表与 CASES 对不上 —— 先修它们，再跑。')
        for problem in gate_problems:
            print('  - %s' % problem)
        print('这不是「通过」，也不是「发现缺陷」：是这份覆盖声明自己说不通。')
        return gate_rc

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

    # --- 实测环境：这三个值决定了「本次结论在哪个镜像上成立」------------------
    # `image  : postgres:N` 这一行的形状被两个门禁当判据读（C6/C17），所以它是输出
    # 契约的一部分，不是给人看的注释。取不到时留 `(未取到)` 并吼一声：一份「没写镜像」
    # 的证伪快照既不能被核对、也不该被当成证据用，而门禁会因此判 FAIL（fail-closed）。
    print('--- 实测环境 ---')
    version, image, digest = _environment()
    OBSERVED['version'], OBSERVED['image'], OBSERVED['digest'] = version, image, digest
    print('  image  : %s' % (image or '(未取到)'))
    print('  digest : %s' % (digest or '(未取到)'))
    print('  server : PostgreSQL %s' % (version or '(未取到)'))
    if not image or not digest:
        print('WARNING: 取不到 image/digest —— 本次结论无法标注「在哪个镜像上证伪过」。')
        print('         报告里缺 `image  : postgres:N` 会让证据集少一个分量，')
        print('         tools/verify_data_center.py C6 / verify_risk_config.py C17 会判 FAIL。')
    print('')

    results = []
    try:
        try:
            for tag, ddl, smoke, guard, edits, expect in CASES:
                results.append((tag, case(tag, ddl, smoke, guard, edits, expect,
                                          KINDS.get(tag, 'check'))))
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
    # 这几个数都**数出来**，不写死：写死的分母在加案例时会静默漂移，
    # 而「报告里的计数比真实覆盖漂亮」正是本仓库反复踩过的那一类假绿。
    n_cases = len(CASES)
    check_cases = [c for c in CASES if KINDS.get(c[0], 'check') == 'check']
    n_checks = len({c[3] for c in check_cases})
    guards = {}
    for c in CASES:
        guards.setdefault(KINDS.get(c[0], 'check'), []).append(c[0])
    print('         覆盖: %d 个案例 / %d 条命名 CHECK 约束' % (n_cases, n_checks))
    print('               （ck_risk_rule_ratio_range 上下界各一例，所以 CHECK 类的案例数'
          '比约束数多 %d）' % (len(check_cases) - n_checks))
    print('         非 CHECK 守门: %d 类 / %d 个案例（%s）'
          % (len(guards) - (1 if 'check' in guards else 0),
             n_cases - len(check_cases),
             '、'.join('%s %d 个' % (k, len(v))
                      for k, v in sorted(guards.items()) if k != 'check')))
    print('               （每类都有自己的落地查询：%s）'
          % '、'.join(k for k in sorted(LANDINGS) if k != 'check'))
    print('         够不着的守门: %d 条（理由写在 NOT_FALSIFIED 里；C9 静态核对每条都有理由、'
          '且没有守门既无案例又无登记）' % len(NOT_FALSIFIED))
    for name, _why in NOT_FALSIFIED:
        print('           - %s' % name)
    print('         另一半边界: 本工具只证明「被放宽的那一条会红」，不证明「除它以外全是绿」——')
    print('                 后者靠 n_fail == 1 这个签名间接兜住（变异波及多例会判 MISSED）。')
    return EXIT_OK


# ---------------------------------------------------------------------------
# 自测：只测「参数怎么解析」与「报告落点怎么守」，**不需要 docker**。
# 落点规则本身与 tools/run_sql_smoke.py 共用（import 那边一个纯函数），但坏样本的
# **后果**在两边不同：这边一旦半途改了 REPORT，会覆盖掉上一轮的 falsify 快照。
# ---------------------------------------------------------------------------

def selftest():
    ok = True

    def check(tag, cond, detail=''):
        nonlocal ok
        print('  [%s] %s%s' % (tag, 'OK' if cond else 'MISSED',
                               (' -- ' + detail) if detail else ''))
        ok = ok and cond

    pg, rep, _ = parse_args(['--pg-image=postgres:14', '--report=tools/x.txt'])
    check('args-POS-both', (pg, rep) == ('postgres:14', 'tools/x.txt'), 'got %r' % ((pg, rep),))

    # 关键样本：`--report=` 后面留空**必须**带着空串走到守卫里报错，而不是被当成
    # 「没传这个开关」⇒ 悄悄写默认落点 ⇒ 覆盖上一轮证据。这两个断言是同一个坑的两半：
    # 前一半证明解析器没把它吞掉，后一半证明守卫真的拒绝它。
    _, rep_empty, _ = parse_args(['--report='])
    check('args-NEG-empty-report-is-not-none', rep_empty == '', 'got %r' % (rep_empty,))
    check('args-NEG-empty-report-rejected', set_report_path(rep_empty)[0] is None)

    _, _, flags = parse_args(['--list'])
    check('args-POS-list', flags['list'] is True and flags['selftest'] is False)
    _, _, flags = parse_args(['--selftest'])
    check('args-POS-selftest', flags['selftest'] is True and flags['list'] is False)

    saved = REPORT
    check('default-target-untouched', saved == DEFAULT_REPORT, saved)

    def rcase(tag, value, want_ok):
        path, why = set_report_path(value)
        hit = ((path is not None) == want_ok)
        untouched = want_ok or (REPORT == saved)
        globals()['REPORT'] = saved
        check(tag, hit and untouched,
              'ok=%s untouched=%s why=%s' % (path is not None, untouched, why or ''))

    rcase('report-NEG-empty', '', False)
    rcase('report-NEG-is-a-directory', 'tools', False)
    rcase('report-NEG-parent-missing', 'tools/__selftest_no_such_dir__/x.txt', False)
    rcase('report-POS-relative', 'tools/__selftest_target__.txt', True)
    rcase('report-POS-absolute', os.path.join(ROOT, '__selftest_target__.txt'), True)

    # 另存不许动默认落点：五个样本跑完，默认路径必须还是它自己（否则下一轮不带参数的
    # 那次运行就会写到一个别的地方，看起来「跑了」，其实没人知道写到哪去了）。
    check('report-default-survives-samples', REPORT == DEFAULT_REPORT, REPORT)

    # --- 登记表三张 + 变异器的纯文本自测（不需要 docker）----------------------
    # 这几条原来只有跑完 38 次 docker exec 才可能暴露，可 bug 本身是纯文本的：
    # kind 拼错、expect 写错形态、消歧次数写歪、docstring 里的计数跟 CASES 脱钩。
    probs = registry_problems()
    check('registry-POS-clean', probs == [], '; '.join(probs))

    saved_cases = list(CASES)
    saved_kinds = dict(KINDS)
    saved_nf = list(NOT_FALSIFIED)
    saved_doc = __doc__

    def rreg(tag, want):
        """注入一个坏登记 ⇒ 期望 registry_problems() 报出 want 这句话，然后复原。"""
        probs = registry_problems()
        hit = any(want in p for p in probs)
        CASES[:] = saved_cases
        KINDS.clear()
        KINDS.update(saved_kinds)
        NOT_FALSIFIED[:] = saved_nf
        globals()['__doc__'] = saved_doc
        check(tag, hit, '; '.join(probs) or '(什么都没报 —— 探测器没接上)')

    KINDS['__selftest_no_such_tag'] = 'pk'
    rreg('registry-NEG-kind-registered-for-a-ghost-case', '不存在的案例')

    KINDS['dc-calendar-pk'] = 'nope'
    rreg('registry-NEG-kind-has-no-landing-query', '没有落地查询')

    KINDS['dc-calendar-pk'] = 'pk'
    CASES.append(('__selftest_shape', DC_DDL, DC_SMOKE, 'x', [('a', 'b')], 'maybe'))
    KINDS['__selftest_shape'] = 'pk'
    rreg('registry-NEG-expect-wrong-shape', 'expect 应该是')

    CASES.append(('__selftest_dup', DC_DDL, DC_SMOKE, 'x', [('a', 'b')], 'pk=no'))
    CASES.append(('__selftest_dup', DC_DDL, DC_SMOKE, 'x', [('a', 'b')], 'pk=no'))
    KINDS['__selftest_dup'] = 'pk'
    rreg('registry-NEG-duplicate-tag', '重复的 tag')

    NOT_FALSIFIED.append(('__selftest_nf', ''))
    rreg('registry-NEG-not-falsified-without-a-reason', '理由太短')

    NOT_FALSIFIED.append(('__selftest_nf_bad',))
    rreg('registry-NEG-not-falsified-not-a-pair', '两元组')

    globals()['__doc__'] = saved_doc.replace('覆盖统计：38 个案例', '覆盖统计：99 个案例')
    rreg('registry-NEG-docstring-count-drift', 'docstring 说 99 个案例')

    globals()['__doc__'] = saved_doc.replace('数据中心 20 个案例', '数据中心 19 个案例')
    rreg('registry-NEG-docstring-split-drift', 'docstring 说数据中心 19 个案例')

    # 类别兜底条目「<类别>（除 A、B）」的三条分支。它存在的理由是「整个类别都够不着，
    # 但其中几个已被单独证明」——所以写法错、或例外并不存在，都会让兜底范围变得不可读：
    # 前者让它退化成「只有它自己有效」（看起来像兜底没用），后者让读者以为那几个已被证伪。
    NOT_FALSIFIED[0] = ('PRIMARY KEY 除 dc_trading_calendar', NOT_FALSIFIED[0][1])
    rreg('registry-NEG-class-prefix-without-except-clause', '没写')

    NOT_FALSIFIED[0] = ('PRIMARY KEY（除 dc_trading_calendar', NOT_FALSIFIED[0][1])
    rreg('registry-NEG-class-exception-malformed', '写法不对')

    NOT_FALSIFIED[0] = ('PRIMARY KEYS（除 dc_trading_calendar）', NOT_FALSIFIED[0][1])
    rreg('registry-NEG-class-prefix-unknown', '不在 CLASS_KIND 里')

    NOT_FALSIFIED[0] = ('PRIMARY KEY（除 __selftest_never_a_case）', NOT_FALSIFIED[0][1])
    rreg('registry-NEG-class-exception-without-a-case', '没有 kind=pk 的案例')

    # 落地查询模板的参数摊法（两个方向各一条）：模板零占位符 ⇒ 对每个 guard 返回同一个
    # 常量（看着像「守门没了」其实没在看守门）；模板占位符数与元组 guard 的元数不符 ⇒
    # 真跑时 TypeError。两条都注入假模板，跑完立刻复原（rreg 不管 LANDINGS）。
    saved_land = dict(LANDINGS)
    LANDINGS['unique'] = ("SELECT CASE WHEN true THEN 'unique=yes' ELSE 'unique=no' END",
                          '__selftest 零占位模板__')
    rreg('registry-NEG-template-without-placeholders', '一个 %s 都没有')
    LANDINGS.clear()
    LANDINGS.update(saved_land)

    LANDINGS['notnull'] = ("SELECT '%s' || '%s' || '%s'", '__selftest 三占位模板__')
    rreg('registry-NEG-template-arity-vs-tuple-guard', '模板却要 3 个参数')
    LANDINGS.clear()
    LANDINGS.update(saved_land)

    # landing_args() 的正样本：登记表里**每个 kind 各格式化一次**（不需要 docker）。
    # 这条正是那个 TypeError 的对面 —— 只测 NEG 的话，「所有 kind 都摊得出参数」没人守着。
    kinds_seen = sorted({KINDS.get(c[0], 'check') for c in CASES})
    fmt_err = []
    for kind in kinds_seen:
        guard = next(c[3] for c in CASES if KINDS.get(c[0], 'check') == kind)
        try:
            LANDINGS[kind][0] % landing_args(kind, guard)
        except Exception as exc:            # noqa: BLE001 -- 报出来比崩在半路好
            fmt_err.append('%s: %s' % (kind, exc))
    check('landing-args-POS-every-kind-formats', not fmt_err,
          'kinds=%s %s' % ('/'.join(kinds_seen), '; '.join(fmt_err) or '都摊得出参数'))

    # registry_gate()：登记表不一致必须**挡住真跑**，而不只是被 --selftest 记一笔 ——
    # 这条接线原来不存在（规则只在自测里跑），而「没人调用」在一份报告上和「没问题」
    # 长得一样。一对样本：坏登记 ⇒ EXIT_GUARD（在花掉 38 次 docker exec 之前就停），
    # 干净登记 ⇒ EXIT_OK。
    KINDS['__selftest_no_such_tag'] = 'pk'
    rc_bad, probs_bad = registry_gate()
    CASES[:] = saved_cases
    KINDS.clear()
    KINDS.update(saved_kinds)
    NOT_FALSIFIED[:] = saved_nf
    rc_ok, probs_ok = registry_gate()
    check('registry_gate-NEG-bad-registry-blocks-the-run',
          rc_bad == EXIT_GUARD and bool(probs_bad),
          'rc=%s 问题数=%d' % (rc_bad, len(probs_bad)))
    check('registry_gate-POS-clean-registry-runs',
          rc_ok == EXIT_OK and probs_ok == [], 'rc=%s 问题=%s' % (rc_ok, probs_ok))

    # 一条案例里两处编辑（同一个对象）：risk-config-version-pk 拿掉主键的同时必须把种子行的
    # ON CONFLICT (id) 冲突目标也去掉（否则变异后的 DDL 自己加载不动 = INCONCLUSIVE）。
    # 这里只核纯文本部分：两处都落了、第 2 处（另一张表）还在、delta 正好是两处之和。
    PK1, PK1B = '  PRIMARY KEY (id),', '  -- R28 mutation: primary key removed'
    OC, OCB = 'ON CONFLICT (id) DO NOTHING;', 'ON CONFLICT DO NOTHING;'
    exp2 = len(PK1B) - len(PK1) + len(OCB) - len(OC)
    try:
        dst2, delta2 = mutate(RISK_DDL, [(PK1, PK1B, 1), (OC, OCB)], 'mut-POS-two-edits')
        body2 = open(dst2, encoding='utf-8').read()
        ok2 = (PK1 in body2 and PK1B in body2 and OCB in body2 and OC not in body2
               and delta2 == exp2)
        detail2 = ('delta=%s（期望 %s）另一张表的主键还在=%s 冲突目标已去掉=%s'
                   % (delta2, exp2, PK1 in body2, OC not in body2))
    except SelfAssert as exc:
        ok2, detail2 = False, str(exc)
    check('mutate-POS-two-edits-on-one-guard', ok2, detail2[:140])

    # 变异器的「第几次出现」：它存在的唯一理由是**改对那一处**，所以正样本必须
    # 逐条钉住「剩下的是哪一处」，否则「改了第二处」与「把两处都改了」看不出区别。
    THR = 'threshold    numeric(18,8)  NOT NULL,'
    NEW_THR = 'threshold    numeric(18,8),'

    def mcase(tag, rel_ddl, edits, want_error=None, want_after_thr=None):
        try:
            dst, delta = mutate(rel_ddl, edits, tag)
            err = None
        except SelfAssert as exc:
            dst, delta, err = None, None, str(exc)
        if want_error is not None:
            check(tag, err is not None and want_error in err,
                  (err or 'delta=%s（本该报错）' % delta)[:120])
            return
        ok, detail = err is None, 'delta=%s' % delta
        if err is None:
            body = open(dst, encoding='utf-8').read()
            # 逐字节：多改一处也会让这条挂掉（这正是「改对那一处」的意思）。
            exp_delta = sum(len(e[1]) - len(e[0]) for e in edits)
            ok = ok and delta == exp_delta
            lines = [k for k, ln in enumerate(body.split('\n')) if THR in ln]
            after = body.split('\n')[lines[0] + 1].strip() if lines else '(没有剩下 NOT NULL)'
            ok = ok and len(lines) == 1 and want_after_thr in after
            detail = 'delta=%s（期望 %s）剩下的 NOT NULL 后面一行=%r' % (delta, exp_delta, after)
        check(tag, ok, detail[:140])

    mcase('mutate-NEG-fragment-absent', RISK_DDL,
          [('__selftest_no_such_fragment__', 'x')], want_error='出现 0 次')
    mcase('mutate-NEG-two-hits-without-nth', RISK_DDL,
          [(THR, NEW_THR)], want_error='出现 2 次')
    mcase('mutate-NEG-crosses-a-line', RISK_DDL,
          [('threshold    numeric(18,8)\n  NOT NULL,', 'x')], want_error='跨行')
    mcase('mutate-NEG-nth-is-not-an-int', RISK_DDL,
          [(THR, NEW_THR, True)], want_error='不是 >= 1 的整数')
    mcase('mutate-NEG-nth-past-the-end', RISK_DDL,
          [(THR, NEW_THR, 3)], want_error='只有 2 次')
    mcase('mutate-NEG-nth-when-the-fragment-is-unique', DC_DDL,
          [('CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_data_version_active',
            'CREATE INDEX IF NOT EXISTS uq_dc_data_version_active', 1)],
          want_error='消歧的理由已经不在了')
    # 正样本：第 1 次 = risk_rule（它是**被改掉**的那一处，所以剩下的是
    # risk_intercept_log 的那一行，它后面是 observed），第 2 次正好相反（剩下的是 unit）。
    # 钉「剩下的那一行后面是什么」而不是钉行号 —— 行号会随插入漂走。
    mcase('mutate-POS-nth-1-hits-risk_rule', RISK_DDL, [(THR, NEW_THR, 1)],
          want_after_thr='observed')
    mcase('mutate-POS-nth-2-hits-risk_intercept_log', RISK_DDL, [(THR, NEW_THR, 2)],
          want_after_thr='unit')

    shutil.rmtree(TMP_DIR, ignore_errors=True)

    print('note: 这里只跑了自测 —— db/*.sql 与四份快照的结论由**不带 --selftest 的**那次'
          '运行负责判。')
    print('SELFTEST %s: 参数解析 + --report 落点守卫（含「空值不是没传」与「失败不许改全局」）'
          ' + 三张登记表 + 变异器（含「第几次出现」的两个方向）'
          % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
