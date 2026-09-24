/**
 * 个股终端(全屏工作区) —— P0 的核心交付。
 *
 * 与旧弹窗(StockPreviewDialog)的分工:
 *  - 终端: 独立路由 /stock/:symbol, 占满视口, 图表高度随窗口自适应,
 *          全屏只有一个价格(与盘口共用 ['depth', symbol] 缓存), 承载触发上下文。
 *  - 弹窗: 保留为「快速预览」, 用于自选/监控列表里扫一眼, 不做重度分析。
 *
 * 图表内核仍是 ECharts(见 StockDailyKChart), P0 阶段刻意不动内核 ——
 * 先把容器和一致性问题修掉, 换内核放到 P1 单独做, 失败可独立回退。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useQuote, useElementHeight } from '@/lib/useQuote'
import { usePreferences, useQuoteStatus } from '@/lib/useSharedQueries'
import { setFocusSymbol, clearFocusSymbol } from '@/lib/useQuoteStream'
import { StockPanel, getDefaultRange } from '@/components/StockPanel'
import { DepthPanel } from '@/components/DepthPanel'
import { DatePicker } from '@/components/DatePicker'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { TerminalHeader } from '@/components/stock-terminal/TerminalHeader'
import { ContextRibbon, type TriggerContext } from '@/components/stock-terminal/ContextRibbon'
import { KLinePro } from '@/components/kline/KLinePro'
import { getKLineProFlag, setKLineProFlag } from '@/components/kline/useKLineProFlag'
import type { ChartPriceLine } from '@/components/EChartsCandlestick'
import { cn } from '@/lib/cn'

const PRESETS: { label: string; months: number }[] = [
  { label: '近1月', months: 1 },
  { label: '近3月', months: 3 },
  { label: '近6月', months: 6 },
  { label: '近1年', months: 12 },
  { label: '近3年', months: 36 },
]

function iso(d: Date): string {
  return d.toISOString().slice(0, 10)
}

/**
 * 区间选择条 —— 从旧弹窗顶栏下沉到图表上方。
 * 顶栏因此只剩「标识 + 价格 + 动作」, 视觉重心回到图表。
 */
function RangeBar({
  value,
  onChange,
}: {
  value: { start: string; end: string }
  onChange: (v: { start: string; end: string }) => void
}) {
  const pick = (months: number) => {
    const end = new Date()
    const s = new Date()
    s.setMonth(s.getMonth() - months)
    onChange({ start: iso(s), end: iso(end) })
  }
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5 pb-1.5">
      {PRESETS.map(p => {
        const s = new Date()
        s.setMonth(s.getMonth() - p.months)
        const active = value.start === iso(s)
        return (
          <button
            key={p.label}
            type="button"
            onClick={() => pick(p.months)}
            className={cn(
              'h-6 rounded border px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
              active
                ? 'border-accent/30 bg-accent/20 font-medium text-accent'
                : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
            )}
          >
            {p.label}
          </button>
        )
      })}
      <DatePicker value={value.start} onChange={v => onChange({ ...value, start: v })} max={value.end} />
      <span className="text-[10px] text-muted/40">~</span>
      <DatePicker value={value.end} onChange={v => onChange({ ...value, end: v })} min={value.start} />
    </div>
  )
}

export function StockTerminal() {
  const { symbol = '' } = useParams<{ symbol: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const qc = useQueryClient()

  // 实时刷新: 复用自选列表的「分时刷新开关 + 间隔」偏好, 与弹窗口径一致
  const { data: prefs } = usePreferences()
  const { data: quoteStatus } = useQuoteStatus()
  const realtimeRunning = quoteStatus?.running ?? false
  const intradayRefreshOn = prefs?.minute_intraday_refresh ?? false
  const refetchMs = intradayRefreshOn && realtimeRunning
    ? (prefs?.minute_intraday_refresh_interval ?? 6) * 1000
    : undefined

  // 全屏唯一的价格源(与 DepthPanel 共享缓存)
  const quote = useQuote(symbol, refetchMs)

  const [dateRange, setDateRange] = useState(() => getDefaultRange())
  const [chanOn, setChanOn] = useState(false)
  const [useKLine, setUseKLine] = useState(() => getKLineProFlag())
  const [priceLines, setPriceLines] = useState<ChartPriceLine[]>([])
  const [showMonitor, setShowMonitor] = useState(false)

  // 图表高度随窗口自适应: 扣掉信息条约 92px, 替代写死的 420
  const boxRef = useRef<HTMLDivElement>(null)
  const chartHeight = useElementHeight(boxRef, 92, 260)

  // 焦点股票注册: SSE 推送时精准刷新当前股票日K
  useEffect(() => {
    if (!symbol) return
    setFocusSymbol(symbol)
    return () => clearFocusSymbol()
  }, [symbol])

  // Esc 返回上一页
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !showMonitor) navigate(-1)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [navigate, showMonitor])

  // ── 触发上下文: 优先跳转时带入的 state, 其次该股当前异动 ──────────────
  const stateCtx = (location.state as { trigger?: TriggerContext } | null)?.trigger ?? null
  const abnormal = useQuery({
    queryKey: QK.abnormalOverview(0.5, 300),
    queryFn: () => api.abnormalOverview(0.5, 300),
    enabled: !!symbol && stateCtx === null,
    staleTime: 60_000,
  })
  const abRow = useMemo(() => {
    const rows = (abnormal.data as unknown as { rows?: Record<string, unknown>[] } | undefined)?.rows
    if (!rows) return null
    return rows.find(r => r.symbol === symbol) ?? null
  }, [abnormal.data, symbol])

  const ctx: TriggerContext | null = useMemo(() => {
    if (stateCtx) return stateCtx
    if (!abRow) return null
    return {
      kind: 'abnormal',
      label: `异动 · ${String(abRow.status ?? '关注')}`,
      ts: (abRow.ts as number | string | undefined) ?? null,
      price: (abRow.price as number | null | undefined) ?? null,
      changePct: (abRow.change_pct as number | null | undefined) ?? null,
      message: (abRow.message as string | undefined) ?? '',
      signals: (abRow.signals as string[] | undefined) ?? [],
      backTo: '/abnormal',
    }
  }, [stateCtx, abRow])

  // ── 自选 ──────────────────────────────────────────────────────
  const watchlist = useQuery({ queryKey: QK.watchlist, queryFn: () => api.watchlistList() })
  const inWatchlist = useMemo(
    () => (watchlist.data?.symbols ?? []).some(s => s.symbol === symbol),
    [watchlist.data, symbol],
  )
  const toggleWatchlist = useMutation({
    mutationFn: (action: 'add' | 'remove') =>
      action === 'add' ? api.watchlistAdd(symbol) : api.watchlistRemove(symbol),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
    },
  })

  const handleRefresh = () => {
    qc.invalidateQueries({ queryKey: ['kline', symbol] })
    qc.invalidateQueries({ queryKey: ['depth', symbol] })
  }

  const handleLocatePrice = (price: number) => {
    setPriceLines([{ value: price, color: '#F79009', label: '触发价' }])
  }

  const handleToggleKLine = () => {
    setUseKLine(v => {
      const next = !v
      setKLineProFlag(next)
      return next
    })
  }

  if (!symbol) {
    return (
      <div className="grid h-full place-items-center text-sm text-muted">
        缺少股票代码
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <TerminalHeader
        symbol={symbol}
        name={quote.raw?.name ?? undefined}
        quote={quote}
        inWatchlist={inWatchlist}
        onToggleWatchlist={() => toggleWatchlist.mutate(inWatchlist ? 'remove' : 'add')}
        watchlistPending={toggleWatchlist.isPending}
        onMonitor={() => setShowMonitor(true)}
        onRefresh={handleRefresh}
        onClose={() => navigate(-1)}
      />

      <ContextRibbon ctx={ctx} onLocatePrice={handleLocatePrice} />

      <div className="flex min-h-0 flex-1 gap-3 px-3 pb-3 pt-2">
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 items-center justify-between pb-1.5">
            <RangeBar value={dateRange} onChange={setDateRange} />
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setChanOn(v => !v)}
                className={cn(
                  'h-6 rounded border px-2 text-[11px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
                  chanOn ? 'border-accent/30 bg-accent/20 text-accent' : 'border-transparent text-muted hover:bg-elevated',
                )}
              >
                缠论
              </button>
              <button
                onClick={handleToggleKLine}
                title={useKLine ? '切回 ECharts 内核' : '试用 KLineChart 内核'}
                className={cn(
                  'h-6 rounded border px-2 text-[11px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
                  useKLine ? 'border-amber-500/30 bg-amber-500/20 text-amber-400' : 'border-transparent text-muted hover:bg-elevated',
                )}
              >
                {useKLine ? 'KLinePro' : 'ECharts'}
              </button>
            </div>
          </div>
          <div ref={boxRef} className="min-h-0 flex-1">
            {useKLine ? (
              <KLinePro
                symbol={symbol}
                dateRange={dateRange}
                chanEnabled={chanOn}
                priceLines={priceLines}
              />
            ) : (
              <StockPanel
                symbol={symbol}
                height={chartHeight}
                dateRange={dateRange}
                priceLines={priceLines}
                chanOverlay={chanOn}
                onToggleChan={() => setChanOn(v => !v)}
                refetchIntervalMs={refetchMs}
                inWatchlist={inWatchlist}
                onAddToWatchlist={() => toggleWatchlist.mutate('add')}
                onRemoveFromWatchlist={() => toggleWatchlist.mutate('remove')}
                watchlistPending={toggleWatchlist.isPending}
                liveQuote={quote}
              />
            )}
          </div>
        </main>

        <aside className="w-[180px] shrink-0 overflow-hidden rounded-card border border-border bg-surface">
          <DepthPanel symbol={symbol} refetchIntervalMs={refetchMs} />
        </aside>
      </div>

      {showMonitor && (
        <div
          className="absolute inset-0 z-20 flex items-start justify-center overflow-auto bg-black/40 p-4"
          onClick={() => setShowMonitor(false)}
        >
          <div className="mt-8 w-full max-w-2xl" onClick={e => e.stopPropagation()}>
            <RuleEditor
              rule={null}
              simple
              preset={{ scope: 'symbols', symbols: [symbol], type: 'signal', logic: 'or' }}
              onClose={() => setShowMonitor(false)}
              onSaved={() => setShowMonitor(false)}
            />
          </div>
        </div>
      )}
    </div>
  )
}
