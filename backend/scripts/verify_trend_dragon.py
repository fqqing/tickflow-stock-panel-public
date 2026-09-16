"""对拍: 面板矩阵版「趋势擒龙」 vs 源 pandas 公式逐 bar 参考实现。

读本地 data/kline_daily_enriched (前复权 OHLC) —— 先在矩阵管线上跑一遍策略,
再用 ``app.indicators.formula_signals.trend_dragon`` (源脚本
``qushiqinlong/选股_趋势擒龙.py::compute_trend_dragon`` 的逐 bar 直译) 逐票独立
计算, 最后比较 as_of 当日的命中集合。差异即移植偏差 (BARSLAST / BARSLASTCOUNT /
变长 REF 的有效 bar 语义)。

可选用 ``--baseline`` 传入源脚本的历史扫描结果 CSV
(``qushiqinlong/趋势擒龙_选股结果_*.csv``), 额外报告覆盖率 —— 那份 CSV 出自另一条
数据源 (pytdx), 前复权基准与本地不一致, 因此只作参考不参与判定。

用法 (在 backend 目录下):
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon --as-of 2026-08-07
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon --symbols 600026,002506
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon --baseline ../qushiqinlong/趋势擒龙_选股结果_20260807.csv
"""

from __future__ import annotations

import argparse
import csv
import re
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_market_data_matrix,
)
from app.indicators.formula_signals import trend_dragon
from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY, META

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "kline_daily_enriched"
_SHARE_RE = re.compile(r"\d{6}\.(SH|SZ|BJ)")


def _partition_dates() -> list[date]:
    dates: list[date] = []
    for child in _DATA_DIR.glob("date=*"):
        raw = child.name.partition("=")[2]
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            continue
    return sorted(dates)


def _load_panel(as_of: date, symbols: list[str] | None) -> pl.DataFrame:
    """A 股面板 (kline_daily_enriched 里同时住着港股/美股, 策略 asset_types 只认 A 股)。"""
    frame = (
        pl.scan_parquet(str(_DATA_DIR / "date=*" / "*.parquet"))
        .select(["symbol", "date", "open", "high", "low", "close", "volume"])
        .filter(pl.col("date") <= as_of)
        .filter(pl.col("symbol").str.contains(r"\d{6}\.(SH|SZ|BJ)$"))
        .collect()
    )
    if symbols:
        wanted = set(symbols)
        frame = frame.filter(
            pl.col("symbol").is_in(wanted)
            | pl.col("symbol").str.split(".").list.first().is_in(wanted)
        )
    return frame.sort(["date", "symbol"])


def _reference_hit(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    scan_days: int,
) -> bool:
    """直译源公式, 返回序列末尾 ``scan_days`` 根内是否出现过信号。"""
    signal, _ = trend_dragon(open_, high, low, close)
    if not signal.any():
        return False
    return bool(signal[-scan_days:].any())


def _load_baseline(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig") as handle:
        return {
            row["代码"].zfill(6): str(row.get("信号日期") or "") for row in csv.DictReader(handle)
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=None, help="对拍日期 (默认最新分区)")
    parser.add_argument("--symbols", default=None, help="逗号分隔的股票代码, 默认全市场")
    parser.add_argument(
        "--no-basic-filter",
        action="store_true",
        help="关闭 basic_filter (对拍必须关闭, 否则结果被市值/价格门槛裁剪)",
    )
    parser.add_argument(
        "--scan-days", type=int, default=5, help="信号扫描窗口 (源脚本 --days, 默认 5)"
    )
    parser.add_argument("--baseline", default=None, help="源脚本历史选股结果 CSV, 仅作覆盖率参考")
    parser.add_argument("--show", type=int, default=20, help="打印前 N 个差异")
    args = parser.parse_args()

    dates = _partition_dates()
    if not dates:
        print(f"[ERR] 未找到 enriched 分区: {_DATA_DIR}")
        return 2
    as_of = date.fromisoformat(args.as_of) if args.as_of else dates[-1]
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None

    panel = _load_panel(as_of, symbols)
    if panel.is_empty():
        print(f"[ERR] {as_of} 无数据")
        return 2
    print(
        f"as_of={as_of}  scan_days={args.scan_days}  行数={panel.height}  "
        f"票数={panel['symbol'].n_unique()}  区间={panel['date'].min()} → {panel['date'].max()}"
    )

    market = build_market_data_matrix(panel)
    if not args.no_basic_filter:
        print("[WARN] 未加 --no-basic-filter: 结果会被 basic_filter 裁剪, 仅作冒烟")

    signals = MatrixStrategyPipeline().run(
        MATRIX_STRATEGY,
        market,
        {"scan_days": args.scan_days},
        MatrixPipelineConfig(
            basic_filter={"enabled": not args.no_basic_filter},
            scoring=dict(META["scoring"]),
            order_by=META.get("order_by"),
            descending=bool(META.get("descending", True)),
        ),
    )
    as_of_row = None
    for time_id, label in enumerate(market.timestamp_labels):
        if label[:10] == str(as_of):
            as_of_row = time_id
    if as_of_row is None:
        print(f"[ERR] 时间轴缺 {as_of}")
        return 2
    entry = signals.entry[as_of_row].astype(bool)
    panel_hits = {market.symbols[i] for i in np.flatnonzero(entry)}

    close = market.close
    high = market.high
    low = market.low
    open_ = market.open
    reference_hits: set[str] = set()
    mismatched_suspension: list[str] = []
    skipped = 0
    for asset_id, symbol in enumerate(market.symbols):
        usable = (
            np.isfinite(close[: as_of_row + 1, asset_id])
            & np.isfinite(open_[: as_of_row + 1, asset_id])
            & np.isfinite(high[: as_of_row + 1, asset_id])
            & np.isfinite(low[: as_of_row + 1, asset_id])
        )
        rows = np.flatnonzero(usable)
        if rows.size == 0 or int(rows[-1]) != as_of_row:
            # 目标日停牌/未上市 —— 策略按无效 bar 处理, 必须为 False
            if entry[asset_id]:
                mismatched_suspension.append(symbol)
            continue
        if rows.size < 20:  # 源脚本 n < 20 直接返回全 False
            skipped += 1
            continue
        if _reference_hit(
            open_[rows, asset_id],
            high[rows, asset_id],
            low[rows, asset_id],
            close[rows, asset_id],
            args.scan_days,
        ):
            reference_hits.add(symbol)

    # 参考实现不区分资产类别, 只对 A 股代码比对, 以免港美股/ETF 混进差异清单
    reference_hits = {s for s in reference_hits if _SHARE_RE.fullmatch(s)}

    only_panel = sorted(panel_hits - reference_hits)
    only_reference = sorted(reference_hits - panel_hits)
    print(
        f"\n面板命中 {len(panel_hits)} 只 / 参考命中 {len(reference_hits)} 只 "
        f"(历史不足跳过 {skipped} 只)"
    )
    if mismatched_suspension:
        print(
            f"[DIFF] 目标日停牌却命中 {len(mismatched_suspension)}: "
            f"{mismatched_suspension[: args.show]}"
        )
    print(f"仅面板有 {len(only_panel)}: {only_panel[: args.show]}")
    print(f"仅参考有 {len(only_reference)}: {only_reference[: args.show]}")
    if panel_hits:
        print(f"命中样例: {sorted(panel_hits)[: args.show]}")

    if args.baseline:
        baseline = _load_baseline(Path(args.baseline))
        mine = {s[:6] for s in panel_hits if _SHARE_RE.fullmatch(s)}
        covered = set(baseline) & mine
        print(
            f"\n[基线参考] {args.baseline}: {len(baseline)} 只信号股, "
            f"被本次命中覆盖 {len(covered)} 只 ({100 * len(covered) / max(len(baseline), 1):.1f}%)"
        )
        print(f"  基线有本次没有: {sorted(set(baseline) - mine)[: args.show]}")

    consistent = not only_panel and not only_reference and not mismatched_suspension
    print("\n[OK] 两边完全一致" if consistent else "\n[DIFF] 存在偏差")
    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
