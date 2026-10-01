"""B2: 每日选股结果推送飞书多维表格。

从**运行中的后端** HTTP API 取矩阵原生策略的最新结果
(底部结构 / 向上趋势并突破 / 趋势擒龙 / 启动策略), 按各表字段映射后经
lark_bitable 通道推送, 按 (代码, 日期) 去重, 同一天重复运行安全。

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
# 数据源 -> 目标表 映射
#   前三个沿用 qushiqinlong 三表 (走 /api/screener/run_preset);
#   abnormal 走 /api/abnormal/overview (交易所异动规则口径), 表字段各不相同,
#   故 key_fields / date_fields 按表配置而非常量。
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
    "abnormal": {
        "label": "异动预警",
        "base_token": "G5pgbvQSIaJHu6sJibNcK43Inkb",
        "table_id": "tbllEDcfkJ6JR06M",
        "key_fields": ("代码", "日期"),
        "date_fields": ("日期",),
    },
    # 启动策略在面板策略池里有, 但飞书侧还没有对应表 —— base_token/table_id
    # 待补。留 None 时 run() 会跳过并告警, 不会误推进异动预警表。
    "startup_surge": {
        "label": "启动策略",
        "base_token": None,
        "table_id": None,
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


def _fetch_abnormal_rows(backend: str, min_closeness: float = 0.7) -> tuple[str, list[dict]]:
    """调 /api/abnormal/overview, 返回 (as_of, rows)。

    行结构: symbol / name / board / close / windows{3d,10d,30d:{value,threshold,closeness}}
    / max_closeness / status。value 是「N日累计涨跌幅偏离值」(小数)。

    min_closeness 是「接近度」下限 (|偏离|/阈值): 0.5 观察 / 0.7 边缘 / 1.0 已触发。
    默认 0.7 —— 0.5 会把「才刚过半程」的一起拉进来, 实测全市场 187 行(过滤后仍有
    106 条), 与该表历史每天几条的量级不符; 0.7 时约 59 条。
    """
    resp = _http_json(
        f"{backend}/api/abnormal/overview?min_closeness={min_closeness}&limit=500", timeout=120
    )
    as_of = str(resp.get("cache_date") or "")[:10]
    return as_of, resp.get("rows") or []


# 板块 -> 单日涨跌幅上限 (判断「下一日是否可能触发」时用它封顶)
_LIMIT_UP_BY_BOARD = {"主板": 0.10, "创业板/科创板": 0.20, "北交所": 0.30}


def _abnormal_records(rows: list[dict], as_of: str, only_actionable: bool = True) -> list[dict]:
    """异动边缘行 -> 异动预警表记录。

    口径(与表内历史数据对齐, 由表内既有记录反推):
      - 所需最小涨幅 = 目标窗口阈值 - 当前偏离值 (线性近似, 非复利)。
      - 目标等级 = 未触发窗口里「所需涨幅最小」的那个; 全触发则取偏离最大的窗口。
      - 下一日可能触发 = 所需涨幅 <= 该板块单日涨跌幅上限。
      - 是否异动类型 = 已触发窗口的列举, 形如 "10日涨跌幅异常(53.49%)"。

    only_actionable=True (默认) 时只留「明日可能触发」或「已触发」的行 ——
    否则会把「还需涨 133% 才够」这类无行动价值的噪音一起推 (实测默认口径下有
    近 200 条, 有效 actionable 只有几十条)。
    """
    records: list[dict] = []
    for r in rows:
        symbol = str(r.get("symbol") or "")
        code, _, market = symbol.partition(".")
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market, market.lower())
        windows = r.get("windows") or {}

        triggered: list[tuple[int, float]] = []
        pending: dict[int, float] = {}
        for key, w in windows.items():
            try:
                n = int(str(key).rstrip("d"))
            except ValueError:
                continue
            val = lb.to_num(w.get("value")) or 0.0
            thr = lb.to_num(w.get("threshold")) or 0.0
            if (lb.to_num(w.get("closeness")) or 0.0) >= 1.0:
                triggered.append((n, val))
            else:
                pending[n] = thr - val

        if pending:
            target_n = min(pending, key=lambda k: pending[k])
            need = pending[target_n]
        elif triggered:
            target_n = max(triggered, key=lambda t: t[1])[0]
            need = 0.0
        else:
            continue

        limit_up = _LIMIT_UP_BY_BOARD.get(str(r.get("board") or ""), 0.10)
        if only_actionable and need > limit_up and not triggered:
            continue
        desc = ", ".join(f"{n}日涨跌幅异常({v * 100:.2f}%)" for n, v in sorted(triggered))
        records.append({
            "代码": f"{prefix}{code}",
            "名称": str(r.get("name") or ""),
            "日期": as_of,
            "收盘价": lb.to_num(r.get("close")),
            "触发信号次数": len(triggered),
            "所需最小涨幅": round(need, 6),
            "是否异动类型": desc or None,
            "预警信息": f"明日若涨 {need * 100:.2f}% 将触发{target_n}日异动" if need > 0 else None,
            "目标等级": f"{target_n}日异动",
            "下一日可能触发": "True" if need <= limit_up else "False",
        })
    return records


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
    if strategy_id == "abnormal":
        return _abnormal_records(rows, as_of)
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
        elif strategy_id in ("bottom_structure", "startup_surge"):
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
        if not cfg.get("base_token") or not cfg.get("table_id"):
            logger.warning(
                "[%s] 飞书表未配置(base_token/table_id 为空), 跳过 —— "
                "请在 STRATEGY_TABLES 补上该表的 base_token 与 table_id", label,
            )
            continue
        try:
            if sid == "abnormal":
                as_of, rows = _fetch_abnormal_rows(
                    backend, getattr(args, "abnormal_min_closeness", 0.7)
                )
            else:
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
            key_fields=cfg.get("key_fields", _KEY_FIELDS),
            force=args.force,
            date_fields=cfg.get("date_fields", _DATE_FIELDS),
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
    p.add_argument(
        "--abnormal-min-closeness", type=float, default=0.7,
        help="异动接近度下限 0.5/0.7/1.0 (观察/边缘/已触发), 默认 0.7",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
