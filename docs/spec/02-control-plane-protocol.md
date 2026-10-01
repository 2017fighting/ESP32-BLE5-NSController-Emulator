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
| `verb` | u8 | one of the ten verbs (§2.4) |
| `len` | u16, little-endian | payload length in bytes, header excluded |
| `crc` | u16, little-endian | CRC-16/CCITT-FALSE over `ver … payload` |
| payload | `len` bytes | verb-specific |

*The CRC's exact polynomial and initial value are **a decision of record, not a discovery**:
any CRC-16 both sides agree on would do. CCITT-FALSE is chosen because the device can compute
it with `esp_rom_crc16_be` (`esp_rom_crc.h`: poly `0x1021`, init `0xffff`, refin/refout
false, xorout `0`, i.e. `~crc16_be((uint16_t)~0xffff, buf, n)`), so the container does not
guess and the firmware does not carry its own table (§12.3, G-2).*

The frame is COBS-encoded and terminated by a single `0x00`. The header is fixed-width and
the payload is the only variable part.

**The CRC covers the header and the payload.** That is the only reading under which
resynchronisation works as designed: a receiver scans forward for `0x00`, decodes, checks
the CRC, and on failure advances to the **next** `0x00` and tries again.

**Log lines are noise, by construction.** `ESP_LOG` text never contains a `0x00`, so a log
line arrives as a chunk that fails the CRC and is discarded. **Resynchronisation never
resets the link**, and a corrupted frame costs one frame, not a session.

**The device must not interleave log output with a frame's bytes**, because a log line
arriving mid-frame corrupts it and costs a retransmit. `ESP_LOG` and control replies share
one TX lock, and **device logging is suppressed to a bounded rate while a bulk transfer is
active** (chapter 7). This is the price of multiplexing, and it is a firmware requirement,
not an optimisation.

`max_frame` bounds what either side may send. It is advertised in `HELLO` (§2.6) and the
device never exceeds it.

## 2.3 Conversations

- **One outstanding request.** The container sends a `REQUEST` and waits for its `REPLY`
  before sending another. There are **no request ids and no control-verb retransmission**.
  Bulk data is the only thing retried, and it is addressed by offset, not by id.
- **Every accepted request gets exactly one `REPLY` carrying the same `verb`**, with an
  empty or verb-specific payload.
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
| `proto_ver` | u8 | protocol major the device speaks |
| `fw_version` | u32 or 4×u8 | firmware identity |
| `boot_id` | u32 | fresh on every power cycle (§2.8) |
| `vid` / `pid` | u16 each | as reported by the USB stack |
| `max_frame` | u16 | largest frame either side may send |
| `chunk_size` | u16 | bulk chunk payload size; 256 |
| `plan_capacity_bytes` | u32 | plan bytes the device will accept; the device allocates exactly the announced transfer size |
| `plan_slots` | u8 | 1 |
| `features` | u16 bitfield | `macro` / `amiibo` / `config`; the only extensibility mechanism |

- **`features` is how this design stays degradable.** A new verb appears as a bit, so an
  older container disables the control instead of erroring on it. The container reads
  `features.amiibo` to decide whether to offer `AMIIBO` at all (chapter 8).
- **There is no key bit.** The device has no opinion about key material (ADR-0011).
- **There is deliberately no free-heap field.** It changes per allocation, and a UI that
  renders it invites conclusions the number cannot support.
- **`fw_version` exists for a concrete reason.** This exact board previously ran a different
  firmware with its own command shell; "is the right firmware behind this port?" needs an
  answer that neither VID/PID nor luck provides.

## 2.7 Bulk upload

`LOAD_PLAN` and `PLACE_AMIIBO` are the same path with different verbs and sizes: a plan is
`12 + 11·record_count` bytes (chapter 5), a tag is 540 bytes.

1. The container announces a transfer with its total length and, for a plan, the plan hash.
2. It sends **chunks of `chunk_size` (256) bytes**, each frame carrying **the offset it
   writes to** and the bytes.
3. The device replies with a **windowed ACK carrying the next expected offset** — not one
   ACK per chunk. One ACK per ~4 KB keeps a 140 KB upload to ~35 round trips instead of
   ~547.
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

*Field-level detail this chapter fixes.* The chunk frame carries `offset` (u32,
little-endian) followed by the bytes; the announce frame carries `total_len` (u32) and,
for a plan, the 16-byte plan hash; the commit frame carries `total_len` and the same hash.
These packings are decisions of record, and a **checked-in golden fixture** — a plan built
from a real macro, with its exact bytes and hash — is what keeps the two implementations
from drifting (chapter 12, G-8).

## 2.8 Versioning, unknown frames and recovery

- **One 8-bit major protocol version, set at `HELLO`.** A mismatch is `VER_MISMATCH`
  carrying the device's supported version — **never** a silent drop, which would deadlock
  the one frame that establishes the version.
- **An unknown frame type or verb gets a typed `ERROR` and the link stays up.** Container
  and device drift by definition, and resetting turns "one bad verb" into "the board is
  gone". A known type with an unexpected length is `BAD_LENGTH`, same treatment.
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
