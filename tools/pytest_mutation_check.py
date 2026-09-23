"""变异检查：把 quanauto 的实现逐处改坏，确认 `tests/test_backtest_slice.py` 真的会红。

**为什么需要它**：I1 的实现写在测试之前，「先红后绿」的顺序证据不在 git 历史里。
剩下的唯一可信替代是**变异检查** —— 一条永远绿的测试和没有测试是一样的。
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

**它不进 `run_all_gates.py` 的注册表**：每个变异要跑一次 pytest（本文件 8 处变异），
慢，且它验证的对象是测试而不是产物契约。手动跑，或改完测试后跑一次。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TARGET = "tests/test_backtest_slice.py"
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


def run_pytest() -> tuple[int, set, dict, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PYTHON, "-X", "utf8", "-m", "pytest", TARGET, "-q", "--tb=no", "-rfE", "-p", "no:cacheprovider"],
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

    say("baseline: 先跑一次干净的全绿")
    code, names, counts, output = run_pytest()
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
    total_mutations = 0
    caught_count = 0
    for item in MUTATIONS:
        path = os.path.join(ROOT, item["path"].replace("/", os.sep))
        original = read_bytes(path)
        mutated, hits = apply_mutation(original, item["old"], item["new"])
        if hits != 1:
            # 变异没打上去 ⇒ 后面的「没抓到」是假结果，必须当成工具自身故障报出来。
            parse_problems.append("%s: 变异原文在 %s 里出现 %d 次（要求恰好 1 次），变异未生效"
                                  % (item["tag"], item["path"], hits))
            continue
        say("APPLIED %s @ %s (newline=%s)" % (item["tag"], item["path"], repr(newline_of(original))))
        write_bytes(path, mutated)
        try:
            code, names, counts, output = run_pytest()
            collected_errors = counts.get("error", 0) + counts.get("errors", 0)
            collection_broken = "errors during collection" in output or counts.get("passed", 0) == 0
        finally:
            write_bytes(path, original)
            assert read_bytes(path) == original, "恢复失败：%s" % item["path"]

        if item["expect"] == CONTROL:
            if names:
                say("  CONTROL 失败：只改注释的变异居然让测试变红 —— 说明有测试在乱红：%s"
                    % ", ".join(sorted(names)))
                failed_mutations.append(item["tag"])
            else:
                say("  control OK: 没抓到（本检查器确实会报 caught=NO）")
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
    say("mutations=%d caught=%d control=1" % (total_mutations, caught_count))
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
