# The plan executor and the neutral template (issue #24)

**Scope:** §12.1 stage 2 step 1, §7.3 step 5 — the task that walks a committed
plan against the clock, holds each record for its `hold_ms`, and owns the
neutral template and the `LOOP_COMPLETED` rate limit. **Date:** 2026-10-03.
**Hardware on test host:** ESP32-S3-N16R8 on the CH9102 bridge, serial
`5C93063985` (`/dev/cu.usbmodem5C930639851`). The image flashed is §6's:
`0x84f50` bytes, `CONFIG_MCU_DEBUG` off, `LOG_MAXIMUM_LEVEL=INFO`, UART0 at
921600.

This records the choices the ticket asked to be recorded, the one place the
implementation disagreed with a locked chapter (§4.6 against §5.4 — a
commit-handoff bound, and its proof), and the two bench facts that changed the
code. The spec amendments are in §4.

---

## 1. Where the seam went, and why

`main/include/protocol/control/control_executor.h` +
`main/src/protocol/control/control_executor.c`: **portable C, no ESP-IDF**, so
the whole timing contract is asserted on the host rather than inferred from a
bench capture. It is a pure state machine over `(control_state_t, now_ms)` with a
two-function IO vtable:

| Op | Meaning |
| --- | --- |
| `apply_state(ctx, state[9])` | write the nine state bytes into the report's back buffer and commit (`controller_hid_commit`) |
| `commit_idle(ctx)` | true once the reporter has consumed the last commit |

Two functions rather than one because *writing bytes* and *answering "has the
reporter taken them"* are different questions, and only the second is what the
handoff needs. The firmware adapter is `control_parser.c`; the host double is a
faithful model of the double buffer's single `swap_request` bit.

**The nine bytes are the report's own layout**, so one `memcpy` reaches it:
`hid_report_pro2_t`'s `buttons` at 0x02, `left_stick` at 0x05, `right_stick` at
0x08 — contiguous, and now pinned by `static_assert`s in
`hid_controller_pro2.h` rather than by the offset comments, two of which are
off-by-one (they say 0x04/0x07; the `unknown_0x0b`/`unknown_0x0c` fields prove
0x0B/0x0C, hence 0x05/0x08). `pro2_set_state` is the one new HID op.

**The neutral template is compiled in and shared.** `control_executor_neutral`
(portable) and `pro2_neutral_state` (`hid_controller_pro2.c`) are both the nine
bytes `00 00 00 · 00 08 80 · 00 08 80`, and `pro2_report_init` now initialises the
report from the same constant the executor emits, so the release §4.6 guarantees
and the report a fresh controller carries are one definition rather than two that
agree today. Two translation units cannot compare constants at compile time, so
`control_parser_init` compares them once and logs loudly on a mismatch — a wrong
neutral is invisible on the wire, which is exactly why it gets a runtime check
instead of a promise.

## 2. The disagreement: §4.6 against §5.4, and the bounded handoff

**This is the finding the ticket existed to surface.** §4.6 requires the neutral
to be *committed and transmitted*, not merely written into the back buffer. §5.4
requires the loop to restart **immediately**, with no inter-loop gap. On this
firmware those two cannot both hold naively, because the reporter's double buffer
carries **one** `swap_request` bit (`hid_controller.c`): write neutral, write
record 0, and the reporter swaps once — the neutral never reaches the wire.

The executor therefore has an explicit **handoff** state. At a boundary it
commits the neutral, then waits for `commit_idle()` — the adapter's view of "the
reporter consumed that commit" — or for `CONTROL_EXECUTOR_HANDOFF_MAX_MS`
(50 ms), and only then writes record 0 and starts the pass's clock.

**Why this is not the gap §5.4 forbids.** The wait is the *transmission cost of the
neutral itself*, not a dwell the design schedules. The pass's clock starts when
its record 0 is applied, exactly as `web_ui.py::_play` awaits `_release_all()` and
only then sets `start = time.time()` — so `sum(hold_ms) == loop_ms` still holds
exactly of the pass's timeline, and tick quantisation cannot accumulate. The 50 ms
is a safety bound, present so that an absent console cannot stall a replay; the
expected wait is under one report period.

Both chapters were amended in place (§4.6 and §5.4) rather than one being
silently preferred, and the reasoning was verified on the wire — §3.

### A second, measured consequence: the bound must be vacuous without a console

The 50 ms bound is *correct* but was *reachable*, and that is a defect the bench
found rather than the host suite. With no console subscribed, the report task
skips the swap entirely (`hid_controller.c`: a NULL/not-subscribed/not-connected
`g_subscribe_state` short-circuits the loop), so `swap_request` stays 1 forever and
**every** loop boundary paid the full bound. Measured before the fix:

| plan `loop_ms` | records | measured | delta |
| --- | --- | --- | --- |
| 360 | 4 | 363.61 ms | +3.61 ms |
| 1000 | 2 | **1051.89 ms** | **+51.89 ms** |

A 5.2% speed error on a one-second loop, and the error is *proportional to
1/loop_ms* because the bound is fixed — so it would worsen as loops got shorter.
This is G-4 ("is a macro run harmless against an absent console?") answered in the
code: nobody is subscribed means nobody is owed the neutral, so `commit_idle()`
returns true immediately. The adapter now checks the subscription, and the same
measurements became §3.3. The host suite could not have caught it — the double
models a *reporter*, not a *subscriber* — and the portable module's contract
(`commit_idle` says "the reporter has it") was never wrong. The bug was in the
adapter's idea of when the reporter is owed anything.

## 3. The bench

The host side is `scripts/bench_plan_executor.py`, a throwaway stdlib + pyserial
client in the spirit of stage 1's. Re-run with:

```
source ~/esp/idf-env-5.5.5.sh
python3 scripts/bench_plan_executor.py --port /dev/cu.usbmodem5C930639851
```

Two things it has to get right, both learned by getting them wrong first:

- **Opening the port resets the board.** The CH9102 wires DTR→GPIO0 and RTS→EN and
  pyserial asserts both on open; deasserting afterwards still leaves the EN pulse
  behind. So the constructor opens, deasserts, and then waits for a `HELLO`
  answer rather than for a fixed sleep — the same discipline the container's
  `serial` seam needs (ADR-0014).
- **An `ERROR` is a `REPLY` whose verb is 10**, so it answers *any* request. A
  client that waits only for the request's own verb sees a rejection as a dead
  device (which is how the first run reported a firmware reboot that had not
  happened).

### 3.1 The ticket's Done-when, on the wire

| Check | Result |
| --- | --- |
| `HELLO` advertises `features.macro` (bit 0) | `features=0x0001` — §2.6's bit, flipped by #24 |
| a committed plan replays | `current_frame` advanced through the plan's records |
| and loops | `loop_count` reached 28 over 10 s of a 360 ms loop |
| the echoed `plan_hash` matches | exact for the 793 B golden fixture and for the short macro (§5.6) |
| `plan_frame_count == record_count` | 71 records for the fixture, 4 for the short plan |
| `STOP` returns to `IDLE` | `mode=IDLE`, `last_stop_reason=CONTAINER_STOP`, plan retained, `current_frame=0` |
| a zero-record plan is refused at `START` | `ERROR BAD_PLAN` (code 8, `detail` 0 per §2.5), mode stays `IDLE`, `STATUS.last_error=(8,0)` |

The golden fixture (`fixtures/plan/correction.plan.hex`, 793 B, 71 records,
`loop_ms=26205`, full SHA-256 `1f0a90…`) uploads and commits exactly; the
compiled identity is its first 16 bytes (§5.6's truncation). Its 26.2 s loop is too
long to watch loop on the bench, so the replay/loop observations use a
synthesized 360 ms macro, and the fixture's own role here is the *upload* and the
hash echo.

### 3.2 The zero-record plan, and why the fault row is not forced here

`START` on the hand-built `record_count = 0` plan returns `ERROR BAD_PLAN` and does
**not** enter `MACRO` — §4.3's added row. Two things follow that are worth
recording:

- This is an **ERROR reply**, not the executor fault path. The fault path is
  §4.6's — a malformed plan, a frame index out of range, a commit check mismatch —
  and it exits to `IDLE` leaving `last_stop_reason=NONE` with `last_error` set
  (§3.4's NONE+set row). It is asserted in the host suite, where the report can be
  made to fail on demand; on the bench the arm refuses first, because the
  dispatcher checks the arm *before* the mode moves, which is the better outcome
  (the container never sees a `MACRO` that is not running).
- The refusal is legible two ways at once: the typed `ERROR` reply, and
  `STATUS.last_error=(8,0)` on the next poll (§3.2).

### 3.3 Timing: the loop is the plan's, within the poll's granularity

Measured over a 25 s window per case, with the poll as fast as the round trip
allowed (~250–300 Hz rather than the product's 2 Hz):

| plan `loop_ms` | records | loops observed | measured | delta |
| --- | --- | --- | --- | --- |
| 360 | 4 | 69 | 361.89 ms | +1.89 ms |
| 1000 | 2 | 25 | 998.06 ms | −1.94 ms |
| 2500 | 2 | 10 | 2495.91 ms | −4.09 ms |

The deltas are the `STATUS` poll's own phase, not drift, and the sign is not
systematically positive — which is the property that matters. **No tick
accumulation is visible at any loop length**, which is the whole reason the walk
uses absolute-millisecond deadlines instead of accumulating 10 ms ticks: at
`CONFIG_FREERTOS_HZ=100` this host's timing is measured against a 10 ms grid, and
a tick-accumulating walk would show +10 ms per loop rather than ±4 ms.

The 26205 ms fixture produced 0 loops in 25 s, as designed — a 26 s loop does not
complete in a 25 s window — so it contributes only the upload/hash evidence above.

**This is not ADR-0003's vindication, and #35 owns that.** §12.2's validation 3
asks whether the executor holds timing *against the console*: input-to-input
latency on a real BLE link with the console subscribed, at `INFO`. What is
measured here is the executor's own clock against the control link, with the
console link **not established** (`console_link=ADVERTISING` throughout). That
also means the handoff's *steady-state* cost — the one report period the neutral
costs when a console *is* subscribed — is not measured here. It is bounded by
construction (the reporter consumes a commit within one report period, and the
deadline bounds the pathological case at 50 ms), and measuring it needs the BLE
capture #35 has.

### 3.4 The EVENT stream

18 `LOOP_COMPLETED` events (kind 4) were seen during the 10 s run. This is §3.3's
rate limit working: the poll was fast, so most boundaries *did* emit, and the
count is far below the 28 boundaries because a boundary emits only once the
previous event has been superseded by a `STATUS` reply. The host suite pins the
sharp case: 20 boundaries with **no** poll in between emit exactly **one** event,
while `STATUS.loop_count` stays exact at 20.

## 4. The third place the implementation had to decide: the arm can refuse

`control_effects_t.start_macro` changed from `void` to `uint8_t` (returning a
§2.5 code). The reason is that `plan_state = COMMITTED` and "this device can walk
it" are different questions, and the second belongs to the executor. Without the
change, `reply_start` would ACK and enter `MACRO` and *then* discover it could not
arm — a mode the container believes is running and that is not, which is precisely
what §4.4's "no verb with a hidden second effect" exists to prevent. Now the
refusal is a typed `ERROR` returned **before the mode moves**, and §2.3's "a
rejection leaves state untouched" holds.

`control_dispatch.c`'s control-flow note in its header says the physical halves
are effects the verb layer cannot perform; this makes one of them able to say no,
which is a small widening of that comment's contract and is recorded in the
chapter instead (§4.3's added row).

## 5. What the spec amendments are

Per §00's "Amendment mechanics", the owning chapter was edited in place and the
host suite is the machine-checkable companion. No ADR: none of these is hard to
reverse, and none changes a byte on the wire.

1. **§4.6 — the handoff.** The release invariant gains the paragraph explaining
   that the double buffer's single commit bit forces an explicit handoff, that the
   50 ms bound exists so an absent console cannot stall a replay, and that the
   pass's clock starts at record 0 — which is what keeps §5.4's "no inter-loop gap"
   true of the loop's own duration.
2. **§5.4 — the same, from the plan side.** "Immediately" is defined as
   immediately *after the neutral is handed to the reporter*, with the same bound
   and the same clock statement.
3. **§4.6 — the template's home.** `pro2_report_init` was named as the template;
   it is now `pro2_neutral_state`, shared with `pro2_report_init`, and the exit-path
   list gains `START` ("neutral first", §4.3).
4. **§3.4 — the fault row.** The table gains `NONE | set`, and the chapter records
   *why* there is no fourth stop reason: `last_stop_reason` is a closed set of *who
   stopped it*, and a device fault is a *what*, which the five-byte `last_error`
   pair already carries with more precision. §2.5's code set stays closed.
5. **§4.3 — the refused arm.** The transition table gains the
   `START` + unrunnable-plan row, and the chapter explains that a committed plan
   can still be refused and that the mode does not move.

## 6. The code review, and the two defects it found

A two-axis review (`/code-review`, Standards + Spec) of the whole change found two real defects,
one spec tension, and several smaller findings. Recorded here because both defects were
**invisible on the wire**, and neither was reachable by the host suite as it stood.

### 6.1 The `AMIIBO` exit skipped the neutral (a §4.6 breach, Spec axis)

`control_executor_stop` returned early when the executor was already `STOPPED`. But the executor
is `STOPPED` throughout `AMIIBO` — nothing arms it there — while §4.3's `AMIIBO` + `STOP` row
requires a *neutral release*. So the release was skipped on exactly the path that has no walk
running. The fix makes the neutral unconditional: §4.6 makes it the last write of **the mode**,
not of the walk, so the call commits it whether or not this module was ever armed. Halting stays
idempotent, and re-committing nine identical bytes is harmless where a missing release is the
invariant broken.

The same reasoning applies to `UNPLACE_AMIIBO`, which also leaves `AMIIBO` for `IDLE`.

**Coverage added:** a host test that places a tag, asserts the executor was never armed, and then
`STOP`s — plus a bench check that drives `PLACE_AMIIBO` and `STOP` over the control link and
confirms `mode=AMIIBO → IDLE` with `CONTAINER_STOP`.

### 6.2 The spec text described the opposite of the code (Spec axis)

The amendment said "the loop's clock starts when its record 0 is applied", which reads as a
re-anchor *every* loop. The code does the right thing — the first loop's clock starts at the
ACK'd `START`, and each later boundary's deadline is the previous one plus exactly `loop_ms` —
but the prose invited the drift §7.5 warns about. Taken literally, re-anchoring would round every
loop up to the 10 ms tick: a 25 ms loop becomes 30 ms, and 600 ms of clock yields **20**
boundaries instead of the correct **24**. Both chapters now say the first loop, exactly, and name
the re-anchoring reading as the trap it is.

This one is worth its own line because the reviewer and the implementer disagreed and the
*implementer* was wrong — about the documentation, not the code. The host suite's anti-drift
test is what settles it: it asserts 24.

### 6.3 One tension surfaced and deliberately not resolved here

`UNPLACE_AMIIBO` also moves `AMIIBO` → `IDLE`, and it does **not** emit the neutral: it goes
straight to `control_mode_enter(IDLE)` rather than through `control_mode_exit`, and §4.3's row
for it says "tag unplaced; tag data retained" — where the `STOP` and BOOT rows in the same table
both say "neutral release". §4.6's blanket list, "a mode change", would cover it.

So §4.3 and §4.6 disagree about this one path, and this ticket does **not** pick a winner:

- **It is benign on this build, and that is why it can wait.** Nothing writes the nine state
  bytes in `AMIIBO` — the tag server (#25) drives report byte `0x0C`, not the buttons or sticks,
  and `pro2_report_init` leaves them neutral — so the report is already neutral when the path is
  taken. The gap is in the *explicit* write, not in the bytes on the wire.
- **Resolving it is not free.** Reusing `control_mode_exit` would set `last_stop_reason`, and
  §4.3 is explicit that `UNPLACE_AMIIBO` "is a tag operation, not a stop, and it sets no stop
  reason". A correct fix therefore needs a release-without-a-stop entry point, which is a new
  seam in the verb/executor boundary.
- **It belongs to #25.** `AMIIBO`'s physical half is the tag server's; the executor owns the
  neutral, but *which* amiibo transitions owe one is a chapter 4/6 question that #25 is already
  editing. Handing it there keeps one decision in one place rather than settling half of it here.

Recorded as a finding for #25 rather than fixed silently, which is the discipline §00's
amendment mechanics asks for.

### 6.4 The smaller findings, and what was done

| Finding | Axis | Disposition |
| --- | --- | --- |
| `BAD_PLAN`'s `detail` was sent as the record count and the frame index | Spec | **Fixed.** §2.5 types `BAD_PLAN`'s `detail` as 0 (as §2.7 repeats for the commit path); both sites now send 0. The dead `detail` parameter was removed rather than left as generality. |
| `START` raises `BAD_PLAN`, which §2.5's "Raised by" column attributes to `LOAD_PLAN` only | Spec | **Amended.** §2.5 now says the third column is *descriptive* — the closure is over the codes, not over which verb may produce one — and explains the zero-record case. |
| Header comment said a zero-record plan is "not refused here"; the code refuses it | Spec | **Fixed.** The header now documents the refusal, which is the better behaviour (§2.3: a rejection leaves state untouched, and the mode never moves). |
| Six copies of the little-endian readers across five files | Standards | **Fixed.** Four `static inline` helpers in `control_protocol.h`; the five local copies deleted. A 4th copy had been added by this ticket, which is exactly the drift a golden fixture cannot catch (it pins the bytes, not the readers). |
| Vocabulary: "pass", "iteration", "release" | Standards | **Fixed.** `CONTEXT.md` lists `Loop`/_Avoid_: pass, iteration, repeat and `Neutral`/_Avoid_: release. Prose in the new module and the amendments now says *loop* and *neutral*. |
| `executor_commit_idle` reached into `g_hid_controller.buffer.swap_request` | Standards | **Fixed.** `controller_ops_t` gains `commit_idle`; "has the reporter taken the commit" is the HID layer's question, not the control layer's. |
| The HID-wire neutral is not directly observed | Spec | **Accepted, and recorded.** It needs a subscribed console; #35 owns it. §7 below. |

## 7. Verification performed

| Step | Result |
| --- | --- |
| Host suite `test_control_executor.c`, plain | `control executor ok: 147 checks` |
| Host suite, ASan + UBSan | same, clean |
| The other three CONTROL suites (regression) | framing 193, verbs 211, mode 264 — all pass |
| `test_plan_fixture.c` (the G-13 `static_assert`s) | `plan ok: 71 records, loop_ms=26205, 793 bytes` |
| Container + scripts Python suites | 49 + 29 tests, OK |
| `idf.py build` (esp32s3, `CONFIG_MCU_DEBUG` off) | rc 0, 0 warnings; app image **`0x84f50`** B, 83% of the 3 MB slot free |
| `idf.py -p … flash` | rc 0, hash verified |
| The bench run | §3 — all checks passed, including the §4.3/§4.6 `AMIIBO` exit |
| CI workflow updated | `test_control_executor` added to both the plain and the ASan/UBSan job, with `control_executor.c` in the portable list |

## 8. Still open, and handed to #35

- **The comparative timing measurement** (§12.2 validation 3): input-to-input
  latency with the console connected, at `INFO`. This is #35's, and it is the
  ticket that decides whether ADR-0003 needs a report-period change.
- **The neutral on the HID wire.** Everything here observes the neutral *at the
  commit* (`commit_idle` is the proof the reporter took it) and via the executor's
  own state machine. Observing the actual BLE notification needs a capture on
  `0x000e`, which needs a subscribed console — #35's harness. **This ticket's
  Done-when is therefore met on the control wire and by construction on the HID
  wire, not by direct observation of the latter.** The distinction is the honest
  one and is recorded rather than glossed.
- **The steady-state handoff cost** when a console *is* subscribed (bounded by one
  report period, ≤ 50 ms): assertable on the host as a bound, measurable only in
  #35's capture.
- **Console-drop behaviour under load** (§12.2 validation 7, G-5): with a console
  subscribed and then dropped mid-pass, whether the resumed mid-press is a
  problem. The container's stop-on-drop policy avoids needing the answer; the
  bench here had no console to drop.
