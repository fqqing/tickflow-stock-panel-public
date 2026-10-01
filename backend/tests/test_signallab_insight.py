"""AI 形态归因解读 (app.signallab.insight) 单测 — 只测提示词装配, 不真调 LLM。

核心保证: 喂给 LLM 的**每一个数字都来自台账统计**, 提示词里没有模型自己算的余地。
因此这里锁的是「事实装配」而不是生成效果。
"""
from __future__ import annotations

import json

import numpy as np
import polars as pl

from app.signallab.insight import (
    attribution_markdown,
    build_user_prompt,
    collect_attribution_facts,
    overall_markdown,
    params_markdown,
)


def _ledger(n: int = 200) -> pl.DataFrame:
    rng = np.random.default_rng(11)
    vol_ratio = rng.uniform(0.5, 3.0, n)
    return pl.DataFrame({
        "symbol": [f"{i:06d}" for i in range(n)],
        "entry_signal_name": ["底部结构"] * n,
        "ctx_vol_ratio": vol_ratio,
        "ctx_atr_pct": rng.uniform(0.01, 0.05, n),
        "ret_5d": (vol_ratio - 1.75) * 0.05,
        "ret_20d": (vol_ratio - 1.75) * 0.08,
    })


def test_collect_attribution_facts_returns_only_bucket_stats():
    rows, overall, horizons = collect_attribution_facts(_ledger(), 5, min_samples=20)
    assert horizons == [5, 20]
    assert rows, "有足够样本时应产出分桶行"
    assert set(rows[0]) == {
        "feature", "bucket", "ret_n", "ret_win_rate", "ret_mean", "ret_profit_factor",
    }
    assert "ret5_mean" in overall, "整体战绩用汇总口径的列名"


def test_collect_attribution_facts_empty_when_all_buckets_thin():
    rows, _, _ = collect_attribution_facts(_ledger(n=40), 5, min_samples=1000)
    assert rows == []


def test_markdown_tables_contain_the_numbers():
    rows, overall, horizons = collect_attribution_facts(_ledger(), 5, min_samples=20)
    table = attribution_markdown(rows, 5)
    assert table.startswith("| 特征 | 档位 | 样本数 | 胜率 | 平均收益 | 盈亏比 |")
    assert "%" in table, "收益与胜率应以百分数呈现, 避免模型按小数理解"
    overall_table = overall_markdown(overall, horizons)
    assert "5 日" in overall_table and "20 日" in overall_table


def test_params_markdown_lists_ids_and_defaults():
    text = params_markdown([
        {"id": "require_gap", "label": "需要跳空", "type": "bool", "default": True},
    ])
    assert "require_gap" in text and "需要跳空" in text
    assert "该策略没有声明可调参数" in params_markdown(None)


def test_user_prompt_carries_dataset_range_and_focus():
    rows, overall, horizons = collect_attribution_facts(_ledger(), 5, min_samples=20)
    prompt = build_user_prompt(
        strategy_name="底部结构",
        strategy_desc="测试策略",
        dataset={"start": "2026-03-01", "end": "2026-09-30", "rows": 123},
        horizons=horizons,
        overall=overall,
        rows=rows,
        params=[{"id": "p1", "label": "参数1", "type": "float", "default": 1.5}],
        horizon=5,
        focus="只看创业板",
    )
    assert "2026-03-01" in prompt and "123" in prompt
    assert "只看创业板" in prompt
    assert "p1" in prompt


def test_insight_stream_emits_error_when_no_bucket(monkeypatch):
    """档位全被样本量门槛剔掉时, 必须回一条 error 而不是空流。"""
    import asyncio

    from app.signallab import insight

    async def run() -> list[dict]:
        out = []
        async for chunk in insight.analyze_attribution_stream(
            _ledger(n=40), {"start": None, "end": None},
            strategy_name="x", horizon=5, min_samples=1000,
        ):
            out.append(json.loads(chunk))
        return out

    events = asyncio.run(run())
    assert events[0]["type"] == "error"
