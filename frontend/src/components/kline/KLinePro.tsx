/**
 * KLineChart 内核的个股日 K 组件（P1 起点）。
 *
 * 当前能力：
 *   - 日 K + 成交量 + MA
 *   - 缠论笔/中枢/买卖点叠加层
 *   - 监控价位水平线
 *   - 暗色主题, 红涨绿跌
 *
 * 未做（后续迭代）：周期切换、复权切换、分钟 K、手绘线、副图指标、涨停标记。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as kc from 'klinecharts'
import { api, KLINE_CHART_FIELDS, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChanOverlay } from '@/lib/useChanOverlay'
import type { ChartPriceLine } from '@/lib/chart-primitives'
import { registerChanOverlay } from './chan-overlay-kline'
import { registerPriceLineOverlay } from './price-line-overlay'
import { cn } from '@/lib/cn'

const BULL = '#F04438' // --bull 红涨
const BEAR = '#12B76A' // --bear 绿跌
const CHART_BG = '#0B1220'
const GRID = '#1E293B'
const TEXT = '#94A3B8'

const CUSTOM_INDICATORS = 'trend_dragon,capital_momentum,structure,macd_structure'

export interface KLineProProps {
  symbol: string
  className?: string
  dateRange: { start: string; end: string }
  chanEnabled?: boolean
  priceLines?: ChartPriceLine[]
}

function rowToKLine(r: KlineRow): kc.KLineData | null {
  if (!r || r.date == null || r.open == null || r.close == null) return null
  const date = typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date).slice(0, 10)
  const ts = Date.UTC(+date.slice(0, 4), +date.slice(5, 7) - 1, +date.slice(8, 10))
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

export function KLinePro({ symbol, className, dateRange, chanEnabled = false, priceLines = [] }: KLineProProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<kc.Chart | null>(null)
  const rowsRef = useRef<kc.KLineData[]>([])
  const [ready, setReady] = useState(false)

  const days = useMemo(() => {
    const s = new Date(dateRange.start), e = new Date(dateRange.end)
    return Math.max(1, Math.ceil((e.getTime() - s.getTime()) / 86400000) + 1)
  }, [dateRange])

  const kline = useQuery({
    queryKey: QK.kline(symbol, dateRange.start, dateRange.end, undefined, 'day', 'qfq'),
    queryFn: () => api.klineDaily(symbol, days, dateRange, undefined, CUSTOM_INDICATORS, KLINE_CHART_FIELDS, 'day', 'qfq'),
    enabled: !!symbol,
    placeholderData: prev => prev,
  })

  const rows = useMemo(() => parseRows(kline.data?.rows ?? []), [kline.data?.rows])
  const chartDates = useMemo(() => rows.map(d => {
    const t = new Date(d.timestamp)
    return t.toISOString().slice(0, 10)
  }), [rows])

  const chanLayers = useChanOverlay(symbol, chartDates, chanEnabled)

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

    chart.setDataLoader({
      getBars: ({ type, callback }) => {
        if (type === 'init') callback(rowsRef.current, { backward: false, forward: false })
        else callback([], false)
      },
    })
    chart.setSymbol({ ticker: symbol, pricePrecision: 2, volumePrecision: 0 })
    chart.setPeriod({ type: 'day', span: 1 })
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
      {kline.isLoading && (
        <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-muted">加载 K 线…</div>
      )}
      {kline.isError && (
        <div className="absolute inset-0 z-10 grid place-items-center bg-base/60 text-sm text-danger">K 线加载失败</div>
      )}
      <div ref={containerRef} className="h-full w-full" />
    </div>
  )
}
