#!/usr/bin/env python
"""跨层对拍：Java 侧 REST 下发的 text 与 Python 侧 `dashboard.format_metric` 是否逐位一致。

⚠️ **这不是门禁。** 它没注册进 `tools/run_all_gates.py`，也没进 CI，要人手动跑：

    & .\\.venv\\Scripts\\python.exe -X utf8 platform\\check_text_parity.py

为什么要手动跑它（而不是假装不需要）
------------------------------------
显示规则（量纲换算 + 定点小数）在仓库里**有两份实现**：

* Python：`quanauto/dashboard.py` 的 `format_metric`
* Java：`platform/.../ReportProjection.formatMetric` / `fixed`

而这两份实现**没有任何自动检查对拍过它们**。两边各自跑绿，不等于屏幕上会显示同一个数
（这条教训在仓库里出现过不止一次：跨层边界的类型/属性/调用次序在单层测试里天然不可见）。

本脚本就干一件事：把同一份报告分别喂给两侧，**逐字段**比。

⚠️ 订正（2026-10-01）：「没有任何自动检查对拍过它们」**已过期** —— 现在有门禁
`platform-text-parity`（`tools/verify_platform_text_parity.py`）用
`platform/text-parity-cases.json` 这份夹具（由 Python 现算）对拍两侧的**显示规则**，
Java 那半由 `platform-runtime` 的真 `mvn test` 跑 `FormatParityTest`。
那次对拍的前提是一次差分实测：73929 组输入里 **6887 组（≈9.3%）两侧打印出不同的串**，
而当时两侧**已有的用例全绿**（含本脚本跑的那 3 份真报告 —— 它们碰巧没踩到边界）。
⇒ **本脚本仍然不是门禁、仍然有价值**，但它现在管的是**另一件事**：
一个**活着的服务端**到底下发了什么（含 `/api/reports` 的信封形状）。

为什么不注册成门禁
------------------
它需要**一个真的在跑的后端**（JVM 进程 + 端口）。把这种东西塞进门禁，得到的是
「环境没起 ⇒ 门禁红」—— 判据依赖环境就是判据自己的缺陷。所以它留在这里，
让人在对拍那一刻手动起服务、手动跑。

⚠️ 这条理由对**静态对拍**（`platform-text-parity`）不成立：那个不需要活着的进程，
所以它能当门禁。两者不是「一个更好的版本」，而是**两件事**（显示规则 vs 展示链路）。

用法
----
    python platform/check_text_parity.py [--base http://localhost:8080] [--dir .rounds/i1]

退出码：0 = 逐位一致；1 = 有分歧或没比到东西（后者比前者更该看：**没有比对到任何指标**
必须判失败，否则脚本会因为「什么都没比」而显得很干净）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from quanauto import dashboard  # noqa: E402  （必须先加 sys.path，故放在此处）


def get_json(url: str, timeout: float = 10.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit("HTTP %s: %s\n%s" % (exc.code, url, body))
    except urllib.error.URLError as exc:
        raise SystemExit(
            "连不上 %s（%s）—— 后端起了吗？\n    mvn -B -f platform/pom.xml -pl api spring-boot:run"
            % (url, exc.reason)
        )


class Diff:
    """收集分歧。行数就是结论，别让「找到了 N 处」埋在一大段输出里。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.compared = 0

    def check(self, what: str, java: object, python: object) -> None:
        self.compared += 1
        if java != python:
            self.lines.append("  %-48s java=%r  python=%r" % (what, java, python))

    def fail(self, what: str) -> None:
        # 计数不算「比过」，只报分歧：分母是「真正比对过的字段数」。
        self.lines.append("  %-48s %s" % (what, "(结构性缺失，没得比)"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Java REST 与 Python dashboard 的显示口径对拍")
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--dir", default=".rounds/i1")
    args = parser.parse_args()

    health = get_json(args.base + "/api/health")
    reports_payload = get_json(args.base + "/api/reports")
    print("后端报告目录：%s" % health.get("reportsDir"))
    print("后端声明指标数：%s（Python 侧 %d）" % (health.get("metricCount"), len(dashboard.METRIC_SPECS)))

    # 先钉住**列表端点的信封形状**：前端就是按 {"reports": [...]} 拆包的。
    # 少了这一条，「后端返回裸数组、前端读 body.reports」这种缝上的错可以与本脚本同时全绿
    # （本脚本第一版就是这样绿的，而页面显示「目录里没有报告」）。
    if not isinstance(reports_payload, dict) or not isinstance(reports_payload.get("reports"), list):
        print(
            "\nFAIL: /api/reports 的信封不是 {\"reports\": [...]}（实为 %s）—— "
            "前端的拆包方式与后端不一致" % type(reports_payload).__name__
        )
        return 1
    reports = reports_payload["reports"]

    if not reports:
        print("\nFAIL: 后端一份报告都没返回 —— 没有比对到任何东西，脚本不能报绿")
        return 1

    directory = Path(args.dir)
    if not directory.is_absolute():
        directory = REPO_ROOT / directory

    diff = Diff()
    for item in reports:
        report_id = item["id"]
        path = directory / (report_id + ".json")
        if not path.is_file():
            diff.fail("%s: 本地找不到 %s" % (report_id, path))
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        py_view = dashboard.read_view(payload)
        java_view = get_json(args.base + "/api/reports/" + report_id)

        diff.check("%s: schema" % report_id, java_view.get("schema"), dashboard.REPORT_SCHEMA)
        diff.check("%s: symbol" % report_id, java_view.get("symbol"), py_view.symbol)
        diff.check("%s: strategy_id" % report_id, java_view.get("strategyId"), py_view.strategy_id)
        diff.check("%s: strategy_version" % report_id, java_view.get("strategyVersion"), py_view.strategy_version)
        diff.check("%s: data_version" % report_id, java_view.get("dataVersion"), py_view.data_version)
        diff.check("%s: status" % report_id, java_view.get("status"), py_view.status)
        diff.check("%s: seed" % report_id, java_view.get("seed"), py_view.seed)
        diff.check(
            "%s: window" % report_id,
            java_view.get("window"),
            list(py_view.window) if py_view.window else None,
        )

        counts = java_view.get("counts") or {}
        diff.check("%s: counts.equity_curve" % report_id, counts.get("equityCurve"), py_view.counts.get("equity_curve"))
        diff.check("%s: counts.trades" % report_id, counts.get("trades"), py_view.counts.get("trades"))
        diff.check("%s: counts.orders" % report_id, counts.get("orders"), py_view.counts.get("orders"))

        java_metrics = java_view.get("metrics") or []
        diff.check("%s: 指标条数" % report_id, len(java_metrics), len(py_view.metrics))
        for index, py_metric in enumerate(py_view.metrics):
            if index >= len(java_metrics):
                diff.fail("%s: metrics[%d] (%s) java 侧没有" % (report_id, index, py_metric.key))
                continue
            java_metric = java_metrics[index]
            spec = dashboard.SPEC_BY_KEY[py_metric.key]
            where = "%s: metrics[%d] %s" % (report_id, index, py_metric.key)
            diff.check(where + ".key", java_metric.get("key"), py_metric.key)
            diff.check(where + ".group", java_metric.get("group"), py_metric.group)
            diff.check(where + ".label", java_metric.get("label"), py_metric.label)
            diff.check(where + ".unit", java_metric.get("unit"), spec.unit)
            diff.check(where + ".digits", java_metric.get("digits"), spec.digits)
            # 这一行才是本脚本存在的理由：屏幕上会显示的那个字符串。
            diff.check(where + ".text", java_metric.get("text"), py_metric.text)

        java_curve = java_view.get("curve") or []
        diff.check("%s: 曲线点数" % report_id, len(java_curve), len(py_view.curve))
        for index, py_point in enumerate(py_view.curve):
            if index >= len(java_curve):
                diff.fail("%s: curve[%d] java 侧没有" % (report_id, index))
                break
            java_point = java_curve[index]
            where = "%s: curve[%d]" % (report_id, index)
            diff.check(where + ".timestamp", java_point.get("timestamp"), py_point.timestamp)
            diff.check(where + ".equity", java_point.get("equity"), py_point.equity)
            diff.check(where + ".drawdown", java_point.get("drawdown"), py_point.drawdown)

    print("\n比对过的字段数：%d（报告 %d 份）" % (diff.compared, len(reports)))
    if diff.compared == 0:
        # 提取为空必须判失败：这时候「0 分歧」只是因为一个字段都没比到。
        print("FAIL: 一个字段都没比到，本次结论无意义")
        return 1
    if diff.lines:
        print("FAIL: 发现 %d 处分歧：" % len(diff.lines))
        for line in diff.lines:
            print(line)
        return 1
    print("PASS: 两侧逐字段一致（含屏幕上会显示的 text）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
