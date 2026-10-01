/**
 * KLineChart 内核的个股 K 线组件。
 *
 * S1: 多周期打通 —— 日 / 周 / 月 + 1m / 5m / 15m / 30m / 60m / 90m / 120m。
 *   - 日/周/月 走 /api/kline/daily(周月由后端按日 K 聚合并重算指标)
 *   - 分钟档 走 /api/kline/minute-k, 后端两级数据源(响应 source 字段标明):
 *       preagg = 预聚合周期目录(可回溯到 2025-01), local = 1m 现场聚合
 *
 * ★ 时间戳口径(与 ECharts 的 formatMinuteTime 同判据, 别改成无条件 +8):
 *   后端分钟 datetime 有两种口径并存 ——
 *     naive UTC(北京 09:30 记成 01:30) 与 北京墙钟(09:30 记成 09:30)。
 *   判别: hour < 8 视为前者(它本身就是真实 epoch), hour >= 8 视为后者(需减 8h)。
 *   图表再 setTimezone('Asia/Shanghai'), 保证 x 轴/十字线按北京时间显示。
 *
 * ★ klinecharts v10 不做周期聚合(源码里没有 aggregate): setPeriod 只影响
 *   轴标签/十字线的时间格式。所以聚合必须由后端完成, 前端只负责喂数。
 *
 * S2: 主副图窗格 + 指标管理 —— 指标清单持久化在 localStorage(换股不换指标),
 *   主图指标挂 candle_pane, 副图自动开新窗格; 窗格分隔条可拖动, 高度同样持久化。
 *   指标参数**不硬编码**: 创建时不传 calcParams, 再从 getIndicators() 回读库内默认值。
 *
 * 当前能力: 多周期 K + 指标自选(27 个内置) + 缠论叠加(仅日线档) + 监控价位水平线 + 暗色主题(红涨绿跌)。
 * 未做: 复权切换、涨停标记、手绘线、分时(均价)图。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as kc from 'klinecharts'
import { api, KLINE_CHART_FIELDS, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChanOverlay } from '@/lib/useChanOverlay'
import type { ChartPriceLine } from '@/lib/chart-primitives'
import { isMinutePeriod, type KLinePeriod } from '@/components/StockDailyKChart'
import { registerChanOverlay } from './chan-overlay-kline'
import { registerPriceLineOverlay } from './price-line-overlay'
import { registerChipsOverlay } from './chips-overlay'
import { IndicatorManager } from './IndicatorManager'
import {
  MAIN_PANE_ID,
  loadIndicators,
  loadPaneHeights,
  saveIndicators,
  savePaneHeights,
  type IndicatorConfig,
} from '@/lib/klineIndicators'
import { cn } from '@/lib/cn'

const BULL = '#F04438' // --bull 红涨
const BEAR = '#12B76A' // --bear 绿跌
const CHART_BG = '#0B1220'
const GRID = '#1E293B'
const TEXT = '#94A3B8'

const CUSTOM_INDICATORS = 'trend_dragon,capital_momentum,structure,macd_structure'

/** 分钟档回看天数(交给后端 days 参数) */
const MINUTE_LOOKBACK_DAYS = 120
/**
 * 分钟档只取最近 N 根。5m/120 交易日全量是 8640 根 ≈ 1MB, 图也塞不下;
 * 截断到 800 根 ≈ 80KB。指标是后端在**完整数据**上算好之后再截的, 不失真。
 */
const MINUTE_BAR_LIMIT = 800

/** 筹码分布回望交易日数 / 价格档数 */
const CHIPS_DAYS = 250
const CHIPS_BINS = 60

/** 分钟周期 -> klinecharts span */
const MINUTE_SPAN: Record<string, number> = {
  '1m': 1, '5m': 5, '15m': 15, '30m': 30, '60m': 60, '90m': 90, '120m': 120,
}

/** 图内周期工具条(90m/120m 后端支持, 但终端常用档位里不放) */
const PERIOD_TABS: { key: KLinePeriod; label: string }[] = [
  { key: 'day', label: '日' },
  { key: 'week', label: '周' },
  { key: 'month', label: '月' },
  { key: '1m', label: '1分' },
  { key: '5m', label: '5分' },
  { key: '15m', label: '15分' },
  { key: '30m', label: '30分' },
  { key: '60m', label: '60分' },
]

const CN_OFFSET_MS = 8 * 60 * 60 * 1000

/**
 * 后端日期 -> 毫秒 epoch。
 * 兼容三种形态: 纯日期(日/周/月)、带时间的分钟戳、以及已是数字的时间戳。
 */
function parseTs(v: unknown): number | null {
  if (v == null) return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(String(v).trim())
  if (!m) return null
  const [, y, mo, d, hh, mi, ss] = m
  const ts = Date.UTC(+y, +mo - 1, +d, hh ? +hh : 0, mi ? +mi : 0, ss ? +ss : 0)
  if (!Number.isFinite(ts)) return null
  // hour < 8 => 源里存的是 naive UTC(北京 09:30 记成 01:30), 它本身就是真实 epoch
  // hour >= 8 => 源里存的是北京墙钟, 必须减 8h 才是真实 epoch
  return (hh ? +hh : 0) < 8 ? ts : ts - CN_OFFSET_MS
}

function rowToKLine(r: KlineRow): kc.KLineData | null {
  if (!r || r.date == null || r.open == null || r.close == null) return null
  const ts = parseTs(r.date)
  if (ts == null) return null
  return {
    timestamp: ts,
    open: Number(r.open),
    high: Number(r.high ?? r.close),
    low: Number(r.low ?? r.close),
    close: Number(r.close),
    volume: Number(r.volume ?? 0),
  }
}

function parseRows(rows: KlineRow[]): kc.KLineData[] {
  return rows.map(rowToKLine).filter((d): d is kc.KLineData => d !== null)
}

/** 从已创建的指标回读库内默认参数(避免硬编码, 见 klineIndicators 顶部说明) */
function readRealParams(ind: kc.Indicator | undefined): number[] | null {
  const cp = ind?.calcParams
  if (!Array.isArray(cp) || cp.length === 0) return null
  const nums = cp.filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  return nums.length === cp.length ? nums : null
}

/** 恢复上次拖动过的副图窗格高度(按副图顺序, paneId 是库内自增的, 不能当 key) */
function applyPaneHeights(chart: kc.Chart): void {
  const saved = loadPaneHeights()
  if (saved.length === 0) return
  const opts = chart.getPaneOptions()
  if (!Array.isArray(opts)) return
  opts.slice(1).forEach((p, i) => {
    const h = saved[i]
    if (h) chart.setPaneOptions({ id: p.id, height: h, dragEnabled: true })
  })
}

function toChartPeriod(p: KLinePeriod): kc.Period {
  const span = MINUTE_SPAN[p]
  if (span) return { type: 'minute', span }
  if (p === 'week') return { type: 'week', span: 1 }
  if (p === 'month') return { type: 'month', span: 1 }
  return { type: 'day', span: 1 }
}

export interface KLineProProps {
  symbol: string
  className?: string
  dateRange: { start: string; end: string }
  /** 当前周期; 不传则组件自己维护(内部工具条) */
  period?: KLinePeriod
  /** 受控模式: 由外层持有周期(终端键盘 1/2/3 与图内按钮共用一份状态) */
  onPeriodChange?: (p: KLinePeriod) => void
  chanEnabled?: boolean
  priceLines?: ChartPriceLine[]
  /** 盘中自动刷新间隔(毫秒); 分钟档配合实时同步使用 */
  refetchIntervalMs?: number
}

export function KLinePro({
  symbol,
  className,
  dateRange,
  period: periodProp,
  onPeriodChange,
  chanEnabled = false,
  priceLines = [],
  refetchIntervalMs,
}: KLineProProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<kc.Chart | null>(null)
  const rowsRef = useRef<kc.KLineData[]>([])
  const [ready, setReady] = useState(false)

  // ── S2 指标清单(持久化在 localStorage, 换股不换指标) ──
  const [indicators, setIndicators] = useState<IndicatorConfig[]>(() => loadIndicators())
  const [managerOpen, setManagerOpen] = useState(false)
  /** key -> 图表内指标 id / 所在窗格 */
  const indRefs = useRef(new Map<string, { id: string; paneId: string }>())

  const [innerPeriod, setInnerPeriod] = useState<KLinePeriod>('day')
  const period = periodProp ?? innerPeriod
  const applyPeriod = useCallback((p: KLinePeriod) => {
    if (onPeriodChange) onPeriodChange(p)
    else setInnerPeriod(p)
  }, [onPeriodChange])
  const minutePeriod = isMinutePeriod(period)

  const days = useMemo(() => {
    const s = new Date(dateRange.start), e = new Date(dateRange.end)
    return Math.max(1, Math.ceil((e.getTime() - s.getTime()) / 86400000) + 1)
  }, [dateRange])

  const daily = useQuery({
    queryKey: QK.kline(symbol, dateRange.start, dateRange.end, undefined, period, 'qfq'),
    queryFn: () => api.klineDaily(symbol, days, dateRange, undefined, CUSTOM_INDICATORS, KLINE_CHART_FIELDS, period, 'qfq'),
    enabled: !!symbol && !minutePeriod,
  })

  const minuteK = useQuery({
    queryKey: QK.klineMinuteK(symbol, period, MINUTE_LOOKBACK_DAYS, MINUTE_BAR_LIMIT),
    queryFn: () => api.klineMinuteK(symbol, period, MINUTE_LOOKBACK_DAYS, KLINE_CHART_FIELDS, MINUTE_BAR_LIMIT),
    enabled: !!symbol && minutePeriod,
    refetchInterval: minutePeriod ? refetchIntervalMs : undefined,
  })

  // 两条查询互斥启用, 下游只读这一个
  const active = minutePeriod ? minuteK : daily

  const rows = useMemo(() => parseRows(active.data?.rows ?? []), [active.data?.rows])
  const chartDates = useMemo(
    () => (minutePeriod ? [] : rows.map(d => new Date(d.timestamp).toISOString().slice(0, 10))),
    [rows, minutePeriod],
  )

  // 缠论只在日线档有意义(笔/中枢按日线口径算), 分钟档直接关掉, 免得发无谓请求
  const chanLayers = useChanOverlay(symbol, chartDates, chanEnabled && !minutePeriod)

  // ── S3 筹码分布: 工具条开关控制, 只在日线档取(分钟档没有"持仓成本"意义) ──
  const [chipsOn, setChipsOn] = useState(false)
  const chips = useQuery({
    queryKey: QK.stockChips(symbol, CHIPS_DAYS, CHIPS_BINS),
    queryFn: () => api.stockAnalysisChips(symbol, { days: CHIPS_DAYS, bins: CHIPS_BINS }),
    enabled: !!symbol && chipsOn && !minutePeriod,
    staleTime: 10 * 60 * 1000,
  })
  const chipsData = useMemo(() => {
    const d = chips.data
    if (!d?.ok || !d.bins?.length) return null
    return { bins: d.bins, close: d.close, avg_cost: d.avg_cost, step: d.step }
  }, [chips.data])

  // 初始化图表（仅一次）
  useEffect(() => {
    const el = containerRef.current
    if (!el || chartRef.current) return
    // 换股会重建图表: 先把 ready 打回 false, 否则 setReady(true) 同值不触发重渲染,
    // 指标 diff 的 effect 不会重跑, 新图上就一个指标都没有。
    setReady(false)
    indRefs.current.clear()
    registerChanOverlay()
    registerPriceLineOverlay()
    registerChipsOverlay()

    const chart = kc.init(el, {
      styles: {
        grid: { horizontal: { color: GRID }, vertical: { color: GRID } },
        candle: {
          bar: { upColor: BULL, downColor: BEAR, noChangeColor: TEXT },
          priceMark: { last: { upColor: BULL, downColor: BEAR, noChangeColor: TEXT } },
        },
        xAxis: { axisLine: { color: GRID }, tickLine: { color: GRID }, tickText: { color: TEXT } },
        yAxis: { axisLine: { color: GRID }, tickLine: { color: GRID }, tickText: { color: TEXT } },
        separator: { color: GRID },
        crosshair: {
          horizontal: { text: { color: TEXT, backgroundColor: CHART_BG }, line: { color: '#475569' } },
          vertical: { text: { color: TEXT, backgroundColor: CHART_BG }, line: { color: '#475569' } },
        },
      },
    })
    if (!chart) return
    chartRef.current = chart

    // 后端给的是北京时间口径的墙钟/naive-UTC 字符串, 已统一转成真实 epoch;
    // 这里指定时区, 让 x 轴与十字线按北京时间渲染。
    chart.setTimezone('Asia/Shanghai')

    chart.setDataLoader({
      getBars: ({ type, callback }) => {
        if (type === 'init') callback(rowsRef.current, { backward: false, forward: false })
        else callback([], false)
      },
    })
    chart.setSymbol({ ticker: symbol, pricePrecision: 2, volumePrecision: 0 })
    chart.setPeriod(toChartPeriod(period))
    setReady(true)

    return () => {
      kc.dispose(el)
      chartRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol])

  // 数据刷新
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    rowsRef.current = rows
    chart.resetData()
  }, [rows])

  // 周期切换: setPeriod 内部会 resetData 重新走 loader, 先清空 rowsRef
  // 避免切档瞬间用上一档的数据重绘(会看到错周期的 K 线)。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    rowsRef.current = []
    chart.setPeriod(toChartPeriod(period))
  }, [period, ready])

  // 缠论叠加层：创建一次，之后用 overrideOverlay 更新 extendData
  const chanOverlayIdRef = useRef<string | null>(null)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    const payload = {
      polylines: chanLayers.polylines,
      ranges: chanLayers.ranges,
      markers: chanLayers.markers,
    }
    if (!chanOverlayIdRef.current) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'chan',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: payload,
      })
      if (typeof id === 'string') chanOverlayIdRef.current = id
    } else {
      chart.overrideOverlay({ id: chanOverlayIdRef.current, extendData: payload })
    }
  }, [chanLayers, ready])

  // 筹码分布: 创建一次, override 更新 extendData
  const chipsOverlayIdRef = useRef<string | null>(null)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    if (!chipsData) {
      // 关掉开关时要真的移除, 否则图上残留旧筹码
      if (chipsOverlayIdRef.current) {
        chart.removeOverlay({ id: chipsOverlayIdRef.current })
        chipsOverlayIdRef.current = null
      }
      return
    }
    if (!chipsOverlayIdRef.current) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'chips',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: chipsData,
      })
      if (typeof id === 'string') chipsOverlayIdRef.current = id
    } else {
      chart.overrideOverlay({ id: chipsOverlayIdRef.current, extendData: chipsData })
    }
  }, [chipsData, ready])

  // 监控价位线：创建一次，override 更新
  const priceOverlayIdRef = useRef<string | null>(null)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    if (!priceOverlayIdRef.current) {
      const first = rowsRef.current[0]
      if (!first) return
      const id = chart.createOverlay({
        name: 'priceLine',
        paneId: 'candle_pane',
        points: [{ timestamp: first.timestamp, value: first.close }],
        extendData: priceLines,
      })
      if (typeof id === 'string') priceOverlayIdRef.current = id
    } else {
      chart.overrideOverlay({ id: priceOverlayIdRef.current, extendData: priceLines })
    }
  }, [priceLines, ready])

  // ── S2: 指标清单 diff 到图表 ──
  // 增删改一律走增量, 不做「全量重建」 —— 重建窗格会把用户拖动过的高度一起丢掉。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    const refs = indRefs.current
    const alive = new Set(indicators.map(c => c.key))
    const defaults: IndicatorConfig[] = []

    for (const [key, ref] of Array.from(refs.entries())) {
      if (alive.has(key)) continue
      chart.removeIndicator({ id: ref.id })
      refs.delete(key)
    }

    for (const c of indicators) {
      const ref = refs.get(c.key)
      if (!ref) {
        const payload: kc.IndicatorCreate = { name: c.name }
        // 主图指标显式挂到 K 线窗格; 副图不传 paneId, 库会自动新开一个窗格
        if (c.group === 'main') payload.paneId = MAIN_PANE_ID
        if (c.params.length > 0) payload.calcParams = c.params
        const id = chart.createIndicator(payload)
        if (!id) continue
        const ind = chart.getIndicators({ id })[0]
        refs.set(c.key, { id, paneId: ind?.paneId ?? '' })
        if (c.params.length === 0) {
          const real = readRealParams(ind)
          if (real) defaults.push({ ...c, params: real })
        }
      } else if (c.params.length > 0) {
        chart.overrideIndicator({ id: ref.id, name: c.name, calcParams: c.params })
      }
    }

    // 首次创建时把库内默认参数回写进状态(这样管理面板才显示得出输入框)
    if (defaults.length > 0) {
      setIndicators(prev => prev.map(c => defaults.find(d => d.key === c.key) ?? c))
    }
    applyPaneHeights(chart)
  }, [indicators, ready])

  // 指标清单落盘。默认清单也要写 —— 否则 store 只有用户改过之后才有值,
  // 排查时看到的是空 localStorage, 与图上实际有指标对不上。
  useEffect(() => { saveIndicators(indicators) }, [indicators])

  // 拖动副图分隔条 -> 持久化高度(防抖 200ms)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    let timer: number | undefined
    const onDrag = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        const opts = chart.getPaneOptions()
        if (!Array.isArray(opts)) return
        savePaneHeights(opts.slice(1).map(p => p.height))
      }, 200)
    }
    chart.subscribeAction('onPaneDrag', onDrag)
    return () => {
      window.clearTimeout(timer)
      chart.unsubscribeAction('onPaneDrag', onDrag)
    }
  }, [ready])

  return (
    <div className={cn('relative flex h-full w-full flex-col', className)}>
      <div className="flex shrink-0 items-center gap-1 px-1 py-1">
        {PERIOD_TABS.map(t => (
          <button
            key={t.key}
            type="button"
            onClick={() => applyPeriod(t.key)}
            className={cn(
              'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              t.key === period
                ? 'border-accent/30 bg-accent/20 font-medium text-accent'
                : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
            )}
          >
            {t.label}
          </button>
        ))}
        <button
          type="button"
          onClick={() => setManagerOpen(v => !v)}
          title="指标设置(主图 / 副图、参数、窗格高度可拖动)"
          className={cn(
            'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
            managerOpen
              ? 'border-accent/30 bg-accent/20 font-medium text-accent'
              : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
          )}
        >
          指标{indicators.length > 0 ? ` ${indicators.length}` : ''}
        </button>
        <button
          type="button"
          onClick={() => setChipsOn(v => !v)}
          disabled={minutePeriod}
          title={minutePeriod ? '筹码分布只在日线档有意义' : '筹码分布(成本分布): 右侧横条, 红=获利盘 / 绿=套牢盘'}
          className={cn(
            'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
            chipsOn && !minutePeriod
              ? 'border-accent/30 bg-accent/20 font-medium text-accent'
              : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
            minutePeriod && 'cursor-not-allowed opacity-40',
          )}
        >
          筹码
        </button>
        {chipsOn && !minutePeriod && chips.data?.ok && (
          <span className="ml-1 text-[10px] text-muted" title="平均成本 / 获利盘比例">
            成本 {chips.data.avg_cost?.toFixed(2) ?? '—'} · 获利{' '}
            {chips.data.profit_ratio != null ? `${(chips.data.profit_ratio * 100).toFixed(1)}%` : '—'}
          </span>
        )}
        {minutePeriod && (
          <span className="ml-auto pr-1 text-[10px] text-muted/60" title="分钟K数据源: preagg=预聚合目录 / local=1m现场聚合">
            {period} · {active.data?.source ?? '…'}
          </span>
        )}
      </div>
      <div className="relative min-h-0 flex-1">
        {active.isLoading && (
          <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-muted">加载 K 线…</div>
        )}
        {active.isError && (
          <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-danger">K 线加载失败</div>
        )}
        {!active.isLoading && !active.isError && rows.length === 0 && (
          <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-muted">
            暂无该周期数据
          </div>
        )}
        <div ref={containerRef} className="h-full w-full" />
        {managerOpen && (
          <IndicatorManager
            configs={indicators}
            onChange={setIndicators}
            onClose={() => setManagerOpen(false)}
          />
        )}
      </div>
    </div>
  )
}
