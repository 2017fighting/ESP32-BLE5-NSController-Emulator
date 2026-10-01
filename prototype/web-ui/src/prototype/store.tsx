/**
 * Simulated device store for the prototype.
 *
 * This is NOT product code: it fakes the control plane so the screens can be
 * felt. Actions mirror real verbs (`LOAD_PLAN` → `START`, `PLACE_AMIIBO`,
 * `STOP`, `PAIR_UNPAIR`) and the legal sequencing #6 fixed: a mode change is
 * always STOP then START/PLACE, and no verb implicitly changes mode.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import {
  SCENARIOS,
  buildState,
  figureById,
  macroById,
  type DeviceState,
  type LogEntry,
  type ScenarioId,
} from './model'

export type ViewId = 'control' | 'logs' | 'connection' | 'settings'

interface StoreValue {
  state: DeviceState
  view: ViewId
  setView: (v: ViewId) => void
  setScenario: (id: ScenarioId) => void
  selectMacro: (id: string) => void
  selectFigure: (id: string) => void
  startMacro: () => void
  cancelUpload: () => void
  placeAmiibo: () => void
  stop: (reason?: 'CONTAINER_STOP' | 'BOOT_LOCAL') => void
  requestUnpair: () => void
  reconnect: () => void
  saveConfig: (cfg: { reportIntervalMs: number; led: boolean }) => void
  clearLogs: () => void
}

const StoreContext = createContext<StoreValue | null>(null)

let uid = 1000
const nextId = () => ++uid

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DeviceState>(() => buildState(initialScenario()))
  const [view, setView] = useState<ViewId>(() => initialView())
  const timers = useRef<number[]>([])
  // Placement counter survives an unplace within a scenario (rotation mints a
  // fresh identity each time; the ordinal keeps counting).
  const placements = useRef(3)

  const clearTimers = useCallback(() => {
    for (const t of timers.current) window.clearTimeout(t)
    timers.current = []
  }, [])

  useEffect(() => clearTimers, [clearTimers])

  const appendLog = useCallback((entries: Omit<LogEntry, 'id'>[]) => {
    setState((s) => {
      const added = entries.map((e) => ({ ...e, id: nextId() }))
      const logs = [...s.logs, ...added].slice(-400)
      return { ...s, logs }
    })
  }, [])

  const setScenario = useCallback(
    (id: ScenarioId) => {
      clearTimers()
      resetIds()
      placements.current = 3
      setState(buildState(id))
      const meta = SCENARIOS.find((s) => s.id === id)
      if (meta) toast.info(`Prototype scenario: ${meta.label}`)
    },
    [clearTimers],
  )

  const selectMacro = useCallback((id: string) => {
    setState((s) => (s.mode === 'IDLE' ? { ...s, selectedMacroId: id } : s))
  }, [])

  const selectFigure = useCallback((id: string) => {
    setState((s) => (s.mode === 'IDLE' ? { ...s, selectedFigureId: id } : s))
  }, [])

  const startMacro = useCallback(() => {
    setState((s) => {
      if (!s.selectedMacroId || s.mode !== 'IDLE' || s.control.link !== 'UP') return s
      const macro = macroById(s.selectedMacroId)
      if (!macro || macro.status !== 'ready') return s
      // Axiom (#6): STOP then START; nothing is queued, nothing implicit.
      return { ...s, uploading: { macroId: macro.id, offset: 0, total: macro.bytes }, lastError: null }
    })
  }, [])

  // Abandoning a staged transfer is NOT a STOP: the device discards the staging
  // buffer on its own, and no mode verb has been issued yet (#5's atomic commit).
  const cancelUpload = useCallback(() => {
    setState((s) =>
      s.uploading
        ? {
            ...s,
            uploading: null,
            logs: [
              ...s.logs,
              { id: nextId(), t: 1.5, source: 'container', level: 'info', message: 'plan: upload abandoned at the ACK\u2019d offset — staging buffer discarded, no plan committed' },
            ],
          }
        : s,
    )
  }, [])

  // Drive the fake windowed upload, then commit the plan and START.
  useEffect(() => {
    const up = state.uploading
    if (!up) return
    const step = Math.max(256, Math.round(up.total / 12))
    const t = window.setTimeout(() => {
      const next = up.offset + step
      if (next >= up.total) {
        const macro = macroById(up.macroId)
        appendLog([
          { t: performance.now() / 1000, source: 'frame', level: 'debug', message: 'REP  ACK            next=commit' },
          { t: performance.now() / 1000, source: 'container', level: 'info', message: `plan: ${macro?.name ?? up.macroId} → ${up.total} B committed atomically, sha256 3f9c1e7a44b0d2c6` },
          { t: performance.now() / 1000, source: 'frame', level: 'debug', message: 'REQ  START' },
          { t: performance.now() / 1000, source: 'container', level: 'info', message: 'mode: MACRO started — the device runs the plan and loops it from RAM' },
        ])
        setState((s) => ({
          ...s,
          uploading: null,
          mode: 'MACRO',
          stopReason: 'CONTAINER_STOP',
          plan: {
            macroId: up.macroId,
            hash: '3f9c1e7a44b0d2c6',
            frameCount: macro?.events ?? 0,
            currentFrame: 0,
            loopCount: 0,
            bytes: up.total,
          },
        }))
        toast.success('Macro started', { description: 'The device owns loop timing; this page can be closed.' })
        return
      }
      setState((s) => (s.uploading ? { ...s, uploading: { ...s.uploading, offset: next } } : s))
    }, 120)
    timers.current.push(t)
    return () => window.clearTimeout(t)
  }, [state.uploading, appendLog])

  const placeAmiibo = useCallback(() => {
    setState((s) => {
      if (!s.selectedFigureId || s.mode !== 'IDLE' || s.control.link !== 'UP') return s
      // #13: KEY_UNVERIFIED is not a lock — verification completes at the first
      // placement. Only an absent or invalid key blocks.
      const keyUsable = s.key === 'KEY_READY' || s.key === 'KEY_UNVERIFIED'
      if (!keyUsable || s.amiiboLibrary !== 'READY' || !s.firmware?.features.amiibo) return s
      const figure = figureById(s.selectedFigureId)
      const identity = mintIdentity()
      const index = ++placements.current
      return {
        ...s,
        lastError: null,
        mode: 'AMIIBO',
        stopReason: 'CONTAINER_STOP',
        placement: { figureId: s.selectedFigureId, identity, index, scans: 0, sinceSec: 0 },
        logs: [
          ...s.logs,
          { id: nextId(), t: 1.02, source: 'container', level: 'info', message: `seal: ${figure?.name ?? s.selectedFigureId} + key → 540 B tag, identity ${identity} (placement #${index})` },
          { id: nextId(), t: 1.05, source: 'frame', level: 'debug', message: 'REQ  PLACE_AMIIBO   len=540' },
          { id: nextId(), t: 1.09, source: 'frame', level: 'debug', message: 'REP  ACK' },
          { id: nextId(), t: 1.10, source: 'frame', level: 'debug', message: `EVT  TAG_PLACED     identity=${identity.replaceAll(' ', '').toLowerCase()}` },
        ],
      }
    })
  }, [])

  const stop = useCallback(
    (reason: 'CONTAINER_STOP' | 'BOOT_LOCAL' = 'CONTAINER_STOP') => {
      setState((s) => {
        if (s.mode === 'IDLE' && !s.uploading) {
          // STOP is an idempotent success when nothing is active (#6).
          return { ...s, stopReason: reason }
        }
        const verb = s.mode === 'AMIIBO' ? 'UNPLACE_AMIIBO' : 'STOP'
        return {
          ...s,
          mode: 'IDLE',
          stopReason: reason,
          uploading: null,
          placement: null,
          logs: [
            ...s.logs,
            { id: nextId(), t: 2.0, source: 'frame', level: 'debug', message: `REQ  ${verb}` },
            { id: nextId(), t: 2.05, source: 'container', level: 'info', message: `mode: ${s.mode} → IDLE (${reason}) — neutral is the last write` },
          ],
        }
      })
      if (reason === 'CONTAINER_STOP') toast.success('Stopped')
    },
    [],
  )

  const requestUnpair = useCallback(() => {
    setState((s) => ({
      ...s,
      console: { ...s.console, bonded: false },
      logs: [
        ...s.logs,
        { id: nextId(), t: 3.0, source: 'frame', level: 'debug', message: 'REQ  PAIR_UNPAIR' },
        { id: nextId(), t: 3.05, source: 'container', level: 'info', message: 'pairing: device asked to forget its bond; re-pair from the console controller menu' },
      ],
    }))
    toast.success('Unpair requested', { description: 'The console still owns its side — remove the controller there too.' })
  }, [])

  const reconnect = useCallback(() => {
    clearTimers()
    setState((s) => {
      const restored = { ...buildState('idle'), config: s.config }
      return restored
    })
    toast.info('Reconnecting: HELLO sent, capabilities read')
  }, [clearTimers])

  const saveConfig = useCallback(
    (cfg: { reportIntervalMs: number; led: boolean }) => {
      setState((s) => ({
        ...s,
        config: cfg,
        logs: [
          ...s.logs,
          { id: nextId(), t: 4.0, source: 'frame', level: 'debug', message: `REQ  CONFIG         report_interval_ms=${cfg.reportIntervalMs} led=${cfg.led ? 'on' : 'off'}` },
          { id: nextId(), t: 4.05, source: 'container', level: 'info', message: 'config: applied at the next loop boundary (volatile — re-sent on reconnect)' },
        ],
      }))
      toast.success('Config saved', { description: 'Applies at the next loop boundary.' })
    },
    [],
  )

  const clearLogs = useCallback(() => setState((s) => ({ ...s, logs: [] })), [])

  const value = useMemo<StoreValue>(
    () => ({ state, view, setView, setScenario, selectMacro, selectFigure, startMacro, cancelUpload, placeAmiibo, stop, requestUnpair, reconnect, saveConfig, clearLogs }),
    [state, view, setScenario, selectMacro, selectFigure, startMacro, cancelUpload, placeAmiibo, stop, requestUnpair, reconnect, saveConfig, clearLogs],
  )

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>
}

export function useStore(): StoreValue {
  const v = useContext(StoreContext)
  if (!v) throw new Error('useStore must be used inside <StoreProvider>')
  return v
}

// Deep links so a reviewer (or a capture script) can open one exact state:
//   ?scenario=key-absent&view=control&theme=dark
// Prototype-only affordance; the real app has no such params.
const VIEWS: ViewId[] = ['control', 'logs', 'connection', 'settings']

function initialScenario(): ScenarioId {
  const p = new URLSearchParams(window.location.search).get('scenario')
  return (SCENARIOS.find((s) => s.id === p)?.id ?? 'idle') as ScenarioId
}

function initialView(): ViewId {
  const p = new URLSearchParams(window.location.search).get('view')
  return VIEWS.find((v) => v === p) ?? 'control'
}

// `buildState` assigns log ids from the model's own sequence; the store keeps
// its runtime ids above that band so the two never collide on a scenario swap.
function resetIds() {
  uid = 1000
}

/** #9: identity is 7 bytes, `0x04` + 6 CSPRNG bytes, BCC0/BCC1 recomputed. */
function mintIdentity(): string {
  const cryptoObj = globalThis.crypto
  const bytes = new Uint8Array(6)
  cryptoObj.getRandomValues(bytes)
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0').toUpperCase())
  return ['04', ...hex].join(' ')
}
