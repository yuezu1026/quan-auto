"""I0 骨架自检：证明「可运行」这一半真的成立，而不是「pytest 退出码 0」这种空判据。

这三条测试都**不是**占位符 —— 每一条都钉住一个真实的漂移源：
  1. 同一件事写在两个地方（pyproject 的 version 与包里的 __version__）⇒ 必须核对
  2. 研究层 import 时不该拖入重型依赖（pandas / torch / matplotlib …）⇒ 一旦有人
     在模块顶层 `import pandas`，全仓库每个 import quanauto 的进程都变慢
  3. 「测试是否存在」不能靠人记得有多少个测试文件 ⇒ 数量地板 + 收集到 0 个也要红

第 3 条特别说明：pytest 在「一个测试都没收集到」时自身退出码是 **5**（非 0），
所以 CI 不会静默变绿。这里再显式断言一次，是为了让人在本地也能一眼看到地板。
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# 研究层的「重」依赖：import quanauto 时一个都不该被拖进来。
HEAVY_MODULES = ("pandas", "numpy", "torch", "matplotlib", "duckdb", "pyarrow")


def test_version_matches_pyproject() -> None:
    """包里的 __version__ 与 pyproject.toml 的 project.version 必须逐字相同。"""
    import quanauto

    with PYPROJECT.open("rb") as fp:
        data = tomllib.load(fp)

    declared = data["project"]["version"]
    assert quanauto.__version__ == declared, (
        f"版本号两处不一致：pyproject.toml={declared!r} vs "
        f"quanauto.__version__={quanauto.__version__!r}"
    )
    # PEP 440 的最小形状检查（不是完整实现，够拦住「v0.1」「0.1.0-dev」这类写法）。
    parts = quanauto.__version__.split(".")
    assert len(parts) >= 2 and all(p.isdigit() for p in parts), (
        f"__version__ 不是 PEP 440 的 release 形式：{quanauto.__version__!r}"
    )


def test_import_does_not_pull_heavy_dependencies() -> None:
    """import quanauto 必须是轻的：重型依赖一个都不能在 import 时被加载。"""
    code = (
        "import sys, quanauto;"
        f"heavy=[m for m in {HEAVY_MODULES!r} if m in sys.modules];"
        "print(';'.join(heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"import quanauto 失败：\n{out.stdout}\n{out.stderr}"
    pulled = [m for m in out.stdout.strip().split(";") if m]
    assert pulled == [], f"import quanauto 拖入了重型依赖：{pulled}（改成函数内延迟 import）"


def test_there_is_a_real_test_floor() -> None:
    """测试文件数量地板，并且这个文件本身必须被收集到。"""
    test_files = sorted((REPO_ROOT / "tests").glob("test_*.py"))
    assert len(test_files) >= 1, "tests/ 下一个 test_*.py 都没有 —— 之后所有「绿」都不作数"
    assert Path(__file__).resolve() in test_files, "本文件没被 test_*.py 规则收集到"
