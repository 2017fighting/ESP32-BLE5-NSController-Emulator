# 1 · System overview

## 1.1 The two artifacts

```text
   ┌──────────────────────────────┐            ┌───────────────────────────────┐
   │  Container                   │            │  Device (ESP32-S3-N16R8)      │
   │  python:3.12-slim            │  control   │  firmware on the IDF v5.5.5    │
   │  one asyncio process         │  link      │  NimBLE peripheral             │
   │  macro + amiibo libraries    │ ◄════════► │  HID report task               │
   │  the retail key (in RAM)     │  USB 921600│  plan + tag in RAM             │
   └──────────────────────────────┘            └───────────────┬───────────────┘
                                                              │ console link
                                                              │ BLE, 5 ms
                                                              ▼
                                                    ┌──────────────────────┐
                                                    │  Nintendo Switch 2   │
                                                    └──────────────────────┘
```

**The device** is an ESP32-S3-N16R8: 16 MB flash, 8 MB octal PSRAM, an CH9102-class USB
bridge on `UART0`. It advertises as a Pro Controller 2, runs the console's pairing
handshake, and holds exactly one of three modes. It owns replay timing and nothing else.

**The container** is one Python process. It owns the macro library, the amiibo library, the
retail key, and all console-lifecycle policy. It compiles macros into plans, seals tags,
uploads both, and reports what the device says.

The split is deliberate and is the single most load-bearing thing in this design: the
device is dumb and opinionated about nothing, the container is opinionated and holds all
the state that matters (ADR-0003, ADR-0004, ADR-0011).

## 1.2 The two links

| | Control link | Console link |
| --- | --- | --- |
| Between | container ↔ device | device ↔ NS2 |
| Physical | USB serial, CH9102 bridge, `UART0` | BLE GATT |
| Speed | 921600 baud recommended (unbenched; 115200 fallback) | 5 ms connection interval, measured |
| Direction | container-led, one outstanding request | device is peripheral; the console drives |
| Payloads | framed verbs, bulk upload, multiplexed `ESP_LOG` | Pro2 GATT command/notify + HID input report `0x09` |
| Failure | device keeps running (ADR-0008) | device keeps running (ADR-0008) |

Neither link's loss changes the mode. Everything about what to *do* when a link drops is a
container decision (chapter 9).

## 1.3 The cast, and the words to use

- **Device** — the S3 and its firmware. Not "the board", not "the controller". The console
  is the thing that thinks it has a controller.
- **Container** — the host process. Not "the app", not "the server", not "the web UI" (the
  web UI is one of its surfaces).
- **Console** — the Nintendo Switch 2.
- **Control link**, **console link**, **bond**, **boot id** — see `CONTEXT.md`.

## 1.4 Modes

Exactly one of three, and `IDLE` is the *absence* of a mode rather than a third one:

| Mode | What the device is doing |
| --- | --- |
| `IDLE` | nothing: no plan replaying, no tag placed |
| `MACRO` | replaying the committed plan on a loop |
| `AMIIBO` | presenting one tag at a time to the console |

Modes cannot overlap, and the reason is physical rather than policed: HID input report
`0x09` byte `0x0C` is the console's view of NFC state, and it is `0x00` in `IDLE` and
`MACRO` and driven only in `AMIIBO`. There is one byte to express a mode, and the console
samples it every report.

## 1.5 What each half owes the other

The device owes the container: a ten-verb interface it can drive with one outstanding
request, a typed error on every rejection, a `STATUS` that never lies about the mode, and
a neutral report on every exit path.

The container owes the device: a compiled plan that fits and validates, a fully sealed tag,
a `HELLO` on every reconnect, and the discipline not to retransmit a verb that starts
something.

## 1.6 What is deliberately absent

No WiFi and no on-device HTTP server (ADR-0002). No OTA and no flashing over the control
link (ADR-0001). No authentication — the cable is the trust boundary. No telemetry push
channel; numbers come from a 2 Hz poll and edges from events. No controller recording, no
amiibo authoring, no NS1/BR-EDR support, no USB-wired controller emulation. The full
out-of-scope list is on map issue #1.
