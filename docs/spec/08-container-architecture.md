# 8 · Container architecture

**One process, one image, four screens.** A single asyncio Python service on
`python:3.12-slim` owns the control link, the HTTP API and the static UI; deployment is
compose-first with three read-only mounts and one `--device`.

## 8.1 Runtime

| Choice | Why |
| --- | --- |
| **Python 3.12** on `python:3.12-slim` | the corpus that matters is Python: the reference player whose replay semantics chapter 5 locks against, and the sealing path |
| **asyncio** | the serial read must not block the loop that serves the UI |
| **`aiohttp`** | static files, an SSE stream and state push in one dependency; it is also what the reference uses |
| **`pyserial-asyncio`** | non-blocking serial on the same loop |

One language for the container means one place where the plan and the tag are built, and no
second implementation of the COBS/CRC framing to keep in step (ADR-0006).

## 8.2 The seams

Four modules with narrow interfaces. The seam is where the device is, and nowhere else.

```text
web (aiohttp)         routes + SSE + static
  │                   never opens the port; never sees key bytes
  │  DeviceApi: status(), load_plan(hash, bytes), start(), stop(),
  │             place(tag), unplace(), pair_unpair(), config(**kw)
  ▼
device (session)      owns the port; one outstanding request; STATUS @2 Hz;
  │                   EVENT on edges; recovery on boot_id
  │  FrameIO: request(verb, payload) -> reply | error ;  events() -> async iterator
  ▼
serial (transport)    COBS + CRC framing, log demux, resync on CRC failure

sealing               pure: (figure's raw tag image, key material) -> 540 B Tag + Identity
compile               pure: (macro JSON) -> plan bytes + 16-byte hash  (chapter 5)
```

Three properties the shape enforces rather than encourages:

- **One outstanding request** lives in the `device` interface. Retrying `START` is a second
  start, so there is no retry loop to add by accident — the interface has nowhere to put one.
- **Sealing is a pure function** with no device and no HTTP access, so key bytes never reach
  the framing layer or a request handler (ADR-0012).
- **The browser never talks to the device.** The web module converts device truth into UI
  state in one place, which is what makes "honest about pairing" implementable rather than
  aspirational.

## 8.3 The device session

- **Owns the port** for the process lifetime. One process per port; a second container holding
  the same device is a deployment error, detected as a busy port (§8.6).
- **Poll task**: `STATUS` at **2 Hz**. The container holds the last status in memory and pushes
  it to the browser; the browser does not poll, and the container does not poll faster to make
  the UI feel live.
- **Event handling**: an `EVENT` is a prompt to act or to re-read `STATUS`, never a substitute
  for it (chapter 3, §3.1).
- **Recovery**: `HELLO` on every attach and reconnect, then branch on `boot_id` (§2.8). A
  `BOOT` event means the same thing mid-session.
- **Console-lifecycle policy** (chapter 9) is implemented here, in one place, from `STATUS`
  alone.

### Open sequence, fixed and non-negotiable

```python
port = serial.Serial(PATH, BAUD, timeout=0.2, dsrdtr=False, rtscts=False)
port.setDTR(False)
port.setRTS(False)
try:
    ...
finally:
    port.setDTR(False)
    port.setRTS(False)
    port.close()
```

The CH9102 wires **DTR→GPIO0 and RTS→EN**; pyserial asserts both by default, which holds the
board in reset and presents as "the device is doing nothing"
(`s3-bringup.md` §10). This cost a bench session that logged zero bytes and was nearly written
up as "no connection occurred", and it is why the rule is stated as code rather than as advice.

## 8.4 The macro library

- **Read-only mount at `/library/macros`**, scanned at startup with a manual **`Rescan`** in
  the UI. A macro is every `*.json` in the mount; `.png`/`.jpg` companions are ignored.
- **Ingestion is all-or-nothing** (chapter 5): one unknown event rejects the whole macro.
- **A rejected macro still appears in the list with its reason** and cannot be started.
  Hiding it is how a user concludes the file is missing.
- **Identity is the compiled payload's SHA-256** (ADR-0010). The cache is keyed
  `(path, mtime, size) → {hash, records}` and is **in memory only**; the container hands the
  device the hash, never the reverse.
- **No recompile on a `CONFIG` change** — holds are milliseconds (ADR-0009).
- **Validate against `HELLO.plan_capacity_bytes`** before uploading, so `PLAN_TOO_LARGE` is a
  container-side pre-check and a device `ERROR` is the backstop, not the first line of defence.

## 8.5 The amiibo library

- **Read-only mount at `/library/amiibo`**, indexed at startup. `.bin` canonical, `.nfc`
  ignored, `!Essential Files/` excluded.
- **The index handles the three intake surprises of §6.2 deliberately**: slice the first 540 B
  rather than trusting file size; key by **file**, grouped by figure and series, never by ID.
- **Per-placement synthesis** with a lazy in-memory plaintext cache of ~494 KB worst case,
  copied before rotation (§6.4).
- **Key handling is exactly §6.7**: fixed path, read once, never written, never framed, never
  uploaded.

## 8.6 The three amiibo locks, and where they are decided

Amiibo can be unavailable for three independent reasons, and **the container must keep them
distinct** (§6.7). The important structural point is *where each is decided*:

| Lock | Decided by | Reaches the device? |
| --- | --- | --- |
| `KEY_ABSENT` / `KEY_INVALID` / `KEY_UNVERIFIED` | container, at startup or first placement | **no** — `PLACE_AMIIBO` is refused locally |
| `LIBRARY_EMPTY` | container, from the index | **no** |
| `features.amiibo == false` | device, in `HELLO` | yes — it is a capability, not an error |

So **two of the three never produce a frame**. The container's refusal carries a local reason
naming the one action that fixes it; the device's refusal comes from `HELLO` and disables the
control. Nothing in the wire protocol names a key (§2.5).

## 8.7 Browser ↔ container

| Surface | Shape |
| --- | --- |
| `GET /api/state` | the whole UI state: links, mode, plan, placement, key state, libraries, firmware, last error, stop reason |
| `GET /api/events` | one **SSE** stream of state snapshots and log lines |
| `POST /api/<verb>` | one endpoint per verb, returning the resulting state or a typed error |

- **State is sent whole**, because it is small enough and it keeps the client free of merge
  logic.
- **No optimistic mode change.** The device is the authority on mode; the browser shows what
  the device said.
- **Log lines carry `{source, level, message}`** so the Logs screen can separate container,
  device (`ESP_LOG`) and frame traffic.
- **The frame trace is the debugger of record.** The protocol is binary and the device is
  across a wire, so a decoded frame list is the only place a framing bug is visible.

## 8.8 The four screens

Four destinations, ≤ 5 so a mobile tab bar is legal; **exactly one dominant action per view**.

| Screen | Purpose | Focal action |
| --- | --- | --- |
| **Control** | choose what to run, then run it | `Start macro` / `Place amiibo` → `Stop macro` / `Unplace` |
| **Logs** | control-link traffic, frame trace, device log | none (read-only) |
| **Connection** | control link, console link + pairing, recovery, device facts | `Reconnect` / `Request unpair` |
| **Settings** | `CONFIG` (`report_interval_ms`, `led`), mounts, appearance | `Save config` |

Control carries a two-option **mode selector** and the picker for the selected mode, so
"choose a mode", "pick a macro and start it" and "pick an amiibo and place it" are one coherent
task rather than three screens to stitch together. **Connection status is always visible in the
header**; its detail is on Connection.

**Settings exists beyond the minimum list for a reason:** `CONFIG` is a verb the protocol
defines and needs a home, and the key notice has to live somewhere persistent that can print
the expected mount path (§6.7).

**The shipped app follows the OS theme.** The prototype's theme toggle is a prototype
affordance, not product scope.

### States the UI must render

- **Macro:** idle; selected/ready; running (loop count, frame progress, plan hash); uploading
  (determinate progress from the windowed ACK, with cancel); rejected at ingestion; rejected by
  the device (`BAD_PLAN`/`PLAN_TOO_LARGE`); stopped at the board (`BOOT_LOCAL`); library not
  mounted.
- **Amiibo:** selected; placed (minted identity, placement number, scan count); and the three
  locks of §8.6 with their one fixing action.
- **Connection:** no device; port held by another process; new power (`boot_id` changed);
  different firmware; same-power reconnect.
- **Console:** dropped mid-run (chapter 9).

**Not built, and not to be added without reopening a decision:** recording, macro authoring or
editing, key upload or any key path in the UI, pairing initiation, OTA/flashing,
authentication, telemetry graphs, multi-board or multi-user anything, and any on-screen claim
the firmware does not back.

## 8.9 Failure UX and the retry rule

| Failure | The user sees | The container does |
| --- | --- | --- |
| Board unplugged | header `No device`; an alert naming the DTR/RTS silent-reset explanation and a checklist | retries the open with backoff; never claims the board is gone permanently |
| Port held elsewhere | `Port busy — <process>` and "stop the process holding the port" | does not retry through a held port |
| Console link dropped mid-run | `Stopped: console disconnected` (a warning), no auto-restart | sends `STOP` once, records the reason locally; rotates a placement on reconnect |
| Device rejects a plan | inline error plus a top summary; nothing committed | surfaces the typed code; the user changes the macro and retries |
| Macro rejected at ingestion | the macro stays listed as `Rejected` with its reason | never starts it |
| `BOOT_LOCAL` panic stop | `Stopped at board` (a warning, not an error), plan retained; with an error pending, `Stopped at board, and something is broken` | **does not auto-restart** |
| New power | `boot_id changed`; plan and placement cleared | requires re-upload; re-`HELLO`s on every reconnect |
| Different firmware | announced; capability-dependent controls disabled | degrades rather than erroring |
| An amiibo lock | the specific lock state and its one fixing action | starts anyway; `AMIIBO` offered but locked |

> **Retry the transport, never retry a mode verb.**

## 8.10 What the container must never do

- Open the port with DTR or RTS asserted.
- Poll `STATUS` faster than 2 Hz to make the UI feel live.
- Send a control verb twice because a reply was slow.
- Optimistically show a mode it has not observed.
- Write key material anywhere, or let it cross a request boundary.
- Hide a rejected macro, a lock, or a `BOOT_LOCAL` stop.
- Restart anything by itself.
