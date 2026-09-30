"""通达信(eltdx)分钟 K provider。

为什么需要它
============
现用的腾讯 mkline 有三个硬限制(2026-09-28 实测), 直接决定了分时图的可用性:

1. **单次最多约 482 根 bar** => m1 只覆盖约 2 个交易日。用户点开任一历史日期的
   分时图都拿不到数据, 只能看到"是否立即获取最近5日分钟K"的询问框。
2. **不提供成交额** => amount 只能用 ``vol x 100 x close`` 估算。
3. **北交所无分钟数据**。

eltdx(PyPI 包, 通达信 7709 协议)同场景实测(2026-09-30):

| 能力         | 腾讯 mkline        | eltdx                        |
| ------------ | ------------------ | ---------------------------- |
| 1m 历史      | 约 2 个交易日      | **22483 根 = 约 94 交易日**  |
| 成交额       | 不提供(需估算)     | **直接提供 amount**          |
| 北交所       | 不支持             | **支持** (bj920002)          |
| 指数 / ETF   | 不支持             | **支持** (sh000001/sh510300) |
| 批量         | 不支持             | **100 只/请求, 约 0.66s**    |

量纲对拍(600519.SH, 2026-09-29, 240 根 1m bar)::

    volume  合计 26366.0   本地日K 26366     ratio = 1.0000
    amount  合计 3260057824 本地日K 3260060000 ratio = 0.999999
    OHLC    逐项一致, 极值违例 0

接口形态
========
``cli.bars.get(codes, period="1m", count=240, anchor_date="2026-09-29")``
返回 ``{full_code: KlineSeries}``, ``KlineBar`` 的关键字段::

    time: datetime (tz-aware, Asia/Shanghai)
    open / high / low / close: float
    volume_lots: float   <- 手, 与内部口径一致, 直接用
    amount: float        <- 元, 与内部口径一致, 直接用

``anchor_date`` 是窗口**末尾**: ``count=240, anchor=2026-09-29`` 恰好返回
该日 09:31 ~ 15:00 的 240 根, 不多不少。

⚠️ 三个必须记住的坑
====================
1. **不要用 ``all_pages=True``**。服务器在返回空页之前耗尽 ``max_pages`` 会抛
   ``RuntimeError("bars.get reached max_pages before the server returned an
   empty page")`` —— 1m 有 2 万多根, 分页必然踩到。改为按天用
   ``anchor_date`` + ``count`` 精确取窗口。
2. **代码必须带市场前缀**。裸 ``"510300"`` 会 ``ValueError(unable to infer
   market)``, 必须 ``"sh510300"`` / ``"sz159915"`` / ``"bj920002"``。
3. **共享一个 client**。每线程各建 ``TdxClient`` 实测 6 只 2.21s, 共享 client
   并发同样 6 只只要 0.07s。eltdx 内部是连接池 + RLock, 线程安全。

已知边界
========
- 1m 历史约 94 个交易日(实测最早 2026-05-20), 是**滚动窗口**而非永久存档 ——
  要长期历史仍需每日定时落盘累积, 与分钟同步链路同理。
- 非交易日(周末/节假日)用 anchor_date 会返回前一个交易日的尾部数据, 被日期
  过滤掉后为空, 属预期行为(浪费一次空请求, 无害)。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.base import AssetType
from app.market_time import cn_today

logger = logging.getLogger(__name__)

_DATASETS = ("minute",)

#: freq -> eltdx period。eltdx 只认这几档, 传别的会 ValueError(invalid kline period)。
_FREQ_TO_PERIOD: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
}

#: 每个交易日的 bar 数。配合 anchor_date 取窗口时可精确覆盖一天(源端口径与
#: 通达信一致: 上下午分别连续计数, 1m 一天 240 根)。
_BARS_PER_DAY: dict[str, int] = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}

#: 单批代码数。实测 400 只仍稳定, 100 只约 0.66s 且单批失败时损失面小。
_BAR_BATCH = 100

#: 并发批数。共享 client 下 8 并发实测无错误, 再高收益已被单批耗时摊薄。
_MAX_WORKERS = 8

#: 单次 get_minute 最多回补的交易日数。1m 源端约 94 天, 留一点余量。
#: 超出时只取窗口内**最近**的这些天 —— 历史分时按需增量, 不做全量考古。
_MAX_DAYS = 100

_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

#: 面板后缀 -> eltdx 市场前缀。
_SUFFIX_TO_PREFIX: dict[str, str] = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

#: 进程内共享的 eltdx client。建连有成本(每线程自建实测慢约 30 倍), 且
#: 内部是连接池, 复用即可。
_CLIENT: object | None = None
_CLIENT_LOCK = threading.Lock()


def _client() -> object:
    """惰性创建并返回共享的 eltdx TdxClient。"""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                import eltdx

                _CLIENT = eltdx.TdxClient()
    return _CLIENT


def app_to_eltdx(sym: str) -> str | None:
    """600519.SH -> sh600519。非沪深北交易所返回 None(不可用)。

    必须保留市场前缀: eltdx 无法从裸代码推断 ETF/指数所属市场, 会直接
    ValueError。股票虽然能推断, 但统一加前缀可避免分支。
    """
    code, _, suffix = str(sym or "").partition(".")
    prefix = _SUFFIX_TO_PREFIX.get(suffix.upper())
    return prefix + code if prefix and code else None


def _f(raw: object) -> float | None:
    """转 float, 失败或非有限值返回 None。"""
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _trading_days(
    start_time: datetime | None,
    end_time: datetime | None,
    max_days: int = _MAX_DAYS,
) -> list[date]:
    """把时间窗口展开成交易日列表(最新在前), 跳过周末。

    节假日无法离线判定(需要交易日历), 用 anchor_date 请求时会拿到前一交易日
    的尾部数据, 被 ``t.date() != day`` 过滤掉 —— 只是多一次空请求, 无副作用。
    """
    today = cn_today()
    end = today if end_time is None else (
        end_time.date() if isinstance(end_time, datetime) else end_time
    )
    start = end if start_time is None else (
        start_time.date() if isinstance(start_time, datetime) else start_time
    )
    if end > today:
        end = today
    if start > end:
        start = end

    days: list[date] = []
    cur = end
    while cur >= start and len(days) < max_days:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
    return days


def _fetch_one(task: tuple[date, list[tuple[str, str]], str, int]) -> list[dict]:
    """拉一批代码在单个交易日的分钟 bar, 转成内部 schema 的 dict 列表。"""
    day, batch, period, count = task
    codes = [code for _, code in batch]
    try:
        res = _client().bars.get(  # type: ignore[attr-defined]
            codes, period=period, count=count, anchor_date=day,
        )
    except Exception as e:
        logger.debug("eltdx 分钟K拉取失败 (day=%s, %d 只): %s", day, len(codes), e)
        return []
    if not isinstance(res, dict):
        return []

    sym_of = {code: sym for sym, code in batch}
    out: list[dict] = []
    for full_code, series in res.items():
        sym = sym_of.get(full_code)
        if sym is None:
            continue
        bars = getattr(series, "bars", None)
        if not bars:
            continue
        for b in bars:
            t = getattr(b, "time", None)
            # 非交易日的 anchor 会拿到前一交易日的尾部, 这里按日历日精确过滤。
            if t is None or t.date() != day:
                continue
            close = _f(b.close)
            volume = _f(b.volume_lots)
            if close is None or volume is None:
                continue
            open_ = _f(b.open)
            high = _f(b.high)
            low = _f(b.low)
            amount = _f(b.amount)
            out.append(
                {
                    "symbol": sym,
                    # 源端是 tz-aware 北京时间; 与腾讯 provider 一致, 统一落成
                    # 北京墙钟的 naive datetime (custom 源下游不再做时区换算)。
                    "datetime": t.replace(tzinfo=None),
                    "open": close if open_ is None else open_,
                    "high": close if high is None else high,
                    "low": close if low is None else low,
                    "close": close,
                    "volume": volume,
                    # 正常路径 eltdx 直接给 amount(实测与日K ratio 0.999999);
                    # 兜底才用 成交量(手) x 100 x 收盘价 估算。
                    "amount": volume * 100.0 * close if amount is None else amount,
                }
            )
    return out


@dataclass
class _EltdxConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "eltdx"
    display_name: str = "通达信行情(分钟K)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class EltdxMinuteProvider:
    """通达信行情数据源: ``minute``(bars.get, 含真实成交额)。"""

    name = "eltdx"
    builtin = True

    def __init__(self) -> None:
        self.config = _EltdxConfig()
        self.display_name = self.config.display_name

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        return None

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset == "minute":
            df = self.get_minute(symbols or ["600519.SH"], None, None)
            return {
                "provider": self.name,
                "dataset": "minute",
                "rows": df.height,
                "columns": df.columns,
                "preview": df.head(5).to_dicts() if not df.is_empty() else [],
            }
        raise ValueError(f"通达信行情不支持数据集: {dataset}")

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K, 按交易日 + 批次并发。

        与腾讯 provider 的关键差异: 这里**尊重** start_time/end_time, 用
        anchor_date 逐日精确取窗口, 因此历史分时也能拉到(源端约 94 个交易日)。
        """
        if not symbols:
            return pl.DataFrame()

        period = _FREQ_TO_PERIOD.get(str(freq or "").strip().lower())
        if period is None:
            # 90m/120m 等: eltdx 不支持, 返回空让调用方回落(不要猜着聚合)。
            logger.debug("eltdx 不支持周期 %s, 返回空由调用方回落", freq)
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        pairs = [(s, app_to_eltdx(s)) for s in symbols]
        pairs = [(s, c) for s, c in pairs if c]
        if not pairs:
            # 全部被过滤掉时仍回调一次, 否则前端进度条卡在 0。
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        days = _trading_days(start_time, end_time)
        if not days:
            days = [cn_today()]
        count = _BARS_PER_DAY.get(period, 240)

        batches = [pairs[i : i + _BAR_BATCH] for i in range(0, len(pairs), _BAR_BATCH)]
        tasks = [(d, b, period, count) for d in days for b in batches]
        total = len(tasks)

        logger.info(
            "eltdx 分钟K 拉取开始(%d symbols, %d 交易日, period=%s, %d 批)",
            len(pairs), len(days), period, total,
        )
        rows: list[dict] = []
        t0 = time.perf_counter()
        workers = min(_MAX_WORKERS, max(1, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, part in enumerate(pool.map(_fetch_one, tasks)):
                rows.extend(part)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
        logger.info(
            "eltdx 分钟K 拉取完成(%d 行, %.2fs)",
            len(rows), time.perf_counter() - t0,
        )

        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        keep = [c for c in _MINUTE_CANONICAL if c in df.columns]
        return df.select(keep).sort(["symbol", "datetime"])


def availability() -> tuple[bool, str]:
    """探活: 装了包 + 能连通达信主站 + 真能取到 bar。不抛异常。

    在 loader 模块导入时就会被调用, 所以必须短平快: 只取 5 根 bar。
    """
    try:
        import eltdx  # noqa: F401
    except ImportError:
        return False, "未安装 eltdx(需 >=3.1.7), 请在设置页点击安装"
    try:
        res = _client().bars.get(  # type: ignore[attr-defined]
            "sh600519", period="1m", count=5,
        )
    except Exception as e:
        return False, f"eltdx 连接通达信主站失败: {e}"
    bars = getattr(res, "bars", None)
    if not bars:
        return False, "eltdx 已安装但主站返回 0 根 bar(网络不通或被风控)"
    latest = getattr(bars[-1], "time", None)
    stamp = latest.strftime("%Y-%m-%d %H:%M") if latest is not None else "?"
    return True, f"ok (通达信 7709, {len(bars)} bars, latest={stamp})"
