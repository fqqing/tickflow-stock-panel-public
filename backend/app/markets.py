"""市场注册表 — cn / hk / us 市场元数据的单一事实来源。

方案 A 多市场改造的地基：所有「市场特化」判断（symbol 归属、交易时段、
涨跌停、T+N、费率、交易单位）都从这里取，避免各模块各自硬编码。

设计原则（对齐 CONTRIBUTING.md 的数据契约规范）:
  - 交易时段统一按北京时间显式判断，服务器时区不参与业务逻辑。
  - 涨跌停/连板是 A 股专属概念，港美股恒为「无涨跌停」。
  - symbol 格式: A 股 600000.SH / 000001.SZ / 920344.BJ，港股 00700.HK，美股 AAPL.US。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time

CN_TZ = timezone(timedelta(hours=8))

MARKET_CN = "cn"
MARKET_HK = "hk"
MARKET_US = "us"

# symbol 后缀 → 市场
_SUFFIX_MAP = {
    ".SH": MARKET_CN,
    ".SZ": MARKET_CN,
    ".BJ": MARKET_CN,
    ".HK": MARKET_HK,
    ".US": MARKET_US,
}

# 市场列表（管道/API 枚举用）
ALL_MARKETS = [MARKET_CN, MARKET_HK, MARKET_US]


@dataclass(frozen=True)
class MarketMeta:
    market: str
    label: str                     # 中文名
    exchanges: tuple[str, ...]     # TickFlow exchange 代码
    t_plus: int                    # 0 = T+0, 1 = T+1
    stamp_tax: float               # 印花税率（cn 卖出单边 0.05%；hk 双边 0.1%；us 无）
    stamp_tax_double_sided: bool   # 印花税是否买卖双边都收（hk 是）
    lot_size: int                  # 最小交易单位（股/手）
    price_round: float             # 价格精度
    has_limit: bool                # 是否有涨跌停
    default_limit_pct: float | None  # 默认涨跌幅 %（cn 占位 10，实际按板块；港美股 None）
    # 交易时段（北京时间，24h 制）。跨午夜时段用 end < start 表示（美股）。
    sessions: tuple[tuple[dt_time, dt_time], ...]


_META: dict[str, MarketMeta] = {
    MARKET_CN: MarketMeta(
        market=MARKET_CN, label="A股", exchanges=("SH", "SZ", "BJ"),
        t_plus=1, stamp_tax=0.0005, stamp_tax_double_sided=False,
        lot_size=100, price_round=0.01, has_limit=True, default_limit_pct=10.0,
        sessions=((dt_time(9, 30), dt_time(11, 30)), (dt_time(13, 0), dt_time(15, 0))),
    ),
    MARKET_HK: MarketMeta(
        market=MARKET_HK, label="港股", exchanges=("HK",),
        t_plus=0, stamp_tax=0.001, stamp_tax_double_sided=True,
        lot_size=100, price_round=0.01, has_limit=False, default_limit_pct=None,
        sessions=((dt_time(9, 30), dt_time(12, 0)), (dt_time(13, 0), dt_time(16, 0))),
    ),
    MARKET_US: MarketMeta(
        market=MARKET_US, label="美股", exchanges=("US",),
        t_plus=0, stamp_tax=0.0, stamp_tax_double_sided=False,
        lot_size=1, price_round=0.01, has_limit=False, default_limit_pct=None,
        # 美东 9:30-16:00 → 北京 21:30-04:00（夏令时）/ 22:30-05:00（冬令时），取两者并集
        sessions=((dt_time(21, 30), dt_time(4, 0)), (dt_time(22, 30), dt_time(5, 0))),
    ),
}


def get_market(market: str) -> MarketMeta:
    meta = _META.get(market)
    if meta is None:
        raise ValueError(f"未知市场: {market}（可选: {ALL_MARKETS}）")
    return meta


def market_of(symbol: str) -> str:
    """从 symbol 后缀解析市场。600000.SH → cn，00700.HK → hk，AAPL.US → us。

    入参先 strip：带首尾空白的 symbol 会导致 endswith 后缀匹配失败并错误
    兜底（" 600000.SH " 曾被判为 us），且与 normalize_symbol 的行为不一致。
    """
    s = symbol.strip().upper()
    for suffix, market in _SUFFIX_MAP.items():
        if s.endswith(suffix):
            return market
    # 无后缀兜底: 6/9 开头→沪(A股)，其他数字→深(A股)，字母→美股
    code = s.split(".")[0]
    if code and code[0].isdigit():
        return MARKET_CN
    return MARKET_US


def normalize_symbol(symbol: str) -> str:
    """把外部常见写法归一化为项目符号。如 hk00700→00700.HK、AAPL→AAPL.US。"""
    s = symbol.strip().upper()
    if s.startswith("HK"):
        return s[2:].zfill(5) + ".HK"
    if s.startswith("US"):
        return s[2:] + ".US"
    if s.startswith(("SH", "SZ", "BJ")) and len(s) > 2:
        code = s[2:]
        if code.isdigit():
            return code + "." + s[:2]
    if s.endswith((".SH", ".SZ", ".BJ", ".HK", ".US")):
        return s
    if s.isdigit():
        # 纯数字: 6/9 开头→沪，否则深（A股）
        return s + (".SH" if s.startswith(("6", "9")) else ".SZ")
    return s + ".US"


def exchanges_for(market: str) -> list[str]:
    return list(get_market(market).exchanges)


# ── 交易时段（北京时间）──────────────────────────────────────────────
def _dt_in_session(t: dt_time, session: tuple[dt_time, dt_time]) -> bool:
    start, end = session
    if end > start:  # 同一天内
        return start <= t < end
    # 跨午夜（美股）
    return t >= start or t < end


def is_trading_now(market: str, dt: datetime | None = None) -> bool:
    """当前（北京时间）是否处于该市场交易时段。"""
    meta = get_market(market)
    dt = dt or datetime.now(CN_TZ)
    return any(_dt_in_session(dt.time(), s) for s in meta.sessions)


def trading_session_label(market: str) -> str:
    meta = get_market(market)
    parts = []
    for start, end in meta.sessions:
        parts.append(f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}")
    return " / ".join(parts)


# ── 涨跌停 / 连板（A 股专属）────────────────────────────────────────
def has_limit(market: str) -> bool:
    return get_market(market).has_limit


def market_limit_pct(symbol: str, name: str | None = None) -> float | None:
    """市场级涨跌幅。A 股按板块规则；港美股恒 None（无涨跌停）。"""
    market = market_of(symbol)
    if not has_limit(market):
        return None
    # A 股板块规则（与 app.price_limits 对齐）
    code = symbol.split(".")[0]
    if name and ("ST" in name.upper()):
        return 5.0
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("8", "4", "920")):
        return 30.0
    return 10.0


# ── 交易费用（回测撮合用）───────────────────────────────────────────
def stamp_tax_for(market: str) -> float:
    return get_market(market).stamp_tax


def stamp_tax_double_sided(market: str) -> bool:
    return get_market(market).stamp_tax_double_sided


def lot_size_for(market: str) -> int:
    return get_market(market).lot_size
