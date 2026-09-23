# -*- coding: utf-8 -*-
"""契约签名门禁：契约文档里的类/方法/字段签名 与 tools/contract-signature-manifest.json
与 quanauto/ 的真实实现，三方一致。

===== 为什么需要这个门禁 =====

I1 的第一版实现里有三处「契约说 A、代码写 B」的问题（滑点单位被当成比例、`get_account`
把冻结资金算两遍、`_match` 先改状态后校验）。它们**不是**靠读契约发现的，是靠跑出
「回撤 49.9% 而总收益 -3.5%」这种自相矛盾的数字才暴露出来的。也就是说：只要没有一条
自动判据盯着「契约怎么写、代码怎么长」，偏差就只能靠巧合被抓到。

签名是最容易悄悄漂移、又最容易被机器比对的表层：改一个参数名、少一个参数、把契约里的
方法删掉，调用方就会静默地把参数错位传进去 —— Python 不会为此报错。

===== 它挡的是哪三种假绿 =====

1) **清单抄错了**。如果清单是从实现里生成的（而不是从契约里抄的），那么契约和实现同时
   写错一个参数名时，两边一致、门禁全绿。所以 S1 专门核对「清单里的契约侧签名必须能在
   契约文档里逐字找到」—— 清单是抄写，不是发明。
2) **只比名字**。名字都在、参数列表全变，是最典型的漂移。所以两边都比**参数名列表**，
   而不是方法名集合。
3) **空转**。文档正则失配、`ast` 一个类都没解析出来、清单 `classes` 是空的 —— 这三种
   情况下所有探测器都在空转却会打印「0 问题 PASS」。S6 对每一类提取结果都要求非空。

===== 边界（这个门禁不证明什么）=====

- **不比返回类型**。契约里大量返回类型引用的是库里并不存在的类型（`Dict[str, Position]`、
  `Account`、`MarketState`），逐字比会制造大量噪声，比出来的红也不是真缺陷。返回类型
  漂移目前只由 core-contract-refs（引用是否定义）与人工评审覆盖，这是**已知缺口**。
- **不比私有成员**。`_match` / `_reject` / `__init__` 属于实现细节，改名不该逼人改契约清单；
  S4 只对**公有**成员做「多出来的必须在 extras 里登记」的反向检查。
- **只证明三方一致，不证明三方都对**。契约自己也可能写错（例：契约的类定义写
  `CsvDataFeed(csv_path)`，同一份文档 20xx 行的用例却写 `CsvDataFeed(file_path=, date_col=,
  symbol_col=)`；我们跟随类定义，那条差异登记在契约附录 E1）。签名一致 = 没有新的、
  未登记的偏差，仅此而已。
- **不覆盖 impl_only 类的「新增」**。`BacktestEngine` 等没有契约类块的类，只查「清单里记
  的成员还在不在、参数还对不对」；新增公有方法不判红（它们本来就没有契约侧可比，要求
  每次新增都改清单只会训练人无脑改清单）。
"""

import ast
import json
import os
import re
import shutil
import sys
import tempfile

DETECTORS = (
    ("S1-CONTRACT-TRANSCRIPT", "清单里的契约侧签名能在契约文档里逐字找到"),
    ("S2-IMPL-MATCHES", "契约侧方法在实现里存在，且参数列表一致（除已登记的差异）"),
    ("S3-FIELDS-MATCH", "数据类字段名列表 契约=实现"),
    ("S4-EXTRAS-REGISTERED", "契约类里多出来的公有成员必须在 extras 里登记"),
    ("S5-DIVERGENCE-REGISTRY", "divergences 里登记的差异必须真的存在（不许留过期条目）"),
    ("S6-NON-VACUOUS", "提取结果非空：清单有类、契约有签名、实现有类（防空转假绿）"),
    ("S7-NON-NORMATIVE-REGISTRY", "非规范块的登记必须仍能逐字命中契约原文（附录 E1 的裁决不许腐化）"),
)

MANIFEST_REL = os.path.join("tools", "contract-signature-manifest.json")
EXPECTED_SCHEMA = "quanauto.contract-signature-manifest/1"
PY_BLOCK_RE = re.compile(r"```python\r?\n(.*?)```", re.S)
CLASS_RE = re.compile(r"^class\s+(\w+)", re.M)
FIELD_RE = re.compile(r"^    (\w+)\s*:\s*[^=\n]+$", re.M)
ANN_ASSIGN_RE = re.compile(r"^    (\w+)\s*:\s*[^=\n]+=\s*", re.M)


# ── 读取与规范化 ──────────────────────────────────────────────────────
def read_text(path):
    """统一按 UTF-8 + LF 读。CRLF 会让裸 \\n 的正则**静默**失配（0 提取却全绿）。"""
    with open(path, "r", encoding="utf-8-sig") as handle:
        return handle.read().replace("\r\n", "\n")


def write_text(path, text):
    """写盘一律无 BOM UTF-8 + LF：BOM 会让下一次读取的第一个 token 变成 \\ufeff。"""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def is_public(name: str) -> bool:
    return not name.startswith("_")


# ── 契约侧提取 ────────────────────────────────────────────────────────
def closing_paren(text, start):
    """从 text[start] == '(' 起找配平的 ')'。参数可能跨多行，必须配平后再切。"""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def params_from_def(chunk):
    """'name(self, a: int = 1, *args, **kw) -> X' -> ['self','a','*args','**kw']。

    只留参数名、丢掉注解与默认值：`Dict[str, Any]` 与 `Dict[str,Any]` 的空格差异
    不是漂移，逐字比会把格式差异报成缺陷。
    """
    open_at = chunk.find("(")
    if open_at < 0:
        return None
    close_at = closing_paren(chunk, open_at)
    if close_at < 0:
        return None
    names = []
    for piece in chunk[open_at + 1:close_at].split(","):
        piece = piece.strip()
        if not piece:
            continue
        if piece in ("*", "/"):
            names.append(piece)
            continue
        name = re.split(r"[:\s=]", piece, maxsplit=1)[0].strip()
        if not name:
            continue
        if piece.startswith("**"):
            name = "**" + name
        elif piece.startswith("*"):
            name = "*" + name
        names.append(name)
    return names


def extract_contract(text):
    """契约里可解析 python 块 -> {类名: {'params': {方法: [参数]}, 'fields': [字段]}}。"""
    found = {}
    for block in PY_BLOCK_RE.findall(text):
        lines = block.split("\n")
        current = None
        index = 0
        while index < len(lines):
            line = lines[index]
            match = CLASS_RE.match(line)
            if match:
                current = match.group(1)
                found.setdefault(current, {"params": {}, "fields": []})
                index += 1
                continue
            if current is not None:
                if line.startswith("    def "):
                    chunk = line
                    while closing_paren(chunk, chunk.find("(")) < 0 and index + 1 < len(lines):
                        index += 1
                        chunk = chunk + " " + lines[index].strip()
                    named = re.match(r"^    def\s+(\w+)", chunk)
                    names = params_from_def(chunk)
                    if named and names is not None:
                        found[current]["params"][named.group(1)] = names
                elif ANN_ASSIGN_RE.match(line) or FIELD_RE.match(line):
                    field = re.match(r"^    (\w+)", line)
                    if field:
                        found[current]["fields"].append(field.group(1))
                elif line.startswith("class "):
                    current = None
            index += 1
    return found


# ── 实现侧提取 ────────────────────────────────────────────────────────
def arg_names(args):
    """ast 参数 -> 名字列表，口径与 params_from_def 完全一致。"""
    names = []
    for arg in list(args.posonlyargs) + list(args.args):
        names.append(arg.arg)
    if args.vararg:
        names.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        names.append("*")
    for arg in args.kwonlyargs:
        names.append(arg.arg)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return names


def extract_impl(root, sources):
    """{类名: {'params': {方法: [参数]}, 'fields': [字段], 'source': rel}}。

    只解析清单点名的那几个文件（而不是整个包）：沙箱样本因此只须复制 1~3 个文件，
    触发测试的成本可控。代价是「新文件里的新类」看不见 —— 由 S4 的反向检查与
    core-contract-refs 各自覆盖一部分。
    """
    found = {}
    errors = []
    for rel in sorted(sources):
        path = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(path):
            continue
        try:
            tree = ast.parse(read_text(path))
        except SyntaxError as exc:
            # 语法错不能静默跳过：跳过等于把这个文件的比对变成空转，而门禁会报 PASS。
            errors.append("%s 语法错，无法比对签名：%s" % (rel, exc))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            params = {}
            fields = []
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    params[item.name] = arg_names(item.args)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.append(item.target.id)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            fields.append(target.id)
            entry = found.setdefault(node.name, {"params": {}, "fields": [], "source": rel})
            entry["params"].update(params)
            if fields and not entry["fields"]:
                entry["fields"] = fields
            entry["source"] = rel
    return found, errors


def public_members(entry):
    return {name for name in entry["params"] if is_public(name)}


# ── 探测器 ───────────────────────────────────────────────────────────
def check_shape(manifest, label):
    """S6：结构与非空守卫。提取为空时后面的探测器全在空转，必须在这里就判 FAIL。"""
    findings = []
    if not isinstance(manifest, dict):
        return [("S6-NON-VACUOUS", "%s: 清单不是 JSON 对象" % label)]
    if manifest.get("schema") != EXPECTED_SCHEMA:
        findings.append(("S6-NON-VACUOUS", "%s: schema 应为 %s，实际 %r"
                         % (label, EXPECTED_SCHEMA, manifest.get("schema"))))
    classes = manifest.get("classes")
    if not isinstance(classes, dict) or not classes:
        return findings + [("S6-NON-VACUOUS",
                            "%s: 清单里一个契约类都没有 —— 后面的比对全是空转，拒绝通过" % label)]
    if not manifest.get("contract"):
        findings.append(("S6-NON-VACUOUS", "%s: 清单没写 contract，无法定位契约文档" % label))
    total = 0
    total_fields = 0
    for name, entry in sorted(classes.items()):
        if not entry.get("source"):
            findings.append(("S6-NON-VACUOUS", "%s: 类 %s 没写 source，实现侧无法定位" % (label, name)))
        # 注意：只有**方法**没有还不足以判定空转 —— Order/Trade/Position/BacktestConfig/
        # BacktestResult 是纯数据类，本来一个方法都没有，被比的是字段名（S3）。所以空转信号
        # 取「方法也没有、字段也没有」，再加两条全局汇总（防止逐类看着都非空、整体却是 0）。
        if not (entry.get("contract_params") or entry.get("contract_fields")):
            findings.append(("S6-NON-VACUOUS",
                             "%s: 类 %s 契约侧既没有方法也没有字段 —— 没有任何可比的东西"
                             % (label, name)))
        if not (entry.get("impl_params") or entry.get("impl_fields")):
            findings.append(("S6-NON-VACUOUS",
                             "%s: 类 %s 实现侧既没有方法也没有字段 —— 没有任何可比的东西"
                             % (label, name)))
        total += len(entry.get("contract_params") or {})
        total_fields += len(entry.get("contract_fields") or [])
    if not total:
        findings.append(("S6-NON-VACUOUS",
                         "%s: 所有类的 contract_params 加起来是 0 —— S1/S2 没有比对对象" % label))
    if not total_fields:
        findings.append(("S6-NON-VACUOUS",
                         "%s: 所有类的 contract_fields 加起来是 0 —— S3 没有比对对象" % label))
    return findings


def check_transcript(manifest, contract_text, label):
    """S1：清单的契约侧签名必须能在契约文档里逐字找到（清单是抄写，不是发明）。"""
    findings = []
    live = extract_contract(contract_text)
    for name, entry in sorted((manifest.get("classes") or {}).items()):
        spec = live.get(name)
        if spec is None:
            findings.append(("S1-CONTRACT-TRANSCRIPT",
                             "%s: 契约文档里找不到类 %s（类名抄错，或契约被删了）" % (label, name)))
            continue
        for method, params in sorted((entry.get("contract_params") or {}).items()):
            if method not in spec["params"]:
                findings.append(("S1-CONTRACT-TRANSCRIPT",
                                 "%s: 契约 %s 里没有方法 %s（清单是凭空写的）" % (label, name, method)))
            elif spec["params"][method] != params:
                findings.append(("S1-CONTRACT-TRANSCRIPT",
                                 "%s: %s.%s 契约侧参数不一致 清单=%s 文档=%s"
                                 % (label, name, method, params, spec["params"][method])))
    return findings


def check_impl(manifest, impl, label):
    """S2/S3：契约侧的方法要在实现里存在且参数一致；数据类字段名列表要两边一致。"""
    findings = []
    for name, entry in sorted((manifest.get("classes") or {}).items()):
        real = impl.get(name)
        if real is None:
            findings.append(("S2-IMPL-MATCHES", "%s: 实现里找不到类 %s（被改名或删了）" % (label, name)))
            continue
        divergences = entry.get("divergences") or {}
        skipped = set(entry.get("not_implemented") or [])
        for method, params in sorted((entry.get("contract_params") or {}).items()):
            if method in skipped:
                # not_implemented 条目本身就是「契约要求、本迭代不做」的登记，
                # 但它必须是**真的**没实现 —— 实现了却登记成没做，同样是清单说谎。
                if method in real["params"]:
                    findings.append(("S2-IMPL-MATCHES",
                                     "%s: %s.%s 已登记 not_implemented，实现里却有它（清单过期）"
                                     % (label, name, method)))
                continue
            if method in divergences:
                continue
            if method not in real["params"]:
                findings.append(("S2-IMPL-MATCHES",
                                 "%s: 契约要求 %s.%s，实现里没有（也没登记 not_implemented）"
                                 % (label, name, method)))
            elif real["params"][method] != params:
                findings.append(("S2-IMPL-MATCHES",
                                 "%s: %s.%s 参数不一致 契约=%s 实现=%s"
                                 % (label, name, method, params, real["params"][method])))
        want_fields = entry.get("contract_fields")
        if want_fields:
            got_fields = sorted(real["fields"])
            if got_fields != sorted(want_fields):
                missing = sorted(set(want_fields) - set(got_fields))
                extra = sorted(set(got_fields) - set(want_fields))
                findings.append(("S3-FIELDS-MATCH",
                                 "%s: %s 字段不一致 缺=%s 多=%s" % (label, name, missing, extra)))
    return findings


def check_extras(manifest, impl, label):
    """S4：契约类里多出来的公有成员必须登记进 extras；登记了 extras 也要真存在。"""
    findings = []
    for name, entry in sorted((manifest.get("classes") or {}).items()):
        real = impl.get(name)
        if real is None:
            continue
        declared = set(entry.get("extras") or [])
        extra_now = public_members(real) - set(entry.get("contract_params") or {})
        for method in sorted(extra_now - declared):
            findings.append(("S4-EXTRAS-REGISTERED",
                             "%s: %s.%s 是契约里没有的公有成员，但没登记进 extras" % (label, name, method)))
        for method in sorted(declared - public_members(real)):
            findings.append(("S4-EXTRAS-REGISTERED",
                             "%s: %s.extras 登记了 %s，实现里没有这个成员（清单过期）"
                             % (label, name, method)))
    for name, entry in sorted((manifest.get("impl_only") or {}).items()):
        real = impl.get(name)
        if real is None:
            findings.append(("S4-EXTRAS-REGISTERED",
                             "%s: impl_only 登记了 %s，实现里没有这个类" % (label, name)))
            continue
        recorded = set((entry.get("impl_params") or {}).keys())
        for method in sorted(m for m in recorded - set(real["params"])):
            findings.append(("S4-EXTRAS-REGISTERED",
                             "%s: impl_only %s 清单记了 %s，实现里没了（删/改名）"
                             % (label, name, method)))
        for method in sorted(m for m in recorded & set(real["params"])):
            if real["params"][method] != entry["impl_params"][method]:
                findings.append(("S4-EXTRAS-REGISTERED",
                                 "%s: impl_only %s.%s 参数漂移 清单=%s 实现=%s"
                                 % (label, name, method, entry["impl_params"][method],
                                    real["params"][method])))
    return findings


def check_divergences(manifest, contract_text, impl, label):
    """S5：登记的差异必须真的存在。契约/实现已经改成一致了却还留着条目，就是过期清单。

    为什么这条值得单独一个探测器：`divergences` 是「允许契约与实现不一致」的唯一出口，
    一个不再成立的出口等于给未来的真偏差留了一道没有人会再检查的后门。
    """
    findings = []
    live = extract_contract(contract_text)
    for name, entry in sorted((manifest.get("classes") or {}).items()):
        real = impl.get(name) or {"params": {}}
        spec = live.get(name) or {"params": {}}
        for method, item in sorted((entry.get("divergences") or {}).items()):
            if not isinstance(item, dict) or "contract" not in item or "impl" not in item:
                findings.append(("S5-DIVERGENCE-REGISTRY",
                                 "%s: %s.%s 的差异条目缺 contract/impl 字段" % (label, name, method)))
                continue
            if not item.get("why"):
                findings.append(("S5-DIVERGENCE-REGISTRY",
                                 "%s: %s.%s 登记了差异却没写 why" % (label, name, method)))
            now_contract = spec["params"].get(method)
            now_impl = real["params"].get(method)
            if item["contract"] == now_contract and item["impl"] == now_impl:
                findings.append(("S5-DIVERGENCE-REGISTRY",
                                 "%s: %s.%s 登记的差异已不存在（两边现在都是 %s），删掉这条"
                                 % (label, name, method, now_contract)))
    return findings


def check_non_normative(manifest, contract_text, label):
    """S7：非规范块的登记必须仍能逐字命中契约原文。

    契约附录 E1 判了「§2.9 的示例不是规范」，并写明这份裁决的机器可读副本就在清单的
    `non_normative_blocks` 里。问题在于：那条裁决**指向的是原文里的具体几行**。原文一旦
    被重排、被删、被改，登记就变成一段对不上任何东西的文字 —— 看上去还在，实际已经
    不再覆盖任何内容。这里就是钉这一条：每个登记项都要能在原文里找到，找不到就红。
    """
    findings = []
    blocks = manifest.get("non_normative_blocks")
    if not isinstance(blocks, list) or not blocks:
        return [("S7-NON-NORMATIVE-REGISTRY",
                 "%s: non_normative_blocks 为空 —— 附录 E1 的裁决没有任何机器可读登记" % label)]
    for index, item in enumerate(blocks):
        if not isinstance(item, dict) or not item.get("text"):
            findings.append(("S7-NON-NORMATIVE-REGISTRY",
                             "%s: non_normative_blocks[%d] 没有 text" % (label, index)))
            continue
        if not item.get("why"):
            findings.append(("S7-NON-NORMATIVE-REGISTRY",
                             "%s: non_normative_blocks[%d] 没写 why" % (label, index)))
        if item["text"] not in contract_text:
            findings.append(("S7-NON-NORMATIVE-REGISTRY",
                             "%s: non_normative_blocks[%d] 的 text 在契约原文里找不到（登记已过期）：%r"
                             % (label, index, item["text"][:60])))
    return findings


def verify(root):
    """三方比对。清单缺失/契约缺失都判 FAIL，绝不静默跳过。"""
    manifest_path = os.path.join(root, MANIFEST_REL.replace("/", os.sep))
    if not os.path.exists(manifest_path):
        return "root", [("S6-NON-VACUOUS", "清单不存在：%s" % MANIFEST_REL)]
    try:
        manifest = json.loads(read_text(manifest_path))
    except ValueError as exc:
        return "root", [("S6-NON-VACUOUS", "清单不是合法 JSON：%s" % exc)]
    findings = check_shape(manifest, "root")
    contract_rel = manifest.get("contract") or ""
    contract_path = os.path.join(root, contract_rel.replace("/", os.sep))
    if not contract_rel or not os.path.exists(contract_path):
        return "root", findings + [("S6-NON-VACUOUS",
                                    "契约文档不存在：%r（清单里的 contract 路径写错？）" % contract_rel)]
    contract_text = read_text(contract_path)
    sources = set()
    for group in ("classes", "impl_only"):
        for entry in (manifest.get(group) or {}).values():
            if entry.get("source"):
                sources.add(entry["source"])
    impl, impl_errors = extract_impl(root, sources)
    for message in impl_errors:
        findings.append(("S6-NON-VACUOUS", message))
    if not impl:
        findings.append(("S6-NON-VACUOUS",
                         "实现侧一个类都没解析出来（source=%s）—— 后面的比对全是空转" % sorted(sources)))
        return contract_rel, findings
    findings += check_transcript(manifest, contract_text, "root")
    findings += check_impl(manifest, impl, "root")
    findings += check_extras(manifest, impl, "root")
    findings += check_divergences(manifest, contract_text, impl, "root")
    findings += check_non_normative(manifest, contract_text, "root")
    return contract_rel, findings


# ── 触发测试 ─────────────────────────────────────────────────────────
def _sandbox(tmp, root, manifest):
    """沙箱里只放清单 + 契约文档 + 清单点名的实现文件。"""
    box = os.path.join(tmp, "repo")
    write_text(os.path.join(box, MANIFEST_REL.replace("/", os.sep)),
               json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    rel = manifest["contract"].replace("/", os.sep)
    target = os.path.join(box, rel)
    if not os.path.isdir(os.path.dirname(target)):
        os.makedirs(os.path.dirname(target))
    shutil.copyfile(os.path.join(root, rel), target)
    sources = set()
    for group in ("classes", "impl_only"):
        for entry in (manifest.get(group) or {}).values():
            if entry.get("source"):
                sources.add(entry["source"])
    for rel in sorted(sources):
        target = os.path.join(box, rel.replace("/", os.sep))
        if not os.path.isdir(os.path.dirname(target)):
            os.makedirs(os.path.dirname(target))
        shutil.copyfile(os.path.join(root, rel.replace("/", os.sep)), target)
    return box


def _load_box(box):
    return json.loads(read_text(os.path.join(box, MANIFEST_REL.replace("/", os.sep))))


def _needle_for(method, params):
    """定位一个「唯一」的 def 行：`def m(self, first` —— 只用 `def m(` 会命中别的类的同名方法
    （`__init__` 在契约里有好几个），变异就会打到错的地方，而样本会静默地什么都没改。
    """
    assert len(params) >= 2, "需要至少一个真实参数才能构造唯一 needle：%s%s" % (method, params)
    return "def %s(%s, %s" % (method, params[0], params[1])


def _save_box(box, manifest):
    write_text(os.path.join(box, MANIFEST_REL.replace("/", os.sep)),
               json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _codes(findings):
    return {item[0] for item in findings}


SAMPLES_SEEN = []


def expect(tag, got, want):
    SAMPLES_SEEN.append(tag)
    want_set = set(want)
    got_set = _codes(got)
    if got_set == want_set:
        print("  OK   %-34s codes=%s" % (tag, sorted(got_set)))
        return True
    print("  FAIL %-34s want=%s got=%s" % (tag, sorted(want_set), sorted(got_set)))
    for code, message in got[:6]:
        print("         %s: %s" % (code, message))
    return False


def selftest(root):
    print("[selftest] 触发测试：每个样本都要看见它该报的 FINDING")
    ok = True
    manifest_path = os.path.join(root, MANIFEST_REL.replace("/", os.sep))
    if not os.path.exists(manifest_path):
        print("  FAIL 清单不存在，自测无法进行")
        return 2
    base = json.loads(read_text(manifest_path))
    tmp = tempfile.mkdtemp(prefix="quanauto-sig-")

    # 干净样本：真实产物原封不动 ⇒ 期望 0 报错（防误报，也证明探测器不是「总报错」）。
    # 沙箱里的清单/契约/实现都是真实文件的副本，所以它必须和真实仓库同样全绿。
    box = _sandbox(tmp, root, base)
    ok &= expect("CLEAN-real-artifacts", verify(box)[1], [])

    # 后面几个样本都要改「同一个类」：选第一个契约侧与实现侧都非空的类
    # （排在最前的 BacktestConfig 只有字段、没有方法，直接取 sorted()[0] 会 IndexError）。
    impl_class = None
    for name, entry in sorted(base["classes"].items()):
        if entry["contract_params"] and entry["impl_params"]:
            impl_class = name
            break
    assert impl_class, "清单里没有一个契约侧与实现侧都非空的类"
    impl_method = sorted(base["classes"][impl_class]["contract_params"])[0]
    params = base["classes"][impl_class]["contract_params"][impl_method]
    source_rel = base["classes"][impl_class]["source"]
    source_path = os.path.join(box, source_rel.replace("/", os.sep))
    contract_rel = base["contract"]
    contract_path = os.path.join(box, contract_rel.replace("/", os.sep))

    # 2) 清单里的契约侧签名被改动 ⇒ S1（证明清单是「抄来的」而不是「从实现生成的」）。
    #    被动的清单同时和文档、和实现都对不上，所以 S2 也会同时报 —— 这不是误报：
    #    两份不一致的登记就是两条独立的坏消息。判据因此按「两项都出现」钉住。
    mutated = json.loads(json.dumps(base))
    mutated["classes"][impl_class]["contract_params"][impl_method] = ["self", "invented_arg"]
    assert mutated["classes"][impl_class]["contract_params"][impl_method] != params, "变异没生效"
    _save_box(box, mutated)
    ok &= expect("MUT-manifest-contract-param", verify(box)[1],
                 ["S1-CONTRACT-TRANSCRIPT", "S2-IMPL-MATCHES"])
    _save_box(box, base)

    # 3) 契约文档里的签名被改动 ⇒ 只有 S1（有人改了文档却没改清单）。
    #    这条和上一条是对照：证明 S1 是**独立**成立的，不是靠 S2 捎带出来的。
    original_doc = read_text(contract_path)
    needle = _needle_for(impl_method, params)
    assert original_doc.count(needle) == 1, "样本构造失效：契约里 %r 命中 %d 次" % (needle, original_doc.count(needle))
    write_text(contract_path, original_doc.replace(needle, needle + "_x", 1))
    ok &= expect("MUT-contract-doc-param", verify(box)[1], ["S1-CONTRACT-TRANSCRIPT"])
    write_text(contract_path, original_doc)

    # 4) 实现的参数列表被改动 ⇒ S2（DoD 点名的那一类：签名与契约不符）
    original_src = read_text(source_path)
    assert original_src.count(needle) == 1, "样本构造失效：实现里 %r 命中 %d 次" % (needle, original_src.count(needle))
    write_text(source_path, original_src.replace(needle, needle.replace("(", "(bogus_extra, "), 1))
    ok &= expect("MUT-impl-param", verify(box)[1], ["S2-IMPL-MATCHES"])
    write_text(source_path, original_src)

    # 5) 实现里把契约方法改了名 ⇒ S2
    write_text(source_path, original_src.replace(needle, needle + "_renamed", 1))
    ok &= expect("MUT-impl-method-renamed", verify(box)[1], ["S2-IMPL-MATCHES"])
    write_text(source_path, original_src)

    # 6) 数据类少一个字段 ⇒ S3
    field_class = None
    for name, entry in sorted(base["classes"].items()):
        if entry.get("contract_fields"):
            field_class = name
            break
    assert field_class, "清单里没有一个数据类带字段"
    field_rel = base["classes"][field_class]["source"]
    field_path = os.path.join(box, field_rel.replace("/", os.sep))
    field_src = read_text(field_path)
    field = base["classes"][field_class]["contract_fields"][0]
    assert re.search(r"^    %s\s*:" % field, field_src, re.M), "样本构造失效：找不到字段 %s" % field
    write_text(field_path, re.sub(r"^    %s\s*:" % field, "    %s_kept:" % field, field_src, count=1, flags=re.M))
    ok &= expect("MUT-impl-field-renamed", verify(box)[1], ["S3-FIELDS-MATCH"])
    write_text(field_path, field_src)

    # 7) 实现里多出一个公有成员却没登记 ⇒ S4。插在类里第一个 def 之前（插到文件尾会在
    #    最后一个类之后变成缩进错，反而考不出来 S4）。
    write_text(source_path,
               original_src.replace(needle, "def surprise_new_api(self):\n        return None\n\n    " + needle, 1))
    ok &= expect("MUT-unregistered-extra", verify(box)[1], ["S4-EXTRAS-REGISTERED"])
    write_text(source_path, original_src)

    # 8) impl_only 类：清单记了一个实现里已经没了的成员 ⇒ S4
    mutated = json.loads(json.dumps(base))
    only = sorted(mutated["impl_only"])[0]
    mutated["impl_only"][only]["impl_params"]["gone_method"] = ["self"]
    _save_box(box, mutated)
    ok &= expect("MUT-impl-only-member-gone", verify(box)[1], ["S4-EXTRAS-REGISTERED"])
    _save_box(box, base)

    # 10) 过期差异条目：登记一条「差异」但两边其实一样 ⇒ S5
    mutated = json.loads(json.dumps(base))
    same = mutated["classes"][impl_class]["contract_params"][impl_method]
    mutated["classes"][impl_class]["divergences"] = {
        impl_method: {"contract": list(same), "impl": list(same), "why": "过期条目样本"}}
    _save_box(box, mutated)
    ok &= expect("MUT-stale-divergence", verify(box)[1], ["S5-DIVERGENCE-REGISTRY"])
    _save_box(box, base)

    # 11) 清单被清空 ⇒ S6（防空转：提取为空时后面的探测器全在空转却会报 PASS）
    mutated = json.loads(json.dumps(base))
    mutated["classes"] = {}
    _save_box(box, mutated)
    ok &= expect("MUT-empty-classes", verify(box)[1], ["S6-NON-VACUOUS"])
    _save_box(box, base)

    # 12) 某个类的契约侧方法和字段全被清空 ⇒ S6。挑纯数据类来改：它本来就没有方法，
    #     清空后实现侧也不会多出「未登记的公有成员」，样本只考 S6 一项。
    data_class = None
    for name, entry in sorted(base["classes"].items()):
        if not entry.get("contract_params") and entry.get("contract_fields"):
            data_class = name
            break
    assert data_class, "样本构造失效：清单里找不到「没有方法、只有字段」的纯数据类"
    mutated = json.loads(json.dumps(base))
    mutated["classes"][data_class]["contract_params"] = {}
    mutated["classes"][data_class]["contract_fields"] = None
    assert not mutated["classes"][data_class]["contract_params"], "变异没生效"
    assert mutated["classes"][data_class]["contract_fields"] is None, "变异没生效"
    _save_box(box, mutated)
    ok &= expect("MUT-class-contract-side-empty", verify(box)[1], ["S6-NON-VACUOUS"])
    _save_box(box, base)

    # 13) 非规范块登记改成一个契约里根本没有的字符串 ⇒ S7（证明 S7 真的在查原文，
    #     不是「有这条登记就放行」）
    mutated = json.loads(json.dumps(base))
    assert mutated["non_normative_blocks"], "样本构造失效：清单里没有非规范块登记"
    mutated["non_normative_blocks"][0]["text"] = "这行在契约原文里不存在（样本）"
    _save_box(box, mutated)
    ok &= expect("MUT-non-normative-text-gone", verify(box)[1], ["S7-NON-NORMATIVE-REGISTRY"])
    _save_box(box, base)

    # 14) 非规范块登记本身为空 ⇒ 也是 S7（附录 E1 承诺了这份登记，空清单等于承诺落空）
    mutated = json.loads(json.dumps(base))
    mutated["non_normative_blocks"] = []
    _save_box(box, mutated)
    ok &= expect("MUT-non-normative-empty", verify(box)[1], ["S7-NON-NORMATIVE-REGISTRY"])
    _save_box(box, base)

    # 15) 清单缺失 ⇒ S6
    os.remove(os.path.join(box, MANIFEST_REL.replace("/", os.sep)))
    ok &= expect("MUT-missing-manifest", verify(box)[1], ["S6-NON-VACUOUS"])

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 2


# ── 主流程 ───────────────────────────────────────────────────────────
def main(argv):
    # 位置参数只留非选项：`--selftest` 时也要知道仓库根（沙箱要从真实仓库复制文件）。
    positional = [a for a in argv[1:] if not a.startswith("--")]
    default_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    root = os.path.abspath(positional[0]) if positional else default_root
    print("repo: %s" % root)
    print("detectors=%d" % len(DETECTORS))
    for code, what in DETECTORS:
        print("  - %s: %s" % (code, what))
    if "--selftest" in argv:
        # 自测与真实仓库那一遍分开：两个信号互不借用对方的绿。
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
    label, findings = verify(root)
    for code, message in findings:
        print("FINDING [%s] %s" % (code, message))
    print("contract=%s issues=%d" % (label, len(findings)))
    if findings:
        print("verdict: FAIL (%d issue(s))" % len(findings))
        return 1
    print("verdict: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
