# Bench facts: NS2 console-side lifecycle (issue #14)

**Scope:** what a real Switch 2 does across sleep, wake, the grip-order screen and a
link drop, observed against the S3 running the committed base firmware.
**Date:** 2026-09-30. **Hardware:** ESP32-S3-N16R8 on `/dev/ttyACM0`, real NS2.

This records **observed facts, not conclusions**, per the ticket. Every number below
comes from a capture of the board's own log during a live bench session; the capture
files and the analysis method are given so it can be re-run. Anything the firmware
cannot exercise is in §6 and is **not** a result.

The ticket asks six questions. Three of them are answerable with the base firmware and
are answered in §3–§5. Three presuppose behaviour the firmware does not implement, so
they are unobservable rather than unanswered — §6 says which, and why.

---

## 1. Setup, and one correction to the record

| Fact | Value | How |
| --- | --- | --- |
| Board | ESP32-S3 (QFN56) rev v0.2, 16 MB flash, 8 MB octal PSRAM | boot log |
| MAC | `84:fc:e6:58:57:88` | boot log / `esptool chip_id` |
| Bridge | CH9102-class `1a86:55d3` → `/dev/ttyACM0` | `lsusb`, `udevadm` |
| Firmware | `7164f28-dirty`, built Sep 30 2026 20:05:36 | boot log |
| ELF SHA-256 | `4e93655ff2e70ead…` (matches `build/*.elf`) | boot log vs `sha256sum` |
| IDF | v5.5.5 | boot log |
| Board address | `78:81:8c:…` (public) | `Setting manufacturer data` log |
| Console address | `40:44:f7:2b:12:97` | `connected, set nintendo switch addr` log |

The flashed image is `7164f28` plus an uncommitted `sdkconfig`, not `HEAD`. `HEAD`
changes only docs and three `main/` lines relative to `7164f28`:

```
main/include/controller/hid_controller_pro2.h | 2 +-
main/src/gatt.c                               | 6 +++---
main/src/ns2_codec.c                          | 2 +-
```

**The `sdkconfig` is the important caveat, and it is not the committed one.**
`sdkconfig` is git-ignored (`.gitignore:49`), so the build under test carries local
settings the repo does not describe:

| Setting | Committed `sdkconfig.defaults(.esp32s3)` | Build under test |
| --- | --- | --- |
| `CONFIG_MCU_DEBUG` | unset | `y` |
| `CONFIG_LOG_MAXIMUM_LEVEL` | (default INFO) | `4` (`DEBUG`) |
| `CONFIG_HID_REPORT_INTERVAL` | 15 | 15 |

Everything in this document therefore describes **a DEBUG-log build of `7164f28`**.
No source change was made for this session; nothing was re-flashed except the NVS erase
in §2.

**The DEBUG build is a confound, and one measurement is affected by it.** At default
`CONFIG_LOG_MAXIMUM_LEVEL=INFO` the per-notification logs in §4.3 do not exist; with
`DEBUG` they do, and they are written synchronously to UART 0 at 115200 baud
(`CONFIG_ESP_CONSOLE_UART=y`, `CONFIG_ESP_CONSOLE_UART_BAUDRATE=115200`; v5.5.5 has no
deferred/background log mode). Measured over the parked phase, the captured log stream
is **11,376 B/s against a 11,520 B/s link — 99% of UART capacity.** A synchronous
`ESP_LOG` that cannot drain blocks its caller, so on this build the HID task's own
logging is a plausible throttle on the report rate. §4.3 states what is solid from
source and marks what is not attributable. The crash in §5.3 is a second, independent
effect of the same DEBUG build: it is a stack-size interaction the log formatting makes
visible, so its *rate* should not be assumed to hold for a release build either.

### 1.1 Capture method

Opening `/dev/ttyACM0` resets the board (the DTR→GPIO0 / RTS→EN wiring of
`s3-bringup.md` §10), so the session was one continuous capture rather than many short
ones:

```python
s = serial.Serial('/dev/ttyACM0', 115200, timeout=0.2, dsrdtr=False, rtscts=False)
s.setDTR(False); s.setRTS(False)          # asserted either one holds the S3 in reset
```

Each reported line is stamped `[t=+<seconds>]`. The board's own `ESP_LOG` timestamps
(`D (10305)`) are the authoritative device clock and are used for any rate below.

**The capture logs are bench-local and are not committed** (an 11 MB serial dump does
not belong in the repo). The numbers in this document are reproducible from the
commands given inline — in particular, rates come from the device's own `(\d+)`
millisecond timestamps, and this one-liner reproduces the per-notification interval
histogram of §4.3:

```sh
# FROZEN.log = the raw capture, one line per reported line
grep 'Notification sent, handle=0x000e' FROZEN.log \
  | sed -E 's/.*\(([0-9]+)\).*/\1/' \
  | awk 'NR>1{print $1-p} {p=$1}' | sort -n | uniq -c | sort -rn
```

### 1.2 How the session actually started — a stale bond, cleared

The first two captures (`session.log`, `session2.log`) recorded **zero** connections
over ~13 minutes while the console sat at the grip-order screen. The cause was not the
console:

```
I (405) app: device already paired.
I (465) app: Injecting LTK for peer addr:
I (465) app: 40:44:f7:2b:12:97            <- the stored peer; the console no longer holds this bond
I (475) app: Setting manufacturer data for advertising
I (475) app: Legacy advertising started (ADV_IND)
```

`device_info_init()` succeeding sets `g_adv_opcode = 0x81` (`main/src/device.c`), which
makes the board emit the **Wake Console** advertisement and carry the stored peer
address in manufacturer data bytes `0x0C..0x11`
(`switch2_controller_research/bluetooth_interface.md:22-44`). That advertisement is
addressed at a console which (per the operator) had no pairing record for this board,
so nothing answered it; and a wake advertisement is not a discovery beacon either, so
the board was invisible to the grip-order screen while its log said only "advertising".

Erasing NVS (`esptool erase_region 0x9000 0x6000`) made the board boot with no stored
peer and advertise normally; the console then found it and paired. **This is a bring-up
trap, not a defect in the firmware** — the board was faithfully wake-advertising for the
peer it had been told about. It is recorded because it cost the session ~20 minutes and
will recur for anyone pairing this board to a second console.

The erase was not surgical, and that is worth knowing before anyone repeats it. NVS also
held the **controller's own Bluetooth address**, which `pro2_addr_init()` generates once
and persists (`NVS_KEY_CTRL_ADDR`), so the board came back as a *different* controller:

| Capture | Board address | Stored LTK |
| --- | --- | --- |
| `session.log`, `session2.log` (before erase) | `78:81:8c:05:97:71` | `550cf828…` |
| `session3.log` / `FROZEN.log` (after erase) | `78:81:8c:4e:3c:9c` | `1e4af72e…` |

**A caveat on what this session did and did not capture.** The pairing itself happened
in the gap between the erase and the start of the captured run: the capture's very first
boot already logs `device already paired.`, and no boot in it shows `Failed to get LTK`.
So the address and LTK change above is read across two captures, not observed as a
pairing event, and **the pairing handshake's command sequence is not in this session's
data**. What *is* captured is every subsequent reconnect (§3.1). The paired state is
stable and reproducible (`1e4af72e…` survives 8 reboots unchanged), which is the part
§3 and §5 depend on.

---

## 2. What was run

A single continuous capture, with the operator driving the console:

| Phase | Operator action | Capture window |
| --- | --- | --- |
| Bonded start | capture begins; board already paired from before the run | `t=+0.6`–`+19.2` |
| Parked | console held at the **grip-order screen** | `t=+19.8`–`+625.9` (≈10 min) |
| Cycle | console slept and woken **repeatedly** | `t=+625.9`–`+910` |

The mid-session console operations are the operator's, not the firmware's; the device
side is entirely passive here (there is no control-plane client attached, and the
firmware has no macro or amiibo mode — §6).

The parked phase is one uninterrupted link: 45,091 input notifications at 74.4/s over
605.7 s, largest gap 0.20 s. The cycle phase covers `t=+625.9`–`+910` and contains 17
connections and 17 disconnects; the other 3 connections and 2 disconnects fall in the
opening phase, for 20/19 over the captured session.

---

## 3. Pairing and reconnection

### 3.1 The full initialisation sequence is re-run on every connection

The console re-sends its whole init on each connect, not a reduced reconnect subset.
Measured command sequence, first **captured** connection (offsets relative to
`connected`):

```
  +400ms  0x07/0x01     0x02/0x04     0x10/0x01     0x16/0x01
  +601ms  0x0a/0x02     0x09/0x07     0x0c/0x02     0x02/0x04  (x4)
  +801ms  0x11/0x03     0x02/0x04
 +1001ms  0x0a/0x08   ...
```

Per connection over the whole session (20 connections):

| Observation | Count |
| --- | --- |
| `connected, set nintendo switch addr` | 20 |
| `disconnected, reason=531` | 19 |
| `subscribe event` on `0x001a`/`0x001e`/`0x0022` (command-response CCCDs) | 20 each |
| `subscribe event` on `0x000e` (HID input) | 19 |
| `controller report task start` | 19 |
| command `0x02/0x04` (flash read) | 140 (~7 per connection) |
| command `0x15/*` (pairing) | **0** |
| `pling complete event` (NimBLE logs "paring" for `BLE_GAP_EVENT_PARING_COMPLETE`) | 20 |

**Command `0x15` is never sent again after the initial pairing**, over 20 reconnections.
This confirms `switch2_controller_research/bluetooth_interface.md:418` ("`0x15`
commands are only used during initial pairing and are omitted on reconnection") from the
console side, and it means the reconnect path does not re-run the pseudo-OOB key
exchange — the bond in NVS is what carries.

Also note that the ticket's own notation needs care: it asks whether the console
"re-sends `0x03/0x01` (start NFC polling)". Canonically **`0x03/0x01` is Bluetooth
Wake** (`switch2_controller_research/commands.md:229,244-250`), while **NFC polling is
`0x01/0x03`** (`switch2_controller_research/commands.md:61`; see also
`ns2-amiibo-path.md` §2 Q1). Neither was observed: the session contains **zero**
`0x03/0x01` and **zero** `0x01/0x03`. The Bluetooth-Wake half is answered (§5.2); the
NFC-polling half is not, for the reason in §6.

### 3.2 The console re-subscribes HID itself — by CCCD restore, not by rewriting

The `0x000e` subscription arrives in one of two ways, and the firmware's
`BLE_GAP_EVENT_SUBSCRIBE` handler logs which:

| `reason` (NimBLE `BLE_GAP_SUBSCRIBE_REASON_*`) | Meaning | Occurrences (`0x000e`) |
| --- | --- | --- |
| `1` (`WRITE`) | peer wrote the CCCD | 8 |
| `2` (`TERM`) | cleared on disconnect | 18 |
| `3` (`RESTORE`) | **restored from the bond** | 11 |

So on most reconnects the console does **not** write the CCCD again; NimBLE restores the
persisted subscription from the bond and the firmware's handler runs anyway, starting
the HID report task (`gap.c`: `attr_handle == 0x000e && cur_notify == 1 → start_task`).
The report stream resumes with **no container command and no peer action** beyond the
reconnect itself.

**This is the answer to "does the report stream resume without a container command":
yes.** The firmware's own `subscribe`-driven task start is sufficient, and it fires
identically for a fresh CCCD write and for a bond restore.

One clarification against §5.3, because the two can look contradictory: the resumption
described here happens **on connect**, while the reboot in §5.3 happens on a **later
disconnect**. Per connection the order is connect → subscribe → HID task start → ~10 s of
reports → disconnect → (sometimes) reboot. The stream does resume every time; the
reboot is the *next* teardown, not a failure to resume.

### 3.3 Reconnect latency

Disconnect → next `connected`: **3.6 s min, 4.4 s median, 11.4 s max** (n=19). The
firmware's own advertise restart is on a 3 s timer (§5.3), so the observed latency is
that timer plus the console's scan interval.

---

## 4. The link, and two corrections to #2

### 4.1 The link runs at 5 ms — `conn_itvl=4`, on every connection

| Observation | Value |
| --- | --- |
| `conn_itvl` at connect, all 40 samples | **`4`** (= 5.0 ms) |
| `BLE_GAP_EVENT_CONN_UPDATE` events | **0** |
| `failed to update connection parameters` (from `gap.c`) | 20 of 20 |

This **corrects `s3-bringup.md` §11.4**, which measured `conn_itvl=12` (15 ms) with zero
update events and left the 5 ms question open. On this session the console set 5 ms
immediately on every connect, including the very first one, before any controller
command had been exchanged.

**The console was never observed at 15 ms in this session.** A candidate explanation for
the difference is that §11.4's run was fighting the mbuf exhaustion bug fixed in
`7164f28`; whatever the cause, the map's open question "why does the console never
request 5 ms" should be re-read as "it does request 5 ms, at least from a
freshly-bonded controller with the msys fix in place".

### 4.2 `gap.c`'s `ble_gap_update_params` call fails on every connection, harmlessly

`main/src/gap.c` asks for `itvl_min = 6, itvl_max = desc.conn_itvl`; with
`desc.conn_itvl = 4` this is an inverted range and the host rejects it:

```
E (2235) NimBLE: ble_gap_update_params rc=3        <- BLE_HS_EINVAL
E (2235) ble_gap: failed to update connection parameters, rc=3
```

`ble_gap_validate_conn_params()` (`ble_gap.c:8971`) rejects `itvl_min > itvl_max` first.
So the call that `host-stack-subspec-intervals.md` flags as "the only gate" is failing
**before** it reaches any interval floor, on every connection. The observed 5 ms link
is therefore the console's own value, not anything this call negotiated — which
strengthens that doc's conclusion (the peripheral receives the interval inbound) with
a cleaner observation: the host-side call is not merely unnecessary, it is a no-op
error in the common case.

### 4.3 The report rate is set by the UART log budget, and the interval setting is quantised to ticks

**Solid from source.** `CONFIG_HID_REPORT_INTERVAL=15` with `CONFIG_FREERTOS_HZ=100`
(`sdkconfig:1761`) gives `pdMS_TO_TICKS(15) == 1` tick = **10 ms**, and
`vTaskDelayUntil` can only delay whole ticks:

| Setting | Ticks at 100 Hz | Effective delay |
| --- | --- | --- |
| 5 ms | 0 | **0 ms** (spins) |
| 10 ms | 1 | 10 ms |
| 15 ms | 1 | **10 ms** |
| 20 ms | 2 | 20 ms |
| 25 ms | 2 | 20 ms |

So `CONFIG_HID_REPORT_INTERVAL` is **not a millisecond control**, and the sub-10 ms
periods a 5 ms link would want are unreachable without raising `CONFIG_FREERTOS_HZ`.
A 5 ms setting is worse than useless here: it converts the wait into a `0`-tick delay
and the task spins.

**Observed on this build, and explained by the UART budget.** Measured intervals:

| Interval | Count | Share |
| --- | --- | --- |
| 10 ms | 38,780 | 65.9% |
| 20 ms | 19,976 | 33.9% |
| 30 ms | 117 | 0.2% |
| 40 ms | 6 | <0.1% |

A 10/20 ms mix is what a 10 ms loop looks like when it wants 100/s and gets ~74/s. The
cause is the logging, and the arithmetic is close to exact:

| Quantity | Value |
| --- | --- |
| log bytes emitted per successful report (`notify_tx` + `Notification sent`) | 152 B |
| UART capacity at 115200 8N1 | 11,520 B/s |
| ⇒ maximum report rate the log stream permits | **75.8/s** |
| observed rate | **74.4/s** = **98%** of that ceiling |
| rate a true 10 ms period needs | 100/s ⇒ 15,200 B/s = **132%** of UART |

So the loop is not failing to keep its 10 ms cadence; it is being held at the rate its
own logs can drain. The 10 ms and 20 ms gaps carry **identical** log volume (median 153 B
either way), which rules out "the 20 ms gaps are expensive reports" and leaves the
blocking write as the explanation. **The durable finding is that on a DEBUG build the
report rate is set by the UART log budget, not by `CONFIG_HID_REPORT_INTERVAL` or the
link.** Any latency figure taken from a DEBUG-logged run — including §11.4's and this
document's — is a floor set by logging. Re-measuring the true rate needs
`CONFIG_LOG_MAXIMUM_LEVEL=INFO`, which was not done in this session.

### 4.4 The grip-order screen keeps draining — the msys guard never engages

`s3-bringup.md` §11.5 reproduced the README's "HID reports stall in the grip-order
screen" bug and fixed it with a 40-block headroom guard in `gatt_notify()`. Over the
whole session, including ~10 minutes parked on that screen:

| Observation | Count |
| --- | --- |
| `Failed to allocate mbuf` | **0** |
| `msys low, dropped …` (the guard firing) | **0** |
| `controller report send failed` | **0** |
| notifications delivered on `0x000e` | 58,903 |

**The console drains continuously while parked at the grip-order screen.** The guard
never fired and no allocation ever failed across ~10 minutes of parking, so the console
is *not* refusing to drain — the parked link carried 45,091 notifications with a largest
gap of 0.20 s and no back-pressure. This is a stronger result than §11.5's: that section
showed the guard prevents the stall, this one shows that in normal parked operation
there is no back-pressure to guard against.

**What the HID stream itself looks like while parked.** Every notification is the same
63-byte Pro2 input report (`Notification sent, handle=0x000e, len=63`, 58,903 of 58,903).
The firmware's `hid_report_pro2_t` is `static_assert`ed to 63 bytes
(`main/include/controller/hid_controller_pro2.h:90`) and the only field that changes is
`counter` (`pro2_next_report()` increments it on each send); everything else stays at
its initialised value. With no control-plane client attached there is no input source,
so the reported state is the **Neutral** report — buttons released, both sticks centred
(`pro2_report_init()`). Byte `0x0C`, the NFC state, is the field documented as the tag
indicator (`switch2_controller_research/hid_reports.md:178`) and is hardcoded `0x00`.
The console draining a stream of identical-neutral 63-byte reports is therefore exactly
what "parked" looks like on this build.

Two caveats, stated rather than glossed. First, the **absolute** rate (74.4/s) is
subject to §4.3's UART confound; the *absence* of back-pressure is not, because the
msys guard is a free-block check independent of how fast the reports flow. That said,
the confound does bound what can be concluded: because synchronous logging held the
rate to ~74/s, this session cannot show whether a *faster* report stream would have
driven msys into the guard. "No back-pressure at ~74/s" is the claim, not "no
back-pressure at any rate". Second, whether the console was *parked* in the sense #2
meant — the specific screens that reproduced the original stall — is not distinguishable
from this log alone. The operator reports the grip-order screen, and that screen drained.

---

## 5. Sleep, wake, and the disconnect path

### 5.1 The console drops the link on sleep, and it is the console that ends it

Every one of the 19 disconnects carries the same reason:

| `reason` | NimBLE decode | Occurrences |
| --- | --- | --- |
| `531` | `BLE_HS_ERR_HCI_BASE (0x200) + 0x13` = **HCI `0x13` Remote User Terminated Connection** | 19 |

The console initiates the teardown; the controller never times out. **This differs from
`s3-bringup.md` §11.5**, where the observed reason was `520` (HCI `0x08`, connection
timeout) because the firmware had stopped sending. With the msys fix in place, the
teardown seen here is the clean console-initiated one and HCI `0x08` does not appear at
all.

**Answering the ticket's "does the device's `ble_gap` disconnect path behave as
`s3-bringup.md` records": partly, and the one place it does not is the important one.**
The recorded path is `BLE_GAP_EVENT_DISCONNECT` → log the reason → schedule the 3 s
advertise restart → stop the HID task, and all of that happened as recorded. What
§11.5 does **not** record is that the scheduled restart's callback **crashes the device
8 times in 19** (§5.3) — because that section's session ended at the first reconnect
and never exercised repeated disconnects. So the disconnect path behaves as recorded on
its first occurrence and diverges under repetition; the divergence is a new finding,
not a contradiction of the recorded one.

The teardown ordering is consistent across cycles: the console clears the CCCDs
(`reason=2` on `0x000e`, `0x001a`, `0x001e`, `0x0022`) and then the link drops; the
firmware logs `disconnected, reason=531, restart advertising after 5s` and schedules its
advertise restart. **The log string says "after 5s" but the timer is 3000 ms**
(`main/src/gap.c:80`, `pdMS_TO_TICKS(3000)`), and the measured delay is 3.00 s in every
case — so the string is stale, not the behaviour. Worth noting because a reader
timing a recovery from the log text would be wrong by 2 s.

### 5.2 Wake: the console reconnects and re-subscribes on its own

There is **no `0x03/*` command anywhere in the session** — neither `0x03/0x01`
(Bluetooth Wake, `switch2_controller_research/commands.md:229,244-250`) nor `0x03/0x02`
(Bluetooth Cancel, `switch2_controller_research/commands.md:231,259`) — and no `0x01/0x03`
(NFC poll start) either. Over 19 wake/reconnect cycles the console never asked the
controller to start or stop advertising. It reconnects to the already-advertising board
and re-runs init (§3.1).

The pieces of the resume, all observed per §3.1–3.2:

1. console reconnects with no controller action;
2. command-response CCCDs re-subscribed (write or bond restore);
3. `0x000e` HID subscription reinstated (8 writes, 11 restores);
4. firmware starts the HID report task from its own `subscribe` handler;
5. notifications resume at the §4.3 rate with no container involvement.

**Step 5 is the one the ticket asks about and the answer is "nothing is re-sent"**:
the console does not re-issue a "start polling" style command on wake, and the
firmware needs no state machine of its own beyond the CCCD it already tracks.

### 5.3 Every wake works; roughly half are preceded by a firmware reboot

This is the session's significant defect finding, and it is intermittent.

| Observation | Count |
| --- | --- |
| disconnects | 19 |
| disconnect followed within 3.0 s by a **timer-task stack overflow** | **8** |
| `rst:0xc (RTC_SW_CPU_RST)` reboots | 8 |

The crash is deterministic in its trigger and timing:

```
I (619483) ble_gap: disconnected, reason=531, restart advertising after 5s
...
I (622493) app: Setting manufacturer data for advertising
I (622493) app: Legacy advertising s
***ERROR*** A stack overflow in task Tmr Svc has been detected.
Backtrace: 0x40380931 0x403808f9 0x403819c2 0x4038307b 0x40381a88 0x40381a7e
Rebooting...
rst:0xc (RTC_SW_CPU_RST)
```

Decoded with `xtensa-esp32s3-elf-addr2line` against the flashed ELF:

```
0x403819c2: vApplicationStackOverflowHook   freertos/portable/xtensa/port.c:573
0x403808f9: esp_system_abort                esp_system/port/esp_system_chip.c:87
0x40380931: panic_abort                     esp_system/panic.c:477
0x4038307b: vTaskSwitchContext              freertos/tasks.c:3698
```

The crashed call is `ble_advertise()` on the timer service task, whose stack is
`CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH = 2048` bytes (`sdkconfig:1778`; the value is
in bytes — `StackType_t` is `uint8_t` on xtensa,
`portmacro.h:88`). That task is a deliberately small, callback-agnostic stack, and
`ble_advertise()` composes `ESP_LOGI` format strings and a 30-byte adv-data buffer on
it.

**Why only 8 of 19 is *not* established, and the tempting explanation is wrong.** The
timer is one-shot (`pdFALSE`), created on the first disconnect and `xTimerReset` on each
one, and it fires at **+3.00 s after every one of the 19 disconnects** — the reconnect
always lands later (3.6–11.4 s, §3.3), so `xTimerStop` in `BLE_GAP_EVENT_CONNECT` is a
no-op on an already-expired one-shot and **nothing ever cancels it**. Since the callback
runs 19 times and aborts 8 times, the split is not "which disconnects reached the
timer".

The log gives a consistent hint and no proof: the 8 aborts are cut off **mid-line**
(`Legacy advertising s`, `Legacy advertising started (A`), i.e. the overflow is detected
*while* that `ESP_LOGI` is being written, whereas the 11 survivors complete
`Legacy advertising started (ADV_IND)` and continue. That is what a stack sitting right
at its limit looks like — the same code path tips over or not depending on incidental
depth (interrupt nesting, printf state) rather than on anything this log records.
Uptime at disconnect does not separate the two groups (means 105 s vs 21 s, but purely
because one survivor had been up 619 s; pairs at ~13 s and ~29 s go both ways). **The
defensible statement is: the callback's stack usage is at or over the 2048-byte limit
and overflows non-deterministically at a rate of 8/19 in this session.** The exact
tipping factor is not recoverable from the log and was not pursued.

Consequences for the design, stated as facts rather than decisions:

- **The device reboots on a substantial fraction of sleep/wake cycles** — 8 reboots in
  19 cycles here. Q9 says the device is stateless beyond pairing keys, so a reboot is
  survivable (the bond is in NVS and the board rejoins), but every reboot re-runs the
  boot log, resets the HID task, and changes `boot_id`: a container watching `boot_id`
  for the #5 recovery flow would see a device restart mid-session, and that is now a
  behaviour it has to tolerate rather than a hypothetical.
- **This is independent of the console and of this ticket's questions.** It is a
  defect in the base firmware, present at `7164f28` and unchanged at `HEAD`, owned by
  whoever owns `gap.c`. Flagged here, not fixed here.

The rate is DEBUG-build-influenced (the `ESP_LOGI` format-string work is part of what
consumes the stack) but the fragility is structural: a callback this heavy does not
belong on the timer task at any log level. **The overflow was not re-measured on a
release build**, so 8/19 is a DEBUG-build figure and the underlying margin — not the
rate — is what generalises.

---

## 6. What the console cannot be asked with this firmware

The ticket's remaining three items need behaviour the flashed firmware does not have.
This is not "untested": the firmware cannot present the condition, so the console can
never be observed responding to it.

| Item | Why it is unobservable |
| --- | --- |
| **Mid-MACRO reconnect** — does a pass resume at its current frame? | There is **no MACRO mode**. `grep -ri macro main/` matches only `uthash.h`; there is no plan format, no replay task, no `START`/`STOP`. Nothing executes, so there is no pass to interrupt, and the "stray input into a freshly-connected console" question has no subject. |
| **Console absent** — is a macro running into a sleeping console harmless? | Same: nothing executes, so there is no pass to run into an absent console. The device's own "survives a console drop" property (#6) is a statement about a mode this build never enters. |
| **Amiibo unplace / per-scan freshness** | There is **no AMIIBO mode**. `cmd_0x01_handler` handles only subcmd `0x0C` (the PN7160 probe) and returns 0 for everything else; `unknown_0x0c` in `hid_report_pro2.h` is fixed `0x00`, so the console can never learn a tag exists. |

The last one has a directly observed corroboration, and it is worth recording because
it is exactly the fact #9 leans on:

- The console **did** probe NFC. On 19 of 20 connections it sent
  `01 91 01 0c 00 00 00 00` — that is command `0x01`, subcommand `0x0C`, the capability
  probe (`ns2-amiibo-path.md` §2 Q6). The firmware answered, and the relationship held.
- The console **never** sent any other NFC subcommand. Across the whole session the only
  `0x01` subcommand seen was `0x0c`, **19 times, and `0x03` (start polling) zero times**.

So on this evidence the console probes for NFC capability once per connection and then
waits. It does not begin polling, and it cannot begin polling, because the firmware
never signals a tag (§2's hardcoded `0x0c = 0x00`). **The console's polling state is
consequently unknown from this session** — the `.nfp` service is never entered, so
neither "does it re-send `0x03/0x01` on wake" nor "does the unplace have to be
observable" can be answered. #9's freshness mechanism is not falsified by this; it is
also not confirmed. Both remain open.

### 6.1 What would have to exist first

For the record, because it changes who this ticket is blocked on: answering the three
unobservable items needs, at minimum, the same two pieces of base firmware that the map
has not built yet:

- a **MACRO** mode that replays a compiled plan on a loop (the mid-macro and
  console-absent items), and
- an **AMIIBO** mode that serves `0x01/0x03`, `0x01/0x05`, `0x01/0x15` and drives
  input-report byte `0x0C` from `0x01` to a tag-detected value (the amiibo item).

Neither is in scope for a design-lock map (Q1). The honest state is that these three
items are **bench-blocked on implementation**, and the implementation is a later
effort's, not this map's.

---

## 7. Corrections owed

| Claim | Where | Status |
| --- | --- | --- |
| The console never requests 5 ms; link observed at `conn_itvl=12` (15 ms) with zero update events | `s3-bringup.md` §11.4 | **Superseded.** 40/40 samples read `conn_itvl=4` (5 ms); 0 CONN_UPDATE events needed. |
| Disconnect reason `520` (HCI `0x08` connection timeout) is what a link drop looks like | `s3-bringup.md` §11.5 | **Narrowed.** With the msys fix, all 19 drops are `531` (HCI `0x13`, console-initiated). `0x08` did not occur once. |
| Grip-order screen stalls HID because the console stops draining | `s3-bringup.md` §11.5, `README.md:158` | **Not reproduced** on this build. ~10 min parked at 74.4/s, 0 msys pressure, guard never fired. The fix works; the stall was not reproducible to guard against. |
| `ble_gap_update_params(itvl_min=6, …)` is the only gate on the interval | `host-stack-subspec-intervals.md` | **Sharpened.** It fails `BLE_HS_EINVAL` (`itvl_min > itvl_max`) on every connection and never reaches the floor check; the 5 ms link is the console's inbound value. |
| `CONFIG_HID_REPORT_INTERVAL` sets the report period in ms | `s3-build-and-link.md`, `main/Kconfig.projbuild` | **Qualified.** At `CONFIG_FREERTOS_HZ=100` it rounds to the tick grid (`15 → 10 ms`, and `5 → 0`); the observed 74.4/s is 98% of the 75.8/s ceiling the DEBUG log stream imposes on UART, so the true rate was not measured. |
| *(new)* A DEBUG-logged run's report rate is set by the UART log budget, not by the interval setting | — | **New.** 152 B of log per report against 11,520 B/s ⇒ 75.8/s ceiling; observed 74.4/s. Latency figures from DEBUG runs are logging floors. |
| *(new)* A 3 s advertise-restart timer callback overflows the timer-service task stack on ~half of slow reconnects | — | **New defect.** `gap.c` → `vApplicationStackOverflowHook`, `rst:0xc`, 8/19 disconnects. Present at `7164f28` and `HEAD`. |
| *(new)* Stale NVS pairing makes the board wake-advertise (`0x81`) and invisible to a different console | — | **Bring-up trap.** `ble_advertise()`'s opcode logic; cost ~20 min, fixed by erasing NVS. |

---

## 8. What this settles, and what it does not

**Settled for the container's console-lifecycle policy** (#6 left this to the container,
#14 was to supply the facts):

- A console sleep **does** drop the link, and it is the console that ends it (HCI `0x13`).
- On wake the console reconnects, re-runs its full init, and re-subscribes HID — by
  fresh CCCD write **or by bond restore**, with the firmware starting the report task in
  either case. Nothing is re-sent and no container command is needed.
- Command `0x15` is never re-sent after pairing; the bond carries the reconnect.
- The console does **not** re-issue `0x03/0x01` (Bluetooth Wake) on wake.
- The grip-order screen drains continuously; the device's own msys guard is not a
  normal-operation path.
- The link runs at 5 ms from the first connection.

**Not settled, and now known to be blocked on implementation rather than on the bench:**

- whether a macro pass resumes at its frame or must restart after a link drop;
- whether a macro pass is harmless when it runs into an absent console;
- whether per-scan freshness requires an observable unplace through the PN7160 path.

**Also worth flagging for the spec (#10), found incidentally:** the device reboots on
approximately half of slow sleep/wake cycles via the `gap.c` timer stack overflow. Even
with Q9's stateless design that is a user-visible restart and a `boot_id` change, so
the container's recovery story has to tolerate it whether or not the defect is fixed.
