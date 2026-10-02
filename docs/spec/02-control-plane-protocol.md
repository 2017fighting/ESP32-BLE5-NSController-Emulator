# 2 · Control-plane protocol reference

The container drives the device with framed binary verbs over the control link. One framing
carries control and bulk upload alike (ADR-0006). Nothing else shares the wire except
`ESP_LOG`, which is multiplexed onto it and is *not* part of the protocol (ADR-0001).

## 2.1 The wire

| | Value | Where it comes from |
| --- | --- | --- |
| Port | `UART0` — the CH9102 bridge, `/dev/ttyACM0` on Linux | `sdkconfig:1585` (`CONFIG_ESP_CONSOLE_UART_NUM=0`) |
| Baud | **921600** recommended, **115200** fallback | #5; *recommendation, not a bench fact* |
| Line state | DTR and RTS **deasserted**, on open and again in `finally` | `s3-bringup.md` §10 |
| Console | `ESP_LOG` stays on this wire; `CONFIG_ESP_CONSOLE_NONE` is not set | `sdkconfig:1581` |

The CH9102 supports up to 4 Mbps (WCH CH9102DS1), and Linux's stock `cdc_acm` drives it at
high rate. The `115200` seen in bring-up was that session's *log* baud, not a link ceiling.
**921600 needs bench verification before it is treated as settled** (known gap G-1).

## 2.2 Framing

```text
        ┌──────────── COBS-encoded, no 0x00 inside ────────────┐
0x00 ───┤ ver │ type │ verb │ len(le16) │ crc(le16) │ payload … │─── 0x00
        └─────────────────────────────────────────────────────┘
```

| Field | Width | Meaning |
| --- | --- | --- |
| `ver` | u8 | protocol major version |
| `type` | u8 | `REQUEST` (1) / `REPLY` (2) / `EVENT` (3) |
| `verb` | u8 | one of the ten verbs (§2.4); **0 on an `EVENT`** (§3.3) |
| `len` | u16, little-endian | payload length in bytes, header excluded |
| `crc` | u16, little-endian | CRC-16/CCITT-FALSE over header bytes 0–4 and the payload |
| payload | `len` bytes | verb-specific |

**The header's exact offsets.** The seven header bytes are fixed, and the offsets are part of
the protocol:

| Offset | Width | Field |
| --- | --- | --- |
| 0 | u8 | `ver` |
| 1 | u8 | `type` |
| 2 | u8 | `verb` |
| 3 | u16, little-endian | `len` |
| 5 | u16, little-endian | `crc` |
| 7 | `len` bytes | payload |

*The CRC's exact polynomial and initial value are **a decision of record, not a discovery**:
any CRC-16 both sides agree on would do. CCITT-FALSE is chosen because the device can compute
it with `esp_rom_crc16_be` (`esp_rom_crc.h`: poly `0x1021`, init `0xffff`, refin/refout
false, xorout `0`), so the container does not guess and the firmware does not carry its own
table.*

**What the CRC covers, exactly.** It is CRC-16/CCITT-FALSE over **header bytes 0–4 and then
the payload** — `5 + len` bytes, in that order. The `crc` field's own two bytes are skipped (a
field cannot cover itself, and they are not always zero), and so are the COBS encoding and the
delimiters (the CRC is a property of the frame, not of its wire encoding). The input is
therefore **two segments**, which is what the ROM API's non-continuous form exists for:

- device: `uint16_t c = esp_rom_crc16_be((uint16_t)~0xffff, hdr, 5);` then
  `c = esp_rom_crc16_be(c, payload, len); crc = ~c;`
- container: `c = binascii.crc_hqx(hdr[:5], 0xFFFF); c = binascii.crc_hqx(payload, c)`

Both produce CCITT-FALSE (poly `0x1021`, init `0xffff`, refin/refout false, xorout `0`) over
`ver ‖ type ‖ verb ‖ len ‖ payload`, and both store the result in the little-endian `crc`
field. That is the whole contract: no table on either side, and no other polynomial.

**The wire form of a frame.** A frame on the wire is

```text
0x00 · COBS(hdr ‖ payload) · 0x00
```

— a **mandatory** `0x00` before and after the COBS block, not merely a terminator after it.
The leading delimiter is what makes a frame's first byte unambiguous: a CONTROL frame always
begins with `0x00`, so a receiver sharing the byte stream with other parsers (§7.2) and with
`ESP_LOG` can tell them apart from one byte, and "a partial frame is discarded" is the single
rule *advance to the next `0x00`* rather than a special case for the first frame after attach.
Two consecutive `0x00` bytes are an **empty segment and are ignored**, which is what makes the
leading and trailing delimiters harmless when frames are sent back to back.

COBS is the ordinary ≤ 254-byte-run variant: the encoded block contains no `0x00`, and runs of
up to 254 non-zero bytes cost one overhead byte. The encoder never appends or prepends the
delimiter; the framer adds both, outside the encoding. The header is fixed-width and the
payload is the only variable part.

**Resynchronisation depends on that coverage.** A receiver scans forward for `0x00`, decodes,
checks the CRC, and on failure advances to the **next** `0x00` and tries again; a `len` the
CRC did not cover would be a corrupted length nothing could catch.

**Log lines are noise, by construction.** `ESP_LOG` text never contains a `0x00`, so a log
line arrives as a chunk that fails the CRC and is discarded. **Resynchronisation never
resets the link**, and a corrupted frame costs one frame, not a session.

**The device must not interleave log output with a frame's bytes**, because a log line
arriving mid-frame corrupts it and costs a retransmit. `ESP_LOG` and control replies share
one TX lock, and **device logging is suppressed to a bounded rate while a bulk transfer is
active** (chapter 7). This is the price of multiplexing, and it is a firmware requirement,
not an optimisation.

`max_frame` bounds what either side may send: the **decoded** frame, header plus payload
(`7 + len`), and its value is **512**. It is advertised in `HELLO` (§2.6) and the device never
exceeds it. The largest frame either side sends is a `LOAD_PLAN` chunk — `7 + 5 + 256 = 268`
bytes — so 512 leaves room without budgeting a larger buffer than §7.4 counts. A frame whose
`7 + len` exceeds `max_frame` decodes past the receiver's `max_frame` buffer, so its CRC
cannot be computed over the whole `5 + len` bytes and the frame cannot be validated: it is
discarded silently and the receiver resyncs to the next `0x00`, exactly as §2.8 requires of
any block it cannot trust. The header being "readable" is not enough to name a code — the
receiver never read the payload the CRC covers.

## 2.3 Conversations

- **One outstanding *control* request.** The container sends a `REQUEST` and waits for its
  `REPLY` before sending another. There are **no request ids and no control-verb
  retransmission**. **Bulk upload is the one exception, and it is deliberate** (§2.7): a plan
  or a tag is a *windowed stream* of chunk frames paced by offset-keyed ACKs, so the chunks in
  one window are sent without a per-chunk reply. No other verb may be interleaved with a
  transfer, so the session stays strictly ordered; what changes is the unit of waiting — one
  **window**, not one frame. The trade-off, the rejected alternative, and the reason the
  exemption is confined to bulk are **ADR-0015**.
- **Every accepted request gets exactly one `REPLY` carrying the same `verb`**, with an
  empty or verb-specific payload. For a bulk transfer the ACK is the reply, and there is one
  per window rather than one per chunk (§2.7).
- **Every rejection gets exactly one `REPLY` of type `REPLY`, verb `ERROR`**, carrying a
  typed code and detail (§2.5). A rejection never leaves state touched.
- **`EVENT` is unsolicited**, carries no reply, and marks an edge, never a level.
- Retransmission is refused by design and not merely omitted: retrying `START` is a second
  start, and retrying `PLACE_AMIIBO` would be a second placement. Anyone adding a sequence
  field "for safety" and then a retry loop would be building double-starts.

## 2.4 The ten verbs

| # | Verb | Dir | Request | Reply |
| --- | --- | --- | --- | --- |
| 1 | `HELLO` | C→D | `proto_ver` (u8) | capabilities (§2.6) |
| 2 | `LOAD_PLAN` | C→D | staged, windowed, atomic (§2.7) | ACK / `ERROR` |
| 3 | `START` | C→D | — | ACK once the executor is armed / `ERROR` |
| 4 | `STOP` | C→D | — | ACK / `ERROR` |
| 5 | `STATUS` | C→D | — | volatile truth (chapter 3) |
| 6 | `PLACE_AMIIBO` | C→D | 540-byte tag, same chunked path as a plan | ACK / `ERROR` |
| 7 | `UNPLACE_AMIIBO` | C→D | — | ACK / `ERROR` |
| 8 | `PAIR_UNPAIR` | C→D | — | ACK / `ERROR` — a *request*; the firmware owns pairing (ADR-0013) |
| 9 | `CONFIG` | C→D | `report_interval_ms`, `led` | ACK / `ERROR` |
| 10 | `ERROR` | D→C | — | typed `code` + detail, on **every** rejection |

### Verb and event numbering

**The `#` column is the wire value of `verb`.** It is part of the protocol, not a reading
aid: `HELLO` is 1, `LOAD_PLAN` 2, `START` 3, `STOP` 4, `STATUS` 5, `PLACE_AMIIBO` 6,
`UNPLACE_AMIIBO` 7, `PAIR_UNPAIR` 8, `CONFIG` 9, `ERROR` 10. `ERROR` is the one verb whose
direction is device→container, and it is always a `REPLY` — there is no `ERROR` request, and
the container never sends verb 10. A verb outside 1–10 is `ERROR UNKNOWN_SUBCMD` (§2.5). The
`verb` field of an `EVENT` frame is **0**, reserved (§3.3), so an event is never mistaken for
a verb.

### Verbs with no payload

The requests for `START`, `STOP`, `STATUS`, `UNPLACE_AMIIBO` and `PAIR_UNPAIR` carry
`len = 0`, and their replies are empty — except `STATUS`, whose reply is the fixed 47 bytes of
§3.2. `PAIR_UNPAIR` **always means *forget the bond and go pairable***: it is not a toggle,
and it has no payload to select a direction because there is no action a device that does not
initiate pairing could take that differs from unpair (ADR-0013). When there is no bond it is
an ACK and nothing changes, like the other reduce-activity verbs of §4.4.

### Deliberately absent

- **`TELEMETRY`** — a per-frame push channel would drag mode state back onto the device and
  make the container a clock, contradicting ADR-0003.
- **`RESET`** — redundant: `STOP` then `LOAD_PLAN`.
- **OTA or flashing** — out of scope, and the control plane shares the flashing port
  (ADR-0001).
- **Any authentication** — the cable is the trust boundary.
- **Any verb that makes the device hold a library**, and **any per-verb timestamp**.
- **Any key verb** — the device has no key and never will (ADR-0011, ADR-0012).

### Verb legality

Legal in every mode and with no mode change: `HELLO` (the one tolerantly parsed frame),
`STATUS`, `CONFIG`, `PAIR_UNPAIR`, and the device→container `ERROR`. The rest are governed
by the transition table in chapter 4, which is where a verb's legal states live — this
chapter defines the verbs, chapter 4 defines when they are accepted.

## 2.5 The canonical `ERROR` table

`ERROR` is the device's only negative reply. `code` is one of the following, and the set is
**closed**: adding a code is a protocol change.

| Code | Means | Raised by |
| --- | --- | --- |
| `VER_MISMATCH` | the `HELLO` protocol version is not supported | `HELLO` |
| `UNKNOWN_TYPE` | frame `type` is not 1/2/3 | any frame |
| `UNKNOWN_SUBCMD` | `verb` is not one of the ten | any frame |
| `BAD_LENGTH` | a known type and verb with an unexpected payload length | any frame |
| `BAD_STATE` | the verb is illegal in the current mode | chapter 4 |
| `NO_PLAN` | `START` with no committed plan | `START` |
| `ALREADY_RUNNING` | `START` while already in `MACRO` | `START` |
| `BAD_PLAN` | committed bytes failed the structural check | `LOAD_PLAN` |
| `PLAN_TOO_LARGE` | the transfer exceeds `plan_capacity_bytes` | `LOAD_PLAN` |

**The wire values.** The codes are numbered **1–9 in the order of the table above**:
`VER_MISMATCH` 1, `UNKNOWN_TYPE` 2, `UNKNOWN_SUBCMD` 3, `BAD_LENGTH` 4, `BAD_STATE` 5,
`NO_PLAN` 6, `ALREADY_RUNNING` 7, `BAD_PLAN` 8, `PLAN_TOO_LARGE` 9. Zero is `NONE` and is
**`STATUS`-only** (§3.2): an `ERROR` reply never carries it. The set is closed, so these
numbers are as fixed as the names.

**The `ERROR` frame.** `ERROR` is a `REPLY` whose `verb` is 10 and whose payload is exactly
five bytes:

| Offset | Width | Field |
| --- | --- | --- |
| 0 | u8 | `code` (1–9) |
| 1 | u32, little-endian | `detail` |

`detail` is typed by the code, and **every code defines it** — a `detail` with no meaning
would leave the closed code set half-specified:

| Code | `detail` |
| --- | --- |
| `VER_MISMATCH` | the device's supported `proto_ver` |
| `UNKNOWN_TYPE` | the offending `type` byte |
| `UNKNOWN_SUBCMD` | the offending `verb` byte |
| `BAD_LENGTH` | the offending `len` |
| `BAD_STATE` | the current `mode` (§3.2) |
| `PLAN_TOO_LARGE` | the offending `total_len` |
| `NO_PLAN`, `ALREADY_RUNNING`, `BAD_PLAN` | 0 |

The `ERROR_RAISED` event carries the identical `code`·`detail` pair (§3.3).

Three notes that are decisions, not omissions:

- **There is no `NO_TAG`.** `PLACE_AMIIBO` carries the tag bytes itself, so an absent
  payload is `BAD_LENGTH` or a bulk rejection — a wire problem — rather than a state error.
- **There is no `KEY_MISSING` on the wire.** The device never has the key, so it can never
  miss it. An earlier resolution proposed `KEY_MISSING` as an `ERROR` code; it stopped being
  one the moment the device stopped holding key material, and the container's refusal is the
  local `KEY_ABSENT` / `KEY_INVALID` / `KEY_UNVERIFIED` state of §6.7 instead — carrying the
  expected mount path and the single action that fixes it. Nothing is lost by the move: the
  refusal never needed a round trip.
- **There is no device-side "internal error" code.** If a fault cannot be expressed by the
  table above, that is a gap to raise rather than a code to invent at runtime.

An unparseable or CRC-failing frame produces **no** `ERROR` — the receiver cannot trust
enough of it to name a code, and it resynchronises instead. Errors are for frames that
arrived intact and were understood.

## 2.6 `HELLO` capabilities

`HELLO` is the only frame the device parses tolerantly, because it is the frame that
establishes the version. Everything else is strict.

| Field | Width | Meaning |
| --- | --- | --- |
| `proto_ver` | u8 | protocol major the device speaks; 1 |
| `fw_version` | 4×u8 | `major`, `minor`, `patch`, `build`, in that order |
| `boot_id` | u32 | fresh on every power cycle (§2.8) |
| `max_frame` | u16 | largest frame either side may send; 512 |
| `chunk_size` | u16 | bulk chunk payload size; 256 |
| `plan_capacity_bytes` | u32 | plan bytes the device will accept; the device allocates exactly the announced transfer size; 65536 |
| `plan_slots` | u8 | 1 |
| `features` | u16 bitfield | `macro` (bit 0) / `amiibo` (bit 1) / `config` (bit 2); bits 3–15 reserved 0 |

**The reply's payload, in order:** `proto_ver`(1) · `fw_version`(4) · `boot_id`(4) ·
`max_frame`(2) · `chunk_size`(2) · `plan_capacity_bytes`(4) · `plan_slots`(1) ·
`features`(2) = **20 bytes**, every multi-byte field little-endian. **The request's payload
is one byte**: `proto_ver` u8.

**There is no `vid`/`pid` field, and its removal was deliberate.** The control link is UART0
through the CH9102 — a separate chip whose USB descriptors the ESP32-S3 cannot read — and the
native OTG port is deliberately unused (ADR-0001). A field nothing on this link can produce
is worse than an absent one, because it invites a container to key on a constant. The "is the
right firmware behind this port?" question this section exists to answer is answered by
`fw_version`; "a different device" is answered by `boot_id` plus `features`.

**`fw_version` is four bytes and not a `u32`,** so the UI renders `major.minor.patch` without
unpacking a bitfield, and `build` stays free for a monotonic per-flash counter.

- **`features` is how this design stays degradable.** A new verb appears as a bit, so an
  older container disables the control instead of erroring on it. The container reads
  `features.amiibo` to decide whether to offer `AMIIBO` at all (chapter 8).
- **There is no key bit.** The device has no opinion about key material (ADR-0011).
- **There is deliberately no free-heap field.** It changes per allocation, and a UI that
  renders it invites conclusions the number cannot support.
- **`fw_version` exists for a concrete reason.** This exact board previously ran a different
  firmware with its own command shell; "is the right firmware behind this port?" needs an
  answer that neither the port name nor luck provides.

## 2.7 Bulk upload

`LOAD_PLAN` and `PLACE_AMIIBO` are the same path with different verbs and sizes: a plan is
`12 + 11·record_count` bytes (chapter 5), a tag is 540 bytes.

1. The container announces a transfer with its total length and, for a plan, the plan hash.
2. It sends **chunks of `chunk_size` (256) bytes**, each frame carrying **the offset it
   writes to** and the bytes.
3. The device replies with a **windowed ACK carrying the next expected offset** — not one
   ACK per chunk. One ACK per ~4 KB keeps a 64 KiB upload to ~16 round trips instead of
   ~256. Bulk is the one exception to §2.3's one-outstanding rule, and **ADR-0015** records
   why.
4. **An ACK means the bytes are written to device RAM**, not buffered. The device has a
   256-byte RX ring at a 100 Hz tick and no TX ring or TX task; it cannot absorb a burst.
5. The container **resumes at the ACK'd offset**. Resume is free, because the previous ACK
   already names it.
6. **Commit is atomic.** Bytes become the active plan or placed tag only when the final
   offset and the whole-transfer check pass. Until commit, `STATUS` reports
   `mode=IDLE, plan=none` (or the previous tag), so container and device can never disagree
   about what exists.
7. **A failed transfer discards the staging buffer** and changes nothing. For a plan the
   device additionally runs the structural check of chapter 5 (§5.3) at commit.

**Container death mid-upload needs no protocol feature.** On control-link re-attach the
device resets the RX ring through the existing `zc_reset(tp->rx_buffer)`
(`main/src/transport/transport_usb_cdc.c:118`) and aborts staging on the same event. An
in-flight upload dies by construction.

**The three bulk frames, exactly.** Every bulk frame's payload begins with a one-byte `op`:
`1` announce, `2` chunk, `3` commit. `0` is reserved. All multi-byte fields are
little-endian.

| Frame | `op` | Payload after `op` | `len` |
| --- | --- | --- | --- |
| `LOAD_PLAN` announce | 1 | `total_len` u32 · `plan_hash` 16 B | 21 |
| `LOAD_PLAN` chunk | 2 | `offset` u32 · `chunk` bytes | 5 + n, n ≤ 256 |
| `LOAD_PLAN` commit | 3 | `total_len` u32 · `plan_hash` 16 B | 21 |
| `PLACE_AMIIBO` announce | 1 | `total_len` u32 (= 540) | 5 |
| `PLACE_AMIIBO` chunk | 2 | `offset` u32 · `chunk` bytes | 5 + n, n ≤ 256 |
| `PLACE_AMIIBO` commit | 3 | `total_len` u32 (= 540) | 5 |
| bulk ACK (`REPLY`) | — | `next_expected_offset` u32 | 4 |

**A tag carries no hash.** Its identity is inside the 540 bytes and the container is what
keys on it (ADR-0011), so `PLACE_AMIIBO`'s announce and commit are sixteen bytes shorter than
a plan's. The frame's `verb` already says which transfer is being announced, so no extra
discriminator is needed.

`next_expected_offset` is the byte offset the device will write next: after a chunk at
`offset` of `n` bytes it is `offset + n`, and after a commit it is `total_len`. Zero after an
announce means "start at zero". The container resumes **from the ACK'd offset**, so a retry
needs no request id and no byte count it did not already have.

**The ACK window is 4096 bytes.** The device ACKs once it has consumed at least 4096 bytes
since its last ACK, and it **may ACK earlier, at any time**, because it must never overrun:
the RX ring is 256 bytes (§7.1, §7.5), so at `chunk_size = 256` one chunk fills it and the
device may ACK every chunk under load. That is a permitted outcome, not a protocol change,
and the container must never depend on an ACK arriving only at a window boundary. The
container keeps **at most one window of un-ACKed chunk bytes in flight** and sends no other
verb for the life of the transfer. The window is a constant of this section rather than a
`HELLO` field — `chunk_size` is already advertised — and retry is at **window granularity**:
resend from `next_expected_offset`. Why bulk is the exception, and the alternative it
rejected, are **ADR-0015**.

A **checked-in golden fixture** — a plan built from a real macro, with its exact frame bytes
and hash — is what keeps the two implementations from drifting (chapter 12, G-8).

## 2.8 Versioning, unknown frames and recovery

- **One 8-bit major protocol version, set at `HELLO`.** It is carried twice — the frame
  header's `ver` and the `HELLO` payload's `proto_ver` — and both must be 1. A mismatch is
  `VER_MISMATCH` carrying the device's supported version in `detail` — **never** a silent
  drop, which would deadlock the one frame that establishes the version. The header's `ver`
  is checked on every frame before the payload is interpreted, which is safe because the
  header layout is fixed across versions: the version exists so a *stale* build announces
  itself.
- **The container sends `HELLO` first on every attach and every reconnect, and nothing
  before it.** The device does **not require** it: there is no version gate on the other
  verbs, because one container and one board are flashed together and a gate would turn a
  forgotten `HELLO` into a capability error. It is the container's fixed convention and the
  only source of `features` and the advertised limits.
- **Tolerant parsing is `HELLO`'s alone, and it is exactly this much.** Any
  `REQUEST`/`HELLO` frame is accepted whatever its declared length: `len = 0` is a version
  probe and is answered with the capabilities reply; `len ≥ 1` is compared against the
  device's version, a match answered with the capabilities reply and a mismatch with
  `ERROR VER_MISMATCH`; bytes past the first are ignored. The link stays up in every case.
  Every other verb is parsed strictly.
- **An unknown frame type or verb gets a typed `ERROR` and the link stays up.** Container
  and device drift by definition, and resetting turns "one bad verb" into "the device is
  gone". A known verb whose *payload* length is wrong is `BAD_LENGTH`. A frame whose
  `7 + len` exceeds `max_frame` is **not** named: it decodes past the receiver's buffer, so
  its CRC cannot be checked and it is discarded silently under the next rule.
- **A frame the receiver cannot trust produces no `ERROR` at all.** A CRC failure, a COBS
  block that does not decode, or a COBS block that decodes past `max_frame` is discarded
  silently and the receiver advances to the next `0x00` (§2.2) — it cannot name a code for
  bytes it did not understand, and §2.5 reserves errors for frames that arrived intact. For
  the same reason a `REPLY` or an `EVENT` arriving **at the device** is discarded silently:
  it is well-framed, but it is not a request the device can be asked to answer, and calling
  it `UNKNOWN_TYPE` would be false.
- **No forward-compatibility promise.** One container, one board, flashed together. The
  version exists so a *stale* build announces itself, not so two versions interoperate.
- **Recovery is container-led `HELLO` plus `boot_id`.** The container sends `HELLO` on
  every attach and every reconnect, and `boot_id` distinguishes the three cases that
  otherwise look identical:

| Case | Container does |
| --- | --- |
| same `boot_id`, same power | reconnect: keep UI state, re-read `STATUS`, and **assume nothing about the mode** — a run may not have stopped |
| new `boot_id`, new power | discard the plan, the placement and the UI state; require re-upload (ADR-0004) |
| different device or firmware | announce it; some verbs may not exist; disable what `features` does not claim |

`boot_id` changes are **ordinary operation**, not an error: the console's own sleep/wake
cycle reboots this board on roughly half of slow cycles (chapter 9, `ns2-console-lifecycle.md`
§5.3). A recovery path that treats a reboot as exceptional will be wrong in normal use.

## 2.9 `CONFIG`

**The payload is exactly three bytes**: `report_interval_ms` u16, `led` u8 (0 or 1), both
little-endian. **Both fields are always present** — there is no presence mask — because the
container re-sends the whole setting on reconnect anyway and a mask would be a third way to
say something both sides already know. The reply is empty.

`CONFIG` carries `report_interval_ms` and `led`. Both are **volatile** — never persisted in
NVS — and both apply at the **next safe boundary**:

| Mode | Applied |
| --- | --- |
| `MACRO` | at the next loop boundary |
| `IDLE`, `AMIIBO` | immediately |

A loop boundary is the only safe moment in `MACRO`, because a report-interval change
mid-macro perturbs the one thing ADR-0003 says is consistent. Volatile costs nothing, since
the container re-sends on reconnect.

**Not in `CONFIG`:** the advertising opcode and grip-order data. Those are console-facing
and already decided in the firmware's `ns2_codec.c`.

**Two cautions the implementation inherits, both measured:**

- `CONFIG_HID_REPORT_INTERVAL` is **not a millisecond control**. At `CONFIG_FREERTOS_HZ=100`
  (`sdkconfig:1761`) it rounds to the tick grid: `15 → 10 ms`, `25 → 20 ms`, and **`5 → 0`
  ticks, which spins** (`ns2-console-lifecycle.md` §4.3). Sub-10 ms periods need the tick
  rate raised. **This applies to `CONFIG`'s `report_interval_ms` too** — the verb exposes the
  setting, and the setting is quantised, so a UI that offers "5 ms" would be offering a spin
  loop. The UI must present the achievable grid (10, 20, 30, … ms at the current tick rate)
  or name the quantisation; §7.5 has the arithmetic.
- On a DEBUG-logged build the report rate is set by the **UART log budget**, not by this
  setting: 152 B of log per report against 11,520 B/s caps it at 75.8/s, and 74.4/s was
  observed — 98% of that ceiling. Any latency figure from a DEBUG run is a logging floor.
  Logging must be at `INFO` or lower for any timing measurement.

## 2.10 The frame catalogue, in decode order

The sections above own every fact here. This is the order to decode in, so an implementation
does not have to reassemble the layout from prose, and the pointer is the authority wherever a
byte's meaning is owned elsewhere.

1. **Scan** to the next `0x00`. The bytes up to it are one COBS block; an empty block (two
   consecutive delimiters) is skipped.
2. **Decode COBS**, bounded by `max_frame`; a block that decodes past it is unreadable.
3. **Read the header**: `ver`(u8), `type`(u8), `verb`(u8), `len`(le16), `crc`(le16) (§2.2).
4. **Locate the frame's extent**: the block must be exactly `7 + len` bytes, which is what
   fixes the CRC's input. A block that is not exactly `7 + len` bytes, that decodes past
   `max_frame`, or whose COBS decode fails is discarded silently and the receiver resyncs
   (§2.2, §2.8) — its `len` cannot be trusted and its CRC cannot be computed.
5. **Check the CRC** over bytes 0–4 ‖ payload. On failure, discard silently and resync (§2.2,
   §2.8). **The CRC is the trust gate**: `ver`, `type` and `verb` are interpreted only once it
   passes, so a log line that happens to decode to seven plausible bytes can never draw an
   `ERROR`. A CRC-valid frame with `ver ≠ 1` is `ERROR VER_MISMATCH`; a known verb whose
   *payload* length is wrong (a non-empty `STATUS`) is `ERROR BAD_LENGTH` (§2.5).
6. **Dispatch on `type`**: 1 `REQUEST`, 2 `REPLY`, 3 `EVENT`; anything else is
   `ERROR UNKNOWN_TYPE`.
7. **For a `REQUEST`**, dispatch on `verb` 1–10 (§2.4) with the payload layouts of §2.5–§2.9;
   `verb` outside 1–10 is `ERROR UNKNOWN_SUBCMD`.
8. **For an `EVENT`**, `verb` is 0 and the payload begins with the `kind` byte (§3.3).
9. **For a `REPLY`**, `verb` echoes the request's; `verb` 10 is `ERROR` and verbs 2 and 6
   carry the bulk ACK's `next_expected_offset` (§2.7).

The one thing a reader may look for and not find: **`ERROR` is a `REPLY`, never a request.**
It shares the verb numbering so a decode table has one column, while §2.4 fixes its direction.
