# 3 · `STATUS` and `EVENT` reference

Numbers come from a **2 Hz poll**; edges come from **events**. There is no periodic push
channel and no per-frame stream — that is what ADR-0003 buys and what keeps the device from
becoming a clock.

## 3.1 One rule, stated once

> **`STATUS` is the truth. `EVENT` is a hint that the truth changed.**

The container never derives state from an event alone. Every event is a prompt to poll
`STATUS` (or to act on a one-way fact, like "the scan ended"). This is what makes a lost
event survivable, and it is why events carry almost no payload.

## 3.2 `STATUS` fields

Little-endian, packed, no padding. The payload is **exactly 47 bytes** — a fixed length, not a
variable one — and the offsets below are part of the protocol. `len` covers the whole
payload; a container that sees anything other than 47 bytes must surface a protocol error,
never pad.

| Offset | Width | Field | Values |
| --- | --- | --- | --- |
| 0 | u8 | `console_link` | `ADVERTISING` (0) / `CONNECTED` (1) — this is what an earlier resolution called "connect/pair state", and it is the **console** link, never the control link (§3.3) |
| 1 | u8 | `bond` | `UNPAIRED` (0) / `PAIRED` (1) |
| 2 | u8 | `mode` | `IDLE` (0) / `MACRO` (1) / `AMIIBO` (2) |
| 3 | u8 | `plan_state` | `NONE` (0) / `COMMITTED` (1) |
| 4 | 16 B | `plan_hash` | the hash the device was given at commit; zero when `plan_state = NONE` |
| 20 | u16 | `plan_frame_count` | `record_count` of the committed plan; 0 when none |
| 22 | u16 | `current_frame` | 0-based index of the frame being replayed; 0 when not in `MACRO` |
| 24 | u32 | `loop_count` | completed loops since `START` |
| 28 | u8 | `tag_state` | `NONE` (0) / `PLACED` (1) |
| 29 | 7 B | `tag_identity` | the identity of the placed tag; zero when `tag_state = NONE` |
| 36 | u8 | `console_polling` | `IDLE` (0) / `POLLING` (1) / `TAG_DETECTED` (2) |
| 37 | u8 | `last_error.code` | a code from §2.5, or `NONE` (0); cleared by the next successful verb |
| 38 | u32 | `last_error.detail` | the per-code meaning of §2.5; 0 when `last_error.code = NONE` |
| 42 | u8 | `last_stop_reason` | `NONE` (0) / `CONTAINER_STOP` (1) / `BOOT_LOCAL` (2) |
| 43 | u32 | `uptime_ms` | since boot; **not** a host-visible date |

**`last_error` is always five bytes** — `code` then `detail` — because a variable-length
field in the frame the container parses twice a second would buy nothing and would
reintroduce the length question this section already answers. The pair carries the same
per-code meaning as an `ERROR` reply (§2.5), so a stop reason and an error code are read the
same way from `STATUS` as from the wire.

### Notes that are decisions

- **`current_frame` and `loop_count` are the container's progress view.** With a 2 Hz poll
  this gives a ~500 ms-granular progress display and no more; a smooth progress bar is not
  a thing this design can produce, and the UI must not fake one.
- **`plan_state` is separate from `plan_hash`** so "no plan" is not expressed by a zero
  hash. Zero is a legal hash prefix.
- **`last_error` is cleared by the next successful verb**, not by a read. A user who fixes
  the macro and starts it successfully should stop seeing the old error, and no extra verb
  is needed to achieve that. It is a **five-byte pair** — `code` u8 then `detail` u32, with
  `detail` typed by the code (§2.5) — so the field is a fixed-width part of the 47-byte
  payload rather than a variable-length tail.
- **`console_polling` is what the container keys rotation on.** When it falls from `POLLING`
  or `TAG_DETECTED` to `IDLE`, the console has stopped asking; that is the moment to push
  the next identity (chapter 6, §6.5).
- **`uptime_ms` is not a clock.** It is for "has this thing been up since I last looked",
  which `boot_id` answers better. Nothing may display it as a time of day.

**One field this spec adds to the earlier list.** An earlier resolution enumerated
`STATUS` as connect/pair, mode, plan hash, frame count, current frame, loop count, last
error, stop reason and uptime. The amiibo design needs placement state — the container
cannot rotate an identity it cannot see is placed — so `tag_state`, `tag_identity` and
`console_polling` are added here. They are named as an extension rather than smuggled in.

## 3.3 `EVENT`

An `EVENT` frame carries one `kind` byte and an optional small payload. The set is closed
and short.

**The event frame.** An `EVENT` is the event frame type of §2.2, so its `verb` is 0 (§2.4) and
its payload begins with one `kind` byte. The kinds are numbered **1–10 in the order of the
table below**, and the event's own payload follows the `kind` byte:

| Kind | # | Fires when | Payload after `kind` |
| --- | --- | --- | --- |
| `MODE_CHANGED` | 1 | the mode field changes, for any reason | `mode` u8 |
| `PLAN_COMMITTED` | 2 | a `LOAD_PLAN` commits | `plan_hash` 16 B |
| `PLAN_DISCARDED` | 3 | a long panic stop, or a `PLACE_AMIIBO` transition, drops the plan | — |
| `LOOP_COMPLETED` | 4 | a loop boundary is crossed (rate-limited, below) | `loop_count` u32 |
| `TAG_PLACED` | 5 | a `PLACE_AMIIBO` commits and the tag starts answering | `tag_identity` 7 B |
| `TAG_UNPLACED` | 6 | a tag stops answering, including the atomic-replace gap | — |
| `SCAN_ENDED` | 7 | the console stops polling a placed tag | — |
| `ERROR_RAISED` | 8 | an `ERROR` reply was sent for a reason other than the container's last request | `code` u8 · `detail` u32 (§2.5) |
| `CONSOLE_LINK` | 9 | the console link connects, disconnects, or re-subscribes | `which` u8 (0 `DISCONNECTED`, 1 `CONNECTED`, 2 `RESUBSCRIBED`) · `reason` u16 (0 unless `which = DISCONNECTED`) |
| `BOOT` | 10 | the device has finished booting and is ready for `HELLO` | `boot_id` u32 |

**`CONSOLE_LINK`'s `reason` is a `u16`, not a `u8`, and that is measured rather than
chosen.** The only disconnect this hardware produces is 531 = `BLE_HS_ERR_HCI_BASE (0x200) +
0x13` (`ns2-console-lifecycle.md` §5.1), which does not fit a byte; the host-level reason space
starts at 0x200, so the field must be 16 bits or the one value that matters is unrepresentable.

### Two corrections to earlier thinking, recorded here

- **The device cannot report its own control link.** An earlier resolution listed control
  link up/down among device-known events. It cannot be: a report about the control link can
  only travel over the control link. **The container infers control-link state** from the
  port: open, readable, and answering. There is no `CONTROL_LINK` event kind and no
  `control_link` field in `STATUS` — a `STATUS` reply *is* the proof the link is up.
- **`HELLO` may be unnecessary when a `BOOT` event arrives on an open port.** The container
  still sends it, because the port being open does not imply the device rebooted (it may
  have been rebooted before the container attached). `BOOT` exists so an *in-session* reboot
  is noticed in milliseconds rather than at the next 2 Hz poll.
- **There is no `plan_committed` field, and none is needed.** An earlier resolution listed a
  committed-or-staging flag alongside `plan_hash`. `plan_state` is here (it must be: zero is a
  legal hash prefix, so a hash cannot express "none"), but a second boolean saying the same
  thing would be a field that can disagree with itself. §3.1's rule covers the rest: the poll
  that reports a non-zero `plan_hash` is the evidence a commit landed, and `PLAN_COMMITTED` is
  a prompt to look rather than a fact to trust.

### Rate limiting, and why it is not optional

A macro may be short enough to complete many loops per second, and a linked-list of
`LOOP_COMPLETED` events would then saturate the control link — the very thing ADR-0003
exists to avoid. Therefore:

> **`LOOP_COMPLETED` is emitted at most at the `STATUS` poll rate.** A device cannot know the
> container's poll rate, so the rule is stated in the one thing it can observe: **the device
> emits `LOOP_COMPLETED` only if `loop_count` has advanced since the last `STATUS` reply it
> served.** If a loop boundary is crossed while the previous `LOOP_COMPLETED` has not been
> superseded by a poll, the device suppresses the event. `loop_count` in `STATUS` remains
> exact.

Every other event kind marks a genuine, sparse edge and needs no limiting.

## 3.4 Reading a stop

`last_stop_reason` exists so that a panic stop at the board is observable without a new
verb and without an ACK. `BOOT_LOCAL` means **stopped by a person at the board** — it is a
distinct reason, never an error, and the container must **not** auto-restart in either of
its cases. `last_error` disambiguates "by hand" from "by hand, and broken":

| `last_stop_reason` | `last_error` | What the UI says |
| --- | --- | --- |
| `CONTAINER_STOP` | `NONE` | stopped in the app |
| `BOOT_LOCAL` | `NONE` | stopped by hand at the board |
| `BOOT_LOCAL` | set | stopped by hand, and something is broken |

`NONE` is the third value: a device that has booted and has not yet stopped. It is not
"unknown" and must not be rendered as one.

**On the name.** `BOOT_LOCAL` reads as "the device rebooted locally", and it does not mean
that: the value travels on this link, so by definition the device did **not** reboot to produce
it. It means *stopped by a person at the board*, and the name is kept rather than improved
because it is already fixed in two resolved decisions and in the UI copy the prototype was
scored against; renaming it here would put the spec and the record in disagreement for a
cosmetic gain. The UI must therefore carry the meaning in its wording ("stopped by hand at the
board") and never show the identifier to a user.

## 3.5 What the container does with all this

- Holds the last `STATUS` in memory and pushes it to the browser; the browser never polls
  the device (chapter 8).
- On `MODE_CHANGED`, re-reads `STATUS` and reconciles — never assumes the change was the
  one it asked for.
- On `CONSOLE_LINK`, applies the console-lifecycle policy of chapter 9.
- On `SCAN_ENDED`, mints the next identity and pushes it (chapter 6).
- On `BOOT`, discards the plan and the placement and requires re-upload (ADR-0004).
