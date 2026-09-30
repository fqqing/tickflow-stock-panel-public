"""B2: 每日选股结果推送飞书多维表格。

从**运行中的后端** HTTP API 取三个矩阵原生策略的最新选股结果
(底部结构 / 向上趋势并突破 / 趋势擒龙), 按各表字段映射后经 lark_bitable 通道推送,
按 (代码, 信号日期) 去重, 同一天重复运行安全。

为什么走 HTTP 而不是离线自举 repo+engine:
  后端进程里 enriched 缓存 / 策略引擎都已预热, 离线脚本重走一遍既慢又容易口径漂移;
  与面板看到的结果严格一致。

用法(盘后跑, 后端需在运行)::

    python backend/scripts/push_screener_to_lark.py                 # 三策略全推
    python backend/scripts/push_screener_to_lark.py --dry-run       # 只看记录不推送
    python backend/scripts/push_screener_to_lark.py --only trend_dragon,bottom_structure
    python backend/scripts/push_screener_to_lark.py --as-of 2026-09-30 --force

字段口径备忘(与 qushiqinlong 源脚本推送的表结构对齐):
  - 涨跌幅%: screener 返回小数, 推送时 x100 (表内是百分数数值)。
  - 趋势擒龙表 MA13: enriched 无 ma13 (只有 ma5/10/20/30/60), 置空。
  - 趋势擒龙表 资金动能: 逐只调 /api/kline/daily?indicators=capital_momentum 取 cm_value
    (选中股票通常只有几十只, 代价可接受; --no-enrich 可关)。
  - 底部结构表 信号状态/钝化类型: 策略矩阵内部值, 输出行不携带, 置空。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

# 直接 python 跑脚本时 backend 不在 sys.path, 补上以 import app.services
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import lark_bitable as lb

logger = logging.getLogger("push_screener_to_lark")

# ---------------------------------------------------------------------------
# 策略 -> 目标表 映射 (沿用 qushiqinlong 三表; 启动策略暂无表, 后续加表后在此登记)
# ---------------------------------------------------------------------------
STRATEGY_TABLES: dict[str, dict] = {
    "bottom_structure": {
        "label": "底部结构",
        "base_token": "EEYSbsdLpa9QkZsmGyVc7vCCnbb",
        "table_id": "tbl8TfrGiJYfzDd7",
    },
    "upward_trend_breakout": {
        "label": "向上趋势并突破",
        "base_token": "E1yLbkvgQaO28rssEbAca1twnxc",
        "table_id": "tbl9qX3qJx0Wr5vf",
    },
    "trend_dragon": {
        "label": "趋势擒龙",
        "base_token": "S4lKbOf6TaQ7A2sFw4hcDbyanFE",
        "table_id": "tbl8ZWQKMgGqyawK",
    },
}

_KEY_FIELDS = ("代码", "信号日期")
_DATE_FIELDS = ("信号日期",)


# 本机调用必须绕过代理: 沙箱/终端常设 HTTP_PROXY, 后端重启间隙代理会返 502
# 而不是连接拒绝, 且本机流量没必要出站绕一圈
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, payload: dict | None = None, timeout: int = 300) -> dict:
    """POST/GET 本地后端, 返回解析后的 JSON。失败抛 RuntimeError。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except OSError as e:
        raise RuntimeError(f"后端请求失败 {url}: {e}") from e


def _fetch_strategy_rows(backend: str, strategy_id: str, as_of: str | None) -> tuple[str, list[dict]]:
    """调 /api/screener/run_preset, 返回 (as_of, rows)。"""
    payload: dict = {"strategy_id": strategy_id, "asset_type": "stock", "market": "cn"}
    if as_of:
        payload["as_of"] = as_of
    resp = _http_json(f"{backend}/api/screener/run_preset", payload)
    return str(resp.get("as_of") or ""), resp.get("rows") or []


def _fetch_capital_momentum(backend: str, symbol: str) -> float | None:
    """逐只取资金动能 cm_value (最后一根 bar)。失败返回 None, 不阻断主流程。"""
    try:
        resp = _http_json(
            f"{backend}/api/kline/daily?symbol={symbol}&days=10"
            "&indicators=capital_momentum&fields=date,close,cm_value",
            timeout=60,
        )
    except RuntimeError as e:
        logger.warning("  %s 资金动能获取失败: %s", symbol, e)
        return None
    rows = resp.get("rows") or []
    for r in reversed(rows):
        v = r.get("cm_value")
        if v is not None:
            return lb.to_num(v)
    return None


def _pct(x: float | None) -> float | None:
    """小数涨跌幅 -> 百分数数值。"""
    v = lb.to_num(x)
    return round(v * 100, 4) if v is not None else None


def _bias_ma20(close: float | None, ma20: float | None) -> float | None:
    """乖离MA20% = (close / ma20 - 1) * 100。"""
    c, m = lb.to_num(close), lb.to_num(ma20)
    if c is None or m in (None, 0):
        return None
    return round((c / m - 1) * 100, 4)


def build_records(
    strategy_id: str,
    rows: list[dict],
    as_of: str,
    momentum_map: dict[str, float | None] | None = None,
) -> list[dict]:
    """把 screener 行映射成目标表记录 (各表字段对齐 qushiqinlong 源脚本)。"""
    records: list[dict] = []
    for r in rows:
        symbol = str(r.get("symbol") or "")
        code, _, market = symbol.partition(".")
        base = {
            "代码": code,
            "名称": str(r.get("name") or ""),
            "市场": market,
            "信号日期": as_of,
            "收盘价": lb.to_num(r.get("close")),
            "涨跌幅%": _pct(r.get("change_pct")),
        }
        if strategy_id == "trend_dragon":
            base.update({
                "MA5": lb.to_num(r.get("ma5")),
                "MA13": None,  # enriched 无 ma13
                "乖离MA20%": _bias_ma20(r.get("close"), r.get("ma20")),
                "资金动能": (momentum_map or {}).get(symbol),
            })
        elif strategy_id == "bottom_structure":
            base.update({
                "信号状态": "",   # 策略矩阵内部值, 输出行不携带
                "DIF": lb.to_num(r.get("macd_dif")),
                "DEA": lb.to_num(r.get("macd_dea")),
                "钝化类型": "",
                "MA5": lb.to_num(r.get("ma5")),
                "MA20": lb.to_num(r.get("ma20")),
            })
        records.append(base)
    return records


def run(args: argparse.Namespace) -> int:
    backend = args.backend.rstrip("/")
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else list(STRATEGY_TABLES)
    unknown = [s for s in only if s not in STRATEGY_TABLES]
    if unknown:
        logger.error("未知策略: %s (可选: %s)", unknown, list(STRATEGY_TABLES))
        return 2

    total_pushed = 0
    exit_code = 0
    for sid in only:
        cfg = STRATEGY_TABLES[sid]
        label = cfg["label"]
        try:
            as_of, rows = _fetch_strategy_rows(backend, sid, args.as_of)
        except RuntimeError as e:
            logger.error("[%s] %s", label, e)
            exit_code = 1
            continue
        logger.info("[%s] as_of=%s 选中 %d 只", label, as_of, len(rows))
        if not rows:
            continue

        momentum_map: dict[str, float | None] | None = None
        if sid == "trend_dragon" and not args.no_enrich:
            momentum_map = {}
            for r in rows:
                symbol = str(r.get("symbol") or "")
                momentum_map[symbol] = _fetch_capital_momentum(backend, symbol)

        records = build_records(sid, rows, as_of, momentum_map)
        if args.dry_run:
            logger.info("[%s] DRY RUN 记录样例: %s", label, json.dumps(records[:3], ensure_ascii=False))
            continue

        result = lb.push_records(
            cfg["base_token"], cfg["table_id"], records,
            key_fields=_KEY_FIELDS, force=args.force, date_fields=_DATE_FIELDS,
        )
        for d in result.details:
            logger.info("[%s] %s", label, d)
        if not result.ok:
            logger.error("[%s] 推送失败: %s", label, result.error)
            exit_code = 1
            continue
        logger.info("[%s] 推送 %d 条 (去重跳过 %d)", label, result.pushed, result.skipped)
        total_pushed += result.pushed

    logger.info("=== 完成: 共推送 %d 条 ===", total_pushed)
    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(description="每日选股结果推送飞书多维表格")
    p.add_argument("--backend", default="http://127.0.0.1:3018", help="后端地址 (默认本机 3018)")
    p.add_argument("--only", default=None, help="只跑指定策略, 逗号分隔 (默认全部)")
    p.add_argument("--as-of", default=None, help="指定交易日 YYYY-MM-DD (默认最新)")
    p.add_argument("--dry-run", action="store_true", help="只打印记录不推送")
    p.add_argument("--force", action="store_true", help="跳过去重, 强制全量推送")
    p.add_argument("--no-enrich", action="store_true", help="不逐只补资金动能 (更快)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
