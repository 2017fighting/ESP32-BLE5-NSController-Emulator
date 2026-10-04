# 7 · Firmware architecture

The firmware is the upstream `master` tree plus targeted additions (ADR-0005). This chapter
says what exists today, what the design adds, where the seams go, and the memory budget.

## 7.1 What exists today

Verified against the tree, not assumed:

| Area | File | State |
| --- | --- | --- |
| Transport seam | `main/include/transport/transport.h`, `main/src/transport/transport.c` | vtable (`open`/`close`/`activate_rx`/`submit_tx`/`flush_tx`/`is_ready`) with three backends: UART, USB-Serial-JTAG, USB-CDC. RX ring **256 B** (`transport.c:37`), TX ring 256 B |
| UART backend | `main/src/transport/transport_uart.c` | `UART_NUM_1`, GPIO4/5 (`transport.c:100-105`, `main/Kconfig.projbuild`), 115200, a 10 ms-poll RX task |
| USB-CDC backend | `main/src/transport/transport_usb_cdc.c` | no TX ring and no TX task; `zc_reset(tp->rx_buffer)` on re-attach (`:118-119`) |
| Protocol seam | `main/src/protocol/protocol_router.c`, `easycon/*` | two layers: `SIMPLE`, `EASYCON` |
| HID | `main/src/controller/hid_controller.c`, `hid_controller_pro2.c` | Pro2 report, front/back buffer, `controller_hid_commit` (`:233`), `pro2_report_init` (`:61`) — already the neutral template §4.6 needs; the `nfc_state` byte at `0x0C` is driven by `controller_ops_t.set_nfc_state` (#25) |
| Console protocol | `main/src/ns2_codec.c` | command handlers; `cmd_0x01_handler` routes `0x03`/`0x04`/`0x05`/`0x06`/`0x14`/`0x15` to the NFC tag server (#25) and keeps `0x0C` as its constant — the documented `61 12 50 0d` since bench #36's A/B | 
| NFC tag server | `main/src/controller/nfc_tag.c` | §4.9's state machine and §6.6's 540-byte byte-sink, portable and host-tested; the singleton, the report byte and `SCAN_ENDED` live in `control_parser.c` |
| NFC console-traffic trace | `main/src/protocol/control/nfc_trace.c` | §6.6's bench instrument (#36): the console's NFC exchanges with the CRC of what was served, accumulated in RAM and drained as `console nfc:` INFO at the scan's edge — never logged inline, so the ~9 round trips never pay for their own instrumentation |
| BLE | `main/src/gap.c`, `main/src/gatt.c` | NimBLE peripheral; GATT service with `0x000e` HID notify, `0x0014`/`0x0016` command write, `0x001a` notify |
| Flows | `sdkconfig:1761` `CONFIG_FREERTOS_HZ=100`; `main/Kconfig.projbuild` `HID_REPORT_INTERVAL` default 15, range 5–100 | |

The base is real on this hardware: it pairs with a real NS2, completes the console's
out-of-band handshake, and HID input reaches the console (verified by toggling a button on
the console's button-check page — `s3-bringup.md` §11.3).

## 7.2 The seam the control plane goes at

The control plane is **a third protocol layer on the transport vtable**, not a new transport
and not a rewrite of the router. That keeps the existing EasyCon path intact for anyone using
it, and it means the framing has exactly one implementation site on the device.

```text
        ┌── transport (vtable: open / activate_rx / submit_tx / flush_tx) ──┐
        │  uart(now UART0)        usb_cdc            usb_serial_jtag        │
        └───────────────────────────────┬───────────────────────────────────┘
                                        │  byte stream (plus ESP_LOG noise)
                        ┌───────────────┴────────────────┐
                        │  protocol_router               │
                        │   ├── SIMPLE                    │
                        │   ├── EASYCON                   │
                        │   └── CONTROL  ← new            │
                        └───────────────┬────────────────┘
                                        │
                    ┌───────────────────┼────────────────────┐
                    │                   │                    │
              control verbs      plan executor        nfc tag server
              (chapter 2)        (chapter 5)          (chapter 6)
```

The transport's byte stream carries both framed protocol traffic and `ESP_LOG` text;
separating them is the CONTROL layer's job (§7.3), not the transport's. That is the seam
discipline ADR-0006 implies: the framing knows about the noise, the transport does not.

## 7.3 The firmware changes, in dependency order

1. **A UART0 transport instance.** `transport_uart` is hardcoded to `UART_NUM_1` on GPIO4/5.
   The control plane needs `UART0` — the CH9102 bridge — where `ESP_LOG` already lives
   (`sdkconfig:1585`). Parameterise the port (or add a second instance) and **keep the RX
   ring at 256 B or raise it deliberately** — the measurement says keep it: the 256 B ring
   absorbs the §2.7 window at the design baud with margin (§7.5); the 100 Hz tick means a
   burst larger than the ring is data loss, not backpressure (§7.5).
2. **A shared TX lock, and logging through it.** `ESP_LOG` and control replies must not
   interleave (§2.2). Route log output through a hook that takes the same lock as reply
   transmission, and **suppress device logging to a bounded rate while a bulk transfer is
   active**. This is the single most subtle firmware requirement in the design, and getting it
   wrong costs retransmits rather than correctness.
3. **The CONTROL protocol layer**: COBS decode/encode, CRC-16 check, frame dispatch, the ten
   verbs, and the canonical `ERROR` table. It parses `HELLO` tolerantly and everything else
   strictly (§2.8).
4. **The state model**: the five axes of chapter 4, the transition table, the panic-stop
   reader (a 100 Hz `gpio_get_level(GPIO0)` poll with ≤ 30 ms debounce and edge-triggered
   stop), and the `STATUS`/`EVENT` surface of chapter 3.
5. **The plan executor**: a task that walks plan frames against the tick clock, holds each
   for its `hold_ms`, calls `controller_hid_commit`, and on every exit path emits the neutral
   template (§4.6). It owns the loop-boundary neutral and the `LOOP_COMPLETED` rate limit
   (§3.3).
6. **The NFC state machine and tag server** — **done with #25.** `main/src/controller/
   nfc_tag.c` drives HID report `0x09` byte `0x0C` (§4.9, now `nfc_state`) and answers
   `0x01/0x03`, `0x01/0x04`, `0x01/0x05`, `0x01/0x06`, `0x01/0x08`, `0x01/0x14`, `0x01/0x15`.
   The device is a byte server: it slices a RAM buffer and does no crypto (ADR-0011) — 60 bytes
   of framing plus the 540-byte image, served 70 bytes at a time, with the `0x14` stream staged
   and `0x08` committing it (§6.6, §7.7, #46/#47). The module is portable C, so the
   placement/unplacement/gap ordering and the served-space slice are asserted on the host
   (`test/host/test_nfc_tag.c`); `control_parser.c` owns the singleton, the report write and the
   `SCAN_ENDED` event, and `ns2_codec.c` routes the subcommands. The console-facing offset space
   of `0x14`/`0x15` is the arithmetic of §6.6 (`image = wire − 0x3C`), and the device serves it
   as of #46; only its out-of-range case is still validation 5's question.
7. **`CONFIG`**: `report_interval_ms` and `led`, volatile, applied at the boundary §2.9 fixes.
8. **The Direction byte is already handled — no decision is left here.** `cmd_process()` flips
   response byte 1 from `0x91` to `0x01` centrally (`main/src/ns2_codec.c:656-658`), for
   **every** command it dispatches, so the NFC probe `0x01/0x0C` and the calibration read
   `0x02/0x04` are rewritten identically and the console accepts both — 140/140 calibration
   reads on the bench. There is no asymmetry to preserve or design around, and the NFC
   subcommands of step 6 inherit the correct byte by routing through the same path. What was
   recorded as **G-14** was a misreading of the handler-versus-payload split and is retired
   (§12.3, `ns2-0x01-0c-asymmetry.md` §3).

Nothing in this list needs a new transport, a new radio, or a filesystem.

## 7.4 The plan buffer, the tag buffer and the memory budget

| Consumer | Size | Lifetime |
| --- | --- | --- |
| Plan staging buffer | exactly the announced `LOAD_PLAN` length, up to `CONFIG_PLAN_CAPACITY_BYTES = 65536` | allocated on announce, freed on discard, replacement, or commit-then-replace |
| Plan replay structure | the committed buffer itself (no copy) | until discarded or superseded |
| Tag staging buffer | 540 B | until the commit copies it (§4.3) or a panic stop forgets it |
| Committed tag buffer | 540 B | the placement's own bytes; until replaced or the long panic stop |
| Served tag buffer (`nfc_tag_t`) | 540 B | the volatile copy the console reads; a `0x14` write-back lands here and is discarded on unplace (§6.5) |
| HID report front/back buffers | 63 B each | process lifetime |
| Control-layer frame buffers | decoded `max_frame` (512 B) plus the COBS block and encoded wire form, up to `max_frame + max_frame/254 + 3` ≈ 517 B per direction (§2.2) | process lifetime |
| NFC chunk buffer | one `0x15` response, ~75 B | process lifetime |
| NFC trace ring (`nfc_trace_t`) | 16 × ~100 B ≈ 1.7 KB | process lifetime; one scan's worth of distinct exchanges, drained and reset at each scan's edge (#36) |

**`plan_slots = 1`, so the worst single allocation is 64 KiB**, and it fits internal SRAM
without PSRAM. Two measured facts bound the risk:

- **The committed base firmware is 553,504 B in a 3 MB `ota_0` slot — 82% free**
  (`s3-bringup.md` §11.1). The code budget is not tight, so the plan and tag buffers are the
  only allocations worth counting.
- **The largest real plan is 3,356 B** (§5.7), so the common case is three kilobytes, not
  64 KiB. `plan_capacity_bytes` is the ceiling, not the expectation.
- **The tag's three 540-byte buffers are 1,620 B in total** — all statically allocated, none
  per-transfer, and all dwarfed by the plan ceiling. The split exists so a `0x14` write-back
  cannot corrupt the placement's own bytes (§6.5): the served copy is the console's to
  scribble on, the committed copy is the container's.

**PSRAM is present on this board and deliberately not enabled.** Nothing yet allocates from
it, and an earlier revision that turned it on was reverted as unprompted scope creep with
boot-loop risk. **It goes on together with the first real PSRAM user, verified against the
board** — and the plan buffer is not that user, because 64 KiB is not worth a boot risk.
*(If a future user needs the full 64 KiB under pressure from the tag buffer and the framebuffers,
that is the moment PSRAM gets enabled and measured, not before.)*

## 7.5 Timing: the tick grid, and why the report interval is not a millisecond knob

`CONFIG_FREERTOS_HZ=100` (`sdkconfig:1761`) makes `vTaskDelayUntil` quantise to 10 ms ticks,
so `CONFIG_HID_REPORT_INTERVAL` is not a millisecond control
(`ns2-console-lifecycle.md` §4.3):

| Setting | Ticks at 100 Hz | Effective delay |
| --- | --- | --- |
| 5 ms | 0 | **0 ms — the task spins** |
| 10 ms | 1 | 10 ms |
| 15 ms | 1 | **10 ms** |
| 20 ms | 2 | 20 ms |
| 25 ms | 2 | 20 ms |

Consequences the design takes as given: a **5 ms report period is unreachable without raising
`CONFIG_FREERTOS_HZ`**, a 5 ms setting is worse than useless, and the plan format's
millisecond holds (ADR-0009) are what keep playback speed independent of all of this.

**And on a DEBUG build the report rate is set by the UART log budget, not by any of it.**
152 B of log per report against 11,520 B/s caps the rate at 75.8/s; 74.4/s was observed — 98%
of that ceiling. **Any latency measurement from a DEBUG-logged run is a logging floor**, so
timing work runs with `CONFIG_LOG_MAXIMUM_LEVEL=INFO` and the per-notification logs off. An
`INFO` build reaches **10.00 ms — 100.0 notifications/s — measured**, 32% above that ceiling,
which is what makes the numbers below a measurement of the report period rather than of the log
(`macro-timing-bench.md` §2).

**The report period against the plan's holds, measured** (validation row 3, G-16;
`macro-timing-bench.md` §2). With a real console connected and subscribed — its own link measured
in the same capture at **5.00 ms** (`conn_itvl=4`) — a plan's holds are delivered as holds on the
report grid: a 50 ms hold reads 49.98 ms, a 10 ms hold 10.01, a 120 ms hold 119.96, and a hold that
is not a multiple of the period reads as the two neighbouring grid values (25 ms alternates
20.2/30.2) — the *error is bounded by one period and never accumulates*: over 9–54 loops the mean
loop period is the plan's `loop_ms` + ≤ 1.1 ms, with ±1 tick of per-loop jitter. That is ADR-0009's
absolute-deadline walk visible in the data.

Two consequences the measurement settles, and the condition that would reopen the 5 ms question:

- **The neutral's handoff costs exactly one report period, per loop boundary.** §4.6/§5.4 make
the boundary commit the neutral and wait for the reporter to take it; measured, that wait is
9.93–9.99 ms mean (min 9.60, max 10.03) over 1–55 waits — the arm's plus one per boundary — and it
appears as the +0.2…+1.1 ms on the mean loop period, never as drift. With **no console subscribed
it is zero**: the same plans measure `loop_ms` exactly, min = mean = max (a 190 ms loop reads 190.0
on all 26 loops).
- **A state held for less than the report period can be overwritten before the reporter samples
it, and is then never on the wire.** Delivery of the executor's input changes, measured: **100%**
at a 50 ms hold and at 25 ms (99.1–100% across runs), on a real 71-record library macro's first 34
records, and **100%** on a 360 ms loop of 120 ms holds — against **90.1%** at a 10 ms hold and
**5.1%** at 5 ms. A 10 ms period therefore carries any macro whose holds are much wider than the
period, which is every macro in the pinned library (median hold 21–120 ms); **holds at or below the
period are the case that would justify raising `CONFIG_FREERTOS_HZ`**, and that is a macro-authoring
limit rather than an executor defect — the executor walks a 5 ms plan exactly (95.2 ms loop,
`loop_ms` + 0.2) and it is the report that cannot carry it. ADR-0003's `hold_ms` keeps that fork to
a clock change and nothing else.

The device prints this run's meter as one INFO line per section when the mode exits
(`macro meter: applied=… changes=… inputs=… notified=…`, then the exact delta extremes, the handoff,
the loop periods, and the newest 64 inputs with their intervals), never per report — for the same
reason the staging-close line is printed once above. The console's own connection interval is
logged on the same `control` tag from `gap.c`'s connect handler, because validation 3's comparison
needs both halves in one INFO capture (`main.c` raises no other tag above `WARN`).

**The 100 Hz tick against the §2.7 ACK window, measured** (validation row 2, `rx-ring-bench.md`).
The 256 B RX ring **absorbs the 4096 B window at the design baud with the window and ring
both unchanged: 0 ACK stalls and 0 untrusted frames across every transfer to the 65,528 B
capacity maximum**, on both host stacks — the macOS native path and the OrbStack-forwarded
container path — with the real library's largest plan (杏仁巢穴宏, 3,356 B) among the rows.
The tick's contribution is invisible because the line is the throttle: at 115200 the wire
carries 11.5 KiB/s, below the device's ≈14–20 KiB/s drain, and the two rings behind the tick
quantisation (the 256 B zc ring the parser drains and the 256 B driver ring behind it) ride
out every window — the measured margin is a **driver-ring backlog high-water of ≤ 248/256 B**.
Two facts the measurement added to the design's picture: a near-full zc ring is the blast's *steady
state*, not a hazard (one 256 B chunk frame out-sizes the ring, and the producer's
`pdMS_TO_TICKS(1)` is **0 ticks** at 100 Hz — a yield, not a block); and at 921600 the same
instrument reads the cliff directly — hundreds of frames the decoder could not trust per
large transfer and the driver backlog at its full 256/256 B. The device prints the per-transfer
meter as one INFO line when staging closes (`staging closed: …`), which is why the `control`
tag is the one INFO the deployment build does not silence.

## 7.6 The base firmware's disconnect path: one defect fixed, one tolerated

**1 · Fixed: the advertise-restart callback overflowed the timer task stack.** `gap.c` created
a one-shot 3 s `xTimerCreate` on disconnect whose callback called `ble_advertise()`, which
composes `ESP_LOGI` format strings and a 30-byte buffer on a task whose stack is
`CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH = 2048` **bytes** (`sdkconfig:1778`; not set in any
committed defaults file, so it is the IDF default). It fired 19 times in one session and
aborted with `rst:0xc` **8 times** (`ns2-console-lifecycle.md` §5.3).

**The tempting fix is the wrong one.** Raising `CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH` is one
line, and it is wrong twice over: the timer-service task's stack is shared by *every* FreeRTOS
timer callback in the firmware, so the constant has to cover the heaviest callback anyone ever
adds, in code that has nothing to do with advertising; and the actual defect is that
`ble_advertise()` — which composes log format strings — has no business running on a callback
task at all. **The fix is to move that work off the timer task**, which is local to the file
that owns it, and the stack depth stays at its default.

**The fix landed with [Fix the gap.c timer-stack overflow](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/27).**
The 3 s one-shot timer stays — it is the cancellable delay (`xTimerStop` on reconnect) and is
good at it — and its callback now only `xTaskNotifyGive`s a dedicated 4096-byte `restart_adv`
task that calls `ble_advertise()`. That is twice the 2048-byte timer-service stack it replaces,
and the size the firmware's other NimBLE-calling tasks use (`controller_task`; the NimBLE host
task's own default), because the call depth is shared with them even though the stack is not.
The task logs its minimum-ever free stack at `DEBUG`, so the margin the fix relies on is a
measurement rather than an assumption. **The bench re-run landed** (`advertise-restart-bench.md`
§1, §3): **0 overflows and 0 `rst:0xc` in 22 sleep/wake cycles**, against
**8 overflows and 8 `rst:0xc` in 26** on the pre-fix firmware, and the new task's minimum-ever
free stack is **2172 B of its 4096**.

**The bench also corrects the ticket's expectation, and this is the part worth reading.** The
pre-fix callback does *not* reproduce at `HEAD`: with the same `ble_advertise()` still on the
timer task, the timer service task bottoms out at **52 B free of 2048** — measured with a probe
in `app_main` that never touches the callback's own frame — and it did not tip in **41
disconnects**, at 921600 or at 115200. The 8/19 rate belongs to the base firmware's console path,
which wrote `ESP_LOG` straight to the ROM console on a driverless UART0 and is deeper than the
log hook of §2.2; that path overflows 8 times in 26. So at `HEAD` the defect is **latent rather
than manifest**: 52 bytes is still not a design — a callback that leaves 2.5% of its stack is one
edit away from the same crash, and the fix is what turns that depth into a budgeting decision —
but a `HEAD` rate quoted as 8/19 would overstate both the defect and the fix.

This mattered because it made **a device reboot an ordinary event**, which is why the
container's recovery story keys on `boot_id` (§2.8) and why nothing about a reboot may be
treated as exceptional. **That stance does not change with the fix**: a reboot can have causes
this callback never had, and the device's
statelessness (ADR-0004) is what makes the recovery safe either way.

**2 · `ble_gap_update_params` fails on every connection, harmlessly.** `gap.c:82-83` sets
`itvl_min = 6` with `itvl_max = desc.conn_itvl`, an inverted range, and the call returns
`BLE_HS_EINVAL` every time — it never reaches any floor check
(`ns2-console-lifecycle.md` §4.2). The link still runs at `conn_itvl = 4` (**5 ms**) because
that value is the console's *inbound* request, which passes no validation on this path.

The design does not depend on the sub-spec work at all, but the reason is worth recording so
nobody "fixes" the call into something load-bearing: **the peripheral receives the interval
inbound; the host-side gate would only matter if the device initiated** (`host-stack-subspec-intervals.md`).
The build requirement stands regardless — **ESP-IDF `v5.5.5`** (the released floor; v5.5.4
does not contain the symbol and the failure is *silent*, `s3-bringup.md` §11) with
`CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y`, and **`patch_nimble_lib.py` must not be run
for S3** — it is C6/C61-only.

## 7.7 The BLE host stack, and the console-side air-time budget

**The host stack stays NimBLE.** An early research claim that Bluedroid is required for
sub-spec connection intervals does not appear in any Espressif source; it was a researcher's
inference presented as a quotation, and it is wrong. The Kconfig `select` *is* satisfied by
`BT_NIMBLE_ENABLED`, but the symbol it selects is consumed **only by Bluedroid** — it would
have been a no-op for NimBLE. NimBLE needs no relaxation because a **peripheral receives the
interval inbound**: the console as central sends the connection update and
`ble_gap_rx_update_complete` stores it unchecked. The one gate is host-initiated
`ble_gap_update_params()`, and this repo already sits at or above the spec minimum there
(§7.6). **No migration to Bluedroid, and no `patch_nimble_lib.py` on S3.**

**The console-side budget, which the tag path depends on.** Espressif's caution about large
payloads at sub-spec intervals applies to full-length 251-octet DLE frames, not to this
traffic:

| Setting | Value | Why |
| --- | --- | --- |
| Data Length Extension | **enabled** | disabling it caps LL PDUs at 27 bytes and fragments each 81-byte `0x15` response across three connection events, tripling latency |
| ATT MTU | **≥ 84**; 128–256 a safe target | a `0x15` response is 8 + 3 + 70 = **81 bytes** (§6.6), and a notification carries `MTU − 3`; below an MTU of 84 it is segmented |
| Chunk size | 70 bytes | the console's read granularity in the capture — the request at offset `0x46` had already consumed one 70-byte chunk (§6.6) |
| The whole tag read | **9 round trips ≈ 90 ms** | 600 served bytes at 70 per exchange; ~15% of a 5 ms window per exchange; comfortably inside the interactive window and against ~1.5 ms of host-side re-sealing (§6.4) |

So the 540-byte tag path and the 5 ms link are compatible, and the constraint is real but not
binding at these sizes. **This is a firmware configuration requirement, not an optimisation:**
an MTU that segments the response silently costs a third of the throughput and nothing reports
it.

## 7.8 The storage partition, unowned

`partitions_16mb_s3.csv` reserves **~10 MB** of `spiffs` named `storage`, and nobody owns it.
The design cannot quietly drop it or quietly keep it:

- It must **not** hold the macro library, the amiibo library (ADR-0004 — those live in the
  container), or `key_retail.bin` (ADR-0012 — the key never reaches the board).
- Dual OTA slots exist in the layout and **OTA is out of scope** (ADR-0001), so nothing uses
  `ota_1` either.

**Decision: keep the partition table as measured, and record the reservation as unclaimed.**
It stays because the layout is verified, flashing with it works, and shrinking it buys nothing
today; it is *unclaimed* rather than *reserved for a feature* because no feature has claimed
it. If the implementation effort needs neither OTA nor storage, **dropping both is the
honest move** — this is the one place the spec leaves a table row deliberately unused, and
§12.3 carries it as G-3 rather than as a promise.
