# 9 · Console lifecycle: what the NS2 does, and what the container does about it

The device watches neither link (ADR-0008). Console-lifecycle policy therefore belongs to the
container, and it can only be written from facts about the real console. This chapter separates
**observed** from **unverified** deliberately — the device-side rule is settled, the console's
tolerance is not.

Evidence: `docs/research/ns2-console-lifecycle.md` (bench session against a real NS2, 20
connections / 19 disconnects, one uninterrupted ~10-minute parked window, 58,903 input
notifications).

## 9.0 The question #6 deferred, and why it needed the bench

Issue #6 could not write this chapter from sources. Two deferrals pointed at each other: the
mode machine needed to know what a console *does* about a link drop, and the console ticket
needed a mode machine to observe it against. It resolved by owning the device-side half (the
device watches neither link — ADR-0008) and graduating the console-side half to a bench ticket
whose facts are §9.1.

That is why the split in this chapter is where it is: **the device's rule is decided**
(§4.7) and **the console's tolerance is measured** (§9.1), while everything in between is
ordinary policy the container can change without a protocol change (§9.3). The earlier draft
of this design left "what happens to a macro on console disconnect" to the container with no
facts at all; the facts below are what turn that into a decision.

## 9.1 Observed: the console's side

| Fact | Observation |
| --- | --- |
| **Full init per connection** | every connection re-runs the whole sequence (`0x07/0x01`, `0x02/0x04` ×4, `0x10/0x01`, `0x16/0x01`, `0x0a/*`, `0x09/0x07`, `0x0c/0x02`, `0x11/0x03`, …), not a reduced reconnect subset |
| **Sleep drops the link, and the console ends it** | 19/19 disconnects are `reason=531` = HCI `0x13`, *Remote User Terminated Connection*. HCI `0x08` (connection timeout) does not occur once |
| **Wake needs nothing from us** | the console reconnects and **re-subscribes `0x000e` itself** — by a fresh CCCD write (8 times) or by **restore from the bond** (11 times) — and the firmware's subscribe handler starts the report task in either case. The stream resumes with no container command and no peer action |
| **Command `0x15` is never re-sent** | zero occurrences over 20 reconnections; the NVS bond carries the reconnect |
| **`0x03/0x01` (Bluetooth Wake) is never re-issued** | zero occurrences |
| **The link runs at 5 ms** | `conn_itvl = 4` on **all 40 samples**, with **zero** connection-update events, from the first connection onward |
| **Reconnect latency** | 3.6 s min, 4.4 s median, 11.4 s max (n=19) — the firmware's 3 s advertise timer plus the console's scan interval |
| **The grip-order screen drains continuously** | ~10 minutes parked: 45,091 notifications, largest gap 0.20 s, **zero** msys pressure, the firmware's guard never fired |
| **NFC is probed but never used** | `0x01/0x0C` once per connection (19 times); **no other** NFC subcommand ever — `0x01/0x03` (start polling) zero times |

**Two corrections to earlier records, kept here because they change what the design may
assume:**

- The link **is** 5 ms. An earlier bring-up measured 15 ms with zero update events; on this
  build the console sets 5 ms immediately on every connect, before any controller command is
  exchanged. The most likely explanation for the difference is that the earlier run was
  fighting the mbuf-exhaustion bug fixed in `7164f28`.
- "The console never asks for 5 ms" and "the console stalls HID on the grip-order screen" are
  both **not reproduced**. The design does not rely on either.

**And one thing the console cannot be asked about.** With the base firmware (no `MACRO`, no
`AMIIBO`), a mid-macro reconnect, a console-absent run, and an amiibo unplace are
**unobservable, not unanswered** — the firmware cannot present the condition, so the console
can never be seen responding to it. NFC polling starting is the clearest example: the console
probes `0x01/0x0C` and then never enters the `.nfp` service, so nothing about the tag path is
confirmed or falsified.

## 9.2 Observed: the device's own reboot habit

`gap.c`'s advertise-restart callback overflowed the 2048-byte timer-service stack on **8 of 19
disconnects**, each time with `rst:0xc` (§7.6). Every disconnect fired the timer at +3.00 s, so
the split was not "which disconnects reached the callback"; it was a stack sitting at its limit
and tipping over non-deterministically. The rate is DEBUG-influenced; the fragility is
structural.

*(The overflow itself is **fixed and benched at `HEAD`** (§7.6). That section also reattributes
the observation above: the 8/19 rate measured the pre-fix *console path*, not `HEAD`, where the
same callback is latent rather than manifest. The observation stays as measured, and the policy
below is unchanged — a `boot_id` change has causes beyond this callback.)*

**The container's consequence is not "it is fixed now" — it is "expect a reboot anyway".**
A container watching `boot_id` for the ADR-0004 recovery flow must tolerate a device restart
mid-session, so:

- a `boot_id` change requires a full discard and re-upload, and that is normal operation;
- a reboot during a macro silently ends the run, and the container's next `STATUS` after
  `HELLO` reports `mode=IDLE` with `last_stop_reason=NONE` — the run is simply gone;
- the bond survives, so no re-pairing is needed;
- nothing about this may be surfaced as an error, because it can happen to a working setup.

## 9.3 The container's policy

Derived from §9.1 and §9.2, and decided here:

### `MACRO` + console link drop → **stop the run**

The container sends `STOP` and surfaces `Stopped: console disconnected`.

The one real risk is a run **resuming mid-press into a freshly-connected console** — a pass
that continues at its current frame (ADR-0008) while the console re-runs its full init and
comes up in whatever screen it was in. A stray input into a live game is unrecoverable; a
stopped run is one `START` away. Given the risk is unverified and the cost of avoiding it is
one verb, the container stops.

- **The device sees a plain `CONTAINER_STOP`.** `CONSOLE_LOST` is a **container-side** reason
  and is not a protocol value — the device never learns why.
- **No auto-restart.** §9.1 shows the console re-initialises itself on wake with nothing
  re-sent, so the container has nothing to do but wait for the human — exactly as after a
  `BOOT_LOCAL` stop.

### `AMIIBO` + console link drop → **keep the placement, rotate on reconnect**

A placed tag nobody is reading is harmless. But a tag the console has **already scanned** must
not be the next one it sees (the freshness requirement, chapter 6). So on reconnect the
container mints a fresh identity and re-places **before the console's first scan** — the same
action it already takes on `SCAN_ENDED` (§6.5), so this is a policy, not new machinery.

### Console never connected → **allow `START` and `PLACE_AMIIBO`, warn**

The device does not need the console to run a macro. Refusing would be dishonest about a
dependency that does not exist, so the container allows it and — before the first byte is
uploaded — logs the one consequence worth stating: *the run's inputs go nowhere until the console
connects*. The UI says the console is not connected as well. Measured on the real container with
the console off, the run is allowed, the mode reads `MACRO` throughout, `currentFrame` advances,
and nothing else happens (`macro-timing-bench.md` §3).

### Device reboot mid-session → **discard and re-upload** (ADR-0004)

`boot_id` changes → plan, placement and UI state are cleared, and the container requires a
re-upload rather than guessing what survived. The device reports `mode=IDLE`, so there is
nothing ambiguous to reconcile — but the *reason* the run ended is not knowable from the
device, and the container must not invent one.

### Port busy → **do not retry through it**

Another process holding the device is a deployment error. Retrying would either fail forever
or, worse, fight the other holder for the port. The UI names the situation and the user's fix.

## 9.4 What stays unverified

These are **known gaps**, carried in §12.3, not silent assumptions:

| Gap | Why it is open |
| --- | --- |
| **G-5** — is the console content with a pass resumed mid-press after a link drop? | needs a working `MACRO` mode on the bench to present the condition |
| **G-4** — ~~is a macro run harmless against an absent console?~~ **Answered.** It is harmless and silent: the container allows the run and warns, the executor walks every record, **zero** notifications go out, the handoff is vacuous, and the mode returns `IDLE` with `CONTAINER_STOP` — nothing a console could be surprised by. §7.5 carries the numbers; `macro-timing-bench.md` §3 the observation | closed by [Bench: plan-executor timing, with and without a console (G-16, G-4)](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/35) |
| **G-6** — does per-scan freshness require an **observable** unplace through the PN7160 path? | the console never enters `.nfp`, so neither half can be checked |
| **G-6** — does freshness actually key on the UID on NS2? | proven on NS1 through emuiibo's random-UUID toggle; the NS2-through-PN7160 equivalence is an inference |
| **G-16** — ~~does the 5 ms link hold under macro load?~~ **Answered.** The link is not the constraint; the report period is, and it holds. The one condition that would reopen it is a macro whose holds are at or below the period. §7.5 carries the numbers; `macro-timing-bench.md` §2 the observation | closed by the same ticket |

The container's policy of §9.3 is chosen so that **none of the four gaps can produce a wrong
input on the console**: the risky half (a resumed mid-press pass) is stopped rather than
risked, and the freshness half is a superset (always emit the gap) rather than an assumption.
That is the point of deciding policy on unverified facts: pick the branch that is safe under
either answer. Two of the four were then closed by measurement — **G-4** and **G-16** — and the
table above says with which numbers.
