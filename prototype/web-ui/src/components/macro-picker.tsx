import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { MACROS, formatBytes, formatLoop, type MacroFixture } from '@/prototype/model'
import { useStore } from '@/prototype/store'
import { CircleX, FolderX, RefreshCw, Search } from 'lucide-react'
import { useMemo, useState } from 'react'

function MacroRow({
  macro,
  selected,
  onSelect,
}: {
  macro: MacroFixture
  selected: boolean
  onSelect: () => void
}) {
  const rejected = macro.status === 'rejected'
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        disabled={rejected}
        aria-pressed={selected}
        id={`macro-${macro.id}`}
        className={cn(
          'w-full rounded-lg border bg-card p-3 text-left transition-colors',
          'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none',
          selected && !rejected ? 'border-ring ring-1 ring-ring' : 'border-border',
          rejected ? 'cursor-not-allowed opacity-80' : 'hover:bg-accent/50',
        )}
      >
        <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-sm font-medium">{macro.name}</span>
              {rejected ? (
                <>
                  <Badge variant="destructive" className="gap-1">
                    <CircleX className="size-3" aria-hidden />
                    Rejected
                  </Badge>
                  {macro.synthetic ? <span className="text-xs text-muted-foreground">(synthetic example)</span> : null}
                </>
              ) : null}
            </div>
            <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">{macro.source}</p>
          </div>
          {!rejected ? (
            <dl className="flex shrink-0 flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <div className="flex gap-1">
                <dt className="sr-only">Loop length</dt>
                <dd className="font-mono">{formatLoop(macro.loopMs)}</dd>
              </div>
              <div className="flex gap-1">
                <dt className="sr-only">Compiled plan size</dt>
                <dd className="font-mono">{formatBytes(macro.bytes)}</dd>
              </div>
              <div className="flex gap-1">
                <dt className="sr-only">Source events</dt>
                <dd className="font-mono">{macro.events} ev</dd>
              </div>
            </dl>
          ) : null}
        </div>
        {rejected && macro.rejection ? (
          <p className="mt-2 text-xs text-destructive">
            {macro.rejection.message} at t={macro.rejection.atMs} ms. {macro.rejection.fix}
          </p>
        ) : null}
      </button>
    </li>
  )
}

export function MacroPicker({ selectedId, onSelect }: { selectedId: string | null; onSelect: (id: string) => void }) {
  const { state } = useStore()
  const [q, setQ] = useState('')
  const [rescanning, setRescanning] = useState(false)

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return MACROS
    return MACROS.filter((m) => m.name.toLowerCase().includes(needle) || m.source.toLowerCase().includes(needle))
  }, [q])

  const empty = state.macroLibrary === 'EMPTY'

  function rescan() {
    setRescanning(true)
    window.setTimeout(() => setRescanning(false), 900)
  }

  return (
    <Card id="macro-library">
      <CardHeader className="gap-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-sm">Macro library</CardTitle>
          {!empty ? (
            <Button type="button" variant="outline" size="sm" onClick={rescan} disabled={rescanning}>
              <RefreshCw className={cn(rescanning && 'animate-spin')} aria-hidden />
              {rescanning ? 'Scanning…' : 'Rescan'}
            </Button>
          ) : null}
        </div>
        <CardDescription>
          {empty ? (
            'Nothing is mounted.'
          ) : (
            <>
              {MACROS.length} files in <span className="font-mono">/library/macros</span> — one is rejected, {MACROS.filter((m) => m.status === 'ready').length} can run.
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {empty ? (
          <div className="flex flex-col items-start gap-3 rounded-lg border border-dashed border-border p-6">
            <FolderX className="size-6 text-muted-foreground" aria-hidden />
            <div>
              <p className="text-sm font-medium">No macro library is mounted</p>
              <p className="text-sm text-muted-foreground">
                Mount the macro clone at <span className="font-mono">/library/macros</span> read-only, then rescan.
              </p>
            </div>
            <Button type="button" onClick={rescan} disabled={rescanning}>
              <RefreshCw className={cn(rescanning && 'animate-spin')} aria-hidden />
              Rescan library
            </Button>
          </div>
        ) : (
          <>
            <div className="relative">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
              <label htmlFor="macro-search" className="sr-only">
                Search macros
              </label>
              <Input
                id="macro-search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search by name or file…"
                className="pl-8"
                type="search"
              />
            </div>
            {list.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                No macros match “{q}”.{' '}
                <button type="button" className="underline underline-offset-2" onClick={() => setQ('')}>
                  Clear search
                </button>
              </p>
            ) : (
              <ul className="space-y-2">
                {list.map((m) => (
                  <MacroRow key={m.id} macro={m} selected={m.id === selectedId} onSelect={() => onSelect(m.id)} />
                ))}
              </ul>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
