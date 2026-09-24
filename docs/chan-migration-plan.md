# 缠论叠加层迁移方案（ECharts → 新图表内核）

> 配套文档：`docs/frontend-redesign-plan.html`（整体三阶段路线）
> 本文只讲**缠论**在 P1 换内核时怎么迁、风险在哪、怎么验收、怎么回退。

## 结论先行

**缠论是这次迁移里风险最低的部分**，不是最高的。

因为缠论的**计算完全在后端**（`backend/app/indicators/chan.py`，纯函数 + numpy 逐 bar 顺序算法），前端只做三件事：拉结果、转成绘图原语、交给图表库画。换图表内核时：

- 后端 `chan.py` —— 一行不动
- 前端 `lib/chan-overlay.ts` 的转换逻辑 —— 基本不动
- 只有「原语 → 具体绘制指令」这一层要重写

真正难的是**主图 K 线 + 6 个副图 + 指标计算**（那才是 1718 行的主体），缠论只是挂在主图上的一层。

## 一、现状数据流

```
后端 /api/chan/analysis
    │  ChanAnalysis { strokes, centers, signals }
    ▼
lib/useChanOverlay.ts        (39 行)  查询 + 缓存 5 分钟 + 按图表日期序列裁剪
    ▼
lib/chan-overlay.ts          (180 行) 纯数据转换 —— 不含任何 ECharts 运行时
    │   笔     → ChartPolyline（向上笔红 / 向下笔绿）
    │   中枢   → ChartRange（色块）+ 2 条水平 ChartPolyline（ZG/ZD）
    │   买卖点 → ChartMarker（买点在 low 下方↑，卖点在 high 上方↓）
    ▼
lib/chart-polyline.ts        (55 行)  densePolyline: 顶点序列 → 与 x 轴等长的数组
    ▼
components/EChartsCandlestick.tsx  polylines / ranges / markers props
```

关键性质：**`buildChanOverlay()` 是纯函数**，输入 `ChanAnalysis` + 图表日期序列，输出四个原语数组。它不含 `echarts` 的 import，也不碰 DOM。

## 二、已完成的前置解耦（本次会话做掉）

四个原语类型（`ChartMarker` / `ChartPolyline` / `ChartPriceLine` / `ChartRange`）原先**定义在 `EChartsCandlestick.tsx` 里**，而 `chan-overlay.ts`、`StockDailyKChart.tsx`、`pages/` 多处都从那个巨石组件 import 它们。这意味着换内核时，光搬类型就会牵动全项目。

已抽出为 **`frontend/src/lib/chart-primitives.ts`**（与图表库无关的中间表示 IR）：

- `EChartsCandlestick.tsx` 改为从 lib 引入并原样 `export type` —— **所有现有 import 路径保持可用，零破坏**
- `lib/chan-overlay.ts` 改为从 lib 引入
- 组件行数 1718 → 1686

至此渲染层变成可替换的：

```
缠论 / 监控价位 / 手绘线  →  产出 chart-primitives 原语  →  由具体图表库适配
                                                          ├─ ECharts 适配器（现状）
                                                          └─ KLineChart 适配器（P1）
```

## 三、原语映射表

| 原语 | 现在的 ECharts 实现 | 换成 KLineChart 后 | 说明 |
|---|---|---|---|
| `ChartPolyline`（笔） | `densePolyline` 稠密化成等长数组 + `'-'` 占位 + `connectNulls:false` | overlay/indicator 里 `drawLine`，直接给两个端点的坐标 | **不再需要稠密化**，`chart-polyline.ts` 可删 |
| `ChartRange`（中枢色块） | `markArea`（只接受 xAxis 范围） | `drawRect` | KLineChart 能直接给 x/y 两个方向的范围，比 markArea 直观 |
| `ChartPolyline`（ZG/ZD 水平线） | 水平折线（**不能**用 `ChartPriceLine`） | `drawLine` + 虚线样式 | 见下方「ECharts 三坑」第 2 条 |
| `ChartMarker`（买卖点） | `markPoint` | `drawText` / `drawIcon` | 位置换算逻辑可复用 |

## 四、ECharts 的三个坑（换库后自动消失，但要确认新库没有同类问题）

1. **`markArea` 只接受 xAxis 范围** —— y 方向边界属于 `markLine` 的语法 `[[{yAxis},{yAxis}]]`。所以中枢矩形只能用 `ChartRange` 走 markArea，不能混用。
2. **y 轴 min/max 会把所有 `priceLines` 的 value 并入**（`axisMin/axisMax`）—— 历史中枢的 ZG/ZD 离现价很远，用 `ChartPriceLine` 画会把纵轴拉爆。这是缠论中枢改用「水平折线」的原因。
   → 迁移时要**确认新内核画水平线不会撑开 y 轴范围**，否则同样的坑会再来一次。
3. **category 轴 + `data: [[date, price]]` 按名字查找，行为依赖版本** —— 所以必须稠密化成等长数组。新内核用时间戳坐标，无此问题。

## 五、实现路径（已核实 klinecharts@10.0.3 的真实 API）

> 本节结论来自实际下载 `klinecharts@10.0.3` 的 `dist/index.d.ts` 逐条核对，**不是猜测**。

**先说一个被推翻的判断**：我最初推测用「indicator 的 `draw` 钩子」自由绘制（因为能拿到 visibleRange 做裁剪）。核对后发现 **IndicatorTemplate 里根本没有 `draw` 方法**（`draw ?:` 零命中）——这条路走不通。

### 实际可用的路径：`registerOverlay` + `extendData`

类型定义里确认存在：

```
registerOverlay<E>(template: OverlayTemplate<E>): void
chart.createOverlay(value: string | OverlayCreate | Array<...>): Nullable<string> | Array<Nullable<string>>

OverlayCreateFiguresCallbackParams<E> = {
  chart: Chart
  overlay: Overlay<E>          ← 含 extendData: E, 缠论数据从这里进
  coordinates: Coordinate[]    ← 锚点已转好的像素坐标
  bounding: Bounding
  xAxis / yAxis
}

createPointFigures: (params) => OverlayFigure | OverlayFigure[]
```

图元 attrs 类型（对应 `type` 名）：

| type | attrs | 缠论用途 |
|---|---|---|
| `line` | `LineAttrs { coordinates: Coordinate[] }` | 笔（斜线）、中枢 ZG/ZD（水平线） |
| `polygon` | `PolygonAttrs { coordinates: Coordinate[] }` | 备选：不规则填充 |
| `rect` | `RectAttrs { x, y, width, height }` | 中枢色块 |
| `text` | `TextAttrs { x, y, text, align?, baseline? }` | 买卖点标签 |
| `circle` / `arc` | `CircleAttrs { x, y, r }` | 笔端点小圆点 |

`Overlay` 上还有两个关键字段：

- `totalStep: number` —— 「完成鼠标操作所需步骤数」。缠论是数据驱动、不需要用户点击，设为 `1`
- `lock: boolean` —— 锁定后不响应鼠标事件。**缠论 overlay 必须 `lock: true`**，否则会挡住图表的缩放拖拽

### 代码骨架

```ts
registerOverlay<ChanLayers>({
  name: 'chan',
  totalStep: 1,
  lock: true,
  createPointFigures: ({ overlay, coordinates, bounding }) => {
    const layers = overlay.extendData        // 缠论数据, 由 createOverlay 时传入
    const figs: OverlayFigure[] = []
    // 笔: 斜线, 端点坐标用 chart.convertToPixel 换算(笔端点可能落在可视区外)
    // 中枢: rect + 两条 line(ZG/ZD)
    // 买卖点: text + circle
    return figs
  },
})

// 挂载: 锚点给一个占位坐标, 绘制完全由 extendData 驱动
chart.createOverlay({
  name: 'chan',
  paneId: 'candle_pane',
  points: [{ timestamp: firstBarTs, value: firstBarClose }],
  extendData: chanLayers,
})

// 切股 / 切周期时: 先 removeOverlay 老的, 再 createOverlay 新的
```

### 仍需 1 天最小验证的三件事

1. `points` 只给 1 个锚点时，`createPointFigures` **是否会被调用**（overlay 本为「用户拖拽创建」设计，纯数据驱动属于非典型用法，必须实测）
2. `coordinates` 只含锚点坐标 —— 笔/中枢的其它端点需要用 `chart.convertToPixel` 自行换算，确认换算在缩放/平移后仍正确
3. 画出来的水平线**不会撑开 y 轴范围**（对应「ECharts 三坑」第 2 条）

若第 1 条不成立，兜底方案：把每笔拆成独立 overlay（`totalStep: 2`，两个端点各一个 point），笔数通常 30 左右、中枢 8 个左右，overlay 数量在百级，性能可接受。

## 六、改动清单

| 文件 | 动作 | 工作量 |
|---|---|---|
| `lib/chart-primitives.ts` | ✅ 已建（本次会话） | — |
| `lib/chan-overlay.ts` | 保留；仅需去掉对 `densePolyline` 的调用（改输出原始顶点） | 0.5 天 |
| `lib/chart-polyline.ts` | **删除**（稠密化是 ECharts category 轴的专属 hack） | — |
| `lib/useChanOverlay.ts` | 保留，输出结构微调（不再稠密化） | 0.5 天 |
| `components/chan/ChanLayer.tsx`（新） | KLineChart 适配器：原语 → draw 指令 | 2 天 |
| `components/KLinePro.tsx`（新） | 新内核主图，挂载 ChanLayer | 与 P1 主体合并计 |

**不需要动**：`backend/app/indicators/chan.py`、`backend/app/api/chan.py`、`ChanScan.tsx`、自选页缠论单元格、`chan_structure` 策略 —— 全部与渲染层无关。

## 七、对拍验收

缠论的正确性不靠肉眼，靠对拍。项目里已有对拍脚本套路可复用（`tickflow-tdx-strategy-port` skill）。

1. **计数对拍**：同一 symbol、同一 lookback(400)，新旧两版渲染各自导出 `{ 笔数, 中枢数, 买点数, 卖点数 }`，要求**完全一致**
2. **逐笔对拍**：导出每笔的 `(start_date, start_price, end_date, end_price)`，逐条比对，允许 0 差异
3. **中枢对拍**：导出每个中枢的 `(start, end, zd, zg)`，逐条比对
4. **裁剪对拍**：切换日期区间（近1月 / 近6月 / 近3年），确认图外端点被正确裁剪、不会整条笔消失
5. **抽样人工核对**：10 只股票截图对比，重点看笔的端点是否落在分型上

## 八、回退机制

- **双轨灰度**：`StockDailyKChart` 保留 ECharts 路径，新增 `KLinePro` 并行；按 `symbol` 哈希分流，设置项可一键全量回退
- **缠论独立开关**：缠论图层挂不挂，与用哪个内核解耦。即使新内核的 K 线本身有问题，缠论仍可在旧内核上看
- **备份点**：`backup/pre-terminal-20260924`（tag + branch + bundle + 物理文件），任何时候可整体回退

## 九、分步计划

| 步骤 | 内容 | 工作量 |
|---|---|---|
| 0 | ✅ 抽取 `chart-primitives.ts`（已完成） | — |
| 1 | **KLineChart overlay API 最小验证**（斜线 + 矩形 + 不撑开 y 轴） | 1 天 |
| 2 | `chan-overlay.ts` 去稠密化，输出原始顶点 | 0.5 天 |
| 3 | 写 `ChanLayer` 适配器 | 1.5 天 |
| 4 | 对拍（计数 + 逐笔 + 中枢 + 裁剪） | 1 天 |
| 5 | 接入 `KLinePro`，删 `chart-polyline.ts` | 0.5 天 |

**合计约 4.5 天**，且第 1 步是 go/no-go 闸门。

## 十、风险与不做的事

- **最大风险**：`registerOverlay` 本为「用户拖拽创建」设计，而缠论是纯数据驱动的非典型用法 —— 若锚点式 `points` 不触发 `createPointFigures`，需退到「每笔一个 overlay」的兜底（笔 ~30 + 中枢 ~8，overlay 数量百级，性能可接受，工作量 +1 天）。由第 1 步验证兜底，不影响其它部分。
- **不做**：把缠论计算搬到前端（后端已有成熟实现 + 事件研究结论，前端重算是重复造轮子且口径必然分叉）
- **不做**：迁移期间同时改缠论口径（严格笔/宽松笔参数一律不动，保证对拍可比）
