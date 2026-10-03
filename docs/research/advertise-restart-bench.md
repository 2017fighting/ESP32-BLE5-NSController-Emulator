# Bench facts: the advertise-restart timer stack (issue #27)

**Scope:** the bench half of [Fix the gap.c timer-stack overflow](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/27)
— sleep/wake cycles, before and after counts, and the margin the fix relies on.
**Date:** 2026-10-03. **Hardware on test host:** ESP32-S3-N16R8, `MAC 84:fc:e6:58:57:88`
(advertising as `78:81:8c:4e:3c:9c`), paired to a real Switch 2 (`40:44:f7:2b:12:97`).

This records **observed facts**, per the ticket. Every number below comes from a capture run in
this session; the command or the derivation is given so it can be re-run. Anything not actually
run is in §8 and is not a result.

Raw captures are at `/tmp/ns27/session_*/{uart0,jtag}.log` on the bench host (ephemeral, not
committed; `/tmp/ns27/session_after_ab1c282*/uart0.log` alone is 8 MB, so the counts below are
the durable evidence).

---

## 1. The result the ticket asked for

| # | Image | Log path | `CONTROL_UART_BAUD` | disconnects | `Tmr Svc` overflows | `rst:0xc` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `5a85567` (pre-fix) | control-plane hook | 921600 (shipped) | 19 | **0** | **0** |
| 2 | `5a85567` (pre-fix) | control-plane hook | 115200 | 16 | **0** | **0** |
| 3 | `5a85567` + §3's probe | control-plane hook | 115200 | 6 | 0 | 0 |
| 4 | `7164f28` (pre-fix, base firmware) | IDF console, no driver | 115200 | 26 | **8** | **8** |
| 5 | `ab1c282` (the fix) | control-plane hook | 115200 | 22 | **0** | **0** |
| 6 | `ab1c282` + §3's probe | control-plane hook | 115200 | 14 | 0 | 0 |

All 103 disconnects across the six runs are `reason=531` (HCI `0x13`, the console ending it).

**Rows 1–3 are the finding.** The pre-fix callback does **not** overflow at `HEAD` — not at the
shipped 921600, not at 115200, in 41 disconnects. Row 4 reproduces the recorded defect exactly,
and it does so on the code the 8/19 figure came from (the base firmware), whose `ESP_LOG` went
straight to the ROM console on a UART0 with no driver installed. §3 gives the margin that
explains the difference, and §5 states what that means for the fix.

The ticket expected row 1 or 2 to reproduce `~8/19`. They do not, and §5 is the corrected
statement.

## 2. How each run was produced

**Condition.** Both trees build from the same DEBUG sdkconfig the 8/19 figure came from —
`CONFIG_MCU_DEBUG=y`, `CONFIG_LOG_MAXIMUM_LEVEL=DEBUG` — applied on top of the committed
`sdkconfig.defaults` + `sdkconfig.defaults.esp32s3`, with `CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH`
left at its default **2048**. The two sdkconfigs (before and after worktrees) are `diff`-clean
against each other, so the only variable between rows 1–3 and rows 5–6 is `gap.c`.

```sh
# per tree
cp -R managed_components dependencies.lock "$tree"/      # gitignored, so a fresh worktree lacks them
idf.py set-target esp32s3                                # generates sdkconfig from the defaults
#  then set CONFIG_MCU_DEBUG=y and CONFIG_LOG_MAXIMUM_LEVEL_DEBUG=y
idf.py build
idf.py -p /dev/cu.usbmodem5C930639851 flash
```

| Image | app `.bin` | SHA-256 |
| --- | --- | --- |
| `5a85567` pre-fix, `CONTROL_UART_BAUD=921600` | 553,760 B | `ee2c367b1b7c8c559fd1afea568ce43bb525d45ac928a4f914b97271c84bc60c` |
| `5a85567` pre-fix, `=115200` | 553,760 B | `83379fcd211193f7e2ead5c36fb541745412e898dde7d5490a7bf7e85d1b0514` |
| `ab1c282` fix, `=921600` | 554,096 B | `9fe6a29ab6c61416b3808ce2a70c2291588c92954448155413d7ca692d625553` |
| `ab1c282` fix, `=115200` | 554,096 B | `b6377af07fcac56b8251a92c95aec4ebac5fc91a2d96ef2faa51d405e5ca9922` |
| `7164f28` base firmware | 554,528 B | `53b230643923d4c7cb1d46fdfc86975581c4fa3f18c6ef66ea47812df886d126` |

**Capture.** Both console channels, concurrently, via `pyserial` with `dsrdtr=False, rtscts=False`
and DTR/RTS deasserted after open (ADR-0014):

- `/dev/cu.usbmodem5C930639851` — the CH9102 on **UART0**. The ROM/bootloader prints at 115200 and
  the control plane then reconfigures UART0 to `CONFIG_CONTROL_UART_BAUD`, so this stream is read
  at that rate: the boot segment arrives as noise and everything from `transport_init` onward is
  clean text. Row 4's base firmware never reconfigures UART0, so that capture is clean end to end
  and the `rst:0xc` line is readable on it.
- `/dev/cu.usbmodem1301` — the S3's own USB-Serial/JTAG. It carries the ROM console and is
  baud-agnostic, but on this build it produced **zero bytes** after the reset its own open
  triggers, so `rst:0x…` for rows 1–3, 5–6 comes from the panic text instead (§3).

**The cycles.** Opening a capture port pulses a chip reset, so the session starts from a fresh
boot; the console reattaches on its own within ~2.5 s off the firmware's wake advertisement. The
Switch 2 then **cycled sleep/wake by itself** — a single power-button press put it to sleep and it
subsequently woke (the board's `0x81` wake advertisement) and slept again on its own, every
~11–20 s, for the whole session. So the 19-cycle target was reached without per-cycle human
action, and the cycles are more uniform than the hand-driven ones in the original session.

## 3. The mechanism, measured rather than assumed

The 8/19 figure said the callback's stack usage "is at or over the 2048-byte limit and overflows
non-deterministically" (`ns2-console-lifecycle.md` §5.3). That is confirmed, and in the process
the reason rows 1–3 survive is visible.

**The overflow, reproduced verbatim (row 4):**

```
I (30245) ble_gap: disconnected, reason=531, restart advertising after 5s
I (33245) app: Setting manufacturer data for advertising
I (33245) app: Legacy advert
***ERROR*** A stack overflow in task Tmr Svc has been detected.

Backtrace: 0x40380931:0x3fca5d40 0x403808f9:0x3fca5d60 0x403819c2:0x3fca5d80 0x4038307b:0x3fca5e00 0x40381a88:0x3fca5e20 0x40381a7e:0x00000000 |<-CORRUPTED

Rebooting...
rst:0xc (RTC_SW_CPU_RST)
```

Same truncation mid-line (`Legacy advert`), same `vApplicationStackOverflowHook` →
`vTaskSwitchContext` backtrace, `+3.00 s` after the disconnect (the 3 s one-shot timer). All
eight are `+2.8–3.5 s` after their disconnect, i.e. all eight are the timer callback. Eight
crashes produced eight `rst:0xc` and eight forward boots.

**The margin (rows 3 and 6).** Both runs used the *same* 1 Hz probe, added only to `app_main`'s
idle loop — `uxTaskGetStackHighWaterMark(xTimerGetTimerDaemonTaskHandle())` — so the measured
frame (`restart_adv_timer_cb` → `ble_advertise()`) is untouched. `StackType_t` is one byte on
xtensa, so the reading is bytes:

| | `Tmr Svc` min-ever free stack (of 2048) | `restart_adv` min-ever free stack (of 4096) |
| --- | --- | --- |
| `5a85567` pre-fix (row 3) | 1316 B at boot → **52 B**, at the first timer-callback advertise, and never lower | — |
| `ab1c282` fixed (row 6) | **1172 B** — the callback no longer touches it | **2160 B** (and 2172 B in row 5, the fix's own `DEBUG` line) |

So the pre-fix callback consumes **1996 of the 2048 bytes** on `HEAD`'s log path, and the fix
gives back 2172 bytes on a task sized for it. The two tasks agree on the call depth — 1996 B on
the timer stack, ~1924 B on the new one — which is what the fix's "twice the old stack" sizing
argument assumed.

**Therefore the 8/19 rate is a property of the *log path*, not of `HEAD`.** The base firmware at
`7164f28` sent `ESP_LOG` straight to the ROM console (`TRANSPORT_LAYER_USB_CDC`; nothing installs
a driver on UART0), and that path is deeper than `HEAD`'s `control_link_vprintf` — which formats
into a 256-byte buffer and hands it to `uart_write_bytes` (§2.2). A 52-byte residue is what is
left when the shallower of the two runs the same callback; the deeper one goes over. *That
attribution is an inference from the two measurements, not a separately measured stack trace* —
what is measured is the 52 B, the 8/26, and the fact that the only difference between the two
overflowing and non-overflowing pre-fix builds is where `ESP_LOG` writes.

`CONTROL_UART_BAUD` is **not** a lever: rows 1 and 2 differ only in that, and neither tips. The
first hypothesis was that a 115200 write blocks ~8× longer on the timer task and so nests
interrupts deeper; 19 disconnects at 921600 and 16 at 115200 refute it.

## 4. The pairing trap, hit and resolved

The board booted paired (`g_adv_opcode = 0x81`) and wake-advertised to `40:44:f7:2b:12:97`, and
the console — sitting on the "更改握法/顺序" / change-grip-order screen — never reacted. Verified
on-air with a CoreBluetooth scan rather than inferred:

```
53 05 01 00 03 7e 05 69 20 00 01 81 97 12 2b f7 44 40 0f 00 …
                          ^^                          ^^^^^^^^^^^^^^^^^^
                          opcode 0x81                 console addr, LSB-first
```

Byte `[11] = 0x81` and `[12..17]` the bonded console, at RSSI −34 — the advertisement was
correct and the console was ignoring it. This is `ns2-console-lifecycle.md` §7's bring-up trap
("stale NVS pairing → wake-advertise `0x81` → invisible"), reached by a different route: the
grip-order screen lists controllers in **pairing** mode, and a bonded controller wake-advertising
into it is not one. Erasing the bond and letting the board advertise as unpaired fixes it:

```sh
python -m esptool --chip esp32s3 -p /dev/cu.usbmodem5C930639851 erase_region 0x9000 0x6000
```

The console paired on the first attempt afterwards and finished the whole `0x15` handshake
itself. **No app partition is touched, so a re-pair costs one grip-order visit and nothing else**
— worth knowing for the remaining bench tickets, since every flash that resets the board is one
more chance to land here.

## 5. What this settles for #27

- **The defect is real and reproduces exactly** on the code it was recorded on: 8 overflows and
  8 `rst:0xc` in 26 disconnects, every one at +3.00 s in the timer callback, with the recorded
  truncation and backtrace (§3).
- **The fix removes the work from that task**: 0 overflows and 0 `rst:0xc` in 22 sleep/wake
  cycles, with the new task's minimum-ever free stack at **2172 B of 4096** and the timer task
  back to 1172 B (§1, §3).
- **Corrected: at `HEAD` the defect is latent, not manifest.** The pre-fix callback leaves
  **52 B** of the 2048-byte timer task on `HEAD`'s log path and did not tip in 41 disconnects,
  at either baud. §7.6's "8/19" belongs to the pre-fix *console path*; the ticket's expectation
  that `5a85567` would reproduce it does not hold. 52 bytes of margin is still not a design — a
  callback that leaves 2.5% of its stack is one edit away from the same crash, and the fix is
  what makes the depth a budgeting decision instead of an accident — but the rate as recorded is
  not the rate at `HEAD`.
- The `gap.c` disconnect log string now reads 3 s, matching the 3000 ms timer; the stale "5s"
  text visible in row 4's log is the pre-fix build's.

## 6. Corrections owed

| Claim | Where | Status |
| --- | --- | --- |
| `5a85567` on the DEBUG sdkconfig will reproduce `~8/19` | the #27 ticket body | **Not reproduced.** 0/19 at 921600, 0/16 at 115200. Reproduced 8/26 on `7164f28` (§1, §3). |
| "the overflow rate is DEBUG-influenced" | `ns2-console-lifecycle.md` §5.3 | **Sharpened, and it is not the baud.** The lever is the *log path*: 52 B of margin on `HEAD`'s hook, over the limit on the base firmware's ROM console (§3). Baud was tested and is not a factor. |
| `CONFIG_CONTROL_UART_BAUD` might influence the rate through write-blocking | — | **New, refuted.** 921600 and 115200 both give 0 in the pre-fix image (§3). |
| The USB-Serial/JTAG port mirrors the console on this build | — | **New, refuted.** It carries the ROM/bootloader output at boot and then nothing, on both the base and the `HEAD` firmware; `rst:0x…` had to be read from the panic text instead (§2). |
| Stale NVS pairing makes the board wake-advertise and invisible | `ns2-console-lifecycle.md` §7 | **Reproduced by a second route** — the grip-order screen itself, not a different console. Fixed by erasing `0x9000`/`0x6000`; the board then pairs on the first try (§4). |

## 7. Consequences for the design

Nothing in the design changes: `boot_id` recovery stays (§2.8), because a reboot has causes this
callback never had. What the bench buys is that §7.6's claim is now a measurement on both sides —
0/22 with the fix against 8/26 on the pre-fix console path, with the margins in §3 — rather than a
landed patch with a plausible argument.

It also bounds the 8/19 figure honestly: that rate belongs to the base firmware's console path,
and quoting it as a `HEAD` rate would overstate both the defect's reach and the fix's effect.

## 8. Not attempted

- **The release build.** Every run here is `CONFIG_MCU_DEBUG=y` + `LOG_MAXIMUM_LEVEL=DEBUG`,
  because that is the condition the 8/19 figure came from. With the fix the question is moot for
  this callback — it no longer runs on the timer task at all (§3) — but no release-build cycle
  count was taken.
- **A `HEAD`-only reproduction.** No configuration of `5a85567` was found that tips the callback
  over; the reproduction is the base firmware (§1). Forcing it at `HEAD` (e.g. `-O0`) was not
  attempted, because the 52-byte margin already says the same thing without changing the
  optimisation level out from under the measurement.
- **The remaining bench tickets.** Nothing about #33 (baud), #34 (RX ring) or validations 3–7 was
  touched; no container was involved and no plan was uploaded.
- **The `ble_gap_update_params` `EINVAL`** (G-11, §7.6 defect 2) was left alone, per the ticket.
- **The queued-notification race in `gap.c`**, found while reviewing the fix: `xTimerStop` on
  `BLE_GAP_EVENT_CONNECT` cancels a *pending* timer but not a notification the callback has
  already given, so a reconnect inside the 3 s window can still let `restart_adv_task` call
  `ble_advertise()` against a live link. Pre-existing (the pre-fix callback had the same window,
  one scheduling hop narrower), harmless in the state it leaves (`device_status_set()` is a bare
  assignment, and `DEV_ADV_IND` is the value a normal connect expects), and **not hit in any of
  the 103 benched disconnects** — every reconnect came 3.6–11 s after its disconnect. It is
  deliberately **not fixed here**: `gap.c` is the file this ticket benched, and the commit must
  stay the artifact that was measured (`ab1c282`, `b6377af0…`). Filed as
  [gap.c: drain the queued advertise-restart notification on reconnect](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/41).
