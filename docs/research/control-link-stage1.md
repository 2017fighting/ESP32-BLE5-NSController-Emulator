# CONTROL framing over UART0: the stage-1 choices (issue #21)

**Scope:** §12.1 stage 1 — the UART0 transport instance, the shared TX lock with
rate-suppressed logging, and the CONTROL layer with `HELLO`/`STATUS` only. **Date:** 2026-10-02.
**Hardware on test host:** none. This is build-time work: the code compiles against ESP-IDF
v5.5.5, the host suite passes, and nothing was flashed. The bench questions in §4 are open.

This records the choices the ticket asked to be recorded (the ring and the baud), and — per
`AGENTS.md` — the places where the implementation turned out to disagree with a locked chapter,
surfaced rather than silently followed.

---

## 1. The three choices

| Choice | Value | Why |
| --- | --- | --- |
| Control-plane UART | **`UART_NUM_0`** (the CH9102 bridge) | §2.1/ADR-0001: the control plane shares the console/log wire. `transport_uart` was hardcoded to `UART_NUM_1` on GPIO4/5; it is now parameterised by `CONFIG_CONTROL_UART_PORT` (default 0) and `CONFIG_CONTROL_UART_RX_PIN`/`_TX_PIN` (default `-1` = chip-default IO_MUX, i.e. UART0's 43/44). |
| RX ring | **256 B, unchanged** | Deliberate, and the spec depends on it: §2.7 says at `chunk_size = 256` one chunk fills the ring and the device may ACK every chunk, which is permitted; §7.5 says a burst larger than the ring is data loss, not backpressure. Whether it absorbs the ACK window is **bench ticket #34**, not a code guess. The ring lives at `main/src/transport/transport.c` (`TRANSPORT_RX_BUF_SIZE`). |
| Baud | **921600 default**, `CONFIG_CONTROL_UART_BAUD` | §2.1's recommendation, explicitly "not a measurement" (G-1). The fallback is 115200 and is one Kconfig value; **bench ticket #33** settles it. The container/monitor must open at 921600 or nothing is legible, because the driver reprograms UART0 from the ROM console's 115200 at init. |

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
  `test/host/test_control_framing.c` (192 checks, also under ASan/UBSan) and CI workflow
  `.github/workflows/control-framing.yml`.
- `main/src/protocol/control/control_parser.c` — the CONTROL layer as a `protocol_router` parser.
  It is a **streaming** parser (`min_peek_len = 1`): a 512-byte frame cannot be contiguous in a
  256-byte ring, so it drains the ring byte by byte and returns one reply per completed frame.
- `main/src/protocol/control/control_link.c` — the **one TX lock**. `control_link_write()` takes
  it; the `esp_log_set_vprintf` hook takes the same lock, so a log line cannot land inside a
  frame's bytes (§2.2). While bulk is active, `control_log_policy` (portable, host-tested)
  allows at most one line per 100 ms and counts the rest.
- Decoder resynchronisation model: the leading `0x00` is mandatory (bytes before it are noise);
  after that **every `0x00` ends a segment and starts the next**. This is what makes "advance to
  the next `0x00`" recover after a corrupted block instead of swallowing the frame behind it —
  a property the random-resync test pins.

## 3. Four places the implementation disagrees with a locked chapter

Surfaced, not fixed in flight. Each is a candidate for an in-place amendment under §00's
"Amendment mechanics" (and #40 is the open ticket for spec corrections).

1. **§2.10 checks the version before the CRC; the decoder checks the CRC first.** §2.8 requires an
   untrusted frame to produce *no* ERROR, and a log line that happens to decode to seven
   plausible bytes is untrusted whatever its version byte says. With §2.10's order, a log flood
   would occasionally draw `VER_MISMATCH`/`BAD_LENGTH` for pure noise; the host suite asserts it
   draws zero. **Recommendation:** §2.10 gains one sentence — the CRC gates interpretation, and
   the version/type/verb checks apply only to a CRC-valid frame.
2. **§2.10's `7 + len > max_frame → BAD_LENGTH` is reachable only through the COBS-overflow
   path.** §2.8 says a block that decodes past `max_frame` is unreadable (silent). The decoder
   decodes into a `max_frame` buffer, so an over-long frame arrives as an overflow; it keeps the
   first bytes, and only emits `BAD_LENGTH` when the header's `len` really does exceed
   `max_frame`. The two sections are reconciled in code; the prose still reads as if a >512 frame
   is decoded then rejected.
3. **§7.4 undercounts the control-layer buffers.** "`max_frame` for RX + TX" is the *decoded*
   size. The wire form is COBS-encoded and delimiter-wrapped: for `max_frame = 512` that is
   `CONTROL_COBS_MAX = 515` encoded, `CONTROL_WIRE_MAX = 517` on the wire. The per-parser state
   here is ~1 KB (decode + block + TX), still inside the §7.4 budget's intent, but the table's
   arithmetic is short by ~5 bytes per buffer at the maximum.
4. **§7.2's router parser is not the peek-based kind.** The diagram is honoured (CONTROL is a
   protocol layer, not a new transport), but `protocol_router`'s `peek`/`probe` contract assumes
   a frame fits the peek window. CONTROL instead consumes the ring itself. No interface change
   was needed (`min_peek_len = 1`), but a reader should not expect the EasyCon parser shape.

**Two intentional stage-1 stubs**, both to be replaced by later tickets: `HELLO` advertises
`features = 0` because only `HELLO`/`STATUS` exist and neither has a feature bit (§2.6), and the
mode verbs (`LOAD_PLAN`, `START`, `STOP`, `PLACE_AMIIBO`, `UNPLACE_AMIIBO`, `PAIR_UNPAIR`,
`CONFIG`) answer `ERROR BAD_STATE(mode)` — the §2.5 code set is closed and has no "not
implemented", and silence would hang the one-outstanding conversation of §2.3. A correct
container reads `features = 0` and does not send them. #22–#25 replace the arms.

## 4. What is *not* proven here

Everything in §12.2 that needs the board:

- **#33** — 921600 with `ESP_LOG` on the same wire (this code makes the baud a Kconfig value so
  the fallback is one edit).
- **#34** — the 256 B ring under the largest plan (this code keeps the ring and lets the device
  ACK early, as §2.7 permits).
- `HELLO`/`STATUS` end to end on the real wire: the code replies, but no container has driven it
  against a flashed board yet. That is the ticket's Done-when and is the next bench step.

## 5. Verification performed

| Step | Result |
| --- | --- |
| Host suite, plain | `control framing ok: 192 checks` |
| Host suite, ASan + UBSan | same, clean |
| `idf.py set-target esp32s3` with the new defaults | rc 0; sdkconfig shows `TRANSPORT_LAYER_UART`, `PROTOCOL_LAYER_CONTROL`, `CONTROL_UART_PORT=0`, `CONTROL_UART_BAUD=921600` |
| `idf.py build` | rc 0, 0 warnings; app image **533,632 B** (`0x82480`), 83% of the 3 MB slot free |
