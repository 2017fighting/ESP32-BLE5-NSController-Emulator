import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Separator } from '@/components/ui/separator'
import { cn } from '@/lib/utils'
import { amiiboBlocked, amiiboLock, type AmiiboLockKind } from '@/components/amiibo-lock'
import type { PendingMode } from '@/components/mode-selector'
import { StopReasonBadge } from '@/components/status'
import { figureById, formatBytes, formatLoop, macroById, type DeviceState, type StopReason } from '@/prototype/model'
import { useStore } from '@/prototype/store'
import { CircleSlash, Play, ScanLine, Square, Upload, X } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useMemo, type ReactNode } from 'react'

/** A custom shape so the same primary action can render in the card and in the mobile bar. */
export interface PrimaryActionShape {
  label: string
  icon: LucideIcon
  onClick: () => void
  disabled: boolean
  variant?: 'default' | 'secondary' | 'destructive' | 'outline'
  hiddenOnMobile?: boolean
}

export function blockedReason(state: DeviceState, pending: PendingMode, lock: AmiiboLockKind): string | null {
  if (state.control.link === 'BUSY') return 'The serial port is held by another process. Stop it, then reconnect.'
  if (state.control.link === 'DOWN') return 'No board is connected. Plug it in, then reconnect.'
  if (state.uploading) return null
  if (pending === 'MACRO') {
    if (state.macroLibrary === 'EMPTY') return 'No macro library is mounted.'
    if (!state.selectedMacroId) return 'Choose a macro first.'
    const m = macroById(state.selectedMacroId)
    if (m?.status === 'rejected') return 'That macro was rejected at ingestion and cannot run.'
    return null
  }
  if (!state.selectedFigureId) return 'Choose a figure first.'
  if (amiiboBlocked(lock)) return 'Amiibo is locked — fix the notice above to unlock it.'
  return null
}

/**
 * The one dominant action for the current view state. Both the desktop card and
 * the mobile bar render the result, so the two can never diverge (Von Restorff
 * + Nielsen H8).
 */
export function usePrimaryAction(pending: PendingMode): { action: PrimaryActionShape; blocked: string | null } {
  const { state, startMacro, placeAmiibo, stop, cancelUpload } = useStore()
  const lock = amiiboLock(state)
  const blocked = blockedReason(state, pending, lock)
  const action = useMemo<PrimaryActionShape>(() => {
    if (state.uploading) return { label: 'Cancel upload', icon: X, onClick: cancelUpload, disabled: false, variant: 'outline' }
    if (state.mode === 'MACRO') return { label: 'Stop macro', icon: Square, onClick: () => stop(), disabled: false, variant: 'secondary' }
    if (state.mode === 'AMIIBO') return { label: 'Unplace amiibo', icon: CircleSlash, onClick: () => stop(), disabled: false, variant: 'secondary' }
    if (pending === 'MACRO') return { label: 'Start macro', icon: Play, onClick: startMacro, disabled: Boolean(blocked) }
    return { label: 'Place amiibo', icon: ScanLine, onClick: placeAmiibo, disabled: Boolean(blocked) }
  }, [state.uploading, state.mode, pending, blocked, startMacro, placeAmiibo, stop, cancelUpload])
  return { action, blocked }
}

/** Why a run is no longer going, in the user's terms (Nielsen H9). */
function stopReasonCopy(reason: StopReason, error: boolean): string | null {
  if (reason === 'BOOT_LOCAL')
    return error
      ? 'Somebody pressed BOOT on the board, and an error is still pending. The plan is retained — fix the error, then start again; the app will not restart it for you.'
      : 'The plan is still loaded. Start again when you are ready; the app will not restart it for you.'
  if (reason === 'CONSOLE_LOST')
    return 'The console disconnected, so the app stopped the run. The firmware watches neither link; the console re-initialises itself on reconnect, so restarting is yours to do.'
  return null
}

/** The single dominant action for the Control view (Von Restorff + Nielsen H8). */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-right text-sm">{children}</dd>
    </div>
  )
}

function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('font-mono text-xs break-all', className)}>{children}</span>
}

export function RunCard({ pending }: { pending: PendingMode }) {
  const { state } = useStore()
  const { action, blocked } = usePrimaryAction(pending)

  const macro = macroById(state.selectedMacroId)
  const figure = figureById(state.placement?.figureId ?? state.selectedFigureId)
  const reasonCopy = stopReasonCopy(state.stopReason, Boolean(state.lastError))

  return (
    <Card className="lg:sticky lg:top-28" id="run-card">
      <CardHeader className="gap-1">
        <CardTitle className="text-sm">Run</CardTitle>
        <CardDescription>
          {state.mode === 'MACRO'
            ? 'The board loops the plan from RAM — this page can be closed.'
            : state.mode === 'AMIIBO'
              ? 'The console scans the placed tag; place again for a fresh identity.'
              : 'Nothing is running.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {state.uploading ? (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Upload className="size-4" aria-hidden />
              Uploading plan
            </div>
            <Progress
              value={Math.round((state.uploading.offset / Math.max(1, state.uploading.total)) * 100)}
              aria-label={`Uploading ${state.uploading.offset} of ${state.uploading.total} bytes`}
            />
            <p className="font-mono text-xs text-muted-foreground">
              {state.uploading.offset} / {state.uploading.total} B — commits atomically on the final CRC
            </p>
          </div>
        ) : null}

        {state.mode === 'MACRO' && state.plan ? (
          <div className="space-y-3">
            <div>
              <p className="text-sm font-medium">{macro?.name ?? 'Unknown macro'}</p>
              <p className="font-mono text-xs text-muted-foreground">plan {state.plan.hash}</p>
            </div>
            <div className="flex items-end gap-2">
              <span className="font-mono text-3xl font-semibold tabular-nums">{state.plan.loopCount}</span>
              <span className="pb-1 text-xs text-muted-foreground">loops completed</span>
            </div>
            <div className="space-y-1.5">
              <Progress
                value={Math.round((state.plan.currentFrame / Math.max(1, state.plan.frameCount)) * 100)}
                aria-label={`Frame ${state.plan.currentFrame} of ${state.plan.frameCount}`}
              />
              <p className="font-mono text-xs text-muted-foreground">
                frame {state.plan.currentFrame} / {state.plan.frameCount} · {formatLoop(macro?.loopMs ?? 0)} per loop · {formatBytes(state.plan.bytes)}
              </p>
            </div>
          </div>
        ) : null}

        {state.mode === 'AMIIBO' && state.placement ? (
          <dl className="divide-y divide-border">
            <Row label="Figure">{figure?.name ?? 'Unknown figure'}</Row>
            <Row label="Series">{figure?.series ?? '—'}</Row>
            <Row label="Identity">
              <Mono>{state.placement.identity}</Mono>
            </Row>
            <Row label="Placement">
              <span className="font-mono tabular-nums">#{state.placement.index}</span>
            </Row>
            <Row label="Console scans">
              <span className="font-mono tabular-nums">{state.placement.scans}</span>
            </Row>
            <Row label="Placed">
              <span className="font-mono tabular-nums">{Math.round(state.placement.sinceSec)} s ago</span>
            </Row>
          </dl>
        ) : null}

        {state.mode === 'IDLE' && !state.uploading ? (
          <div className="space-y-3">
            {blocked ? (
              <p className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground">{blocked}</p>
            ) : null}
            {pending === 'MACRO' && macro ? (
              <dl className="divide-y divide-border">
                <Row label="Macro">{macro.name}</Row>
                <Row label="Loop">{formatLoop(macro.loopMs)}</Row>
                <Row label="Compiled">{formatBytes(macro.bytes)}</Row>
                {state.plan ? <Row label="Plan loaded"><Mono>{state.plan.hash}</Mono></Row> : null}
              </dl>
            ) : null}
            {pending === 'AMIIBO' && figure ? (
              <dl className="divide-y divide-border">
                <Row label="Figure">{figure.name}</Row>
                <Row label="Series">{figure.series}</Row>
                <Row label="Source">
                  <Mono className="text-muted-foreground">{figure.source.split('/').pop()}</Mono>
                </Row>
              </dl>
            ) : null}
            {pending === 'MACRO' && !macro ? (
              <p className="text-sm text-muted-foreground">Pick a macro from the library to continue.</p>
            ) : null}
            {pending === 'AMIIBO' && !figure ? (
              <p className="text-sm text-muted-foreground">Pick a figure from the library to continue.</p>
            ) : null}
            {reasonCopy ? (
              <div className="flex flex-wrap items-start gap-2 rounded-md border border-border bg-muted/50 p-3">
                <StopReasonBadge reason={state.stopReason} error={Boolean(state.lastError)} />
                <p className="w-full text-sm text-muted-foreground">{reasonCopy}</p>
              </div>
            ) : null}
          </div>
        ) : null}

        <Separator />

        <div className="space-y-2">
          <Button
            type="button"
            size="lg"
            variant={action.variant ?? 'default'}
            onClick={action.onClick}
            disabled={action.disabled}
            className="hidden h-11 w-full md:inline-flex"
          >
            <action.icon aria-hidden />
            {action.label}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

export function MobileActionBar({ pending }: { pending: PendingMode }) {
  const { action } = usePrimaryAction(pending)
  return (
    <div className="fixed inset-x-0 bottom-[calc(3.5rem+env(safe-area-inset-bottom))] z-30 border-t border-border bg-background/95 p-3 backdrop-blur md:hidden">
      <Button type="button" size="lg" variant={action.variant ?? 'default'} onClick={action.onClick} disabled={action.disabled} className="h-11 w-full">
        <action.icon aria-hidden />
        {action.label}
      </Button>
    </div>
  )
}
