"""eltdx(通达信)分钟K provider 纯逻辑测试(不依赖网络)。

网络相关的部分一律 monkeypatch 掉模块级的 ``_client``, 只验证:
代码映射 / 交易日展开 / 结果字段与量纲 / 非本市场过滤 / 不支持周期回落 /
进度回调 / 数据集声明 / 容错。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.plugins.eltdx import provider as eltdx_mod
from app.plugins.eltdx.provider import (
    _BARS_PER_DAY,
    EltdxMinuteProvider,
    _trading_days,
    app_to_eltdx,
)


class _FakeBar:
    def __init__(self, time, o, h, low, c, vol, amt):
        self.time = time
        self.open = o
        self.high = h
        self.low = low
        self.close = c
        self.volume_lots = vol
        self.amount = amt


class _FakeSeries:
    def __init__(self, bars):
        self.bars = bars


class _FakeBars:
    """记录调用参数, 按构造时的映射返回结果。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[dict] = []

    def get(self, codes, *, period, count, anchor_date):
        self.calls.append(
            {"codes": list(codes), "period": period, "count": count, "anchor_date": anchor_date}
        )
        out = {}
        for c in codes:
            series = self.mapping.get(c)
            if series is not None:
                out[c] = series
        return out


class _FakeClient:
    def __init__(self, mapping):
        self.bars = _FakeBars(mapping)


@pytest.fixture
def provider():
    return EltdxMinuteProvider()


# ---------------- 代码映射 ----------------

def test_app_to_eltdx_maps_a_share_markets():
    assert app_to_eltdx("600519.SH") == "sh600519"
    assert app_to_eltdx("000001.SZ") == "sz000001"
    assert app_to_eltdx("920002.BJ") == "bj920002"


def test_app_to_eltdx_rejects_foreign_markets():
    """港美股必须被过滤, 否则会白烧请求且污染结果。"""
    assert app_to_eltdx("AAPL.US") is None
    assert app_to_eltdx("00700.HK") is None
    assert app_to_eltdx("BTC.US") is None


def test_app_to_eltdx_rejects_malformed():
    assert app_to_eltdx("") is None
    assert app_to_eltdx("600519") is None
    assert app_to_eltdx(".SH") is None


# ---------------- 交易日展开 ----------------

def test_trading_days_skips_weekend():
    # 2026-09-26 是周六, 2026-09-27 周日; 窗口 09-25(周五) ~ 09-28(周一)
    days = _trading_days(datetime(2026, 9, 25), datetime(2026, 9, 28))
    assert days  # 非空
    assert all(d.weekday() < 5 for d in days)


def test_trading_days_newest_first():
    days = _trading_days(datetime(2026, 9, 1), datetime(2026, 9, 10))
    assert days == sorted(days, reverse=True)


def test_trading_days_respects_max_days():
    days = _trading_days(datetime(2026, 1, 1), datetime(2026, 9, 30), max_days=5)
    assert len(days) == 5


def test_trading_days_defaults_to_today_when_none():
    days = _trading_days(None, None)
    assert len(days) == 1
    assert days[0] <= date.today()


# ---------------- 数据字段与量纲 ----------------

def _make_mapping(day: date, codes=("sh600519",)) -> dict:
    bars = [
        _FakeBar(datetime(day.year, day.month, day.day, 9, 31), 10.0, 10.5, 9.9, 10.2, 100.0, 102000.0),
        _FakeBar(datetime(day.year, day.month, day.day, 9, 32), 10.2, 10.4, 10.1, 10.3, 50.0, 51500.0),
    ]
    return {c: _FakeSeries(list(bars)) for c in codes}


def test_get_minute_schema_and_units(provider, monkeypatch):
    day = date(2026, 9, 29)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient(_make_mapping(day)))
    df = provider.get_minute(
        ["600519.SH"],
        datetime(2026, 9, 29, 9, 25),
        datetime(2026, 9, 29, 15, 5),
    )
    assert not df.is_empty()
    for col in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount"):
        assert col in df.columns
    row = df.row(0, named=True)
    # volume 用 volume_lots(手), amount 用上游真实成交额(元), 不做换算
    assert row["volume"] == 100.0
    assert row["amount"] == 102000.0
    assert row["close"] == 10.2


def test_get_minute_datetime_is_naive_beijing(provider, monkeypatch):
    """与腾讯 provider 一致: 落成北京墙钟的 naive datetime, 下游不再换算时区。"""
    day = date(2026, 9, 29)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient(_make_mapping(day)))
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    dt = df["datetime"][0]
    assert dt.tzinfo is None
    assert dt.hour == 9 and dt.minute == 31


def test_get_minute_uses_anchor_date_and_per_day_count(provider, monkeypatch):
    """必须按天用 anchor_date 取窗口(不能用 all_pages, 会抛 RuntimeError)。"""
    day = date(2026, 9, 29)
    fake = _FakeClient(_make_mapping(day))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: fake)
    provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert fake.bars.calls, "应至少发起一次请求"
    call = fake.bars.calls[0]
    assert call["period"] == "1m"
    assert call["count"] == _BARS_PER_DAY["1m"]
    assert call["anchor_date"] == day


# ---------------- 过滤与回落 ----------------

def test_get_minute_filters_foreign_symbols(provider, monkeypatch):
    day = date(2026, 9, 29)
    fake = _FakeClient(_make_mapping(day))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: fake)
    df = provider.get_minute(
        ["AAPL.US", "00700.HK"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5)
    )
    assert df.is_empty()
    assert not fake.bars.calls, "全部被过滤时不应发起任何请求"


def test_get_minute_callbacks_once_when_all_filtered(provider, monkeypatch):
    """全滤空仍要回调一次, 否则前端进度条卡在 0。"""
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient({}))
    seen: list[tuple[int, int]] = []
    provider.get_minute(["AAPL.US"], None, None, on_chunk_done=lambda c, t: seen.append((c, t)))
    assert seen == [(1, 1)]


def test_get_minute_returns_empty_for_unsupported_freq(provider, monkeypatch):
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient({}))
    for freq in ("90m", "120m", "1d"):
        df = provider.get_minute(["600519.SH"], None, None, freq=freq)
        assert df.is_empty(), f"{freq} 应返回空交由调用方回落"


def test_get_minute_empty_symbols(provider):
    assert provider.get_minute([], None, None).is_empty()


def test_get_minute_drops_bars_of_other_days(provider, monkeypatch):
    """非交易日的 anchor 会拿到前一交易日尾部, 必须按日历日过滤掉。"""
    day = date(2026, 9, 29)
    prev = date(2026, 9, 28)
    bars = [
        _FakeBar(datetime(prev.year, prev.month, prev.day, 14, 59), 1.0, 1.0, 1.0, 1.0, 1.0, 100.0),
        _FakeBar(datetime(day.year, day.month, day.day, 9, 31), 10.0, 10.5, 9.9, 10.2, 100.0, 102000.0),
    ]
    monkeypatch.setattr(
        eltdx_mod, "_client", lambda: _FakeClient({"sh600519": _FakeSeries(bars)})
    )
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert df.height == 1
    assert df["datetime"][0].date() == day


# ---------------- 容错 ----------------

def test_get_minute_survives_upstream_error(provider, monkeypatch):
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    class _Cli:
        bars = _Boom()

    monkeypatch.setattr(eltdx_mod, "_client", lambda: _Cli())
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert df.is_empty()


def test_get_minute_falls_back_amount_when_missing(provider, monkeypatch):
    """上游缺 amount 时用 成交量(手) x 100 x 收盘价 估算, 不能整行丢弃。"""
    bars = [_FakeBar(datetime(2026, 9, 29, 9, 31), 10.0, 10.5, 9.9, 10.2, 100.0, None)]
    monkeypatch.setattr(
        eltdx_mod, "_client", lambda: _FakeClient({"sh600519": _FakeSeries(bars)})
    )
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert df.height == 1
    assert df["amount"][0] == pytest.approx(100.0 * 100 * 10.2)


# ---------------- 契约 ----------------

def test_dataset_declaration(provider):
    assert "minute" in provider.config.datasets
    assert provider.name == "eltdx"
    assert provider.builtin is True


def test_close_is_noop(provider):
    assert provider.close() is None


def test_test_dataset_minute(provider, monkeypatch):
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient(_make_mapping(date.today())))
    info = provider.test_dataset("minute", ["600519.SH"])
    assert info["provider"] == "eltdx"
    assert info["dataset"] == "minute"
    assert info["rows"] >= 0


def test_test_dataset_rejects_unknown(provider):
    with pytest.raises(ValueError):
        provider.test_dataset("daily")


def test_bars_per_day_matches_a_share_session():
    """A股一天 4 小时连续竞价: 1m=240, 5m=48, 15m=16, 30m=8, 60m=4。"""
    assert _BARS_PER_DAY["1m"] == 240
    assert _BARS_PER_DAY["5m"] == 48
    assert _BARS_PER_DAY["15m"] == 16
    assert _BARS_PER_DAY["30m"] == 8
    assert _BARS_PER_DAY["60m"] == 4
