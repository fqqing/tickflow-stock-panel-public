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
 * 当前能力: 多周期 K + 成交量 + MA + 缠论叠加(仅日线档) + 监控价位水平线 + 暗色主题(红涨绿跌)。
 * 未做: 复权切换、副图指标自选、涨停标记、手绘线、分时(均价)图。
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

  // 初始化图表（仅一次）
  useEffect(() => {
    const el = containerRef.current
    if (!el || chartRef.current) return
    registerChanOverlay()
    registerPriceLineOverlay()

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
    chart.createIndicator('MA')
    chart.createIndicator('VOL')
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
      </div>
    </div>
  )
}
