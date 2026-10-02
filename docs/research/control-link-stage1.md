# CONTROL framing over UART0: the stage-1 choices (issue #21)

**Scope:** §12.1 stage 1 — the UART0 transport instance, the shared TX lock with
rate-suppressed logging, and the CONTROL layer with `HELLO`/`STATUS` only. **Date:** 2026-10-02.
**Hardware on test host:** ESP32-S3-N16R8 on the CH9102 bridge, serial `5C93063985`. The
build-time half was verified with no device; the shipped image was then flashed and driven on the
wire — §4. The questions the bench did not close are in §5.

This records the choices the ticket asked to be recorded (the ring and the baud), and — per
`AGENTS.md` — the places where the implementation turned out to disagree with a locked chapter,
surfaced rather than silently followed.

---

## 1. The three choices

| Choice | Value | Why |
| --- | --- | --- |
| Control-plane UART | **`UART_NUM_0`** (the CH9102 bridge) | §2.1/ADR-0001: the control plane shares the console/log wire. `transport_uart` was hardcoded to `UART_NUM_1` on GPIO4/5; it is now parameterised by `CONFIG_CONTROL_UART_PORT` (default 0) and `CONFIG_CONTROL_UART_RX_PIN`/`_TX_PIN` (default `-1` = chip-default IO_MUX, i.e. UART0's 43/44). |
| RX ring | **256 B, unchanged** | Deliberate, and the spec depends on it: §2.7 says at `chunk_size = 256` one chunk fills the ring and the device may ACK every chunk, which is permitted; §7.5 says a burst larger than the ring is data loss, not backpressure. Whether it absorbs the ACK window is **bench ticket #34**, not a code guess. The ring lives at `main/src/transport/transport.c:47` (`TRANSPORT_RX_BUF_SIZE`). |
| Baud | **921600 default**, `CONFIG_CONTROL_UART_BAUD` | §2.1's recommendation, explicitly "not a measurement" (G-1). The fallback is 115200 and is one Kconfig value; **bench ticket #33** settles it. The container/monitor must open at 921600 or nothing is legible, because the driver reprograms UART0 from the ROM console's 115200 at init — **bench-confirmed, §4.2**. |

The S3 build now selects the UART transport and the CONTROL protocol
(`sdkconfig.defaults.esp32s3`: `CONFIG_TRANSPORT_LAYER_UART=y`,
`CONFIG_PROTOCOL_LAYER_CONTROL=y`). This **supersedes the base's `CONFIG_TRANSPORT_LAYER_USB_CDC=y`
for this build**: ADR-0001 leaves the native OTG port unused and puts the control plane on the
CH9102/UART0 where `ESP_LOG` already is. The EasyCon path and the USB-CDC transport are kept in
the tree, selectable, as §7.2 requires.

## 2. What the code does

- `main/include/protocol/control/control_protocol.h`, `main/src/protocol/control/control_frame.c`,
  `control_dispatch.c`, `control_log.c` — **portable C, no ESP-IDF.** COBS (the ≤254 run variant,
  delimiters added by the framer), CRC-16/CCITT-FALSE (bitwise; check value `0x29B1`,
  numerically identical to §2.2's `~esp_rom_crc16_be(~0xffff, …)`), a streaming decoder, the
  frame encoder, the `HELLO`/`STATUS` serialisers and the dispatch. Tested by
  `test/host/test_control_framing.c` and CI workflow `.github/workflows/control-framing.yml`.
- `main/src/protocol/control/control_parser.c:56` — the CONTROL layer as a `protocol_router`
  parser. It is a **streaming** parser (`min_peek_len = 1`): a 512-byte frame cannot be
  contiguous in a 256-byte ring, so it drains the ring byte by byte and returns one reply per
  completed frame.
- `main/src/protocol/control/control_link.c:32` — the **one TX lock**. `control_link_write()`
  takes it; the `esp_log_set_vprintf` hook (`control_link.c:67`) takes the same lock, so a log
  line cannot land inside a frame's bytes (§2.2). While bulk is active, `control_log_policy`
  (portable, host-tested) allows at most one line per 100 ms and counts the rest.
- Decoder resynchronisation model: the leading `0x00` is mandatory (bytes before it are noise);
  after that **every `0x00` ends a segment and starts the next**. This is what makes "advance to
  the next `0x00`" recover after a corrupted block instead of swallowing the frame behind it —
  a property the random-resync test pins.

## 3. Amendments landed in place, and one architectural note

Per §00's "Amendment mechanics", the owner chapter was edited rather than a second home
created, and the host suite is the machine-checkable companion. No ADR: none of these is hard
to reverse, and none changes a byte on the wire.

1. **The CRC is the trust gate (amended in §2.10, with §2.2 and §2.8 corrected to match).**
   §2.10 checked the version before the CRC; §2.8 forbids an `ERROR` for an untrusted frame.
   With the old order a log line that happened to decode to seven plausible bytes could draw
   `VER_MISMATCH`/`BAD_LENGTH`. §2.10 now says `ver`/`type`/`verb` are interpreted only once the
   CRC passes, and §2.2/§2.8 no longer name `BAD_LENGTH` for a frame whose `7 + len` exceeds
   `max_frame`: such a block decodes past the receiver's buffer, so its CRC cannot be computed
   and it is discarded silently. The decoder (`main/src/protocol/control/control_frame.c:167`)
   and the host suite assert exactly this.
2. **§7.4 now counts the control-layer buffers correctly (amended).** "`max_frame` for RX +
   TX" is the *decoded* size; the wire form is COBS-encoded and delimiter-wrapped, up to
   `max_frame + max_frame/254 + 3` ≈ 517 B per direction at `max_frame = 512`. §7.4's row now
   says so; the per-parser state here is ~1 KB, still inside the budget's intent.
3. **§7.2's router parser is not the peek-based kind (note, no amendment needed).** The diagram
   is honoured (CONTROL is a protocol layer, not a new transport), but `protocol_router`'s
   `peek`/`probe` contract assumes a frame fits the peek window. CONTROL instead consumes the
   ring itself (`control_parser.c:43`, `:56`). No interface change was needed
   (`min_peek_len = 1`), but a reader should not expect the EasyCon parser shape.

**Two intentional stage-1 stubs**, both to be replaced by later tickets: `HELLO` advertises
`features = 0` because only `HELLO`/`STATUS` exist and neither has a feature bit (§2.6), and the
unimplemented verbs (`LOAD_PLAN`, `START`, `STOP`, `PLACE_AMIIBO`, `UNPLACE_AMIIBO`,
`PAIR_UNPAIR`, `CONFIG`) answer `ERROR BAD_STATE(mode)`. Only some of those are really *mode*
verbs — §2.2 makes `CONFIG` and `PAIR_UNPAIR` legal in every mode — so for those two `BAD_STATE`
is a placeholder, not a claim about mode. That answer is a placeholder, not the final semantics —
§4.3 makes `LOAD_PLAN` legal in `IDLE`, and `START` with no plan is `NO_PLAN` — but the §2.5 code
set is closed and has no "not implemented", and silence would hang the one-outstanding
conversation of §2.3. A correct container reads `features = 0` and does not send them;
#22–#25 replace the arms.

## 4. The bench: what the wire settled (2026-10-02)

The image flashed is the one §6 builds: `0x82460` bytes, `CONFIG_MCU_DEBUG` off, UART0 at
921600. The board is the S3-N16R8 on the CH9102 bridge, USB Serial Number `5C93063985`
(`/dev/cu.usbmodem5C930639851`, resolved by `scripts/find_serial_port.py` — the arm64 host also
carries a second `usbmodem` node, so the serial pin matters). The host side is a throwaway
COBS+CRC client implementing §2.2; the container's real seams are #30.

### 4.1 `HELLO` and `STATUS`, end to end

| Check | Result |
| --- | --- |
| `HELLO` request `(REQUEST, verb 1, payload 01)` | one `REPLY`, `verb 1`, **20 bytes** |
| Capability fields | `proto_ver=1`, `fw=(0,1,0,0)`, `max_frame=512`, `chunk_size=256`, `plan_capacity_bytes=65536`, `plan_slots=1`, `features=0x0000`, fresh `boot_id` per power cycle — exactly §2.6 |
| `STATUS` request, twice | one `REPLY`, `verb 5`, **47 bytes** each; `mode=IDLE` and every other field at the §4.8 boot default; `uptime_ms` advanced 1778 → 1878 |
| `HELLO` with `proto_ver = 0x7f` | `ERROR VER_MISMATCH`, `detail = 1` (the device's own `proto_ver`) |
| Round trip, 60 × `HELLO`, shipped build | 59/60 answered; **min 18.3 ms, median 19.6 ms, p95 21.2 ms, max 22.9 ms** |

The ~20 ms floor is the transport, not the protocol: `transport_uart_rx_task` asks
`uart_read_bytes` for the whole free ring, so a 14-byte request still waits out
`UART_RX_TIMEOUT_MS = 10` (`transport_uart.c:24`), and the protocol task polls with a 2 ms
delay. That is irrelevant to the one-outstanding-request conversation of §2.3 and recorded for
#30 (a 2 Hz `STATUS` does not notice it).

### 4.2 The baud is not optional

| Host baud | `HELLO` replies |
| --- | --- |
| **921600** | **20/20** |
| 115200 | **0/20** (the 3070 bytes read are the boot banner) |

The driver reprograms UART0 from the ROM console's 115200 at init, so a host opened at the
fallback while the firmware is built at 921600 gets noise — the two must match, which is the
whole reason the baud is one Kconfig value. This is *not* #33's measurement: it says nothing
about whether 921600 survives a bulk transfer with logs multiplexed, only that the control plane
is legible there.

### 4.3 A log flood costs frames, never the link

`CONFIG_MCU_DEBUG=y` plus `LOG_MAXIMUM_LEVEL_DEBUG` makes the firmware flood
`protocol_layer`/`transport_layer` debug lines through the same `esp_log_set_vprintf` hook and
the same TX lock. Over a 10 s reset-and-blast of `HELLO`:

| Measure | Value |
| --- | --- |
| `HELLO` sent | 462 |
| `HELLO` replies (all 20 bytes, all CRC-valid) | 443 |
| Frames lost to the flood | 19 |
| Raw bytes received | 116,027 (**11.3 KiB/s**), 3098 `0x00`-delimited segments |
| Log/noise segments | 2655 |
| **`HELLO`-shaped replies with a bad CRC** | **0** |
| `boot_id` values across the run | **1** — the link never reset |
| `HELLO` + `STATUS` after the flood | both answered, same `boot_id` |

The zero is the point: the lock held, so not one reply was split by a log line. Frames *were*
lost — the reset window and TX contention cost 19 of 462 — and each time §2.8's resync recovered;
the link never reset and never needed a reconnect.

## 5. Still open

Not closed by the bench above, and not this ticket's to close:

- **#33** — 921600 with a *bulk* transfer and `ESP_LOG` multiplexed, measured as CRC retries and
the largest clean transfer. §4.2 shows the control plane is legible at 921600; it does not
measure bulk.
- **#34** — the 256 B ring under the largest plan. No bulk path exists before #22, so this cannot
be measured yet; the code keeps the ring and lets the device ACK early, as §2.7 permits.
- **#30** — the container's `serial`/`device` seams, which carry the DTR/RTS trap, the pinning by
USB serial number (§10.5) and the `boot_id` recovery of §2.8. The bench used a throwaway client,
not those seams.

### 5.1 From the independent review (2026-10-02)

A two-axis review of the whole ticket (`git diff 7fa7103`, Standards + Spec) found no defect that
blocks the Done-when. Two findings are worth recording rather than leaving in a review log:

- **`control_link_init()` is not guarded by `CONFIG_PROTOCOL_LAYER_CONTROL`.**
  `main/src/transport/transport.c:137` calls it inside the `CONFIG_TRANSPORT_LAYER_UART` branch
  only, but `main/CMakeLists.txt` compiles `control_link.c` unconditionally, so a
  UART-transport + EasyCon-protocol build would still install the CONTROL log hook and redirect
  `ESP_LOG`. No default configuration reaches that pair (S3 is UART+CONTROL, the base is
  USB-CDC+EasyCon), so it is latent; recorded here as the same transport→protocol seam note as
  §3.3.
- **`STATUS.last_error` stays `NONE` in stage 1.** `control_fill_status()` re-defaults the struct
  on every poll and the reject path does not record the code, so §3.2's "the last `ERROR` the
  device sent" is not yet observable even though stage 1 does send framing `ERROR`s. The code
  comment defers all `STATUS` field behaviour to #23; this makes the deferral explicit.

## 6. Verification performed

| Step | Result |
| --- | --- |
| Host suite, plain | `control framing ok: 196 checks` |
| Host suite, ASan + UBSan | same, clean |
| `idf.py set-target esp32s3` with the new defaults | rc 0; sdkconfig shows `TRANSPORT_LAYER_UART`, `PROTOCOL_LAYER_CONTROL`, `CONTROL_UART_PORT=0`, `CONTROL_UART_BAUD=921600` |
| `idf.py build` | rc 0, 0 warnings; app image **533,600 B** (`0x82460`), 83% of the 3 MB slot free |
| `idf.py -p /dev/cu.usbmodem5C930639851 flash`, shipped image | rc 0, hash verified |
| `HELLO`/`STATUS` on the wire | §4.1 — capability and status bytes exact, round-trip median 19.6 ms |
| Log flood on the wire | §4.3 — 443/462 replies, 0 corrupted, 1 `boot_id` |
