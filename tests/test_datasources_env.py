"""`.env` 供值通道（`quanauto/datasources.py` 的凭证读取）回归。

数据源适配器里**第一次**出现需要鉴权的源（tushare），于是「凭证从哪来」这件事
第一次要定规矩。本文件的判据不是「能读出字符串」，而是四条容易静静失守的性质：

1. **真实环境变量优先于 `.env` 文件** —— 反过来会让 CI / 容器里注入的凭证被一份
   遗留的本地 `.env` 悄悄盖掉，而症状只是「鉴权失败」。
2. **取不到凭证必须响亮地失败**，且**失败信息里不许出现令牌值** —— 报错会被贴进
   日志 / issue / 截图。
3. **坏文件不许被静默忽略** —— PS 5.1 的 `>` 重定向写的是 **UTF-16LE**；按 UTF-8
   硬读能读出来，只是一堆夹着 NUL 的「键」，于是查找落到「没有凭证」那条路，
   让人去错怪令牌的值，而不是去查文件编码。
4. **解析规则本身**（注释 / 空行 / `export` / 成对引号 / 值里含 `=` / CRLF）。

本文件**不联网**、不需要真令牌，也不读仓库里可能存在的真 `.env`（每个用例都显式
传 `env_file=`）—— 判据不许依赖本机状态。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quanauto.datasources import (
    ENV_FILE_NAME,
    TUSHARE_TOKEN_ENV,
    project_root,
    read_env_file,
    tushare_token,
)
from quanauto.errors import SourceAdapterError

#: 明显不是真密钥的样本值。用来断言「它不会出现在报错文本里」。
FAKE_SECRET = 'not-a-real-secret-0000'


def _write_bytes(path: Path, data: bytes) -> str:
    """按**字节**落盘。

    不用 `Path.write_text`：那几个编码用例（CRLF / BOM / UTF-16）要的就是**精确的
    字节序列**，让文本层的换行转换插一脚，样本本身就不再是我想测的那个样本了。
    """
    path.write_bytes(data)
    return str(path)


# ── 解析规则 ────────────────────────────────────────────────────────────────

def test_read_env_file_parses_the_plain_cases(tmp_path: Path) -> None:
    """干净样本（防误报）：注释 / 空行 / `export` / 成对引号 / 值里含 `=`。

    `WITH_EQUALS` 那一条是有来历的收紧：按 `split('=', 1)` 之外的任何切法，base64
    类的令牌（结尾常常是 `=`）都会被悄悄截断，而截断后的令牌**看起来**是个令牌。
    """
    env = _write_bytes(tmp_path / ENV_FILE_NAME, (
        '# 整行注释\n'
        '\n'
        '   \n'
        '#PLAIN_COMMENT_NO_SPACE\n'
        'PLAIN=abc\n'
        'export EXPORTED=def\n'
        'QUOTED_DOUBLE="a b"\n'
        "QUOTED_SINGLE='c d'\n"
        'WITH_EQUALS=YQ==\n'
        '  PADDED  =  e f  \n'
        "UNPAIRED='tail\n"
        'NO_EQUALS_LINE\n'
        '=only_value\n'
        '# WITH_EQUALS_TAIL COMMENT\n'
    ).encode('utf-8'))
    assert read_env_file(env) == {
        'PLAIN': 'abc',
        'EXPORTED': 'def',
        'QUOTED_DOUBLE': 'a b',
        'QUOTED_SINGLE': 'c d',
        'WITH_EQUALS': 'YQ==',
        'PADDED': 'e f',
        # 引号不配对 ⇒ 原样保留。**不**自作聪明地剥掉一个引号：那样会把一个
        # 「文件写坏了」变成「令牌多了一个引号」，还是离原因很远。
        'UNPAIRED': "'tail",
    }
    # 没有 `=` 的行与空键的行都不产生条目（显式断言，别只靠上面那个等式）
    parsed = read_env_file(env)
    assert 'NO_EQUALS_LINE' not in parsed
    assert '' not in parsed


def test_read_env_file_handles_crlf(tmp_path: Path) -> None:
    """CRLF：`.env` 在 Windows 上多半是这个行尾，而它**会**静默变成值的一部分。

    本仓库已经吃过一次「正则写裸 `\\n` 对 CRLF 静默失配」的亏 ⇒ 这里单独钉一条。
    收尾若只 strip 空格而不 strip `\\r`，值就成了 `'abc\\r'`：它不是空、也不报错，
    只是永远匹配不上源，鉴权失败的现场离这里很远。
    """
    env = _write_bytes(tmp_path / ENV_FILE_NAME,
                       ('%s=abc\r\n# c\r\n' % TUSHARE_TOKEN_ENV).encode('utf-8'))
    assert read_env_file(env)[TUSHARE_TOKEN_ENV] == 'abc'


def test_read_env_file_accepts_a_utf8_bom(tmp_path: Path) -> None:
    """UTF-8 BOM：不少编辑器另存为 UTF-8 时会加上它。BOM 不许混进**键名**。"""
    env = _write_bytes(tmp_path / ENV_FILE_NAME,
                       b'\xef\xbb\xbf' + ('%s=abc\n' % TUSHARE_TOKEN_ENV).encode('utf-8'))
    assert read_env_file(env)[TUSHARE_TOKEN_ENV] == 'abc'


def test_read_env_file_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    """没有 `.env` 是**正常状态**（CI 就没有）⇒ 返回 `{}`，不抛。"""
    assert read_env_file(str(tmp_path / 'does-not-exist.env')) == {}


def test_read_env_file_rejects_utf16_loudly(tmp_path: Path) -> None:
    """触发测试：PS 5.1 的 `>` 重定向写的是 UTF-16LE。

    它**必须**响亮地失败，不能返回一个空 dict —— 空 dict 与「这台机器没配 .env」
    长得一模一样，于是有人去怀疑令牌的值，而真问题在文件编码。
    """
    env = _write_bytes(tmp_path / ENV_FILE_NAME,
                       ('%s=abc\n' % TUSHARE_TOKEN_ENV).encode('utf-16-le'))
    with pytest.raises(SourceAdapterError) as info:
        read_env_file(env)
    assert 'NUL' in str(info.value)
    assert _rejected_kind(info.value) == 'SOURCE_AUTH'


def _rejected_kind(error: SourceAdapterError) -> str:
    """坏 `.env` 也必须走**本模块唯一允许逃出去的异常类**。

    若这里逃的是裸 `UnicodeDecodeError` / `ValueError`，上层读到的语义是
    「调用方写错了」—— 那正是本模块的异常规矩要挡的假信号。
    """
    return error.kind


# ── 优先级 ──────────────────────────────────────────────────────────────────

def test_real_environment_variable_wins_over_the_file(tmp_path: Path) -> None:
    """**本文件存在的主要理由之一。** 反过来（文件优先）会让 CI / 容器里注入的
    凭证被一份遗留的本地 `.env` 盖掉，症状只是「鉴权失败」。
    """
    env_file = _write_bytes(tmp_path / ENV_FILE_NAME,
                            ('%s=from-file\n' % TUSHARE_TOKEN_ENV).encode('utf-8'))
    assert tushare_token(environ={TUSHARE_TOKEN_ENV: 'from-env'},
                         env_file=env_file) == 'from-env'


def test_file_is_used_when_the_environment_has_nothing(tmp_path: Path) -> None:
    env_file = _write_bytes(tmp_path / ENV_FILE_NAME,
                            ('# c\n%s=from-file\n' % TUSHARE_TOKEN_ENV).encode('utf-8'))
    assert tushare_token(environ={}, env_file=env_file) == 'from-file'


def test_blank_environment_variable_falls_back_to_the_file(tmp_path: Path) -> None:
    """空串与空白按「没有」处理 —— `.env.example` 原样复制过来就是 `TUSHARE_TOKEN=`，
    而 shell 里 `set TUSHARE_TOKEN=` 也是常见的残留形态。"""
    env_file = _write_bytes(tmp_path / ENV_FILE_NAME,
                            ('%s=from-file\n' % TUSHARE_TOKEN_ENV).encode('utf-8'))
    assert tushare_token(environ={TUSHARE_TOKEN_ENV: '   '}, env_file=env_file) == 'from-file'


# ── 失败路径 ────────────────────────────────────────────────────────────────

def test_missing_token_raises_source_adapter_error(tmp_path: Path) -> None:
    """取不到凭证必须**响亮**失败，类别落在闭集里的 `SOURCE_AUTH`（动作：去配凭证），
    且不可重试（重试不会让凭证出现）。"""
    with pytest.raises(SourceAdapterError) as info:
        tushare_token(environ={}, env_file=str(tmp_path / 'does-not-exist.env'))
    error = info.value
    assert error.kind == 'SOURCE_AUTH'
    assert error.retryable is False
    assert TUSHARE_TOKEN_ENV in str(error)          # 报错要指路
    assert ENV_FILE_NAME in str(error)              # ……指到具体哪个文件


def test_the_token_value_never_appears_in_an_error(tmp_path: Path) -> None:
    """凭证**不进报错文本**：报错会被贴进日志 / issue / 截图。

    样本构成：**先**证明读取侧真的认得出这个值（正样本），**再**构造一条失败路径
    断言它不在文案里 —— 只测后者的话，「读不出来」与「读了但没往文案里写」在报告
    里是一样的。
    """
    env_file = _write_bytes(tmp_path / ENV_FILE_NAME,
                            ('%s=%s\n' % (TUSHARE_TOKEN_ENV, FAKE_SECRET)).encode('utf-8'))
    assert tushare_token(environ={}, env_file=env_file) == FAKE_SECRET
    for environ, path in (
        ({}, str(tmp_path / 'does-not-exist.env')),
        ({TUSHARE_TOKEN_ENV: '   '}, str(tmp_path / 'does-not-exist.env')),
        ({}, _write_bytes(tmp_path / 'broken.env', ('%s=x\n' % TUSHARE_TOKEN_ENV)
                          .encode('utf-16-le'))),
    ):
        with pytest.raises(SourceAdapterError) as info:
            tushare_token(environ=environ, env_file=path)
        assert FAKE_SECRET not in str(info.value)


# ── 路径口径 ────────────────────────────────────────────────────────────────

def test_default_env_path_is_the_repository_root() -> None:
    """默认读的是**项目根**的 `.env`，不是包目录 —— 凭证属于这份工作树，不属于包。

    锚点用 `pyproject.toml` 而不是仓库目录名：目录名会随克隆位置变（CI 上就不是
    这个名字），那样这条用例会变成一条**环境判据**，在别处静默失效。
    """
    assert os.path.isfile(os.path.join(project_root(), 'pyproject.toml'))
    assert os.path.basename(project_root()) != 'quanauto'
