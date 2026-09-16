"""解密公式派生指标 (趋势擒龙 / 资金动能) 的单元测试。

对拍对象是「直译源脚本」的参考实现 (逐 bar 纯 Python), 用于保证 numpy
实现与 ``qushiqinlong/选股_趋势擒龙.py`` 语义逐位一致。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.indicators.formula_signals import (
    barslast,
    barslastcount,
    capital_momentum,
    trend_dragon,
)

# ===== 参考实现 (直译源脚本, 故意不复用被测代码) =====


def _ref_barslastcount(cond: list[bool]) -> list[int]:
    run, out = 0, []
    for flag in cond:
        run = run + 1 if flag else 0
        out.append(run)
    return out


def _ref_barslast(cond: list[bool]) -> list[int]:
    last, out = -1, []
    for i, flag in enumerate(cond):
        if flag:
            last = i
        out.append(i - last if last >= 0 else -1)
    return out


def _ref_trend_dragon(
    open_: list[float], high: list[float], close: list[float]
) -> tuple[list[bool], list[int]]:
    n = len(close)
    signal = [False] * n
    a3 = [-1] * n
    if n < 20:
        return signal, a3

    a1 = [False] * n
    for i in range(4, n):
        a1[i] = close[i] > close[i - 4]

    a2 = [count == 9 for count in _ref_barslastcount(a1)]
    a3 = _ref_barslast(a2)

    ma10 = [float("nan")] * n
    for i in range(9, n):
        window = close[i - 9 : i + 1]
        ma10[i] = sum(window) / 10

    for i in range(n):
        v = a3[i]
        if v < 1:
            continue
        c, h, m10 = close[i], high[i], ma10[i]
        if np.isnan(m10):
            continue

        hit = False
        if 1 <= v <= 3:
            j = i - v
            if j >= 0 and c > m10 and h > high[j] and close[j] < open_[j]:
                hit = True
        if not hit and 2 <= v <= 4:
            j, j2 = i - v, i - (v - 1)
            if j >= 0 and j2 >= 0 and c > m10 and h > high[j] and close[j2] < open_[j2]:
                hit = True
        if not hit and 3 <= v <= 4:
            j, j3 = i - v, i - (v - 2)
            if j >= 0 and j3 >= 0 and c > m10 and h > high[j] and close[j3] < open_[j3]:
                hit = True
        if not hit and v == 5 and i >= 4 and h >= max(high[i - 4 : i + 1]) and c > m10:
            hit = True

        if hit and i > 0 and close[i] >= close[i - 1]:
            signal[i] = True

    return signal, a3


def _random_ohlc(n: int, seed: int):
    rng = np.random.default_rng(seed)
    close = np.cumprod(1 + rng.normal(0.0008, 0.02, n)) * 10.0
    open_ = close * (1 + rng.normal(0, 0.006, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    return open_, high, low, close


# ===== BARSLAST / BARSLASTCOUNT =====


def test_barslast_semantics():
    cond = np.array([False, True, False, False, True, False])
    np.testing.assert_array_equal(barslast(cond), [-1, 0, 1, 2, 0, 1])


def test_barslastcount_semantics():
    cond = np.array([True, True, False, True, True, True])
    np.testing.assert_array_equal(barslastcount(cond), [1, 2, 0, 1, 2, 3])


# ===== 趋势擒龙 =====


def test_trend_dragon_short_series_never_signals():
    n = 12
    signal, a3 = trend_dragon(np.ones(n), np.ones(n), np.ones(n), np.ones(n))
    assert not signal.any()
    assert (a3 == -1).all()


@pytest.mark.parametrize("seed", [1, 7, 20260916, 42, 99])
def test_trend_dragon_matches_reference(seed: int):
    open_, high, low, close = _random_ohlc(400, seed)
    signal, a3 = trend_dragon(open_, high, low, close)
    ref_signal, ref_a3 = _ref_trend_dragon(open_.tolist(), high.tolist(), close.tolist())
    np.testing.assert_array_equal(a3, np.array(ref_a3))
    np.testing.assert_array_equal(signal, np.array(ref_signal))


def test_trend_dragon_actually_produces_hits():
    """随机数据里必须真的出现过信号, 否则上面的一致性测试是空跑。"""
    total = 0
    for seed in range(30):
        open_, high, low, close = _random_ohlc(500, seed)
        signal, _ = trend_dragon(open_, high, low, close)
        total += int(signal.sum())
    assert total > 0


# ===== 资金动能 =====


def _ref_capital_momentum(close: list[float], index_close: list[float]) -> list[float]:
    out = [float("nan")] * len(close)
    rs = [c / ic * 1_000_000.0 for c, ic in zip(close, index_close, strict=True)]
    for i in range(51, len(rs)):
        window = rs[i - 51 : i + 1]
        if any(np.isnan(x) for x in window):
            continue
        mean = sum(window) / 52
        if mean == 0:
            continue
        out[i] = (rs[i] / mean - 1) * 10
    return out


def test_capital_momentum_insufficient_window_is_nan():
    close = np.linspace(10, 11, 51)
    index_close = np.linspace(3000, 3100, 51)
    momentum = capital_momentum(close, index_close)
    assert np.isnan(momentum).all()


def test_capital_momentum_matches_reference():
    _, _, _, close = _random_ohlc(300, 2026)
    rng = np.random.default_rng(7)
    index_close = np.cumprod(1 + rng.normal(0.0005, 0.01, close.shape[0])) * 3000.0
    momentum = capital_momentum(close, index_close)
    ref = np.array(_ref_capital_momentum(close.tolist(), index_close.tolist()))
    np.testing.assert_allclose(momentum, ref, rtol=1e-9, atol=1e-9, equal_nan=True)
    assert np.isfinite(momentum[51:]).all()


def test_capital_momentum_nan_index_window_is_nan():
    """指数缺数据的那一段窗口整体作废, 不得用残缺窗口偷算。"""
    close = np.linspace(10, 12, 120)
    index_close = np.linspace(3000, 3200, 120)
    index_close[70] = np.nan
    momentum = capital_momentum(close, index_close)
    # 早于该缺口收口的窗口仍可计算
    assert np.isfinite(momentum[51:70]).all()
    # 覆盖到 index 70 的窗口 (i>=70 且 i-51<=70) 全部作废
    assert np.all(np.isnan(momentum[70:]))
