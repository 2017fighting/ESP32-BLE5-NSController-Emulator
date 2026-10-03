# 11 · Worked traces

Three end-to-end sequences, written to check that the chapters agree with each other. A
spec is a set of claims; a trace is where the claims have to meet. Where a step had no
answer, the chapter was wrong and was fixed rather than the trace bent.

Each trace is *normative*: it is what the two sides do, in order, with the chapter that
governs each step.

## Trace A — attach a cold container, run a macro, stop it

State going in: board freshly booted, container just started, no plan, console connected.

| # | Container | Device | Governed by |
| --- | --- | --- | --- |
| 1 | opens the port with DTR/RTS deasserted | — | §8.3, §10.5 |
| 2 | — | resets the RX ring on re-attach, aborts any staging | §2.7 |
| 3 | sends `HELLO(proto_ver)` | replies capabilities, including a fresh `boot_id` | §2.6, §2.8 |
| 4 | sees a `boot_id` it has never seen → **new power**: clears plan, placement and UI state | — | §2.8, ADR-0004 |
| 5 | starts the 2 Hz `STATUS` poll | replies `mode=IDLE, plan_state=NONE, tag_state=NONE, bond=PAIRED` | chapter 3 |
| 6 | reads a macro, compiles it, gets `(bytes, hash)`, checks it against `plan_capacity_bytes` | — | chapter 5, §8.4 |
| 7 | announces `LOAD_PLAN(total_len, hash)` | allocates exactly `total_len` | §2.7 |
| 8 | sends 256 B chunks; device ACKs the next expected offset about every 4 KB | ACKs offsets | §2.7 |
| 9 | sends the commit frame | validates magic, version, record size, and `payload_len == 12 + 11·record_count` → commits, emits `PLAN_COMMITTED` | §2.7, §5.3 |
| 10 | polls; sees `plan_state=COMMITTED, plan_hash=<the hash it sent>` | — | §5.6 |
| 11 | sends `START` | plan is committed → arms the executor at frame 0, emits neutral first, ACKs once armed, enters `MACRO`, emits `MODE_CHANGED` | §4.3, §4.6 |
| 12 | re-reads `STATUS` on the mode event rather than assuming the `START` was what moved it | — | §3.1, §3.5 |
| 13 | shows frame progress from `current_frame`/`loop_count` at ~500 ms granularity | replays frames; emits at most one `LOOP_COMPLETED` per poll interval | §3.3, ADR-0003 |
| 14 | sends `STOP` | emits neutral (committed and transmitted), enters `IDLE`, retains the plan, and ACKs — **empty payload**; the reason is read off the next poll | §4.3, §4.6 |
| 15 | polls, sees `last_stop_reason=CONTAINER_STOP`, and shows `Stopped in the app`; the plan is still there, so `START` re-arms in one verb | — | §3.4, §4.3 |

**What this trace caught.** Step 13 needed the `LOOP_COMPLETED` rate limit to exist at all —
without it a short macro can generate a monotonic event stream and saturate the link, which is
exactly the failure ADR-0003 exists to prevent. The limit is in §3.3 because of this trace.

## Trace B — two scans, one rotation

State going in: `IDLE`, console connected, key `KEY_OK`, a figure selected.

| # | Container | Device | Console | Governed by |
| --- | --- | --- | --- | --- |
| 1 | seals `figure + identity₁` → 540 B tag (identity₁ is fresh CSPRNG) | — | | §6.3, §6.4 |
| 2 | `PLACE_AMIIBO(540 B)` over the chunked path | commits, drives the NFC state byte, enters `AMIIBO`, emits `TAG_PLACED` | | §2.7, §4.3, §4.9 |
| 3 | | | probes `0x01/0x0C`, starts polling `0x01/0x03`, reads `0x01/0x05` + `0x01/0x15` | §6.5 |
| 4 | polls; sees `tag_state=PLACED, console_polling=POLLING`, then `TAG_DETECTED` | answers the console from the RAM tag | | chapter 3, §6.6 |
| 5 | | | stops polling `0x01/0x04` | |
| 6 | | emits `SCAN_ENDED` (console stopped polling) | | §3.3, §6.5 |
| 7 | seals the **same figure** under a fresh identity₂, and pushes it | | | §6.3, §6.4 |
| 8 | waits for the console's next poll rather than oscillating `UNPLACE`/`PLACE` | on commit: **atomic replace** — the old tag is unplaced (NFC byte → `0x00`, `0x01/0x05` reports no tag), then the new one answers; emits `TAG_UNPLACED` then `TAG_PLACED` | | §4.3, §6.5 |
| 9 | | | scans again, sees a **different UID** for the same figure | §6.1 |

**What this trace caught.** Step 8 is where two chapters could have disagreed: the transition
table's atomic replace and the amiibo chapter's mandatory gap. The table now says the replace
is atomic *and* the gap is emitted, and `AMIIBO` is defined as *presents one tag at a time*
rather than *always has a tag placed* — so "the mode never oscillates but the tag-absent
state is visible" is consistent instead of contradictory.

**What this trace deliberately does not claim.** That the console *needs* step 8's gap. The
gap is a design guarantee chosen because something must leave the field for repeated scanning
to work on NS1 (G-6). Step 9's freshness is also an inference from NS1, not a measurement on
NS2 (G-6).

## Trace C — the console goes away mid-macro, and the device reboots

State going in: `MACRO` running, console connected, plan committed.

| # | Container | Device | Console | Governed by |
| --- | --- | --- | --- | --- |
| 1 | | sees the disconnect, `reason=531` (console-initiated) | goes to sleep | §9.1 |
| 2 | | emits `CONSOLE_LINK(disconnected)` | | §3.3 |
| 3 | **keeps replaying** — nothing in the device watches the console | loops on | | ADR-0008, §4.7 |
| 4 | decides to stop: sends `STOP` | neutral release, `IDLE`, plan retained, `last_stop_reason=CONTAINER_STOP` | | §9.3, §4.3 |
| 5 | shows `Stopped: console disconnected` as a **warning**, not an error, and does **not** restart | | | §9.3, §8.9 |
| 6 | | the device reboots for a reason this trace does not model — power, watchdog, brownout; §7.6's overflow used to be the routine one | | §9.2, §7.6 |
| 7 | sees the port drop and re-attach; sends `HELLO`; reads a **new `boot_id`** | boots to `mode=IDLE, plan_state=NONE, tag_state=NONE`, bond intact | | §2.8, ADR-0004 |
| 8 | discards plan and UI state, requires a re-upload, and **does not** report the lost run as an error | — | | §9.2, §8.9 |
| 9 | | | wakes, reconnects, re-runs its full init, re-subscribes HID itself | §9.1 |
| 10 | | starts the report task on the `0x000e` subscribe (fresh write or bond restore) | gets neutral reports; nothing is re-sent | §4.3, §9.1 |
| 11 | leaves the macro stopped and waits for the human | | | §9.3 |

**What this trace caught, and it is the most important one.** Steps 3–5 and 6–8 look
contradictory until they are ordered: the device keeps running after the console leaves
(ADR-0008), the *container* chooses to stop it (§9.3), and then the device reboots for an
unrelated reason (§9.2). Three separate mechanisms, and only the middle one is a policy. This
is why chapter 9 states that a `boot_id` change is **normal operation** and why §2.8 forbids
treating it as exceptional — a recovery story that assumed a reboot only follows a power
cycle would break whenever a reboot landed mid-run, which on the pre-fix firmware (§7.6) was
roughly half of all sleep/wake cycles on this hardware.

**Step 6's cause changed after this trace was written.** It named the §7.6 overflow; that
overflow is fixed, so the step now stands for a reboot from any source. The recovery path the
trace exercises is unchanged, which is the point: `boot_id` never says *why*.

It also settles a wording problem: the run ended twice over (stopped by the container, then
erased by a reboot). The container reports the first reason, because it is the one it knows,
and never guesses at the second — the device has no field for "I rebooted during a macro" and
must not be given one, since `last_stop_reason=NONE` after a reboot is honest.

## What the traces establish

- Every verb in the ten-verb surface is exercised by at least one trace, and every `ERROR`
  code is reachable from one of them.
- Every one of the five axes of §4.1 moves in at least one trace, and the mode moves only on
  an explicit verb or a BOOT press in all three — no trace needs an implicit transition.
- Every `EVENT` kind fires in at least one trace, and none is required for correctness: the
  container re-reads `STATUS` at each one (§3.1).
- The three traces together need no protocol feature the chapters do not define, and no
  chapter defines a feature the traces do not use.
