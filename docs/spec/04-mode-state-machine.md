# 4 · Mode state machine, transitions and panic stop

The "state machine" is a product of **five independent axes**, only one of which is a mode.
That is the whole design: most of what looks like state transitions is one axis moving while
the others stay still, and the modes cannot overlap because exclusivity is structural rather
than policed.

## 4.1 The five axes

| Axis | Values | Moved by |
| --- | --- | --- |
| **mode** | `IDLE` / `MACRO` / `AMIIBO` | container verbs, the BOOT button |
| **control link** | `UP` / `DOWN` | USB attach / detach — **inferred by the container**, never reported by the device (chapter 3, §3.3) |
| **console link** | `ADVERTISING` / `CONNECTED` | BLE events |
| **bonding** | `PAIRED` / `UNPAIRED` | firmware (ADR-0013); durable in NVS |
| **staging** | `NONE` / `PLAN` / `TAG` | bulk verbs, commit or abort |

**The staging axis has no `KEY` value**, though it had one when this model was first written.
Key material stopped being something the device could stage the moment sealing moved
container-side (ADR-0011, ADR-0012): there is no key verb, no key frame and no key bit in
`HELLO`, so a `KEY` staging state would be a state nothing can reach. The axis is
`NONE`/`PLAN`/`TAG` and that is the whole set.

**Only `mode` is a mode.** "Is the container connected?" is orthogonal to the mode, and the
protocol models it that way: `STATUS` reports console link and bond *alongside* mode. The
device-owns-timing design (ADR-0003) and the survives-either-link-death rule (ADR-0008) only
make sense if the two can disagree.

**`IDLE` is the absence of a mode**, not a third one. Exclusivity therefore costs no code:
there is a single field with a single value.

## 4.2 There are no transient modes

- **loading** is *staging*, deliberately outside the committed plan. Until commit `STATUS`
  reports `plan_state=NONE`, so there is no window in which "loading" is a mode. That
  atomicity is exactly why container and device can never disagree.
- **starting** is invisible: `START`'s ACK is sent only once the executor is armed, so
  device-owned timing begins at the ACK.
- **stopping** cannot exist, because of the release invariant (§4.6).

## 4.3 Transition table

Only the `mode` axis has transitions. "Always legal" verbs change no mode.

**Reading the Reply column.** A `REPLY` carries no payload unless the verb is `HELLO`,
`STATUS`, `ERROR` or a bulk ACK (§2.5–§2.7, §3.2). Where the table says
`ACK, last_stop_reason=CONTAINER_STOP` it is
describing **observable state**, not an ACK payload: the ACK is empty, and the caller reads the
reason off the next `STATUS` poll (at worst 500 ms later). The column is written that way
because what matters at a transition is that the reason *changed*, not how it is delivered.

| From | Event | To | Effect | Reply |
| --- | --- | --- | --- | --- |
| `IDLE` | `LOAD_PLAN` (transfer + check ok) | `IDLE` | plan committed | ACK |
| `IDLE` | `START`, plan committed | **`MACRO`** | arm at frame 0; neutral first; ACK once armed | ACK |
| `IDLE` | `START`, committed plan the device cannot replay | `IDLE` | — | `ERROR BAD_PLAN` |
| `IDLE` | `START`, no committed plan | `IDLE` | — | `ERROR NO_PLAN` |
| `IDLE` | `PLACE_AMIIBO` (transfer + commit ok) | **`AMIIBO`** | tag committed and placed; the NFC state byte starts answering console polling | ACK |
| `IDLE` | `STOP` | `IDLE` | — | ACK (idempotent) |
| `IDLE` | `UNPLACE_AMIIBO` | `IDLE` | — | ACK (idempotent) |
| `IDLE` | BOOT short | `IDLE` | — | — |
| `IDLE` | BOOT long | `IDLE` | plan→none, tag→none | `EVENT` if anything was loaded |
| **`MACRO`** | `START` | `MACRO` | — | `ERROR ALREADY_RUNNING` |
| **`MACRO`** | `STOP` | **`IDLE`** | neutral release; plan retained | ACK, `last_stop_reason=CONTAINER_STOP` |
| **`MACRO`** | `PLACE_AMIIBO` | `MACRO` | — (**the crux**, §4.4) | `ERROR BAD_STATE` |
| **`MACRO`** | `UNPLACE_AMIIBO` | `MACRO` | — | ACK (idempotent; no tag is placed) |
| **`MACRO`** | `LOAD_PLAN` | `MACRO` | — | `ERROR BAD_STATE` |
| **`MACRO`** | BOOT short | **`IDLE`** | neutral release; plan retained | `EVENT`, `BOOT_LOCAL` |
| **`MACRO`** | BOOT long | **`IDLE`** | neutral release; plan→none, tag→none | `EVENT`, `BOOT_LOCAL` |
| **`AMIIBO`** | `START` | `AMIIBO` | — | `ERROR BAD_STATE` |
| **`AMIIBO`** | `STOP` | **`IDLE`** | tag unplaced; neutral release; tag data retained | ACK, `CONTAINER_STOP` |
| **`AMIIBO`** | `UNPLACE_AMIIBO` | **`IDLE`** | tag unplaced; tag data retained | ACK |
| **`AMIIBO`** | `PLACE_AMIIBO` | `AMIIBO` | tag replaced atomically (old unplaced, new placed) | ACK |
| **`AMIIBO`** | `LOAD_PLAN` | `AMIIBO` | — | `ERROR BAD_STATE` |
| **`AMIIBO`** | BOOT short | **`IDLE`** | tag unplaced; neutral release; tag data retained | `EVENT`, `BOOT_LOCAL` |
| **`AMIIBO`** | BOOT long | **`IDLE`** | tag unplaced; neutral release; plan→none, tag→none | `EVENT`, `BOOT_LOCAL` |

**`PLACE_AMIIBO` is itself the tag's bulk verb.** It carries the 540 bytes on the same
chunked, windowed, atomic path as a plan, so there is no load-without-place verb and no
`NO_TAG` state error. A failed transfer is `BAD_LENGTH` or a CRC rejection; a missing key is
refused by the **container** before anything reaches the wire (§8.6).

**`START` can still refuse after the commit check passed (added by #24).** `plan_state` is
committed, so the wire says a plan is there — but "is it committed?" and "can this device walk
it?" are different questions, and the second is the executor's. The case is a plan that is
structurally valid yet unrunnable, reachable only by a hand-built `record_count = 0` transfer:
§5.3's check accepts `len == 12` and §5.5's compiler never emits one. The arm refuses it with
`BAD_PLAN` **before the mode moves**, so the container never sees a `MACRO` whose walk is not
running. That is §2.3's "a rejection leaves state untouched" applied to the one callback that
can say no.

**Always legal, no mode change:**

| Verb | Applied |
| --- | --- |
| `HELLO` | immediately — the one tolerantly parsed frame |
| `STATUS` | immediately |
| `CONFIG` | next loop boundary in `MACRO`; immediately in `IDLE`/`AMIIBO` (§2.9) |
| `PAIR_UNPAIR` | accepted in every mode; its effect is on the console link, which is orthogonal. The unpair may drop that link, and the drop changes no mode |
| `ERROR` | device→container, always |

**Link and power events, no mode change:** control link up/down, console link
connect/disconnect/subscribe, and a planned or unplanned power cycle. On a console
**re-subscribe** the executor re-arms neutral and the pass **continues at its current frame**
(§4.7). An in-flight staging transfer aborts on control-link loss — that is the RX-ring
reset, and it is the device's whole recovery story (§2.7).

**`gap.c`'s current behaviour matches this by accident and must be preserved deliberately.**
`main/src/gap.c:179-183` resets the HID report and restarts the report task on a `0x000e`
subscribe; that is the neutral re-arm this chapter requires. It must not be turned into a
frame-0 restart.

## 4.4 The mechanism: no verb implicitly changes mode

`PLACE_AMIIBO` while a macro loops is the test case, and the answer is **reject with
`ERROR BAD_STATE`; the macro keeps running** — not queue, not implicit stop.

The rule generalises, and it is worth more than the individual answer: **a mode change is
always caused by an explicit verb the container sent, or by a human at the BOOT button**
(ADR-0007). Reasons:

- **Implicit stop makes a 2 Hz poll lie.** With one outstanding control request and no request ids,
  the container can never distinguish "my `PLACE_AMIIBO` succeeded" from "my `PLACE_AMIIBO`
  silently killed the run I am watching". Events are edge-only.
- **A verb must not have a hidden second effect.** `START` is not retransmitted because a
  retried `START` is a second start; the same reasoning says a stray `PLACE_AMIIBO` must not
  be a hidden `STOP`.
- **The container owns the mode.** Sequencing `STOP` then `PLACE_AMIIBO` is one extra round
  trip over an idle link, in exchange for a machine that is a pure function of
  `(mode, verb)` and therefore testable.
- **Nothing is queued, ever.** A deferred request that fires at an unknown later loop
  boundary is a surprise with a memory, which is exactly what statelessness dislikes.

`LOAD_PLAN` while `MACRO` rejects for the same reason plus a sharper one: if a commit could
land mid-replay, "atomic" would stop meaning anything and neither side could say which bytes
were running.

The asymmetry with `PLACE_AMIIBO` is deliberate: a tag is `AMIIBO`'s **own payload**, so
replacing it happens *within* the mode, whereas a plan committed during `AMIIBO` would be a
landmine — it would come alive at the next `START` with no visible connection to the
transfer that planted it.

**Illegal-but-harmless verbs stay idempotent successes**: `STOP` in `IDLE`,
`UNPLACE_AMIIBO` with no tag placed. They reduce activity, so accepting them is safe, while
verbs that *start* or *switch* activity are the ones that reject.

## 4.5 Panic stop

The BOOT button is the only halt that does not come from the container. It is two-tier, and
the tiers differ in **how much the press forgets**:

| | Short press (< 1.5 s) | Long press (≥ 1.5 s) |
| --- | --- | --- |
| mode | → `IDLE` | → `IDLE` |
| inputs | neutral release | neutral release |
| tag | unplaced | unplaced |
| committed plan | **retained** | **discarded** (`plan_state=NONE`) |
| loaded tag bytes | retained | discarded |
| console bond | kept | **kept** (never unpair) |
| `last_stop_reason` | `BOOT_LOCAL` | `BOOT_LOCAL` (same value; distinguished by plan/tag state) |
| restart | one `START` re-arms | needs a fresh `LOAD_PLAN` |

**Timings and mechanics:**

- **≤ 30 ms debounce, short `< 1.5 s`, long `≥ 1.5 s`.** Poll `gpio_get_level(GPIO0)` in a
  100 Hz task; 10 ms resolution is ample and no ISR is needed. GPIO0 is the BOOT strap pin,
  but the strap is sampled by the ROM bootloader at reset and the application only reacts to
  **edges**, so a level already low at boot fires nothing.
- **The stop fires on the press edge; the forget fires on release.** This is load-bearing,
  not cosmetic: the CH9102 wires **DTR→GPIO0, RTS→EN**, so a container that asserts DTR looks
  electrically like a BOOT button that never gets released. Edge-triggered stop means such a
  mistake costs one harmless stop; release-gated escalation means it can never silently
  discard the plan.
- The panic stop is **read in every mode**, with the control link up or down, and needs no
  ACK. In `IDLE` a short press is a no-op; the long press still clears.
- It is observable through the `STATUS` poll at worst — **no new verb, no new frame type**.

**"Return to `IDLE`" guarantees, precisely:** all inputs released (§4.6), both sticks
centred, the tag unplaced, playback **not** resumed, and an `EVENT` on the mode edge. The
plan is discarded only by the long press.

## 4.6 The release invariant

> **Neutral is the last write of a mode. There is no state in which a mode has ended and
> inputs are still held.**

The device owns this completely, independently of the plan. A fixed neutral report is
emitted from a **compiled-in template, never derived from plan bytes**, on *every* exit
path: `START` ("neutral first", §4.3), the loop boundary, `STOP`, the BOOT stop, a mode
change, the console re-subscribe re-arm, and the executor fault path (malformed plan, frame
index out of range, commit check mismatch). It must be **committed and transmitted**, not
merely written into the back buffer (`controller_hid_commit`,
`main/src/controller/hid_controller.c:233`).

**`UNPLACE_AMIIBO` is the one mode change that emits no release (settled by #25).** That is
§4.3's table rather than an omission: `AMIIBO` holds no inputs — the executor is stopped for
as long as a tag is placed — so the last write into the mode is already the neutral, and the
invariant above ("no state in which a mode has ended and inputs are still held") is satisfied
without another report. Emitting one would add a second effect to a verb that §4.3 defines as
a tag operation, which is the thing §4.4 exists to prevent. #24 raised the tension and left it
here; this is the answer, and it changes no code.

The template is `pro2_neutral_state` (`main/src/controller/hid_controller_pro2.c`), the nine
state bytes in the report's own layout: it zeroes the button bytes, centres both sticks at
`PRO2_STICK_CENTER` (`0x800`), and leaves byte `0x0C` at `0x00`. `pro2_report_init` initialises
the report from the same nine bytes, so the neutral the executor emits and the neutral a fresh
controller carries are one definition rather than two that agree today. That makes the guarantee
**total and stronger than the reference player's**: `web_ui.py::_release_all` enumerates 18
named buttons and predates Pro2's `GR`/`GL`/`C`, whereas a zeroed Pro2 button field cannot miss
one.

Logically the transition is atomic (`mode=IDLE` the moment `STOP` is accepted or the BOOT
edge is read); physically it completes within one report interval.

**The handoff, and why "no inter-loop gap" survives it (amended by #24).** The reporter's
double buffer carries a single `swap_request` bit (`hid_controller.c`), so writing neutral
and then the loop's first record back to back loses the neutral: the reporter swaps once and
only the record reaches the wire. The executor therefore has an explicit handoff step. At the
loop boundary it commits the neutral, then waits for `swap_request` to clear — the reporter
has taken that commit — or for a 50 ms bound, and only then writes record 0. The bound exists
so an absent console, whose report task never reaches the swap, cannot stall a replay; §4.6's
"committed and transmitted" is what makes the wait necessary, and the bound is what makes it
safe. **Measured, with a console subscribed, the wait is one report period — and with nothing
subscribed it is zero** (the same plans then loop at `loop_ms` exactly, min = mean = max), so the
50 ms bound is a bound and not a cost. §7.5 carries the number; #35 measured it
(`macro-timing-bench.md` §2–§3).

**This is the transmission cost of the neutral, not a scheduled gap.** §5.4's "no inter-loop
gap" names what the wait is not: there is no dwell, no sleep, and no frame the design holds
for its own sake. The loop's clock is **free-running from the arm**: the first loop starts at
the moment record 0 is applied (the ACK'd start, §4.3), and every later boundary's deadline is
the previous one plus exactly `loop_ms` — so `sum(hold_ms) == loop_ms` still holds exactly of
the loop's own timeline, and tick quantisation moves *when a boundary is observed* but never
*how long a loop lasts*.

**Anchoring each loop to the moment its record 0 lands would be the drift §7.5 warns about.**
That is the tempting reading of "the clock starts when record 0 is applied", and it is wrong:
it re-anchors to the 10 ms tick grid every loop, so a 25 ms loop becomes 30 ms and 600 ms of
clock yields 20 boundaries instead of 24. "Starts when record 0 is applied" is about the
**first** loop — the one begun by an ACK'd `START` — not about every restart.

**This is the confirmation chapter 5 depends on: the compiler must not inject trailing
neutral frames.** A release that lives in the data can be omitted by a malformed plan, and
neutral is the executor's fixed step instead.

## 4.7 Link loss

**Control link drops mid-macro: the replay keeps running** (ADR-0008). Nothing in the device
watches the container. Link loss is **not** a mode change: an `EVENT` on the edge, mode
untouched, and a mid-upload staging buffer dies by construction.

On re-attach the container leads with `HELLO`:

- **same `boot_id`** → resync from `STATUS` and **must not assume the device stopped** —
  `mode`, `current_frame`, `loop_count` and `plan_hash` are all there;
- **new `boot_id`** → discard and re-upload (ADR-0004).

**Console link drops mid-macro: also keeps running**, for the same reason — the device cannot
tell "the console is asleep" from "the console is slow", so inventing a difference creates a
state it cannot observe. On a console **re-subscribe** the pass **continues at its current
frame**; restarting would silently produce a shorter loop and make `current_frame` appear to
jump backward, indistinguishable from a bug.

**Accepted risk, recorded deliberately:** a macro can keep looping after the driver has died,
and only a person at the board will notice. That is the price of ADR-0003, and the mitigation
is the BOOT panic stop, not a watchdog.

**Settled, and it agrees with the policy:** whether the console is content with a pass resumed
mid-press. The bench presented the condition — a run holding A, dropped while the press was on
the wire — and the container's stop ended it before the console returned; the device's last write
was the neutral and nothing resumed (§9.4, `link-drop-bench.md` §3). Stopping on a console link
drop (chapter 9) is what keeps the question from having to be answered, and it held.

## 4.8 Boot state

After power-up, before any container: `mode=IDLE`, `plan_state=NONE`, `tag_state=NONE`,
console link `ADVERTISING`, bonding as NVS says, and a fresh `boot_id`. **Nothing resumes** —
not a macro, not a placed tag. That is the container-re-uploads contract, and it is the
*new-power* case of §2.8.

## 4.9 The NFC state byte is the mode's physical expression

Not protocol detail for its own sake — it is the *mechanism* behind exclusivity.

`hid_report_pro2_t` byte `0x0C` (`main/include/controller/hid_controller_pro2.h`, the
`nfc_state` field) is the **NFC state**, `0x00` = idle, as documented in
`switch2_controller_research/hid_reports.md:178`. `0x00` is the whole of `IDLE` and `MACRO`, the
§6.5 gap and a post-eject read (§6.6): it means **no tag in the reader's field**. While a tag is
in that field the byte is the **reader's event counter**, `0x01`–`0x07`, advanced once on each of
the reader's five events — tag presented, scan ready (`0x03`), operation ready (`0x06`), write
complete (`0x08`), tag removed — and wrapping `0x07 → 0x01` rather than through the reserved
`0x00`. The tag-removed event advances the counter *and* leaves the byte at `0x00`, because it is
the event that empties the field (`nfc_tag.c` → `controller_ops_t.set_nfc_state`, into both
report buffers). **That is what makes
it the mode's physical expression** and why `AMIIBO` is the only mode that drives it — a console
that polls in `IDLE` or `MACRO` moves `STATUS.console_polling` and must not touch the byte.

**Amended by #48, and why.** The byte was placement-only — `0x00`/`0x02`, written only by a
placement — until G-18's ledger isolated the shape that kills the console's amiibo module: a
byte **held** at one value across a whole armed read, together with a `0x05` answer that always
says `04` (`register-screen-bench.md` §7). The second implementation does both halves the other
way — it answers that `04` as a *level* **and** drives this byte as an event counter,
`(previous + 1) & 0x07`, on exactly those five events
(`ns_pc_control/server/src/virtual_controller.cpp:195-266`, context tier) — and reports no crash.
With the read's completion carried on the `0x05` answer (§6.6), the input report's byte is the
only remaining channel the console can read a *change* from, so it carries the reader's sequence:
`hid_reports.md:178`'s `0x00`–`0x07` range read as a *sequence* rather than a vocabulary. The one
difference from the reference is the wrap — `0x07 → 0x01`, so `0x00` is never on the wire while
the reader has a tag in the field and the exclusivity argument below survives the amendment
intact (ADR-0016).

**The byte and `console_polling` are one vocabulary and two signals (settled by #25).** Both use
`IDLE`/`POLLING`/`TAG_DETECTED` (§3.2), but they are read from different sides:

- The **byte** says what the *device* is doing — `0x00` only while the reader's field is empty
  (no placement, the §6.5 gap, or a post-eject read), and the reader's event counter while a tag
  is in the field. `0x00` therefore cannot be mistaken for `IDLE`: the placement's own first
  advance moves the byte off it, and the wrap skips it.
- **`console_polling`** says what the *console* is doing — `0x03` sets `POLLING`,
  `0x05` asks again, `0x04` returns it to `IDLE`. A game can open its amiibo menu while the device
  is in `IDLE` or `MACRO`, so it cannot be gated on the mode, and §3.2's rotation key reads it.

Collapsing them would either lie about the mode or lose the container's `SCAN_ENDED`/rotation edge.

**A console re-subscribe re-asserts the byte.** `gap.c`'s `0x000e` subscribe handler calls
`hid_reset`, which re-initialises both report buffers and so resets `nfc_state` to `0x00`; the
`RESUBSCRIBED` edge therefore writes the server's current byte back (`control_parser.c`). Without
that, a tag placed across a sleep/wake would vanish from the wire until the next placement — the
same edge §4.7 already re-arms the neutral on.

Consequences worth writing down:

- **The two modes cannot overlap at the hardware level** — there is one byte to express them,
  and it is in the report the console samples every ~15 ms. "Only one at a time" is a property
  of the wire rather than a policy the device enforces.
- It fixes the console-observability of a mode change at within one report interval.
- The field's name is `nfc_state` rather than `unknown_0x0c` as of #25. Its *values* are the
  reader's counter (§4.9); the earlier NFC research's `0x01` polling / `0x02` tag-detected reading
  of `switch2_controller_research/hid_reports.md:178` is `STATUS.console_polling`'s vocabulary
  (§3.2), which is the console's own level rather than the device's activity.
- **The byte's *change* is what the reader carries** (amended by #48, ADR-0016). It is the event
  counter of the paragraph above — tag-presented, scan-ready, operation-ready, write-complete,
  tag-removed — and never rests on one value across a read window, which is exactly the factor
  G-18's ledger isolates as the crash shape when it is held. Nothing is read *from* the counter's
  value: `0x00` is the only value with a meaning (no tag in the field).
