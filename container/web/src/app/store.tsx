/**
 * The real store: `GET /api/state`, one SSE stream, and one call per verb.
 *
 * This replaces the prototype's simulated device (`prototype/web-ui/src/
 * prototype/store.tsx`). The shape it exposes is unchanged, so the views did
 * not move — but the source of truth did: `mode`, `plan` and `placement` come
 * from the container's `STATUS`, never from what a button was asked to do
 * (§8.10: no optimistic mode change).
 *
 * Two facts stay client-side because they are not device or container truth:
 * the picker selection (nothing on the wire carries it) and the log ring.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { ApiFailure, api, subscribe } from '@/lib/api'
import type { DeviceState, LogEntry, UiState } from '@/lib/model'

export type ViewId = 'control' | 'logs' | 'connection' | 'settings'

interface StoreValue {
  state: UiState
  connection: 'connecting' | 'live' | 'lost'
  view: ViewId
  setView: (v: ViewId) => void
  selectMacro: (id: string) => void
  selectFigure: (id: string) => void
  startMacro: () => void
  placeAmiibo: () => void
  stop: () => void
  cancelUpload: () => void
  requestUnpair: () => void
  reconnect: () => void
  rescan: () => Promise<void>
  saveConfig: (cfg: { reportIntervalMs: number; led: boolean }) => void
  clearLogs: () => void
}

const StoreContext = createContext<StoreValue | null>(null)

/** The state before the first `GET /api/state` answers. `control.link` is
 *  `DOWN` so the header says "No device" rather than guessing. */
const EMPTY_STATE: DeviceState = {
  control: { link: 'DOWN', port: '—', baud: 0, heldBy: null },
  console: { link: 'DISCONNECTED', bonded: false, lastDropReason: null },
  firmware: null,
  mode: 'IDLE',
  plan: null,
  uploading: null,
  placement: null,
  key: 'KEY_ABSENT',
  keySpelling: null,
  macroLibrary: 'EMPTY',
  amiiboLibrary: 'EMPTY',
  macros: [],
  figures: [],
  series: [],
  stopReason: 'NONE',
  lastError: null,
  recovery: 'SAME_POWER',
  config: { reportIntervalMs: 15, led: true },
}

const LOG_CAPACITY = 400

function report(error: unknown): void {
  if (error instanceof ApiFailure) {
    // Abandoning an upload is a normal act, not a failure (§8.8).
    if (error.code === 'UPLOAD_CANCELLED') {
      toast.info(error.message)
      return
    }
    toast.error(`${error.code}: ${error.message}`)
    return
  }
  toast.error(error instanceof Error ? error.message : String(error))
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [server, setServer] = useState<DeviceState>(EMPTY_STATE)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [connection, setConnection] = useState<StoreValue['connection']>('connecting')
  const [selectedMacroId, setSelectedMacroId] = useState<string | null>(null)
  const [selectedFigureId, setSelectedFigureId] = useState<string | null>(null)
  const [view, setView] = useState<ViewId>('control')

  useEffect(() => {
    let cancelled = false
    api
      .getState()
      .then((initial) => {
        if (cancelled) return
        setServer(initial.state)
        setLogs(initial.logs.slice(-LOG_CAPACITY))
      })
      .catch(report)
    const close = subscribe(
      (next) => setServer(next),
      (line) => setLogs((current) => [...current, line].slice(-LOG_CAPACITY)),
      (status) => setConnection(status),
    )
    return () => {
      cancelled = true
      close()
    }
  }, [])

  const run = useCallback(async (action: () => Promise<DeviceState>) => {
    try {
      setServer(await action())
    } catch (error) {
      report(error)
    }
  }, [])

  // A rescan can remove the selected file; never keep a dangling selection.
  useEffect(() => {
    if (selectedMacroId && !server.macros.some((m) => m.id === selectedMacroId)) setSelectedMacroId(null)
  }, [server.macros, selectedMacroId])
  useEffect(() => {
    if (selectedFigureId && !server.figures.some((f) => f.id === selectedFigureId)) setSelectedFigureId(null)
  }, [server.figures, selectedFigureId])

  const selectMacro = useCallback(
    (id: string) => {
      setSelectedMacroId((current) => (server.mode === 'IDLE' ? id : current))
    },
    [server.mode],
  )
  const selectFigure = useCallback(
    (id: string) => {
      setSelectedFigureId((current) => (server.mode === 'IDLE' ? id : current))
    },
    [server.mode],
  )

  const value = useMemo<StoreValue>(
    () => ({
      state: { ...server, selectedMacroId, selectedFigureId, logs },
      connection,
      view,
      setView,
      selectMacro,
      selectFigure,
      startMacro: () => {
        if (selectedMacroId) void run(() => api.start(selectedMacroId))
      },
      placeAmiibo: () => {
        if (selectedFigureId) void run(() => api.place(selectedFigureId))
      },
      stop: () => void run(api.stop),
      cancelUpload: () => void run(api.cancel),
      requestUnpair: () => void run(api.pairUnpair),
      reconnect: () => void run(api.reconnect),
      rescan: () => run(api.rescan).then(() => undefined),
      saveConfig: (cfg) => void run(() => api.saveConfig(cfg.reportIntervalMs, cfg.led)),
      clearLogs: () => void run(api.clearLogs),
    }),
    [server, logs, connection, view, selectedMacroId, selectedFigureId, selectMacro, selectFigure, run],
  )

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>
}

export function useStore(): StoreValue {
  const value = useContext(StoreContext)
  if (value === null) throw new Error('useStore must be used inside <StoreProvider>')
  return value
}
