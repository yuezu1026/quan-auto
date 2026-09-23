"""命令行入口：`python -m quanauto.cli backtest ...`

存在的理由只有一条：**让「同一条命令跑两次，结果逐字节相同」成为一个可以被机器检查的事实**。
如果只能从 python 里调 `BacktestEngine`，可复现性门禁就得自己写驱动代码，那它测的是
门禁自己的驱动代码，不是引擎。

## 两种输出模式

* 给了 `--out <path>`：报告写进文件，stdout 只打一段**纯 ASCII** 摘要（点位控制在 GBK
  控制台能原样打印的范围内：不用 `↔`、`✅` 这类字符，cp936 下会直接 `UnicodeEncodeError`）。
* 没给 `--out`：整份报告 JSON 打到 stdout（`ensure_ascii=True`），内容与文件里的一模一样。

## 为什么默认 `--slippage-pct 0.002` 而不是 0

默认为 0 的话「换种子结果会不会变」这个问题就恒等于「不会」—— 随机源有没有接进结果
这件事永远测不出来。默认给一个非零滑点，`--seed` 才是一个**有后果**的参数；
要跑纯确定性的回测就显式 `--slippage-pct 0`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from .broker import SimulatedBroker
from .datafeed import CsvDataFeed
from .engine import BacktestEngine, dump_report, report_payload
from .enums import CapitalAllocation
from .errors import QuanAutoError
from .models import (
    BacktestConfig,
    CommissionConfig,
    PositionLimit,
    SlippageConfig,
    StrategyConfig,
    TaxConfig,
)
from .strategies import MA_Cross_Strategy

DEFAULT_SYMBOL = "000001.SZ"
DEFAULT_CAPITAL = 100000.0
DEFAULT_SEED = 0
DEFAULT_SLIPPAGE_PCT = 0.002
DEFAULT_SHORT = 5
DEFAULT_LONG = 20
STRATEGY_CLASS = "MA_Cross_Strategy"
# 策略满仓买入、下一根开盘价又跳高时，会因资金不足被拒单。I1 不做「按可用现金缩量」，
# 所以默认只给策略 90% 的资金留缓冲。想让拒单路径跑起来，把 --strategy-capital 调大即可。
STRATEGY_CAPITAL_RATIO = 0.9


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m quanauto.cli",
        description="quanauto 命令行：跑一次回测并导出确定性报告",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backtest = sub.add_parser(
        "backtest",
        help="跑一次 MA 双均线回测",
        description="读一份 CSV 行情，用 MA(short, long) 跑一次回测，导出报告。",
        allow_abbrev=False,
    )
    backtest.add_argument("--strategy-csv", required=True, metavar="PATH", help="行情 CSV 路径")
    backtest.add_argument("--symbol", default=None, metavar="SYM", help="标的代码，CSV 只有一个标的时可省")
    backtest.add_argument("--short", type=int, default=DEFAULT_SHORT, metavar="N", help="短均线窗口")
    backtest.add_argument("--long", type=int, default=DEFAULT_LONG, metavar="N", help="长均线窗口")
    backtest.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, metavar="X", help="账户初始资金")
    backtest.add_argument(
        "--strategy-capital",
        type=float,
        default=None,
        metavar="X",
        help="分配给策略的资金；默认账户资金的 %d%%（留出现金缓冲）" % round(STRATEGY_CAPITAL_RATIO * 100),
    )
    backtest.add_argument("--seed", type=int, default=DEFAULT_SEED, metavar="N", help="随机种子")
    backtest.add_argument(
        "--slippage-pct",
        type=float,
        default=DEFAULT_SLIPPAGE_PCT,
        metavar="X",
        help="百分比滑点；0 表示不抽随机数（纯确定）",
    )
    backtest.add_argument("--start", default=None, metavar="DATE", help="起始日期 YYYY-MM-DD，默认取行情首日")
    backtest.add_argument("--end", default=None, metavar="DATE", help="结束日期 YYYY-MM-DD，默认取行情末日")
    backtest.add_argument("--risk-free-rate", type=float, default=0.0, metavar="X", help="无风险年化利率")
    backtest.add_argument("--out", default=None, metavar="PATH", help="报告输出路径；不给则打到 stdout")
    backtest.add_argument("--strategy-id", default="ma-cross", metavar="ID", help="策略 id")
    return parser


def pick_symbol(feed: CsvDataFeed, requested: Optional[str]) -> str:
    available = feed.get_available_symbols()
    if not available:
        raise QuanAutoError("行情文件里没有任何标的")
    if requested is None:
        if len(available) > 1:
            raise QuanAutoError(
                "行情文件里有多个标的（%s），请用 --symbol 指定其中一个" % ", ".join(available)
            )
        return available[0]
    if requested not in available:
        raise QuanAutoError("行情文件里没有标的 %s（实际有：%s）" % (requested, ", ".join(available)))
    return requested


def window_from_feed(
    feed: CsvDataFeed, symbol: str, start_text: Optional[str], end_text: Optional[str]
) -> tuple[datetime, datetime]:
    dates = feed.get_available_dates(symbol)
    if not dates:
        raise QuanAutoError("标的 %s 没有任何 K 线" % symbol)
    if start_text is None and end_text is None:
        return dates[0], dates[-1]
    if start_text is None:
        start = dates[0]
    else:
        start = _parse_cli_date(start_text, "--start")
    if end_text is None:
        end = dates[-1]
    else:
        end = _parse_cli_date(end_text, "--end")
    if start > end:
        raise QuanAutoError("--start（%s）晚于 --end（%s）" % (start.date(), end.date()))
    return start, end


def _parse_cli_date(text: str, label: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    raise QuanAutoError("%s 不是合法日期（要 YYYY-MM-DD）：%r" % (label, text))


def resolve_strategy_capital(args: argparse.Namespace) -> float:
    if args.strategy_capital is None:
        return float(args.capital) * STRATEGY_CAPITAL_RATIO
    if args.strategy_capital < 0:
        raise QuanAutoError("--strategy-capital 不能为负")
    return float(args.strategy_capital)


def build_config(args: argparse.Namespace, symbol: str, start: datetime, end: datetime) -> BacktestConfig:
    """组装 `BacktestConfig`。18 个字段一个不落 —— 少一个就是 `TypeError`，
    这是契约里**必填**字段的好处：没人能靠「默认值」把配置项悄悄跳过。"""
    strategy_capital = resolve_strategy_capital(args)
    params = {
        "short_window": int(args.short),
        "long_window": int(args.long),
        "capital": strategy_capital,
        "symbol": symbol,
    }
    slippage = SlippageConfig(
        fixed_slippage=0.0,
        percentage_slippage=float(args.slippage_pct),
        volume_impact_factor=0.0,
        min_slippage=0.0,
        max_slippage=float("inf"),
    )
    return BacktestConfig(
        strategy_configs=[
            StrategyConfig(
                strategy_id=args.strategy_id,
                strategy_class=STRATEGY_CLASS,
                params=dict(params),
                capital=strategy_capital,
            )
        ],
        datafeeds={},
        initial_capital=float(args.capital),
        start_date=start,
        end_date=end,
        commission_config=CommissionConfig(),
        slippage_config=slippage,
        benchmark="",
        benchmark_datafeed=None,
        output_path=args.out or "",
        enable_checkpoint=False,
        checkpoint_interval=0,
        enable_parallel=False,
        max_workers=1,
        risk_free_rate=float(args.risk_free_rate),
        tax_config=TaxConfig(),
        position_limit=PositionLimit(),
        capital_allocation=CapitalAllocation.EQUAL,
    )


def run_backtest(args: argparse.Namespace) -> Dict[str, Any]:
    feed = CsvDataFeed(args.strategy_csv)
    symbol = pick_symbol(feed, args.symbol)
    start, end = window_from_feed(feed, symbol, args.start, args.end)
    config = build_config(args, symbol, start, end)
    engine = BacktestEngine(config)
    engine.add_datafeed(symbol, feed)
    engine.set_broker(
        SimulatedBroker(
            initial_capital=config.initial_capital,
            seed=int(args.seed),
            commission=config.commission_config,
            slippage=config.slippage_config,
        )
    )
    strategy = MA_Cross_Strategy(
        args.strategy_id,
        {
            "short_window": int(args.short),
            "long_window": int(args.long),
            "capital": resolve_strategy_capital(args),
            "symbol": symbol,
        },
    )
    engine.add_strategy(strategy, resolve_strategy_capital(args), dict(strategy.get_strategy_params()))
    result = engine.run()
    payload = report_payload(result, int(args.seed))
    payload["summary"] = {
        "symbol": symbol,
        "bars": len(result.account_history),
        "trades": len(result.trades),
        "orders": len(result.orders),
        "window": [start.isoformat(), end.isoformat()],
    }
    return payload


def format_summary(payload: Dict[str, Any], out_path: str) -> str:
    """纯 ASCII 摘要。数字用 `repr`，不用千分位、不用货币符号 —— 避免任何非 GBK 字符。"""
    summary = payload["summary"]
    perf = payload["deterministic"]["performance"]
    lines = [
        "schema: %s" % payload["schema"],
        "symbol: %s" % summary["symbol"],
        "window: %s .. %s" % (summary["window"][0], summary["window"][1]),
        "bars: %d" % summary["bars"],
        "orders: %d" % summary["orders"],
        "trades: %d" % summary["trades"],
        "final_equity: %r" % perf["final_equity"],
        "total_return: %r" % perf["total_return"],
        "annual_return: %r" % perf["annual_return"],
        "max_drawdown: %r" % perf["max_drawdown"],
        "sharpe_ratio: %r" % perf["sharpe_ratio"],
        "report: %s" % out_path,
    ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "backtest":
        parser.print_help(sys.stderr)
        return 2
    try:
        payload = run_backtest(args)
    except QuanAutoError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1
    except OSError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1
    if args.out:
        try:
            directory = os.path.dirname(os.path.abspath(args.out))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.out, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(dump_report(payload))
        except OSError as exc:
            sys.stderr.write("ERROR: 报告写入失败: %s\n" % exc)
            return 1
        sys.stdout.write(format_summary(payload, args.out))
        return 0
    sys.stdout.write(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
