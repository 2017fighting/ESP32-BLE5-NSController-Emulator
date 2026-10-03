# The console-lifecycle policy on the bench (issue #31)

**Scope:** §9.3's container-side policy — stop-on-drop for `MACRO`, rotate-on-reconnect for a kept
placement, `boot_id` recovery as normal operation, and the screens saying the right thing.
**Date:** 2026-10-03.
**Hardware on test host:** ESP32-S3-N16R8 on the CH9102 bridge, serial `5C930639851`
(`/dev/cu.usbmodem5C930639851`), the flashed image answering at **115200** (`fw=0.1.0`,
`features=0x0001` = `MACRO` only, as in #30's session — the tree's 921600 default is #33's).
**Console:** a real NS2, paired fresh for this session (the board's stale bond was dropped with
`PAIR_UNPAIR` first; ADR-0013's flow, observed end to end — grip-order scan, full init,
`pairing complete status=0`, encryption, bond).

This records what the bench settled, the three findings that changed code or the record, and the
half of the policy that cannot be observed on this firmware.

---

## 1. What was built

Chapter 9's policy lives in `Controller._on_console_link` / `_on_scan_ended`
(`container/ns2container/state.py`), applied from `STATUS` + `EVENT` alone (§8.3: "in one
place, from STATUS alone"):

- **`MACRO` + drop → stop the run.** One `STOP`, decided from a re-read `STATUS` (§3.1), so a
  duplicate edge or a run that already ended sends nothing further. `CONSOLE_LOST` is a
  container-side reading that outranks the device's plain `CONTAINER_STOP` in the UI and never
  becomes a wire value; it is cleared by a new run, a manual stop, and `NEW_POWER` — never by
  the reconnect (the story of why the run ended survives it).
- **`AMIIBO` + drop → keep the placement, rotate on the first up-edge** (`CONNECTED` or the
  `RESUBSCRIBED` the console issues itself), idempotent per drop. `SCAN_ENDED` takes the same
  `_rotate_placement` action (§6.5: "the same action it already takes on SCAN_ENDED, so this is
  a policy, not new machinery"). A failed rotation is a warning; the event loop survives it and
  the next edge retries.
- **The `StubDevice` was corrected to the firmware's stop-reason semantics** (finding §3.2
  below): `last_stop_reason` is written by a stop and by nothing else.

Ten new offline tests pin the policy (`container/tests/test_ns2container_state.py`), including
the rotation with a sealer double that mints a fresh identity per call (the property a rotation
is only observable against). The screens: the RunCard now states §9.3's one consequence of
starting with no console, and the Connection screen's console card names the drop/rotation
policy where the user looks when the console misbehaves.

## 2. The bench: 18 checks, 0 failures

`scripts/bench_console_lifecycle.py` drives the real `Controller` — the same `snapshot()` and
log ring the four screens render — through seven phases. The evidence row for each phase is in
the script's summary; the story the Logs screen told, verbatim:

```text
19:19:02  console link: resubscribed
19:19:05  mode: MACRO started on correction.json
19:19:58  console link: disconnected (reason=531 (BLE_HS_ERR_HCI_BASE + 0x13))
19:19:58  console: macro stopped — the console re-initialises itself on reconnect, so restarting is yours to do
19:20:03  console link: connected            ← the sleeping console reconnected by itself
19:20:05  console link: resubscribed         ← and re-subscribed 0x000e itself (§9.1, both halves observed)
19:20:15  mode: MACRO started on correction.json   ← the second run (a human's START)
19:20:15  console link: disconnected (reason=531 …) ← the sleep-loop dropped this one too
19:20:15  console: macro stopped — …          ← the policy stopped it again: one STOP per drop
19:20:15  mode: stopped from the app
19:20:18  recovery: the device reported a boot (boot_id=70d8a160)
19:20:18  recovery: new power — plan and placement cleared; re-upload required
```

| Phase | What was asserted | Result |
| --- | --- | --- |
| 2 | the run is in `MACRO`, console connected | PASS |
| 3 | drop → `IDLE`, **exactly one** `STOP`, UI reads `CONSOLE_LOST`, no error, plan retained | PASS ×5 |
| 4 | the device's own `last_stop_reason` is a plain `CONTAINER_STOP` | PASS |
| 5 | reconnect → still `IDLE`, no second `START`, the drop story survives | PASS ×3 |
| 6 | a fresh run replaces the drop reading (`CONTAINER_STOP` after a stop by hand) | PASS ×3 |
| 7 | RTS reboot mid-run → `NEW_POWER`, `IDLE`, reason `NONE`, no error, plan cleared | PASS ×4 |

Phase 7 is §9.2's "expect a reboot anyway" read against the board: the run ends with
`last_stop_reason=NONE` and nothing is surfaced as an error — normal operation, exactly as the
issue's wording budget assumes.

## 3. Findings

### 3.1 A verb's reply can drown during a console reconnect (transport, #33's territory)

One bench round died at `START`: the console's reconnect burst (full init + the DEBUG log flood
at 115200) cost the `START` reply its whole 2 s request timeout. The **container behaved per
§8.9**: it marked the link down, surfaced `NO_DEVICE`, and did not re-send the verb — the
recovery is the connect loop's re-`HELLO`, then reading the truth (`mode=MACRO` means the verb
landed late; anything else means it never landed). No code change taken; the 2 s ceiling under
a log flood is a fact for #33's baud measurement, and #30's ~60 ms `STATUS` floor should not be
quoted as the worst case — a reconnect burst is.

### 3.2 The stub cleared `last_stop_reason` on `START`/`PLACE_AMIIBO`; the firmware does not, and §3.4 sides with the firmware

The first bench round's phase 6 asserted "a fresh run carries no stop reason" and failed: the
device kept `CONTAINER_STOP` across a `START`. §4.3's transition table writes `last_stop_reason`
only on stop rows, and §3.4 defines `NONE` as "a device that has booted and has not yet
stopped" — the value survives starts on the wire. The stub's clearing was an embellishment a
test could inherit (this one did); `stub.py` now writes the reason only where the firmware
does (a stop, a boot). The UI is unaffected by construction: the stop-reason badge renders only
in `IDLE`.

### 3.3 The RTS reset pulse must be esptool's recipe

The first attempt at phase 7 pulsed nothing: `setRTS(True)→(0.2 s)→setRTS(False)` with **DTR
left high throughout drives `BOOT`, not `EN`**, on the CH9102's cross-coupled auto-reset
circuit. The working pulse is esptool's hard reset — `dtr=False, rts=True` at open, hold, then
`rts=False` — which resets the board through a second opener while the container keeps the
port (the node is non-exclusive on macOS, #30 §3.2). `bench_console_lifecycle.py` carries it as
code so the next session does not rediscover it.

### 3.4 A powered, bonded board keeps the console from sleeping (observed, out of scope)

The user reported it mid-session and #14's data explains it: a sleeping NS2 **keeps scanning
for its bonded controllers** (it is how "press a button to wake" works), the board re-advertises
3 s after every drop, the sleeping console reconnects and re-subscribes **while asleep**, and
the resumed HID reports wake it to the lock screen — where, with no triple-press unlock, it
sleeps again. Loop. This is the console-half mirror of §9.1's "wake needs nothing from us",
seen from the other end: nothing needs re-sending, and nothing can stop it either. The device
watches neither link (ADR-0008), so no container policy can intervene; the operational escape
is unplug or unpair. It is recorded here rather than amended into §9/§10 — promoting a
deployment rule ("to let the console sleep, the board must be off or unpaired") was left to the
deployment/operational-rules pass. **That pass was #38, and the rule now lives in §10.5**
(`link-drop-bench.md` §2 observed the same sleep/wake loop again).

### 3.5 The rotation half is pinned offline, not benched

`HELLO.features` reports `amiibo = false` on this firmware, so no placement can exist to
rotate — the offline suite is the guarantee until #25's NFC path and #32's sealer land. What
*was* observed on the wire is the trigger the rotation hangs on: both up-edges of §9.1
(`CONNECTED` and `RESUBSCRIBED`, the latter from the bond-restore subscribe) fire on every
reconnect, so the rotation has two real edges to hang on and the idempotency per drop matters.

## 4. Verification performed

| Step | Result |
| --- | --- |
| Offline suite, `python3 -m unittest discover -s container/tests -p 'test_*.py'` | **169 tests, OK** (10 new) |
| Repo ruff (`ruff check container/`) | **All checks passed** |
| `tsc -b && vite build` (`container/web`) | clean |
| Bench phases 1–7 (`scripts/bench_console_lifecycle.py`) | **18 PASS / 0 FAIL** (§2) |
| Pairing from the console after `PAIR_UNPAIR` | grip-order scan → full init → bonded (ADR-0013) |

## 5. Still open

- **#38** — ~~mid-press resume and the release-build reboot rate~~ **Closed.** This ticket's
  stop-on-drop is the policy #38 verified never bites (one `STOP`, `IDLE`, no resume), and the
  neutral-across-a-drop check was #38's; the release build read **0 reboots in 21 disconnects**
  (`link-drop-bench.md`).
- **#33** — the 2 s request timeout under a reconnect-time log flood (§3.1) is a bench fact for
  the baud ticket; at 921600 the flood drains 8× faster.
- **#25/#32/#36** — the rotation on real hardware, once `features.amiibo` exists to place
  against.
- **§10 (deployment)** — the "a bonded board keeps the console awake" rule (§3.4) wants a home
  in the operational rules when that chapter's pass happens.
