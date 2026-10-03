/**
 * The UI's domain model — the TypeScript side of `GET /api/state`.
 *
 * The shape mirrors `container/ns2container/state.py:Controller.snapshot()`
 * key for key; that method is the single producer and this file is the single
 * consumer. Types use `CONTEXT.md`'s vocabulary and the enum names the wire
 * uses (`Mode`, `StopReason`, `KeyState`), so a mismatch is a bug in one of
 * the two files and not a translation to maintain.
 *
 * The prototype's `scenario` selector and fixture arrays are gone: macros,
 * figures and series now come from the mounted libraries through the state.
 */

// ── Vocabulary (CONTEXT.md) ────────────────────────────────────────────────

export type Mode = 'IDLE' | 'MACRO' | 'AMIIBO'
// The UI's four stop reasons. §3.2's wire enum is `NONE`/`CONTAINER_STOP`/
// `BOOT_LOCAL`; `ERROR` and `CONSOLE_LOST` are the container-side readings
// §3.4's table and chapter 9's policy add, fixed by `ui-contract.md`.
export type StopReason = 'NONE' | 'CONTAINER_STOP' | 'BOOT_LOCAL' | 'CONSOLE_LOST' | 'ERROR'
export type ControlLink = 'UP' | 'BUSY' | 'DOWN'
export type ConsoleLink = 'CONNECTED' | 'ADVERTISING' | 'DISCONNECTED'
export type RecoveryCase = 'SAME_POWER' | 'NEW_POWER' | 'DIFFERENT_FIRMWARE'
export type KeyState = 'KEY_OK' | 'KEY_ABSENT' | 'KEY_INVALID' | 'KEY_UNVERIFIED'
export type LibraryState = 'READY' | 'EMPTY'

export interface DeviceFeatures {
  macro: boolean
  amiibo: boolean
  config: boolean
}

export interface Firmware {
  protoVer: number
  fwVersion: string
  bootId: string
  maxFrame: number
  chunkSize: number
  planCapacityBytes: number
  features: DeviceFeatures
}

export interface MacroFixture {
  id: string
  name: string
  source: string
  status: 'ready' | 'rejected'
  events: number
  loopMs: number
  buttons: number
  sticks: number
  /** Compiled plan size: 12 B header + 11 B per record (§5.3). */
  bytes: number
  rejection: { code?: string; message: string; fix: string; atMs: number | null } | null
}

export interface Figure {
  id: string
  name: string
  series: string
  source: string
}

export interface PlanInfo {
  macroId: string | null
  hash: string
  frameCount: number
  currentFrame: number
  loopCount: number
  bytes: number
}

export interface PlacementInfo {
  figureId: string | null
  identity: string
  index: number
  scans: number
  sinceSec: number
}

export interface LogEntry {
  id: number
  t: number
  source: 'container' | 'device' | 'frame'
  level: 'debug' | 'info' | 'warn' | 'error'
  message: string
}

// ── State ──────────────────────────────────────────────────────────────────

/** Exactly `Controller.snapshot()` (§8.7). */
export interface DeviceState {
  control: { link: ControlLink; port: string; baud: number; heldBy: string | null }
  console: { link: ConsoleLink; bonded: boolean; lastDropReason: string | null }
  firmware: Firmware | null
  mode: Mode
  plan: PlanInfo | null
  uploading: { macroId: string; offset: number; total: number } | null
  placement: PlacementInfo | null
  key: KeyState
  keySpelling: string | null
  macroLibrary: LibraryState
  amiiboLibrary: LibraryState
  macros: MacroFixture[]
  figures: Figure[]
  series: string[]
  stopReason: StopReason
  lastError: { code: string; message: string } | null
  recovery: RecoveryCase
  config: { reportIntervalMs: number; led: boolean }
}

/** The server state plus the two things the client owns (a picker selection,
 *  which is not device or container truth, and the log ring it accumulates). */
export interface UiState extends DeviceState {
  selectedMacroId: string | null
  selectedFigureId: string | null
  logs: LogEntry[]
}

// ── Formatting helpers ─────────────────────────────────────────────────────

export function formatLoop(ms: number): string {
  if (ms <= 0) return '—'
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)} s`
  const m = Math.floor(ms / 60_000)
  const sec = Math.round((ms % 60_000) / 1000)
  return m > 0 ? `${m} m ${String(sec).padStart(2, '0')} s` : `${(ms / 1000).toFixed(1)} s`
}

export function formatBytes(n: number): string {
  if (n <= 0) return '—'
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

export function formatClock(t: number): string {
  return t.toFixed(3).padStart(8, '0')
}

export function findMacro(state: Pick<DeviceState, 'macros'>, id: string | null): MacroFixture | undefined {
  if (!id) return undefined
  return state.macros.find((m) => m.id === id)
}

export function findFigure(state: Pick<DeviceState, 'figures'>, id: string | null): Figure | undefined {
  if (!id) return undefined
  return state.figures.find((f) => f.id === id)
}
