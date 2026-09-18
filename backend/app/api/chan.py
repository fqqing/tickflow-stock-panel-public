"""缠论分析 API。

三个入口:
- ``GET  /api/chan/analysis``  单票完整缠论结构 (笔 / 中枢 / 买卖点), 供 K 线图叠加
- ``POST /api/chan/annotate``  批量标注 (给策略选股结果加「缠论买点」列)
- ``GET  /api/chan/scan``      全市场扫描当日出现指定买点的标的

数据来源统一走 enriched 日线 (前复权)。全市场框架按 lookback 缓存, 避免重复扫描
parquet; 单票结构本身只有 O(bar) 开销, 5400 只全市场约 6 ~ 10s, 缓存后即时返回。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.indicators.chan import analyze, latest_signal
from app.parquet import market_symbol_filter, scan_enriched_parquet
from app.tickflow.repository import enriched_dirname

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chan", tags=["chan"])

# 全市场框架缓存 TTL (秒)。日线数据一天只更新一次, 15 分钟足够新鲜。
_MARKET_CACHE_TTL = 900
_market_cache: dict[int, tuple[float, pl.DataFrame]] = {}
_scan_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_market_lock = threading.Lock()

# 扫描时按上市状态过滤: 太短的序列构不出结构
_MIN_BARS_FOR_SCAN = 60


# ===== 序列化 =====


def _iso(value: Any) -> str:
    return value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)[:10]


def _serialize(analysis, dates: list[str], symbol: str, name: str | None) -> dict[str, Any]:
    """把 ChanAnalysis 转成前端友好的结构 (索引 + 日期双写, 便于按日期对齐)。"""
    strokes = [
        {
            "start_index": s.start_index,
            "end_index": s.end_index,
            "start_date": dates[s.start_index] if s.start_index < len(dates) else None,
            "end_date": dates[s.end_index] if s.end_index < len(dates) else None,
            "start_price": round(s.start_price, 4),
            "end_price": round(s.end_price, 4),
            "direction": s.direction,
        }
        for s in analysis.strokes
    ]
    centers = [
        {
            "start_index": c.start_index,
            "end_index": c.end_index,
            "start_date": dates[c.start_index] if c.start_index < len(dates) else None,
            "end_date": dates[c.end_index] if c.end_index < len(dates) else None,
            "zd": round(c.zd, 4),
            "zg": round(c.zg, 4),
            "stroke_count": c.stroke_count,
        }
        for c in analysis.centers
    ]
    signals = []
    for s in analysis.signals:
        center = analysis.centers[s.center_pos] if s.center_pos is not None and s.center_pos < len(analysis.centers) else None
        signals.append(
            {
                "kind": s.kind,
                "label": s.label,
                "is_buy": s.is_buy,
                "index": s.index,
                "date": dates[s.index] if s.index < len(dates) else None,
                "price": round(s.price, 4),
                "divergence": s.divergence,
                "center_zd": round(center.zd, 4) if center else None,
                "center_zg": round(center.zg, 4) if center else None,
            }
        )
    snap = analysis.snapshot
    sell_snap = analysis.sell_snapshot

    def _snap_payload(item) -> dict[str, Any]:
        return {
            "kind": item.kind,
            "label": item.label,
            "bars_since": item.bars_since,
            "price": round(item.price, 4) if item.price is not None else None,
            "center_zd": round(item.center_zd, 4) if item.center_zd is not None else None,
            "center_zg": round(item.center_zg, 4) if item.center_zg is not None else None,
            "trend": item.trend,
            "text": item.text,
        }

    return {
        "symbol": symbol,
        "name": name,
        "bars": len(dates),
        "dates": dates,
        "trend": analysis.trend,
        "snapshot": _snap_payload(snap),
        "sell_snapshot": _snap_payload(sell_snap),
        "strokes": strokes,
        "centers": centers,
        "signals": signals,
        "counts": {
            "merged": len(analysis.merged),
            "fractals": len(analysis.fractals),
            "strokes": len(strokes),
            "centers": len(centers),
            "signals": len(signals),
        },
    }


# ===== 数据加载 =====


def _enriched_glob(repo) -> str:
    return str(repo.store.data_dir / enriched_dirname("stock", "cn") / "**" / "*.parquet")


def _load_market_frame(repo, lookback: int) -> pl.DataFrame:
    """加载全市场近 ``lookback`` 个交易日所需窗口的 OHLC。

    按日历日近似换算 (交易日约占 5/7), 再在分组时按每只标的取尾部 lookback 根。
    """
    now = time.monotonic()
    with _market_lock:
        hit = _market_cache.get(lookback)
        if hit is not None and now - hit[0] < _MARKET_CACHE_TTL:
            return hit[1]

    cutoff = date.today() - timedelta(days=int(lookback * 1.7) + 30)
    lf = scan_enriched_parquet(_enriched_glob(repo)).filter(pl.col("date") >= cutoff)
    symbol_filter = market_symbol_filter("cn")
    if symbol_filter is not None:
        lf = lf.filter(symbol_filter)
    df = lf.select("symbol", "date", "high", "low", "close").collect().sort(["symbol", "date"])

    with _market_lock:
        _market_cache[lookback] = (time.monotonic(), df)
        # 不同 lookback 各留一份即可, 超过 4 份清理最旧的
        if len(_market_cache) > 4:
            oldest = min(_market_cache.items(), key=lambda kv: kv[1][0])[0]
            _market_cache.pop(oldest, None)
    logger.info("缠论: 全市场框架载入 %d 行 (lookback=%d)", len(df), lookback)
    return df


def _load_symbol_frame(repo, symbol: str, lookback: int) -> pl.DataFrame:
    """单票 OHLC, 用 repo 缓存优先 (命中 0ms), 否则退回全市场框架里筛。"""
    asset_type = repo.resolve_asset_type(symbol)
    if asset_type == "stock":
        df = repo.get_daily(symbol, date.today() - timedelta(days=int(lookback * 1.7) + 30), date.today())
        if not df.is_empty():
            return df.select("date", "high", "low", "close").sort("date").tail(lookback)
    market = _load_market_frame(repo, lookback)
    sub = market.filter(pl.col("symbol") == symbol)
    return sub.select("date", "high", "low", "close")


def _instrument_names(repo) -> dict[str, str]:
    try:
        inst = repo.get_instruments_asset("stock", "cn")
        if inst.is_empty() or "name" not in inst.columns:
            return {}
        return dict(zip(inst["symbol"].to_list(), inst["name"].to_list(), strict=False))
    except Exception as exc:
        logger.debug("缠论: instruments 读取失败: %s", exc)
        return {}


def _iter_symbol_bars(df: pl.DataFrame, lookback: int):
    """按 symbol 切分行情, 产出 ``(symbol, high, low, close)``。

    用 numpy 连续切片而不是 ``partition_by`` —— 后者会对 5000+ 只标的各建一个
    DataFrame, 全市场扫描时开销远大于缠论本身。前提是 df 已按 (symbol, date) 排序。
    """
    if df.is_empty():
        return
    symbols = df["symbol"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    total = symbols.shape[0]
    if total == 0:
        return
    bounds = np.flatnonzero(symbols[1:] != symbols[:-1]) + 1
    starts = np.concatenate((np.zeros(1, dtype=np.int64), bounds))
    ends = np.concatenate((bounds, np.array([total], dtype=np.int64)))
    for start, end in zip(starts.tolist(), ends.tolist(), strict=True):
        if end - start < _MIN_BARS_FOR_SCAN:
            continue
        offset = max(start, end - lookback)
        yield str(symbols[offset]), high[offset:end], low[offset:end], close[offset:end]


def _analyze_arrays(high, low, close, *, strict: bool):
    if close.shape[0] < _MIN_BARS_FOR_SCAN:
        return None
    return analyze(high, low, close, strict=strict)


# ===== 端点 =====


@router.get("/analysis")
def chan_analysis(
    request: Request,
    symbol: str = Query(..., description="标的代码, 如 600519.SH"),
    lookback: int = Query(400, ge=60, le=1500, description="使用的日线根数"),
    strict: bool = Query(True, description="True 严格笔, False 宽松笔"),
):
    """单票完整缠论结构 (笔 / 中枢 / 买卖点), 供 K 线图叠加。"""
    repo = request.app.state.repo
    df = _load_symbol_frame(repo, symbol, lookback)
    if df.is_empty() or df.height < _MIN_BARS_FOR_SCAN:
        raise HTTPException(status_code=404, detail=f"标的 {symbol} 日线数据不足, 无法做缠论分析")
    dates = [_iso(d) for d in df["date"].to_list()]
    analysis = analyze(
        df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), strict=strict
    )
    names = _instrument_names(repo)
    return _serialize(analysis, dates, symbol, names.get(symbol))


class AnnotateRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    lookback: int = Field(400, ge=60, le=1500)
    strict: bool = True
    recent_bars: int = Field(60, ge=1, le=500, description="只保留近 N 根内出现的买点")


def _side_payload(prefix: str, snap, recent_bars: int, side_cn: str) -> dict[str, Any]:
    """把某一方向的快照转成标注行字段 (买点不加前缀, 卖点加 ``sell_``)。"""
    stale = snap.kind is None or snap.bars_since is None or snap.bars_since > recent_bars
    if stale:
        text = f"最近{side_cn}已超 {recent_bars} 根 ({snap.text})" if snap.kind else snap.text
    else:
        text = snap.text
    return {
        f"{prefix}kind": None if stale else snap.kind,
        f"{prefix}label": f"无{side_cn}" if stale else snap.label,
        f"{prefix}bars_since": snap.bars_since,
        f"{prefix}price": round(snap.price, 4) if snap.price is not None else None,
        f"{prefix}trend": snap.trend,
        f"{prefix}text": text,
        f"{prefix}stale": stale,
    }


@router.post("/annotate")
def chan_annotate(request: Request, req: AnnotateRequest):
    """批量标注: 给策略选股结果加「缠论买点」/「缠论卖点」列。

    返回每个标的的**最新买点**与**最新卖点**各自的类型、距今根数与快照文案;
    两侧都无信号的也返回一行, 便于前端统一渲染。买卖点共用同一次分析结果,
    所以加一列卖点不增加任何扫描开销。
    """
    repo = request.app.state.repo
    symbols = [s for s in dict.fromkeys(req.symbols) if s]
    if not symbols:
        return {"items": []}
    if len(symbols) > 800:
        raise HTTPException(status_code=400, detail="单次标注上限 800 只, 请分批请求")

    market = _load_market_frame(repo, req.lookback)
    names = _instrument_names(repo)
    wanted = set(symbols)
    subset = market.filter(pl.col("symbol").is_in(list(wanted)))
    found: dict[str, Any] = {
        symbol: analysis
        for symbol, high, low, close in _iter_symbol_bars(subset, req.lookback)
        if (analysis := _analyze_arrays(high, low, close, strict=req.strict)) is not None
    }

    items: list[dict[str, Any]] = []
    for symbol in symbols:
        name = names.get(symbol)
        analysis = found.get(symbol)
        if analysis is None:
            items.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "kind": None,
                    "label": "数据不足",
                    "bars_since": None,
                    "price": None,
                    "trend": None,
                    "text": "",
                    "stale": True,
                    "sell_kind": None,
                    "sell_label": "数据不足",
                    "sell_bars_since": None,
                    "sell_price": None,
                    "sell_trend": None,
                    "sell_text": "",
                    "sell_stale": True,
                }
            )
            continue
        item: dict[str, Any] = {"symbol": symbol, "name": name}
        item.update(_side_payload("", analysis.snapshot, req.recent_bars, "买点"))
        sell = _side_payload("sell_", analysis.sell_snapshot, req.recent_bars, "卖点")
        if sell["sell_kind"] is None and analysis.snapshot.kind is None:
            sell["sell_label"] = "无卖点"
        item.update(sell)
        items.append(item)
    return {"items": items}


@router.get("/scan")
def chan_scan(
    request: Request,
    kinds: str = Query("3buy", description="逗号分隔的买卖点类型: 1buy,2buy,3buy,1sell,2sell,3sell"),
    lookback: int = Query(320, ge=120, le=1500, description="每只标的使用的日线根数"),
    recent_bars: int = Query(15, ge=1, le=250, description="信号距今不超过 N 根"),
    strict: bool = Query(True),
    limit: int = Query(300, ge=1, le=2000),
):
    """全市场扫描: 找出最近出现指定缠论买卖点的标的。

    ⚠️ 判定用的是「wanted 内最新的那个信号」而不是快照 —— 快照只覆盖买点,
    直接读快照会让卖点扫描恒定返回空 (2026-09-18 修正)。
    """
    wanted = {k.strip() for k in kinds.split(",") if k.strip()}
    valid = {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}
    invalid = wanted - valid
    if invalid:
        raise HTTPException(status_code=400, detail=f"不支持的买卖点类型: {', '.join(sorted(invalid))}")

    repo = request.app.state.repo
    # 全市场扫描是 CPU 密集操作 (5000+ 只 x 300 根), 命中结果缓存直接返回
    cache_key = (tuple(sorted(wanted)), lookback, recent_bars, strict)
    with _market_lock:
        cached = _scan_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _MARKET_CACHE_TTL:
            return cached[1]

    t0 = time.perf_counter()
    market = _load_market_frame(repo, lookback)
    frame_ms = (time.perf_counter() - t0) * 1000
    names = _instrument_names(repo)

    hits: list[dict[str, Any]] = []
    scanned = 0
    for symbol, high, low, close in _iter_symbol_bars(market, lookback):
        analysis = _analyze_arrays(high, low, close, strict=strict)
        if analysis is None:
            continue
        scanned += 1
        snap = latest_signal(analysis.signals, wanted)
        if snap is None:
            continue
        bars_since = max(close.shape[0] - 1 - snap.index, 0)
        if bars_since > recent_bars:
            continue
        center = (
            analysis.centers[snap.center_pos]
            if snap.center_pos is not None and snap.center_pos < len(analysis.centers)
            else None
        )
        label = snap.label
        hits.append(
            {
                "symbol": symbol,
                "name": names.get(symbol),
                "kind": snap.kind,
                "label": label,
                "is_buy": snap.is_buy,
                "bars_since": bars_since,
                "price": round(snap.price, 4),
                "trend": analysis.trend,
                "center_zd": round(center.zd, 4) if center else None,
                "center_zg": round(center.zg, 4) if center else None,
                "text": (
                    f"{label} @ {snap.price:.2f}, 距今 {bars_since} 根"
                    + (" (伴随背驰)" if snap.divergence else "")
                    + (f", 中枢 {center.zd:.2f} ~ {center.zg:.2f}" if center else "")
                ),
            }
        )

    hits.sort(key=lambda h: (h["bars_since"], h["label"], h["symbol"]))
    elapsed = time.perf_counter() - t0
    logger.info(
        "缠论扫描: %d 只 -> %d 命中, 总 %.1fs (框架 %.1fs), lookback=%d strict=%s",
        scanned,
        len(hits),
        elapsed,
        frame_ms / 1000,
        lookback,
        strict,
    )
    payload = {
        "as_of": date.today().isoformat(),
        "kinds": sorted(wanted),
        "strict": strict,
        "scanned": scanned,
        "hit_count": len(hits),
        "elapsed_ms": round(elapsed * 1000),
        "frame_ms": round(frame_ms),
        "items": hits[:limit],
    }
    with _market_lock:
        _scan_cache[cache_key] = (time.monotonic(), payload)
        if len(_scan_cache) > 8:
            oldest = min(_scan_cache.items(), key=lambda kv: kv[1][0])[0]
            _scan_cache.pop(oldest, None)
    return payload
