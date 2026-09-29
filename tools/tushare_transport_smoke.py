"""tushare 传输层冒烟：**人工执行，不是门禁**。

为什么单独一个脚本而不是一条 pytest：这里要的是「真实券源上、拿着真凭证时的真实响应
长什么样」，它**不可重复**（凭证是本机的、源会限流、积分会变、网络会断），把它写成
用例就等于把外网与本机凭证变成红的理由 —— 本仓库明确不许这样（C4：门禁不许把
「装不装 / 连不连得上 / 有没有凭证」变成能不能跑的条件）。

它补的是 I2c 收工记录里**唯一**还开着的那条口子（`docs/迭代计划.md` I2c §未收口 ③）：
那次真实联网只探过**一个标的 × 一段 9 天窗口**（`600000.SH` × 20240102~20240110）。
本工具把它扩成七条探针：复现那次窗口 + **多标的 × 整年**走完整适配器链路 + `has_more`
边界 + 空区间 + 不存在的标的 + **真实端点的鉴权失败码** + 「反区间在本地就被拦住」。

它**不能**证明的事（先写在这里，免得报告被读成「tushare 已验证」）：

* 它只测**取数**这一侧。**写入侧与读侧的状态不归它管**，它一个字节都不验证 ——
  2026-09-29 晚 **Ⅱ**（复权价实施）之后的现状是：因子**读得到**（`HFQ`/`QFQ` 缺行抛 DATA_001）、
  **复权价已在读取时现算**（`quanauto/datafeed.py` 的 `_rescale_price`，全仓库**两个**调用点是
  `DbDataFeed._row_to_bar` 与 `CsvDataFeed._load_data`（晚 Ⅲ 补上第二条），
  四列 OHLC 各乘一次；`volume` / `amount` **刻意不乘**）、写入口有实现但**没有任何产品调用点**。
  **订正**：这一段此前写的是「**复权价仍未实施**（`BarData` 的 OHLC 仍是不复权价，引擎里没有
  一处算术用因子）」—— 那句话**已作废**；「缺口没关」本身仍然成立，只是开着的换成了另外三半
  （分红 / 两个 ingestor 零产品调用点）。**2026-09-29 晩 Ⅳ**：第三条「`validate_frame` 对复权帧
  零判据」**已收口**（值域 / 帧级自然键重复 / 认 schema）⇒ 只剩上面前两条。逐条见 DC 契约附录 A6
  的收口块（内有 **2026-09-29 晚 Ⅱ 的状态订正**）与 B21.3 的订正。
* 它只测 `adj_factor` 这一条通道。日线 / 财务 / 指数成分股本迭代没接。
* 凭证是**本机这一份**，测出来的「取得到」不等于「别人也取得到」。
* 报告是一份**快照**：源改版、积分变化、换个窗口，结论就过期。

凭证**不从这里传**（契约 §3.2 的签名里没有凭证参数）。它由 `tushare_token()` 读环境
变量 `TUSHARE_TOKEN`（优先）或项目根 `.env`。本文件、本次输出、写出的报告里**只有
变量名、没有值**；请求体里的 `token` 字段在报告里一律写成 `<redacted:TUSHARE_TOKEN>`。

用法：
    python tools/tushare_transport_smoke.py                      # 真联网，写默认报告
    python tools/tushare_transport_smoke.py --report=PATH        # 另存，不动默认那份
    python tools/tushare_transport_smoke.py --env-file=PATH      # 换一份 .env 取凭证
    python tools/tushare_transport_smoke.py --symbols=A.SH,B.SZ  # 换标的（默认三个）
    python tools/tushare_transport_smoke.py --selftest           # 不联网，自测判据 + 末尾那一段
    python tools/tushare_transport_smoke.py --endpoint=URL       # 只给离线自检/触发测试用

The `--endpoint=` line is not for normal use: it points the tool at a local fake server so the
seven probes can be exercised with **no credential**. Whenever it is used, `STUB` is stamped
into the report header and the verdict line, so such a report cannot be read as evidence about
the real source.

怎么读这份结果（这三行是**实测**出来的，不是设计意图）：

* `SMOKE OK` = 七条探针都按预期，且至少有一条做过列名比对。
* `T2 transport-failed` + `ALL: nothing-compared` = 适配器整体没跑通（本机假源上把
  `adj_factor` 字段去掉就是这个形状）⇒ 先看 T2 那条的异常，别去看 T1。
* T1~T5 全 `transport-failed` + `ALL: nothing-measured` = **一个字节都没拿到**（假源返回
  非 JSON 就是这个形状）—— 这是最该警惕的形状：它和「源很好、只是没数据」完全不同。
* `T5` 报 `code=0` 且 0 行时，**它和「窗口内没数据」在响应上不可区分** —— 报告里只记录
  这个事实，不猜。

退出码：
    0 = 全部探针都按预期（含两条**预期被拒**的对照），且至少做过一条列名比对；
    1 = 提取为空 / 传输失败 / 预期被拒的却没被拒 / 一条数据都没测到 / 一条比对都没做；
    2 = `--selftest` 自测失败，或者命令行参数不认识。

**没有凭证时退 1 且一个字节都不写**（fail-closed）：一份写着「未执行」的报告进仓以后，
下一个人看到的是「有个文件」而不是「没跑过」—— 那正是本仓库吃过不止一次的假绿。

自测（`--selftest`）覆盖两组：`_judge` 的判据本身，以及主流程末尾写报告 / 打印路径那
一小段（东方财富那个工具就是在那一段崩的）。它只在系统临时目录里写东西，不碰仓库。

控制台只打 ASCII 或 GBK 覆盖得到的字符 —— 非 GBK 字符（连 `⇒` 都不行）会让 Python
抛 UnicodeEncodeError，而一个字符打印不出来就把整条命令变成崩溃，太贵了。报告的正文
不受这条限制（它是 UTF-8 文件）。报告写 UTF-8 + LF，与
`tools/eastmoney-transport-smoke-report.txt`、`tools/ci-dryrun-report.txt` 一致。
"""

from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from quanauto import datasources  # noqa: E402  （--endpoint= 要改它的 TUSHARE_API）
from quanauto.datasources import (  # noqa: E402  （必须在 sys.path 调整之后）
    ADJUST_FACTOR_COLUMNS,
    ENV_FILE_NAME,
    HTTP_TIMEOUT_SECONDS,
    TUSHARE_ADJ_FACTOR,
    TUSHARE_ADJ_FACTOR_API,
    TUSHARE_API,
    TUSHARE_AUTH_CODE,
    TUSHARE_TOKEN_ENV,
    TushareAdapter,
    _http_post_json,
    _tushare_rows,
    normalize_symbol,
    tushare_token,
)
from quanauto.errors import SourceAdapterError  # noqa: E402

REPORT_PATH = os.path.join(ROOT, 'tools', 'tushare-transport-smoke-report.txt')

#: I2c 那次探针原样复现用的窗口（`docs/迭代计划.md` 记的是 `code=0` / 7 行）——
#: 有了同一个窗口，两份记录才能并排看。
I2C_SYMBOL = '600000.SH'
I2C_START = date(2024, 1, 2)
I2C_END = date(2024, 1, 10)

#: 多标的 × 整年：**这是本工具相对 I2c 那次探针的主要扩面**。
#: 三个标的跨两个交易所（沪 / 深），一年 ≈ 242 个交易日 ⇒ 期望七百多行。
DEFAULT_SYMBOLS = ('600000.SH', '000001.SZ', '600519.SH')
DEFAULT_START = date(2024, 1, 1)
DEFAULT_END = date(2024, 12, 31)

#: `has_more` 边界：宽到「一次可能拿不完」。只读信封，**不**构造 DataFrame ——
#: 因为适配器遇到 `has_more=true` 会直接抛（本迭代不实现翻页），那样就测不到它的值了。
WIDE_START = date(1990, 1, 1)

#: 空区间：未来日期。要回答的是「items 为空时 `fields` 还在不在」——
#: `TushareAdapter._default_fetch` 正是靠 `fields` 造空帧的列名。
EMPTY_START = date(2030, 1, 2)
EMPTY_END = date(2030, 1, 5)

#: 结构合法（6 位数字 + `.SH`）但**不存在**的标的。要回答的是「源怎么报告它」。
MISSING_SYMBOL = '999999.SH'

#: 反区间：`_default_fetch` 应当在**发请求之前**就拦掉。用计数器证明「一个请求都没发」。
REVERSED_START = date(2024, 1, 10)
REVERSED_END = date(2024, 1, 2)

#: 故意写坏的字面量。**它不是任何真实凭证，也不是真凭证的变形** ——
#: 目的是让源回一次真实的鉴权失败，好核对 `TUSHARE_AUTH_CODE` 到底是不是 40101。
BAD_TOKEN = 'quan-auto-smoke-test-invalid-token'

#: 实际要打的地址，正常就是 `TUSHARE_API`。`--endpoint=` 把它指向**本机假服务端**，
#: 好在本机没有凭证的情况下把七条探针真的跑一遍（本工具第一次交付时就是这个状态）。
#: 被改过时报告头部与 verdict 行都会带上 `STUB` 字样 —— **一个开关不该能伪造证据**。
#:
#: 🔴 **探针里的地址一律用 `ENDPOINT`，不许直接引用裸常量 `TUSHARE_API`**：
#: 2026-09-29 实测撞到过 —— T6 当时写的是 `_http_post_json(TUSHARE_API, body)`，于是
#: `--endpoint=` 指向假源时它**仍然打了真端点**，而报告抬头写着 `STUB`（`TUSHARE_API`
#: 是 `from ... import` 进来的**本模块另一个名字**，改 `datasources` 里的它不会跟着变）。
#: `main()` 里现在把两个名字一起改，但「一处覆盖、每个探针都用它」才是治根的那条。
ENDPOINT = TUSHARE_API

# ── 五类探针结果 ─────────────────────────────────────────────────────────────
#: 拿到了要测的东西。只有它可以带 `compares=True`。
MEASURED = 'measured'
#: 提取为空（信封解不开 / 该有的字段不是列表）—— 后面的比对会全部空转。
EXTRACT_EMPTY = 'extract-empty'
#: 连不上 / 解不了 / 适配器抛了别的异常。
TRANSPORT_FAILED = 'transport-failed'
#: **预期**被拒，而且确实被拒了（T6/T7 两条对照）。
EXPECTED_REJECT = 'expected-reject'
#: 预期与实际不符 —— 包括「预期被拒的竟然被接受」。
EXPECTATION_MISSED = 'expectation-missed'


def _r(label, outcome, compares=False, note=''):
    """一条探针的结果。`note` 只进报告，用来解释「为什么是这个 outcome」。"""
    return {'label': label, 'outcome': outcome, 'compares': compares, 'note': note}


def _d(value):
    return value.isoformat()


def _ascii(text):
    """控制台出口。本机是 cp936，非 GBK 字符会让 Python 抛 UnicodeEncodeError。"""
    return str(text).encode('ascii', 'replace').decode('ascii')


def _show_path(report_path):
    r"""报告路径在人看的那一行的写法。**绝不抛**：跨盘符时 `os.path.relpath` 抛
    `ValueError`（如 `--report=%TEMP%\x.txt`，D: -> C:）。东方财富那个冒烟工具当场
    撞上过这个 —— 报告已经写好了，却在最后一行打印时崩掉、退出码变成 1。
    工具既然广告了 `--report=`，就得对任何盘符都能用。"""
    try:
        return os.path.relpath(report_path, ROOT)
    except ValueError:
        return report_path


def _write_report(report_path, lines):
    """写报告，返回 `(ok, message)`。**fail-closed**：`--report=` 的路径不可写就
    **不写**，绝不退回默认路径 —— 退回会把上一版快照覆盖掉（`run_sql_smoke.py`
    的 `--report=` 是同一约定）。"""
    try:
        with io.open(report_path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write('\n'.join(lines) + '\n')
    except OSError as exc:
        return False, '%s: %s' % (type(exc).__name__, exc)
    return True, ''


def _redact(body):
    """请求体里的凭证换成占位符。**报告会进仓库，令牌值绝不进去。**"""
    safe = dict(body)
    if 'token' in safe:
        safe['token'] = '<redacted:%s>' % TUSHARE_TOKEN_ENV
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


def _raw_post(params, token):
    """按适配器那条真实路径 POST 一次（**不**构造 DataFrame）。

    T3 / T4 / T5 / T6 为什么绕开适配器：适配器会在几个地方**先**抛（`has_more=true`
    直接拒绝、空帧要 `fields`、鉴权失败要分类），而这几条探针要测的恰好就是那些抛点
    **之前的**原始值。用适配器就测不到它们了。
    """
    body = {'api_name': TUSHARE_ADJ_FACTOR_API, 'token': token,
            'params': dict(params), 'fields': ''}
    doc = _http_post_json(ENDPOINT, body)
    return doc, _redact(body)


def _envelope(doc, lines, prefix='    '):
    """把信封的公共字段写进报告，返回 `(code, data)`；形状不认得时 `(None, None)`。

    **不抛异常**：解不开信封不是本工具崩了，而是一条要记下来的结论（提取为空）。
    """
    if not isinstance(doc, dict) or 'code' not in doc:
        lines.append('%s!! code: （没有）—— 回的不是本工具认得的信封（%s），提取为空'
                     % (prefix, type(doc).__name__))
        return None, None
    code = doc.get('code')
    data = doc.get('data') if isinstance(doc.get('data'), dict) else None
    lines.append('%scode=%s  msg=%s' % (prefix, code, doc.get('msg')))
    if data is None:
        lines.append('%s!! data: 不是对象（%s），提取为空'
                     % (prefix, type(doc.get('data')).__name__))
        return code, None
    fields, items = data.get('fields'), data.get('items')
    lines.append('%sdata.count=%s  data.has_more=%s'
                 % (prefix, data.get('count'), data.get('has_more')))
    if isinstance(fields, list):
        lines.append('%sdata.fields(%d): %s'
                     % (prefix, len(fields), ', '.join(str(name) for name in fields)))
    else:
        lines.append('%s!! data.fields: 不是列表（%s），提取为空'
                     % (prefix, type(fields).__name__))
    if isinstance(items, list):
        lines.append('%sdata.items: %d 行' % (prefix, len(items)))
    else:
        lines.append('%s!! data.items: 不是列表（%s），提取为空'
                     % (prefix, type(items).__name__))
    return code, data


def _window(start, end):
    """tushare 要 `YYYYMMDD` 的无分隔形式（写成 ISO 会得到一个 code 非 0 的「成功响应」）。"""
    return {'start_date': start.strftime('%Y%m%d'), 'end_date': end.strftime('%Y%m%d')}


# ── 七条探针 ─────────────────────────────────────────────────────────────────

def _probe_i2c(lines, token):
    """T1：复现 I2c 那次窗口，并逐字比对映射表。"""
    label = 'T1'
    lines.append('[%s] 复现 I2c 那次探针：%s × %s~%s（与 `docs/迭代计划.md` 记的那次可比）'
                 % (label, I2C_SYMBOL, _d(I2C_START), _d(I2C_END)))
    params = dict({'ts_code': I2C_SYMBOL}, **_window(I2C_START, I2C_END))
    try:
        doc, sent = _raw_post(params, token)
    except Exception as exc:  # noqa: BLE001 —— 冒烟工具，任何异常都记进报告
        lines.append('    TRANSPORT FAILED: %s: %s' % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    lines.append('    发出（token 已脱敏）: %s' % sent)
    code, data = _envelope(doc, lines)
    if data is None or not isinstance(data.get('fields'), list):
        return _r(label, EXTRACT_EMPTY, note='信封没解出 fields')
    lines.append('    第一行: %s' % (data['items'][0] if data.get('items') else '(空)'))
    lines.append('    最后一行: %s' % (data['items'][-1] if data.get('items') else '(空)'))
    if code != 0:
        return _r(label, EXTRACT_EMPTY,
                  note='code=%s 非 0，这次没拿到数据，列名比对无法进行' % code)
    mapping = list(TUSHARE_ADJ_FACTOR)
    keys = [str(name) for name in data['fields']]
    present = [key for key in mapping if key in keys]
    missing = [key for key in mapping if key not in keys]
    lines.append('    比对 TUSHARE_ADJ_FACTOR -> 真实 data.fields：命中 %d/%d'
                 % (len(present), len(mapping)))
    lines.append('      present: %s' % (', '.join(present) or '(none)'))
    lines.append('      missing: %s' % (', '.join(missing) or '(none)'))
    if missing:
        lines.append('    !! 映射表里有 %d 个键在真实返回里**没有** —— 这是**已证伪**的映射，'
                     '不是「未验证」' % len(missing))
        return _r(label, EXTRACT_EMPTY, compares=True, note='映射表键在真实 fields 里缺失')
    lines.append('    I2c 那次记的是 7 行；本次是 %d 行（交易日历一致就该一样）'
                 % len(data['items']))
    return _r(label, MEASURED, compares=True)


def _probe_multi_symbol_year(lines, symbols, start, end):
    """T2：多标的 × 整年，走**完整适配器链路**（POST → 解帧 → `normalize_adjust_factor`）。"""
    label = 'T2'
    lines.append('[%s] 多标的 × 整年，走完整适配器链路：%d 标的 × %s~%s'
                 % (label, len(symbols), _d(start), _d(end)))
    lines.append('    symbols: %s' % ', '.join(symbols))
    lines.append('    注：`fetch_adjust_factor` 逐标的 POST 一次 ⇒ 期望 %d 次请求'
                 % len(symbols))
    try:
        frame = TushareAdapter().fetch_adjust_factor(list(symbols), start, end)
    except Exception as exc:  # noqa: BLE001
        lines.append('    TRANSPORT FAILED: %s: %s' % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    columns = list(frame.columns)
    count = len(frame)
    lines.append('    frame: %d 行, 列=%s' % (count, ', '.join(columns)))
    lines.append('    列名与顺序 == ADJUST_FACTOR_COLUMNS(%s): %s'
                 % (', '.join(ADJUST_FACTOR_COLUMNS), columns == list(ADJUST_FACTOR_COLUMNS)))
    if not count:
        lines.append('    !! extract-empty: 整年一条因子都没有，后面的比对无法进行')
        return _r(label, EXTRACT_EMPTY)
    per = {}
    for symbol, day in zip(frame['symbol'].tolist(), frame['trade_date'].tolist()):
        low, high, seen = per.get(symbol, (day, day, 0))
        per[symbol] = (min(low, day), max(high, day), seen + 1)
    for symbol in sorted(per):
        low, high, seen = per[symbol]
        lines.append('      %s: %d 行 %s~%s' % (symbol, seen, _d(low), _d(high)))
    asked = set(normalize_symbol(symbol) for symbol in symbols)
    answered = set(per)
    if asked != answered:
        lines.append('    !! 请求了 %s，只答了 %s —— 缺失的标的一条数据都没有'
                     % (sorted(asked), sorted(answered)))
        return _r(label, EXTRACT_EMPTY, compares=True, note='有标的整段没有数据')
    if columns != list(ADJUST_FACTOR_COLUMNS):
        lines.append('    !! 归一化后的列名/顺序与契约的 `ADJUST_FACTOR_COLUMNS` 不一致')
        return _r(label, EXTRACT_EMPTY, compares=True, note='列名/顺序不符')
    return _r(label, MEASURED, compares=True)


def _probe_has_more(lines, token):
    """T3：`has_more` 边界。**只读信封** —— 适配器遇到 true 会直接抛，就测不到它的值了。"""
    label = 'T3'
    today = date.today()
    lines.append('[%s] `has_more` 边界：%s × %s~%s（只读信封，不构造 DataFrame）'
                 % (label, I2C_SYMBOL, _d(WIDE_START), _d(today)))
    params = dict({'ts_code': I2C_SYMBOL}, **_window(WIDE_START, today))
    try:
        doc, sent = _raw_post(params, token)
    except Exception as exc:  # noqa: BLE001
        lines.append('    TRANSPORT FAILED: %s: %s' % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    lines.append('    发出（token 已脱敏）: %s' % sent)
    code, data = _envelope(doc, lines)
    if data is None:
        return _r(label, EXTRACT_EMPTY, note='信封没解出 data')
    has_more = data.get('has_more')
    lines.append('    判定：has_more=%s ⇒ 「翻页没实现」这条未收口项在**这个区间**上%s'
                 % (has_more, '是**真的会撞到**的，不是一个理论风险' if has_more
                    else '一次就拿到了，这里撞不到'))
    lines.append('    判定：count=%s ⇒ 这是该区间真实行数；若它恰好卡在某个整数上，'
                 '那个整数就是源的上限（本工具**不猜**上限是多少）' % data.get('count'))
    if code != 0:
        return _r(label, EXTRACT_EMPTY, note='code=%s 非 0，没拿到信封里的真值' % code)
    return _r(label, MEASURED)


def _probe_empty_window(lines, token):
    """T4：空区间。要回答「items 为空时 `fields` 还在不在」。"""
    label = 'T4'
    lines.append('[%s] 空区间（未来日期）：%s × %s~%s'
                 % (label, I2C_SYMBOL, _d(EMPTY_START), _d(EMPTY_END)))
    params = dict({'ts_code': I2C_SYMBOL}, **_window(EMPTY_START, EMPTY_END))
    try:
        doc, sent = _raw_post(params, token)
    except Exception as exc:  # noqa: BLE001
        lines.append('    TRANSPORT FAILED: %s: %s' % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    lines.append('    发出（token 已脱敏）: %s' % sent)
    code, data = _envelope(doc, lines)
    if data is None:
        return _r(label, EXTRACT_EMPTY, note='信封没解出 data')
    fields, items = data.get('fields'), data.get('items')
    if code != 0:
        return _r(label, EXTRACT_EMPTY, note='code=%s 非 0' % code)
    if not isinstance(items, list):
        return _r(label, EXTRACT_EMPTY, note='items 不是列表')
    lines.append('    判定：items=%d 行 且 fields %s ⇒ `_default_fetch` 拿 `fields` 造空帧'
                 '列名这条假设%s'
                 % (len(items), '在' if fields else '**不在**',
                    '成立' if fields else '**不成立**（空区间会得到一列都没有的空帧）'))
    return _r(label, MEASURED)


def _probe_missing_symbol(lines, token):
    """T5：结构合法但不存在的标的。要回答「源怎么报告它」。"""
    label = 'T5'
    lines.append('[%s] 不存在的标的：%s × %s~%s（结构合法，6 位数字 + .SH）'
                 % (label, MISSING_SYMBOL, _d(I2C_START), _d(I2C_END)))
    params = dict({'ts_code': MISSING_SYMBOL}, **_window(I2C_START, I2C_END))
    try:
        doc, sent = _raw_post(params, token)
    except Exception as exc:  # noqa: BLE001
        lines.append('    TRANSPORT FAILED: %s: %s' % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    lines.append('    发出（token 已脱敏）: %s' % sent)
    code, data = _envelope(doc, lines)
    if data is None:
        return _r(label, EXTRACT_EMPTY, note='信封没解出 data')
    items = data.get('items')
    if code == 0 and isinstance(items, list) and not items:
        lines.append('    判定：code=0 且 0 行 ⇒ **「标的不存在」与「区间内没有数据」在源这一层'
                     '长得一模一样**。适配器若想区分它们，只能靠上游的标的表 —— '
                     '不能靠这里的返回。这不是缺陷，是一条要写进证据的事实。')
    elif code != 0:
        lines.append('    判定：code=%s 非 0 ⇒ 源用**错误码**报告不存在的标的，'
                     '与「区间内没有数据」可区分' % code)
    else:
        lines.append('    判定：code=0 且 %s 行 ⇒ 源对不存在的标的**给了数据**，'
                     '这需要单独查清' % (len(items) if isinstance(items, list) else '?'))
    if code != 0:
        return _r(label, EXTRACT_EMPTY, note='code=%s 非 0' % code)
    return _r(label, MEASURED)


def _probe_bad_token(lines):
    """T6：**预期被拒**的对照 —— 用坏令牌核对真实的鉴权失败码是不是 `TUSHARE_AUTH_CODE`。

    这是本工具最有价值的一条：`TUSHARE_AUTH_CODE = '40101'` 这条判据来自**真实的**空/错
    令牌响应（DC 契约附录 B21.4 那张表），而它一旦错了，「凭证被拒」就会被归到
    `UNKNOWN`，动作从「换凭证」变成「照着 msg 去查文档」—— 修错方向。

    ⚠️ 这条对照**不需要有效凭证**（它需要的恰好是一份被拒的凭证），但本工具要先拿到
    凭证才会开始跑探针 ⇒ 想单跑这一条，目前只能用一份真实凭证把 T1~T5 一起跑起来。
    这是**已知的别扭处**，不是设计意图。
    """
    label = 'T6'
    lines.append('[%s] 坏令牌（字面量 `%s`，**不是任何真实凭证**）：预期被拒'
                 % (label, BAD_TOKEN))
    params = dict({'ts_code': I2C_SYMBOL}, **_window(I2C_START, I2C_END))
    body = {'api_name': TUSHARE_ADJ_FACTOR_API, 'token': BAD_TOKEN,
            'params': params, 'fields': ''}
    lines.append('    发出（token 已脱敏）: %s' % _redact(body))
    try:
        doc = _http_post_json(ENDPOINT, body)
    except Exception as exc:  # noqa: BLE001
        lines.append('    TRANSPORT FAILED: %s: %s' % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    code, _ = _envelope(doc, lines)
    if code is not None:
        lines.append('    实测 code=%s，常量 TUSHARE_AUTH_CODE=%s ⇒ %s'
                     % (code, TUSHARE_AUTH_CODE,
                        '一致' if str(code) == TUSHARE_AUTH_CODE else '**不一致**'))
    try:
        # 走真实的分类路径（`_tushare_rows` 是那个唯一的分类点）。
        _tushare_rows(doc)
    except SourceAdapterError as exc:
        lines.append('    SourceAdapterError kind=%s retryable=%s' % (exc.kind, exc.retryable))
        lines.append('    报错里含变量名 %s: %s'
                     % (TUSHARE_TOKEN_ENV, TUSHARE_TOKEN_ENV in str(exc)))
        lines.append('    报错里含坏令牌字面量（必须为 False）: %s' % (BAD_TOKEN in str(exc)))
        if exc.kind != 'SOURCE_AUTH':
            lines.append('    !! 预期被拒的方式是 SOURCE_AUTH，实际是 %s ⇒ '
                         '`TUSHARE_AUTH_CODE` 这条判据要改（动作会跟着错）' % exc.kind)
            return _r(label, EXPECTATION_MISSED, note='被拒的方式不是 SOURCE_AUTH')
        if BAD_TOKEN in str(exc):
            lines.append('    !! 报错里出现了令牌值 —— 这条判据是坏的')
            return _r(label, EXPECTATION_MISSED, note='报错回声了令牌值')
        if str(code) != TUSHARE_AUTH_CODE:
            lines.append('    !! 实测 code 与常量不一致 ⇒ 分类是按字符串比出来的，'
                         '但常量本身要改')
            return _r(label, EXPECTATION_MISSED, note='实测 code 与常量不一致')
        return _r(label, EXPECTED_REJECT)
    except Exception as exc:  # noqa: BLE001
        lines.append('    TRANSPORT FAILED: 抛的是 %s（不是 SourceAdapterError）：%s'
                     % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    lines.append('    !! 预期被拒，实际**没有抛异常** ⇒ 那条拒绝判据是空的')
    return _r(label, EXPECTATION_MISSED, note='坏令牌竟然被接受')


def _probe_reversed_window(lines):
    """T7：**预期被拒**的对照 —— 反区间应当在本地就被拦，一个请求都不发。

    用 `_call`（而不是公共的 `fetch_adjust_factor`）只是因为要注入一个计数用的 opener：
    tushare 只吃 POST，`opener` 是模块里给测试留的注入口，公共签名不转发它。
    数到的是**真实 HTTP 出口**被调用了几次，不是 fetch 被调用了几次。
    计数用的 opener **故意抛异常**：万一守卫没了，这次探针不会真的把请求打到源上。
    """
    label = 'T7'
    lines.append('[%s] 反区间（start=%s > end=%s）：预期在**本地**被拦，一个请求都不发'
                 % (label, _d(REVERSED_START), _d(REVERSED_END)))
    sent = {'requests': 0}

    def counting_opener(request, timeout=None):  # pragma: no cover —— 正常情况下不会被调到
        sent['requests'] += 1
        raise AssertionError('T7 不该发出任何请求')

    try:
        TushareAdapter()._call(symbol=I2C_SYMBOL, start=REVERSED_START,
                               end=REVERSED_END, opener=counting_opener)
    except SourceAdapterError as exc:
        lines.append('    SourceAdapterError kind=%s' % exc.kind)
        lines.append('    实际发出的请求数（必须为 0）: %d' % sent['requests'])
        if exc.kind != 'UNSUPPORTED' or sent['requests']:
            lines.append('    !! 预期「本地拦截 + 0 请求」，实际 kind=%s / %d 次请求'
                         % (exc.kind, sent['requests']))
            return _r(label, EXPECTATION_MISSED, note='反区间没在本地被拦')
        return _r(label, EXPECTED_REJECT)
    except Exception as exc:  # noqa: BLE001
        lines.append('    TRANSPORT FAILED: 抛的是 %s：%s' % (type(exc).__name__, exc))
        return _r(label, TRANSPORT_FAILED)
    lines.append('    !! 预期被拒，实际**没有抛异常**（发出 %d 次请求）' % sent['requests'])
    return _r(label, EXPECTATION_MISSED, note='反区间竟然被接受')


# ── 唯一的判据（`--selftest` 测的就是它）─────────────────────────────────────

def _judge(results):
    """把探针结果汇总成 `(ok, failures, counts)`。五条，每条对应一类假绿：

    * 任何一条**提取为空** ⇒ FAIL。提取失配时后面的比对全在空转，报告却会打印
      「全部探针正常」—— 本仓库吃过不止两次的假绿（空转守卫）。
    * 任何一条**传输失败** ⇒ FAIL。「连不上」不是「没测到坏东西」。
    * 任何一条**预期被拒**的探针没被拒 ⇒ FAIL。那说明那条拒绝判据是空的。
    * `measured` 为 0 ⇒ FAIL。一条数据都没测到，整份报告形同虚设。
    * 做过**列名比对**的探针为 0 ⇒ FAIL。那是本工具存在的主要理由。

    失败码是**纯 ASCII**（`<label>: <outcome>` / `ALL: <code>`），这样控制台不需要
    经 `encode('ascii','replace')` 就已经可读，中文解释留在报告正文里。
    """
    failures = []
    counts = {}
    for result in results:
        outcome = result['outcome']
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == EXTRACT_EMPTY:
            failures.append('%s: extract-empty' % result['label'])
        elif outcome == TRANSPORT_FAILED:
            failures.append('%s: transport-failed' % result['label'])
        elif outcome == EXPECTATION_MISSED:
            failures.append('%s: expectation-missed' % result['label'])
    if counts.get(MEASURED, 0) == 0:
        failures.append('ALL: nothing-measured')
    if not any(r['outcome'] == MEASURED and r['compares'] for r in results):
        failures.append('ALL: nothing-compared')
    return (not failures), failures, counts


def _selftest(out=print):
    """自测本工具的判据本身。**不联网。**只在系统临时目录里写东西，不碰仓库里的报告。

    第一组（判据）：一个探测器一个样本，干净样本一条（防误报），五条负样本各打到一条
    不同的分支。期望值写成**精确的失败码集合**，不是「是否非空」—— 否则「打到别的
    分支」会看起来像「打到了」。

    第二组（末尾）：主流程末尾还有一小段 —— 写报告 + 打印路径。它不依赖网络，所以
    **必须跑到**：东方财富那个工具就是在这里崩的（跨盘符 `os.path.relpath` 抛
    ValueError），而它当时已经在真网络上验证过了。
    """
    bad = 0
    total = 0

    def _record(name, matched, detail=()):
        nonlocal bad, total
        total += 1
        if not matched:
            bad += 1
        out('%s  %s' % ('ok  ' if matched else 'FAIL', name))
        for line in detail:
            out('        %s' % line)

    cases = [
        ('判据/干净样本：2 条 measured(含比对) + 2 条 expected-reject',
         [_r('T1', MEASURED, True), _r('T2', MEASURED, True),
          _r('T6', EXPECTED_REJECT), _r('T7', EXPECTED_REJECT)],
         True, []),
        ('判据/负样本：一条 extract-empty',
         [_r('T1', MEASURED, True), _r('T3', EXTRACT_EMPTY)],
         False, ['T3: extract-empty']),
        ('判据/负样本：一条 transport-failed',
         [_r('T1', MEASURED, True), _r('T2', TRANSPORT_FAILED)],
         False, ['T2: transport-failed']),
        ('判据/负样本：预期被拒的却没被拒',
         [_r('T1', MEASURED, True), _r('T6', EXPECTATION_MISSED)],
         False, ['T6: expectation-missed']),
        ('判据/负样本：一条数据都没测到（全部是 expected-reject）',
         [_r('T6', EXPECTED_REJECT), _r('T7', EXPECTED_REJECT)],
         False, ['ALL: nothing-measured', 'ALL: nothing-compared']),
        ('判据/负样本：测到了但一条比对都没做',
         [_r('T3', MEASURED)],
         False, ['ALL: nothing-compared']),
    ]
    for name, results, expect_ok, expect_failures in cases:
        ok, failures, _counts = _judge(results)
        matched = (ok == expect_ok) and (sorted(failures) == sorted(expect_failures))
        detail = [] if matched else ('expected: ok=%s failures=%s' % (expect_ok, expect_failures),
                                     'actual  : ok=%s failures=%s' % (ok, failures))
        _record(name, matched, detail)

    # ── 第二组：主流程末尾那一小段 ──────────────────────────────────────────
    inside = os.path.join(ROOT, 'tools', 'x.txt')
    _record('末尾/仓内路径缩略成相对路径',
            _show_path(inside) == os.path.join('tools', 'x.txt'),
            ['_show_path(%s) = %s' % (inside, _show_path(inside))])

    cross = 'C:\\quan-auto-smoke-does-not-exist\\x.txt'
    same_drive = (os.path.splitdrive(ROOT)[0].lower()
                  == os.path.splitdrive(cross)[0].lower())
    shown = _show_path(cross)
    # 跨盘符那条分支只在 Windows 上可达；同盘时样本只能守「不抛」，所以名字里写明。
    _record('末尾/跨盘符路径不抛异常%s'
            % ('' if same_drive else '（本机走回退分支）'),
            isinstance(shown, str) and bool(shown) and (shown == cross or same_drive),
            ['_show_path(%s) = %s' % (cross, shown)])

    tmp = tempfile.mkdtemp(prefix='tushare-transport-smoke-')
    try:
        target = os.path.join(tmp, 'report.txt')
        payload = ['第一行', '第二行']
        ok, message = _write_report(target, payload)
        with io.open(target, 'rb') as handle:
            raw = handle.read()
        _record('末尾/报告是 UTF-8 + LF + 末尾换行',
                ok and message == ''
                and raw == ('\n'.join(payload) + '\n').encode('utf-8')
                and b'\r' not in raw
                and not raw.startswith(b'\xef\xbb\xbf'),
                ['ok=%s message=%r bytes=%r' % (ok, message, raw)])

        blocker = os.path.join(tmp, 'regular-file')
        with io.open(blocker, 'w', encoding='utf-8'):
            pass
        _record('末尾/不可写的 --report= 判失败且带上原因（fail-closed）',
                _write_report(os.path.join(blocker, 'x.txt'), payload)[0] is False,
                ['_write_report(文件下的路径) = %r'
                 % (_write_report(os.path.join(blocker, 'x.txt'), payload),)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out('selftest: %s (%d/%d case(s) as expected)'
        % ('OK' if not bad else 'FAIL', total - bad, total))
    return 0 if not bad else 2


# ── 主流程 ───────────────────────────────────────────────────────────────────

def _usage():
    return ('usage: python tools/tushare_transport_smoke.py [--selftest] '
            '[--report=PATH] [--env-file=PATH] [--symbols=A.SH,B.SZ] [--endpoint=URL]')


def main(argv):
    global TUSHARE_API
    report_path = REPORT_PATH
    selftest = False
    env_file = None
    endpoint = None
    symbols = list(DEFAULT_SYMBOLS)
    for arg in argv[1:]:
        if arg == '--selftest':
            selftest = True
        elif arg.startswith('--report='):
            report_path = arg.split('=', 1)[1]
        elif arg.startswith('--env-file='):
            env_file = arg.split('=', 1)[1]
        elif arg.startswith('--endpoint='):
            endpoint = arg.split('=', 1)[1]
        elif arg.startswith('--symbols='):
            symbols = [name for name in arg.split('=', 1)[1].split(',') if name]
        elif arg in ('-h', '--help'):
            print(__doc__)
            return 0
        else:
            print('unknown argument: %s' % _ascii(arg))
            print(_usage())
            return 2

    if selftest:
        # 自测**不联网、不写仓库里的报告** —— 它的结论只关于本工具的判据与末尾那一段。
        print('mode: selftest (no network, no report)')
        return _selftest()

    if not symbols:
        print('--symbols= 给了一个空清单')
        print(_usage())
        return 2

    # `--endpoint=`：同改本工具的 `ENDPOINT` 与 `datasources.TUSHARE_API`（适配器走的是后者，
    # 只改一处的话 T2 会去打真端点）。**本模块的裸名字也要一起改** —— 2026-09-29 实测：
    # T6 曾经引用 `TUSHARE_API`，于是 `--endpoint=` 指向假源时它仍然打了真端点。
    # `stub` 一为真，报告与控制台都带 `STUB`。
    global ENDPOINT
    stub = bool(endpoint) and endpoint.rstrip('/') != TUSHARE_API.rstrip('/')
    if stub:
        ENDPOINT = endpoint
        TUSHARE_API = endpoint
        datasources.TUSHARE_API = endpoint

    lines = []
    lines.append('tushare 传输层冒烟（人工执行，不是门禁）')
    lines.append('run at: %s' % datetime.datetime.now().isoformat(timespec='seconds'))
    lines.append('python: %s' % sys.version.split()[0])
    lines.append('seam: quanauto.datasources._http_post_json（标准库 urllib；'
                 'tushare **只吃 POST**）')
    lines.append('timeout: %ss' % HTTP_TIMEOUT_SECONDS)
    lines.append('TUSHARE_API: %s%s'
                 % (ENDPOINT, '' if not stub else '   <== --endpoint= 覆盖了它，本次不是真实券源'))
    if stub:
        lines.append('!! STUB ENDPOINT：本报告是**离线自检**（本机假服务端），')
        lines.append('   它的任何结论都**不适用于**真实券源 —— 只能用来证明本工具自己能跑通。')
    lines.append('api_name: %s' % TUSHARE_ADJ_FACTOR_API)
    lines.append('TUSHARE_AUTH_CODE: %s（本工具用坏令牌核对它，见 T6）' % TUSHARE_AUTH_CODE)
    lines.append('映射表 TUSHARE_ADJ_FACTOR: %s'
                 % json.dumps(TUSHARE_ADJ_FACTOR, ensure_ascii=False, sort_keys=True))
    lines.append('urlopen 实际会用的代理（urllib.request.getproxies()）: %s'
                 % (urllib.request.getproxies() or '(none)'))
    lines.append('  ↑ Windows 上这个值来自**注册表**里的系统代理，curl 则只看环境变量。'
                 '两者不一致时，同一台机器上 curl 200 / python 000 是完全正常的，'
                 '不要据此说「端点不稳」。')
    lines.append('')

    # 凭证：**先解析，再决定要不要跑**。拿不到就退 1 且一个字节都不写（fail-closed）——
    # 一份写着「未执行」的报告进仓，下一个人看到的是「有个文件」而不是「没跑过」。
    try:
        token = tushare_token(env_file=env_file)
    except SourceAdapterError as exc:
        print('NO CREDENTIAL -- nothing was probed, no report was written.')
        print('  %s' % _ascii(str(exc)))
        print('  变量名：%s（本工具只读这个名字，不读值）' % TUSHARE_TOKEN_ENV)
        print('EXIT=1')
        return 1

    if os.environ.get(TUSHARE_TOKEN_ENV, '').strip():
        source = '环境变量 %s' % TUSHARE_TOKEN_ENV
    elif env_file:
        source = '--env-file=%s' % env_file
    else:
        source = '项目根 %s' % ENV_FILE_NAME
    # 把解析出来的凭证放回环境变量：适配器内部走的是 `tushare_token()` 那条读取路径，
    # 不这样做的话 `--env-file=` 只会对 T1/T3~T6 生效，T2 会去读**另一份**凭证 ——
    # 一份报告里出现两种凭证来源。值只在进程内存里，不进报告、不进任何输出。
    os.environ[TUSHARE_TOKEN_ENV] = token
    lines.append('凭证来源: %s' % source)
    lines.append('  ↑ 本文件与本次输出里**只有变量名，没有值**；请求体里的 token 字段')
    lines.append('    一律写成 <redacted:%s>。' % TUSHARE_TOKEN_ENV)
    lines.append('')
    lines.append('本文件能证明的：这条通道在**本机这份凭证下、此刻**可用，真实返回的列名')
    lines.append('与 `TUSHARE_ADJ_FACTOR` 对不对得上，以及 has_more / 空区间 / 不存在的')
    lines.append('标的 / 鉴权失败码各自的真实表现。')
    lines.append('本文件**不能**证明的：写侧与读侧（附录 B21.3）、另外三条数据面、')
    lines.append('别人的凭证、以及「以后也这样」。')
    lines.append('')

    # 统一的探针调用形状：一条探针自己的兜底，不许把工具打崩（打崩了就没有报告）。
    plan = (('T1', _probe_i2c, (token,)),
            ('T2', _probe_multi_symbol_year, (symbols, DEFAULT_START, DEFAULT_END)),
            ('T3', _probe_has_more, (token,)),
            ('T4', _probe_empty_window, (token,)),
            ('T5', _probe_missing_symbol, (token,)),
            ('T6', _probe_bad_token, ()),
            ('T7', _probe_reversed_window, ()))
    results = []
    for label, probe, args in plan:
        lines.append('')
        try:
            results.append(probe(lines, *args))
        except Exception as exc:  # noqa: BLE001
            lines.append('    PROBE CRASHED: %s: %s' % (type(exc).__name__, exc))
            results.append(_r(label, TRANSPORT_FAILED,
                              note='探针自己崩了：%s' % type(exc).__name__))

    ok, failures, counts = _judge(results)

    lines.append('')
    lines.append('[汇总] 探针结果')
    for result in results:
        lines.append('    %s  %s%s%s' % (result['label'], result['outcome'],
                                         '  (含列名比对)' if result['compares'] else '',
                                         '  — %s' % result['note'] if result['note'] else ''))
    lines.append('    计数: %s'
                 % '  '.join('%s=%d' % (name, counts[name]) for name in sorted(counts)))
    lines.append('    做过列名比对的探针: %d'
                 % sum(1 for r in results if r['outcome'] == MEASURED and r['compares']))
    if failures:
        lines.append('    判据不通过（%d）:' % len(failures))
        for failure in failures:
            lines.append('      - %s' % failure)
    lines.append('')
    lines.append('verdict: %s%s%s' % ('SMOKE OK' if ok else 'SMOKE FAIL',
                                      '  [STUB ENDPOINT -- 不是真实券源]' if stub else '',
                                      '' if ok else '  (%s)' % '; '.join(failures)))

    try:
        written, message = _write_report(report_path, lines)
    except Exception as exc:  # noqa: BLE001 —— 写报告这一步也不许把工具打崩
        written, message = False, '%s: %s' % (type(exc).__name__, exc)
    if not written:
        print('REPORT NOT WRITTEN: %s' % _ascii(message))
        print('  --report= 的路径不可写时不退回默认路径（否则会覆盖上一版快照）')
        print('verdict: SMOKE FAIL')
        print('EXIT=1')
        return 1

    for result in results:
        print('probe %s %s' % (result['label'], result['outcome']))
    for failure in failures:
        print('FAILURE %s' % failure)
    print('verdict: %s%s' % ('SMOKE OK' if ok else 'SMOKE FAIL',
                             ' [STUB ENDPOINT]' if stub else ''))
    print('report: %s' % _ascii(_show_path(report_path)))
    print('EXIT=%d' % (0 if ok else 1))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
