"""M2 集合竞价: 09:15~09:25 匹配序列 + 开盘量额 + 强度评分。

上游
====
``cli.auctions.series(code)``  -> ``AuctionSeries.points``, 118 点(约每 5 秒一点)::

    time_label      : "09:15:01"
    price           : 当前撮合价(元)
    matched_volume  : **累计**已匹配量(手)
    unmatched_volume: 当前未匹配量(手), 即挂着没成交的单
    unmatched_direction_raw: 未匹配方向

``cli.helpers.auction_data(code)`` -> ``AuctionData``(本次实测 1.33s, 含一次 series 调用)::

    trading_date / pre_close_price / open_price / open_volume / open_amount
    open_change_pct : **百分数**(0.3197 表示 0.3197%), 契约要小数 => /100
    snapshot_0925   : 09:25 的撮合快照

强度评分口径(自建, 非上游提供)
==============================
竞价是"真金白银"最集中的十分钟, 评分看四件事:

1. **加速** accel: 后 1/3 时段的匹配量增量 / 前 1/3 增量。>1 说明越到后面越有人抢。
2. **撤单** cancel_rate: 未匹配量从峰值回落的比例, **只在 09:20 之前统计**。
   09:20 之后规则上不允许撤单, 末端未匹配量必然被撮合掉(实测 600519 末端只剩 1 手),
   把全时段算进去会得到 98% 这种"人人都在撤单"的荒谬结论。
3. **价格稳定** stability: 撮合价相对前收的极差占比, 越小越稳。
4. **开盘量能** open_turnover_bp: 开盘成交额 / 流通市值(基点)。衡量竞价资金体量。

综合分 0~100 = 加速 30 + (1-撤单) 25 + 稳定 20 + 量能 25。分项原样返回,
权重可以后面调 —— **先给分项再加总, 别只给一个数**(用户能自己判断权重是否合理)。
"""

from __future__ import annotations

import logging
from typing import Any

from app.plugins.eltdx.provider import app_to_eltdx
from app.pulse.gateway import client

logger = logging.getLogger(__name__)


def _f(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v


def _float_shares(symbol: str) -> float | None:
    """流通股本(股)。取不到返回 None(量能分项会退化成 0 而不是报错)。"""
    try:
        from app.plugins.eltdx.provider import market_meta
    except Exception:  # pragma: no cover - 维表不可用时仅降级
        return None
    meta = market_meta().get(symbol) or {}
    return _f(meta.get("float_shares"))


def fetch_auction(symbol: str) -> dict:
    """取单只标的当日集合竞价序列与强度评分。"""
    code = app_to_eltdx(symbol)
    empty: dict[str, Any] = {"symbol": symbol, "points": [], "summary": None, "score": None}
    if code is None:
        return empty
    try:
        data = client().helpers.auction_data(code)
    except Exception as e:
        logger.warning("eltdx 竞价数据失败 %s: %s: %s", symbol, type(e).__name__, e)
        return empty

    series = getattr(data, "series", None)
    points: list[dict] = []
    for p in getattr(series, "points", ()) or ():
        points.append({
            "time": str(getattr(p, "time_label", "") or ""),
            "price": _f(getattr(p, "price", None)),
            "matched": _f(getattr(p, "matched_volume", None)),
            "unmatched": _f(getattr(p, "unmatched_volume", None)),
            "unmatched_dir": getattr(p, "unmatched_direction_raw", None),
        })

    pre_close = _f(getattr(data, "pre_close_price", None))
    open_price = _f(getattr(data, "open_price", None))
    open_volume = _f(getattr(data, "open_volume", None))
    open_amount = _f(getattr(data, "open_amount", None))
    change_pct = _f(getattr(data, "open_change_pct", None))
    return {
        "symbol": symbol,
        "date": str(getattr(data, "trading_date", "") or ""),
        "pre_close": pre_close,
        "open_price": open_price,
        "open_volume": open_volume,
        "open_amount": open_amount,
        # 上游是百分数(0.3197), 契约要小数。
        "open_change_pct": (change_pct or 0.0) / 100.0,
        "points": points,
        "score": _score(points, pre_close, open_price, open_amount, _float_shares(symbol)),
    }


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _score(
    points: list[dict],
    pre_close: float | None,
    open_price: float | None,
    open_amount: float | None,
    float_shares: float | None,
) -> dict | None:
    """竞价强度评分。points 太少(<6)时返回 None(数据不足以判断)。"""
    if len(points) < 6:
        return None

    matched = [p["matched"] for p in points if p["matched"] is not None]
    prices = [p["price"] for p in points if p["price"] is not None]

    # 1) 加速: 后 1/3 增量 / 前 1/3 增量。
    accel = None
    if len(matched) >= 6:
        third = max(1, len(matched) // 3)
        head = matched[third] - matched[0]
        tail = matched[-1] - matched[-third]
        accel = (tail / head) if head > 0 else (2.0 if tail > 0 else 0.0)

    # 2) 撤单: 只统计**可撤单期**(09:20 之前)的未匹配量回落。
    #    09:20 后不允许撤单, 末端归零是撮合完成的必然结果, 不是撤单。
    cancel_rate = None
    revocable = [p["unmatched"] for p in points
                 if p["unmatched"] is not None and str(p["time"] or "") < "09:20"]
    if len(revocable) >= 6:
        peak = max(revocable)
        end = revocable[-1]
        cancel_rate = (peak - end) / peak if peak > 0 else 0.0

    # 3) 价格稳定: 撮合价极差 / 前收。
    stability = None
    if len(prices) >= 6 and pre_close:
        spread = (max(prices) - min(prices)) / pre_close
        stability = 1.0 - _clamp01(spread / 0.05)  # 5% 以上极差视为完全不稳

    # 4) 开盘量能: 竞价成交额 / 流通市值(基点)。实测 600519 是 0.13bp,
    #    活跃小票能到 1~2bp, 故满分线定在 2bp(30bp 会让所有票都拿 0 分)。
    turnover_bp = None
    if open_amount is not None and float_shares and pre_close:
        turnover_bp = open_amount / (pre_close * float_shares) * 10_000.0

    def part(value: float | None, full: float) -> float:
        return _clamp01(value / full) if value is not None else 0.0

    s_accel = part(accel, 2.0) * 30.0
    s_cancel = (1.0 - _clamp01(cancel_rate or 0.0)) * 25.0
    s_stable = (stability or 0.0) * 20.0
    s_volume = part(turnover_bp, 2.0) * 25.0  # 2bp 视为满分量能

    return {
        "total": round(s_accel + s_cancel + s_stable + s_volume, 1),
        "accel": accel,
        "cancel_rate": cancel_rate,
        "stability": stability,
        "open_turnover_bp": turnover_bp,
        "parts": {
            "accel": round(s_accel, 1),
            "cancel": round(s_cancel, 1),
            "stability": round(s_stable, 1),
            "volume": round(s_volume, 1),
        },
    }
