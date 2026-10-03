# The container's serial and device seams, against the real board (issue #30)

**Scope:** §12.1 stage 1 item 3 — the container's `serial` and `device` seams against real
verbs: COBS/CRC both ways, one outstanding request, the DTR/RTS line discipline, the §2.7
windowed bulk upload, the §5.6 plan-hash comparison, and the §2.8 `boot_id` recovery.
**Date:** 2026-10-03.
**Hardware on test host:** ESP32-S3-N16R8 on the CH9102 bridge, USB Serial Number
`5C93063985`, node `/dev/cu.usbmodem5C930639851` (resolved by `scripts/find_serial_port.py`;
the arm64 host also carries a second `usbmodem` node, `000000000000`, so the serial pin
matters). Firmware on the board: `fw=0.1.0`, `features=0x0001` (`MACRO`), `boot_id` fresh per
power cycle.

This records what the bench settled, the one place the run disagreed with a stated assumption,
and the questions this ticket does not close.

---

## 1. What was built

Four modules, each a seam of §8.2, and nothing in the state model or the web seam moved:

| Module | What it is |
| --- | --- |
| `container/ns2serial/port.py` | `SerialPortTransport` — the real asyncio port. The §8.3 open sequence (`dsrdtr=False, rtscts=False`, DTR/RTS deasserted on open **and again before close**), and `PortBusy` as a distinct failure with a best-effort holder name from `lsof`. |
| `container/ns2serial/frame_io.py` | `FrameTransport` — the §2.3 conversation and the §2.7 bulk stream. One outstanding request behind one lock; the 4096-byte ACK window; resume from the ACK'd offset at window granularity; `EVENT` and `ESP_LOG` demux. |
| `container/ns2device/session.py` | `SessionDevice` — the verbs. `HELLO`/`STATUS` fixed-width checks, `LOAD_PLAN` with the §5.6 echo comparison (`PlanHashMismatch`), `START`/`STOP`/`PLACE_AMIIBO`/`UNPLACE_AMIIBO`/`PAIR_UNPAIR`/`CONFIG`. |
| `container/ns2container/state.py` | The Controller now owns reconnection: `HELLO` on every attach, the `boot_id` branch, a backoff connect loop that **never retries through a held port**, and device log lines forwarded to the Logs screen. |

`create_controller()` now builds the real session by default; `StubDevice` stays for tests. 32
new offline tests drive `FrameTransport`/`SessionDevice` against a byte-level fake board, so
both sides cross a real COBS/CRC encode and decode.

**The line discipline is in the one place the spec fixes it** (`SerialPortTransport._deassert`,
called in `open()` and at the top of `close()`), because the CH9102 wires DTR→GPIO0 and
RTS→EN. Asserted, the board sits in reset and the port returns nothing forever.

Both directions check the header `ver` before a payload is interpreted (§2.8), and a frame
that arrives intact but is the wrong shape — a 46-byte `STATUS`, a 3-byte bulk ACK, a header
`ver` of 2 — raises a typed `ProtocolError` rather than being padded or attributed to the
device's own `ERROR` (§3.2).

## 2. The bench: what the wire settled

The host side is the real `SerialPortTransport` → `FrameTransport` → `SessionDevice` stack,
driven both directly and through the `Controller`. The board was opened at **115200** because
that is what the flashed image runs; the branch's `sdkconfig` says `921600`, so the flashed
image and the current tree disagree on the baud — see §3.

### 2.1 `HELLO`, `STATUS` and the ESP_LOG demux

| Check | Result |
| --- | --- |
| `HELLO`, one request | one `REPLY`, verb 1, **20 bytes** |
| Capability bytes | `proto_ver=1`, `fw=0.1.0.0`, `max_frame=512`, `chunk_size=256`, `plan_capacity_bytes=65536`, `plan_slots=1`, `features=0x0001` — exactly §2.6 |
| `STATUS`, 20 × at 2 Hz | **20/20 answered**, 47 bytes each; round trip min **58.2 ms**, max **62.1 ms** |
| `ESP_LOG` demuxed from the same bytes | **720 lines in 2 s** (~360/s), first line `debug transport_layer: Protocol route result: 1` |

The ~60 ms `STATUS` floor is the DEBUG image's log budget at 115200, not the protocol — the
same effect §2.9 warns about ("any latency figure from a DEBUG run is a logging floor"). It is
**not** #33's measurement and nothing here depends on it. The demux count is the point: the
log text and the frames share one byte stream, and the container separates them with no
dropped **frame** (`FrameDecoder.dropped` only counts segments that were neither valid frames
nor printable text).

### 2.2 The container drives `LOAD_PLAN`, `START` and `STOP`

Through the `Controller`, with the golden fixture (793 B; `docs/spec/05-plan-format.md:182`,
`fixtures/plan/correction.json`):

| Step | Snapshot |
| --- | --- |
| after `start()` | `link=UP`, `fw=0.1.0`, macro library `ready` |
| after `start_macro` | `mode=MACRO`, `plan_hash=1f0a90d3ccabcb198cd236afc4c75726`, `bytes=793` |
| ~2 s later | `mode=MACRO`, `loopCount=0`, `currentFrame=1` (the fixture's `loop_ms=26205`, so this is the first pass) |
| after `stop_macro` | `mode=IDLE`, `stopReason=CONTAINER_STOP`, plan **retained** (§4.3) |

The echoed hash is exactly the first 16 bytes of the fixture's SHA-256, so the §5.6
comparison agrees on real bytes rather than in a test double.

### 2.3 A power cycle recovers with no manual step

The board was reset in place by pulsing RTS→EN from a second opener (the node is
non-exclusive on macOS, §3.2), while the container kept the port open. The container saw the
`BOOT` event, re-sent `HELLO`, branched on `boot_id`, and reported:

| | before | after |
| --- | --- | --- |
| `boot_id` | `3ffd4274` | `cf1a566c` |
| `recovery` | `SAME_POWER` | **`NEW_POWER`** |
| `link` | `UP` | **`UP`** |
| `mode` / `plan` | `IDLE` (stopped) | `IDLE` / `plan=None` |

**No `Reconnect` was pressed** — the §2.8 contract holds on the wire.

### 2.4 What the first attach looks like, and why

Every open **loses the first `HELLO`**: the OS pulses DTR/RTS as the node is acquired, and
RTS→EN resets the board, so the board is in its boot window when the first request lands.
The measured sequence is `open → HELLO lost (timeout) → backoff → HELLO answered`, which is
exactly what §8.9's "retries the open with backoff" is for. It is recorded here rather than
papered over with a fixed settle sleep, because the retry is the specified mechanism. The
connect loop makes the recovery invisible after ~1–3 s.

## 3. Findings, and the one disagreement

### 3.1 The flashed image runs 115200; the tree says 921600

The captured `HELLO` reports `fw=0.1.0` and answers at **115200** (20/20 `HELLO` at 115200,
0/20 at 921600 and 460800). The current `sdkconfig` and `sdkconfig.defaults.esp32s3` both say
`CONFIG_CONTROL_UART_BAUD=921600`, and `build/config/sdkconfig.h` says 115200 — the build tree
and the flashed image predate the current defaults. This is **G-1/#33's territory, not this
ticket's**: the seams were validated at the documented fallback, and the default is unchanged
at 921600. Nothing in the container assumes either value; the baud is configuration
(`CONTROLLER_BAUD`, §8.3).

### 3.2 macOS does not enforce one process per port

§10.5's "one process per port, surfaced as `Port busy`" is implemented and unit-tested
(`PortBusy` is a distinct type from `TransportUnavailable`, the holder is named from `lsof`,
and the connect loop never retries through it). But the macOS node **accepts a second open**:
measured with one pyserial instance holding `/dev/cu.usbmodem5C930639851` and a second
opening the same node — it succeeds. This matches ADR-0014's "forwarding is non-exclusive".

So on the macOS bench host the OS cannot produce the `Port busy` signal, and the protection
against a second holder is the **operational rule** (§10.2: stop the container before
flashing), not the detector. A second *container* on the same forwarded node is not detectable
from inside the VM either. No code change is taken here — the detector is correct where the OS
refuses, and the container's own behaviour (surface, do not retry through) is as specified —
but the gap should not be mistaken for coverage. It is the kind of thing that graduates from
§10.5 if a second holder is ever observed.

### 3.3 A stale `BOOT` event on first attach

The board emits a `BOOT` event at boot; a container that attaches after that boot receives it
and logs "the board rebooted" even though it never held the link. The state is already fresh,
so nothing is lost, and the following `HELLO` establishes the real `boot_id` — this is §3.3's
"`HELLO` may be unnecessary when a `BOOT` event arrives on an open port" seen from the other
side. Recorded, not changed: a "was I already attached?" refinement would be a new fact on the
device, and the current behaviour is safe.

## 4. Verification performed

| Step | Result |
| --- | --- |
| Offline suite, `python3 -m unittest discover -s container/tests -p 'test_*.py'` | **159 tests, OK** (32 new) |
| Repo ruff (`ruff check container/`) | **All checks passed** |
| `HELLO`/`STATUS` on the wire | §2.1 — capability and status bytes exact, 20/20 polls |
| `LOAD_PLAN` + `START` + `STOP` through the `Controller` | §2.2 — fixture hash echoed, `MACRO`, `CONTAINER_STOP`, plan retained |
| Power cycle, no manual step | §2.3 — `NEW_POWER`, new `boot_id`, link `UP`, plan cleared |
| `ESP_LOG` demux | §2.1 — 720 lines in 2 s, zero corrupted frames |
| DTR/RTS discipline | unit-tested order (deassert after construction, again before close) |

## 5. Still open

- **#31** — the console-lifecycle policy (stop-on-drop, rotate-on-reconnect) belongs in
  `Controller._on_event`; the seam it reads from (`STATUS` + `EVENT`) is now real.
- **#32** — the sealing round trip; `place_figure` is refused as `SEALING_UNAVAILABLE` until
  then, and this firmware advertises `features.amiibo = false` anyway.
- **#33** — 921600 with `ESP_LOG` on the same wire, and the 256 B RX ring under the largest
  plan (#34). This ticket used the 115200 fallback and did not measure either.
- **The `round-trip ~60 ms` figure** is a DEBUG-log floor, not a transport or protocol
  property; it must not be quoted as either.
