"""腾讯行情分钟 K provider。

为什么需要它
============
现用的 ``stocksdk``(东方财富) 分钟链路已基本失效, 详见 plugin.yaml。
腾讯 ``mkline`` 是同场景下唯一实测可用的免费源:

    GET https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m1,,320

2026-09-28 实测 ~90~160 只/秒, 沪深 300 只样本 0 失败; 而东财同口径 27.6s 后仍 0 根。

⚠️ 上游行格式的反直觉之处
=========================
腾讯返回的行是::

    ['202609281456', '1243.30', '1243.38', '1243.49', '1242.60', '156.00', {}, '0.12']
       datetime      open       close      high       low        vol(手)  {}   换手率基点

**第 2 位是 close, 不是 high** —— 与常见的 OHLC 顺序不同, 映射错会让最高/最低价
互换, 而因为 high/low 恰好常常包住 open/close, 不显式核对极值关系很难发现。
上面的样例可用 ``high >= max(open, close)`` 且 ``low <= min(open, close)`` 反证。

量纲
====
与项目内部口径(``memory/MEMORY.md``「内部量纲」节)的对照:

| 字段     | 腾讯原始        | 内部口径 | 处理             |
| -------- | --------------- | -------- | ---------------- |
| volume   | 手              | 手       | 直接用           |
| amount   | **不提供**      | 元       | 用 see 下方公式估算 |
| 第 7 位  | 换手率基点      | 非成交额 | **不能用**       |

``amount`` 需自算: ``vol(手) x 100 x price``。实测(600519 2026-09-28, 241 根 m1)
用收盘价估算得 34.87 亿, 本地 enriched 当日 amount = 34.89 亿, 相对差 ~0.06%,
符合 panel 对 amount 的一致性要求。

已知边界
========
- **单次上限约 482 根 bar**: ``count`` 传 <=320 生效, 传更大值会被静默封顶到 320;
  留空反而返回 482 根。故一律留空以拿满。折合 m1 ≈ 2 个交易日 / m5 ≈ 11 个交易日。
- **不支持 beg/end 区间**: 传了返回 0 根(不是报错), 所以本 provider **忽略
  start_time/end_time**, 由下游按 datetime 自行裁剪。
- **不支持批量**: param 拼多只返回空 data, 只能单只请求 + 线程池并发。
- **北交所无分钟数据**: 430/83x/87x/920 各号段实测 mkline 均返回 0 根。
  这些标的自动回落到 stock-sdk(``_bj_fallback``), 失败则静默丢弃 —— 前端分时图
  本就对无数据做了容错。
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import polars as pl

from app.data_providers.base import AssetType

logger = logging.getLogger(__name__)

_DATASETS = ("minute",)

_HOST = "https://ifzq.gtimg.cn"
_MKLINE = f"{_HOST}/appstock/app/kline/mkline"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    # 实测不带也能通, 带上更贴近浏览器来源。
    "Referer": "https://gu.qq.com/",
}
_TIMEOUT_S = 15.0

#: 周期映射。腾讯用 m1/m5/m15/m30/m60, 不支持 n 分钟自定义周期。
_FREQ_TO_PERIOD: dict[str, str] = {
    "1m": "m1",
    "5m": "m5",
    "15m": "m15",
    "30m": "m30",
    "60m": "m60",
}
_DEFAULT_PERIOD = "m1"

#: 并发上限。腾讯实测不限流, 但保守起见控制连接数: 过高会增加被风控的概率,
#: 而收益已被单连接 ~0.15s 的延迟摊薄(workers 24 → 40 实测吞吐几乎不再上升)。
_MAX_WORKERS = 24

#: 北交所前缀 —— 腾讯 mkline 不支持, 需回落到 stock-sdk。
_BJ_SUFFIX = ".BJ"

_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

#: 这几个代码段的分钟 vol 单位是「股」而非「手」(见 parse_bars 的量纲说明)。
_VOL_IN_SHARES_PREFIXES = ("688", "689")


def _ssl_context() -> ssl.SSLContext:
    """弱化校验的 SSL context。

    腾讯财经站点在部分网络环境下证书链不完整, httpx 严格校验会直接失败;
    分钟数据本身非敏感, 这里只为连通性放宽。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL_CTX = _ssl_context()


def app_to_tencent(sym: str) -> str | None:
    """600519.SH -> sh600519。非沪深交易所返回 None(不可用)。"""
    code, _, suffix = str(sym or "").partition(".")
    suffix = suffix.upper()
    if suffix == "SH":
        return "sh" + code
    if suffix == "SZ":
        return "sz" + code
    return None


def _http_get(url: str) -> dict | None:
    """单次 GET + JSON 解析。失败返回 None(不抛)。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.debug("腾讯分钟请求失败: %s | %s", url, e)
        return None
    if "=" in body[:40]:  # jsonp 形态: xxx={...}
        body = body.split("=", 1)[1]
    try:
        return json.loads(body)
    except Exception:
        logger.debug("腾讯分钟返回非 JSON: %s", body[:120])
        return None


def _fetch_bars(tcode: str, period: str) -> list[list]:
    """拉单只标的单周期的分钟 bar 原始行。count 留空以取最大可用跨度。"""
    payload = _http_get(f"{_MKLINE}?param={tcode},{period},,")
    if not payload:
        return []
    node = (payload.get("data") or {}).get(tcode) or {}
    return node.get(period) or []


def _to_float(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def parse_bars(rows: list[list], symbol: str) -> list[dict]:
    """原始行 -> 内部 schema(dict 列表)。amount 由 vol x 100 x close 估算。

    ⚠️ 量纲陷阱: 科创板(688xxx)的 vol 单位是**股**, 其余板块是**手**。
    实测 2026-09-28 分钟合计 / 日 K volume 的比值:
        600519.SH(主板) = 1.0   300750.SZ(创业板) = 1.0   688788.SH(科创板) = 100.0
    不处理的话科创板约 500 只标的的分钟 volume / amount 会整体放大 100 倍。
    """
    vol_div = 100.0 if symbol[:3] in _VOL_IN_SHARES_PREFIXES else 1.0
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        ts = str(row[0] or "")
        # 腾讯形如 202609281456(12 位)。不足 12 位或非数字直接丢弃。
        if len(ts) != 12 or not ts.isdigit():
            continue
        open_ = _to_float(row[1])
        close_ = _to_float(row[2])
        high_ = _to_float(row[3])
        low_ = _to_float(row[4])
        vol = _to_float(row[5])
        if close_ is None or vol is None:
            continue
        try:
            dt = datetime.strptime(ts, "%Y%m%d%H%M")
        except ValueError:
            continue
        out.append(
            {
                "symbol": symbol,
                "datetime": dt,
                "open": open_,
                "high": high_,
                "low": low_,
                "close": close_,
                "volume": vol / vol_div,
                # 腾讯不给成交额(第 7 位是换手率基点, 不是 amount)。
                # 用 成交量(手) x 100 x 收盘价 估算, 实测与本地日 K amount 相对差 ~0.06%。
                "amount": vol / vol_div * 100.0 * close_,
            }
        )
    return out


@dataclass
class _TencentConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "tencent"
    display_name: str = "腾讯行情(分钟K)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class TencentMinuteProvider:
    """腾讯 mkline 分钟数据源。只提供 ``minute`` 数据集。"""

    name = "tencent"
    builtin = True

    def __init__(self) -> None:
        self.config = _TencentConfig()
        self.display_name = self.config.display_name

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        return None

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K。

        ``start_time`` / ``end_time`` 由调用方传达过来但**上游不支持区间参数**
        (传了会返回 0 根), 故这里忽略, 一律取最近约 482 根 bar, 由下游自行裁剪。
        """
        if not symbols:
            return pl.DataFrame()

        period = _FREQ_TO_PERIOD.get(str(freq or "").strip().lower(), _DEFAULT_PERIOD)
        routed = [(s, app_to_tencent(s)) for s in symbols]
        cn_pairs = [(s, c) for s, c in routed if c]
        bj_symbols = [s for s, c in routed if not c and s.upper().endswith(_BJ_SUFFIX)]

        total = len(cn_pairs) + (1 if bj_symbols else 0)
        if total == 0:
            # 全部被过滤掉时仍回调一次, 否则前端进度条卡在 0(见 skill §5)。
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        frames: list[pl.DataFrame] = []
        workers = min(_MAX_WORKERS, max(1, len(cn_pairs)))
        if cn_pairs:
            logger.info(
                "腾讯分钟K 拉取开始(%d symbols, period=%s, workers=%d)",
                len(cn_pairs),
                period,
                workers,
            )

            def one(pair: tuple[str, str]) -> pl.DataFrame:
                sym, tcode = pair
                rows = parse_bars(_fetch_bars(tcode, period), sym)
                return pl.DataFrame(rows) if rows else pl.DataFrame()

            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for df in pool.map(one, cn_pairs):
                    if not df.is_empty():
                        frames.append(df)
            got = sum(f.height for f in frames)
            logger.info(
                "腾讯分钟K 拉取完成(%d symbols, %d 行, %.2fs)",
                len(cn_pairs),
                got,
                time.perf_counter() - t0,
            )
            if on_chunk_done:
                on_chunk_done(1 if not bj_symbols else total - 1, total)

        if bj_symbols:
            frames.extend(self._bj_fallback(bj_symbols, freq))
            if on_chunk_done:
                on_chunk_done(total, total)

        if not frames:
            return pl.DataFrame()
        df = pl.concat(frames, how="diagonal_relaxed")
        df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        keep = [c for c in _MINUTE_CANONICAL if c in df.columns]
        return df.select(keep).sort(["symbol", "datetime"])

    @staticmethod
    def _bj_fallback(bj_symbols: list[str], freq: str) -> list[pl.DataFrame]:
        """北交所分钟数据: 腾讯不支持, 回落到 stock-sdk(东财)。

        东财现状失效概率很高, 故失败只记 debug 不告警 —— 否则每次全市场同步
        都会被 347 条 warning 刷屏。拿不到就当无数据, 不影响沪深主链路。
        """
        try:
            from app.plugins.stocksdk.provider import StockSDKProvider

            provider = StockSDKProvider()
            try:
                df = provider.get_minute(bj_symbols, None, None, freq=freq)
            finally:
                provider.close()
        except Exception as e:
            logger.debug("北交所分钟回落失败: %s", e)
            return []
        if df.is_empty():
            logger.info("北交所分钟: %d 只标的无数据(腾讯不支持, 东财回落为空)", len(bj_symbols))
            return []
        logger.info("北交所分钟: 东财回落 %d 只 -> %d 行", len(bj_symbols), df.height)
        return [df]


def availability() -> tuple[bool, str]:
    """探活: 拉一只沪深标杆.Response 结构正常即认为可用。不抛异常。"""
    rows = _fetch_bars("sh600519", "m1")
    if not rows:
        return False, "腾讯 mkline 无数据返回(可能被风控或网络不通)"
    last = rows[-1] if rows else []
    if len(last) < 6:
        return False, "腾讯 mkline 返回行结构异常"
    return True, f"ok ({len(rows)} bars, latest={last[0]})"
