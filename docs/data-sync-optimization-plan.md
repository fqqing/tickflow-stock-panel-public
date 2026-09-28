# 数据同步优化：诊断与方案

> 目标：解决两个互相纠缠的问题
> 1. 每次数据同步耗时长（盘后跑一次要等很久）
> 2. 盘后没同步 → 第二天开盘只能看到上一交易日的数据（分钟数据尤其明显）
>
> 本文只做诊断与方案。
>
> **更新（2026-09-28 16:20）**：用户确认「只做 A 股，港股美股基本不看」，
> **P0 已实施**（见 §5）。诊断结论与其余方案仍然有效。

---

## 一、症状（用户原话）

- 「数据没同步完，一般只会出现上一个交易日的数据」
- 「分钟数据是更新得最慢的」
- 盘后如果没跑同步，第二天开盘体验明显退化

注意：这不是「完全看不了」，而是**数据滞后** —— 系统退回到最近一次完整同步的日期。

---

## 二、实测数据（来源 `data/backend.log`）

```text
pipeline: enriched 完成 [incremental (4623 symbols)]: 429.90s, 共 83056940 行, 含复权
pipeline: enriched 完成 [incremental (4623 symbols)]: 246.26s, 共 83056940 行, 含复权
pipeline: sync_minute: [2026-09-23 ~ 2026-09-28] done, 21 days
minute 拉取开始(20914 symbols, period=1)
pipeline: sync_daily: [2026-09-25 ~ 2026-09-28] done, 3 days
pipeline: sync_adj: [2026-09-25 ~ 2026-09-28] done, 4623 symbols
```

| 阶段 | 实测耗时/规模 | 备注 |
|---|---|---|
| `compute_enriched` | **430s / 246s** | 4623 只、8305 万行，管道最大瓶颈 |
| `sync_minute` | 21 天区间，**20914 只**标的 | ← 异常，见 3.1 |
| `sync_daily` | 3 天增量 | 已经是增量，正常 |
| `sync_adj` | 4623 只 | 正常 |

---

## 三、根因分析

### 3.1 分钟同步标的池未按市场过滤（主因，也是最容易修的）

`backend/app/api/kline.py` 的 `sync_minute()`（约 1665–1675 行）：

```python
universe = sorted(set(get_pool("watchlist")) | set(get_pool("CN_Equity_A")))
# 补充 instruments 全量标的，覆盖北交所、新股等
inst = pl.read_parquet(inst_path, columns=["symbol"])
universe = sorted(set(universe) | set(inst["symbol"].to_list()))
# 剔除指数 symbol
index_set = repo.get_index_symbol_set()
universe = [s for s in universe if s not in index_set]
```

这里把 `instruments.parquet` **全量并入**，只剔除了指数，**没有按 market 过滤**。
而 `instruments.parquet` 是全市场维表（含美股/港股）：

| 后缀 | 数量 | market |
|---|---|---|
| `.US` | 12439 | us |
| `.HK` | 2906 | hk |
| `.SZ` | 2902 | cn |
| `.SH` | 2320 | cn |
| `.BJ` | 347 | cn |
| **合计** | **20914** | cn 仅 **5569** |

⇒ 分钟同步在拉 **20914** 只标的，而 A 股实际需要 **5569** 只。
**73%（15345 只）是美股和港股，属于无效工作量。**

叠加分钟数据本身的量级（每只每天约 240 根，是日 K 的 ~240 倍），
这就是「分钟数据更新最慢」的直接原因。

**修复**：从 `instruments` 读时按市场过滤（见 P0）。理论收益 **≈3.76 倍**。

#### 3.1.1 决定性证据：港美股**拉了也白拉**（已确认，非假设）

查 `data/kline_minute` 实际入库内容：

```text
分钟 parquet 文件数 : 21
总行数              : 6803145
按后缀分布          : [('.SZ', 3619360), ('.SH', 3061267), ('.BJ', 122518)]
日期范围            : 2026-08-28 ~ 2026-09-28
标的数量            : 5568
```

**库里一条 `.US` / `.HK` 的分钟数据都没有。** 也就是说：

- 请求了 **20914** 只（含 12439 美股 + 2906 港股）
- 入库仍然只有 **5568** 只 A 股 —— 数据源对非 A 股返回空

⇒ 这 15345 只请求是**纯浪费**，砍掉**零损失**。

#### 3.1.2 实际代价：一次分钟同步 **5 小时 7 分**

```text
09:13:06  stock-sdk minute 拉取开始(20914 symbols, period=1)
14:20:14  minute K synced: 1606529 rows (20914 symbols)
```

`5:07:08`。按请求量线性折算，5569 只约 **1h20m**，省下 **≈3h50m**。

（16:13 的日志还显示桥接超时 `stock-sdk 桥接超时 (op=minute, 180s)`，
说明当前仍在跑第二次同步 —— 池过滤对这类超时同样有缓解作用。）

> 已确认（原「待确认」项）：`run_market_sync` 只管港美股**日 K**，没有分钟通道；
> 港股/美股分钟数据本来就不存在。故 P0 直接「只留 A 股」，无需按市场分别同步。

### 3.2 enriched 是「伪增量」

日志里标着 `incremental`，但规模是 **4623 只 × 全历史 = 8305 万行**，单次 4~7 分钟。
说明它的增量粒度是**按标的**（哪些标的变了就重算该标的的全部历史），
而不是**按日期**（只算新增交易日）。

只要跑管道，就要重算 8305 万行 ⇒ 这就是「每次同步都要花大量时间」的本体。

### 3.3 实时链路与历史同步耦合

「实时行情」实际由三块组成，对历史同步的依赖差别很大：

| 组成 | 依赖历史同步？ | 说明 |
|---|---|---|
| 快照（现价 / 五档盘口） | ❌ 不依赖 | 直连数据源，昨天同没同步都能拿到 |
| 涨跌幅 | ⚠️ 依赖昨收 `prev_close` | 优先用接口自带值（`quote_service.py` 的 `q.get("prev_close")`），缺失才回落本地日 K |
| 分时图 | ✅ 强依赖当日分钟数据 | 靠 `minute_intraday_refresh` 盘中积累或回补 |

所以「没同步就退回上一交易日」主要发生在**分时图**和**依赖本地昨收的涨跌幅**上，
现价与盘口本身不受影响。这个耦合是可以切断的。

### 3.4 附：启动并不阻塞

`main.py` 中 `repo.refresh_cache(background=True)` 是后台预热，应用立即 ready。
所以「第二天开机慢」不是启动被同步卡住，而是**数据本身就是旧的**。

---

## 四、优化方案（按优先级）

### P0 — 分钟同步标的池只留 A 股 ★最高性价比（**已实施**）

**改动**：两处入口各加一层后缀过滤（未动日 K 标的池，港美股日线能力保留）。

1. `backend/app/jobs/daily_pipeline.py`

```python
# A 股标的后缀 (沪深北)。分钟 K 只同步 A 股: instruments.parquet 里 73% 是
# 美股(.US 12439) 与港股(.HK 2906), 数据源对非 A 股返回空, 请求了也不入库。
_CN_SYMBOL_SUFFIXES: tuple[str, ...] = (".SH", ".SZ", ".BJ")

def _resolve_minute_symbols(capset, repo=None) -> list[str]:
    universe = _resolve_universe(capset, repo)   # 日K/分钟原先共用
    return [s for s in universe if str(s).upper().endswith(_CN_SYMBOL_SUFFIXES)]
```

2. `backend/app/api/kline.py::sync_minute`（剔除指数之后）

```python
from app.jobs.daily_pipeline import _CN_SYMBOL_SUFFIXES

universe = [s for s in universe if str(s).upper().endswith(_CN_SYMBOL_SUFFIXES)]
```

**为什么用后缀而不是 `market` 列**：`instruments.parquet` 的 `market` 列取值未逐一核实，
而后缀分布已实测（`.SH/.SZ/.BJ` = A 股），且 ETF 同为 `.SH/.SZ` 后缀会**被保留**
（沿用既有「刻意保留 ETF」行为），指数由上游 `index_set` 剔除。

- **收益**：分钟同步标的 20914 → **5569**，降 **73.4%**；一次同步 5h07m → 约 1h20m
- **零损失**：过滤后 5569 只 vs 库内实际入库 5568 只，**仅差 1**
- **风险**：低。ruff 基线对比 55/59 → 55/59，**零新增告警**
- **不受影响**：单股补齐端点（`[symbol]`，force_full_days）、
  `scripts/backfill_minute_tdx.py`（本来就是 `all_a_share_symbols()`）、
  日 K 标的池（未改，港美股日线仍可同步）

### P1 — enriched 真增量（按日期分区）

**改动**：`enriched_generation.py` / `run_pipeline_market`

把增量粒度从「按标的全量重算」改成「按 `(symbol, date)` 分区，只重算最近 N 个交易日」。

- **收益**：430s → 目标 30s 内
- **风险**：中。enriched 是选股/策略/回测的共同底座，改错影响面大
  ⇒ **必须做对拍**：改前改后对同一批标的的指标值逐列比对
- **工作量**：2~3 天（含对拍）

### P2 — 分钟数据分层（活跃股全量 + 冷门按需）

分钟数据没必要全市场常驻。建议分三层：

| 层级 | 范围 | 策略 |
|---|---|---|
| L1 | 自选股 + 监控池 | 盘中实时积累，优先保证 |
| L2 | 沪深主板 + 创业板活跃股 | 每日增量 |
| L3 | 其余（含北交所、冷门股） | **打开时才拉**（单只 ~1s） |

- **收益**：分钟同步再降一个数量级，且打开任何股票都能立即看到分时
- **工作量**：约 2 天

### P3 — 实时链路与历史同步解耦

- 涨跌幅：接口自带 `prev_close` 就直接用，**不回落等本地日 K**
- 分时图：当日分钟数据为空时，用当日已积累的快照序列兜底，而不是退回上一交易日
- 前端：明确区分「数据未就绪」与「无数据」两种状态，避免看起来像坏了

- **收益**：即使盘后完全没同步，开盘也能正常看盘
- **工作量**：约 1 天

### P4 — 调度前移 + 开机自启（运维手段）

- 盘后同步改为**盘前预热**（如 8:30 跑增量），配合 P1 后耗时可接受
- 或开机自启 + 启动即跑增量

- **工作量**：约 0.5 天（配置）

---

## 五、预期收益汇总

| 项 | 现在 | P0 后 | P0+P1 后 |
|---|---|---|---|
| 分钟同步标的数 | 20914 | 5569 | 5569（+分层后更少） |
| 分钟同步耗时 | 基准 | **≈ 1/3.76** | ≈ 1/4 |
| enriched 耗时 | 430s | 430s | **≈30s** |
| 单次完整管道 | 5~10 分钟 | 3~7 分钟 | **<1 分钟** |
| 盘后不跑能否看盘 | 退回上一交易日 | 退回上一交易日 | **能正常看**（P3） |

---

## 六、验证方法

1. **P0 验证**（最直观）
   - 改前后各跑一次 `POST /api/kline/sync/minute`，对比日志里的
     `minute 拉取开始(N symbols, period=1)` 中的 N，应从 20914 降到 ~5569
   - 对比两次总耗时
2. **P1 验证**（必须对拍）
   - 改前后对同一批标的、同一日期区间，逐列比对 enriched 指标值
   - 抽样不少于 200 只 × 最近 20 个交易日
3. **P2/P3 验证**
   - 清空某只冷门股的分钟数据 → 打开该股分时图，应在 ~1s 内自动补齐
   - 模拟「昨日未同步」→ 检查开盘时现价/盘口/涨跌幅是否可用

---

## 七、建议实施顺序

```
P0（0.5天，改 1 处，收益 3.76x，风险低）
  ↓
P3（1天，解耦，让「没同步」不再是致命伤）
  ↓
P1（2~3天，治本，但要对拍）
  ↓
P2 / P4（按需）
```

**P0 和 P3 可以一起做**，做完这两条，用户描述的痛点基本消失；
P1 再作为治本项跟进。

---

## 九、P0 实施记录（2026-09-28）

### 用户决策

> 「只做 a 股的就行，港股美股基本不看」

⇒ P0 从「按市场分别同步」简化为「只留 A 股」。港美股**日 K** 不受影响
（`_resolve_universe` 未改，`kline_daily_hk` / `kline_daily_us` 仍可同步），
只有**分钟**通道收窄到 A 股 —— 而港美股分钟数据本来就不存在（见 3.1.1）。

### 改动文件

| 文件 | 改动 |
|---|---|
| `backend/app/jobs/daily_pipeline.py` | 新增 `_CN_SYMBOL_SUFFIXES` 常量；`_resolve_minute_symbols` 由「直接复用 `_resolve_universe`」改为「复用后再按后缀过滤」 |
| `backend/app/api/kline.py` | `sync_minute` 在剔除指数之后，追加同一层后缀过滤 |

### 已完成的验证

| 项 | 结果 |
|---|---|
| 过滤效果 | 20914 → **5569**（剔除 15345，降 73.4%） |
| 零损失核对 | 过滤后 5569 vs 库内实际入库 **5568**，仅差 1 |
| ruff 基线对比 | `daily_pipeline.py` 55 → 55、`kline.py` 59 → 59，**零新增**（用 `git stash push -- <file>` 就地对比） |
| 副作用排查 | 单股补齐端点走 `[symbol]` 不受影响；`backfill_minute_tdx.py` 本就是 `all_a_share_symbols()`；日 K 池未动 |

### 待用户执行

后端需**重启**才生效（新代码要进内存）。触发一次分钟同步后，日志里应看到：

```text
标的池 5569 只          # 原先是「标的池 20914 只」
stock-sdk minute 拉取开始(5569 symbols, period=1)
```

> ⚠️ 截至记录时点，后端仍在跑 15:32 发起的那次分钟同步（16:13 有桥接超时日志）。
> 建议等它跑完再重启，避免打断。

---

## 八、附：关键代码位置索引

| 关注点 | 位置 |
|---|---|
| 分钟同步入口 | `backend/app/api/kline.py::sync_minute`（~1610 行） |
| 分钟标的池构造 | 同上，~1665–1675 行 ← **P0 改动点** |
| 分钟权限门槛 | `backend/app/api/kline.py::_minute_allowed`（~24 行） |
| A 股管道 | `backend/app/jobs/daily_pipeline.py::run_now`（~185 行） |
| 港美股管道 | `backend/app/jobs/daily_pipeline.py::run_market_sync`（~116 行） |
| enriched 计算 | `backend/app/enriched_generation.py`、`run_pipeline_market` |
| 实时行情服务 | `backend/app/services/quote_service.py`（`boot_check` ~355 行） |
| 昨收回落 | `quote_service.py` 中 `q.get("prev_close")` 相关分支 |
| 档位门槛 | `tiers.yaml`：`none` 无实时 / `free` 10 次每分·5 标的 / `starter+` 付费端点 |
| 启动流程 | `backend/app/main.py::_application_lifespan`（~91 行） |
| 标的维表 | `data/instruments/instruments.parquet`（20914 行，含 us/hk） |
