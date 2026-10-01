# 5 · Macro ingestion, compilation and the plan binary format

The container turns the canonical JSON macro into a **self-describing, event-preserving
plan**: a 12-byte header plus one 11-byte record per *distinct controller state*, each
carrying a `hold_ms`. The device replays it against the HID tick's wall clock and owns the
loop-boundary neutral.

It is not a fixed timestep (ADR-0009). The reference player is an event scheduler; an
event-preserving plan reproduces it exactly, and the whole real library compiles to
**793–3,356 B** against a 64 KiB capacity rather than the ~140 KB a 100 Hz grid would cost.

## 5.1 Ingestion

**Accepted input** is the canonical macro format: a JSON array of `{t, ev}`, where `t` is a
float in milliseconds and `ev.type` is exactly:

- `button` — `name` (string), `pressed` (bool);
- `stick` — `stick` (`"left"` | `"right"`), `h` and `v` (floats in [-1, 1]).

**Any unrecognised event, name, side, value shape or out-of-range value rejects the whole
macro** with a precise error. The container never silently drops or rewrites a single event:
a dropped press changes replay semantics invisibly, and the format is meant to be a faithful
record of what the author wrote.

**Button name → Pro2 register bit** (`btn_bits_pro2_t` order,
`main/include/controller/hid_controller_pro2.h:17-78`):

| Byte | Bits (LSB→MSB) |
| --- | --- |
| 0 | `b` `a` `y` `x` `r` `zr` `plus` `r_stick` |
| 1 | `down` `right` `left` `up` `l` `zl` `minus` `l_stick` |
| 2 | `home` `capture` `gr` `gl` `c` (bits 5–7 reserved) |

The reference player's release helper knows 18 names; `gr`/`gl`/`c` exist in the register
and are **accepted** if a future recording emits them, and are released by the neutral
template regardless (§4.6).

## 5.2 Compilation

The only resampling is the reference player's own, replicated exactly. Starting from the
first event:

1. `t0 = t_first`; `D = round(t_last - t_first)`.
2. Walk events in order, maintaining a full controller state (3 button bytes + four 12-bit
   stick axes, neutral by default).
3. **Stick keep-filter:** a `stick` event is applied only if `t - last_kept_t[side] >= 10`.
   The counter updates **only on keep**. This is the reference's per-side `<10 ms` rule.
   **Buttons are never filtered.**
4. A kept event that changes the full state becomes a record; a state identical to its
   predecessor is folded into the predecessor's hold (**no-op suppression**).
5. Two changes at the same rounded millisecond merge into the later state, so no middle
   record holds 0 ms.
6. `hold_ms[i] = start[i+1] - start[i]`; the last record's hold runs to `D` and may be 0.
7. **Stick axis:** `u12 = clamp(round(2048 + clamp(v,-1,1)·2048), 0, 4095)`; a stick is
   centred (both axes 2048) when `|h| < 0.01 && |v| < 0.01`, matching the reference's apply
   step. Event times round to the nearest millisecond — **container-side only**; the device
   never rounds.

Because holds are **milliseconds**, macro duration is independent of
`CONFIG.report_interval_ms`: changing the HID tick changes report density, not playback
speed, and needs no recompile (ADR-0009).

**The compiler injects no trailing neutral frame.** Neutral is executor-owned (§4.6), so a
malformed plan cannot omit the release and a valid plan does not carry a redundant one.

## 5.3 Plan binary format

Little-endian throughout. `plan_slots = 1`.

**Header (12 B):**

| Offset | Width | Field |
| --- | --- | --- |
| 0 | u32 | magic `0x4C50534E` — bytes `4E 53 50 4C` = `"NSPL"` |
| 4 | u8 | `format_version` = 1 |
| 5 | u8 | `record_size` = 11 |
| 6 | u16 | `record_count` |
| 8 | u32 | `loop_ms` (= `D`) |

**Record (11 B):**

| Offset | Width | Field |
| --- | --- | --- |
| 0 | 3 B | buttons, `btn_bits_pro2_t` order |
| 3 | 3 B | left stick, `pack_stick_data(lx, ly)` bit order |
| 6 | 3 B | right stick, `pack_stick_data(rx, ry)` bit order |
| 9 | u16 | `hold_ms` |

Payload = `12 + 11·record_count` bytes. The state bytes are the report's own
`buttons` + `left_stick` + `right_stick` layout, so the executor can `memcpy` 9 bytes into
the Pro2 report; motion, counter, power and the constants of `pro2_report_init` are **never**
plan data.

**Device-side structural check — yes, the device validates.** On commit it checks the magic,
`format_version`, `record_size == 11`, and `payload_len == 12 + 11·record_count`. Failure is
`ERROR BAD_PLAN` and the staging buffer is discarded. `payload_len > plan_capacity_bytes` is
`ERROR PLAN_TOO_LARGE`. Without this a truncated-but-CRC-valid transfer would replay as
garbage.

## 5.4 Loop semantics

`sum(hold_ms) == loop_ms`. The last record's hold runs to `loop_ms`, then the executor emits
neutral and immediately restarts the loop — **no inter-loop gap** (the map's Q10). The
neutral is the compiled-in template, on every exit path, never plan data.

## 5.5 Validation

**Refuse the whole compile** (nothing is uploaded): an empty array; a missing, non-finite or
backward `t` (non-decreasing required, duplicates allowed and merged); an unknown type, name,
side or value shape; `|h| > 1` or `|v| > 1`; a single `hold_ms > 65535`; compiled bytes
exceeding `plan_capacity_bytes` from `HELLO`.

The capacity check **is** the "absurd total length" gate — there is no separate arbitrary
wall-clock cap. Self-check: `sum(hold_ms) == round(t_last - t_first)`, reported as
"duration mismatch".

**Repaired rather than refused:** duplicate timestamps, no-op states, and same-millisecond
changes (§5.2).

## 5.6 Provenance and caching

Plan identity is a SHA-256 over the compiled payload (header + records), **truncated to 16
bytes**. It is carried in `LOAD_PLAN` and echoed in `STATUS.plan_hash`; the device never
computes one (ADR-0010). Hashing the *output* bytes means a compiler or format change yields
a new identity and forces a re-upload, so a stale plan can never be mistaken for current.
`plan_frame_count == record_count`, and `current_frame` is the record index.

**If the two hashes disagree, the container believes neither side silently.** The device does
not verify a hash it was given — it has no plan to compare it against, and hashing on the
device is exactly what ADR-0010 removes — so the *container* compares the echoed value against
the hash it sent and surfaces a mismatch rather than rendering a run as current.

## 5.7 Agreement without a shared header

The layout is frozen here, the firmware `static_assert`s the header and record sizes and
asserts the magic, and a **checked-in golden fixture** — one real macro, its exact plan hex,
and its SHA-256 — is asserted by both the container's compiler test and a host-side C test.
The fixture, not a generated header, is what catches container/firmware drift (known gap
G-8: it does not exist yet).

**Worked example** (the compiler's own reference run, macro `纠错宏.json`; 88 events → 71
records, `loop_ms = 26205`, 793 B):

```text
header  4e53504c 01 0b 4700 5d660000
frame 0 400000 000880 000880 7d00      (plus held, sticks centred, hold 125 ms)
SHA-256 of the full payload:
        1f0a90d3ccabcb198cd236afc4c7572658505bd4ecc429974aff15354661fdfa
```

**The real library, compiled** — this is the acceptance range, not a prediction:

| Macro | Events | Records | `loop_ms` | Plan B | `SHA-256[:16]` |
| --- | --- | --- | --- | --- | --- |
| 天妇罗巢穴宏1 | 243 | 243 | 115321 | 2685 | `9c3aabab06ca2d9f` |
| 天妇罗巢穴风扇 | 243 | 243 | 115321 | 2685 | `140336da0735aac2` |
| 杏仁巢穴宏 | 411 | 304 | 62148 | 3356 | `d2717773e32486c9` |
| 纠错宏 | 88 | 71 | 26205 | 793 | `1f0a90d3ccabcb19` |

`sum(hold) == loop_ms` exactly in all four; largest single hold 11184 ms (well inside the
u16); record counts far inside u16; 杏仁's 411 events collapse to 304 records via the no-op
merge and 153 sub-10 ms stick events are filtered by the reference rule. Worst plan is
3,356 B against a capacity of 65,536 B.
