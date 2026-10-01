# Container architecture, deployment and web UI (#7)

Answer to [issue #7](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/7),
the container half of the map. Decisions only: the artifact is a throwaway UI
prototype under `prototype/web-ui/` plus a written architecture. No container
code was built (map Q1).

Constraints taken as given, not re-opened here: Q2 (USB serial, no Wi-Fi), Q6
(device owns replay timing), Q7 (container-primary control, BOOT panic stop),
Q9 (stateless device), Q10 (neutral at loop boundaries), Q12 (firmware owns
pairing), and the handoffs from [#5] (framing, verbs, one outstanding request),
[#6] (mode machine), [#8] (plan format), [#9] (identity model) and [#13] (key
policy).

## Verdict

**A single-process, single-container Python service** — one asyncio loop, the
control link, the HTTP API and the static UI in one image — talking to one board
over the CH9102 port. **Four screens**: Control, Logs, Connection, Settings,
with a two‑option mode switch on Control. The container **holds no durable
state** and **never owns pairing**; both are told to the user on screen rather
than hidden. Deployment is `docker compose` with three read-only bind mounts
(macros, amiibo library, key material) and one `--device` passthrough.

Three things the ticket asked to weigh, answered:

- **Compose over `docker run`** as the documented default, because the product
  is defined by its mounts and device passthrough, and a run line with four
  flags is a copy-paste hazard every time. The equivalent `docker run` is kept
  in the same doc — one person will always want it.
- **One process, not two.** Q6 removed the timing argument for a separate
  serial daemon; what remains is a single 2 Hz poll and a serialised request
  queue, which is one asyncio task, not a service.
- **The serial port is a setting whose default is `/dev/ttyACM0`.** Q2's literal
  `--device=/dev/ttyACM0` stays the documented default; the container itself
  takes the path as configuration so a replugged board can be pinned by
  `/dev/serial/by-id`.

## Evidence

### The prototype

`prototype/web-ui/` — Vite + React 19 + TypeScript (strict) + Tailwind v4 +
shadcn/ui (`radix-nova`, neutral, Geist) + Noto Sans SC (the macro library's
filenames are Chinese and neither Geist nor a stock Linux desktop has a CJK
face). Fixtures are the real ones: the four `宏/*.json` macros with their
measured event counts, loop lengths and compiled sizes, and real `.bin` figures
across seven series.

`prototype/web-ui/ui-contract.md` is the design control plane; the live token
authority is `prototype/web-ui/src/index.css`.

### The measured floor

| Check | Tool | Result |
| --- | --- | --- |
| WCAG 2.0/2.1/2.2 A+AA, 22 states × 2 themes × 3 widths | axe-core via Playwright | **0 violations** |
| Browser console/page errors, 22 states | Playwright console listener | **0** |
| shadcn colour/radius/spacing trace to tokens (no raw hex/px) | `validate_mockup{system:"shadcn"}` L1 | **PASS** |
| Contrast floor | `validate_mockup` L2 | **PASS** |
| Visible focus on every focusable element (keyboard walk, 18 elements) | `scripts/gate-checks.mjs` | **PASS** |
| Primary action ≥ 44 px; no target < 24 px | `scripts/gate-checks.mjs` | **PASS** |
| `prefers-reduced-motion` collapses transitions; zoom not disabled | `scripts/gate-checks.mjs` | **PASS** |
| shadcn boolean rubric | L4 | **7/7** |

Two token-layer deviations from the preset were forced by the gate and are
recorded in `ui-contract.md`: light `--muted-foreground` L 0.556 → 0.46 and
light `--destructive` L 0.577 → 0.50, so muted and destructive text clear 4.5:1
on their own tints rather than only on `--background`.

### Expert-UX protocol (5 steps, the `frontend-mockup-loop` rule corpus)

1. **Accessibility floor** — the table above. No WCAG-AA or severity-4 defect open.
2. **Heuristic rubric** — the 22-item seed minus the three that cannot apply to a
   persistent-panel control surface (free-text field validation, multi-step
   progress, onboarding tour); every applicable check passes.
3. **PURE friction** — the worst step is "place an amiibo with no key mounted",
   which is red **by design**: no screen can mount a host file, and the key is
   read once at startup. The screen names the exact mount line instead of
   pretending, so the blocking is honest. Every other step is green.
4. **Severity** — the only defects found were accessibility-gate (contrast,
   focusable scroll regions, the 44 px target) and are fixed; no severity ≥ 3
   remains.
5. **Fix list** — applied one at a time, re-scored after each; the loop is the
   commit history, not a plan.

## 1 · Web UI scope

Four destinations, ≤ 5 so the mobile tab bar is legal (NN/g hamburger/mobile
nav). Every screen has exactly one dominant action (Von Restorff + Nielsen H8).

| Screen | Purpose | Focal action |
| --- | --- | --- |
| **Control** | choose what to run, then run it | `Start macro` / `Place amiibo` → `Stop macro` / `Unplace` |
| **Logs** | control-link traffic, frame trace, device log | none (read-only) |
| **Connection** | control link, console link + pairing, recovery, device facts | `Reconnect` / `Request unpair` |
| **Settings** | `CONFIG`, mounts, appearance | `Save config` |

Control carries a two-option **mode selector** (Hick's Law) and the picker for
the selected mode — macro library or amiibo library — so the "mode selector",
"macro picker + start/stop" and "amiibo picker + place/unplace" the ticket asked
for are one coherent task, not three screens the user has to stitch together
(Miller's Law / cognitive-load reduction). **Connection status is always
visible** in the header (control link, console link, mode) — H1 — with the
detail on Connection.

States the prototype renders, each deep-linkable and each scored:

- **Macro:** idle, selected/ready, running (loop count, frame progress, plan
  hash), uploading (windowed ACK, determinate progress + cancel), rejected at
  ingestion, rejected by the device (`PLAN_TOO_LARGE`), stopped at the board
  (`BOOT_LOCAL`), library not mounted.
- **Amiibo:** selected, placed (minted identity, placement #, scan count), and
  the four independent lock states — `KEY_ABSENT`, `KEY_INVALID`,
  `KEY_UNVERIFIED`, `LIBRARY_EMPTY`, plus `features.amiibo == false`.
- **Connection:** no device, port held by another process, new power
  (`boot_id` changed), different firmware, same-power reconnect.

### Explicitly not built

Recording, macro authoring or editing, key upload or any key path in the UI,
pairing initiation, OTA/flashing, authentication, telemetry graphs, multi-board
or multi-user anything, and any on-screen claim the firmware does not back.
Macro/library editing is out of scope on the map (Q8); key material never
crosses a request boundary (#13).

**Two things beyond the ticket's minimum screen list, kept deliberately.**
*Settings* holds `CONFIG` (`report_interval_ms`, `led`) — a verb #5's own table
defines, volatile and applied at the next loop boundary — plus the mount lines
and the key state that #13 requires the UI to be able to print; without a
Settings screen, `CONFIG` has no home and the key notice has nowhere persistent
to live. The *theme toggle* is a **prototype affordance, not product scope**:
the mockup loop requires dark and light to be verified, and the shipped app
simply follows the OS preference (the `theme` deep-link and the toggle exist
only in the prototype).

## 2 · UX decisions, each with its cited rule

| Decision | Rule / source |
| --- | --- |
| Sidebar + inset dashboard, status pills top-right, skip link | Jakob's Law — work like the dashboards users know (lawsofux.com/jakobs-law) |
| Two mode options, never three | Hick's Law (lawsofux.com/hicks-law) |
| Exactly one visually-dominant action per view | Von Restorff + Nielsen H8 (nngroup.com/articles/ten-usability-heuristics) |
| Status = icon **and** word, never colour alone | WCAG 2.2 SC 1.4.1 (w3.org/TR/WCAG22) |
| Errors listed once at the top, each linking to its control | GOV.UK error-summary (design-system.service.gov.uk/components/error-summary) |
| Locked/empty states name the one action that fixes them | Nielsen H9 + NN/g empty-state (nngroup.com/articles/empty-state-interface-design) |
| Determinate upload progress with a cancel | NN/g response-time limits (nngroup.com/articles/response-times-3-important-limits) |
| Four nav items in a bottom bar on mobile | NN/g mobile nav (nngroup.com/articles/hamburger-menus) |
| Copy uses *Mode, Macro, Amiibo, Tag, Placement, Identity, Panic stop* | Nielsen H2 + `CONTEXT.md` |
| `BOOT_LOCAL` is a distinct reason, never an error, never auto-restarted | #6 resolution; map Q7 |
| The three amiibo lock states stay separate | #13 resolution |

Notable microcopy: pairing says *what the app cannot do*; the key notice prints
the exact mount line with a copy button; the operating-rules card says flashing
needs the container stopped.

## 3 · Container architecture

### 3.1 Runtime

**Python 3.12 on `python:3.12-slim`,** asyncio, `aiohttp`, `pyserial-asyncio`.
Python because the whole corpus that matters to the container is Python: the
reference control plane (`switch-controller-macro/web_ui.py`), the macro
ingestion semantics #8 locked against it, and the sealing path (`amiitool`'s
flow at `amiitool@4fe80a1` — **not cloned locally**; source:
`socram8888/amiitool`, per `docs/references.md`). One language for the container means
one place where the plan and the tag are built, and no second implementation of
the CRC/COBS framing to keep in step.

`aiohttp` specifically, not FastAPI: it gives the static-file serving, the SSE
stream and the WebSocket-free state push this UI needs, and it is what the
reference already uses — fewer new moving parts. `pyserial-asyncio` because the
serial read must not block the same loop that serves the UI (the reference's
`_ns_reader_thread` exists for the same reason).

### 3.2 Serial device exposure and replug

- **Passthrough is `--device`, never `--privileged`.** The board is one CDC/ACM
  node; there is nothing else to grant.
- **Host access is a group and udev concern, not a container one.** Docker hands
  the container the host node with the host's ownership, so the person running
  it must already be able to read the device — `dialout` on Debian/Arch, `uucp`
  or `plugdev` elsewhere — or the container needs `--group-add`. Diagnose with
  `id -nG` and `ls -l /dev/ttyACM0`. No udev rule is **required**: the CH9102 is
  a stock CDC/ACM device, so udev already creates `/dev/serial/by-id/…`; a rule
  is only wanted for a fixed friendly name.
- **Default path `/dev/ttyACM0`** (Q2, upheld literally by #5: on this host that
  is the CH9102 `1a86:55d3`, not the native OTG port). The path is
  configuration, `CONTROLLER_PORT`, defaulting to that value; the deployment doc
  shows the by-id form for a machine where the ACM number moves.
- **Open sequence is fixed and non-negotiable:** `dsrdtr=False`,
  `rtscts=False`, then `setDTR(False)`/`setRTS(False)`, and again in `finally`.
  Without it the CH9102 asserts DTR→GPIO0/RTS→EN and the board sits in a reset
  loop that looks exactly like "unplugged" (#5, `s3-bringup.md` §10). The
  Connection screen says this in words, because it is the failure a person will
  otherwise misdiagnose.
- **Baud 921600** with 115200 fallback (a #5 recommendation, not a measured
  fact — flagged as such).
- **Replug is container-led and stateless.** On re-attach the container sends
  `HELLO`, reads `boot_id` and takes one of three branches — same power (keep
  UI, re-`STATUS`), new power (clear plan and placement, require re-upload),
  different firmware (announce; features may differ). The board is never
  assumed to have survived a replug (Q9).
- **No hot-plug policy on the key**: key material is read once at startup
  (#13), so a replug does not re-read it.

### 3.3 Service decomposition

One process, four modules with narrow interfaces — the seam is where the device
is, and nowhere else:

```
web (aiohttp)        routes + SSE + static; never opens the port, never sees key bytes
  │  DeviceApi:  status(), load_plan(hash, bytes), start(), stop(),
  │              place(tag), unplace(), pair_unpair(), config(**kw)
device (session)     owns the port; one outstanding request; STATUS @2 Hz;
  │                  EVENT on edges; recovery on boot_id
  │  FrameIO:  request(verb, payload) -> reply | error ;  events() -> async iterator
serial (transport)   COBS + CRC framing, log demux, resync on CRC failure
sealing              pure: (figure's raw tag image, key material) -> 540 B Tag + minted Identity
```

- **One outstanding request** at the serial layer, no request ids, no
  control-verb retransmission (#5). Retrying `START` is a second start; this is
  enforced by the interface's shape, not by discipline.
- **Bulk upload** is the only retried thing, addressed by the ACK'd offset.
- **`STATUS` at 2 Hz, `EVENT` only on edges** (#5): the container holds numbers
  in memory and pushes them to the browser; it does not poll faster to make the
  UI feel live.
- **Sealing is a pure function** with no device and no HTTP access, so key bytes
  never reach the framing layer or a request handler (#13).
- **The browser never talks to the device.** HTTP/SSE only; one place converts
  device truth into UI state, which is what makes the "honest about pairing"
  requirement implementable rather than aspirational.

### 3.4 Macro library and plan cache

- **Bind mount `/library/macros` read-only**, scanned at startup with a manual
  `Rescan` in the UI. A macro is every `*.json` in the mount (the reference
  library also contains `.png`/`.jpg` companions, which are ignored).
- **Ingestion is all-or-nothing** (#8): one unknown event type rejects the whole
  macro. A rejected macro still appears in the list with its reason and cannot be
  started — hiding it is how a user concludes the file is missing.
- **Identity is the compiled payload's SHA-256** (#8). The cache is keyed
  `(path, mtime, size) → {hash, records}` and the container hands the device the
  hash, which the device echoes; the container never asks the device to compute
  one.
- **The cache is in memory only.** Nothing durable is written anywhere — the
  same rule that keeps `key_retail.bin` off disk keeps the compiled plans
  disposable; a restart recompiles in well under a second (the whole library is
  793–3356 B).

### 3.5 Amiibo library and sealing

- **Bind mount `/library/amiibo` read-only**, indexed at startup. `.bin` is
  canonical; `.nfc` is ignored (#9). `!Essential Files/` is excluded from the
  index — it is the key, not a figure.
- **The index handles #13's intake surprises deliberately:** 11 of 940 `.bin`
  files are 572 B (540 + a 32-byte trailer that is *not* a hash of the 540
  bytes), so the reader slices the first 540 B rather than trusting file size;
  846 distinct IDs across 955 files and four IDs with multiple distinct UIDs mean
  the index is keyed by **file**, grouped by figure and series — never by ID,
  which is not unique.
- **Per-placement synthesis, never precompute** (#9): the plaintext for a figure
  is unpacked lazily on first placement and cached in memory (520 B/figure,
  ~494 KB worst case), copied before rotation.
- **Key handling is exactly #13's policy**: fixed path `/keys/key_retail.bin`
  (or the two-file spelling), read once at startup, never written, never framed,
  never uploaded through HTTP. The Settings screen prints the mount line and the
  three lock states distinguish key vs library vs firmware.

### 3.6 Browser ↔ container interface

`GET /api/state` returns the whole UI state (links, mode, plan, placement, key
state, libraries, firmware, last error, stop reason) — small enough to send
whole, which keeps the client free of merge logic. `GET /api/events` is a single
**SSE** stream of state snapshots and log lines; the 2 Hz device poll is the
container's, and the browser simply receives. Commands are one `POST` per verb
(`/api/start`, `/api/stop`, `/api/place`, …), each returning the resulting state
or a typed error; there is no client-side optimistic mode change, because the
device is the authority on mode. Log lines carry `{source, level, message}` so
the Logs screen can separate container, device (`ESP_LOG`) and frame traffic.

### 3.7 Pairing UX, given Q12

The firmware owns bonding; the container observes and may request `PAIR_UNPAIR`.
The UI therefore: states the link and the bond as **observed** facts, offers
`Request unpair` as a request (confirm-gated, and explicitly not a data-loss
action), and carries a short *what this app can and cannot do* list saying that
pairing is started from the console's controller menu. It never shows a
"pair now" button it cannot honour.

### 3.8 Console-lifecycle policy (the container's decision)

The firmware watches neither link, so a run survives a console drop unless the
*container* stops it (#6). #14 then supplied the console-side facts this
depends on, from the bench: a console sleep **drops the Console link, ended by
the console** (`reason=531`); on wake the console reconnects and re-runs its
full init itself with **nothing re-sent and no container command**; and the
mid-macro-reconnect and console-absent questions stayed unobservable because
the base firmware has no macro mode. The policy is therefore:

- **MACRO + Console link drop → the container sends `STOP`** and surfaces it as
  `Stopped: console disconnected`. Reason: the one real risk #14 named — a run
  resuming mid-press into a freshly-connected console — is *unverified*, and a
  stopped run is recoverable while a stray input into a live game is not. The
  device sees a plain `CONTAINER_STOP`; `CONSOLE_LOST` is a container-side
  reason, not a new protocol value.
- **No auto-restart.** #14 also showed the console re-initialises itself on
  wake with no container help, so the container has nothing to re-send; it waits
  for the human, exactly as it does after `BOOT_LOCAL`.
- **AMIIBO + Console link drop → keep the placement, rotate on reconnect.** A
  placed tag with no console reading it is harmless, but a tag the console has
  already scanned must not be the one it sees next (Q3). On reconnect the
  container mints a fresh identity and re-places before the console's first
  scan, which is what it already does on the polling-stop `EVENT` (#9).
- **Console never connected → allow `START`/`PLACE_AMIIBO`, warn in the UI.**
  The device does not need the console to run, so refusing would be dishonest
  about the dependency.

### 3.9 Failure UX and retries

| Failure | What the user sees | What the container does |
| --- | --- | --- |
| Board unplugged | header `No device`; Control/Connection alert with the DTR/RTS silent-reset explanation and a checklist | retries the open with backoff; never claims the board is gone permanently |
| Port held elsewhere | `Port busy — <process>` + "stop the process holding the port" | does not retry through a held port; resumes when the connect succeeds |
| Console link dropped mid-run | `Stopped: console disconnected` (a warning) with the reason; no auto-restart | sends `STOP` once, records `CONSOLE_LOST` (device sees `CONTAINER_STOP`); rotates a placement on reconnect |
| Device rejects the plan (`BAD_PLAN`/`PLAN_TOO_LARGE`) | inline error + top summary; staging discarded, nothing committed | surfaces the typed code; the user retries after changing the macro |
| Macro rejected at ingestion | the macro stays listed with `Rejected` and its reason | never starts it |
| `BOOT_LOCAL` panic stop | `Stopped at board` (a warning, not an error); plan retained. With an error also pending it reads `Stopped at board, and something is broken` | **does not auto-restart** (map Q7) |
| New power | `boot_id changed`; plan and placement cleared | requires re-upload; re-`HELLO` on every reconnect |
| Different firmware | announce; capability-dependent controls disabled | degrades rather than erroring |
| Key/library/firmware lock | the specific lock state and its one fixing action | starts anyway (no strict mode, #13); AMIIBO offered but locked |

The general rule: **retry the transport, never retry a mode verb.**

## 4 · Deployment shape

Default: `compose.yaml` (mounts + device + `restart: unless-stopped`), the run
line beside it for the person who wants one.

```yaml
services:
  controller:
    image: ghcr.io/2017fighting/ns2-controller:latest
    devices:
      - /dev/ttyACM0:/dev/ttyACM0
    volumes:
      - "$HOME/clone/switch-controller-macro/宏:/library/macros:ro"
      - "$HOME/clone/Amiibo:/library/amiibo:ro"
      - "$HOME/clone/Amiibo/!Essential Files/key_retail.bin:/keys/key_retail.bin:ro"
    ports:
      - "8080:8080"
    restart: unless-stopped
```

Equivalent, when the ACM number is more stable by id:

```sh
docker run --rm -it \
  --device=/dev/serial/by-id/usb-1a86_USB_Single_Serial_*-if00:/dev/ttyACM0 \
  -v "$HOME/clone/switch-controller-macro/宏":/library/macros:ro \
  -v "$HOME/clone/Amiibo":/library/amiibo:ro \
  -v "$HOME/clone/Amiibo/!Essential Files/key_retail.bin":/keys/key_retail.bin:ro \
  -p 8080:8080 ghcr.io/2017fighting/ns2-controller:latest
```

Operational rules that ship with the deployment doc, all inherited and none
optional: DTR/RTS deasserted on open (silent reset loop otherwise), the host
user in the device's group (or `--group-add`), the by-id symlink as the stable
path for a replugged board, 921600 baud with fallback, one process per port, and
**flashing requires the container stopped** — one wire now carries flash, log
and control.

No Wi-Fi, no host networking, no privileged mode, no named volumes, no port
other than the UI's.

## 5 · Handed on

- **ADR candidates for [#10]** (which selects, not this ticket): single-process
  container; container-owned in-memory plan cache keyed by the compiled hash;
  the port as configuration with Q2's literal path as the default.
- **`BOOT_LOCAL` copy is UI-owned and now written:** "stopped at the board, the
  plan is still loaded, the app will not restart it".
- **The `Rescan` affordance implies a real capability** (re-walk a mount without
  restart). The library index supports it; the key deliberately does not (#13).
- **A real container build is still not licensed by this map** (Q1). This ticket
  fixes the shape; the implementation effort owns the code, the image and the
  first bench validation of 921600 baud.

[#5]: https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/5
[#6]: https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/6
[#8]: https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/8
[#9]: https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/9
[#10]: https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/10
[#13]: https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/13
