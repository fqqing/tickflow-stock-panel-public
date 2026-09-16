"""解密公式派生指标 (逐 bar 精确复刻, 纯函数)。

这些指标不随 enriched parquet 落盘, 而是由 ``/api/kline/daily`` 的
``indicators`` 参数按需计算 (个股弹窗按需点亮), 避免给全市场重建增加负担。

实现口径与源脚本保持一致:
- 趋势擒龙 / 蛟龙出海: ``qushiqinlong/选股_趋势擒龙.py::compute_trend_dragon``
- 资金动能: ``qushiqinlong/选股_趋势擒龙.py::compute_capital_momentum``
  (等价于 ``yanwen/.../tdx_zb_qsql.py`` 中 RS / RS_MA52 / 资金动能 三行)

约定:
- 输入为一维 numpy 数组, 按时间升序, 与 K 线一一对应。
- 输出与输入等长; 语义缺口用 ``np.nan`` / ``-1`` 表示, 由调用方决定如何展示。
"""

from __future__ import annotations

import numpy as np

__all__ = ["barslast", "barslastcount", "capital_momentum", "trend_dragon"]

TREND_CONTINUOUS_BARS = 9
CAPITAL_MOMENTUM_WINDOW = 52
CAPITAL_MOMENTUM_SCALE = 10.0
RS_SCALE = 1_000_000.0


def barslastcount(condition: np.ndarray) -> np.ndarray:
    """BARSLASTCOUNT: 条件连续成立的有效 bar 数 (不成立则归零)。

    与通达信一致: 当前成立计入计数, 因此首次成立为 1。
    """
    cond = np.asarray(condition, dtype=bool)
    out = np.zeros(cond.shape, dtype=np.int64)
    run = 0
    for i in range(cond.shape[0]):
        run = run + 1 if cond[i] else 0
        out[i] = run
    return out


def barslast(condition: np.ndarray) -> np.ndarray:
    """BARSLAST: 距上一次条件成立的 bar 数; 当前成立记 0, 从未成立记 -1。"""
    cond = np.asarray(condition, dtype=bool)
    out = np.full(cond.shape, -1, dtype=np.int64)
    last_true = -1
    for i in range(cond.shape[0]):
        if cond[i]:
            last_true = i
        if last_true >= 0:
            out[i] = i - last_true
    return out


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """滚动均值; 不足 window 根输出 NaN (对应 pandas ``min_periods=window``)。"""
    arr = np.asarray(values, dtype=np.float64)
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    if arr.shape[0] < window:
        return out
    cumsum = np.concatenate(([0.0], np.nancumsum(arr)))
    sums = cumsum[window:] - cumsum[:-window]
    out[window - 1 :] = sums / window
    # 窗口内含 NaN 时整体作废, 避免把停牌缺口当成真实价格
    for i in range(window - 1, arr.shape[0]):
        if np.isnan(arr[i - window + 1 : i + 1]).any():
            out[i] = np.nan
    return out


def trend_dragon(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """趋势擒龙 (主图「蛟龙出海」信号)。

    公式:
        A1 = C > REF(C, 4)
        A2 = BARSLASTCOUNT(A1) == 9          # 连续 9 根满足 A1
        A3 = BARSLAST(A2)                    # 上次 A2 距今的 bar 数
        XGY = 四组回调突破条件取 OR (依赖 A3 与 MA10)
        信号 = XGY AND C >= REF(C, 1)

    返回 ``(signal, a3)``: signal 为 bool 数组; a3 为 A3 原始值 (-1 表示从未成立)。
    ``low`` 参数暂不参与判断, 保留以对齐 K 线入参顺序。
    """
    close = np.asarray(close, dtype=np.float64)
    open_ = np.asarray(open_, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    n = close.shape[0]
    signal = np.zeros(n, dtype=bool)
    a3 = np.full(n, -1, dtype=np.int64)
    if n < 20:
        return signal, a3

    # A1: C > REF(C, 4)
    a1 = np.zeros(n, dtype=bool)
    a1[4:] = close[4:] > close[:-4]

    # A2: 连续 9 根 A1
    a2 = barslastcount(a1) == TREND_CONTINUOUS_BARS

    # A3: 上次 A2 距今 bar 数
    a3 = barslast(a2)

    ma10 = _rolling_mean(close, 10)

    for i in range(n):
        v = int(a3[i])
        if v < 1:
            continue
        c = close[i]
        h = high[i]
        m10 = ma10[i]
        if np.isnan(m10):
            continue

        hit = False

        # 条件1: A3∈[1,3] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3)<REF(O,A3)
        if 1 <= v <= 3:
            j = i - v
            if j >= 0 and c > m10 and h > high[j] and close[j] < open_[j]:
                hit = True

        # 条件2: A3∈[2,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-1)<REF(O,A3-1)
        if not hit and 2 <= v <= 4:
            j = i - v
            j2 = i - (v - 1)
            if j >= 0 and j2 >= 0 and c > m10 and h > high[j] and close[j2] < open_[j2]:
                hit = True

        # 条件3: A3∈[3,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-2)<REF(O,A3-2)
        if not hit and 3 <= v <= 4:
            j = i - v
            j3 = i - (v - 2)
            if j >= 0 and j3 >= 0 and c > m10 and h > high[j] and close[j3] < open_[j3]:
                hit = True

        # 条件4: A3=5 AND H>=HHV(H,5) AND C>MA10
        if not hit and v == 5 and i >= 4 and h >= high[i - 4 : i + 1].max() and c > m10:
            hit = True

        if hit and i > 0 and close[i] >= close[i - 1]:
            signal[i] = True

    return signal, a3


def capital_momentum(close: np.ndarray, index_close: np.ndarray) -> np.ndarray:
    """资金动能 = (RS / RS_MA52 - 1) * 10, 其中 RS = C / INDEXC * 1e6。

    ``close`` 与 ``index_close`` 需已按同一交易日对齐 (缺失日传 NaN)。
    不足 52 根或指数缺失的 bar 输出 NaN。
    """
    close = np.asarray(close, dtype=np.float64)
    index_close = np.asarray(index_close, dtype=np.float64)
    if close.shape != index_close.shape:
        raise ValueError("close 与 index_close 长度不一致")

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = close / index_close * RS_SCALE
    rs_ma = _rolling_mean(rs, CAPITAL_MOMENTUM_WINDOW)
    with np.errstate(divide="ignore", invalid="ignore"):
        momentum = (rs / rs_ma - 1.0) * CAPITAL_MOMENTUM_SCALE
    momentum[~np.isfinite(momentum)] = np.nan
    return momentum
