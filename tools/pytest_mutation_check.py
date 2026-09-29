"""变异检查：把 quanauto 的实现逐处改坏，确认对应套件真的会红。

基线跑八个套件（即下面那些常量）：
  * `tests/test_backtest_slice.py`（I1 回测切片）
  * `tests/test_data_center_adapter.py`（I2 S2 适配器，含复权帧的 `validate_frame` 判据）
  * `tests/test_data_center_store.py`（I2 S3 落库侧，含 A6 的因子读写）
  * `tests/test_backtest_db_feed.py`（I2 库喂数据的回测侧）
  * `tests/test_data_center_pit.py`（I2 A6：复权因子的读数侧与 PIT 守卫）
  * `tests/test_backtest_risk_gate.py`（I3 风控闸门）
  * `tests/test_risk_store.py`（I3b 规则存储 + 拦截留痕）
  * `tests/test_dashboard.py`（I4 绩效看板：只读数不算数 + CLI 接线）
（本仓库对门禁的同一条纪律：每个自建检查器都要做触发测试；测试套件就是检查器。）

**纪律（每条都对应过一次真实的假绿）**：
  * 先跑一次干净的全绿当基线；基线不绿的话后面的「红」说明不了任何事。
  * 每处变异都**自 assert 已应用**（`old` 必须在文件里恰好出现一次）。没打上去的变异
    会伪装成「测试没抓到」，这是最容易骗过人的假结果。
  * 变异必须打在**断言真的会看的地方**（改注释、改空格不算）—— 唯一的例外是下面
    那条 `CONTROL`，它存在的意义恰恰是证明本检查器**会**报 `caught=NO`。
  * 恢复后**逐字节**比对备份，不是「大概改回来了」。
  * `MUTATION` 触发出来的失败必须是**断言/异常**，不能是 `ImportError`/语法错
    （收集阶段就炸掉，等于测试根本没跑）。

**它不进 `run_all_gates.py` 的注册表**：每条样本要跑一次 pytest（2026-09-29 晚 Ⅳ「复权帧的 `validate_frame`
判据」后实测 **73 条样本：67 条变异 + 6 条 CONTROL + 0 条 ENV-LIMIT**（基线八套件 `passed=388`；报告里那行
`mutations=67 caught=67 control=6 env_limited=0` 的 `mutations` **只数变异、不含 CONTROL**，所以总数是 73
而不是 67 —— 别把两处加混了）；同日晚 Ⅲ「CSV 侧复权口径」那轮是 68 条 = 63 + 5 + 0
（基线七套件 `passed=252`）；同日晚 Ⅱ「复权价实施」那轮是 66 条 = 61 + 5 + 0
（基线七套件 `passed=249`）；同一日的 A6 收口那轮是 59 条 = 54 + 5 + 0
（基线七套件 `passed=241`），I4 是 54 条 = 49 + 5 + 0（基线六套件 `passed=181`），I3 那轮是 43 条 = 39 + 4 + 0
（基线五套件 `passed=145`），再往前 B20 后是 32 条 = 29 + 3 + 0），慢，且它验证的对象是测试而不是产物契约。

⚠️ **2026-09-29 A6 那一批同时干了三件事，别只看新增的 5 条**：
  ① 新增 `A1`~`A5`（因子读语句的版本过滤与窗口 / 因子 upsert 退化成裸 INSERT / 标度 8 塌成日线那个 4 /
     「HFQ 缺因子行静默补 1.0」——最后这条正是本轮修掉的那个缺口）；
  ② **重新锚定 5 条**（`S1`/`S2`/`S4`/`S9`/`S12b`）：新加的因子 SQL 让前三条的针从「命中 1 次」
     变成**命中 2 次**，导入行被改写让 `S9` 变成**命中 0 次**，`_as_decimal` 抽出 `_to_decimal`
     让 `S12b` 的针**命中 0 次**。工具的「恰好 1 次」自 assert 把它们全拦下了 ——
     **这批变异当时一条都没跑**，报告里 5 条 `[MUTATION-HARNESS]`。
     教训与铁律 ⑤ 同族：**新加的产物会让原本有效的变异悄悄变成 no-op**；
  ③ 基线从六个套件加到七个（补 `tests/test_data_center_pit.py`）—— 一条变异期望落在哪个套件，
     那个套件就必须在基线里，否则「红」没有全绿垫底，什么都证明不了。

⚠️ **2026-09-29 晚 Ⅳ（复权帧的 `validate_frame` 判据）又动了一次，两件事**：
  ① 新增 `A15`~`A18` 并按「变异要有对照」的惯例补一条 `CONTROL-comment-only-datasources`：
     `A15` 值域放成 `value <= -1e9`（等于复权因子不再拒非正）、`A16` 从 `_match_schema` 的元组表里
     抽掉 `('factor', ADJUST_FACTOR_COLUMNS)`（等于不再认这张 schema）、`A17` 给主键重复检查加
     `and kind != 'factor'`（等于复权帧的重复不报）、`A18` 把 `ADJUST_FACTOR_COLUMNS` 从
     `STANDARD_COLUMNS` 里拿掉（等于 `adjust_factor` 又被当成「多出来的列」）。
     **这是本仓库第一次有变异打在 `quanauto/datasources.py` 上**（此前那个文件零变异覆盖）。
  ② 基线从七个套件加到八个（补 `tests/test_data_center_adapter.py`）—— `A15`~`A18` 期望的失败
     全落在它里面，不在基线里的话「红」没有全绿垫底。

⚠️ **2026-09-29 晚「复权价实施」那一批：新增 7 条 + 重锚 1 条，是同一批**：
  ① **重锚 1 条**（`S11`）：`_row_to_bar` 的四个价格参数从裸 `_as_float(row.x)` 变成
     `_rescale_price(_as_float(row.x), factor)` ⇒ 旧针**命中 0 次**（工具拦成 `HARNESS-FAIL`）。
     一次真发生的"新产物把有效变异变成 no-op"（铁律⑤）。新针改成拿掉 `_as_float`：
     因子 1.0 时 `_rescale_price` 走恒等快路径原样返回那个 `Decimal` ⇒ `float * Decimal` 当场撞。
     顺带把本轮新的端到端控制组加进它的 `expect`（它走同一条读路径）。
  ② **新增 `A6`~`A12`**，每题都对着一类真实漏法：`A6` 前复权基准被忽略（`QFQ` 退化成 `HFQ`）、
     `A7` 连 `amount` 一起缩、`A8` `_rescale_price` 变恒等（整个复权功能不存在）、
     `A9` **读路径**把缺因子静默退回 1.0（注意 `A5` 只管因子接口那一层，这条管价格路径）、
     `A10` 只乘 `close`、`A11` 把 `volume` 也缩、`A12` bundle 里的因子钉成 1.0。
  ③ **`A12` 全仓库只由一条用例抓住**（`test_adjusted_prices_reach_the_strategy_through_the_engine`）
     —— 它改的是 bundle 的**声明**，价格仍然是对的，所以只有"策略拿它自己除一遍"的断言看得见。
     这是「端到端控制组必须有牙」的具体含义：跨层缝上的错，单层断言天然看不见。
  ④ `A10`/`A11` 这两条能成立，前提是夹具的四列价格**可分**（本轮给 `test_data_center_pit.py` 的
     `_row()` 加了 `open_/high/low` 参数）—— 夹具里 `open==high==low==close` 时，
     "只乘一列"与"全乘了"在数值上完全一样，变异会在**正确的实现上**保持沉默。

⚠️ **2026-09-29 晩 ⅲ「CSV 侧复权口径」那一批：新增 2 条 + 重锚 1 条，是同一批**：
  ① **重锚 1 条**（`A8` 的 `path`）：`_rescale_price` 的函数本体从 `quanauto/datacenter.py`
     下移到 `quanauto/datafeed.py`（两条实现体共用同一份算式）⇒ 这条变异的针**命中 0 次**。
     又一次"新产物把有效变异变成 no-op"（铁律⑤）—— 这次动的是**位置**而不是字。
  ② **新增 `A13`~`A14`**：`A13` CSV 侧完全不乘因子（就是本轮关掉的那个缺口本身）、
     `A14` CSV 侧只乘 `close`（`A10` 的第二条实现体版本）。
  ③ 为什么值得为第二条实现体各加一条：`DataFeed` 是**一个**协议、两条实现体。只端到端钉住
     `DbDataFeed` 时，`CsvDataFeed` 可以悄悄退回不复权口径，而所有单层用例各自仍是绿的
     —— 「量纲分裂」就是这么发生的，也是最难从报告里看出来的那一类错。
  ④ `A13`/`A14` 成主的前提是自造 CSV 的四列价格**可分**（`_adjust_csv_text()` 里
     `open/high/low/close = base, base+2, base-1, base+1`）—— 与 `A10` 同一条教训。
  ⚠️ 这一批**没有**动 `tools/run_all_gates.py` 的注册表（本检查器本来就不在里面）。
手动跑，或改完测试后跑一次。

**`env_limit`（一条变异的出口）**：有的缺陷在**当前环境里根本不可能被断言抓住**
（例：变异把「驱动惰性导入」改成模块顶层拉驱动 —— 若这个解释器里根本没装 psycopg，
结果只能是收集期 ImportError，那条断言真正生效的环境是装了驱动的环境）。
这类变异必须**逐条写明理由**并标为 `ENV-LIMIT`：它只证明「这里验不了」，
**不等于 PASS**，也不允许把一条没被抓到的变异事后追认为 `env_limit`。

⚠️ `env_limit` 的取值**随环境变**，不是常数：本仓库 `psycopg` 只声明在
`[project.optional-dependencies] postgres` 里（2026-09-25 裁决），而 extra 是 opt-in ——
CI 只装 `.[dev]`，所以 CI 里 `S9` 会退化成 `ENV-LIMIT`，
而本机 `.venv` 手工装了 `psycopg` 之后它就是**真的 CAUGHT**（2026-09-25 实测 `env_limited=0`）。
改环境后要重看这一栏，不要把上一轮的 `env_limited` 当现状引用。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TARGET = "tests/test_backtest_slice.py"
TESTS_ADAPTER = "tests/test_data_center_adapter.py"
TESTS_STORE = "tests/test_data_center_store.py"
TESTS_DB_FEED = "tests/test_backtest_db_feed.py"
TESTS_PIT = "tests/test_data_center_pit.py"
TESTS_RISK_GATE = "tests/test_backtest_risk_gate.py"
TESTS_RISK_STORE = "tests/test_risk_store.py"
TESTS_DASHBOARD = "tests/test_dashboard.py"
REPORT = os.path.join(ROOT, "tools", "pytest-mutation-report.txt")

CONTROL = "MUST-NOT-BE-CAUGHT"

# 控制台是 GBK，中文会花；报告文件用 UTF-8 落盘
LINES: list = []


def say(text: str) -> None:
    LINES.append(text)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))

# tag / 文件 / 原文 / 变异后 / 必须变红的测试（`CONTROL` = 必须不变红）
MUTATIONS = [
    {
        "tag": "M1-account-double-frozen",
        "path": "quanauto/broker.py",
        "old": "            total = available_capital + self._frozen + market_value\n",
        "new": "            total = available_capital + 2.0 * self._frozen + market_value\n",
        "expect": ["test_pending_buy_does_not_change_total_capital"],
    },
    {
        "tag": "M2-fill-at-close-not-open",
        "path": "quanauto/broker.py",
        "old": "        price = bar.open\n",
        "new": "        price = bar.close\n",
        "expect": ["test_trades_fill_at_bar_open"],
    },
    {
        "tag": "M3-slippage-silently-zero",
        "path": "quanauto/broker.py",
        "old": "    cost = slippage.fixed_slippage + rate * amount\n",
        "new": "    cost = 0.0\n",
        "expect": ["test_slippage_reduces_available_cash", "test_different_seed_changes_deterministic_section_under_slippage"],
    },
    {
        "tag": "M4-oversell-guard-gone",
        "path": "quanauto/broker.py",
        "old": "        return position is not None and position.quantity >= order.quantity\n",
        "new": "        return True\n",
        "expect": ["test_oversell_is_rejected_without_raising", "test_oversell_leaves_cash_untouched"],
    },
    {
        "tag": "M5-no-leakage-check-dead",
        "path": "quanauto/engine.py",
        "old": "            if trade.timestamp <= submitted:\n",
        "new": "            if False:\n",
        "expect": ["test_no_leakage_can_actually_fail"],
    },
    {
        "tag": "M6-get-result-silent-none",
        "path": "quanauto/engine.py",
        "old": (
            "        if self._result is None:\n"
            '            raise NoResultError("还没有跑过回测，没有结果可取")\n'
        ),
        "new": "        if self._result is None:\n            return None\n",
        "expect": ["test_get_result_before_run_raises"],
    },
    {
        "tag": "M7-run-partial-window-ignored",
        "path": "quanauto/engine.py",
        "old": "        if start < self._config.start_date or end > self._config.end_date:\n",
        "new": "        if False:\n",
        "expect": ["test_run_partial_rejects_window_outside_config"],
    },
    {
        "tag": "M8-no-leakage-silent-pass-without-run",
        "path": "quanauto/engine.py",
        "old": '                warnings=["尚未执行回测，未来函数检查没有任何样本可查"],\n',
        "new": "                warnings=[],\n",
        "expect": ["test_no_leakage_before_run_reports_warning_instead_of_silent_pass"],
    },
    {
        "tag": "CONTROL-comment-only",
        "path": "quanauto/broker.py",
        "old": "    # ── 订单",
        "new": "    # ── 订单（MUTATION：只改注释，必须抓不到）",
        "expect": CONTROL,
    },
    # ── I2 S3：落库侧（quanauto/pgstore.py ↔ tests/test_data_center_store.py） ──
    {
        # 2026-09-29 A6 重新锚定：这两行现在也是 `SQL_SELECT_FACTORS` 的同一行（因子读侧接上来了）
        # ⇒ 裸针从「命中 1 次」变成**命中 2 次**，被工具自己的自 assert 拦下（报告里 5 条 HARNESS 故障之一）。
        # 往前带一行 `FROM dc_daily_bar` 把它钉回**日线那一条**语句；因子那一条另有 A1 管。
        "tag": "S1-read-drops-version-filter",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": '"FROM dc_daily_bar "\n    "WHERE symbol = %s AND data_version = %s "',
        "new": '"FROM dc_daily_bar "\n    "WHERE symbol = %s "',
        "expect": ["test_select_bars_binds_the_data_version"],
    },
    {
        # 同一次重新锚定：窗口那两行在日线与因子两条语句里各出现一次 ⇒ 连带 `FROM` 行一起钉。
        "tag": "S2-read-window-becomes-open",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": ('"FROM dc_daily_bar "\n    "WHERE symbol = %s AND data_version = %s "\n'
                '    "AND trade_date >= %s AND trade_date <= %s "'),
        "new": '"FROM dc_daily_bar "\n    "WHERE symbol = %s AND data_version = %s "',
        "expect": ["test_select_bars_window_is_closed_and_ordered_in_sql"],
    },
    {
        "tag": "S3-symbols-lose-distinct",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": '"SELECT DISTINCT symbol FROM dc_daily_bar WHERE data_version = %s ORDER BY symbol"',
        "new": '"SELECT symbol FROM dc_daily_bar WHERE data_version = %s ORDER BY symbol"',
        "expect": ["test_select_symbols_is_distinct_and_versioned"],
    },
    {
        # 2026-09-29 A6 重新锚定：`ON CONFLICT (...) DO UPDATE ` 这一句在日线与因子两条 INSERT 里各一次。
        # 带上日线那条 `VALUES`（10 个占位符）就唯一了；因子那条另有 A2 管。
        "tag": "S4-write-becomes-plain-insert",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": ('"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "\n'
                '    "ON CONFLICT (symbol, trade_date, data_version) DO UPDATE "'),
        "new": '"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "',
        "expect": ["test_upsert_sql_follows_contract_rule_6"],
    },
    {
        # 2026-09-29 A6：因子读语句是这一轮新增的 SQL ⇒ 它自己的版本过滤没有探测器（S1 重新锚定后只管日线）。
        "tag": "A1-select-factors-drops-version-filter",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": '"FROM dc_adjust_factor "\n    "WHERE symbol = %s AND data_version = %s "',
        "new": '"FROM dc_adjust_factor "\n    "WHERE symbol = %s "',
        "expect": ["test_select_factors_sql_is_parameterized_and_versioned"],
    },
    {
        # 同一条语句的窗口（与 S2 成对：日线那条管不到因子这条）。
        "tag": "A2-select-factors-window-becomes-open",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": ('"FROM dc_adjust_factor "\n    "WHERE symbol = %s AND data_version = %s "\n'
                '    "AND trade_date >= %s AND trade_date <= %s "'),
        "new": '"FROM dc_adjust_factor "\n    "WHERE symbol = %s AND data_version = %s "',
        "expect": ["test_select_factors_sql_is_parameterized_and_versioned"],
    },
    {
        "tag": "A3-factor-upsert-becomes-plain-insert",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": '"VALUES (%s, %s, %s, %s, %s) "\n    "ON CONFLICT (symbol, trade_date, data_version) DO UPDATE "',
        "new": '"VALUES (%s, %s, %s, %s, %s) "',
        "expect": ["test_factor_upsert_sql_follows_contract_rule_6"],
    },
    {
        # 标度 8 是 D6 对 `dc_adjust_factor` 的约定（`numeric(18,8)`），与日线六列的 4 不是一回事。
        # 把两个标度混起来是这一层最容易犯的错（附录 B18.3 那条浮点尾巴就是这个家族的老祖宗）。
        "tag": "A4-factor-scale-collapses-to-the-bar-scale",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": "_FACTOR_SCALE = 8",
        "new": "_FACTOR_SCALE = 4",
        "expect": ["test_factor_scale_is_eight_not_the_bar_scale"],
    },
    {
        "tag": "S5-identical-row-writes-anyway",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            "            _raise_if_divergent(bar, existing[0])\n            return False\n"
            "        written = _run("
        ),
        "new": (
            "            _raise_if_divergent(bar, existing[0])\n            return True\n"
            "        written = _run("
        ),
        "expect": ["test_upsert_identical_rerun_writes_nothing"],
    },
    {
        "tag": "S6-empty-returning-counted-as-inserted",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            '        written = _run(self.conn, SQL_UPSERT_BAR, _insert_params(bar), "写入日线")\n'
            "        if written:\n            return True"
        ),
        "new": (
            '        written = _run(self.conn, SQL_UPSERT_BAR, _insert_params(bar), "写入日线")\n'
            "        return True"
        ),
        "expect": ["test_upsert_concurrent_identical_insert_is_skipped"],
    },
    {
        "tag": "S7-version-check-moves-inside-transaction",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            "        for bar in bars:\n"
            '            _require_version(bar.data_version, "行 %s/%s" % (bar.symbol, bar.trade_date))'
        ),
        "new": "        for bar in bars:\n            pass",
        "expect": ["test_upsert_rejects_a_blank_data_version_before_any_sql"],
    },
    {
        "tag": "S8-our-own-errors-get-wrapped",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            "        return list(conn.execute(sql, params))\n    except QuanAutoError:\n"
            "        raise\n    except Exception as exc:"
        ),
        "new": "        return list(conn.execute(sql, params))\n    except Exception as exc:",
        "expect": ["test_already_classified_error_from_a_lower_layer_passes_through"],
    },
    {
        # 2026-09-29 A6 重新锚定：A6 往这一行加了两个因子侧的名字
        # （`AdjustFactorPoint` / `FactorStore`）⇒ 旧针「命中 0 次」。
        "tag": "S9-driver-imported-eagerly",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": "from .datacenter import AdjustFactorPoint, BarStore, DailyBar, FactorStore",
        "new": ("from .datacenter import AdjustFactorPoint, BarStore, DailyBar, FactorStore"
                "\n\nimport psycopg  # MUT"),
        "expect": ["test_import_pgstore_does_not_import_the_driver"],
        "env_limit": (
            "**只有在这个环境没装驱动时**才走这条退路：顶层拉驱动会是收集期 ImportError，"
            "在断言之前就炸。2026-09-25 起仓库 `.venv` 已装了 psycopg（落库侧要用）"
            "⇒ 本机这条走不到这里，而是像别的变异一样真的变红；CI 只装 `.[dev]`、"
            "`[postgres]` extra 是 opt-in ⇒ 那边仍然会退到这里。"
            "留着它只是为了在没装驱动的环境里不被误当成 CAUGHT"
        ),
    },
    {
        "tag": "S10-driver-row-factory-dropped",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            "            self._conn = psycopg.connect(\n"
            "                self.dsn, row_factory=psycopg.rows.dict_row, autocommit=True\n"
            "            )"
        ),
        "new": (
            "            self._conn = psycopg.connect(\n"
            "                self.dsn, autocommit=True\n"
            "            )"
        ),
        "expect": ["test_psycopg_connection_asks_the_driver_for_mapping_rows"],
    },
    {
        # 2026-09-25 追补，样本来自**真跑出来的**缺陷（契约附录 B20）：
        # 默认 `autocommit=False` 时，第一条裸 `execute()` 就显式发 `BEGIN` 并把连接钉在
        # INTRANS（`_connection_base.py::_start_query`）；而事务块会不会提交取决于**进入
        # 那一刻的 libpq 状态**（`transaction.py::_push_savepoint`）⇒ 之后 `transaction()`
        # 退化成 `SAVEPOINT`/`RELEASE`，**永不 COMMIT**，关连接时整批被回滚。
        # 「先读一次、再写一批」因此在真库上静默丢数据，而离线套件全绿（假驱动把
        # 「我们调了 transaction()」记成了「提交了」）。这条变异把 `autocommit` 拿掉。
        "tag": "S13a-autocommit-dropped",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            "            self._conn = psycopg.connect(\n"
            "                self.dsn, row_factory=psycopg.rows.dict_row, autocommit=True\n"
            "            )"
        ),
        "new": (
            "            self._conn = psycopg.connect(\n"
            "                self.dsn, row_factory=psycopg.rows.dict_row\n"
            "            )"
        ),
        "expect": ["test_a_bare_execute_does_not_swallow_the_next_transactions_commit"],
    },
    {
        # 2026-09-25 追补，B20 的附带缺陷：无结果集的语句（不带 `RETURNING` 的
        # INSERT/DELETE、任何 DDL）在 psycopg 里 `description` 是 `None`、而 `fetchall()`
        # 会抛「didn't produce records」⇒ 无条件 `fetchall()` 把它们全变成 `DATA_008`。
        "tag": "S13b-fetchall-unconditional",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            "            if cursor.description is None:\n"
            "                return []\n"
            "            return list(cursor.fetchall())"
        ),
        "new": "            return list(cursor.fetchall())",
        "expect": ["test_execute_tolerates_statements_without_a_result_set"],
    },
    {
        "tag": "CONTROL-comment-only-store",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": "# ── SQL ──",
        "new": "# ── SQL（MUTATION：只改注释，必须抓不到） ──",
        "expect": CONTROL,
    },
    {
        # 2026-09-24 追补，样本来自**真跑出来的**缺陷（契约附录 B18）：
        # `transaction()` 原先写 `with conn:`。psycopg 3 的 `Connection.__exit__` 在提交/回滚
        # 之后还会 `close()`（无 pool 时），于是「先入库、再读回」在真库上报
        # `the connection is closed`，而当时的离线套件全绿 —— 假驱动的 `__exit__`
        # 只记 commit/rollback、不关连接，它证明的只是「实现等于它自己」。
        # 现在假驱动照实测重写（关连接 + 关掉后再 execute 就抛），这条变异用来钉住这件事。
        "tag": "S12a-transaction-uses-the-connection-context-manager",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": (
            "        conn = self._connect()\n"
            "        with conn.transaction():\n"
            "            yield self"
        ),
        "new": (
            "        conn = self._connect()\n"
            "        with conn:\n"
            "            yield self"
        ),
        "expect": [
            "test_psycopg_transaction_is_the_drivers_commit_and_rollback",
            "test_store_and_ingestor_over_the_driver_seam_end_to_end",
        ],
    },
    {
        # 2026-09-24 追补，同一条真库探针带出来的**第二个**缺陷（契约附录 B18.3）：
        # 入库报告 inserted=86 之后重跑同一批，第 2 行就抛 DATA_007：
        #   amount 库内 842270400.0000 / 本批 842270399.9999999
        # 尾巴来自源侧「万元」× 10000 走 float64；库按 numeric(20,4) 存的就是
        # 842270400.0000。不量化到同一标度 ⇒ D10「重跑 == 跑一次」当场破裂。
        # 修法是 `_as_decimal` 末尾 quantize，这条变异把 quantize 拿掉。
        # 2026-09-29 A6 重新锚定：这一行被抽进 `_to_decimal(value, quantum, scale, what)`
        # （因子要按 8 位量化、日线按 4 位，同一个函数吃两个标度）⇒ 旧针「命中 0 次」。
        # 语义没变：还是「拿浮点尾巴去比」。
        "tag": "S12b-quantize-dropped-before-compare-and-bind",
        "tests": TESTS_STORE,
        "path": "quanauto/pgstore.py",
        "old": "        return number.quantize(quantum, rounding=ROUND_HALF_UP)",
        "new": "        return number  # MUT：不量化，拿浮点尾巴去比",
        "expect": [
            "test_upsert_ignores_a_float_tail_below_the_table_scale",
            "test_upsert_binds_values_already_quantized_to_the_table_scale",
        ],
    },
    # ── I2 S3 后半：引擎接 `as_of()` 产出的 feed（↔ tests/test_backtest_db_feed.py） ──
    {
        # 2026-09-29 A6 收口**重新锚定**：`_row_to_bar` 现在写成
        # `_rescale_price(_as_float(row.open), factor)` —— 原来那四行裸 `_as_float` 已经不在了，
        # 旧针「命中 0 次」（工具的自 assert 会把它拦成 `HARNESS-FAIL`，不是 CAUGHT）。
        # 语义一字未改：还是「`Decimal` 从存储层漏进引擎」。现在把 `_as_float` 拿掉，
        # 因子为 1.0 时 `_rescale_price` 原样返回那个 `Decimal`（正是它那条恒等快路径）
        # ⇒ 下游 `float * Decimal` 当场撞。
        # 顺带把本轮新加的那条端到端控制组也算进来：它走的是同一条读路径。
        "tag": "S11-store-decimals-leak-into-the-engine",
        "tests": TESTS_DB_FEED,
        "path": "quanauto/datacenter.py",
        "old": (
            "            open=_rescale_price(_as_float(row.open), factor),\n"
            "            high=_rescale_price(_as_float(row.high), factor),\n"
            "            low=_rescale_price(_as_float(row.low), factor),\n"
            "            close=_rescale_price(_as_float(row.close), factor),\n"
        ),
        "new": (
            "            open=_rescale_price(row.open, factor),\n"
            "            high=_rescale_price(row.high, factor),\n"
            "            low=_rescale_price(row.low, factor),\n"
            "            close=_rescale_price(row.close, factor),\n"
        ),
        "expect": [
            "test_engine_runs_end_to_end_over_a_store_backed_feed",
            "test_adjusted_prices_reach_the_strategy_through_the_engine",
        ],
    },
    {
        "tag": "S12-data-version-ignores-the-declared-version",
        "tests": TESTS_DB_FEED,
        "path": "quanauto/engine.py",
        "old": '            declared = getattr(feed, "data_version", None)\n',
        "new": "            declared = None\n",
        "expect": ["test_report_data_version_is_the_stores_data_version"],
    },
    {
        "tag": "S13-blank-declared-version-stamped-anyway",
        "tests": TESTS_DB_FEED,
        "path": "quanauto/engine.py",
        "old": "                if not text:\n                    raise DataVersionError(\n",
        "new": "                if False:  # MUT\n                    raise DataVersionError(\n",
        "expect": ["test_a_feed_that_declares_a_blank_version_is_refused_not_faked"],
    },
    {
        "tag": "S14-validation-report-computed-before-the-result-exists",
        "tests": TESTS_DB_FEED,
        "path": "quanauto/engine.py",
        "old": (
            "            self._result = result\n"
            "            result.validation_report = self.validate_no_leakage()\n"
            "            return result\n"
        ),
        "new": (
            "            result.validation_report = self.validate_no_leakage()\n"
            "            self._result = result\n"
            "            return result\n"
        ),
        "expect": ["test_engine_runs_end_to_end_over_a_store_backed_feed"],
    },
    {
        # 2026-09-29 A6：本轮修掉的那个缺口本身 = 「`get_adjustment_factor()` 恒 1.0」。
        # 把「只有 `NONE` 才返回 1.0」改成**无条件**返回 1.0，就是把缺口原样放回去。
        # 它必须被 PIT 那三条钉住（两条拒绝类 + 一条真读到 1.25/1.10 的控制样本）——
        # 只写拒绝类的那两条，一个「恒 1.0」的实现同样能通过，控制样本才是这里的主角。
        "tag": "A5-hfq-factor-silently-defaults-to-one",
        "tests": TESTS_PIT,
        "path": "quanauto/datacenter.py",
        "old": "        if self.adjust_type is AdjustType.NONE:\n            return 1.0",
        "new": "        if True:  # MUT：NONE 快路径变成无条件，缺口原样放回\n            return 1.0",
        "expect": [
            "test_hfq_reads_the_real_factor_instead_of_returning_one",
            "test_hfq_without_a_factor_store_is_refused_not_defaulted",
            "test_hfq_with_a_store_that_has_no_row_for_that_day_is_refused",
        ],
    },
    {
        "tag": "A6-qfq-base-factor-ignored",
        "tests": TESTS_PIT,
        "path": "quanauto/datacenter.py",
        "old": (
            "        factor = self._factor_for(symbol, when)\n"
            "        if self.adjust_type is AdjustType.QFQ:\n"
            "            return factor / self._qfq_base_factor(symbol)\n"
            "        return factor"
        ),
        "new": (
            "        factor = self._factor_for(symbol, when)\n"
            "        if False:  # MUT：前复权退化成后复权\n"
            "            return factor / self._qfq_base_factor(symbol)\n"
            "        return factor"
        ),
        "expect": ["test_qfq_over_the_same_view_is_relative_to_the_last_visible_factor"],
    },
    {
        # 「不缩放 volume/amount」是一条**决定**，不是漏改 —— 决定必须被钉住，
        # 否则下次有人"顺手补全"就把它改掉了（乘上去会让小成交量被 int 截成 0）。
        "tag": "A7-amount-scaled-along-with-the-price",
        "tests": TESTS_PIT,
        "path": "quanauto/datacenter.py",
        "old": "            amount=_as_float(row.amount),\n",
        "new": "            amount=_as_float(row.amount) * factor,  # MUT：连成交额一起缩放\n",
        "expect": ["test_volume_and_amount_are_not_scaled"],
    },
    {
        # `_rescale_price` 恒等 = 「复权价」这个功能整个不存在，而四列价格、报告、曲线
        # 全都照常出得来 ⇒ 只能靠数值断言抓（`A5` 管的是**因子**，这条管的是**乘法**）。
        "tag": "A8-rescale-price-becomes-identity",
        "tests": TESTS_PIT,
        # ⚠️ 2026-09-29 晩 ⅲ：函数本体从 `quanauto/datacenter.py` 下移到
        # `quanauto/datafeed.py`（两条实现体共用一份算式）⇒ 这条变异的针**命中 0 次**。
        # `path` 必须跟着走，否则工具报的是 `[MUTATION-HARNESS]`，而报告里看起来像
        # 「这条变异没生效」——又是一次「新产物把有效变异变成 no-op」（铁律⑤），
        # 只不过这次动的是位置而不是字。
        "path": "quanauto/datafeed.py",
        "old": (
            "    if factor == 1.0:\n"
            "        return value\n"
            "    return value * factor"
        ),
        "new": "    return value  # MUT：复权价恒等于不复权价",
        "expect": [
            "test_hfq_bars_are_the_store_price_times_that_days_factor",
            "test_get_bars_returns_adjusted_prices_too",
        ],
    },
    {
        # 「因子接口会拒」不等于「价格路径会拒」：这条把**读路径**那一支改成静默退回 1.0。
        # 它是最容易留下的形态 —— A6 已经证明 `get_adjustment_factor()` 会抛，
        # 于是很容易以为整条链都守住了，而真正被策略消费的是价格。
        "tag": "A9-read-path-silently-defaults-a-missing-factor-to-one",
        "tests": TESTS_PIT,
        "path": "quanauto/datacenter.py",
        "old": (
            "        if not rows:\n"
            "            raise DataNotAvailableError(\n"
            '                "%s 在 %s 没有复权因子行（dc_adjust_factor，data_version=%s）⇒ DATA_001。"\n'
            '                "D6/§2.4：找不到数据必须显式失败 —— 静默返回 1.0 会让这个标的的回测"\n'
            '                "整体变成不复权，而报告里看不出任何异常" % (symbol, when, self.data_version)\n'
            "            )\n"
            "        return _as_float(rows[0].adjust_factor)"
        ),
        "new": (
            "        if not rows:\n"
            "            return 1.0  # MUT：读路径静默退回 1.0\n"
            "        return _as_float(rows[0].adjust_factor)"
        ),
        "expect": ["test_missing_factor_row_fails_the_read_path_instead_of_returning_the_raw_price"],
    },
    {
        # 只乘 `close`：夹具里四列相等时这条**抓不出来**（所以本轮先给 `_row()` 加了
        # `open_/high/low` 参数，让四列可分）。“策略只看 close”并不能让它无罪 ——
        # 引擎的 `previous_close` 退化成 `bar.open`、涨跌幅、以开盘撮合都会带错。
        "tag": "A10-only-close-is-scaled",
        "tests": TESTS_PIT,
        "path": "quanauto/datacenter.py",
        "old": "            open=_rescale_price(_as_float(row.open), factor),\n",
        "new": "            open=_as_float(row.open),  # MUT：只复权 close\n",
        "expect": ["test_all_four_price_columns_are_scaled_not_just_close"],
    },
    {
        # 端到端那一侧的「股数被缩放」：`int()` 截断在于此只是数值不等，但在真实场景里
        # 会把小成交量的一天变成 `volume == 0` ⇒ `is_symbol_available` 判它不可交易。
        # 期望落在端到端文件，是为了让那条新控制组**自己有牙**（不靠 PIT 那边代它挨打）。
        "tag": "A11-volume-scaled-along-with-the-price",
        "tests": TESTS_DB_FEED,
        "path": "quanauto/datacenter.py",
        "old": "            volume=int(row.volume),\n",
        "new": "            volume=int(row.volume / factor),  # MUT：把股数也缩放\n",
        "expect": ["test_adjusted_prices_reach_the_strategy_through_the_engine"],
    },
    {
        # bundle 里那句 `adjust_factor` 与实际乘上去的倍数必须是同一个数。
        # 钉成 1.0 时**价格是对的**（乘法在 `_row_to_bar`），只有策略自己换算会出第二个答案
        # —— 所以只有端到端那条"拿它除一遍"的断言看得见。
        "tag": "A12-bundle-factor-not-from-the-feed",
        "tests": TESTS_DB_FEED,
        "path": "quanauto/engine.py",
        "old": "        adjust_factor=feed.get_adjustment_factor(bar.symbol, bar.datetime),\n",
        "new": "        adjust_factor=1.0,  # MUT：bundle 声明的因子与实际乘上去的不是同一个\n",
        "expect": ["test_adjusted_prices_reach_the_strategy_through_the_engine"],
    },
    # ---- I2 收口 ⅲ：CSV 侧复权口径（第二条实现体） ------------------------
    {
        # CSV 侧**完全不乘因子** —— 就是 2026-09-29 晩 ⅲ 之前那个缺口本身。
        # 与 `A8`（改算法本体）不同：这条改的是**第二条实现体的调用**，算式本体完好，
        # 所以只有盯 CSV 的那些用例看得见。代价是「量纲分裂」：同一个 `DataFeed`
        # 协议下两条实现体交出已复权/未复权两种价格，而两条路径各自都跑得通、都不报错。
        "tag": "A13-csv-feed-does-not-scale-prices",
        "tests": TARGET,
        "path": "quanauto/datafeed.py",
        "old": (
            '                open=_rescale_price(values["open"], factor),\n'
            '                high=_rescale_price(values["high"], factor),\n'
            '                low=_rescale_price(values["low"], factor),\n'
            '                close=_rescale_price(values["close"], factor),\n'
        ),
        "new": (
            '                open=values["open"],  # MUT：CSV 侧完全不乘因子\n'
            '                high=values["high"],\n'
            '                low=values["low"],\n'
            '                close=values["close"],\n'
        ),
        "expect": [
            "test_csv_feed_scales_all_four_price_columns",
            "test_csv_feed_bundle_factor_matches_the_multiplier_it_applied",
        ],
    },
    {
        # CSV 侧只乘 `close`（`A10` 的第二条实现体版本）。四列价格**可分**是本条成立的
        # 前提：`open==high==low==close` 时「只乘一列」与「全乘了」数值上一样，变异会在
        # **正确的实现上**保持沉默 —— 所以 `_adjust_csv_text()` 四列故意取不相等的数。
        "tag": "A14-csv-feed-only-close-is-scaled",
        "tests": TARGET,
        "path": "quanauto/datafeed.py",
        "old": '                open=_rescale_price(values["open"], factor),\n',
        "new": '                open=values["open"],  # MUT：CSV 侧只复权 close\n',
        "expect": ["test_csv_feed_scales_all_four_price_columns"],
    },
    # ---- 复权因子帧的 `validate_frame` 判据（2026-09-29 晩 Ⅳ）----------------
    {
        # 值域判据被放宽：`<= 0` 变成 `<= -1e9` ⇒ `0.0` 静默通过。
        # 故意「放宽」而不是删掉整条判据：删掉会让「schema 认得出来」一起变红，
        # 就分不清是「判据有牙」还是「文件被改坏了」。
        "tag": "A15-factor-positivity-loosened",
        "tests": TESTS_ADAPTER,
        "path": "quanauto/datasources.py",
        "old": ("        for position, value in enumerate(frame['adjust_factor']):\n"
                "            if pd.isna(value) or value <= 0:\n"
                "                return '第 %d 行 adjust_factor=%r' % (position, value)\n"),
        "new": ("        for position, value in enumerate(frame['adjust_factor']):\n"
                "            if pd.isna(value) or value <= -1e9:\n"
                "                return '第 %d 行 adjust_factor=%r' % (position, value)\n"),
        "expect": ["test_validate_rejects_a_non_positive_adjust_factor"],
    },
    {
        # 复权因子从「认得出的四张 schema」里被拿掉 ⇒ 帧重新落进
        # 「列集合不匹配任何标准 schema」。这正是 B21.6 描述过的那个误诊的回潮，
        # 而且是最隐蔽的一种：判据表**还在**，只是没人再走到它。
        "tag": "A16-factor-schema-no-longer-recognised",
        "tests": TESTS_ADAPTER,
        "path": "quanauto/datasources.py",
        "old": ("                           ('index', INDEX_MEMBER_COLUMNS),\n"
                "                           ('factor', ADJUST_FACTOR_COLUMNS)):\n"),
        "new": "                           ('index', INDEX_MEMBER_COLUMNS)):\n",
        "expect": ["test_validate_frame_judges_the_adjust_factor_schema",
                   "test_validate_rejects_a_non_positive_adjust_factor"],
    },
    {
        # 主键重复检查对复权帧失效（`kind != 'factor'` 这个旁路）。
        # 只关掉复权因子这一支：如果连重复检查一起关，日线那条重复用例也会红，
        # 就分不清「复权帧的自然键没被判」与「重复检查整个没了」。
        "tag": "A17-factor-primary-key-duplicates-not-reported",
        "tests": TESTS_ADAPTER,
        "path": "quanauto/datasources.py",
        "old": "    if bool(duplicates.any()):\n",
        "new": "    if bool(duplicates.any()) and kind != 'factor':\n",
        "expect": ["test_validate_rejects_duplicate_adjust_factor_primary_key"],
    },
    {
        # `ADJUST_FACTOR_COLUMNS` 又被排除出 `STANDARD_COLUMNS` ⇒ 契约标准列
        # `adjust_factor` 被报成「源字段名泄漏到输出列」（B21.6 里第一句误诊）。
        # 与 `A16` 是**两句不同误诊**的两个方向：`A16` 打「认不出 schema」，
        # 这条打「认出了，但把契约标准列当成泄漏」。
        "tag": "A18-factor-column-listed-as-leaked",
        "tests": TESTS_ADAPTER,
        "path": "quanauto/datasources.py",
        "old": ("STANDARD_COLUMNS = (DAILY_BAR_COLUMNS + FINANCIAL_COLUMNS + INDEX_MEMBER_COLUMNS\n"
                "                    + ADJUST_FACTOR_COLUMNS)\n"),
        "new": "STANDARD_COLUMNS = (DAILY_BAR_COLUMNS + FINANCIAL_COLUMNS + INDEX_MEMBER_COLUMNS)\n",
        "expect": ["test_validate_frame_judges_the_adjust_factor_schema"],
    },
    {
        # 对照组：只改注释，适配器套件必须**不**红 —— 证明上面四条抓到的是行为，
        # 不是「改了 `datasources.py` 就报错」。
        "tag": "CONTROL-comment-only-datasources",
        "tests": TESTS_ADAPTER,
        "path": "quanauto/datasources.py",
        "old": "#: 复权因子的**帧级自然键**。注意它**不等于** DDL 主键：",
        "new": "#: 复权因子的**帧级自然键**。注意它**不等于** DDL 主键（MUT：只改注释）：",
        "expect": CONTROL,
    },
    {
        "tag": "CONTROL-comment-only-db-feed",
        "tests": TESTS_DB_FEED,
        "path": "quanauto/datacenter.py",
        "old": "    # ── 两道防线 ",
        "new": "    # ── 两道防线（MUTATION：只改注释，必须抓不到） ",
        "expect": CONTROL,
    },
    # ---- I3 风控闸门 ---------------------------------------------------
    {
        # 把 `(-cash_sign)` 改回 `cash_sign`，即复原那个真实发生过的符号 bug：
        # 多头持仓在净持仓表里变负数 ⇒ 浮动市值项永远加不上 ⇒ 满仓时权益为负。
        "tag": "M15-risk-equity-sign-regression",
        "tests": TESTS_RISK_GATE,
        "path": "quanauto/engine.py",
        "old": "            net[trade.symbol] = net.get(trade.symbol, 0) + (-cash_sign) * int(trade.quantity)\n",
        "new": "            net[trade.symbol] = net.get(trade.symbol, 0) + cash_sign * int(trade.quantity)\n",
        # 两条都是实测出来的：符号一错，满仓即假回撤 ⇒ 熔断 ⇒ 第三笔单被拦，
        # 「放宽阈值 ≡ 不接闸门」这条等价关系当场破裂。
        "expect": ["test_snapshot_equity_counts_floating_value_of_long_position",
                   "test_widened_thresholds_reproduce_the_ungated_run"],
    },
    {
        # 闸门空转：`_risk_gate` 无条件放行。这是最危险的退步 ——
        # 订单照常成交，报告里看不出任何异常，只有风控统计会变成 0。
        "tag": "M16-risk-gate-open-loop",
        "tests": TESTS_RISK_GATE,
        "path": "quanauto/engine.py",
        "old": "        if self._risk is None:\n            return True\n        account = self._broker.get_account()\n",
        "new": "        if self._risk is None:\n            return True\n        return True\n        account = self._broker.get_account()\n",
        "expect": ["test_kill_switch_before_run_keeps_every_order_out_of_market",
                   "test_reduce_shrinks_open_order_to_position_cap"],
    },
    {
        # 拦截不再进 `_rejected`：订单既没进市场也没留在结果里，直接蒸发。
        # 报告看起来「什么都没发生」，而口径上必须每一单都有下落。
        "tag": "M17-risk-block-not-recorded",
        "tests": TESTS_RISK_GATE,
        "path": "quanauto/engine.py",
        "old": "        order.status = OrderStatus.REJECTED\n        order.error_message = response.message\n        self._rejected.append(order)\n",
        "new": "        order.status = OrderStatus.REJECTED\n        order.error_message = response.message\n",
        "expect": ["test_kill_switch_before_run_keeps_every_order_out_of_market"],
    },

    # ── I3b：规则存储 + 拦截留痕（quanauto/risk.py、quanauto/engine.py
    #         ↔ tests/test_risk_store.py、tests/test_backtest_risk_gate.py） ──
    # S14 这个名字已经被占（`S14-validation-report-computed-before-the-result-exists`），
    # 所以本系列从 `S15` 起。
    {
        # 「库以 CHECK 拒绝 ⇒ RISK_004（旧值继续生效）」这条判据拿掉。
        # 这一条是本步从**死代码**里救活的（第一版写在 `save_change` 的外层
        # `except Exception` 里，而它在那条调用链上永远走不到）：拿掉之后两个调用点
        # 都会静默退回 RISK_005「不知道状态，需要人看」—— 调用方据此会去重试一个
        # 永远不可能成功的写入，而库里日志只有一根「约束错」的痕迹。
        "tag": "S15-check-violation-judgement-dead",
        "tests": TESTS_RISK_STORE,
        "path": "quanauto/risk.py",
        "old": "    if _is_check_violation(exc):\n        return RiskConfigInvalidError(\n",
        "new": "    if False:  # MUT: 判据拿掉，库拒绍也按 RISK_005 处理\n        return RiskConfigInvalidError(\n",
        "expect": ["test_save_change_check_violation_becomes_risk_004",
                   "test_save_change_check_violation_at_the_commit_boundary_is_also_risk_004"],
    },
    {
        # ★ 这条钉的是**只在真库上才看得见**的那个缺陷（探针 A 段实测、postgres:17）：
        # 把放行判据退回「本项目异常一律放行」（即 `QuanAutoError`），
        # 但真库上驱动异常早被 `pgstore` 包成了 `DataStoreError`（DATA_008）——
        # 于是风控的写入失败以**别的族**的码逃走，`except RiskError` 接不住，
        # 「约束拒绍 ⇒ RISK_004 / 其余 ⇒ RISK_005」这个二分在真库上**整体失效**。
        # 假连接上永远看不见（裸驱动异常第一层就探到）⇒ 三份端口的形状必须都测。
        "tag": "S15-passthrough-rule-goes-back-to-quanautoerror",
        "tests": TESTS_RISK_STORE,
        "path": "quanauto/risk.py",
        "old": "    if isinstance(exc, RiskError):\n        return None\n",
        "new": "    if isinstance(exc, QuanAutoError):  # MUT: 本项目异常一律放行\n        return None\n",
        "expect": ["test_save_change_check_violation_wrapped_by_pgstore_is_still_risk_004",
                   "test_save_change_check_violation_wrapped_by_pgstore_at_the_commit_boundary_too",
                   "test_driver_facing_errors_from_pgstore_are_collected_into_the_risk_family"],
    },
    {
        # `_sqlstate()` 不追 `__cause__` 链（只探本层属性）。
        # 这一条就是缺陷的**原点**：包进 `DataStoreError` 的 `23514` 探不到 ⇒
        # 约束拒绍被当成「连不上」（RISK_005）。留着它，将来把 `pgstore` 的包装层
        # 换成另一种形状时会立刻变红。
        "tag": "S15-sqlstate-stops-at-the-outer-exception",
        "tests": TESTS_RISK_STORE,
        "path": "quanauto/risk.py",
        "old": "        nxt = node.__cause__\n",
        "new": "        nxt = None  # MUT: 不追链，只看本层\n",
        "expect": ["test_sqlstate_walks_the_cause_chain_and_stops_on_a_cycle",
                   "test_save_change_check_violation_wrapped_by_pgstore_is_still_risk_004",
                   "test_save_change_check_violation_wrapped_by_pgstore_at_the_commit_boundary_too"],
    },
    {
        # 反向：连风控族也重翻一遍（去掉 `isinstance(exc, RiskError)` 这道闸）。
        # 代码里看起来仍然「全收到本层错误族了」，代价是两处：
        # ① `test_our_own_errors_are_not_double_wrapped` —— 本层自己抛的异常
        #    被包成另一种码（RISK_004 → RISK_005），调用方看到的错误码变了；
        # ② 子层已翻好的 RISK_004 在父层再翻一次，消息叠成双层。
        "tag": "S15-own-errors-get-re-wrapped",
        "tests": TESTS_RISK_STORE,
        "path": "quanauto/risk.py",
        "old": "    if isinstance(exc, RiskError):\n        return None\n",
        "new": "    if False:  # MUT: 风控族也重翻一遍\n        return None\n",
        "expect": ["test_our_own_errors_are_not_double_wrapped",
                   "test_save_change_check_violation_becomes_risk_004"],
    },
    {
        # 留痕的 `action` 改取**每条 violation 自己**的裁决。
        # 单测（假连接）与端到端（真闸门）各有一条主人，因为这两个口径不同：
        # 单测里 violation 的 action 是人造的；端到端里它是真引擎算出来的 ——
        # `max_sector_pct` 在快照缺行业归类时发的正是 `action=PASS` 的 WARNING。
        # 后果不是「少记一笔」，是真库用 `ck_risk_intercept_action`（只许
        # REDUCE/REJECT/HALT）**拒掉整笔留痕**，而且要到下一笔真拦截才报出来。
        "tag": "S15-intercept-log-uses-the-violation-action",
        "tests": [TESTS_RISK_STORE, TESTS_RISK_GATE],
        "path": "quanauto/risk.py",
        "old": "                str(getattr(violation.severity, \"value\", violation.severity)),\n                action.value,\n",
        "new": "                str(getattr(violation.severity, \"value\", violation.severity)),\n                RiskActionEnum(violation.action).value,\n",
        "expect": ["test_writer_expands_one_row_per_violation_with_the_order_level_action",
                   "test_a_blocked_order_leaves_exactly_one_log_statement_per_order"],
    },
    {
        # 展开时只写第一条 violation。一行少一条看起来像「留痕写成功了」，
        # 而「哪条规则拦的」在这一行之外永久失传（`blocked_orders` 是内存里的，
        # 进程一退就没了）—— 这正是留痕存在的理由。
        "tag": "S15-intercept-log-drops-the-other-violations",
        "tests": [TESTS_RISK_STORE, TESTS_RISK_GATE],
        "path": "quanauto/risk.py",
        "old": "        for violation in response.violations:\n            rows.append((\n",
        "new": "        for violation in response.violations[:1]:\n            rows.append((\n",
        "expect": ["test_writer_expands_one_row_per_violation_with_the_order_level_action",
                   "test_a_blocked_order_leaves_exactly_one_log_statement_per_order"],
    },
    {
        # 留痕挪进闸门**每单都写**（而不是只在拦下时写）。这是 D1 那条张力的
        # 常见走法：为了让「留痕由引擎负责」看着更顺，把 `record()` 上移到
        # `check()` 之后。后果有两层：① 放行的单也进留痕表（表名与 CHECK 都不允许）；
        # ② 实测会在 PASS 响应上直接抛 `RiskInterceptError`，整个回测当场炸。
        # 主人是那条**对照组**（接了 writer 但一单没拦 ⇒ 一条 SQL 都不许有）——
        # 只断言「没接 writer ⇒ 没 SQL」是空转，假连接根本没挂上去。
        "tag": "S15-gate-logs-every-check-not-just-blocks",
        "tests": TESTS_RISK_GATE,
        "path": "quanauto/engine.py",
        "old": "        response = self._risk.check(request)\n        self._risk_stats[\"checked\"] += 1\n",
        "new": "        response = self._risk.check(request)\n        if self._risk_intercept_log is not None:\n            self._risk_intercept_log.record(request, response, created_at=bar.datetime)\n        self._risk_stats[\"checked\"] += 1\n",
        "expect": ["test_writer_attached_but_nothing_blocked_writes_nothing"],
    },
    {
        # `created_at` 不传 ⇒ `rows_for` 退回 `_now()` 墙钟。
        # 单测那条（`created_at=stamp` 显式传参）**不会**红 —— 它自己给了时间戳，
        # 所以这条的主人只能是端到端那条：它拿 `risk_summary()` 里的 `datetime`
        # （来自 K 线）去对留痕行的时间戳。用墙钟 ⇒ 同一条命令两轮跑出两份对不上
        # 的留痕，`R5` 逐字节可复现当场失效。
        "tag": "S15-intercept-log-created-at-becomes-wall-clock",
        "tests": TESTS_RISK_GATE,
        "path": "quanauto/engine.py",
        "old": "            self._risk_intercept_log.record(request, response, created_at=bar.datetime)\n",
        "new": "            self._risk_intercept_log.record(request, response)\n",
        "expect": ["test_a_blocked_order_leaves_exactly_one_log_statement_per_order"],
    },
    {
        # 审计里 `change_direction` 永远留空。审计表不会报错、行数不变、
        # `old_threshold` / `new_threshold` 都还在 ——「这到底是收紧还是放宽」
        # 却只能靠人拿两个数去比。方向按**注册表**判（不是按大小判），所以
        # 一旦加一条「次数上限」这类反向规则，事后推算还会推反。
        "tag": "S15-audit-direction-always-blank",
        "tests": TESTS_RISK_STORE,
        "path": "quanauto/risk.py",
        "old": "                     \"\" if old is None else self._direction(change, old),\n",
        "new": "                     \"\",\n",
        "expect": ["test_save_change_writes_the_audit_direction_tighten",
                   "test_save_change_writes_the_audit_direction_relax",
                   "test_save_change_direction_follows_the_registry_not_the_size"],
    },
    {
        # 熔断单元 UPSERT 顺手清掉 `resume_reason`。`BreakerState` 里没有这个字段
        # （契约 §3.2.6），所以「不覆盖它」只能靠**不去写它**——
        # 一行 SQL 里多一个 `resume_reason = ''` 就把「上次为什么恢复」抹了，
        # 而下次触发后没人能回答「是谁、凭什么把它恢复的」。
        "tag": "S15-breaker-upsert-clobbers-resume-reason",
        "tests": TESTS_RISK_STORE,
        "path": "quanauto/risk.py",
        "old": "        \"resumed_at = EXCLUDED.resumed_at, resumed_by = EXCLUDED.resumed_by\"\n",
        "new": "        \"resumed_at = EXCLUDED.resumed_at, resumed_by = EXCLUDED.resumed_by, \"\n        \"resume_reason = ''\"\n",
        "expect": ["test_breaker_upsert_never_touches_the_resume_reason_column"],
    },
    {
        # 对照组：只在注释上动手，必须**不**被抓到 —— 用来证明上面几条的红
        # 是变异打在了代码上，而不是「改了文件就红」。
        "tag": "CONTROL-comment-only-risk-store",
        "tests": TESTS_RISK_STORE,
        "path": "quanauto/risk.py",
        "old": "_CHECK_VIOLATION_SQLSTATE = \"23514\"\n",
        "new": "_CHECK_VIOLATION_SQLSTATE = \"23514\"  # MUT: 只改注释\n",
        "expect": CONTROL,
    },
    # ── I4：看板（quanauto/dashboard.py + quanauto/cli.py ↔ tests/test_dashboard.py） ──
    {
        # 回撤是**正数百分比**（`drawdown_series` 的约定），最大回撤取 max。
        # 写成 min 会恒等于 0.000000 —— 不报错，只是安静地告诉读者「这策略从没
        # 回过撤」。这一条与 `tools/verify_dashboard.py` 的 C9 守的是同一件事，
        # 但两边守的对象不同：那边守**门禁脚本**有没有写反，这边守**看板**有没有。
        "tag": "D1-max-drawdown-uses-min",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": "        max_drawdown=max(point.drawdown for point in view.curve),\n",
        "new": "        max_drawdown=min(point.drawdown for point in view.curve),\n",
        "expect": ["test_max_drawdown_uses_max_not_min",
                   "test_curve_stats_are_taken_from_the_curve",
                   "test_real_report_curve_agrees_with_two_performance_metrics"],
    },
    {
        # 「期末净值」取的不是最后一个点。曲线只剩一个点时也「对」，所以这条
        # 需要长度 > 1 的样本：合成报告 5 点、真报告 60 点。
        "tag": "D2-curve-last-point-is-the-first",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": "        last=equities[-1],\n",
        "new": "        last=equities[0],\n",
        "expect": ["test_curve_stats_are_taken_from_the_curve",
                   "test_real_report_curve_agrees_with_two_performance_metrics"],
    },
    {
        # 计数类指标出现小数时静默截断：报告写 3.7、看板显示 3，两边从此对不上。
        "tag": "D3-fractional-count-silently-truncated",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": "        if float(number) != int(number):\n",
        "new": "        if False:\n",
        "expect": ["test_fractional_count_raises"],
    },
    {
        # 缺指标时不再报错而是直接取 —— `performance[spec.key]` 会抛 KeyError
        # （不是 DashboardError）。这正是要抓的：异常类型变了，调用方
        # 那个 `except DashboardError` 就接不住了。
        "tag": "D4-missing-metric-key-not-named",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": ("    missing = [spec.key for spec in METRIC_SPECS if spec.key not in performance]\n"
                "    if missing:\n"),
        "new": ("    missing = [spec.key for spec in METRIC_SPECS if spec.key not in performance]\n"
                "    if False:\n"),
        "expect": ["test_missing_metric_key_is_named"],
    },
    {
        # NaN/Inf 当正常值显示。上游算出非数是个必须停下来的事实，
        # 而 `"%.4f" % nan` 会安静地打成 `nan`，看上去像某种指标。
        "tag": "D5-nan-shown-as-a-number",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": "    if not math.isfinite(number):\n",
        "new": "    if False:\n",
        "expect": ["test_nan_metric_raises"],
    },
    {
        # 曲线长度下界没了：0 个点也能读出「一张空曲线」，与「真的没有数据」
        # 长得一模一样（防空转守卫）。
        "tag": "D6-empty-curve-accepted",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": "    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 2:\n",
        "new": "    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 0:\n",
        "expect": ["test_missing_curve_raises"],
    },
    {
        # 结构计数缺段时退回 0：「报告里没有 trades 段」被显示成「0 笔成交」。
        "tag": "D7-missing-segment-reads-as-zero",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": "        counts[key] = len(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else None\n",
        "new": "        counts[key] = len(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else 0\n",
        "expect": ["test_missing_structure_segment_reads_as_dash_not_zero"],
    },
    {
        # CLI 分派被拆掉：库函数全绿、`quanauto dashboard` 却只会打印帮助并退 2。
        # 本仓库踩过三次「两边各自都绿、缝上没跑过」，这条是缝上的那颗钉子。
        "tag": "D8-cli-dashboard-dispatch-gone",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/cli.py",
        "old": "    if args.command == \"dashboard\":\n",
        "new": "    if False:\n",
        "expect": ["test_cli_main_dispatches_the_dashboard_command_to_stdout"],
    },
    {
        # `--format` 两支接反：要 html 给文本、要文本给 html。
        "tag": "D9-cli-format-branches-swapped",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/cli.py",
        "old": ("        text = render_html(read_view(payload)) if args.format == \"html\" else render_text(read_view(payload))\n"),
        "new": ("        text = render_text(read_view(payload)) if args.format == \"html\" else render_html(read_view(payload))\n"),
        "expect": ["test_cli_dashboard_html_format_writes_the_html_render",
                   "test_cli_dashboard_writes_the_library_render_to_the_out_file",
                   "test_cli_main_dispatches_the_dashboard_command_to_stdout"],
    },
    {
        # 读不出来的报告改为「退 0、什么都不打印」：fail-closed 变成 fail-silent。
        # 调用方（脚本/CI）看到的是成功。
        "tag": "D10-cli-swallows-unreadable-report",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/cli.py",
        "old": ("    except DashboardError as exc:\n"
                "        sys.stderr.write(\"ERROR: %s\\n\" % exc)\n"
                "        return 1\n"),
        "new": "    except DashboardError:\n        return 0\n",
        "expect": ["test_cli_dashboard_refuses_a_report_it_cannot_read"],
    },
    {
        # 对照组：只改 docstring 的一句话，必须**不**被抓到。
        "tag": "CONTROL-comment-only-dashboard",
        "tests": TESTS_DASHBOARD,
        "path": "quanauto/dashboard.py",
        "old": "    \"\"\"等距抽样 + 分档字符。纯 ASCII，确定性（同输入同输出）。\"\"\"\n",
        "new": "    \"\"\"等距抽样 + 分档字符。纯 ASCII，确定性（同输入同输出）。（MUT：只改注释）\"\"\"\n",
        "expect": CONTROL,
    },
]

FAILED_RE = re.compile(r"^(FAILED|ERROR) (\S+)::(\w+)")
SUMMARY_RE = re.compile(r"(\d+) (failed|passed|error|errors)")


def write_report() -> None:
    """UTF-8 落盘：控制台是 GBK，中文经管道会变成乱码，证据得有一份看得懂的。"""
    with open(REPORT, "w", encoding="utf-8", newline="\n") as fp:
        fp.write("\n".join(LINES) + "\n")


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fp:
        return fp.read()


def write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fp:
        fp.write(data)


def newline_of(data: bytes) -> str:
    """文件用的是 CRLF 还是 LF —— 不先搞清楚这个，裸 `\n` 的针会**静默失配**。"""
    return "\r\n" if b"\r\n" in data else "\n"


def apply_mutation(original: bytes, old: str, new: str) -> tuple[bytes, int]:
    """在**规范化成 LF** 的文本上匹配，写回时恢复文件原本的换行符。

    返回 (新字节, 命中次数)。命中次数不是 1 就必须当工具自身故障报出来：
    当时写这工具时忘了这一层，8 处变异全部报「原文出现 0 次」——源文件是 CRLF
    （broker.py 414 个、engine.py 489 个），而针里写的是裸 `\n`。
    """
    text = original.decode("utf-8").replace("\r\n", "\n")
    hits = text.count(old)
    mutated = text.replace(old, new, 1)
    return mutated.replace("\n", newline_of(original)).encode("utf-8"), hits


def run_pytest(test_file) -> tuple[int, set, dict, str]:
    paths = [test_file] if isinstance(test_file, str) else list(test_file)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PYTHON, "-X", "utf8", "-m", "pytest", *paths, "-q", "--tb=no", "-rfE", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    names = set()
    for line in proc.stdout.splitlines():
        match = FAILED_RE.match(line.strip())
        if match:
            names.add(match.group(3))
    counts = {kind: int(number) for number, kind in SUMMARY_RE.findall(proc.stdout)}
    return proc.returncode, names, counts, proc.stdout


def main() -> int:
    if not os.path.isfile(PYTHON):
        say("FINDING [ENV] 找不到仓库 venv 的 python：%s" % PYTHON)
        say("verdict: FAIL")
        return 2

    # 基线一起跑：基线只要有一处不是全绿，后面的「红」就什么都证明不了。
    # 一条变异期望落在哪个套件，那个套件就必须在基线里 —— 否则「红」没有全绿垫底。
    suites = [TARGET, TESTS_ADAPTER, TESTS_STORE, TESTS_DB_FEED, TESTS_PIT,
              TESTS_RISK_GATE, TESTS_RISK_STORE, TESTS_DASHBOARD]
    say("baseline: 先跑一次干净的全绿（%s）" % " + ".join(suites))
    code, names, counts, output = run_pytest(suites)
    if code != 0 or names:
        say("FINDING [BASELINE] 基线不是全绿（exit=%d, failed=%d）—— 后面的红说明不了任何事"
            % (code, len(names)))
        for name in sorted(names):
            say("  baseline-failure: %s" % name)
        say("verdict: FAIL")
        write_report()
        return 1
    say("baseline: 全绿 OK (%s)" % ", ".join("%s=%d" % item for item in sorted(counts.items())))

    failed_mutations = []
    parse_problems = []
    env_limited = []
    total_mutations = 0
    caught_count = 0
    control_count = 0
    for item in MUTATIONS:
        path = os.path.join(ROOT, item["path"].replace("/", os.sep))
        test_file = item.get("tests", TARGET)
        original = read_bytes(path)
        mutated, hits = apply_mutation(original, item["old"], item["new"])
        if hits != 1:
            # 变异没打上去 ⇒ 后面的「没抓到」是假结果，必须当成工具自身故障报出来。
            parse_problems.append("%s: 变异原文在 %s 里出现 %d 次（要求恰好 1 次），变异未生效"
                                  % (item["tag"], item["path"], hits))
            continue
        say("APPLIED %s @ %s (newline=%s, tests=%s)"
            % (item["tag"], item["path"], repr(newline_of(original)), test_file))
        write_bytes(path, mutated)
        try:
            code, names, counts, output = run_pytest(test_file)
            collected_errors = counts.get("error", 0) + counts.get("errors", 0)
            collection_broken = "errors during collection" in output or counts.get("passed", 0) == 0
        finally:
            write_bytes(path, original)
            assert read_bytes(path) == original, "恢复失败：%s" % item["path"]

        if item["expect"] == CONTROL:
            control_count += 1
            if names:
                say("  CONTROL 失败：只改注释的变异居然让测试变红 —— 说明有测试在乱红：%s"
                    % ", ".join(sorted(names)))
                failed_mutations.append(item["tag"])
            else:
                say("  control OK: 没抓到（本检查器确实会报 caught=NO）")
            continue

        if collection_broken and item.get("env_limit"):
            # 本机验不了的变异：只证明「这里验不了」，绝不算 CAUGHT，也不算 PASS。
            env_limited.append(item["tag"])
            say("  ENV-LIMIT %s：%s" % (item["tag"], item["env_limit"]))
            say("    （本机结果：exit=%d passed=%d errors=%d，断言根本没跑到）"
                % (code, counts.get("passed", 0), collected_errors))
            continue

        total_mutations += 1
        missing = [name for name in item["expect"] if name not in names]
        if collection_broken:
            parse_problems.append("%s: 变异导致收集阶段就炸或一个测试都没跑成（ImportError/语法错不算有效触发，passed=%d, errors=%d）"
                                  % (item["tag"], counts.get("passed", 0), collected_errors))
        if missing:
            say("  CAUGHT=no  exit=%d failed=%d  没变红的期望测试: %s"
                % (code, len(names), ", ".join(missing)))
            say("  实际变红: %s" % (", ".join(sorted(names)) or "(无)"))
            failed_mutations.append(item["tag"])
        else:
            caught_count += 1
            say("  CAUGHT=yes exit=%d failed=%d (%s)" % (code, len(names), ", ".join(sorted(names))))

    problems = failed_mutations + parse_problems
    for text in parse_problems:
        say("FINDING [MUTATION-HARNESS] %s" % text)
    for tag in env_limited:
        say("NOTE [ENV-LIMIT] %s 只在本机验不了，不等于通过；换到装了 psycopg 的环境要重跑" % tag)
    say("mutations=%d caught=%d control=%d env_limited=%d"
        % (total_mutations, caught_count, control_count, len(env_limited)))
    say("report=%s" % REPORT)
    if problems:
        say("verdict: FAIL (%d issue(s))" % len(problems))
        write_report()
        return 1
    say("verdict: PASS")
    write_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
