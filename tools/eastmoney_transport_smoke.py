"""东方财富传输层冒烟：**人工执行，不是门禁**。

为什么单独一个脚本而不是一条 pytest：这里要的是「真实网络上的真实响应长什么样」，
它不可重复（源随时改版、限流、断网），把它写成用例就等于把外网变成红的理由 ——
本仓库明确不许这样（C4：门禁不许把「装不装/连不连得上」变成能不能跑的条件）。

它做两件事，两件都不是「约束已验证」：

1. 证明 **唯一网络出口** `quanauto.datasources._http_get_json` 在真实 HTTPS 上可用
   （参数拼装、UA、解帧、envelope 形状）；
2. 把四个已知端点的**响应形状**（列名集合、`success` 标志、分页字段）写进
   `tools/eastmoney-transport-smoke-report.txt`，让「映射表与真实返回对不对得上」
   这件事有个**可复查**的记录，而不是我口头说试过了。

判定规则：**只看数据中心通路**（`datacenter-web`）—— 那才是适配器要用的端点。
kline 探测只为了记录字段顺序，它的成败不影响适配器（契约 §3.2 的表里东财**不**
覆盖行情），但成败两态都写进报告，包括「默认走代理失败 / 绕开代理成功」这种差异。

报告头部会记下 `urllib.request.getproxies()` 的实际取值。这不是装饰：Windows 上
`getproxies()` 会读**注册表**里的系统代理，而 curl 只看环境变量 —— 同一台机器上
两者走的路径可以不同，不写下来就会把「代理导致的不一致」当成「端点不稳」。

用法：
    python tools/eastmoney_transport_smoke.py            # 真联网，写报告
    python tools/eastmoney_transport_smoke.py --report=PATH

退出码：0 = 传输通路可用且报告已写出；1 = 通路不可用或**提取为空**（提取为空时报告
本身就没有内容，此时报 PASS 就是最典型的假绿，所以直接判 FAIL）。
控制台只打 ASCII —— 本机是 GBK 代码页，非 GBK 字符会让 Python 抛 UnicodeEncodeError。
"""

from __future__ import annotations

import datetime
import io
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from quanauto.datasources import (  # noqa: E402  （必须在 sys.path 调整之后）
    EASTMONEY_DATA_API,
    EASTMONEY_FINANCIAL,
    HTTP_TIMEOUT_SECONDS,
    _http_get_json,
)

REPORT_PATH = os.path.join(ROOT, 'tools', 'eastmoney-transport-smoke-report.txt')

KLINE_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
KLINE_PARAMS = {
    'secid': '1.600000',                     # 1 = 上交所，0 = 深交所
    'fields1': 'f1,f2,f3,f4,f5,f6',
    'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58',
    'klt': '101',                            # 101 = 日线
    'fqt': '0',                              # 0 = 不复权
    'beg': '20240102',
    'end': '20240110',
}

# 数据中心三个 report。前两个是**已实测的响应形状**，第三个是**预期被拒**的对照：
# `RPT_INDEX_TS_COMPONENT` 实测返回 `success=false`，正好用来说明「HTTP 200 也可能是失败」。
DATACENTER_PROBES = (
    ('RPT_LICO_FN_CPD', '(SECURITY_CODE="600000")'),
    ('RPT_DMSK_FN_BALANCE', '(SECURITY_CODE="600000")'),
    ('RPT_INDEX_TS_COMPONENT', '(INDEX_CODE="000300")'),
)


def _report_keys(payload):
    """从数据中心响应里取第一页记录的列名集合；取不到就是 None（**不许当成空集**）。"""
    if not isinstance(payload, dict):
        return None
    result = payload.get('result') or {}
    rows = result.get('data') or []
    if not rows or not isinstance(rows[0], dict):
        return None
    return sorted(rows[0].keys())


def _direct_opener(request, timeout=None):
    """绕开**一切**代理取一次。`_http_get_json` 的 `opener` 参数就是为这种对照留的。

    不用这个的话，「取不到」到底是端点不行、还是本机的代理不行，永远分不清 ——
    而这两件事要采取的动作完全不同（换源 vs 改网络配置）。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def _probe_kline(label, opener, lines):
    payload = _http_get_json(KLINE_URL, KLINE_PARAMS, opener=opener)
    rows = ((payload.get('data') or {}).get('klines') or []) if isinstance(payload, dict) else []
    lines.append('%s  %s' % (label, KLINE_URL))
    lines.append('    params: %s' % ' '.join('%s=%s' % kv for kv in sorted(KLINE_PARAMS.items())))
    lines.append('    rc=%s  data.code=%s  klines=%d'
                 % (payload.get('rc'), (payload.get('data') or {}).get('code'), len(rows)))
    if not rows:
        lines.append('    !! 提取为空：klines 一条都没有')
        return None
    lines.append('    fields_per_row=%d  first_row=%s'
                 % (len(rows[0].split(',')), rows[0]))
    lines.append('    last_row=%s' % rows[-1])
    lines.append('    note: klines 元素是「逗号分隔的字符串」，不是 JSON 对象 —— '
                 '解析方式与数据中心接口不同')
    return len(rows)


def _probe_datacenter(index, report, filter_expr, lines):
    params = {
        'reportName': report,
        'columns': 'ALL',
        'filter': filter_expr,
        'pageNumber': 1,
        'pageSize': 2,
        'source': 'WEB',
        'client': 'WEB',
    }
    payload = _http_get_json(EASTMONEY_DATA_API, params)
    lines.append('[%d] datacenter  reportName=%s  filter=%s'
                 % (index, report, filter_expr))
    lines.append('    params: pageNumber=1 pageSize=2 columns=ALL source=WEB client=WEB')
    lines.append('    success=%s  code=%s  message=%s'
                 % (payload.get('success'), payload.get('code'), payload.get('message')))
    result = payload.get('result') or {}
    lines.append('    pages=%s  records=%d' % (result.get('pages'), len(result.get('data') or [])))
    keys = _report_keys(payload)
    if keys is None:
        lines.append('    !! 提取为空：拿不到任何记录，后面的列名比对无法进行')
        return None
    lines.append('    keys=%d' % len(keys))
    lines.append('    keys: %s' % ', '.join(keys))
    present = [k for k in EASTMONEY_FINANCIAL if k in set(keys)]
    missing = [k for k in EASTMONEY_FINANCIAL if k not in set(keys)]
    lines.append('    EASTMONEY_FINANCIAL 命中 %d/%d' % (len(present), len(EASTMONEY_FINANCIAL)))
    lines.append('    present: %s' % (', '.join(present) or '(none)'))
    lines.append('    missing: %s' % (', '.join(missing) or '(none)'))
    return set(keys)


def main(argv):
    report_path = REPORT_PATH
    for arg in argv[1:]:
        if arg.startswith('--report='):
            report_path = arg.split('=', 1)[1]

    lines = []
    lines.append('东方财富传输层冒烟（人工执行，不是门禁）')
    lines.append('run at: %s' % datetime.datetime.now().isoformat(timespec='seconds'))
    lines.append('python: %s' % sys.version.split()[0])
    lines.append('seam: quanauto.datasources._http_get_json (标准库 urllib，唯一网络出口)')
    lines.append('timeout: %ss' % HTTP_TIMEOUT_SECONDS)
    lines.append('EASTMONEY_DATA_API: %s' % EASTMONEY_DATA_API)
    lines.append('urlopen 实际会用的代理（urllib.request.getproxies()）：%s'
                 % (urllib.request.getproxies() or '(none)'))
    lines.append('  ↑ Windows 上这个值来自**注册表**里的系统代理，curl 则只看环境变量。')
    lines.append('  两者不一致时，同一台机器上 curl 200 / python 0 是完全正常的，')
    lines.append('  不要据此说「端点不稳」。')
    lines.append('')
    lines.append('本文件能证明的：端点可达、参数形状可用、envelope 形状如上。')
    lines.append('本文件**不能**证明的：任何一条列名映射是对的 —— 相反，下面的比对就是')
    lines.append('「哪个映射对不上」的直接证据。')
    lines.append('')

    failures = []
    ok = True

    # kline：两种 opener 各探一次。它**不参与判定**（契约 §3.2 的表里东财不覆盖
    # 行情，适配器不会调它），但两种结果都记下来 —— 「默认走代理失败 / 绕开成功」
    # 这种差异本身就是要写进证据的东西。
    for label, opener in (('[1] kline (default opener)', None),
                          ('[1b] kline (direct opener, no proxy)', _direct_opener)):
        lines.append('%s' % label)
        try:
            rows = _probe_kline(label, opener, lines)
        except Exception as exc:  # noqa: BLE001 —— 冒烟工具，任何异常都记进报告
            lines.append('    TRANSPORT FAILED: %s: %s' % (type(exc).__name__, exc))
            continue
        lines.append('    rows=%s' % ('提取为空' if rows is None else rows))
        lines.append('')

    lines.append('  注：kline **不参与判定** —— 契约 §3.2 的表里东财不覆盖行情，适配器不会调它，')
    lines.append('  这里只记录字段顺序。它的可取性也不稳：2026-09-24 同一时段先用 curl 直连')
    lines.append('  拿到过 200/589B，随后 curl 与 python 均 000／RemoteDisconnected。')
    lines.append('  这是**人工交叉核对**（不是本工具测量）—— 别把它的失败当成映射表的问题。')
    lines.append('')

    key_sets = {}
    for index, (report, filter_expr) in enumerate(DATACENTER_PROBES, start=2):
        lines.append('')
        try:
            keys = _probe_datacenter(index, report, filter_expr, lines)
        except Exception as exc:  # noqa: BLE001
            ok = False
            failures.append('%s 传输失败：%s: %s' % (report, type(exc).__name__, exc))
            lines.append('[%d] %s  TRANSPORT FAILED: %s: %s'
                         % (index, report, type(exc).__name__, exc))
            continue
        if keys is None:
            if report == 'RPT_INDEX_TS_COMPONENT':
                lines.append('    (该 report 预期被源拒绝，这里不记为失败)')
                continue
            ok = False
            failures.append('%s 提取为空' % report)
            continue
        key_sets[report] = keys

    lines.append('')
    lines.append('[汇总] EASTMONEY_FINANCIAL 的 9 列在各 report 上的可得性')
    for report, keys in key_sets.items():
        got = [k for k in EASTMONEY_FINANCIAL if k in keys]
        lines.append('    %s: %d/%d' % (report, len(got), len(EASTMONEY_FINANCIAL)))
    if len(key_sets) >= 2:
        union = set()
        for keys in key_sets.values():
            union |= keys
        lines.append('    并集覆盖 %d/%d：%s'
                     % (len([k for k in EASTMONEY_FINANCIAL if k in union]),
                        len(EASTMONEY_FINANCIAL),
                        ', '.join(k for k in EASTMONEY_FINANCIAL if k in union) or '(none)'))
        single = [r for r, keys in key_sets.items()
                  if all(k in keys for k in EASTMONEY_FINANCIAL)]
        lines.append('    单一 report 覆盖全部 9 列的：%s' % (', '.join(single) or '(none)'))
        lines.append('    ⇒ 若为 (none)：EASTMONEY_FINANCIAL 无法由一个 report 喂满，'
                     '映射表与真实返回**对不上**（不是「未验证」，是已证伪）。')

    lines.append('')
    lines.append('verdict: %s%s' % ('SMOKE OK' if ok else 'SMOKE FAIL',
                                    '  (datacenter 通路可用；kline 见 [1]/[1b]，不参与判定)'
                                    if ok else '  (%s)' % '; '.join(failures)))

    with io.open(report_path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write('\n'.join(lines) + '\n')

    for line in lines:
        if line.startswith('verdict') or line.startswith('[') or 'FAILED' in line \
                or line.strip().startswith('!!'):
            print(line.encode('ascii', 'replace').decode('ascii'))
    print('report: %s'
          % os.path.relpath(report_path, ROOT).encode('ascii', 'replace').decode('ascii'))
    print('EXIT=%d' % (0 if ok else 1))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
