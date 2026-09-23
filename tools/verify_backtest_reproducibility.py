#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""I1 门禁（A 级）：同一条命令跑两次，`deterministic` 段必须逐字节相同。

用法：`python tools/verify_backtest_reproducibility.py [repo_root] [--record]`

## 为什么需要这个门禁

「同一条命令跑两次结果一致」这句话，如果没有机器来念，就只是**当时凑巧成立**的一个
观察：换台机器、换次改动、多抽一个随机数，都可能在没人注意的情况下把它变成假的。
手工比对报告里的十几个指标也算不上验证 —— 人的眼睛不会去比第 37 个成交的滑点。

## 这个门禁**必须**防住三种假绿

1. **种子回显**：报告里带一个 `seed` 字段，两次跑只要种子不同就「报告不同」。
   那测的是「字段有没有被复制」，不是「随机源有没有接进结果」。所以 `seed` 只允许
   待在 `inputs` 段，`deterministic` 段里出现 `seed` 直接判 FAIL（R4）。
2. **两份空报告**：0 成交、净值恒为常数，两份空报告比较当然「相同」。
   R3 是硬守卫：成交数 >= 1 且净值序列非常数，否则判 FAIL。
3. **比错了段**：`duration_ms` 天生每次都不同。若把它算进比较范围，门禁永远红；
   若为了让它变绿而把整个报告都排除，门禁就永远绿。所以只比 `deterministic` 段，
   且 R4 会把这段的**键集**钉死 —— 将来谁往这里塞时戳都会被发现。

## 触发测试（自证有效，不跑全绿就算通过）

`selftest()` 用**合成报告**逐一构造上面的坏输入，外加两次**真实** CLI 调用：
同种子两次跑（期望 0 报错）与指定一个不存在的行情文件（期望 CLI 非零退出被拦下）。
每个样本先自断言「变异真的生效了」（例如 R2 样本必须确认两份报告确实不同），
再断言报出的 FINDING 代码集合与预期**完全相等** —— 少了是漏检，多了是误报。

## 为什么默认**不写**仓库（2026-09-23 改）

改之前，这个门禁每次运行都会把 4 份报告写回 `.rounds/i1/`（3 份真跑样本 + 1 份自测
探针）。而报告里 `runtime.duration_ms` 是墙钟耗时、**天生每次都不同**（通常 0，偶尔 1）
⇒ 跑一次检查就可能把已入库的证据文件改掉一个字节。

后果不是「多一个改动」，而是**信号失效**：`git status` 从此永远不干净，「我到底改没改
东西」与「检查跑没跑过」两个问题都答不出来；一次无关的提交也会悄悄带上证据文件的
字节变化（`7c5e0d3` 就是这样带上了一次）。

现在：默认**只读**（报告写进临时目录，跑完删掉），并新增 R5 把「库里那份证据」与
「本次真跑的结果」比一比 —— 过期快照判红，而不是被静默刷新。重新录制是一次**显式
动作**：`--record`（`run_all_gates.py` 从不传它，且它只改「证据」，改不动 R1~R4 ——
那四条只比本次跑出来的两份报告）。

## 边界 —— 这个门禁**不**证明什么

* **不等于跨机器可复现**：只在本机、本解释器、本文件系统上跑过。
* **不等于算得对**：净值算错了也照样逐字节相同。这里只管「一样不一样」。
* **不覆盖浮点等价性**：比的是序列化后的字节，不是「数值在误差内相等」。
* **`runtime` 段整段不比**：这是设计不是漏检（该段只该有 `duration_ms` 每次都变；
  `start_time` / `end_time` 其实是回测区间，被保守地一起排除了）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

DETECTORS = (
    ("R1-SAME-SEED-IDENTICAL", "同种子两次跑，deterministic 段逐字节相同"),
    ("R2-SEED-CHANGES-RESULT", "换种子必须真的改变 deterministic 段"),
    ("R3-NON-VACUOUS", "样本非空：有成交、净值非常数（防空转假绿）"),
    ("R4-DETERMINISTIC-KEYS", "段结构/键集/种子位置符合约定"),
    ("R5-EVIDENCE-CURRENT", "入库的证据样本与本次真跑的一致（过期快照判红，不静默刷新）"),
)

SCHEMA = "quanauto.backtest-report/1"
DETERMINISTIC_KEYS = (
    "account_history",
    "data_version",
    "equity_curve",
    "orders",
    "params_used",
    "performance",
    "status",
    "strategy_id",
    "strategy_version",
    "trades",
    "validation_report",
)
FIXTURE = "tests/fixtures/sample_prices.csv"
MISSING_FIXTURE = "tests/fixtures/this-file-does-not-exist.csv"
EVIDENCE_DIR = os.path.join(".rounds", "i1")
SEED_A = 7
SEED_B = 8
# 「用哪个种子、写哪个文件名」只在这里写一遍：R1/R2 用本次跑出来的两份互比，
# R5 用它们与**入库的那三份**比。文件名写错会让 R5 去读一个不存在的路径 ⇒ 判红，
# 所以它既是配置也是判据的一部分。
EVIDENCE_PLAN = (
    ("a1", SEED_A, "report-seed7-a.json"),
    ("a2", SEED_A, "report-seed7-b.json"),
    ("b", SEED_B, "report-seed8.json"),
)


# ── 工具 ─────────────────────────────────────────────────────────────
def canonical(obj: object) -> bytes:
    """把一段 JSON 物化成**唯一**的字节形式，用来做逐字节比较。

    `sort_keys=True` + 无空格分隔符 ⇒ 字典的插入顺序、空白风格都不影响结论。
    直接比文件原文是不行的：`dump_report` 里 `indent=2` 一旦调整，门禁就会红，
    而那跟可复现性毫无关系。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def check_shape(report: object, label: str, expect_seed: object = None) -> list:
    """R3 + R4：任何一份报告不合格，后面两个「比较」都没有意义，所以先单独查。"""
    findings: list = []
    if not isinstance(report, dict):
        return [("R4-DETERMINISTIC-KEYS", "%s: 报告不是 JSON 对象" % label,)]
    if report.get("schema") != SCHEMA:
        findings.append(
            ("R4-DETERMINISTIC-KEYS", "%s: schema=%r，期望 %r" % (label, report.get("schema"), SCHEMA))
        )
    inputs = report.get("inputs")
    seed = inputs.get("seed") if isinstance(inputs, dict) else None
    if not isinstance(seed, int) or isinstance(seed, bool):
        findings.append(("R4-DETERMINISTIC-KEYS", "%s: inputs.seed 不是整数（%r）" % (label, seed)))
    elif expect_seed is not None and seed != expect_seed:
        findings.append(
            ("R4-DETERMINISTIC-KEYS", "%s: inputs.seed=%r，但这一份是拿 --seed %r 跑出来的" % (label, seed, expect_seed))
        )
    det = report.get("deterministic")
    if not isinstance(det, dict):
        findings.append(("R4-DETERMINISTIC-KEYS", "%s: 缺 deterministic 段（或不是对象）" % label))
        return findings
    missing = [k for k in DETERMINISTIC_KEYS if k not in det]
    if missing:
        findings.append(("R4-DETERMINISTIC-KEYS", "%s: deterministic 缺键 %s" % (label, ", ".join(missing))))
    extra = sorted(k for k in det if k not in DETERMINISTIC_KEYS)
    if extra:
        findings.append(
            (
                "R4-DETERMINISTIC-KEYS",
                "%s: deterministic 多出未登记键 %s —— 若其中含时戳或路径，"
                "同种子两次跑就不可能逐字节相同；若含种子，换种子报告就不同得毫无意义" % (label, ", ".join(extra)),
            )
        )
    if "seed" in det:
        findings.append(
            (
                "R4-DETERMINISTIC-KEYS",
                "%s: deterministic 段里出现 seed —— 「换种子报告不同」会因为字段本身而成立，"
                "测不出引擎到底有没有用到种子" % label,
            )
        )

    trades = det.get("trades")
    if not isinstance(trades, list) or not trades:
        findings.append(
            (
                "R3-NON-VACUOUS",
                "%s: 一笔成交都没有 —— 两份空报告比较当然「相同」，这样的可复现性什么都没证明" % label,
            )
        )
    orders = det.get("orders")
    if not isinstance(orders, list) or not orders:
        findings.append(("R3-NON-VACUOUS", "%s: orders 为空" % label))
    curve = det.get("equity_curve")
    if not isinstance(curve, list) or len(curve) < 2:
        findings.append(("R3-NON-VACUOUS", "%s: equity_curve 少于 2 个点" % label))
    else:
        values = [p.get("equity") if isinstance(p, dict) else None for p in curve]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values):
            findings.append(("R3-NON-VACUOUS", "%s: equity_curve 里的 equity 不是数值" % label))
        elif len({round(float(v), 6) for v in values}) < 2:
            findings.append(
                ("R3-NON-VACUOUS", "%s: 净值序列恒定（%r）—— 绩效分析器在空转" % (label, values[0]))
            )
    perf = det.get("performance")
    if not isinstance(perf, dict):
        findings.append(("R3-NON-VACUOUS", "%s: 缺 performance 段" % label))
    else:
        total = perf.get("total_return")
        if not isinstance(total, (int, float)) or isinstance(total, bool) or abs(float(total)) < 1e-12:
            findings.append(
                ("R3-NON-VACUOUS", "%s: performance.total_return=%r —— 恒为 0 时指标全无意义" % (label, total))
            )
    return findings


def check_identical(a: dict, b: dict, label: str = "seed%d x2" % SEED_A) -> list:
    """R1：同种子的两次跑必须给出同一份 deterministic。"""
    det_a, det_b = a.get("deterministic"), b.get("deterministic")
    if canonical(det_a) == canonical(det_b):
        return []
    findings = [
        (
            "R1-SAME-SEED-IDENTICAL",
            "%s: deterministic 段逐字节不同 —— 同一输入没有给出同一结果，报告不可复现" % label,
        )
    ]
    if isinstance(det_a, dict) and isinstance(det_b, dict):
        differ = [k for k in sorted(set(det_a) | set(det_b)) if canonical(det_a.get(k)) != canonical(det_b.get(k))]
        findings.append(
            (
                "R1-SAME-SEED-IDENTICAL",
                "%s: 不同的键共 %d 个，前几个是 %s" % (label, len(differ), ", ".join(differ[:5])),
            )
        )
    return findings


def check_differs(a: dict, b: dict, label: str = "seed%d vs seed%d" % (SEED_A, SEED_B)) -> list:
    """R2：换种子必须真的改变结果。

    这条是**反向**的：它要求两份报告**不同**。如果它红了，说明随机源没接进结果
    （或者滑点被设成 0，那时「换种子结果不变」是**正确**的，门禁不该红 —— 见
    `real_run` 里对滑点默认值的依赖说明）。
    """
    if canonical(a.get("deterministic")) != canonical(b.get("deterministic")):
        return []
    return [
        (
            "R2-SEED-CHANGES-RESULT",
            "%s: 换了种子 deterministic 段仍逐字节相同 —— 随机源没有接进结果" % label,
        )
    ]


# ── 真实运行 ──────────────────────────────────────────────────────────
def run_cli(root: str, csv_rel: str, seed: int, out_rel: str) -> tuple:
    """跑一次 CLI。返回 `(report_or_None, findings)`。

    CLI 非零退出、报告文件没落地、JSON 读不出来，一律判 FAIL —— **不能**当成
    「跳过这一项」：跳过会让门禁在「什么都没跑」的情况下打印 PASS。
    """
    argv = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "quanauto.cli",
        "backtest",
        "--strategy-csv",
        csv_rel,
        "--out",
        out_rel,
        "--seed",
        str(seed),
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            argv,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, [("R1-SAME-SEED-IDENTICAL", "CLI 起不来：%s (%s)" % (exc, type(exc).__name__))]
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-4:]
        return None, [
            (
                "R1-SAME-SEED-IDENTICAL",
                "CLI 退出码 %d（--seed %d --strategy-csv %s）：%s" % (proc.returncode, seed, csv_rel, " | ".join(tail)),
            )
        ]
    path = os.path.join(root, out_rel)
    if not os.path.exists(path):
        return None, [("R1-SAME-SEED-IDENTICAL", "CLI 退出码 0 但报告文件没生成：%s" % out_rel)]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), []
    except (OSError, ValueError) as exc:
        return None, [("R1-SAME-SEED-IDENTICAL", "报告读不出来：%s (%s)" % (out_rel, exc))]


def _load_report(path: str) -> object:
    """读一份入库的证据报告；读不出来返回 None（由 check_evidence 判红）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def check_evidence(fresh: dict, committed: object, label: str) -> list:
    """R5：入库的证据文件必须与本次真跑的结果一致 —— 只比 `deterministic` 段。

    与 R1 的区别：R1 比「本次跑的两次」，这里比「本次跑的 vs 磁盘上那份」。
    没有这一条，默认只读就会带来新问题：入库的证据可能在无人察觉时变成过期快照，
    而它正是迭代计划 DoD 里按路径引用的那份文件（「快照过期」与「检查通过」会同时成立）。

    `runtime` 段**故意不比** —— 与 R1 同一套理由：那一整段只该有墙钟耗时在变。
    所以这一条永远不会因为「又跑了一次」而红，只会在内容真的变了时红。
    """
    if committed is None:
        return [("R5-EVIDENCE-CURRENT",
                 "%s: 证据文件不存在或读不出来 —— DoD 引用的证据路径成了空地址" % label)]
    if not isinstance(committed, dict):
        return [("R5-EVIDENCE-CURRENT", "%s: 证据文件不是 JSON 对象" % label)]
    if canonical(committed.get("deterministic")) != canonical(fresh.get("deterministic")):
        return [("R5-EVIDENCE-CURRENT",
                 "%s: 入库证据与本次跑出来的 deterministic 段不同 —— 快照已过期"
                 "（引擎改了、或 fixture 改了）。确认新结果无误后用 `--record` 重录并提交" % label)]
    if canonical(committed.get("inputs")) != canonical(fresh.get("inputs")):
        return [("R5-EVIDENCE-CURRENT", "%s: inputs 段与本次跑出来的不同" % label)]
    return []


def check_evidence_dir(root: str, reports: dict) -> list:
    """按 EVIDENCE_PLAN 逐份比对入库证据；一个文件都没有时判红（防空转）。"""
    findings: list = []
    present = 0
    for name, _seed, fname in EVIDENCE_PLAN:
        path = os.path.join(root, EVIDENCE_DIR, fname)
        if os.path.exists(path):
            present += 1
        findings.extend(check_evidence(reports[name], _load_report(path), "%s(%s)" % (name, fname)))
    if not present:
        findings.append(
            ("R5-EVIDENCE-CURRENT",
             "%s 下一个证据文件都没有 —— 证据路径全是空的，这条检查在空转；先 `--record` 录一次" % EVIDENCE_DIR)
        )
    return findings


def real_run(root: str, record: bool = False) -> list:
    if not os.path.exists(os.path.join(root, FIXTURE)):
        # 提取为空必须判 FAIL：没有行情文件就没有可比对的样本，全绿是空转出来的。
        return [("R3-NON-VACUOUS", "行情 fixture 不存在：%s —— 没有样本就没有可复现性可言" % FIXTURE)]
    work = tempfile.mkdtemp(prefix="quanauto-repro-")
    try:
        # 两次同种子跑**故意写到不同文件**：这样「报告里有没有泄漏输出路径」也会被顺带测出来。
        # 2026-09-23 起写进**临时目录**（见模块文档「为什么默认不写仓库」）。
        reports = {}
        findings: list = []
        for name, seed, fname in EVIDENCE_PLAN:
            report, errs = run_cli(root, FIXTURE, seed, os.path.join(work, fname))
            if report is None:
                return errs
            reports[name] = report
            findings.extend(check_shape(report, "%s(seed=%d)" % (name, seed), expect_seed=seed))
        findings.extend(check_identical(reports["a1"], reports["a2"]))
        findings.extend(check_differs(reports["a1"], reports["b"]))
        if record:
            # 显式录制。默认**不写**仓库：跑一次检查就改一次仓库的话，`git status` 的
            # 干净与否就再也回答不了「我改过东西没有」。`run_all_gates.py` 从不传它。
            out_dir = os.path.join(root, EVIDENCE_DIR)
            os.makedirs(out_dir, exist_ok=True)
            names = []
            for _name, _seed, fname in EVIDENCE_PLAN:
                shutil.copyfile(os.path.join(work, fname), os.path.join(out_dir, fname))
                names.append(fname)
            print("[record] 已写入 %s：%s" % (EVIDENCE_DIR, ", ".join(names)))
            print("[record] 这是一次仓库改动，请检查 diff 后提交；"
                  "本次 R5 不参与判定（本次就是录制动作），R1~R4 照常判定")
        else:
            findings.extend(check_evidence_dir(root, reports))
        return findings
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── 触发测试样本 ───────────────────────────────────────────────────────
def synthetic(seed: int, equity: list, trades: object = None, orders: object = None,
              total_return: object = None, extra: dict = None, runtime: dict = None) -> dict:
    """造一份结构合法的报告，用来构造「必须红」的输入。"""
    det = {
        "account_history": [{"total_capital": v} for v in equity],
        "data_version": "000001.SZ:sample_prices.csv:60",
        "equity_curve": [{"equity": v, "timestamp": "2024-01-02T00:00:00"} for v in equity],
        "orders": [{"order_id": "ord-000001", "status": "FILLED"}] if orders is None else orders,
        "params_used": {"ma-cross": {"capital": 90000.0, "long_window": 20, "short_window": 5}},
        "performance": {"final_equity": equity[-1], "total_return": (equity[-1] / equity[0] - 1.0) if total_return is None else total_return},
        "status": "SUCCESS",
        "strategy_id": "ma-cross",
        "strategy_version": "ma-cross@1.0.0",
        "trades": [{"slippage": 12.5, "trade_id": "trd-000001"}] if trades is None else trades,
        "validation_report": {"is_valid": True, "issues": [], "warnings": []},
    }
    if extra:
        det.update(extra)
    return {
        "deterministic": det,
        "inputs": {"seed": seed},
        "runtime": runtime or {"duration_ms": 1, "end_time": "2024-03-25T00:00:00", "start_time": "2024-01-02T00:00:00"},
        "schema": SCHEMA,
    }


def codes(findings: list) -> set:
    return {item[0] for item in findings}


SAMPLES_SEEN: list = []


def expect(tag: str, got: list, want) -> bool:
    SAMPLES_SEEN.append(tag)
    want_set = set(want)
    got_set = codes(got)
    if got_set == want_set:
        print("  OK   %-34s codes=%s" % (tag, sorted(got_set) or "[]"))
        return True
    print("  FAIL %-34s 期望 codes=%s，实际 codes=%s" % (tag, sorted(want_set) or "[]", sorted(got_set) or "[]"))
    for item in got[:4]:
        print("         %s: %s" % (item[0], item[1]))
    return False


def selftest(root: str) -> int:
    print("[selftest] 触发测试：每个样本都要看见它该报的 FINDING")
    ok = True
    a = synthetic(SEED_A, [100000.0, 101000.0, 99500.0])
    # 同种子、只有 runtime 不同 ⇒ 期望 0 报错（证明 runtime 被正确排除，不误报）
    a_rt = synthetic(SEED_A, [100000.0, 101000.0, 99500.0], runtime={"duration_ms": 999})
    assert canonical(a) != canonical(a_rt), "样本构造失效：runtime 变异没生效"
    ok &= expect("CLEAN-runtime-differs-not-compared", check_identical(a, a_rt), [])
    # 换种子、结果真变了 ⇒ 期望 0 报错
    b = synthetic(SEED_B, [100000.0, 101000.0, 98000.0], trades=[{"slippage": 31.0, "trade_id": "trd-000001"}])
    assert canonical(a["deterministic"]) != canonical(b["deterministic"]), "样本构造失效：换种子的变异没生效"
    ok &= expect("CLEAN-different-seeds", check_differs(a, b), [])
    # 同种子但结果不同 ⇒ R1
    a2 = synthetic(SEED_A, [100000.0, 101000.0, 99000.0])
    assert canonical(a["deterministic"]) != canonical(a2["deterministic"]), "样本构造失效"
    ok &= expect("MUT-same-seed-differs", check_identical(a, a2), ["R1-SAME-SEED-IDENTICAL"])
    # 换种子但结果一样 ⇒ R2（随机源没接进结果）
    b_same = synthetic(SEED_B, [100000.0, 101000.0, 99500.0])
    assert canonical(a["deterministic"]) == canonical(b_same["deterministic"]), "样本构造失效"
    ok &= expect("MUT-diff-seed-same-result", check_differs(a, b_same), ["R2-SEED-CHANGES-RESULT"])
    # 0 成交 + 恒定净值 ⇒ R3（这是最难发现的假绿：两份空报告比较也「相同」）
    empty = synthetic(SEED_A, [100000.0, 100000.0], trades=[], orders=[], total_return=0.0)
    ok &= expect("MUT-zero-trades-flat-curve", check_shape(empty, "empty"), ["R3-NON-VACUOUS"])
    # deterministic 里塞 seed ⇒ R4（R2 会被这个字段骗过去，只有 R4 能抓）
    seeded = synthetic(SEED_A, [100000.0, 101000.0, 99500.0], extra={"seed": SEED_A})
    ok &= expect("MUT-seed-inside-deterministic", check_shape(seeded, "seeded"), ["R4-DETERMINISTIC-KEYS"])
    # 少一个键 ⇒ R4
    short = synthetic(SEED_A, [100000.0, 101000.0, 99500.0])
    del short["deterministic"]["validation_report"]
    ok &= expect("MUT-missing-key", check_shape(short, "short"), ["R4-DETERMINISTIC-KEYS"])
    # 多一个键（将来谁往里加时戳/路径）⇒ R4
    extra_key = synthetic(SEED_A, [100000.0, 101000.0, 99500.0], extra={"started_at": "2024-01-02T09:30:00"})
    ok &= expect("MUT-extra-key", check_shape(extra_key, "extra"), ["R4-DETERMINISTIC-KEYS"])
    # 请求的种子与报告里的不一致 ⇒ R4
    ok &= expect("MUT-seed-mismatch", check_shape(a, "mismatch", expect_seed=SEED_B), ["R4-DETERMINISTIC-KEYS"])

    # 真 CLI：行情文件不存在必须非零退出并被拦下（不能静默跳过）
    # 探针一律写**临时目录**：写进 EVIDENCE_DIR 的话，「跑检查」就等于「改仓库」。
    work = tempfile.mkdtemp(prefix="quanauto-repro-probe-")
    try:
        _, errs = run_cli(root, MISSING_FIXTURE, SEED_A, os.path.join(work, "report-should-not-exist.json"))
        ok &= expect("MUT-cli-fails-on-missing-csv", errs, ["R1-SAME-SEED-IDENTICAL"])
        # 真 CLI：正常输入同一份报告结构必须过关（0 报错）
        report, errs = run_cli(root, FIXTURE, SEED_A, os.path.join(work, "report-probe.json"))
        if report is None:
            print("  FAIL CLEAN-real-cli 起不来：%s" % errs)
            ok = False
        else:
            ok &= expect("CLEAN-real-cli-passes-shape", errs + check_shape(report, "real", expect_seed=SEED_A), [])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # R5 的文件系统那一半：用合成报告造沙盒，不需要真跑 CLI。
    eq_a = [100000.0, 101000.0, 99500.0]
    eq_b = [100000.0, 101000.0, 98000.0]
    fresh = {"a1": synthetic(SEED_A, eq_a), "a2": synthetic(SEED_A, eq_a), "b": synthetic(SEED_B, eq_b)}

    def evidence_sandbox(files: dict) -> str:
        """建一个只含 `.rounds/i1/<fname>` 的沙盒根目录，用来考 R5 的文件读取那一半。"""
        made = tempfile.mkdtemp(prefix="quanauto-evidence-")
        os.makedirs(os.path.join(made, EVIDENCE_DIR), exist_ok=True)
        for fname, payload in files.items():
            with open(os.path.join(made, EVIDENCE_DIR, fname), "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        return made

    def committed_from_fresh(runtime: dict = None) -> dict:
        out = {}
        for name, _seed, fname in EVIDENCE_PLAN:
            payload = dict(fresh[name])
            if runtime is not None:
                payload["runtime"] = runtime
            out[fname] = payload
        return out

    # 控制组：库里那份与本次真跑**逐键相同、只有 runtime 不同** ⇒ 必须安静。
    # 这就是「一条测试配一条控制变异」：变异打在这条判据故意不看的地方。
    made = evidence_sandbox(committed_from_fresh(runtime={"duration_ms": 999}))
    try:
        ok &= expect("CLEAN-evidence-current", check_evidence_dir(made, fresh), [])
    finally:
        shutil.rmtree(made, ignore_errors=True)
    # 变异：入库的那份是过期快照（只有 seed8 那份的净值变了）
    stale = committed_from_fresh()
    stale[EVIDENCE_PLAN[2][2]] = synthetic(SEED_B, [100000.0, 101000.0, 99999.0])
    assert canonical(stale[EVIDENCE_PLAN[2][2]]["deterministic"]) != canonical(fresh["b"]["deterministic"]), \
        "样本构造失效：过期快照的变异没生效"
    made = evidence_sandbox(stale)
    try:
        ok &= expect("MUT-evidence-stale", check_evidence_dir(made, fresh), ["R5-EVIDENCE-CURRENT"])
    finally:
        shutil.rmtree(made, ignore_errors=True)
    # 变异：证据文件缺失（DoD 引用的路径成了空地址）
    made = evidence_sandbox(committed_from_fresh())
    os.remove(os.path.join(made, EVIDENCE_DIR, EVIDENCE_PLAN[2][2]))
    assert not os.path.exists(os.path.join(made, EVIDENCE_DIR, EVIDENCE_PLAN[2][2])), "样本构造失效：文件没删掉"
    try:
        ok &= expect("MUT-evidence-missing", check_evidence_dir(made, fresh), ["R5-EVIDENCE-CURRENT"])
    finally:
        shutil.rmtree(made, ignore_errors=True)
    # 空转守卫：目录在、但一个证据文件都没有 ⇒ 拒绝通过
    made = evidence_sandbox({})
    try:
        ok &= expect("GATE-evidence-dir-empty", check_evidence_dir(made, fresh), ["R5-EVIDENCE-CURRENT"])
    finally:
        shutil.rmtree(made, ignore_errors=True)
    return 0 if ok else 2


# ── 主流程 ────────────────────────────────────────────────────────────
def verify(root: str, record: bool = False) -> list:
    return real_run(root, record=record)


def main(argv: list) -> int:
    # 位置参数只留非选项；`--selftest` 也要知道仓库根（触发测试里有一次真实 CLI 调用），
    # 所以不能把 argv[1] 直接当根目录 —— 否则根会变成 '<cwd>/--selftest'。
    positional = [a for a in argv[1:] if not a.startswith("--")]
    default_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root = os.path.abspath(positional[0]) if positional else default_root
    record = "--record" in argv
    if record and "--selftest" in argv:
        # 一个会改仓库、一个必须是只读的触发测试 —— 同时用就没有明确的语义。
        print("usage: --record 与 --selftest 不能同时用")
        return 2
    print("repo: %s" % root)
    if record:
        print("mode: RECORD（会把本次真跑的报告写回 %s —— 这是一次仓库改动）" % EVIDENCE_DIR)
    print("detectors=%d" % len(DETECTORS))
    for code, what in DETECTORS:
        print("  - %s: %s" % (code, what))
    if "--selftest" in argv:
        # 门禁框架要求 `--selftest` 只跑触发测试并打印 SELFTEST OK/FAIL 标记。
        # 与真实仓库那一遍**分开**：这样「自测通过」和「本轮产物没问题」是两个独立
        # 信号，一个 FAIL 不会借用另一个的绿。
        ok = selftest(root) == 0
        print("samples=%d" % len(SAMPLES_SEEN))
        print("SELFTEST %s" % ("OK" if ok else "FAIL"))
        return 0 if ok else 2
    if selftest(root) != 0:
        print("samples=%d" % len(SAMPLES_SEEN))
        print("verdict: FAIL (selftest)")
        return 2
    print("samples=%d" % len(SAMPLES_SEEN))
    print("[real] 在真实仓库上跑（自测通过不代表真实产物没问题）")
    findings = verify(root, record=record)
    for code, message in findings:
        print("FINDING [%s] %s" % (code, message))
    print("issues=%d" % len(findings))
    if findings:
        print("verdict: FAIL (%d issue(s))" % len(findings))
        return 1
    print("verdict: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
