import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { useStore } from '@/app/store'
import type { Figure } from '@/lib/model'
import { Nfc, Search } from 'lucide-react'
import { useMemo, useState } from 'react'

function FigureCard({ figure, selected, disabled, onSelect }: { figure: Figure; selected: boolean; disabled: boolean; onSelect: () => void }) {
  const file = figure.source.split('/').pop() ?? figure.source
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        disabled={disabled}
        aria-pressed={selected}
        className={cn(
          'flex h-full w-full flex-col gap-2 rounded-lg border bg-card p-3 text-left transition-colors',
          'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none',
          selected ? 'border-ring ring-1 ring-ring' : 'border-border',
          disabled ? 'cursor-not-allowed opacity-60' : 'hover:bg-accent/50',
        )}
      >
        <span className="flex size-10 items-center justify-center rounded-md border border-border bg-muted">
          <Nfc className="size-5 text-muted-foreground" aria-hidden />
        </span>
        <span className="text-sm font-medium leading-tight">{figure.name}</span>
        <span className="text-xs text-muted-foreground">{figure.series}</span>
        <span className="mt-auto truncate font-mono text-[0.65rem] text-muted-foreground">{file}</span>
      </button>
    </li>
  )
}

export function AmiiboPicker({
  selectedId,
  onSelect,
  locked,
}: {
  selectedId: string | null
  onSelect: (id: string) => void
  locked: boolean
}) {
  const { state } = useStore()
  const [q, setQ] = useState('')
  const [series, setSeries] = useState<string>('ALL')

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return state.figures.filter((f) => {
      if (series !== 'ALL' && f.series !== series) return false
      if (!needle) return true
      return f.name.toLowerCase().includes(needle) || f.series.toLowerCase().includes(needle) || f.source.toLowerCase().includes(needle)
    })
  }, [q, series, state.figures])

  return (
    <Card id="amiibo-library">
      <CardHeader className="gap-1">
        <CardTitle className="text-sm">Amiibo library</CardTitle>
        <CardDescription>
          {state.figures.length} tags indexed from <span className="font-mono">/library/amiibo</span> ·{' '}
          {state.series.length} series. Each placement re-seals under a fresh identity.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <label htmlFor="figure-search" className="sr-only">
              Search figures
            </label>
            <Input
              id="figure-search"
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search figures…"
              className="pl-8"
              disabled={locked}
            />
          </div>
          <div>
            <label htmlFor="series-filter" className="sr-only">
              Filter by series
            </label>
            <Select value={series} onValueChange={setSeries} disabled={locked}>
              <SelectTrigger id="series-filter" className="w-full sm:w-[13rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ALL">All series</SelectItem>
                {state.series.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {list.length === 0 ? (
          <p className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            No figures match the current filter.{' '}
            <Button
              type="button"
              variant="link"
              className="h-auto p-0"
              onClick={() => {
                setQ('')
                setSeries('ALL')
              }}
            >
              Clear filters
            </Button>
          </p>
        ) : (
          <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
            {list.map((f) => (
              <FigureCard key={f.id} figure={f} selected={f.id === selectedId} disabled={locked} onSelect={() => onSelect(f.id)} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
