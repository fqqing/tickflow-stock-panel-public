"""趋势擒龙 — 连续 9 根重心上移后的回调/突破共振 (通达信主图公式)

源脚本 (qushiqinlong/选股_趋势擒龙.py, compute_trend_dragon):

    A1 = C > REF(C, 4)                       # 重心上移
    A2 = BARSLASTCOUNT(A1) == 9              # 连续 9 根重心上移
    A3 = BARSLAST(A2)                        # 距上次 A2 的 bar 数
    XGY = 四组回调突破条件取 OR (依赖 A3 与 MA10)
    信号 = XGY AND C >= REF(C, 1)

四组 OR (A3 是"距上次 9 连阳"的年龄, 越新越靠前):

    1) A3 in [1,3] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3)<REF(O,A3)        回踩那根收阴
    2) A3 in [2,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-1)<REF(O,A3-1)
    3) A3 in [3,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-2)<REF(O,A3-2)
    4) A3 == 5     AND C>MA10 AND H>=HHV(H,5)                                直接创新高

矩阵原生实现 —— 全部算子走 app.backtest.matrix 的 valid_* 族, 与面板
「有效 bar (自动跳停牌日) + 前复权 OHLC」口径一致: REF / BARSLAST /
BARSLASTCOUNT / HHV / MA 全部按有效 bar 计数, 停牌日不计入窗口。

源脚本里另有两条基于事件研究的可选过滤, 这里只移植了乖离率那条:

- ``--max-bias20`` (信号日收盘价相对 MA20 的乖离率上限) -> ``bias20_cap_pct``
- ``--max-momentum`` (资金动能上限) **未移植**: 资金动能 = (C/INDEXC/MA52-1)*10,
  需要给每只股票对齐一条基准指数序列, 回测矩阵里没有这一列 (面板只在个股
  K 线接口按需算它)。要用这一条, 得先给矩阵加基准列, 属另一个改动。
"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_barslast as barslast,
)
from app.backtest.matrix import (
    valid_barslastcount as barslastcount,
)
from app.backtest.matrix import (
    valid_rolling_max as rolling_max,
)
from app.backtest.matrix import (
    valid_shift as shift,
)
from app.backtest.matrix import (
    valid_shift_at as shift_at,
)

_REF_LOOKBACK = 4  # A1: C > REF(C, 4)
_CONTINUOUS_BARS = 9  # A2: 连续 9 根
_MAX_AGE = 5  # A3 只在前 5 根内有意义 (四组条件的取值上限)
_MA_WINDOW = 10  # MA(C, 10)
_NEW_HIGH_WINDOW = 5  # HHV(H, 5)
_DEFAULT_SCAN_DAYS = 5  # 源脚本 --days 默认 5: 近 5 个交易日出现过信号即入选
_MIN_HISTORY = 20  # 源脚本 n < 20 直接返回全 False

# 依赖深度: A2 需要 4+9 根 -> A3 再等 5 根 -> MA10 / HHV5 各自 10 / 5 根; 60 根足够收敛
_WARMUP_BARS = 60

META = {
    "id": "trend_dragon",
    "name": "趋势擒龙",
    "description": "连续9根收盘重心上移后, 距上次9连阳1~5根内出现回调突破或直接创5日新高, 且站稳MA10、收盘不低于昨收(近N个交易日共振)",
    "tags": ["趋势", "突破", "回调", "量价"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "market_cap_min": 10e8,
        "amount_min": 0.5e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "scan_days",
            "label": "近N个交易日内出现信号",
            "type": "int",
            "default": _DEFAULT_SCAN_DAYS,
            "min": 1,
            "max": 20,
            "step": 1,
        },
        {
            "id": "require_above_ma10",
            "label": "要求站稳MA10",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_strong_close",
            "label": "要求收盘不低于昨收",
            "type": "bool",
            "default": True,
        },
        {
            "id": "bias20_cap_pct",
            "label": "MA20乖离率上限%(0=不过滤)",
            "type": "float",
            "default": 0.0,
            "min": 0.0,
            "max": 50.0,
            "step": 1.0,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_trend_dragon"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20


class TrendDragonMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return _WARMUP_BARS

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        close = market.close
        open_ = market.open
        high = market.high
        valid = np.isfinite(close) & np.isfinite(open_) & np.isfinite(high)

        # A1 / A2 / A3 —— 重心上移 -> 连续 9 根 -> 距上次的年龄
        a1 = close > shift(close, _REF_LOOKBACK, valid)
        a2 = barslastcount(a1, valid) == np.float32(_CONTINUOUS_BARS)
        a3 = barslast(a2, valid)

        # REF(X, A3) 三条: 回踩那根的 高/收/开; A3 逐 bar 变化, 用变长取数
        ref_high = shift_at(high, a3, valid)
        ref_close = shift_at(close, a3, valid)
        ref_open = shift_at(open_, a3, valid)
        # REF(X, A3-1) / REF(X, A3-2): 回踩点再往前数一根/两根的 收/开
        ref_close_prev = shift_at(close, a3 - np.float32(1.0), valid)
        ref_open_prev = shift_at(open_, a3 - np.float32(1.0), valid)
        ref_close_prev2 = shift_at(close, a3 - np.float32(2.0), valid)
        ref_open_prev2 = shift_at(open_, a3 - np.float32(2.0), valid)
        # A3 超过 _MAX_AGE 后不再有任何分支命中, 与源脚本一致
        in_range = (a3 >= np.float32(1.0)) & (a3 <= np.float32(_MAX_AGE))

        breakout = (
            # 1) A3 in [1,3]: 突破回踩那根的高点, 且回踩那根收阴
            (in_range & (a3 <= np.float32(3.0)) & (high > ref_high) & (ref_close < ref_open))
            # 2) A3 in [2,4]: 回踩点前一根收阴
            | (
                (a3 >= np.float32(2.0))
                & (a3 <= np.float32(4.0))
                & (high > ref_high)
                & (ref_close_prev < ref_open_prev)
            )
            # 3) A3 in [3,4]: 回踩点前两根收阴
            | (
                (a3 >= np.float32(3.0))
                & (a3 <= np.float32(4.0))
                & (high > ref_high)
                & (ref_close_prev2 < ref_open_prev2)
            )
            # 4) A3 == 5: 直接站上 5 日最高
            | ((a3 == np.float32(_MAX_AGE)) & (high >= rolling_max(high, valid, _NEW_HIGH_WINDOW)))
        )
        # 源脚本里四组分支各自都要求 C > MA10, 提取成公共项; 保险起见先算, 由开关决定是否施加
        above_ma10 = close > matrix_feature(market, "ma10")

        dragon = breakout & valid
        if params.get("require_above_ma10", True):
            dragon &= above_ma10
        if params.get("require_strong_close", True):
            dragon &= valid & (close >= shift(close, 1, valid))

        # 「近 N 个交易日出现过信号」= 信号在有效 bar 上的滚动窗口内出现过
        scan_days = _resolve_scan_days(params)
        entry = rolling_max(dragon.astype(np.float32), valid, scan_days) >= np.float32(0.5)

        bias_cap = _resolve_float(params.get("bias20_cap_pct"), 0.0)
        if bias_cap > 0:
            bias_pct = matrix_feature(market, "ma20_bias") * np.float32(100.0)
            entry &= np.isfinite(bias_pct) & (bias_pct <= np.float32(bias_cap))
        entry &= valid

        ma20 = matrix_feature(market, "ma20")
        previous_close = shift(close, 1, valid)
        exit_ = valid & (close < ma20) & (previous_close >= shift(ma20, 1, valid))

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_trend_dragon",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


def _resolve_scan_days(params: dict) -> int:
    """扫描窗口: 非法值回落到源脚本默认值, 并夹到 [1, 20]。"""
    raw = params.get("scan_days", _DEFAULT_SCAN_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SCAN_DAYS
    return min(max(days, 1), 20)


def _resolve_float(value: object, fallback: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if np.isfinite(number) else fallback


MATRIX_STRATEGY = TrendDragonMatrixStrategy()
