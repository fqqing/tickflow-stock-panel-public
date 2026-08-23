"""Unit tests for app.markets: market dispatch, symbol normalization, limits, fees, sessions."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from app.markets import (
    ALL_MARKETS,
    CN_TZ,
    MARKET_CN,
    MARKET_HK,
    MARKET_US,
    exchanges_for,
    get_market,
    has_limit,
    is_trading_now,
    lot_size_for,
    market_limit_pct,
    market_of,
    normalize_symbol,
    stamp_tax_double_sided,
    stamp_tax_for,
    trading_session_label,
)

# ── market_of: suffix dispatch ────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", MARKET_CN),
        ("000001.SZ", MARKET_CN),
        ("920344.BJ", MARKET_CN),
        ("00700.HK", MARKET_HK),
        ("AAPL.US", MARKET_US),
    ],
)
def test_market_of_suffix_dispatch(symbol, expected):
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.sh", MARKET_CN),
        ("00700.hk", MARKET_HK),
        ("aapl.us", MARKET_US),
    ],
)
def test_market_of_lowercase_input(symbol, expected):
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000", MARKET_CN),
        ("12345", MARKET_CN),
        ("AAPL", MARKET_US),
        ("ABCD", MARKET_US),
    ],
)
def test_market_of_no_suffix_fallback(symbol, expected):
    # All-digit codes are A shares, alphabetic codes are US by default.
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.XX", MARKET_CN),
        ("ABCD.XX", MARKET_US),
        ("600000.SHX", MARKET_CN),
    ],
)
def test_market_of_unknown_suffix(symbol, expected):
    # Unknown suffix: fallback keyed on the code's first character.
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        (" AAPL.US ", MARKET_US),
        (" 600000.SH ", MARKET_US),
    ],
)
def test_market_of_whitespace_padded_input(symbol, expected):
    # Documenting actual behavior: padded input no longer ends with the
    # suffix, and the leading space is not a digit, so the US branch wins
    # even for numeric A-share codes. Suspected bug, see summary.
    assert market_of(symbol) == expected


def test_market_of_empty_and_none():
    assert market_of("") == MARKET_US
    with pytest.raises(AttributeError):
        market_of(None)


# ── normalize_symbol ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("hk00700", "00700.HK"),
        ("hk700", "00700.HK"),  # zero-padded to 5 digits
        ("AAPL", "AAPL.US"),
        ("aapl", "AAPL.US"),
        ("600000", "600000.SH"),
        ("000001", "000001.SZ"),
        ("900001", "900001.SH"),
        ("300001", "300001.SZ"),
    ],
)
def test_normalize_symbol_common_forms(symbol, expected):
    assert normalize_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("SH600000", "600000.SH"),
        ("sh600000", "600000.SH"),
        ("sz000001", "000001.SZ"),
        ("bj920344", "920344.BJ"),
    ],
)
def test_normalize_symbol_a_share_exchange_prefix(symbol, expected):
    assert normalize_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", "600000.SH"),
        ("00700.HK", "00700.HK"),
        ("AAPL.US", "AAPL.US"),
        (" 600000.SH ", "600000.SH"),
    ],
)
def test_normalize_symbol_idempotent(symbol, expected):
    # Already-normalized symbols round-trip; surrounding whitespace is stripped.
    assert normalize_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("920344", "920344.SH"),  # pure-numeric BJ code falls into the 6/9 -> SH rule
        ("430001", "430001.SZ"),  # pure-numeric BJ code falls into the SZ rule
        ("HK", "00000.HK"),
        ("US", ".US"),
        ("SH", "SH.US"),  # bare "SH" is too short for the prefix branch
        ("US00700", "00700.US"),
        ("600000.XX", "600000.XX.US"),  # unknown suffix gets .US appended
    ],
)
def test_normalize_symbol_quirky_forms(symbol, expected):
    # Documenting actual behavior of edge inputs, not endorsing it.
    assert normalize_symbol(symbol) == expected


# ── market_limit_pct ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", 10.0),
        ("000001.SZ", 10.0),
        ("300001.SZ", 20.0),  # ChiNext
        ("688001.SH", 20.0),  # STAR
        ("689001.SH", 20.0),  # STAR (CDR prefix)
        ("830001.BJ", 30.0),  # Beijing 8xx
        ("430001.BJ", 30.0),  # Beijing 4xx
        ("920344.BJ", 30.0),  # Beijing 920
    ],
)
def test_market_limit_pct_a_share_boards(symbol, expected):
    assert market_limit_pct(symbol) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        ("600001.SH", "ST大王", 5.0),
        ("300001.SZ", "ST创业", 5.0),
        ("300001.SZ", "st公司", 5.0),  # name check is case-insensitive
    ],
)
def test_market_limit_pct_st_name_overrides_board(symbol, name, expected):
    # Actual behavior: any ST name forces 5.0, even on ChiNext/STAR where
    # app.price_limits keeps the 20% board base. See summary for the
    # suspected inconsistency with app.price_limits.
    assert market_limit_pct(symbol, name) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        ("300001.SZ", "普通股", 20.0),
        ("600001.SH", None, 10.0),
    ],
)
def test_market_limit_pct_without_st_name(symbol, name, expected):
    assert market_limit_pct(symbol, name) == pytest.approx(expected)


@pytest.mark.parametrize("symbol", ["AAPL.US", "00700.HK", "aapl.us"])
def test_market_limit_pct_hk_us_has_no_limit(symbol):
    assert market_limit_pct(symbol) is None


# ── fees and trading units ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("market", "tax", "double_sided"),
    [
        (MARKET_CN, 0.0005, False),  # sell side only
        (MARKET_HK, 0.001, True),  # both sides
        (MARKET_US, 0.0, False),  # none
    ],
)
def test_stamp_tax_rates(market, tax, double_sided):
    assert stamp_tax_for(market) == pytest.approx(tax)
    assert stamp_tax_double_sided(market) is double_sided


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, 100),
        (MARKET_HK, 100),
        (MARKET_US, 1),
    ],
)
def test_lot_size_for(market, expected):
    assert lot_size_for(market) == expected


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, True),
        (MARKET_HK, False),
        (MARKET_US, False),
    ],
)
def test_has_limit(market, expected):
    assert has_limit(market) is expected


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, ["SH", "SZ", "BJ"]),
        (MARKET_HK, ["HK"]),
        (MARKET_US, ["US"]),
    ],
)
def test_exchanges_for(market, expected):
    assert exchanges_for(market) == expected


def test_exchanges_for_returns_a_copy():
    result = exchanges_for(MARKET_CN)
    result.append("XX")
    assert get_market(MARKET_CN).exchanges == ("SH", "SZ", "BJ")


# ── trading sessions (deterministic via injected dt) ─────────────────


def _bj(hour: int, minute: int = 0) -> datetime:
    # A fixed weekday in Beijing time; only the wall-clock component matters.
    return datetime(2026, 7, 6, hour, minute, tzinfo=CN_TZ)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 30, True),  # morning open is inclusive
        (9, 29, False),
        (11, 29, True),
        (11, 30, False),  # lunch break starts exactly at 11:30, end is exclusive
        (12, 0, False),
        (12, 59, False),
        (13, 0, True),
        (14, 59, True),
        (15, 0, False),  # close at 15:00, end is exclusive
        (8, 0, False),
    ],
)
def test_is_trading_now_cn_sessions(hour, minute, expected):
    assert is_trading_now(MARKET_CN, _bj(hour, minute)) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 30, True),
        (9, 29, False),
        (11, 59, True),
        (12, 0, False),
        (13, 0, True),
        (15, 59, True),
        (16, 0, False),
    ],
)
def test_is_trading_now_hk_sessions(hour, minute, expected):
    assert is_trading_now(MARKET_HK, _bj(hour, minute)) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (21, 30, True),  # US open (DST window) is inclusive
        (21, 29, False),
        (23, 0, True),
        (3, 59, True),
        (4, 0, True),  # 04:00 belongs to the 22:30-05:00 winter window
        (4, 59, True),
        (5, 0, False),  # all US windows closed at 05:00
        (12, 0, False),
        (20, 0, False),
    ],
)
def test_is_trading_now_us_cross_midnight_sessions(hour, minute, expected):
    # The union of the DST (21:30-04:00) and non-DST (22:30-05:00) windows.
    assert is_trading_now(MARKET_US, _bj(hour, minute)) is expected


def test_is_trading_now_accepts_naive_datetime():
    # Only dt.time() is used, so a naive wall-clock datetime also works.
    assert is_trading_now(MARKET_CN, datetime(2026, 7, 6, 10, 0)) is True
    assert is_trading_now(MARKET_CN, datetime(2026, 7, 6, 16, 0)) is False


def test_is_trading_now_without_dt_returns_bool():
    # Smoke test for the default "now" path: it always yields a bool.
    for market in ALL_MARKETS:
        assert isinstance(is_trading_now(market), bool)


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, "09:30-11:30 / 13:00-15:00"),
        (MARKET_HK, "09:30-12:00 / 13:00-16:00"),
        (MARKET_US, "21:30-04:00 / 22:30-05:00"),
    ],
)
def test_trading_session_label(market, expected):
    assert trading_session_label(market) == expected


# ── registry and error handling ──────────────────────────────────────


@pytest.mark.parametrize(
    "fn",
    [
        exchanges_for,
        has_limit,
        lot_size_for,
        stamp_tax_for,
        stamp_tax_double_sided,
        trading_session_label,
        is_trading_now,
    ],
)
def test_unknown_market_raises_value_error(fn):
    with pytest.raises(ValueError) as excinfo:
        fn("xx")
    assert "xx" in str(excinfo.value)


def test_registry_self_consistency():
    assert ALL_MARKETS == [MARKET_CN, MARKET_HK, MARKET_US]
    for market in ALL_MARKETS:
        meta = get_market(market)
        assert meta.market == market
        assert meta.lot_size > 0
        assert meta.price_round > 0
        assert meta.t_plus in (0, 1)
        assert meta.sessions
        assert all(len(session) == 2 for session in meta.sessions)
        # A market has price limits exactly when it carries a default pct.
        assert has_limit(market) is (meta.default_limit_pct is not None)
        # No tax means no double-sided flag; double-sided implies a tax.
        if meta.stamp_tax == 0:
            assert meta.stamp_tax_double_sided is False
        if meta.stamp_tax_double_sided:
            assert meta.stamp_tax > 0

    cn = get_market(MARKET_CN)
    assert cn.has_limit is True
    assert cn.default_limit_pct == 10.0
    assert cn.t_plus == 1
    assert cn.exchanges == ("SH", "SZ", "BJ")
    assert cn.lot_size == 100

    hk = get_market(MARKET_HK)
    assert hk.t_plus == 0
    assert hk.stamp_tax_double_sided is True
    assert hk.lot_size == 100

    us = get_market(MARKET_US)
    assert us.has_limit is False
    assert us.default_limit_pct is None
    assert us.stamp_tax == 0.0
    assert us.lot_size == 1


def test_market_meta_is_frozen():
    meta = get_market(MARKET_CN)
    with pytest.raises(FrozenInstanceError):
        meta.lot_size = 200
