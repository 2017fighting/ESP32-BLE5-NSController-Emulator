/**
 * The browser's only door to the container (§8.7): one state read, one SSE
 * stream, one `POST` per verb. The browser never talks to the device.
 *
 * A rejected verb carries a typed error — a wire `ERROR` code (`BAD_STATE`,
 * `PLAN_TOO_LARGE`, …) or a container-local one (`KEY_ABSENT`,
 * `SEALING_UNAVAILABLE`, …) — and this module keeps the code, so the store can
 * say the right thing instead of "request failed".
 */

import type { DeviceState, LogEntry } from './model'

export interface ApiErrorBody {
  code: string
  message: string
  detail?: number
}

export class ApiFailure extends Error {
  readonly code: string
  readonly detail: number

  constructor(body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiFailure'
    this.code = body.code
    this.detail = body.detail ?? 0
  }
}

async function post(path: string, body?: unknown): Promise<DeviceState> {
  const response = await fetch(path, {
    method: 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const error = await readError(response)
    throw new ApiFailure(error)
  }
  const payload = (await response.json()) as { state: DeviceState }
  return payload.state
}

async function readError(response: Response): Promise<ApiErrorBody> {
  try {
    const payload = (await response.json()) as { error?: ApiErrorBody }
    if (payload.error?.code) return payload.error
  } catch {
    // fall through to the status-derived error
  }
  return { code: `HTTP_${response.status}`, message: response.statusText || 'the request was refused' }
}

export interface InitialState {
  state: DeviceState
  logs: LogEntry[]
}

export const api = {
  async getState(): Promise<InitialState> {
    const response = await fetch('/api/state')
    if (!response.ok) throw new ApiFailure(await readError(response))
    return (await response.json()) as InitialState
  },
  start: (macroId: string) => post('/api/start', { macroId }),
  stop: () => post('/api/stop'),
  place: (figureId: string) => post('/api/place', { figureId }),
  unplace: () => post('/api/unplace'),
  pairUnpair: () => post('/api/pair_unpair'),
  saveConfig: (reportIntervalMs: number, led: boolean) => post('/api/config', { reportIntervalMs, led }),
  rescan: () => post('/api/rescan'),
  reconnect: () => post('/api/reconnect'),
  cancel: () => post('/api/cancel'),
  clearLogs: () => post('/api/logs/clear'),
}

/** One SSE stream: `state` snapshots and `log` lines (§8.7). */
export function subscribe(
  onState: (state: DeviceState) => void,
  onLog: (line: LogEntry) => void,
  onStatus: (status: 'live' | 'lost') => void,
): () => void {
  const source = new EventSource('/api/events')
  source.addEventListener('open', () => onStatus('live'))
  source.addEventListener('error', () => onStatus('lost'))
  source.addEventListener('state', (event) => {
    onState(JSON.parse((event as MessageEvent<string>).data) as DeviceState)
  })
  source.addEventListener('log', (event) => {
    onLog(JSON.parse((event as MessageEvent<string>).data) as LogEntry)
  })
  return () => source.close()
}
