"""智能量化交易平台 —— Python 研究 / 回测线。

**当前状态：I0 骨架阶段，本包只有结构，没有任何实现。**

这里刻意**不放** `NotImplementedError` 占位模块：占位 stub 会让「已实现」与
「能 import」看起来一样，而 I1 的任务是**测试先行**地把它写出来（红源见
`docs/开发工作流规范.md` 的 TDD 一节）。要做什么以 `docs/迭代计划.md` 的竖切为准，
接口签名以 `docs/智能量化交易平台-核心模块接口契约文档.md` 为准。

模块的导入必须是**轻**的：重依赖（pandas / torch / matplotlib …）一律在函数内延迟导入，
`tests/test_skeleton.py::test_import_does_not_pull_heavy_dependencies` 守着这条。

版本号有两个来源（本文件与 `pyproject.toml`），改一处不改另一处会红。
"""

from __future__ import annotations

__version__ = "0.0.0"

# 还没有对外公开的实现，所以这里是空的 —— 别用「先列上以后要有的名字」的方式预支接口。
__all__: list[str] = []
