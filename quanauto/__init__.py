"""智能量化交易平台 —— Python 研究 / 回测线。

**迭代状态**：I1 已交付第一条端到端竖切（CSV → MA 双均线 → 撮合 → 绩效 → 回测报告）；
I2 的 S1（PIT / `as_of` 边界层，`quanauto/datacenter.py`）、S2（采集侧源适配器，
`quanauto/datasources.py`）与 S3（落库侧 `quanauto/pgstore.py` + 引擎改读 `as_of()` 产出的
feed）已落地，**但 I2 未关闭** —— 订正（2026-09-29）：原先这里写「真实数据源**从未联网联调**」，
那句话**已过期**（I2a/I2b 真的联调过：腾讯日线、东财财务表、一次真入库）；**I2c** 又接上了复权因子的
**采集路径**（tushare `adj_factor`）；**2026-09-29 晚**把**读数侧与写入口**也接上了
（`get_adjustment_factor()` 从 `dc_adjust_factor` 读累计因子：`HFQ`/`QFQ` 缺行 ⇒ 抛 DATA_001，
`AdjustType.NONE` 才不查库直接返回 1.0；写入口是 `quanauto/pgstore.py` 的 `PgFactorIngestor`）。
**2026-09-29 晚 Ⅱ 又把「复权价」实施了**（订正：这一句此前写的是「**但缺口没关闭，
只是换了名字**：现在开着的是『**复权价仍未实施**』—— `BarData` 的 OHLC 仍是不复权价、
`quanauto/engine.py` 里没有一处算术用因子 ⇒ 回测口径**仍等于「不复权」**」，那句话**已作废**）：
`DbDataFeed._row_to_bar` 现在按 `_price_scale()` 给的倍数对 OHLC **四列各乘一次**
（唯一一处算式是 `datafeed._rescale_price`，全仓库**两个**调用点：`DbDataFeed._row_to_bar`
与 `CsvDataFeed._load_data`（2026-09-29 晩 Ⅲ 把第二条也接上了）；`NONE` ⇒ 1.0 不查库、`HFQ` ⇒ 该日累计因子、
`QFQ` ⇒ 该日因子 ÷ 视图末日因子），`volume` / `amount` **刻意不乘** ⇒ 回测口径**不再是「不复权」**。
守着这条缝的是读侧 7 条用例 + 端到端控制组 `test_adjusted_prices_reach_the_strategy_through_the_engine`
（变异 `S11` / `A12`），逐条见数据中心契约附录 A6 / B21.3 的订正块。
**I2 仍未关闭的理由也换了**，而 2026-10-01 又变了一次：收口条件**①（复权价 / 分红 / 写入路径）
三条现已全关** —— ①ⓐ~~分红（`get_dividend()` 恒 `0.0`）~~ **2026-10-01 已收口**：
`DbDataFeed.get_dividend()` 从 `dc_dividend` 按 `ex_date` 取那一天的行、把每行的
`announce_date` 交给 PIT 守卫（**公告日才是可见性依据**，D4）；**缺行/缺源都返回 `0.0`**
（与主契约 §2.2.1 的示例实现、`CsvDataFeed` 同口径 —— 与 `get_adjustment_factor` 缺行抛
DATA_001 **刻意不对称**，理由见附录 B22.3）；①ⓒ~~`validate_frame` 对复权帧一条判据都没有~~
—— **2026-09-29 晩 Ⅳ 已收口**（值域对齐 `ck_dc_factor_positive` / 帧级自然键重复 / 认 schema）。
⇒ **真正开着的现在是②**（「各通道『哪些位置已实测、哪些仍是推断』的未确认清单」）。
⚠️ 分红**读得出来**了 ≠ 引擎在绩效里**计**了分红：`quanauto/engine.py` 一行没动（附录 B22.2）。
①ⓑ~~两个 ingestor 在 `quanauto/` 内**零调用者**（接口有实现，没有产品写入路径）~~ ——
**2026-09-30 已接上**：`quanauto/ingest.py` 就是那条缺的编排层（取数 → 校验 → 盖戳 →
落库 → 写 `dc_ingest_run` 批次留痕），两个 ingestor 各有一个调用点，`tests/test_ingest.py`
45 条压着它。⚠️ **但它不主张这条路径被真实走过**：`quanauto/cli.py` 没开采集子命令、
真库上没跑过 ⇒ 「采集覆盖面够不够」「真源上能不能通」仍归 `docs/迭代计划.md` I2 的
「产物」行，那一行**照旧不打勾**。
I3（风控真正介入交易）已落地两条线：`quanauto/risk.py` 的风控引擎本体，以及
`BacktestEngine.attach_risk_engine()` 这条**默认关闭**的接线 —— 不接引擎时下单路径与 I1
逐一相同，接了之后每单在下单前必经 `RiskEngine.check()`。
迭代边界以 `docs/迭代计划.md` 为准，别把 S3 读成 I2 收工。

实现分布在本包的各模块里，入口是 `quanauto.cli`。

这个 `__init__` **不 re-export** 子模块的符号：全量 re-export 会让「`import quanauto`」
与「把整条竖切拉起来」看起来一样，也会把重依赖在 import 期就拖进来。要做什么
以 `docs/迭代计划.md` 的竖切为准，接口签名以 `docs/智能量化交易平台-核心模块接口契约文档.md` 为准。

模块的导入必须是**轻**的：重依赖（pandas / torch / matplotlib …）一律在函数内延迟导入，
`tests/test_skeleton.py::test_import_does_not_pull_heavy_dependencies` 守着这条。

版本号有两个来源（本文件与 `pyproject.toml`），改一处不改另一处会红。
"""

from __future__ import annotations

__version__ = "0.1.0"

# 不在这里列举实现符号 —— 用「先列上以后要有的名字」的方式预支接口，
# 会让「名字在」与「能跑」长得一样（I0 时刻意空着，I1 之后仍不必填）。
__all__: list[str] = []
