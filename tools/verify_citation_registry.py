#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据可达性门禁（tier-A）：被引用的路径，要么在 git 里，要么在登记处写明「为什么不进仓、归哪一节关」。

## 守的是什么

本仓库的正文（数据中心契约、迭代计划、`quanauto/*.py`、`tests/*.py`）大量用反引号引用**本机产生的**
证据路径 —— 探针脚本、探针输出、冒烟报告。这些路径被 `.gitignore` 排除，**克隆里没有它们**。
而 `tools/verify_skeleton.py` 的 `check_map_paths()` 只做 `os.path.exists()`：文件在本机存在即通过。
两者相乘就是一条实测过的缝 —— 本机全绿，克隆里那批「不可达引用」全部指向空地址。

本门禁把「这条引用在克隆里还在不在」变成一条判据，并强制每一条**不在**的引用都在
`tools/evidence-reachability.json` 里写明：为什么不进仓、归契约 / 计划的哪一节关。

## 判什么

判据面 = `git ls-files` 里那批**人手写的文本**（扩展名在 `CORPUS_EXTS` 里）。对每个文件取出所有
反引号 token，`ref_shape()` 过一遍形状（判据写在那几个常量的注释里），剩下的就是「看起来像仓库内
相对路径」的引用。每一条这样的引用落进四格之一：

1. **在 git 索引里** ⇒ 可达，什么都不说。
2. **不在索引里、`git check-ignore` 排除它、登记处又有它** ⇒ 登记过的不可达引用，不报。
3. **不在索引里、被排除、登记处没有它** ⇒ `EVR-UNREACHABLE`（FAIL）：克隆里这条引用是空地址。
4. 登记处那一行本身要过 `EVR-PATH-BAD` / `EVR-OWNER-MISSING` / `EVR-OWNER-BAD` /
   `EVR-REASON-SHORT`，而且不能是**作废行**（`EVR-REGISTRY-GHOST`：那条路径已经进仓，或已不再被排除）。

另有一条**读数**核对：契约里那句「被引用但不进仓的路径共 N 处」必须唯一、且 N 等于登记处行数
（`EVR-COUNT-MISMATCH`）。

三条**空转守卫**：语料面为空 ⇒ `EVR-EMPTY-CORPUS`；一条引用都没提出来 ⇒ `EVR-EMPTY-REFS`；
登记处读不到 / 不是 JSON / 顶层形状不对 ⇒ `EVR-REGISTRY-MISSING`。前两条直接终止 —— 后面每一项
都在空转的时候打印「0 问题 PASS」是假绿，本仓库的老毛病。

## 为什么用 `git check-ignore` 而不是自己解析 `.gitignore`

`.gitignore` 是**唯一**的排除真值来源。自己写一份 `_is_ignored()` 等于把仓库布局抄进 Python：
以后有人加一条排除规则（比如让某批快照不进仓），门禁会**静默**地不认它 —— 那正是本仓库反复防的
「判据与真值各存一份副本」。调用真 git 之后，排除规则一改，本门禁自动跟着改。

## 登记而不是覆盖

登记**不解除**任何缺口：一条路径被登记，只说明「它不可达这件事被看见了」，不说明它已经进仓、
也不说明引用它那句话现在仍然成立。两者是两件事，别把「已登记」读成「已验证」。

## 它探不到什么（写了「探到了」就必须把这些一起说出来）

- **不判「被引用的路径根本不存在」**。除了被排除的那一批，还有一批被引用但本机也不存在的路径，
  形状五花八门：构建产物（前端打包出来的文件名带内容哈希）、git 的版本语法、被省略号截短的路径，
  以及门禁自测用的**反例**路径。反例是合法的 —— 一份「注定失败的输入」本来就该指向不存在的路径。
  机器区分不了「反例」与「笔误」，所以这一格**明确不判**，边界登记在契约附录 J 的 J-18 之后。
- **不判内容**。它只回答「这个地址在克隆里存不存在」，不回答「那份证据现在还支不支持引用它的论断」。
  后者是探针自己的事：重跑一遍才算。
- **不跑任何探针**，不起容器、不连库、不联网、不装包。
- **语料面只是手写的文本**：`.txt` / `.json` / `.sql` / `.css` / `.xml` / `.docx` 不在扫描面里。
  排除 `.docx` 是因为它是那三份 md 的**源**，机器读不出里面的反引号；排除 `.txt` / `.json`
  是因为它们是**产物**（本仓库全部被跟踪的 `.txt` 都是本机跑出来的报告），不是引用别人的一方。
- **`git check-ignore` 说的是「按当前 `.gitignore` 会被排除」，不是「一定取不到」**：被排除的文件
  可以被 `git add -f` 强加进索引。所以「不在索引里」与「被排除」是两条独立的判据，本门禁两条都查。

## 对脚本自己的约束

- 只用标准库。`git` 是外部命令 —— 本仓库的判据全都建立在「这是个 git 仓库」之上，
  `tools/verify_appendix_refs.py` 拿不到 `git ls-files` 时直接拒判，同一条纪律。
- **只读**：不写任何文件。写文件的后果由 `tools/run_all_gates.py` 的副作用指纹兜住。
- 自测里那个临时 git 仓库造在系统临时目录（不在仓库内），跑完删掉。

## 用法

    python tools/verify_citation_registry.py             # 判整个仓库
    python tools/verify_citation_registry.py <root>       # 判别的根目录
    python tools/verify_citation_registry.py --selftest   # 触发测试
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_REL = 'tools/evidence-reachability.json'
CONTRACT_REL = 'docs/智能量化交易平台-数据中心接口契约文档.md'
PLAN_REL = 'docs/迭代计划.md'

# 判据面：只收**人手写的文本**。`.txt`/`.json`/`.sql` 是产物，`.docx` 机器读不出反引号。
CORPUS_EXTS = ('.md', '.py', '.jsx', '.js', '.java', '.yml', '.html')

# token 形状上要像「仓库内相对路径」，得带一个这些扩展名之一（否则 `行/列` 这种也会被当成引用）。
KNOWN_EXTS = ('.py', '.md', '.json', '.toml', '.yml', '.yaml', '.sql', '.txt', '.csv',
              '.docx', '.cfg', '.ini', '.proto', '.html', '.js', '.jsx', '.java',
              '.css', '.xml', '.tsv', '.ts', '.sh', '.bat', '.ps1')

# 这些字符一出现就说明它不是一个裸的仓库相对路径：
# 空白（是句子）、`*`（是 glob，判不到具体文件）、`<`/`{`（是占位符）、
# `:` 与 `\`（是 URL、git 版本语法或 Windows 绝对路径）。
SHAPE_REJECT = ' \t*<{:\\'

# 引这些目录是在说「环境」或「构建产物」，不是在指一份证据：它们被 `.gitignore` 排除是设计如此，
# 不该要求登记。`.rounds/`、`.github/` 有意**不在**这张名单里 —— 前者正是本门禁要管的东西。
ENV_DIRS = frozenset(('.venv', 'venv', 'node_modules', 'target', '.git', '__pycache__'))

# 契约里那句读数。必须唯一，且数字等于登记处行数。
COUNT_RE = re.compile(r'被引用但不进仓的路径共[ \t]*\*{0,2}(\d+)\*{0,2}[ \t]*处')
BACKTICK_RE = re.compile(r'`([^`\n]+)`')
HEADING_RE = re.compile(r'^#{2,4}[ \t]+(\S+)', re.M)

REASON_MIN = 12
GIT_TIMEOUT = 120


def read_text(path):
    """读成 LF 结尾的文本。CRLF 归一化是必须的：下面几条正则都按 `\\n` 写。"""
    with open(path, 'r', encoding='utf-8-sig') as fh:
        return fh.read().replace('\r\n', '\n').replace('\r', '\n')


def git(root, args, stdin_text=None):
    """(rc, stdout, stderr)。git 不在 PATH 上时 rc=None。"""
    try:
        proc = subprocess.run(['git'] + list(args), cwd=root, input=stdin_text,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, encoding='utf-8',
                              errors='replace', timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return (None, '', str(exc))
    return (proc.returncode, proc.stdout, proc.stderr)


def tracked_files(root):
    """→ (名单, 错误)。用 `-z`：本仓库里有带空格的路径，别的分隔法会把它们切开。"""
    rc, out, err = git(root, ['ls-files', '-z'])
    if rc != 0:
        return None, '拿不到 git ls-files（rc=%r）：%s' % (rc, (err or '').strip()[:200])
    return [p for p in out.split('\0') if p], None


def ignored_paths(root, paths):
    """→ ({路径: (来源文件, 行号, 模式)}, 错误)。**没被排除的路径不会出现在返回值里**。

    `--verbose` 的每条记录是 4 个 NUL 分隔字段：来源文件 / 行号 / 模式 / 路径。
    退出码没有含义（有命中与无命中都可能给 0 或 1），所以只解析 stdout。
    """
    paths = [p for p in paths if p]
    if not paths:
        return {}, None
    rc, out, err = git(root, ['check-ignore', '--stdin', '-z', '--verbose'],
                       '\0'.join(paths) + '\0')
    if rc is None:
        return None, '跑不起 git check-ignore：%s' % (err or '').strip()[:200]
    fields = out.split('\0')
    result = {}
    for i in range(0, len(fields) - 3, 4):
        src, line, pattern, path = fields[i], fields[i + 1], fields[i + 2], fields[i + 3]
        result[path] = (src, line, pattern)
    return result, None


def ref_shape(tok):
    """这个反引号 token 形状上像不像「仓库内相对路径」？只有像的才进判据面。

    像 = 带 `/`、不以 `-`/`/`/`./`/`../` 开头（命令行片段、绝对路径）、不以 `/` 结尾（是个目录）、
    不含 `SHAPE_REJECT` 里的字符、扩展名在 `KNOWN_EXTS` 里、且首段不是 `ENV_DIRS`。
    """
    if not tok or '/' not in tok:
        return False
    if tok.startswith(('-', '/', './', '../')):
        return False
    if tok.endswith('/'):
        return False
    if any(ch in tok for ch in SHAPE_REJECT):
        return False
    if tok.split('/')[0] in ENV_DIRS:
        return False
    return os.path.splitext(tok)[1].lower() in KNOWN_EXTS


def extract_refs(texts):
    """{路径引用: [引用它的语料文件, ...]}，按语料文件里的出现顺序去重。"""
    refs = {}
    for rel in sorted(texts):
        for tok in BACKTICK_RE.findall(texts[rel]):
            tok = tok.strip()
            if ref_shape(tok):
                files = refs.setdefault(tok, [])
                if rel not in files:
                    files.append(rel)
    return refs


def parse_registry(text):
    """→ (entries, problems)。text=None 表示文件读不到。"""
    if text is None:
        return [], [('EVR-REGISTRY-MISSING', '登记处读不到：%s' % REGISTRY_REL)]
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return [], [('EVR-REGISTRY-MISSING', '登记处不是合法 JSON：%s' % exc)]
    if not isinstance(doc, dict) or not isinstance(doc.get('entries'), list):
        return [], [('EVR-REGISTRY-MISSING',
                     '登记处顶层要是 {"_note": ..., "entries": [{...}, ...]}')]
    entries = []
    for item in doc['entries']:
        entries.append(item if isinstance(item, dict) else
                       {'path': str(item), 'owner': '', 'why': ''})
    return entries, []


def load_registry(root):
    path = os.path.join(root, REGISTRY_REL.replace('/', os.sep))
    if not os.path.isfile(path):
        return parse_registry(None)
    try:
        return parse_registry(read_text(path))
    except (IOError, OSError) as exc:
        return [], [('EVR-REGISTRY-MISSING', '登记处读不出来：%s' % exc)]


def resolve_headings(root):
    """契约 + 计划里所有 `##`/`###`/`####` 标题的首个 token（去掉结尾的点）。

    登记行的 `owner` 必须命中这里 —— owner 是**引用**，标题一改名它就该报错，这是设计。
    """
    heads = set()
    for rel in (CONTRACT_REL, PLAN_REL):
        path = os.path.join(root, rel.replace('/', os.sep))
        if not os.path.isfile(path):
            continue
        try:
            text = read_text(path)
        except (IOError, OSError):
            continue
        for m in HEADING_RE.finditer(text):
            heads.add(m.group(1).rstrip('.'))
    return heads


def judge(refs, ignored, entries, headings, contract_text, tracked=(),
          corpus_size=1, registry_rel=REGISTRY_REL, registry_problems=()):
    """→ (problems, notes)。纯函数：不碰文件系统、不调 git，所以自测可以随便造输入。"""
    problems = []
    notes = []

    if not corpus_size:
        problems.append(('EVR-EMPTY-CORPUS',
                         '扫描面为空：一个语料文件都没选中，后面每一项都在空转'))
        return problems, notes
    if not refs:
        problems.append(('EVR-EMPTY-REFS',
                         '提取为空：%d 个语料文件里一条形状像仓库路径的引用都没提出来'
                         % corpus_size))
        return problems, notes

    problems.extend(registry_problems)

    tracked_set = set(tracked)
    if registry_rel not in tracked_set:
        problems.append(('EVR-REGISTRY-UNTRACKED',
                         '%s 不在 git 索引里 ⇒ 它自己就能骗过本门禁，而克隆里根本没有它'
                         % registry_rel))

    rows = {}
    for ent in entries:
        path = str(ent.get('path', '')).strip()
        if (not path or path.startswith('/') or '..' in path
                or '/' not in path or path.endswith('/')):
            problems.append(('EVR-PATH-BAD',
                             '登记行的 path 写法不合法：%r（要仓库相对、含 `/`、不含 `..`、'
                             '不以 `/` 结尾、不以 `/` 开头）' % path))
            continue
        if path in rows:
            problems.append(('EVR-PATH-BAD', '登记行重复：%s' % path))
            continue
        owner = str(ent.get('owner', '')).strip()
        if not owner:
            problems.append(('EVR-OWNER-MISSING', '%s 没写 owner ⇒ 这个缺口没人认领' % path))
        elif owner not in headings:
            problems.append(('EVR-OWNER-BAD',
                             '%s 的 owner=%r 在契约与计划的标题里找不到 ⇒ 这条引用已经失效'
                             % (path, owner)))
        why = str(ent.get('why', '')).strip()
        if len(why) < REASON_MIN:
            problems.append(('EVR-REASON-SHORT',
                             '%s 的 why 只有 %d 个字（少于 %d）⇒ 等于没写理由'
                             % (path, len(why), REASON_MIN)))
        rows[path] = ent

    for path in sorted(rows):
        if path in tracked_set:
            problems.append(('EVR-REGISTRY-GHOST',
                             '%s 已经进 git 索引了 ⇒ 这一行作废：可达性已经成立，不需要登记'
                             % path))
        elif path not in ignored:
            problems.append(('EVR-REGISTRY-GHOST',
                             '%s 既不在 git 索引里，也不再被 .gitignore 排除 ⇒ '
                             '「克隆里没有它」这句话不成立，这一行作废' % path))
        elif path not in refs:
            notes.append(('EVR-REGISTRY-ORPHAN',
                          '%s 仍被排除，但进仓的文件里已经没人引用它 ⇒ 这一行可以删掉（不是失败）'
                          % path))

    cited_ignored = [p for p in sorted(ignored) if p in refs]
    for path in cited_ignored:
        if path not in rows:
            src, line, pattern = ignored[path]
            problems.append(('EVR-UNREACHABLE',
                             '%s 被 %s 引用，但它被 .gitignore 排除（%s，模式 %s）⇒ '
                             '克隆里这条引用是空地址，而登记处里没有它'
                             % (path, '、'.join(refs[path]), src, pattern)))

    hits = COUNT_RE.findall(contract_text)
    if len(hits) != 1:
        problems.append(('EVR-COUNT-MISMATCH',
                         '契约里那句读数要么没有、要么出现了 %d 次 ⇒ 没法核对登记处行数'
                         % len(hits)))
    elif int(hits[0]) != len(rows):
        problems.append(('EVR-COUNT-MISMATCH',
                         '契约写「共 %s 处」，登记处有 %d 行' % (hits[0], len(rows))))

    notes.insert(0, ('TOTAL', 'corpus=%d path_shaped=%d reachable=%d untracked=%d registered=%d'
                     % (corpus_size, len(refs), len(refs) - len(cited_ignored),
                        len(cited_ignored), len(rows))))
    return problems, notes


def check_all(root):
    """→ (problems, notes)。除了 `git` 之外只读文件。"""
    tracked, err = tracked_files(root)
    if err:
        return [('EVR-NO-GIT', err)], []
    corpus = [p for p in tracked if p.endswith(CORPUS_EXTS)]

    texts = {}
    for rel in corpus:
        path = os.path.join(root, rel.replace('/', os.sep))
        try:
            texts[rel] = read_text(path)
        except (IOError, OSError):
            continue
    refs = extract_refs(texts)

    entries, registry_problems = load_registry(root)
    try:
        contract_text = read_text(os.path.join(root, CONTRACT_REL.replace('/', os.sep)))
    except (IOError, OSError):
        contract_text = ''
    headings = resolve_headings(root)

    tracked_set = set(tracked)
    candidates = sorted(set(refs) - tracked_set)
    candidates += [str(e.get('path', '')).strip() for e in entries]
    ignored, err = ignored_paths(root, candidates)
    if err:
        return [('EVR-NO-GIT', err)], []

    return judge(refs, ignored, entries, headings, contract_text, tracked=tracked,
                 corpus_size=len(corpus), registry_problems=registry_problems)


# ---------------------------------------------------------------- 自测

BASE_CONTRACT = '被引用但不进仓的路径共 1 处。'
BASE_REFS = {'.rounds/_probe.py': ['docs/spec.md', 'quanauto/mod.py']}
BASE_IGNORED = {'.rounds/_probe.py': ('.gitignore', '12', '.rounds/_*')}
BASE_ENTRY = {'path': '.rounds/_probe.py', 'owner': 'B14',
              'why': '本机一次性探针，含真实响应片段，不适合进仓'}
BASE_HEADINGS = frozenset(('B14', 'B15', 'B16'))
BASE_TRACKED = ('docs/spec.md', 'quanauto/mod.py', REGISTRY_REL)


def _base(**over):
    kw = {
        'refs': {k: list(v) for k, v in BASE_REFS.items()},
        'ignored': dict(BASE_IGNORED),
        'entries': [dict(BASE_ENTRY)],
        'headings': BASE_HEADINGS,
        'contract_text': BASE_CONTRACT,
        'tracked': BASE_TRACKED,
        'corpus_size': 2,
        'registry_problems': (),
    }
    kw.update(over)
    return kw


SAMPLES = []


def check(name, ok, detail):
    SAMPLES.append((name, bool(ok), detail))
    return bool(ok)


def run(name, want=(), forbid=(), clean=False, want_note=(), **over):
    problems, notes = judge(**_base(**over))
    codes = [c for c, _ in problems]
    note_codes = [c for c, _ in notes]
    ok = True
    if clean and problems:
        ok = False
    for code in want:
        if code not in codes:
            ok = False
    for code in forbid:
        if code in codes:
            ok = False
    for code in want_note:
        if code not in note_codes:
            ok = False
    detail = 'problems=%s notes=%s' % (codes or '-', note_codes or '-')
    return ok, detail


def shape_case(tok, want):
    got = ref_shape(tok)
    return got is want, 'ref_shape(%r)=%r want=%r' % (tok, got, want)


def sandbox_git_case():
    """真造一个 git 仓库（只 `git add`，不提交）来验 `tracked_files`/`ignored_paths` 这个适配器。

    `judge()` 是纯函数，所以 git 这一层没有别的办法被真正打到。
    """
    tmp = tempfile.mkdtemp(prefix='quanauto-evidence-')
    try:
        with open(os.path.join(tmp, '.gitignore'), 'w', encoding='utf-8') as fh:
            fh.write('.rounds/_*\n')
        os.makedirs(os.path.join(tmp, '.rounds', 'i1'))
        with open(os.path.join(tmp, 'kept file.md'), 'w', encoding='utf-8') as fh:
            fh.write('x\n')
        with open(os.path.join(tmp, '.rounds', '_probe.py'), 'w', encoding='utf-8') as fh:
            fh.write('x\n')
        with open(os.path.join(tmp, '.rounds', 'i1', 'r.json'), 'w', encoding='utf-8') as fh:
            fh.write('{}\n')
        rc, _, err = git(tmp, ['init', '-q'])
        if rc != 0:
            return False, 'git init 起不来：%s' % (err or '').strip()[:160]
        git(tmp, ['add', '-A'])

        tracked, err = tracked_files(tmp)
        if err:
            return False, err
        if sorted(tracked) != ['.gitignore', '.rounds/i1/r.json', 'kept file.md']:
            return False, 'tracked=%r（期望 .gitignore / .rounds/i1/r.json / kept file.md）' % (
                tracked,)

        ignored, err = ignored_paths(tmp, ['.rounds/_probe.py', 'kept file.md',
                                           '.rounds/i1/r.json', '.rounds/_never-existed.txt'])
        if err:
            return False, err
        if set(ignored) != {'.rounds/_probe.py', '.rounds/_never-existed.txt'}:
            return False, 'ignored=%r（期望只含那两条下划线路径）' % (sorted(ignored),)
        src, line, pattern = ignored['.rounds/_probe.py']
        if os.path.basename(src) != '.gitignore' or pattern != '.rounds/_*':
            return False, 'ignored 明细=%r' % (ignored['.rounds/_probe.py'],)
        return True, 'tracked=%d ignored=%d src=%s pattern=%s' % (
            len(tracked), len(ignored), os.path.basename(src), pattern)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def selftest(root):
    del root  # 自测不依赖真仓库；真仓库的正控在 main() 里
    check('positive-clean', *run('positive-clean', clean=True, want_note=('TOTAL',)))

    check('unreachable-no-row', *run('unreachable-no-row',
                                     entries=[], contract_text='被引用但不进仓的路径共 0 处。',
                                     want=['EVR-UNREACHABLE'],
                                     forbid=['EVR-REGISTRY-GHOST', 'EVR-COUNT-MISMATCH']))
    check('registry-untracked', *run('registry-untracked',
                                     tracked=('docs/spec.md',),
                                     want=['EVR-REGISTRY-UNTRACKED']))
    check('ghost-already-tracked', *run('ghost-already-tracked',
                                        tracked=BASE_TRACKED + ('.rounds/_probe.py',),
                                        want=['EVR-REGISTRY-GHOST'],
                                        forbid=['EVR-UNREACHABLE']))
    check('ghost-no-longer-ignored', *run('ghost-no-longer-ignored', ignored={},
                                          want=['EVR-REGISTRY-GHOST'],
                                          forbid=['EVR-UNREACHABLE']))
    check('orphan-row', *run('orphan-row',
                             refs={'docs/spec.md': ['db/data_center.sql']},
                             want=[], forbid=['EVR-UNREACHABLE'],
                             want_note=['EVR-REGISTRY-ORPHAN']))

    check('path-absolute', *run('path-absolute',
                                entries=[{'path': '/etc/hosts', 'owner': 'B14',
                                          'why': '一句足够长的理由，占位用'}],
                                want=['EVR-PATH-BAD']))
    check('path-no-slash', *run('path-no-slash',
                                entries=[{'path': 'README.md', 'owner': 'B14',
                                          'why': '一句足够长的理由，占位用'}],
                                want=['EVR-PATH-BAD']))
    check('path-duplicate', *run('path-duplicate',
                                 entries=[dict(BASE_ENTRY), dict(BASE_ENTRY)],
                                 want=['EVR-PATH-BAD']))

    check('owner-missing', *run('owner-missing',
                                entries=[{'path': '.rounds/_probe.py', 'owner': '',
                                          'why': '一句足够长的理由，占位用'}],
                                want=['EVR-OWNER-MISSING']))
    check('owner-bad', *run('owner-bad',
                            entries=[{'path': '.rounds/_probe.py', 'owner': 'J99',
                                      'why': '一句足够长的理由，占位用'}],
                            want=['EVR-OWNER-BAD']))
    check('reason-short', *run('reason-short',
                               entries=[{'path': '.rounds/_probe.py', 'owner': 'B14',
                                         'why': '一句足够长的理由'}],
                               want=['EVR-REASON-SHORT']))

    check('count-drift', *run('count-drift', contract_text='被引用但不进仓的路径共 7 处。',
                              want=['EVR-COUNT-MISMATCH']))
    check('count-missing', *run('count-missing', contract_text='这句话没有出现在契约里。',
                                want=['EVR-COUNT-MISMATCH']))
    check('count-ambiguous', *run('count-ambiguous',
                                  contract_text=BASE_CONTRACT + BASE_CONTRACT,
                                  want=['EVR-COUNT-MISMATCH']))

    check('guard-empty-corpus', *run('guard-empty-corpus', corpus_size=0,
                                     want=['EVR-EMPTY-CORPUS'],
                                     forbid=['EVR-UNREACHABLE']))
    check('guard-empty-refs', *run('guard-empty-refs', refs={},
                                   want=['EVR-EMPTY-REFS'],
                                   forbid=['EVR-UNREACHABLE']))

    entries, problems = parse_registry(None)
    check('registry-file-missing', problems and problems[0][0] == 'EVR-REGISTRY-MISSING'
          and entries == [], 'problems=%s' % ([c for c, _ in problems],))
    entries, problems = parse_registry('{ 不是 JSON')
    check('registry-bad-json', problems and problems[0][0] == 'EVR-REGISTRY-MISSING',
          'problems=%s' % ([c for c, _ in problems],))
    entries, problems = parse_registry('[]')
    check('registry-wrong-shape', problems and problems[0][0] == 'EVR-REGISTRY-MISSING',
          'problems=%s' % ([c for c, _ in problems],))
    entries, problems = parse_registry('{"_note": "x", "entries": [{"path": "a/b.py"}]}')
    check('registry-minimal-row', not problems and entries == [{'path': 'a/b.py'}],
          'entries=%r problems=%r' % (entries, problems))
    check('registry-problems-plumbed', *run('registry-problems-plumbed',
                                            registry_problems=[('EVR-REGISTRY-MISSING', 'x')],
                                            want=['EVR-REGISTRY-MISSING']))

    check('shape-judged-rounds', *shape_case('.rounds/_i2b1_probe.py', True))
    check('shape-judged-doc', *shape_case('docs/迭代计划.md', True))
    check('shape-judged-source', *shape_case('quanauto/datasources.py', True))
    check('shape-skip-glob', *shape_case('assets/index-*.js', False))
    check('shape-skip-relative', *shape_case('./x.py', False))
    check('shape-skip-absolute', *shape_case('/abs/x.py', False))
    check('shape-skip-dir', *shape_case('docs/', False))
    check('shape-skip-url', *shape_case('https://example.invalid/a.py', False))
    check('shape-skip-rev', *shape_case('HEAD:quanauto/enums.py', False))
    check('shape-skip-envdir', *shape_case('.venv/Lib/site-packages/x.py', False))
    check('shape-skip-node', *shape_case('node_modules/pkg/index.js', False))
    check('shape-skip-noext', *shape_case('us/tech/2026/report', False))
    check('shape-skip-chinese', *shape_case('第十二行/那一行', False))

    ok, detail = sandbox_git_case()
    check('sandbox-git-adapter', ok, detail)

    failed = [n for n, ok, _ in SAMPLES if not ok]
    for name, ok, detail in SAMPLES:
        if not ok:
            print('  FAILED %-28s %s' % (name, detail))
    if failed:
        print('SELFTEST FAIL -- %d sample(s), %d failed' % (len(SAMPLES), len(failed)))
        return 1
    print('SELFTEST OK -- %d sample(s), 0 failed' % len(SAMPLES))
    return 0


# ---------------------------------------------------------------- 入口

def main(argv):
    args = argv[1:]
    if '--selftest' in args:
        return selftest(ROOT)

    root = ROOT
    for arg in args:
        if not arg.startswith('--'):
            root = os.path.abspath(arg)
            break

    problems, notes = check_all(root)
    for code, msg in notes:
        print('NOTE [%s] %s' % (code, msg))
    for code, msg in problems:
        print('FINDING [%s] %s' % (code, msg))
    if problems:
        print('verdict: FAIL (%d issue(s))' % len(problems))
        return 1
    print('verdict: PASS (0 issue(s))')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
