import { cn } from '@/lib/utils'
import { CircleDashed, Play, type LucideIcon } from 'lucide-react'
import type { Mode } from '@/prototype/model'

export type PendingMode = 'MACRO' | 'AMIIBO'

const OPTIONS: { id: PendingMode; title: string; blurb: string; icon: LucideIcon }[] = [
  { id: 'MACRO', title: 'Run a macro', blurb: 'Loop one compiled plan on the board', icon: Play },
  { id: 'AMIIBO', title: 'Present an amiibo', blurb: 'Place one tag the console can scan', icon: CircleDashed },
]

/**
 * Mode choice. Hick's Law: exactly two options. While a mode is active the
 * selector is locked — a mode change is STOP then START, never one step (#6).
 */
export function ModeSelector({
  mode,
  pending,
  onSelect,
}: {
  mode: Mode
  pending: PendingMode
  onSelect: (m: PendingMode) => void
}) {
  const locked = mode !== 'IDLE'
  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold" id="mode-heading">
          What do you want to run?
        </h2>
        <p className="text-xs text-muted-foreground">
          {locked ? 'Stop the current run to switch.' : 'One mode at a time. Idle is neither.'}
        </p>
      </div>
      <div role="group" aria-labelledby="mode-heading" className="grid gap-3 sm:grid-cols-2">
        {OPTIONS.map((opt) => {
          const selected = pending === opt.id
          const running = mode === opt.id
          return (
            <button
              key={opt.id}
              type="button"
              onClick={() => onSelect(opt.id)}
              disabled={locked}
              aria-pressed={selected}
              className={cn(
                'flex items-start gap-3 rounded-lg border bg-card p-4 text-left transition-colors',
                'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none',
                selected ? 'border-ring ring-1 ring-ring' : 'border-border hover:bg-accent/50',
                locked && !running && 'cursor-not-allowed opacity-50',
              )}
            >
              <opt.icon className={cn('mt-0.5 size-5 shrink-0', selected ? 'text-foreground' : 'text-muted-foreground')} aria-hidden />
              <span className="flex flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                  {opt.title}
                  {running ? (
                    <span className="rounded-4xl bg-success/10 px-2 py-0.5 text-xs font-medium text-success dark:bg-success/15">
                      Running
                    </span>
                  ) : null}
                </span>
                <span className="text-xs text-muted-foreground">{opt.blurb}</span>
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
