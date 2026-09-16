"""``/api/kline/daily`` 的 indicators 挂接逻辑测试 (趋势擒龙 / 资金动能)。

只测挂接层: 字段命名、按日对齐、指数缺失与历史不足时的 null 语义。
公式本身的正确性由 ``test_formula_signals.py`` 对拍覆盖。
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.api.kline import _attach_indicators
from app.indicators.formula_signals import trend_dragon


class _FakeRepo:
    """只实现 get_daily_asset: 股票返回整段历史, 指数按 symbol 查表, 缺失返回空表。"""

    def __init__(
        self,
        stock_rows: list[dict] | None = None,
        index_frames: dict[str, pl.DataFrame] | None = None,
    ):
        self._stock = pl.DataFrame(stock_rows) if stock_rows else pl.DataFrame()
        self._index_frames = index_frames or {}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        if asset_type == "index":
            df = self._index_frames.get(symbol, pl.DataFrame())
            if df.is_empty():
                return df
        else:
            df = self._stock
            if df.is_empty():
                return df
        df = df.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if columns:
            keep = [c for c in columns if c in df.columns]
            df = df.select(keep)
        return df


class _FakeRequest:
    def __init__(self, repo: _FakeRepo, quote_service=None):
        self.app = SimpleNamespace(state=SimpleNamespace(repo=repo, quote_service=quote_service))


def _business_days(n: int, start: date = date(2026, 1, 1)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _stock_rows(n: int = 90, seed: int = 11) -> list[dict]:
    rng = np.random.default_rng(seed)
    close = np.cumprod(1 + rng.normal(0.001, 0.02, n)) * 10.0
    open_ = close * (1 + rng.normal(0, 0.005, n))
    rows = []
    for i, day in enumerate(_business_days(n)):
        rows.append(
            {
                "date": day,
                "open": float(open_[i]),
                "high": float(max(open_[i], close[i]) * 1.01),
                "low": float(min(open_[i], close[i]) * 0.99),
                "close": float(close[i]),
                "volume": 10_000.0,
            }
        )
    return rows


def _index_frame(rows: list[dict], factor: float = 300.0) -> pl.DataFrame:
    """指数收盘 = 个股收盘 * factor → 个股/指数比值恒定 (动能恒为 0)。"""
    return pl.DataFrame(
        {
            "symbol": ["000001.SH"] * len(rows),
            "date": [r["date"] for r in rows],
            "close": [float(r["close"]) * factor for r in rows],
        }
    )


def test_attach_indicators_matches_pure_functions():
    rows = _stock_rows()
    repo = _FakeRepo(rows, {"000001.SH": _index_frame(rows)})
    resp = {"rows": rows}

    out = _attach_indicators(
        _FakeRequest(repo),
        repo,
        resp,
        "603458.SH",
        "trend_dragon,capital_momentum",
        rows[0]["date"],
        rows[-1]["date"],
    )

    payload = out["rows"]
    assert all("td_signal" in r and "td_a3" in r and "cm_value" in r for r in payload)

    close = np.array([r["close"] for r in payload])
    open_ = np.array([r["open"] for r in payload])
    high = np.array([r["high"] for r in payload])
    low = np.array([r["low"] for r in payload])
    signal, a3 = trend_dragon(open_, high, low, close)
    assert [bool(r["td_signal"]) for r in payload] == signal.tolist()
    assert [r["td_a3"] for r in payload] == [int(v) if v >= 0 else None for v in a3]


def test_attach_indicators_momentum_is_zero_when_ratio_is_constant():
    """个股/指数比值恒定 → RS/RS_MA52 = 1 → 动能恒为 0 (第 52 根起)。"""
    rows = _stock_rows(n=80)
    repo = _FakeRepo(rows, {"000001.SH": _index_frame(rows)})
    resp = {"rows": rows}

    out = _attach_indicators(
        _FakeRequest(repo),
        repo,
        resp,
        "603458.SH",
        "capital_momentum",
        rows[0]["date"],
        rows[-1]["date"],
    )

    values = [r["cm_value"] for r in out["rows"]]
    assert values[:51] == [None] * 51, "不足 52 根不得给出数值"
    assert all(v == pytest.approx(0.0) for v in values[51:])


def test_attach_indicators_missing_index_yields_null_momentum():
    rows = _stock_rows(n=80)
    repo = _FakeRepo(rows, {})  # 指数数据缺失
    resp = {"rows": rows}

    out = _attach_indicators(
        _FakeRequest(repo),
        repo,
        resp,
        "603458.SH",
        "capital_momentum",
        rows[0]["date"],
        rows[-1]["date"],
    )
    assert all(r["cm_value"] is None for r in out["rows"])


def test_attach_indicators_short_history_yields_null_momentum():
    rows = _stock_rows(n=40)
    repo = _FakeRepo(rows, {"000001.SH": _index_frame(rows)})
    resp = {"rows": rows}

    out = _attach_indicators(
        _FakeRequest(repo),
        repo,
        resp,
        "603458.SH",
        "capital_momentum",
        rows[0]["date"],
        rows[-1]["date"],
    )
    assert all(r["cm_value"] is None for r in out["rows"])


def test_attach_indicators_uses_shenzhen_benchmark_for_sz_symbols():
    """深市股票应取 399001.SZ; 只提供该指数时也必须能算出动能。"""
    rows = _stock_rows(n=80)
    repo = _FakeRepo(rows, {"399001.SZ": _index_frame(rows)})
    resp = {"rows": rows}

    out = _attach_indicators(
        _FakeRequest(repo),
        repo,
        resp,
        "000001.SZ",
        "capital_momentum",
        rows[0]["date"],
        rows[-1]["date"],
    )
    assert all(r["cm_value"] == pytest.approx(0.0) for r in out["rows"][51:])


def test_attach_indicators_uses_warmup_history_beyond_requested_window():
    """请求窗口只有 5 根, 但仓库有 200 根历史 → 仍靠预热算出数值, 不得整段 null。"""
    all_rows = _stock_rows(n=200)
    repo = _FakeRepo(all_rows, {"000001.SH": _index_frame(all_rows)})
    visible = all_rows[-5:]

    out = _attach_indicators(
        _FakeRequest(repo),
        repo,
        {"rows": visible},
        "603458.SH",
        "trend_dragon,capital_momentum",
        visible[0]["date"],
        visible[-1]["date"],
    )

    assert len(out["rows"]) == 5
    assert all(r["cm_value"] == pytest.approx(0.0) for r in out["rows"])
    assert all("td_signal" in r and "td_a3" in r for r in out["rows"])


def test_attach_indicators_ignores_unknown_keys_and_empty_rows():
    rows = _stock_rows(n=30)
    repo = _FakeRepo(rows, {})
    request = _FakeRequest(repo)

    out = _attach_indicators(
        request,
        repo,
        {"rows": rows},
        "603458.SH",
        "not_a_real_indicator",
        rows[0]["date"],
        rows[-1]["date"],
    )
    assert all("td_signal" not in r and "cm_value" not in r for r in out["rows"])

    empty = _attach_indicators(
        request,
        repo,
        {"rows": []},
        "603458.SH",
        "trend_dragon",
        rows[0]["date"],
        rows[-1]["date"],
    )
    assert empty["rows"] == []
