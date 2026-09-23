"""智能量化交易平台 —— Python 研究 / 回测线。

**当前状态：I1 已交付第一条端到端竖切**（CSV → MA 双均线 → 撔合 → 绩效 → 回测报告），
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
