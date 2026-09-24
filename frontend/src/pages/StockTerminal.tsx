/**
 * 个股终端(全屏工作区) —— P0 容器 + P2 终端形态。
 *
 * P0: 独立路由 /stock/:symbol, 占满视口, 图表高度自适应, 价格单一源, 承载触发上下文。
 * P2: 三栏可折叠工作区 + 键盘优先 + 响应式三档
 *       >=1440  左栏(股票轨道) + 图表 + 右栏(盘口)
 *       1024-1440  图表 + 右栏(左栏收为抽屉)
 *       <1024   图表全宽(左右都收为抽屉)
 *
 * 图表内核: 默认 ECharts(StockPanel), 灰度开关可切到 KLineChart(KLinePro, P1)。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useQuote, useElementHeight } from '@/lib/useQuote'
import { usePreferences, useQuoteStatus } from '@/lib/useSharedQueries'
import { setFocusSymbol, clearFocusSymbol } from '@/lib/useQuoteStream'
import { useLayoutMode } from '@/lib/useLayoutMode'
import { useRecentStocks } from '@/lib/useRecentStocks'
import { StockPanel, getDefaultRange } from '@/components/StockPanel'
import { DepthPanel } from '@/components/DepthPanel'
import { DatePicker } from '@/components/DatePicker'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { TerminalHeader } from '@/components/stock-terminal/TerminalHeader'
import { ContextRibbon, type TriggerContext } from '@/components/stock-terminal/ContextRibbon'
import { StockRail, type RailItem } from '@/components/stock-terminal/StockRail'
import { CommandPalette } from '@/components/stock-terminal/CommandPalette'
import { ShortcutHelp } from '@/components/stock-terminal/ShortcutHelp'
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

/** 区间选择条 —— 从旧弹窗顶栏下沉到图表上方 */
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
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
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

/** 抽屉入口小按钮 */
function DrawerButton({ side, label, onClick }: { side: 'left' | 'right'; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={`展开${label}`}
      className="h-6 shrink-0 rounded border border-border bg-elevated px-1.5 text-[11px] text-muted transition-colors hover:text-foreground"
    >
      {side === 'left' ? '› ' : ''}{label}{side === 'right' ? ' ‹' : ''}
    </button>
  )
}

export function StockTerminal() {
  const { symbol = '' } = useParams<{ symbol: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const qc = useQueryClient()

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
  const [period, setPeriod] = useState<'day' | 'week' | 'month'>('day')
  const [priceLines, setPriceLines] = useState<ChartPriceLine[]>([])
  const [showMonitor, setShowMonitor] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)

  // ── P2 布局 ────────────────────────────────────────────────
  const mode = useLayoutMode()
  const [railOpen, setRailOpen] = useState(() => mode === 'three')
  const [depthOpen, setDepthOpen] = useState(() => mode !== 'one')
  const prevMode = useRef(mode)
  useEffect(() => {
    if (prevMode.current === mode) return
    prevMode.current = mode
    setRailOpen(mode === 'three')
    setDepthOpen(mode !== 'one')
  }, [mode])
  const railDocked = mode === 'three' && railOpen
  const railDrawer = mode !== 'three' && railOpen
  const depthDocked = mode !== 'one' && depthOpen
  const depthDrawer = mode === 'one' && depthOpen

  // 图表高度随窗口自适应
  const boxRef = useRef<HTMLDivElement>(null)
  const chartHeight = useElementHeight(boxRef, 92, 260)

  // 焦点股票注册: SSE 推送时精准刷新当前股票日K
  useEffect(() => {
    if (!symbol) return
    setFocusSymbol(symbol)
    return () => clearFocusSymbol()
  }, [symbol])

  // ── 触发上下文 ────────────────────────────────────────────
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

  // ── 自选 ──────────────────────────────────────────────────
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

  // ── 左栏轨道 / [ ] 换股序列 ────────────────────────────────
  const { recent } = useRecentStocks({ symbol, name: quote.raw?.name ?? undefined })
  const watchItems = useMemo<RailItem[]>(
    () => (watchlist.data?.symbols ?? []).map(s => ({ symbol: s.symbol, name: s.name ?? null })),
    [watchlist.data],
  )
  const navSeq = useMemo(() => {
    const seen = new Set<string>()
    const out: string[] = []
    for (const it of [...watchItems, ...recent]) {
      if (seen.has(it.symbol)) continue
      seen.add(it.symbol)
      out.push(it.symbol)
    }
    return out
  }, [watchItems, recent])

  const gotoSymbol = useCallback((next: string) => {
    if (!next || next === symbol) return
    navigate(`/stock/${next}`, { state: location.state ?? undefined })
  }, [location.state, navigate, symbol])

  const stepSymbol = useCallback((delta: number) => {
    if (navSeq.length === 0) return
    const idx = navSeq.indexOf(symbol)
    if (idx < 0) { gotoSymbol(navSeq[0]); return }
    gotoSymbol(navSeq[(idx + delta + navSeq.length) % navSeq.length])
  }, [gotoSymbol, navSeq, symbol])

  const handleRefresh = () => {
    qc.invalidateQueries({ queryKey: ['kline', symbol] })
    qc.invalidateQueries({ queryKey: ['depth', symbol] })
  }

  const handleLocatePrice = (price: number) => {
    setPriceLines([{ value: price, color: '#F79009', label: '触发价' }])
  }

  const handleToggleKLine = useCallback(() => {
    setUseKLine(v => {
      const next = !v
      setKLineProFlag(next)
      return next
    })
  }, [])

  // ── P2 键盘优先 ────────────────────────────────────────────
  useEffect(() => {
    if (!symbol) return
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(true)
        return
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return
      if (paletteOpen || helpOpen || showMonitor) {
        if (e.key === 'Escape') {
          e.preventDefault()
          setPaletteOpen(false)
          setHelpOpen(false)
          setShowMonitor(false)
        }
        return
      }
      switch (e.key) {
        case '/':
          e.preventDefault(); setPaletteOpen(true); break
        case '?':
          e.preventDefault(); setHelpOpen(true); break
        case 'c':
          setChanOn(v => !v); break
        case 'r':
          setRailOpen(v => !v); break
        case 'p':
          setDepthOpen(v => !v); break
        case 'g':
          handleToggleKLine(); break
        case '[':
          stepSymbol(-1); break
        case ']':
          stepSymbol(1); break
        case '1':
          if (useKLine) setPeriod('day'); break
        case '2':
          if (useKLine) setPeriod('week'); break
        case '3':
          if (useKLine) setPeriod('month'); break
        case 'Escape':
          e.preventDefault(); navigate(-1); break
        default:
          break
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [handleToggleKLine, helpOpen, navigate, paletteOpen, showMonitor, stepSymbol, symbol, useKLine])

  if (!symbol) {
    return (
      <div className="grid h-full place-items-center text-sm text-muted">
        缺少股票代码
      </div>
    )
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col">
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
        {railDocked && (
          <aside className="w-[190px] shrink-0">
            <StockRail watchlist={watchItems} recent={recent} current={symbol} onSelect={gotoSymbol} className="h-full" />
          </aside>
        )}

        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 pb-1.5">
            {!railDocked && !railDrawer && (
              <DrawerButton side="left" label="股票" onClick={() => setRailOpen(true)} />
            )}
            <RangeBar value={dateRange} onChange={setDateRange} />
            <div className="ml-auto flex items-center gap-1.5">
              {useKLine && (
                <div className="flex items-center gap-0.5 rounded border border-border/70 p-0.5">
                  {([['day', '日'], ['week', '周'], ['month', '月']] as const).map(([k, label]) => (
                    <button
                      key={k}
                      type="button"
                      onClick={() => setPeriod(k)}
                      className={cn(
                        'h-5 rounded px-1.5 text-[10px] font-mono transition-colors',
                        period === k ? 'bg-accent text-white' : 'text-muted hover:text-secondary',
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              )}
              <button
                onClick={() => setChanOn(v => !v)}
                title="缠论笔 / 中枢 / 买卖点 (c)"
                className={cn(
                  'h-6 rounded border px-2 text-[11px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
                  chanOn ? 'border-accent/30 bg-accent/20 text-accent' : 'border-transparent text-muted hover:bg-elevated',
                )}
              >
                缠论
              </button>
              <button
                onClick={handleToggleKLine}
                title={useKLine ? '切回 ECharts 内核 (g)' : '试用 KLineChart 内核 (g)'}
                className={cn(
                  'h-6 rounded border px-2 text-[11px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent',
                  useKLine ? 'border-amber-500/30 bg-amber-500/20 text-amber-400' : 'border-transparent text-muted hover:bg-elevated',
                )}
              >
                {useKLine ? 'KLinePro' : 'ECharts'}
              </button>
              <button
                onClick={() => setHelpOpen(true)}
                title="键盘快捷键 (?)"
                className="h-6 rounded border border-transparent px-1.5 text-[11px] font-mono text-muted transition-colors hover:bg-elevated hover:text-foreground"
              >
                ?
              </button>
              {!depthDocked && !depthDrawer && (
                <DrawerButton side="right" label="盘口" onClick={() => setDepthOpen(true)} />
              )}
            </div>
          </div>
          <div ref={boxRef} className="min-h-0 flex-1">
            {useKLine ? (
              <KLinePro
                symbol={symbol}
                dateRange={dateRange}
                period={period}
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

        {depthDocked && (
          <aside className="w-[190px] shrink-0 overflow-hidden rounded-card border border-border bg-surface">
            <DepthPanel symbol={symbol} refetchIntervalMs={refetchMs} />
          </aside>
        )}
      </div>

      {/* 抽屉形态：窄屏时左右两栏浮在图表之上 */}
      {railDrawer && (
        <div className="absolute inset-y-0 left-0 z-20 flex w-[220px] p-3">
          <div className="absolute inset-0 bg-black/40" onClick={() => setRailOpen(false)} />
          <div className="relative h-full w-full">
            <StockRail
              watchlist={watchItems}
              recent={recent}
              current={symbol}
              onSelect={s => { gotoSymbol(s); setRailOpen(false) }}
              className="h-full"
            />
          </div>
        </div>
      )}
      {depthDrawer && (
        <div className="absolute inset-y-0 right-0 z-20 flex w-[220px] p-3">
          <div className="absolute inset-0 bg-black/40" onClick={() => setDepthOpen(false)} />
          <div className="relative h-full w-full overflow-hidden rounded-card border border-border bg-surface">
            <DepthPanel symbol={symbol} refetchIntervalMs={refetchMs} />
          </div>
        </div>
      )}

      {paletteOpen && (
        <CommandPalette
          current={symbol}
          recent={recent}
          onPick={s => { setPaletteOpen(false); gotoSymbol(s) }}
          onClose={() => setPaletteOpen(false)}
        />
      )}

      {helpOpen && <ShortcutHelp onClose={() => setHelpOpen(false)} />}

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
