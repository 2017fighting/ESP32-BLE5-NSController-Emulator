/**
 * Prototype domain model.
 *
 * Types mirror the settled vocabulary in `CONTEXT.md` and the interfaces fixed
 * by issues #5 (control plane), #6 (mode machine), #8 (plan format), #9
 * (amiibo identity) and #13 (key policy). Fixture data is real: the macro
 * names, event counts and loop durations are the actual `宏/*.json` files from
 * `switch-controller-macro`, and the figures are real `Amiibo` library files.
 * Nothing here is product code — it is the throwaway prototype for issue #7.
 */

// ── Vocabulary (CONTEXT.md) ────────────────────────────────────────────────

export type Mode = 'IDLE' | 'MACRO' | 'AMIIBO'
export type StopReason = 'CONTAINER_STOP' | 'BOOT_LOCAL' | 'CONSOLE_LOST' | 'ERROR'
export type ControlLink = 'UP' | 'BUSY' | 'DOWN'
export type ConsoleLink = 'CONNECTED' | 'ADVERTISING' | 'DISCONNECTED'
export type RecoveryCase = 'SAME_POWER' | 'NEW_POWER' | 'DIFFERENT_FIRMWARE'
export type KeyState = 'KEY_READY' | 'KEY_ABSENT' | 'KEY_INVALID' | 'KEY_UNVERIFIED'
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
  vidPid: string
  maxFrame: number
  chunkSize: number
  planCapacityBytes: number
  features: DeviceFeatures
}

export interface MacroFixture {
  id: string
  name: string
  source: string
  events: number
  loopMs: number
  buttons: number
  sticks: number
  /** Compiled plan size under #8's rule (11 B/record + 12 B header). */
  bytes: number
  status: 'ready' | 'rejected'
  synthetic?: boolean
  rejection?: { atMs: number; message: string; fix: string }
}

export interface Figure {
  id: string
  name: string
  series: string
  source: string
}

// ── Real fixture data ──────────────────────────────────────────────────────

/** `switch-controller-macro@202e512` — `宏/*.json`, measured, not invented. */
export const MACROS: MacroFixture[] = [
  {
    id: 'tempura-1',
    name: '天妇罗巢穴宏1',
    source: '宏/天妇罗巢穴宏1.json',
    events: 243,
    loopMs: 115_321,
    buttons: 116,
    sticks: 127,
    bytes: 2_685,
    status: 'ready',
  },
  {
    id: 'tempura-fan',
    name: '天妇罗巢穴风扇-感谢群友分享',
    source: '宏/天妇罗巢穴风扇-感谢群友分享.json',
    events: 243,
    loopMs: 115_321,
    buttons: 116,
    sticks: 127,
    bytes: 2_685,
    status: 'ready',
  },
  {
    id: 'almond',
    name: '杏仁巢穴宏',
    source: '宏/杏仁巢穴宏.json',
    events: 411,
    loopMs: 62_148,
    buttons: 30,
    sticks: 381,
    bytes: 3_356,
    status: 'ready',
  },
  {
    id: 'correction',
    name: '纠错宏',
    source: '宏/纠错宏.json',
    events: 88,
    loopMs: 26_205,
    buttons: 22,
    sticks: 66,
    bytes: 793,
    status: 'ready',
  },
  {
    id: 'tempura-2',
    name: '天妇罗巢穴宏2',
    source: '宏/天妇罗巢穴宏2.json',
    events: 0,
    loopMs: 0,
    buttons: 0,
    sticks: 0,
    bytes: 0,
    status: 'rejected',
    synthetic: true,
    rejection: {
      atMs: 4_210,
      message: 'unknown event type "motion"',
      fix: 'Only "button" and "stick" events are accepted. Fix the event at t=4210 ms and re-scan the library.',
    },
  },
]

/** `AmiiboDB/Amiibo@58cf455` — real `.bin` paths; UIDs are minted per placement. */
export const FIGURES: Figure[] = [
  { id: 'link-totk', name: 'Link (TotK)', series: 'The Legend of Zelda', source: 'Amiibo Bin/The Legend of Zelda Amiibo/Tears of the Kingdom/Link (TotK).bin' },
  { id: 'zelda-totk', name: 'Princess Zelda', series: 'The Legend of Zelda', source: 'Amiibo Bin/The Legend of Zelda Amiibo/Tears of the Kingdom/Princess_Zelda.bin' },
  { id: 'ganondorf-totk', name: 'Ganondorf (TotK)', series: 'The Legend of Zelda', source: 'Amiibo Bin/The Legend of Zelda Amiibo/Tears of the Kingdom/Ganondorf (TotK).bin' },
  { id: 'gerudo-king', name: 'Gerudo King', series: 'The Legend of Zelda', source: 'Amiibo Bin/The Legend of Zelda Amiibo/Tears of the Kingdom/Gerudo_King.bin' },
  { id: 'samus-dread', name: '[DREAD] Samus', series: 'Metroid', source: 'Amiibo Bin/Metroid Amiibo/Metroid Dread/[DREAD] Samus.bin' },
  { id: 'emmi-dread', name: '[DREAD] E.M.M.I.', series: 'Metroid', source: 'Amiibo Bin/Metroid Amiibo/Metroid Dread/[DREAD]E.M.M.I .bin' },
  { id: 'samus-returns', name: 'Samus Aran', series: 'Metroid', source: 'Amiibo Bin/Metroid Amiibo/Metroid_ Samus Returns/Samus Aran.bin' },
  { id: 'noah', name: 'Noah', series: 'Xenoblade Chronicles', source: 'Amiibo Bin/Xenoblade Chronicles/Noah.bin' },
  { id: 'mio', name: 'Mio', series: 'Xenoblade Chronicles', source: 'Amiibo Bin/Xenoblade Chronicles/Mio.bin' },
  { id: 'kirby-smash', name: 'Kirby', series: 'Super Smash Bros', source: 'Amiibo Bin/Super Smash Bros Amiibo/Kirby.bin' },
  { id: 'isabelle', name: 'Isabelle', series: 'Animal Crossing', source: 'Amiibo Bin/Animal Crossing Amiibo/Amiibo Figures/Isabelle.bin' },
  { id: 'mario-classic', name: 'Mario Classic Colors (Anniversary)', series: 'Super Mario', source: 'Amiibo Bin/Super Mario Amiibo/Mario Classic Colors (Anniversary).bin' },
  { id: 'boo', name: 'Boo', series: 'Super Mario', source: 'Amiibo Bin/Super Mario Amiibo/Boo.bin' },
  { id: 'cat-mario', name: 'Cat Mario', series: 'Super Mario', source: 'Amiibo Bin/Super Mario Amiibo/Cat Mario.bin' },
]

export const SERIES = [...new Set(FIGURES.map((f) => f.series))]

// ── State ──────────────────────────────────────────────────────────────────

export interface LogEntry {
  id: number
  t: number
  source: 'container' | 'device' | 'frame'
  level: 'debug' | 'info' | 'warn' | 'error'
  message: string
}

export interface DeviceState {
  scenario: ScenarioId
  control: { link: ControlLink; port: string; baud: number; heldBy?: string }
  console: { link: ConsoleLink; bonded: boolean; lastDropReason?: string }
  firmware: Firmware | null
  mode: Mode
  selectedMacroId: string | null
  plan: { macroId: string; hash: string; frameCount: number; currentFrame: number; loopCount: number; bytes: number } | null
  uploading: { macroId: string; offset: number; total: number } | null
  selectedFigureId: string | null
  placement: { figureId: string; identity: string; index: number; scans: number; sinceSec: number } | null
  key: KeyState
  keySpelling: string | null
  macroLibrary: LibraryState
  amiiboLibrary: LibraryState
  stopReason: StopReason
  lastError: { code: string; message: string } | null
  recovery: RecoveryCase
  config: { reportIntervalMs: number; led: boolean }
  logs: LogEntry[]
}

// ── Scenarios (prototype-only review aid) ──────────────────────────────────

export type ScenarioId =
  | 'idle'
  | 'macro-ready'
  | 'macro-running'
  | 'amiibo-ready'
  | 'amiibo-placed'
  | 'uploading'
  | 'upload-failed'
  | 'macro-rejected'
  | 'boot-local'
  | 'macro-library-empty'
  | 'key-absent'
  | 'key-invalid'
  | 'key-unverified'
  | 'amiibo-library-empty'
  | 'no-amiibo-feature'
  | 'no-device'
  | 'device-busy'
  | 'new-power'
  | 'firmware-mismatch'
  | 'console-lost'

export interface ScenarioMeta {
  id: ScenarioId
  label: string
  group: 'Happy path' | 'In progress' | 'Failure & recovery' | 'Amiibo lock'
}

export const SCENARIOS: ScenarioMeta[] = [
  { id: 'idle', label: 'Idle — nothing loaded', group: 'Happy path' },
  { id: 'macro-ready', label: 'Macro selected, ready to start', group: 'Happy path' },
  { id: 'macro-running', label: 'Macro running', group: 'Happy path' },
  { id: 'amiibo-ready', label: 'Amiibo selected, ready to place', group: 'Happy path' },
  { id: 'amiibo-placed', label: 'Amiibo placed', group: 'Happy path' },
  { id: 'uploading', label: 'Uploading a plan (windowed ACK)', group: 'In progress' },
  { id: 'upload-failed', label: 'Upload rejected (PLAN_TOO_LARGE)', group: 'Failure & recovery' },
  { id: 'macro-rejected', label: 'Macro rejected at ingestion', group: 'Failure & recovery' },
  { id: 'boot-local', label: 'Panic-stopped at the board', group: 'Failure & recovery' },
  { id: 'macro-library-empty', label: 'Macro library not mounted', group: 'Failure & recovery' },
  { id: 'key-absent', label: 'Amiibo locked — key absent', group: 'Amiibo lock' },
  { id: 'key-invalid', label: 'Amiibo locked — key invalid', group: 'Amiibo lock' },
  { id: 'key-unverified', label: 'Amiibo locked — key unverified', group: 'Amiibo lock' },
  { id: 'amiibo-library-empty', label: 'Amiibo locked — library empty', group: 'Amiibo lock' },
  { id: 'no-amiibo-feature', label: 'Amiibo locked — firmware has no NFC', group: 'Amiibo lock' },
  { id: 'no-device', label: 'Board unplugged / no device', group: 'Failure & recovery' },
  { id: 'device-busy', label: 'Port held by another process', group: 'Failure & recovery' },
  { id: 'new-power', label: 'New power — state cleared', group: 'Failure & recovery' },
  { id: 'firmware-mismatch', label: 'Different firmware', group: 'Failure & recovery' },
  { id: 'console-lost', label: 'Console dropped mid-run', group: 'Failure & recovery' },
]

export const FIRMWARE: Firmware = {
  protoVer: 1,
  fwVersion: '1.4.0',
  bootId: '8f21c4a0',
  vidPid: '1a86:55d3',
  maxFrame: 1024,
  chunkSize: 256,
  planCapacityBytes: 65_536,
  features: { macro: true, amiibo: true, config: true },
}

export const PORT = '/dev/ttyACM0'
export const BAUD = 921_600

const PLAN_HASH = '3f9c1e7a44b0d2c6'

// ── Log fixtures ───────────────────────────────────────────────────────────

let logSeq = 0
function log(t: number, source: LogEntry['source'], level: LogEntry['level'], message: string): LogEntry {
  return { id: ++logSeq, t, source, level, message }
}

const BASE_LOGS: LogEntry[] = [
  log(0.001, 'container', 'info', `control: opening ${PORT} @ ${BAUD} (dsrdtr=False, rtscts=False, DTR/RTS deasserted)`),
  log(0.012, 'container', 'info', `hello: proto_ver=1 fw=1.4.0 boot_id=8f21c4a0 features=macro|amiibo|config`),
  log(0.014, 'frame', 'debug', 'REQ  HELLO            len=1'),
  log(0.031, 'frame', 'debug', 'REP  HELLO            len=46'),
  log(0.061, 'device', 'info', 'I (1288) hid: report task started, itvl=4 (5 ms)'),
  log(0.062, 'device', 'info', 'I (1290) gap: legacy advertising started'),
  log(0.140, 'container', 'info', 'macro library: 5 files scanned, 4 compiled, 1 rejected (宏/天妇罗巢穴宏2.json)'),
  log(0.141, 'container', 'warn', 'macro rejected: 宏/天妇罗巢穴宏2.json:118 unknown event type "motion" — whole macro discarded'),
  log(0.152, 'container', 'info', `key: /keys/key_retail.bin (single file, 160 B) fp=6b1f…c9 state=KEY_READY`),
  log(0.153, 'container', 'info', 'amiibo library: 955 .bin indexed across 27 series, 846 distinct IDs'),
  log(0.160, 'container', 'info', 'console link: advertising as Pro Controller (057e:2009)'),
  log(0.422, 'device', 'info', 'I (1640) gap: connection established, conn_itvl=4'),
  log(0.423, 'frame', 'debug', 'EVT  LINK_UP          console=connected'),
  log(0.430, 'container', 'info', 'console link: connected, bond restored (CCCD 0x000e resubscribed)'),
]

// ── Scenario builders ──────────────────────────────────────────────────────

function base(id: ScenarioId): DeviceState {
  return {
    scenario: id,
    control: { link: 'UP', port: PORT, baud: BAUD },
    console: { link: 'ADVERTISING', bonded: true },
    firmware: FIRMWARE,
    mode: 'IDLE',
    selectedMacroId: null,
    plan: null,
    uploading: null,
    selectedFigureId: null,
    placement: null,
    key: 'KEY_READY',
    keySpelling: 'single file (key_retail.bin)',
    macroLibrary: 'READY',
    amiiboLibrary: 'READY',
    stopReason: 'CONTAINER_STOP',
    lastError: null,
    recovery: 'SAME_POWER',
    config: { reportIntervalMs: 5, led: true },
    logs: [...BASE_LOGS],
  }
}

export function buildState(id: ScenarioId): DeviceState {
  const s = base(id)
  const runningLogs = (macroName: string) => [
    ...s.logs,
    log(1.02, 'frame', 'debug', 'REQ  LOAD_PLAN       off=0 len=256'),
    log(1.06, 'frame', 'debug', 'REP  ACK            next=4096'),
    log(1.09, 'container', 'info', `plan: ${macroName} → 2685 B, sha256 ${PLAN_HASH} committed`),
    log(1.10, 'frame', 'debug', 'REQ  START'),
    log(1.11, 'frame', 'debug', 'REP  ACK'),
    log(1.12, 'container', 'info', `mode: MACRO started, plan=${PLAN_HASH}`),
    log(1.13, 'frame', 'debug', 'EVT  MODE_CHANGED   IDLE→MACRO'),
    log(6.20, 'frame', 'debug', 'EVT  LOOP_COMPLETED loop=1'),
    log(11.4, 'frame', 'debug', 'EVT  LOOP_COMPLETED loop=2'),
  ]

  switch (id) {
    case 'macro-ready':
      s.selectedMacroId = 'tempura-1'
      break
    case 'macro-running':
      s.selectedMacroId = 'tempura-1'
      s.mode = 'MACRO'
      s.console = { link: 'CONNECTED', bonded: true }
      s.plan = { macroId: 'tempura-1', hash: PLAN_HASH, frameCount: 243, currentFrame: 118, loopCount: 3, bytes: 2_685 }
      s.logs = runningLogs('天妇罗巢穴宏1')
      break
    case 'uploading':
      s.selectedMacroId = 'almond'
      s.uploading = { macroId: 'almond', offset: 3_072, total: 3_356 }
      s.logs = [
        ...s.logs,
        log(1.02, 'frame', 'debug', 'REQ  LOAD_PLAN       off=0 len=256'),
        log(1.04, 'frame', 'debug', 'REP  ACK            next=3072'),
        log(1.06, 'container', 'info', 'plan: staging 杏仁巢穴宏 3072/3356 B (atomic commit on final CRC)'),
      ]
      break
    case 'upload-failed':
      s.selectedMacroId = 'almond'
      s.lastError = { code: 'PLAN_TOO_LARGE', message: 'Device plan capacity is 65536 B but the staged payload is 67210 B.' }
      s.logs = [
        ...s.logs,
        log(1.02, 'frame', 'debug', 'REQ  LOAD_PLAN       off=0 len=256'),
        log(1.20, 'frame', 'debug', 'REP  ERROR          code=PLAN_TOO_LARGE'),
        log(1.21, 'container', 'error', 'plan rejected: PLAN_TOO_LARGE (67210 > 65536 B) — staging buffer discarded, no plan committed'),
      ]
      break
    case 'macro-rejected':
      s.selectedMacroId = 'tempura-2'
      s.lastError = { code: 'BAD_PLAN', message: '宏/天妇罗巢穴宏2.json:118 unknown event type "motion" — the whole macro was rejected.' }
      break
    case 'boot-local':
      s.selectedMacroId = 'tempura-1'
      s.plan = { macroId: 'tempura-1', hash: PLAN_HASH, frameCount: 243, currentFrame: 0, loopCount: 7, bytes: 2_685 }
      s.mode = 'IDLE'
      s.stopReason = 'BOOT_LOCAL'
      s.console = { link: 'CONNECTED', bonded: true }
      s.logs = [
        ...s.logs,
        log(6.20, 'frame', 'debug', 'EVT  LOOP_COMPLETED loop=7'),
        log(6.42, 'frame', 'debug', 'EVT  STOPPED        reason=BOOT_LOCAL'),
        log(6.43, 'container', 'warn', 'mode: stopped at the board (BOOT short press) — plan retained, not restarting (container does not auto-restart BOOT_LOCAL)'),
      ]
      break
    case 'macro-library-empty':
      s.macroLibrary = 'EMPTY'
      s.lastError = { code: 'LIBRARY_EMPTY', message: 'No macro library is mounted at /library/macros.' }
      break
    case 'amiibo-ready':
      s.selectedFigureId = 'link-totk'
      break
    case 'amiibo-placed':
      s.selectedFigureId = 'link-totk'
      s.mode = 'AMIIBO'
      s.console = { link: 'CONNECTED', bonded: true }
      s.placement = { figureId: 'link-totk', identity: '04 1F 3A 9C D2 7E B1', index: 4, scans: 2, sinceSec: 96 }
      s.logs = [
        ...s.logs,
        log(1.02, 'container', 'info', 'seal: Link (TotK) + key → 540 B tag, identity 04 1F 3A 9C D2 7E B1 (placement #4)'),
        log(1.05, 'frame', 'debug', 'REQ  PLACE_AMIIBO   len=540'),
        log(1.09, 'frame', 'debug', 'REP  ACK'),
        log(1.10, 'frame', 'debug', 'EVT  TAG_PLACED     identity=041f3a9cd27eb1'),
        log(12.4, 'frame', 'debug', 'EVT  TAG_SCANNED    scans=1'),
        log(95.2, 'frame', 'debug', 'EVT  TAG_SCANNED    scans=2'),
        log(95.3, 'container', 'info', 'console read the tag — keeping placement until it stops polling'),
      ]
      break
    case 'key-absent':
      s.selectedFigureId = 'link-totk'
      s.key = 'KEY_ABSENT'
      s.keySpelling = null
      s.amiiboLibrary = 'READY'
      s.lastError = { code: 'KEY_ABSENT', message: 'No key material found at /keys/key_retail.bin.' }
      break
    case 'key-invalid':
      s.selectedFigureId = 'link-totk'
      s.key = 'KEY_INVALID'
      s.keySpelling = 'pair (unfixed-info.bin + locked-secret.bin)'
      s.lastError = { code: 'KEY_INVALID', message: 'Key material at /keys/ failed the nfc3d round-trip (HMAC mismatch).' }
      break
    case 'key-unverified':
      s.selectedFigureId = 'link-totk'
      s.key = 'KEY_UNVERIFIED'
      s.keySpelling = 'single file (key_retail.bin)'
      break
    case 'amiibo-library-empty':
      s.selectedFigureId = null
      s.amiiboLibrary = 'EMPTY'
      break
    case 'no-amiibo-feature':
      s.selectedFigureId = 'link-totk'
      s.firmware = { ...FIRMWARE, features: { macro: true, amiibo: false, config: true } }
      break
    case 'no-device':
      s.control = { link: 'DOWN', port: PORT, baud: BAUD }
      s.console = { link: 'DISCONNECTED', bonded: true }
      s.firmware = null
      s.mode = 'IDLE'
      s.plan = null
      s.placement = null
      s.logs = [
        log(0.001, 'container', 'info', `control: opening ${PORT} @ ${BAUD} (dsrdtr=False, rtscts=False, DTR/RTS deasserted)`),
        log(0.400, 'container', 'error', 'control: no device on /dev/ttyACM0 — is the board flashed and the cable a data cable?'),
        log(0.401, 'container', 'warn', 'control: if DTR/RTS were asserted the board holds in reset and presents exactly like this'),
        log(3.001, 'container', 'info', 'control: retrying in 2 s'),
      ]
      break
    case 'device-busy':
      s.control = { link: 'BUSY', port: PORT, baud: BAUD, heldBy: 'idf.py monitor (pid 41277)' }
      s.firmware = null
      s.console = { link: 'DISCONNECTED', bonded: true }
      s.plan = null
      s.placement = null
      s.logs = [
        log(0.001, 'container', 'error', `control: cannot open ${PORT}: device busy (held by idf.py monitor, pid 41277)`),
        log(0.002, 'container', 'warn', 'control: stop the flashing/monitor session — one wire carries flash, log and control'),
      ]
      break
    case 'new-power':
      s.recovery = 'NEW_POWER'
      s.control = { link: 'UP', port: PORT, baud: BAUD }
      s.firmware = { ...FIRMWARE, bootId: 'a17e0b93' }
      s.plan = null
      s.mode = 'IDLE'
      s.lastError = { code: 'NEW_POWER', message: 'Device boot_id changed (8f21c4a0 → a17e0b93): the board lost power and its volatile plan and tag were cleared.' }
      s.logs = [
        log(0.001, 'container', 'info', `control: opening ${PORT} @ ${BAUD} (DTR/RTS deasserted)`),
        log(0.020, 'container', 'warn', 'hello: boot_id changed 8f21c4a0 → a17e0b93 — new power, discarding plan and placement'),
        log(0.021, 'container', 'info', 'hello: re-upload required before MACRO or AMIIBO can start'),
      ]
      break
    case 'firmware-mismatch':
      s.recovery = 'DIFFERENT_FIRMWARE'
      s.firmware = { ...FIRMWARE, fwVersion: '1.2.0', bootId: 'c3901f5e', features: { macro: true, amiibo: false, config: true } }
      s.lastError = {
        code: 'VER_MISMATCH',
        message: 'Connected firmware 1.2.0 speaks proto_ver 1 but has no amiibo feature (container expects 1.4.0).',
      }
      s.logs = [
        log(0.001, 'container', 'info', `control: opening ${PORT} @ ${BAUD} (DTR/RTS deasserted)`),
        log(0.030, 'container', 'warn', 'hello: firmware differs (1.2.0 vs expected 1.4.0) — some verbs may be unavailable'),
        log(0.031, 'container', 'warn', 'hello: features=macro|config (amiibo unavailable on this firmware)'),
      ]
      break
    case 'console-lost':
      s.selectedMacroId = 'tempura-1'
      s.mode = 'IDLE'
      s.plan = { macroId: 'tempura-1', hash: PLAN_HASH, frameCount: 243, currentFrame: 87, loopCount: 2, bytes: 2_685 }
      s.stopReason = 'CONSOLE_LOST'
      s.console = { link: 'DISCONNECTED', bonded: true, lastDropReason: '531' }
      s.logs = [
        ...s.logs,
        log(6.20, 'frame', 'debug', 'EVT  LOOP_COMPLETED loop=2'),
        log(9.71, 'frame', 'debug', 'EVT  LINK_DOWN       console=disconnected reason=531'),
        log(9.72, 'container', 'warn', 'console link lost (reason 531) — the firmware does not watch either link, so the container stopped the run'),
        log(9.73, 'frame', 'debug', 'REQ  STOP'),
        log(9.74, 'container', 'info', 'mode: MACRO → IDLE (CONSOLE_LOST) — not auto-restarted'),
      ]
      break
    case 'idle':
    default:
      break
  }
  return s
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

export function macroById(id: string | null): MacroFixture | undefined {
  return MACROS.find((m) => m.id === id)
}

export function figureById(id: string | null): Figure | undefined {
  return FIGURES.find((f) => f.id === id)
}
