import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import {
  Bluetooth,
  BluetoothOff,
  CircleCheck,
  CircleDashed,
  CircleX,
  Info,
  Loader2,
  Megaphone,
  Square,
  Play,
  TriangleAlert,
  Usb,
} from 'lucide-react'
import type { ComponentType, ReactNode } from 'react'
import type { ConsoleLink, ControlLink, KeyState, Mode, StopReason } from '@/lib/model'

export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

const TONE_CLASS: Record<Tone, string> = {
  success: 'border-success/30 bg-success/10 text-success dark:border-success/25 dark:bg-success/15',
  warning: 'border-warning/35 bg-warning/10 text-warning dark:border-warning/25 dark:bg-warning/15',
  danger: 'border-destructive/30 bg-destructive/10 text-destructive dark:border-destructive/30 dark:bg-destructive/20',
  info: 'border-info/30 bg-info/10 text-info dark:border-info/25 dark:bg-info/15',
  neutral: 'border-border bg-muted text-muted-foreground',
}

/** A status badge always pairs an icon with a word — never colour alone (WCAG 1.4.1). */
export function StatusBadge({
  tone,
  icon: Icon,
  children,
  className,
  spin,
}: {
  tone: Tone
  icon?: ComponentType<{ className?: string; 'aria-hidden'?: boolean }>
  children: ReactNode
  className?: string
  spin?: boolean
}) {
  return (
    <Badge variant="outline" className={cn('gap-1', TONE_CLASS[tone], className)}>
      {Icon ? <Icon className={cn('size-3', spin && 'animate-spin')} aria-hidden /> : null}
      {children}
    </Badge>
  )
}

export function ControlLinkBadge({ link, heldBy }: { link: ControlLink; heldBy?: string }) {
  if (link === 'UP') return <StatusBadge tone="success" icon={Usb}>Control link</StatusBadge>
  if (link === 'BUSY')
    return (
      <StatusBadge tone="warning" icon={Usb} className="max-w-[16rem]">
        <span className="truncate">Port busy{heldBy ? ` — ${heldBy}` : ''}</span>
      </StatusBadge>
    )
  return <StatusBadge tone="danger" icon={Usb}>No device</StatusBadge>
}

export function ConsoleLinkBadge({ link, bonded }: { link: ConsoleLink; bonded: boolean }) {
  if (link === 'CONNECTED') return <StatusBadge tone="success" icon={Bluetooth}>Console connected</StatusBadge>
  if (link === 'ADVERTISING')
    return (
      <StatusBadge tone="info" icon={Megaphone}>
        {bonded ? 'Waiting for console' : 'Advertising (unpaired)'}
      </StatusBadge>
    )
  return <StatusBadge tone="neutral" icon={BluetoothOff}>Console disconnected</StatusBadge>
}

export function ModeBadge({ mode }: { mode: Mode }) {
  if (mode === 'MACRO') return <StatusBadge tone="success" icon={Play}>MACRO</StatusBadge>
  if (mode === 'AMIIBO') return <StatusBadge tone="success" icon={CircleDashed}>AMIIBO</StatusBadge>
  return <StatusBadge tone="neutral" icon={Square}>IDLE</StatusBadge>
}

export function StopReasonBadge({ reason, error }: { reason: StopReason; error: boolean }) {
  // BOOT_LOCAL with a pending error reads as both: stopped by hand AND broken.
  if (reason === 'BOOT_LOCAL') {
    return error ? (
      <StatusBadge tone="danger" icon={TriangleAlert}>
        Stopped at board, and something is broken
      </StatusBadge>
    ) : (
      <StatusBadge tone="warning" icon={TriangleAlert}>
        Stopped at board
      </StatusBadge>
    )
  }
  if (reason === 'CONSOLE_LOST')
    return (
      <StatusBadge tone="warning" icon={BluetoothOff}>
        Stopped: console disconnected
      </StatusBadge>
    )
  if (reason === 'ERROR' || error) return <StatusBadge tone="danger" icon={CircleX}>Stopped on error</StatusBadge>
  return <StatusBadge tone="info" icon={CircleCheck}>Stopped from app</StatusBadge>
}

export function KeyStateBadge({ state }: { state: KeyState }) {
  if (state === 'KEY_OK') return <StatusBadge tone="success" icon={CircleCheck}>Key ready</StatusBadge>
  if (state === 'KEY_UNVERIFIED') return <StatusBadge tone="warning" icon={Info}>Key unverified</StatusBadge>
  return <StatusBadge tone="danger" icon={CircleX}>Key {state === 'KEY_ABSENT' ? 'missing' : 'invalid'}</StatusBadge>
}

export function LoadingBadge({ children }: { children: ReactNode }) {
  return <StatusBadge tone="info" icon={Loader2} spin>{children}</StatusBadge>
}
