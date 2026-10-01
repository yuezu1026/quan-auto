# -*- coding: utf-8 -*-
"""Verify that every text-bearing paragraph/cell of a .docx survives in the .md."""
import sys, os, re, glob, shutil, tempfile, zipfile, html

T_RE = re.compile(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>', re.S)
TOKEN_RE = re.compile(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:br(?:\s[^>]*)?/>|<w:cr/>', re.S)
BLOCK_RE = re.compile(r'<w:tbl>.*?</w:tbl>|<w:p(?:\s[^>]*)?>.*?</w:p>|<w:p(?:\s[^>]*)?/>', re.S)
STRIP = re.compile(r'[\s`*|#>\-]+')

# Declared, intentional transformation: the converter re-lays out run-on
# "错误码: X 级别: Y 描述: Z 建议措施: W" paragraphs as a markdown table, which
# moves the four labels into the table header.  Such a paragraph cannot be found
# verbatim, so its FIELDS are checked individually instead.
LABELED = re.compile(
    r'^错误码[:：](\S+)\s*级别[:：](\S+)\s*描述[:：](.*?)\s*建议措施[:：](.*)$')
RELOCATED = []

# Declared, intentional POST-CONVERSION EDIT (different from LABELED above: that one
# is a layout artefact of the converter, this one is a real content change made after
# the fact because the source document contradicted a decision taken later).
#
# A plain "ignore these old paragraphs" allowlist would be a hole -- it would stay
# green even if the replacement text never made it into the file, i.e. exactly the
# case where the content was dropped rather than replaced. So a supersession is only
# honoured when the old paragraph is ABSENT *and* the new one is PRESENT.
#   (old text as it appears in the .docx, new text as it appears in the .md, why)
SUPERSEDED = [
    ('SpringBoot+MySQL+Redis+Kafka/gRPC',
     'Spring Boot + PostgreSQL + Redis + Kafka/gRPC',
     '数据库由 MySQL 改为 PostgreSQL（2026-09-23 选型决策）'),
    ('关系型数据库（MySQL/PostgreSQL）：存储财务数据、股票基本信息',
     '关系型数据库（**PostgreSQL 14+**，已确定）：存储财务数据、股票基本信息',
     '数据库由 MySQL 改为 PostgreSQL（2026-09-23 选型决策）'),
]
SUPERSEDED_OK = []

# Declared, intentional POST-CONVERSION ADDITIONS: text that exists ONLY in the .md and
# never in the .docx (e.g. the "以契约为准" note under 模块4, and 附录A that spells the
# Strategy signatures out).  A re-conversion regenerates the .md from scratch, so these
# vanish -- and the docx-side paragraph diff CANNOT see it, because every docx paragraph
# is still present.  The report then looks *cleaner* than a real pass (missing=0), which
# is the exact shape of a silent content loss.  So they are asserted PRESENT, per md file.
ADDENDA = {
    '智能量化交易平台.md': [
        # 附录A 里写的规范签名（上文示意代码写的是 MarketData/List[Signal] 与 Dict[str, float]）
        'class Strategy(ABC)',
        'on_data(self, data: MarketDataBundle) -> List[TradingSignal]',
        'get_target_positions(self) -> Dict[str, PositionTarget]',
        # 附录B（平台层技术选型补充登记，2026-09-29）：这一块**只活在 md 里**，docx 里没有，
        # 所以上面那条「docx 每段都在」的判据对它完全无感 —— 重新转换一次它就被抹掉，
        # 而 missing 仍是 0（报告看起来比真通过还干净）。锚点同样选**内容**而不是标题：
        # B.1 那张表里「ORM 层」那一行、那句效力边界（「选型登记 ≠ 已交付的事实」）、
        # 以及 B.3 / B.4 各一句论断 —— 「只留标题不留正文」的重生成正是抹掉这几句的形态。
        # ⚠️ 每条锚必须是**文件里的字面文本**且两两不嵌套：自测会逐条 replace 掉它们，
        # 并断言 missing 恰好等于锚数（嵌套、或 norm() 后与别的锚重复，会让计数少一个
        # ⇒ HARNESS 级 FAIL，而报告里看起来只像「少抓了一条」）。
        '## 附录B：平台层技术选型的补充登记（2026-09-29 追加）',
        '本附录是**选型登记**，不是**已交付的事实**，也不是契约。',
        '| ORM / 持久层 | 未写 | MyBatis | **新增登记** |',
        '真要有一个 React 版绩效看板，那是平台层那一轮的新增项',
        '现有自建门禁都是解析 Python 或 SQL 文本的静态检查器',
        # 附录C（回测绩效看板的平台层读取侧 P0 切片，2026-09-29）：同附录B，**只活在 md 里**。
        # 锚点仍然选**内容**而不是标题 —— 只留标题不留正文的重生成，抹掉的正是这几句论断：
        # ① C.2 那句「报告不落库」是 B.3 要求的那个答案本身；
        # ② C.4 那句「只在服务端算一次」是本层零统计口径的判据来源；
        # ③ C.5 的两条（14 条规格表是手抄副本 / 对拍脚本不是门禁）是本层**仅有的诚实边界**，
        #    抹掉它，附录C 就变成一句「已交付」的空话；
        # ④ C.6 那句「dashboard.py 一行未改」是 B.3 合规的唯一依据。
        '## 附录C：回测绩效看板的平台层读取侧（P0 切片）登记（2026-09-29 追加）',
        '本附录是**读取侧切片**的登记，不是契约，也不改变任何迭代的 DoD。',
        '本切片**不新增任何数据库表**，因此也没有「落库 schema」这一层需要裁决。',
        '量纲换算与定点小数**只在服务端做一次**，前端只负责把 `text` 原样印出来。',
        'Java 侧那张 14 条规格表是**手抄的副本**',
        '**`quanauto/dashboard.py` 未被改动一行**',
        # C.7 是**订正块**（本仓库的既成写法：不改上文，追加一条更正）。同样拿内容锚：
        # 它唯一的价值就是那句「附录B 的绝对化措辞已不成立」，抹掉它 = 那句假话又变回现状。
        '附录B 里「更不表示本仓库已有 Java 或前端代码」这句**已不成立**',
        # C.8 是 C.5 的订正块（2026-09-30 追加）：新门禁 platform-spec-parity 落地后，
        # C.5 第 2 条那句「没有任何门禁比对两者」变假、第 1 条那个写死的门禁计数过期。
        # 抹掉这一块 = 两句话又各自变回「现状」，而 C.5 原文（不许改）仍在原地作证。
        # 三条锚各占一行、互不嵌套：① 这是订正块本身（标题即断言），
        # ② 这是新门禁的名字与载体（抹掉它，读者不知道该找哪个门禁），
        # ③ 这是本块**明确不主张**的那半句（最容易被顺手删掉、也最该留住）。
        '### C.8 对 C.5 第 1、2 条的订正：两侧规格表**已经有门禁比对了**（追加，2026-09-30）',
        '新增门禁 `platform-spec-parity`（`tools/verify_platform_specs.py`，tier-A，已接进',
        '门禁**当场红**，不再是 C.5 第 2 条描述的那种「屏幕静默地与 I4 命令行看板不一致」。',
        # C.9 是 C.5 第 1 条**运行侧**那半句的订正块（2026-09-30 追加）：新门禁
        # platform-runtime 落地后，「`mvn test`、`npm run build` …… 没有任何门禁盯着」
        # 这句里的一半变假。抹掉本块 = 那句话又变回「现状」，而 C.5 原文（不许改）
        # 仍在原地作证 —— 正是单向判据那种「报告比真通过还干净」的隐身方式。
        # 四条锚各占一行、互不嵌套：① 订正块本身（标题即断言）；
        # ② 门禁的名字与载体（抹掉它，读者不知道该找哪个门禁）；
        # ③ 本块**明确不主张**的那半句（最容易被顺手删掉、也最该留住：它是
        #    「别把这条绿读成页面受控」的唯一依据）；④ 退出码 3 的口径
        #    （抹掉它，下一个人会把 CI 上那条 SKIPPED 当成 harness 的 bug 去「修」）。
        '### C.9 对 C.5 第 1 条的运行侧订正：`mvn test` / `npm run build` 也已经有门禁了（追加，2026-09-30）',
        '新增门禁 `platform-runtime`（`tools/verify_platform_build.py`，tier-A，已接进',
        '**「页面本身」那半句仍然成立**，本门禁没有解除它。',
        '它探不到工具链时退出码 3、打印 `verdict: SKIPPED`，而 harness 把它记成「不算绿」。',
        # C.10 是**声明兑现**的记录块（2026-09-30 追加）：ApiExceptionHandler 的类注释写着
        # 「不回内部路径」，而实现把带本机绝对路径的 `getMessage()` 直接放进了响应体。
        # 抹掉本块 = 那句声明又变回「无凭无据的话」，而**新的计数（Java 侧 @Test 声明数 15）**
        # 会只剩在 Java 源码里 —— 本文件是 .docx 派生、重转换即抹，正是单向判据那种隐身。
        # 四条锚各占一行、互不嵌套：① 标题即断言（发现的是声明与实现不一致，不是笔误）；
        # ② 修法本身（两条消息、两个去处）；③ 常驻牙在哪（新测试类 + 那条门禁真的跑 mvn test）；
        # ④ 诚实登记的那处缺口（`ids()` 没有覆盖）—— 最容易被顺手删掉的一条。
        '### C.10 错误响应体里不许出现本机路径：声明兑现 + 常驻测试钉住（追加，2026-09-30）',
        '- `getMessage()`（带路径）只进服务端日志；`clientMessage()`（不带路径）才进响应体。',
        '- **这一层是常驻牙**：新增的 `platform/api/src/test/java/com/quanauto/dashboard/ApiExceptionHandlerTest.java`',
        '- **仍然没覆盖的那一处（诚实登记）**：`ReportCatalog.ids()` 的「报告目录读取失败」走的是**同一个**',
        # C.11 是 C.5 第 3 条（「两侧展示文本一致只有手动证据」）的订正块（2026-10-01 追加）。
        # 它也是一个**被实测逼出来**的块：差分实测出 6887 / 73929（≈9.3%）的分歧，而当时两侧
        # 已有的用例全绿。抹掉本块 = C.5 第 3 条那句「只有手动证据」又变回「现状」，
        # 而 C.5 原文（不许改）仍在原地作证 —— 正是单向判据那种「报告比真通过还干净」的隐身方式。
        # 五条锚各占一行、互不嵌套：① 标题即断言（订正的是「有没有常驻证据」）；
        # ② 门禁的名字与载体（抹掉它，读者不知道该找哪个门禁）；③ 那次实测的量级与两个数
        # （抹掉它，本块就变成一句没有依据的「已经修好了」）；④ 本门禁**没有**替代那个手动脚本
        # （最容易被顺手删掉、也最该留住：它是「脚本仍然不是门禁」的唯一依据）；
        # ⑤ 本块**明确不主张**的那半句（「页面本身」仍然零覆盖）。
        '### C.11 对 C.5 第 3 条的订正：两侧**展示文本**（显示规则）也已经有门禁了（追加，2026-10-01）',
        '2. **新增门禁 `platform-text-parity`（`tools/verify_platform_text_parity.py`，tier-A，已接进',
        '   量纲 × 小数位 × 值域铺开共 **73929** 组输入，其中 **6887 组（≈9.3%）两侧打印出不同的串**。',
        '6. **C.5 第 3 条那半句仍然成立，本门禁没有替代它** —— 它说那个对拍脚本「需要一个活着的 JVM 进程，',
        '8. **本块明确不主张的那半句**：门禁**不打开页面** ⇒ **「页面本身」（渲染 / 布局 / 视觉）仍然零覆盖**；',
        # C.12 是**页面这一层**的追加登记（2026-10-01）：新门禁 platform-web-parity 落地后，
        # 「页面这一层一个字都没被机器看过」这半句变假（字段名 / 格式化落点 / 手抄规格表 /
        # 页脚自报清单四件事现在有判据），而 C.5 第 4 条那句**逐字仍然成立**（渲染 / 布局 / 视觉）。
        # 抹掉本块 = 前一句又变回「现状」，后一句则会被读成「整页都没判据」，两个方向都错。
        # 六条锚各占一行、互不嵌套：① 标题即断言（订正的是「有没有被机器看过」这个范围）；
        # ② 门禁的名字与载体（抹掉它，读者不知道该找哪个门禁）；③ 那一族漂移里**最安静**的一种
        # （Java 改名而三层构建全绿、页面只是空一格）；④ 页脚自报清单此前零兜底的理由
        # （层级 + 扫描面），抹掉它就变成一句「顺手加的一条检查」；
        # ⑤ 本块**明确不主张**的那半句（最容易被顺手删掉、也最该留住）；
        # ⑥ 修法方向（不许削参照物）—— 与前三个兄弟块同款，抹掉它就只剩一句「已经绿了」。
        '### C.12 页面这一层**静态可判**的那四件事也有门禁了 —— 但「渲染 / 布局 / 视觉」仍然零覆盖（追加，2026-10-01）',
        '1. **新增门禁 `platform-web-parity`（`tools/verify_platform_web.py`，tier-A，已接进 `tools/run_all_gates.py`',
        '结果只是页面上那一格**空着**。',
        '而注册表里已经是四条，**不会有任何东西变红**。',
        '**这一条仍然零覆盖，本轮没有关掉它**，只是把「页面」这个词里**静态可判**的那四部分单独拎出来给了牙。',
        '**不许**为了让这条门禁变绿去削',
        # C.13 是 C.5 第 5 条（「页面渲染 / 布局 / 视觉」仍然零覆盖）的订正块（2026-10-01 追加）。
        # 它关掉的是那三格里的**一格**（渲染），所以本块的价值有一半在**它不主张什么**：
        # 抹掉本块 = 那句话又变回「整页都没判据」，而**布局 / 视觉仍然零覆盖**这个边界
        # 也会一起消失 ⇒ 下一个人会把 jsdom 里的渲染用例读成「页面受控」。
        # 六条锚各占一行、互不嵌套：① 标题即断言（订正的是那三格里的一格）；
        # ② 判据挂在哪一步 + 顺序本身是判据（抹掉它，读者不知道这条绿从哪来，
        #    也可能把 `npm test` 挪到构建之后而以为无碍）；
        # ③ 为什么**不写**几何断言（jsdom 无布局引擎 ⇒ 几何断言恒真 = 假绿）——
        #    最容易被顺手「补上」的一条，补上去就是假绿；
        # ④ 夹具是派生文件 ⇒ 两个方向都要判（单向判据的隐身形状）；
        # ⑤ 那次产物侧变异的实测数字（抹掉它，本块就变成一句没有依据的「已经验过了」）；
        # ⑥ 修法方向（不许削参照物，与 C.8~C.12 同款）。
        '### C.13 对 C.5 第 5 条的订正：页面**渲染**也已经有判据了 —— 零覆盖只剩「布局 / 视觉」那一格（追加，2026-10-01）',
        '顺序与步骤集合本身就是判据：它必须在构建之前跑',
        '那是**假绿**里最坏的一种',
        '键集合与 Java 记录的**组件集合双向对拍**',
        '(vitest: passed=10 failed=3 skipped=0 total=13)',
        '本轮**只关掉其中一格**：**页面渲染**',
    ],
    # 契约文档同样由 .docx 派生，所以 I1 的裁决也只能追加。追加块丢了 = 裁决没了，
    # 机器可读的那一份（tools/contract-signature-manifest.json）就变成孤儿，所以这里断言存在。
    '智能量化交易平台-核心模块接口契约文档.md': [
        'E. I1 规范性与偏差登记',
        'tools/contract-signature-manifest.json',
        'MA_Cross_Strategy(short_window: int, long_window: int)',
        # E5~E8：追加块丢了 = 裁决没了。这几个编号本身也是锚（下面每一条都写在自己的
        # 段落里），删掉一段就等于删掉一个「已登记的偏差」。
        # ⚠️ 必须写成**文件里的字面文本**（含 `**` 与反引号）。原先登记的是「裸文字」
        # （`E5. Strategy.on_data ...`），它只靠 norm() 去标记才匹配得上 —— 于是自测里
        # `str.replace` 删不动它，那个样本**从来没有真的测过这四条锚**，而报告里看不出来。
        # 现在 selftest 会断言每条锚都是字面文本（删不动就是 HARNESS 级 FAIL）。
        '**E5. `Strategy.on_data` 的纯度偏差：`MA_Cross_Strategy` 持有有界状态。**',
        '**E6. `SimulatedBroker` 与 `BacktestEngine` 的三个附加成员。**',
        '**E7. `volume_impact_factor` 按成交额比例近似。**',
        '**E8. `BacktestResult` 不生成 ATR 报告与基准对比报告。**',
        # F1~F6（I4 绩效看板）：同 E 节。锚点以**正文内容**为主（F1 那句口径、F3 的符号
        # 约定、F4 的「取不到就报错」、F5 的参照物口径都是 I4 的判据来源），标题只作补充
        # —— 只留标题不留正文的重生成最容易被漏掉。
        'F. I4 绩效看板的读数口径登记',
        '看板是纯读数组件 —— 屏幕上出现的每一个数，都是 §2.5 的 `PerformanceAnalyzer` 在本次回测里算出来的数，看板一个都不重算',
        'F1. 「只读数不算数」是本轮的第一条口径',
        'F3. 回撤是正数百分比',
        'F4. 取不到就报错，不退回 0',
        'F5. 一致性判据的参照物必须来自报告之外（这才是 DoD 触发测试瞄的那条边）',
        'F6. 本迭代不做',
        # G（B7 还债）：主契约由 .docx 派生 ⇒ 「补上未定义的类型/异常」只能追加成附录 G。
        # 这些锚点如果不登记，重新转换一次就会把整节抹掉，而**上面那条段落逐个对比的判据
        # 会全绿**（missing=0），报告看起来比真通过还干净 —— 单向判据的经典隐身方式。
        # 锚点选的是**内容**而不是标题：① 那句「新副本 ≠ 原文修好」是本节唯一的诚实声明，
        # 只留标题不留正文的重生成最容易把它丢掉；② `tools/verify_contract_appendix.py`
        # 是盯着本节的机器可读那一半，抹掉它这节就变成没人核对的散文；③ G6 的契约先行
        # 登记与 AX-PENDING-DRIFT / AX-T1-COPY 两个码是「实现落地后必须回来改」的判据
        # 来源，同样不能只活在人的记忆里。
        'G. B7 欠账一次性补齐',
        '**本节的新副本不等于上文已被修好**',
        'tools/verify_contract_appendix.py',
        '#### G6. 契约先行：引用了、但本迭代不实现的 5 个名字',
        '不证明「实现对」',
        # G9 是 G7/G8 两句自述的**更正**（计数 34→35、棘轮口径「清零」→「等于基线」）。
        # 它拿内容锚而不是标题锚：一句自我更正最容易被「只留标题不留正文」的重生成抺平，
        # 而上面那几条锚（G 节在 / 工具在 / G6 在）一条都不会红。
        '#### G9. 计数更正：G7 的第一句少算 1，G8 那句口径也不准',
        '**它不是「清零」，而是「等于基线」**',
        # G10 是 F4 一处拼写（`DASHBOARD001`）的更正，外加同一轮门禁抓出的另一条真冲突
        # （风控契约 §3.7 把 `RISK_001` 的级别转抄成 ERROR）的记录。它同属「只活在 md 里」
        # 的追加块 —— 重新转换一次就没了，而上面那条「docx 每段都在」的判据会全绿。
        # 内容锚选的是那两句论断本身（更正结论、以及「例外不许长成永久免检的挡箭牌」），
        # 因为「只留标题不留正文」正是抹掉这两句的形态（G9 同款理由）。
        '#### G10. F4 引用的看板错误码拼写更正（2026-09-25 追加）',
        '**以实现侧为准**：正确拼写是 `DASHBOARD_001`。',
        '，免得例外长成永久免检的挡箭牌。',
    ],
}


def norm(s):
    s = s.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
    return STRIP.sub('', s)


def fields_kept(p, hay):
    """For a declared-relocated paragraph, every field value must survive."""
    m = LABELED.match(p)
    if not m:
        return False
    for g in m.groups():
        if not g or norm(g) not in hay:
            return False
    RELOCATED.append(norm(m.group(1)))
    return True


def superseded(p, hay):
    """A declared post-conversion edit. Honoured only when the replacement text is
    actually present in the md; otherwise the rewrite dropped the content instead of
    replacing it and the paragraph must still count as missing."""
    for old, new, why in SUPERSEDED:
        if norm(old) == p:
            if norm(new) not in hay:
                print('    SUPERSEDED-BUT-REPLACEMENT-MISSING: %r  (%s)' % (new, why))
                return False
            SUPERSEDED_OK.append(why)
            return True
    return False


def para_texts(xml):
    """every paragraph's normalized text, including those inside table cells"""
    out = []
    for m in BLOCK_RE.finditer(xml):
        block = m.group(0)
        if block.startswith('<w:tbl'):
            for c in re.findall(r'<w:tc>.*?</w:tc>', block, re.S):
                out.extend(para_texts(c))
        else:
            txt = ''.join(html.unescape(t.group(1)) for t in TOKEN_RE.finditer(block) if t.group(1) is not None)
            n = norm(txt)
            if n:
                out.append(n)
    return out


def verify(docx, md_path, label):
    del RELOCATED[:]                    # per-file counter, never accumulate
    del SUPERSEDED_OK[:]
    with zipfile.ZipFile(docx) as z:        xml = z.read('word/document.xml').decode('utf-8', 'ignore')
    body = re.search(r'<w:body>(.*)</w:body>', xml, re.S)
    if body:
        xml = body.group(1)
    paras = para_texts(xml)
    md = open(md_path, encoding='utf-8').read()
    hay = norm(md)

    missing = [p for p in paras if p not in hay
               and not fields_kept(p, hay)
               and not superseded(p, hay)]
    # post-conversion additions: asserted PRESENT (see ADDENDA). Keyed by file name, so
    # a copy under a different name gets no addenda checks at all -- the negative control
    # must therefore write its sample into a temp dir that preserves the basename.
    miss_add = [s for s in ADDENDA.get(os.path.basename(md_path), []) if norm(s) not in hay]
    print('[%s] paragraphs=%d  missing=%d  relocated-fields-ok=%d  superseded-ok=%d  addenda-missing=%d'
          % (label, len(paras), len(missing), len(RELOCATED), len(SUPERSEDED_OK), len(miss_add)))
    for p in missing[:10]:
        print('    MISSING: %s' % p[:110])
    for s in miss_add:
        print('    MISSING-ADDENDUM: %s' % s[:110])
    return len(missing) + len(miss_add)


def gate(ok, msg):
    if not ok:
        print('GATE FAIL: ' + msg)
        sys.exit(1)


def struct_check(md, label):
    """Structural gates that paragraph-by-paragraph diffing cannot catch."""
    issues = []
    if '\r' in md:
        issues.append('CRLF present (count=%d)' % md.count('\r'))
    leaks = [i + 1 for i, ln in enumerate(md.split('\n')) if re.search(r'</?w:[a-zA-Z]', ln)]
    if leaks:
        issues.append('raw XML leaked on lines %s' % leaks[:5])
    lines = md.split('\n')
    depth = 0
    opens = 0
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if not s.startswith('```'):
            continue
        if depth == 0:
            depth = 1
            opens += 1
            if s.lstrip('`').strip() and not re.fullmatch(r'[A-Za-z0-9_+-]+', s.lstrip('`').strip()):
                issues.append('line %d: bad fence info string %r' % (i, s))
        else:
            depth = 0
    if depth != 0:
        issues.append('unclosed code fence')
    if opens == 0:
        issues.append('no code fences found (%s) -> structural check is vacuous' % label)
    for i, ln in enumerate(lines, 1):
        if re.match(r'^#{1,6}[^#\s]', ln):
            issues.append('line %d: heading without space after #: %r' % (i, ln[:40]))
    for m in re.finditer(r'^\|(.+)\|$', md, re.M):
        n = m.group(1).count('|')
        if n < 1:
            issues.append('line with malformed table row: %r' % m.group(0)[:50])
    return issues


if sys.argv[1] == '--selftest':
    # Every sample below goes into a PER-PROCESS directory. The old code used fixed
    # names under %TEMP% (broken-selftest.md / superseded-selftest.md / md-addenda-ctrl),
    # which means two concurrent runs of this file would overwrite each other's samples
    # -- and a negative control that is testing the other process's bytes reports OK
    # while proving nothing about this one. The harness runs this gate's selftest and
    # its real pass back to back in one worker, so today that needs a second harness
    # process to happen; it is fixed anyway because "the control can be clobbered" is
    # not a property anyone can see in the output.
    ctrl_root = tempfile.mkdtemp(prefix='md-coverage-selftest-')
    # negative control: a deliberately gutted md must be reported as FAIL
    d = sys.argv[2]
    docx = sorted(glob.glob(os.path.join(d, '*.docx')))[0]
    md = os.path.splitext(docx)[0] + '.md'
    broken = os.path.join(ctrl_root, 'broken-selftest.md')
    txt = open(md, encoding='utf-8').read()
    with open(broken, 'w', encoding='utf-8', newline='\n') as f:
        f.write(txt[:len(txt) // 2])       # keep only half the document
    n = verify(docx, broken, 'NEGATIVE-CONTROL')
    gate(n > 0, 'broken md reported 0 missing -> verifier is a no-op')
    # second control: the relocation rule must not swallow intact paragraphs
    n2 = verify(docx, md, 'POSITIVE-CONTROL')
    gate(n2 == 0, 'intact md reported %d missing -> false alarm' % n2)

    # third control: a declared supersession must not become a content hole. Delete
    # the REPLACEMENT text from a copy of the md. The old paragraph is still absent,
    # so a naive allowlist would stay green while the content vanished; the gate must
    # report it.
    prd_docx = os.path.join(d, '智能量化交易平台.docx')
    prd_md = os.path.join(d, '智能量化交易平台.md')
    gate(os.path.exists(prd_docx), 'superseded control needs %s' % prd_docx)
    t = open(prd_md, encoding='utf-8').read()
    before = t
    for _, new, _ in SUPERSEDED:
        t = t.replace(new, '')
    gate(t != before, 'no declared replacement text found in the md -> the SUPERSEDED '
                      'table has drifted away from the file it claims to describe')
    lost = os.path.join(ctrl_root, 'superseded-selftest.md')
    with open(lost, 'w', encoding='utf-8', newline='\n') as f:
        f.write(t)
    n3 = verify(prd_docx, lost, 'SUPERSEDED-CONTROL')
    gate(n3 > 0, 'a superseded paragraph whose replacement vanished was NOT reported -> '
                 'the supersession rule is a content hole')

    # fourth control: a declared post-conversion ADDENDUM whose text vanished must be
    # reported. Deleting it leaves every docx paragraph in place, so the paragraph diff
    # stays silent by construction -- without this control a re-conversion would wipe the
    # appended corrections and the gate would print a CLEANER report than a real pass.
    # EVERY file with a declared entry gets its own sample, and every declared anchor of
    # that file is deleted at once: a sample that leaves one anchor behind says nothing
    # about the guard on that one (2026-09-25: I4 added the F anchors -- one PRD sample
    # must not be asked to stand in for them).
    # The samples must keep the basename (ADDENDA is keyed by file name) -- so the
    # per-process directory holds them, rather than the names being made unique.
    ctrl_dir = os.path.join(ctrl_root, 'addenda')
    os.makedirs(ctrl_dir, exist_ok=True)
    n4 = {}
    n5 = {}
    for add_name in sorted(ADDENDA):
        add_docx = os.path.join(d, os.path.splitext(add_name)[0] + '.docx')
        add_md = os.path.join(d, add_name)
        gate(os.path.exists(add_docx) and os.path.exists(add_md),
             'ADDENDA control needs %s and its .docx' % add_name)
        anchors = ADDENDA[add_name]
        gate(anchors, 'ADDENDA entry for %s is empty -> nothing to guard' % add_name)
        t2 = open(add_md, encoding='utf-8').read()
        for a in anchors:
            # A norm()-only match hides markdown drift and, worse, makes this control
            # unable to delete the anchor: the sample silently stops exercising it while
            # still reporting OK. So every declared anchor must be LITERAL file text.
            gate(a in t2, 'ADDENDA anchor %r is not literal text of %s -> deleting it is a '
                          'no-op, so this control could never exercise it (2026-09-25: the '
                          'E5~E8 anchors were registered without their markdown, exactly '
                          'this way)' % (a, add_name))
        t2b = t2
        for a in anchors:
            t2b = t2b.replace(a, '')
        gate(t2b != t2, 'ADDENDA markers of %s not found in the file -> the guard has '
                        'drifted away from the file it claims to describe' % add_name)
        lost2 = os.path.join(ctrl_dir, add_name)
        with open(lost2, 'w', encoding='utf-8', newline='\n') as f:
            f.write(t2b)
        n4[add_name] = verify(add_docx, lost2, 'ADDENDA-CONTROL')
        # EXACT expectation: the docx paragraphs are all still there, so the ONLY way to
        # reach this number is "every declared anchor was reported missing". `> 0` would
        # also pass if just one of five anchors fired.
        gate(n4[add_name] == len(anchors),
             'deleting all %d declared addendum(s) of %s reported %d issue(s) -> the guard '
             'does not fire once per anchor' % (len(anchors), add_name, n4[add_name]))
        # clean sample for the SAME detector: the shipped file must report 0 (otherwise
        # the addenda check fires on a correct file and the negative control proves nothing)
        n5[add_name] = verify(add_docx, add_md, 'REAL-ADDENDA')
        gate(n5[add_name] == 0, 'the shipped %s reported %d issue(s) -> ADDENDA drifted '
                                'away from the file' % (add_name, n5[add_name]))

    # struct_check must actually fire: one INDEPENDENT sample per detector, ordered
    # so that no detector shadows another (an earlier unbalanced fence would swallow
    # the bad info string and make that detector silently never run).
    bad = ''.join([
        '#NoSpace\r\n',                       # heading w/o space + CRLF
        '<w:p>leak</w:p>\n',                  # raw XML leak
        '```bad lang info string\nx\n```\n',  # bad info string, balanced on purpose
        '```python\nno close\n',              # unclosed fence must be LAST
    ])
    got = struct_check(bad, 'STRUCT-CONTROL')
    for want in ('CRLF', 'raw XML leaked', 'bad fence info string',
                 'unclosed code fence', 'heading without space'):
        gate(any(want in g for g in got), 'struct_check missed detector: ' + want)
    print('SELFTEST OK: detects removed content (missing=%d), no false alarm on intact md, '
          'lost replacement caught (missing=%d), lost addenda caught for all %d file(s) '
          '(%s), clean addenda samples (missing=%s), struct_check fires on all %d detectors'
          % (n, n3, len(n4),
             ', '.join('%s=%d/%d' % (k, n4[k], len(ADDENDA[k])) for k in sorted(n4)),
             ', '.join('%s=%d' % (k, n5[k]) for k in sorted(n5)), len(got)))
    shutil.rmtree(ctrl_root, ignore_errors=True)
    sys.exit(0)

d = sys.argv[1]
total = 0
for f in sorted(glob.glob(os.path.join(d, '*.docx'))):
    md = os.path.splitext(f)[0] + '.md'
    gate(os.path.exists(md), 'md not found for ' + os.path.basename(f))
    base = os.path.basename(f)[:34]
    total += verify(f, md, base)
    txt = open(md, encoding='utf-8').read()
    for iss in struct_check(txt, base):
        print('[%s] STRUCT: %s' % (base, iss))
        total += 1

print('verdict: %s (total missing=%d)' % ('PASS' if total == 0 else 'FAIL', total))
sys.exit(0 if total == 0 else 1)
