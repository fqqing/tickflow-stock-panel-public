# 数据同步优化：诊断与方案

> 目标：解决两个互相纠缠的问题
> 1. 每次数据同步耗时长（盘后跑一次要等很久）
> 2. 盘后没同步 → 第二天开盘只能看到上一交易日的数据（分钟数据尤其明显）
>
> 本文只做诊断与方案。
>
> **更新（2026-09-28）**：P0 / P1 / P2 / P3 **全部实施完毕**。
> 按「收益看 ongoing」重排如下，累计让单次分钟同步从 5h07m 降到约 15 分钟、
> enriched 阶段从 430s 降到秒级。每项的实施记录见 §九。

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

### P1 — enriched 真增量（**已实施；根因与预判不同**）

#### 重新诊断：增量能力其实已经有了

`backend/app/indicators/pipeline.py::run_pipeline` 本来就支持三档：

| 模式 | 触发条件 | 代价 |
|---|---|---|
| 全量 | 首次 / 往前扩展历史 | 全重写 |
| 向后增量 `new_dates_only=True` | 有新交易日 | 只算新日期分区 |
| 局部重算 `symbols=[...]` | 指定的标的 | **这些标的的全部日期** |

`daily_pipeline.py::run_now`（约 440–500 行）的分支也是对的。**问题不在这些代码。**

#### 真正的 bug：`sync_adj_factor` 的 affected 粒度是「全市场」

日志现场：

```text
15:26:38  compute_enriched: adj_factor incremental, 4623 symbols
15:26:39  全量计算: 4623 只标的, 按 symbol 分批 [incremental (4623 symbols)]
15:30:44  enriched 完成 [incremental (4623 symbols)]: 246.26s, 共 83056940 行
```

它标着 `incremental`，实际是 **4623 只标的 × 各自全历史** 重算。源头在
`services/kline_sync.py::sync_adj_factor`：

```python
# 改前
affected = new_data["symbol"].unique().to_list()
```

——**「这次从数据源拉回来的所有标的」就是 affected**。而 `ex_factors` 接口多半忽略
`start_time/end_time`、或按标的返回全历史，于是每次都 = 全市场 4623 只。

#### 修复：改成 diff，只认「真变化」

新增 `_diff_affected_symbols(existing, merged)`，只有两类算受影响：

1. 新增的行（`symbol + trade_date` 本地没有）
2. 同一行 `ex_factor` 值变了（浮点容差 1e-9，避免把重拉的噪声当变化）

首次写入（无旧值可比）沿用全量语义；缺列时安全退化，不抛异常。
自定义源分支与 TickFlow 分支**两处**都已替换。

#### 实测效果（对拍）

```text
本地 adj_factor: 17,740 行 / 4,623 只
「无任何变化」时 affected: 0 只      ← 旧行为 4623 只
某只真变化时     affected: ['002461.SZ']  ← 精确命中
```

- **收益**：日常日志会从 `adj_factor incremental, 4623 symbols` 变成
  `adj_factor incremental, N symbols`（N 通常几只到几十只）；
  enriched 重算行数从 **8305 万行降到几十万行**，该阶段 **430s → 秒级**
- **风险**：中（enriched 是选股/策略/回测的共同底座）
  ⇒ 已做对拍，见 `backend/scripts/verify_p1_adj_diff.py`，**12/12 PASS**
  （含「值真变了必须识别」这条正确性红线 —— 漏判会导致 enriched 不更新）
- **工作量**：实际约 1 小时（远小于原估的 2~3 天，因为根因是单点而非架构）

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

## 九、实施记录（2026-09-28）

### 全景收益

| 阶段 | 改了什么 | 实测/折算收益 | 提交 |
|---|---|---|---|
| **P0** | 分钟同步标的只留 A 股 | 20914 → 5569，**3.76x**；5h07m → 约 1h20m | `5b478f6` |
| **P3** | 昨收不再依赖本地日 K | 盘后不同步也能正常看盘 | `5970c17` |
| **P2** | 分钟分层 focus 模式 | 5569 → **1000**，再 **5.57x**；约 1h20m → **约 15min** | `c358006` |
| **P1** | 除权因子 affected 精确化 | enriched 8305 万行 → 几十万行，**430s → 秒级** | 本次 |

**P0 + P2 累计：分钟同步标的 20914 → 1000，20.9 倍。**

P2 的 focus 是**可选**配置（默认 `all` 全量以保证行为不变），在「数据管理 → 分钟同步」
里切「核心池」即生效。

### P0 详录

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

## 十、追加：分钟数据源替换为腾讯 mkline（2026-09-28 晚）

### 为什么 P0+P2+P3 之后还是慢

上面三处优化解决的是「**标的太多**」，但分钟同步依然卡死。追查后发现还有一层
**更根本的问题：数据源本身已经不可用了**，不是慢，是拿不到数据。

同时刻对照实测（同一只票，同为 1 分钟 K）：

| 源 | 单只耗时 | 结果 |
|---|---|---|
| stocksdk（东方财富） | **27.6s** | **0 根 + errors** |
| 腾讯 mkline | **0.11~0.20s** | **320~482 根** |

根因链条（证据在代码注释里早就埋着）：

1. `bridge.mjs::opMinute` 注释原文：
   > 实测: 分钟接口存在十几秒级的间歇性空返回(**单发成功率约 1/5**)
2. 因此配了 `retries:4, delayMs:400, backoff:2` ⇒ 每只票最坏退避
   `400+800+1600+3200 = 6s`，还不含请求本身
3. 一批 40 只 ÷ 并发 6 ≈ 7 轮 ⇒ 单批理论最坏 **315s**
4. 而 `provider.py::get_minute` 的超时是 **180s** ⇒ **必然打满超时**
5. 表现就是：每批都失败重来，35 分钟只挪 18/140 格

### 候选源横向对比

| 源 | 状态 | 速度 | 覆盖 | 结论 |
|---|---|---|---|---|
| **腾讯 mkline** | ✅ 实测可用 | **89~160 只/s** | 沪深 ✅ / 北交所 ❌ | **采用** |
| 东财 stocksdk（原用） | ❌ 分钟已失效 | 27s/只全失败 | — | 替换 |
| **通达信 pytdx / mootdx / xmtdx** | ❌ **已阵亡** | — | — | **2026-09-10 起服务端改协议后被拒绝** |
| 通达信 `tdxdata`（新版逆向） | ⚠️ 可用但很早期 | 每请求 ~3s，43 主站并发 | 沪深 ✅ / 北交所 ❌ | v0.1.1，批量接口已放弃，暂不选 |
| 同花顺 | ❌ | — | — | 无公开免费 API，仅商业 iFinD |

> ⚠️ **连带提醒**：`GP/qushiqinlong` 的三个通达信选股脚本走的就是 pytdx，
> 按上述时效性推断**大概率也已失效**，那条支线需单独排查。

### 腾讯 mkline 实测规格

```text
GET https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m1,,320
Referer: https://gu.qq.com/

返回行: [datetime12, open, close, high, low, vol(手), {}, 换手率基点]
         ^^^^^^^^^^^^^^^^^^^^ 注意第 3 位是 close, 不是 high
```

| 项 | 实测结论 |
|---|---|
| 周期 | m1 / m5 / m15 / m30 / m60 全支持 |
| `count` | ≤320 生效；传更大值被静默封顶 320；**留空反而返回 482 根**（故一律留空） |
| 覆盖跨度 | m1 ≈ 2 交易日 / m5 ≈ 11 交易日 / m30 ≈ 3 个月 |
| `beg/end` | **不支持**，传了返回 0 根（不报错）⇒ provider 忽略区间，由下游裁剪 |
| 批量 | param 拼多只返回空 data ⇒ 只能单只请求 + 线程池并发 |
| 吞吐 | workers=24 约 **89~160 只/s**；300 只样本 0 失败 |
| **北交所** | 430/83x/87x/920 全号段 mkline 均 **0 根**；但 `qt.gtimg.cn` 实时行情可用 |
| **第 7 字段** | 是**换手率基点**（÷100 = 换手率%），**不是成交额** |
| `amount` | 上游不提供，按 `vol(手) × 100 × close` 估算 |

**量纲交叉验证**（600519，2026-09-28，241 根 m1 累加）：

| 指标 | 腾讯 | 本地 enriched | 偏差 |
|---|---|---|---|
| volume 合计 | 28,218.0 手 | 28,218.3 手 | 1.1e-5（源间亚手级差异） |
| 换手率 | 0.226% | 0.2257% | 吻合 |
| amount（估算） | 34.89 亿 | 34.89 亿 | 0.01% |

### 实施内容

| 文件 | 说明 |
|---|---|
| `backend/app/plugins/tencent/plugin.yaml` | 新插件清单，只声明 `minute`，`runtime: none` |
| `backend/app/plugins/tencent/provider.py` | `TencentMinuteProvider`，线程池并发 + 北交所回落东财 |
| `backend/app/plugins/tencent/__init__.py` | 导出 |
| `backend/scripts/probe_minute_sources.py` | **选型探针**，可重复复跑验证上述规格 |
| `backend/scripts/verify_tencent_minute.py` | **验收脚本**，36 项全通过（含北交所回落打桩、极值自洽、量纲比对） |

设计要点：

- **不支持批量 ⇒ 内部用 `ThreadPoolExecutor` 并发 24**；中文命名 `app_to_tencent`
  兼顾 ETF/科创板（同为 `sh`/`sz` 前缀）
- **北交所自动回落 stocksdk（东财）**：东财现状失效概率高，故失败只记 `debug`
  不告警 —— 否则每次全市场同步会被 347 条 warning 刷屏
- **忽略 `start_time` / `end_time`**：上游不支持区间。安全，因为
  `sync_minute_batch` 的自定义源分支**不做时间裁剪**，直接把返回值交给 `on_segment` 落盘
- 每次都返回「最近约 2 天」，天然幂等且带重叠，**不会因为增量窗口算错而丢数据**

### 切源后的位置

- `data/user_data/preferences.json`：`minute_data_provider` 由 `stocksdk` → `tencent`
- 同时保留 P2 的 `minute_sync_scope: focus`（5569 → 约 1000 只）

### 预期收益

| 场景 | 切源后 |
|---|---|
| focus 1000 只（单次请求） | **约 11s** |
| focus 1000 只（覆盖 5 天） | **约 56s** |
| 全 A 5569 只（覆盖 5 天） | **约 313s** |

对比切源前：35 分钟仅 18/140 批**且全部失败**。

> ⚠️ 内存提示：provider 一次性返回全部标的的结果 DataFrame。focus 1000 只约 48 万行
> （安全）；全 A 5569 只约 2140 万行（约 1~1.5GB）——建议配合 focus 模式使用。

### 待用户执行

1. **重启后端**（新插件要进内存，且当前服务已停止响应）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\start.ps1
   ```
2. 触发一次分钟同步，日志里应看到：
   ```text
   腾讯分钟K 拉取开始(1000 symbols, period=m1, workers=24)
   腾讯分钟K 拉取完成(1000 symbols, 480000 行, 12.3s)
   北交所分钟: 347 只标的无数据(腾讯不支持, 东财回落为空)
   ```
3. 设置页「数据源」→ 分钟数据源应能看到「腾讯行情(分钟K)」可用

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
