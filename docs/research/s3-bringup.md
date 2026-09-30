# Bench facts: S3 master bring-up (issue #2)

**Scope:** host-side bring-up of `master` for `esp32s3` on the ESP32-S3-N16R8.
**Date:** 2026-09-30. **Hardware on test host:** none (see "Not attempted").

This records **observed facts, not conclusions**, per the ticket. Every number
below comes from a command run in this session; the command is given so it can
be re-run. Anything not actually run is in "Not attempted" and is not a result.

---

## 1. What could not be attempted

> **UPDATE 2026-09-30 (same day).** An ESP32-S3-N16R8 was attached after §1–§8
> were written, and the ticket's steps 3–4 **were** then carried out against a
> real Switch 2. The results — including a hard blocker and a 15 ms (not 5 ms)
> connection interval — are in **§11**. §1–§8 below are kept as written, as the
> record of what was knowable before the board arrived. §10's DTR/RTS trap was
> hit for real during §11's work.

The ticket's steps 3 and 4c ("pair with a real Switch 2", "observe HID report
interval / console-side rejection") are physical bench work. At the time of
writing they were **not attempted** and are **not** claimed.

Facts about the test host:

| Fact | Observed value | How |
| --- | --- | --- |
| Only serial device present | `/dev/ttyUSB0` | `ls /dev/ttyACM* /dev/ttyUSB*` |
| `/dev/ttyACM*` (the S3 USB-CDC control-plane port) | **does not exist** | same |
| Chip behind `/dev/ttyUSB0` | `ESP32-D0WDQ6-V3` (original ESP32, rev v3.1) | `esptool.py --port /dev/ttyUSB0 chip_id` |
| Bridge | `CP2102` (`10c4:ea60`) | `udevadm info -q property -n /dev/ttyUSB0` |
| MAC | `fc:e8:c0:9e:04:74` | `esptool.py chip_id` |

So the attached board is **not** the S3-N16R8. It is an original ESP32 devkit,
whose chip has BR/EDR and no USB-OTG, and which is in the map's out-of-scope
set ("NS1 / Joy-Con / NS1 Pro Controller emulation … the S3 has no BR/EDR").
**No S3 build was flashed anywhere.** The following sections are build-time
facts only.

Consequently still unknown, and what they block:

- observed HID report interval against a real console;
- whether `CONFIG_HID_REPORT_INTERVAL=15` coexists with a 5 ms link;
- the grip-order / firmware-update-nag behaviour the README describes;
- whether PSRAM works on this board at all (not attempted; PSRAM is left
  disabled, see §5, item 2).

---

## 2. Toolchain

This host had no usable ESP-IDF: `~/esp/v5.4.1` predates the 5 ms fix, and
`export.sh` refused to activate because its virtualenv is `*-py3.12_env` while
the host `python3` is 3.14.7.

Installed for this work:

| Item | Value |
| --- | --- |
| ESP-IDF | `v5.5.5` at `~/esp/v5.5.5` (tag `v5.5.5`, commit `b774170f`) |
| Python | 3.12.14 via `mise` (`~/.local/share/mise/installs/python/3.12`) |
| Compiler | `xtensa-esp-elf-gcc 14.2.0` (`esp-14.2.0_20260121`) |
| Activation helper | `~/esp/idf-env-5.5.5.sh` |

The helper exists because `export.sh` needs a Python 3.12 on `PATH`:

```sh
source ~/esp/idf-env-5.5.5.sh   # prepends python 3.12, then sources export.sh
```

Also cloned `~/esp/v5.5.4` for the comparison in §3 (not activated for the
project build).

---

## 3. The 5 ms requirement: the released-IDF floor is **v5.5.5**, not v5.5.4

`docs/research/s3-build-and-link.md` claims the sub-spec Kconfig symbol is
available in **v5.5.4+** and **v5.4.4+**. Both claims are **wrong**. Primary
sources, checked directly:

`config BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE` is declared in
`components/bt/controller/esp32c3/Kconfig.in` — a file `components/bt/controller/esp32s3/Kconfig.in`
sources wholesale, so the C3 file *is* the S3 file. Presence of that symbol by tag:

| Tag | Symbol present |
| --- | --- |
| `v5.4.0` … `v5.4.6` | no (all of them) |
| `v5.5.0` `v5.5.1` `v5.5.2` `v5.5.3` `v5.5.4` | **no** |
| `v5.5.5` | **yes** |
| `v6.0.0` `v6.0.1` | no |
| `v6.0.2` `v6.0.3` | yes |

Reproduced with a loop over tags fetching
`raw.githubusercontent.com/espressif/esp-idf/<tag>/components/bt/controller/esp32c3/Kconfig.in`
and counting matches for `MIN_CONN_INTERVAL_ENABLE`. Local confirmation:
`grep -c MIN_CONN_INTERVAL_ENABLE ~/esp/v5.5.4/.../Kconfig.in` → no match;
`~/esp/v5.5.5/...` → line 575.

The fix commit the doc cites (`cf133455b6573b6fd015ac8e1db32b8a2ac69a9a`,
2026-05-13) is **not** an ancestor of the `v5.5.4` tag: comparing `v5.5.4` to
the doc's cited commit reports the commit is 752 commits *ahead*. The doc read
`release/v5.5` at a later date and attributed it to the `v5.5.4` tag.

**This failure mode is silent, which is the dangerous part.** On v5.5.4 the
assignment does not error; it emits a Kconfig warning and vanishes:

```
warning: unknown kconfig symbol 'BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE' assigned to 'y'
```

after which the symbol is absent from `sdkconfig` and not `#define`d in
`build/config/sdkconfig.h`. The build still succeeds. The firmware then ships
with a 7.5 ms floor and rejects the console's 5 ms request at run time. Verified
in a throwaway copy at `/tmp` (since removed) with `idf.py set-target esp32s3`
on v5.5.4.

Binary-level confirmation that the *controller* really changed between the two
tags — `nm` over the extracted `libbtdm_app.a` for `esp32s3`:

| Symbol | v5.5.4 | v5.5.5 |
| --- | --- | --- |
| `ble_min_conn_interval_enable` | absent | **defined** |
| `ble_con_interval_min` | absent | **defined** |

The controller hook is guarded the same way:

```c
#if CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE
    ble_min_conn_interval_enable(3); // 3 * 1.25 = 3.75ms
#endif
```

(`~/esp/v5.5.5/components/bt/controller/esp32c3/bt.c:1352`)

### Patch step

**No patch was applied, and none is needed.** `patch/patch_nimble_lib.py`
rejects `esp32s3` outright and is C6/C61-only. On v5.5.5 the symbol alone is
sufficient at build level — `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` is
resolved in `sdkconfig`, and it transitively selects the host-side relaxation:

```
CONFIG_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT=y   # sdkconfig:680
CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y      # sdkconfig:1046
```

That second symbol is what the fixed NimBLE consumes, relaxing its host-side
floor from `0x0006` to `0x0001`:

```c
#if defined(CONFIG_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT) && \
    (CONFIG_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT)
#define BLE_HOST_CONN_PARAM_ITVL_MIN        (0x0001)
#else
#define BLE_HOST_CONN_PARAM_ITVL_MIN        BLE_HCI_CONN_ITVL_MIN
#endif
```

(`~/esp/v5.5.5/.../nimble/include/nimble/hci_common.h:1836`)

**Whether the 5 ms interval is actually held on the air is NOT established.**
That needs §1's hardware. What is established is that the release *contains the
mechanism* and this build *enables* it.

### Unverified code finding: `main/src/gap.c` asks for the wrong interval

`main/src/gap.c:54` hardcodes `params.itvl_min = 6` with the comment
"ESP-IDF 5.5.3, maybe esp-idf support min connection interval". On v5.5.5 this
clamps the host's own request to 7.5 ms. With the relaxed floor the host would
now accept `itvl_min` down to `0x0001`.

**This is not a bench fact and is not a defect claim** — NS2 is the central and
sends the connection update itself, so this host-side call may be irrelevant to
the observed interval. Confirming which it is requires the hardware from §1
(read back `conn_itvl` in the `BLE_GAP_EVENT_CONNECT` log). Flagged here only
because it is the one place in `main/` that touches the interval, and the
research doc (`docs/research/host-stack-subspec-intervals.md`) asserts this call
is the *only* gate.

---

## 4. Build results

Exact commands, in `ESP32-BLE5-NSController-Emulator`:

```sh
source ~/esp/idf-env-5.5.5.sh
idf.py fullclean
idf.py build
python scripts/package_firmware_v5.py
```

| Fact | Value |
| --- | --- |
| Build result | `EXIT=0`, "Project build complete" |
| Compiler warnings | **0** |
| App binary | `0x86260` = 549,472 B |
| Smallest app partition | `0x300000` = 3 MB; `0x279DA0` (83%) free |
| Bootloader | `0x5160` B; 36% free |
| Merged artifact | `release/ns-controller-esp32s3n16.bin`, 680,544 B |

The app is nowhere near the partition ceiling, so the ticket's "does the app
still fit" is settled: **yes, with ~83% of a 3 MB slot spare.**

### 8 MB vs 16 MB, measured

The 16 MB change was made per research-doc recommendation, so here is what it
actually buys. Built the unmodified `HEAD` tree separately:

| | `HEAD` (8 MB, `partitions_n2c.csv`) | This branch (16 MB, `partitions_16mb_s3.csv`) |
| --- | --- | --- |
| Flash size | `8MB` | `16MB` |
| App partition | `factory`, 1500 K = `0x177000` | `ota_0`, 3 MB = `0x300000` |
| Smallest app partition | `0x177000` | `0x300000` |
| Free in it | `0xF0DA0` (64%) | `0x279DA0` (83%) |
| Present in binary | — | `ota_data_initial.bin` |
| Layout | `nvs` 24K, `phy_init` 4K, `factory`, `storage` 512K | `nvs` 24K, `otadata` 8K, `phy_init` 4K, `ota_0`, `ota_1`, `storage` 10048K |
| Storage partition | 512 K (farm of ~950 amiibo dumps would not fit) | 10048 K |
| Packaging | `ns-controller-esp32s3n8.bin` | `ns-controller-esp32s3n16.bin` |

Neither layout fails the build today; the 8 MB one is simply sized for the
wrong module. The 16 MB one is what was flashed-compared, so the difference is
sizing, not correctness.

### Partition table as burnt

`python $IDF_PATH/components/partition_table/gen_esp32part.py build/partition_table/partition-table.bin`:

```
nvs,data,nvs,0x9000,24K
otadata,data,ota,0xf000,8K
phy_init,data,phy,0x11000,4K
ota_0,app,ota_0,0x20000,3M
ota_1,app,ota_1,0x320000,3M
storage,data,spiffs,0x620000,10048K
```

`gen_esp32part.py` prints "Verifying table..." and exits 0, which is its
alignment/overlap check. Total is 15.9375 MB of 16 MB.

Note `build/flash_args` carries `--flash_mode dio`: the module's QIO mode is
**not** enabled, matching `HEAD` and left alone deliberately (§5).

### Resolved S3 configuration

```
CONFIG_ESPTOOLPY_FLASHSIZE="16MB"
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions_16mb_s3.csv"
CONFIG_TRANSPORT_LAYER_USB_CDC=y
CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y
CONFIG_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT=y
```

PSRAM is **not** enabled — see §5, item 2.

---

## 5. Configuration changes

`partitions_16mb_s3.csv` (new) and three edits to `sdkconfig.defaults.esp32s3`:

1. **16 MB flash + custom OTA table** — as recommended by the research doc.
   Low risk; all offsets are declared explicitly and the table verifies.
2. **PSRAM: considered, then deliberately left off.** An earlier revision of
   this branch enabled `SPIRAM=y`, `SPIRAM_MODE_OCT=y`, `SPIRAM_SPEED_80M=y`.
   That was **reverted**, for two reasons. Nothing in `master` allocates from
   PSRAM yet, so it buys nothing today; and if the mode were wrong for the
   actual module the board would boot-loop, which is precisely the failure that
   cannot be diagnosed without hardware. `sdkconfig.defaults.esp32s3` now
   carries only a note; turn PSRAM on in the same commit as the first real
   PSRAM user, verified against the board.
   Separately, and this one is a doc error: the research doc's
   `CONFIG_SPIRAM_TYPE_ESP32S3_OCTAL` and `CONFIG_SPIRAM_SIZE` **do not exist**
   as Kconfig symbols in v5.5.5; `CONFIG_SPIRAM_MODE_OCT=y` is what selects the
   octal part.
3. **Removed `CONFIG_BTDM_CTRL_MODE_BLE_ONLY/BR_EDR_ONLY/BTDM`.** These were
   warning on every build *before* this change — they are declared only in the
   esp32-only `components/bt/controller/esp32/Kconfig.in` and are unknown on S3:

   ```
   warning: unknown kconfig symbol 'BTDM_CTRL_MODE_BLE_ONLY' assigned to 'y' in .../sdkconfig.defaults.esp32s3
   ```

   All three are no-ops for this target. Removing them takes the build from 3
   warnings to 0 with no symbol change. This also explains the dead comment
   `# Test on ESP32-C61-DevKitC-1-N8R2` at the top of an S3 file.

**Left alone, deliberately:**

- `CONFIG_ESPTOOLPY_FLASHMODE` stays `dio`. The N16R8 module supports QIO and
  `EasyMCU_ESP32S3`'s known-good `sdkconfig.n16r8` sets `QIO=y`, but that is a
  different board with a different flash part. Changing flash mode without the
  hardware is the same class of risk as §5, item 2, with no upside.
- `CONFIG_HID_REPORT_INTERVAL=15`, which the research doc flags as mismatched
  against a 5 ms link. It is a one-line change with no defensible value until
  the interval can be observed on the wire (§1).

---

## 6. Toolchain gotcha: two packaging scripts, only one works

`scripts/package_firmware.py` and `scripts/package_firmware_v5.py` differ by one
character in a regex, and only `_v5` works with a current esptool:

| Script | Regex | Result here |
| --- | --- | --- |
| `package_firmware.py` | `--flash-size` | `Error: Could not extract flash size from flash_args` |
| `package_firmware_v5.py` | `--flash_size` | success |

`build/flash_args` emits `--flash_size` (underscore), so the unsuffixed script
cannot match. Both return exit code 0 on that failure, which also makes it
easy to miss in CI. Not fixed here — it is outside issue #2 — but the recipe
must name `_v5`.

---

## 7. Corrections owed to `docs/research/s3-build-and-link.md`

Recorded rather than edited, since #4 is closed and this is a research artifact:

| Doc claim | Status | Primary source |
| --- | --- | --- |
| Available in **v5.5.4+** | **wrong** — v5.5.4 lacks the symbol; floor is **v5.5.5** | Kconfig.in per tag (§3) |
| Available in **v5.4.4+** | **wrong** — not in any v5.4.x checked (`.0`–`.6`) | same |
| `v6.0.2+` | correct | same |
| `CONFIG_SPIRAM_TYPE_ESP32S3_OCTAL` / `CONFIG_SPIRAM_SIZE` | **do not exist** in v5.5.5 | `grep "config SPIRAM_SIZE" components/esp_psram/` |
| "`CONFIG_..._ENABLE=y` is **completely sufficient**" | true at build level; **unverified on air** | §3 |
| Recommended app size 1.8–2.2 MB | measured **0.52 MB** today | §4 |

Its PSRAM recommendation is **not** taken: see §5, item 2, for why that block is now
commented out rather than enabled.

The v5.5.4/v5.4.4 errors are the same failure Q17 warns about in the map
("never propagate a secondary summary"): `release/v5.5` is not `v5.5.4`.
The build-recipe section should pin **v5.5.5**.

---

## 9. The real board: hardware facts

An ESP32-S3-N16R8 was attached after §1–§8 were written. Master was built and
flashed onto it. Everything below is read off the board, not from a datasheet.

| Fact | Value | How |
| --- | --- | --- |
| Chip | ESP32-S3 (QFN56), rev **v0.2** | `esptool ... chip_id` |
| MAC | `84:fc:e6:58:57:88` | same |
| Flash | **16 MB**, `1c:7118`, quad, 3.3V, set in eFuse | `esptool ... flash_id` |
| PSRAM | **8 MB octal** (`AP`, gen 3, 64 Mbit), works | board boot log |
| USB bridge | CH9102-class `1a86:55d3` → `/dev/ttyACM0` | `lsusb`, `udevadm` |
| Firmware replaced | `esp32-joycontrol` (ESP-IDF v5.4.1, `057e:2009`) | board boot log |

Three things this settles that §4–§5 could only guess at:

1. **The board really is 16 MB flash + 8 MB octal PSRAM.**
2. **The module is QIO-capable and the previous firmware booted QIO at 80 MHz**
   (its bootloader logged `SPI Mode: QIO`, `Enabling default flash chip QIO`,
   `Boot SPI Speed : 80MHz`). So the `CONFIG_ESPTOOLPY_FLASHMODE=dio` choice
   left in §5 is **conservatism, not a board limit** — QIO is now testable.
3. **PSRAM works, but the previous firmware ran it at 40 MHz**, not the 80 MHz
   the research doc suggested. Relevant when PSRAM is eventually enabled (§5,
   item 2).

Master's own build affects none of this: after flashing, the boot log reads
`flash io: dio`, and no PSRAM is initialised, exactly as configured — see §10.

### Observed: the previously flashed firmware was `esp32-joycontrol`

Its USB port enumerated on the host as a **Nintendo Pro Controller**
(`057e:2009`, `bManufacturer=Nintendo`, `iProduct="Pro Controller"`, serial
`000000000011`), and its console attached to WiFi `key-2_4g` as `192.168.9.209`
with the amiibo/macro command shell. Confirms Q13's description of the prior
attempt as a wired, WiFi-exposed build — and confirms the board's native USB
port is wired to the host.

After flashing master, that device disconnects and **nothing re-enumerates**:

```
18:54:18 usb 2-2: USB disconnect, device number 3
(nothing since)
```

master's descriptor is `0777:0777` (custom CDC, `bDeviceClass 0xEF`), so the
host should have seen a *new* device. It did not. master's own log nonetheless
claims the CDC mounted (`transport_usb_cdc: USB CDC reconnected, RX buffer
reset`), which is `tud_mounted()` returning true. **Unresolved** — needs
`tud_mounted()`/`tud_cdc_connected()` instrumentation, and a cable re-seat, to
separate "device did not enumerate" from "host missed the re-attach".

---

## 10. Serial-port trap: asserting DTR/RTS holds the S3 in reset

This is the single most dangerous thing found on the bench, because it fails
**silently** and looks exactly like "the device is doing nothing".

The CH9102 bridge wires **DTR into GPIO0 and RTS into EN**, the standard
auto-reset circuit. pySerial asserts *both* when you open the port by default.
With both asserted, EN is held low and the chip never runs.

| DTR/RTS on open | Observed result |
| --- | --- |
| both asserted (pySerial default) | `SerialException: device reports readiness to read but returned no data (device disconnected or multiple access on port?)` — and **zero** bytes forever |
| both deasserted | normal operation; ~4–5 KB of boot log per reset |

Consequences actually hit during this session:

- A 15-minute capture logged **0 bytes** and was nearly written up as "no
  connection was attempted". The board had never run.
- Any tool that opens `/dev/ttyACM0` and holds it asserted keeps the device in
  reset for as long as it lives.

Minimal correct handling:

```python
s = serial.Serial(PORT, 115200, timeout=1, dsrdtr=False, rtscts=False)
s.setDTR(False); s.setRTS(False)   # immediately, and again in a finally block
```

This is not a quirk of a hand-rolled script: it is a **hard requirement for the
container's control plane**. A bridge that opens the port with default line
state — pyserial's `serial.Serial(port)` , or any Docker serial passthrough that
raises RTS — holds the board in a permanent reset loop and presents as "no
device". Filed against the USB-CDC control-plane ticket.

---

## 8. What this unblocks, and what it does not

**Unblocked.** A reproducible S3 build exists on a released IDF: v5.5.5 +
python 3.12 + `idf.py build`, 0 warnings, app at 0.52 MB inside a 3 MB slot,
merged image packaged. The 16 MB/OTA layout is real and verified. The 5 ms
mechanism is present and enabled. The exact IDF floor is now known (v5.5.5).

**Still blocked on hardware.** Every claim that starts with "the console sees…"
or "the interval holds…". Issue #2 as written is **partially** answerable and
its bench half remains open; the honest state is "build known good, pairing
unattempted".

> Superseded by §11: the board arrived the same day and the pairing **was**
> done. See §11 for the results, the extended-advertising blocker, the measured
> 15 ms interval, and the mbuf-exhaustion bug that currently breaks HID.

**Next physical step** (needs the N16R8 attached): flash
`release/ns-controller-esp32s3n16.bin` at `0x0` or `idf.py -p <port> flash`,
watch the UART/COM port for the `BLE_GAP_EVENT_CONNECT` line carrying
`conn_itvl`, and confirm whether it reads `4` (5 ms) or `6` (7.5 ms). That
single number decides both §3's residual question and §3's `gap.c` finding.

---

## 11. Bench results: master on the real board, paired to a real Switch 2

Written after §9–§10. This is the part the ticket actually asked for.

### 11.1 What was run

Master (ESP-IDF v5.5.5, `main/src/device.c` with the change in §11.2) was
flashed to the N16R8 from §9 and paired to a real Switch 2. The console-side log
was captured continuously from `/dev/ttyACM0`.

### 11.2 THE BLOCKER: the console cannot see extended advertising

This is the most important fact found on the bench, and it cost the most time
because the firmware reports success and a phone scanner reports success too.

| | `CONFIG_BT_NIMBLE_EXT_ADV=y` (as `sdkconfig.defaults` ships) | `=n`, legacy `ble_gap_adv_start()` |
| --- | --- | --- |
| Board boots, BLE stack syncs | yes | yes |
| nRF Connect sees `78:81:8C:CA:6D:32` | yes | yes |
| nRF Connect marks it **Connectable** | yes | yes |
| Manufacturer data `53 05 01 00 03 7E 05 69 20 …` | yes, byte-exact | yes, byte-exact |
| A phone can connect to it | yes | yes |
| **Switch 2 grip-order screen lists it** | **no — never** | **yes, immediately** |
| Console pairs + encrypts | n/a | yes |

The advertisement content was verified correct from every angle before this was
found (§2–§3 in this doc, plus a byte-for-byte decode of
`switch2_controller_research/captures/nrf52840/btle_procon2_advertise.pcapng`,
which shows a real Pro2 sending flags `06` + the identical 26-byte Nintendo
manufacturer payload as a **legacy `ADV_IND`**, PDU header `0x00`, length 37,
public address `98:E2:55:C2:16:88`, and answering the console's `SCAN_REQ`).

Ruled out along the way, each by direct measurement:

- payload, flags, AD lengths - byte-identical to the capture;
- address - `78:81:8C:…`, correct byte order (`esp_iface_mac_addr_set` memcpy's
  verbatim and `read_mac_wrapper` logs `mac[0]` first);
- `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE` - disabling it changed nothing;
- missing SCAN_RSP - adding `ble_gap_ext_adv_rsp_set_data()` changed nothing;
- TX power / radio - a phone connects, and the board's WiFi worked before.

Switching to NimBLE's **legacy advertising path** fixed discovery immediately.
The change is `ble_advertise()` in `main/src/device.c`, plus
`# CONFIG_BT_NIMBLE_EXT_ADV is not set` in `sdkconfig.defaults.esp32s3`
(`sdkconfig.defaults` sets it `=y` for every target, so the S3 file overrides).

**Not yet explained:** *why* the console rejects the extended-advertising path.
The working hypothesis is that with `EXT_ADV=y` the controller drives
advertising through the extended HCI commands, and the console's SoC-level
advertisement filter (see `bluetooth_interface.md`: "limit traffic to Nintendo
devices … at the SoC level") does not accept what arrives. This needs a sniffer
capture of our own advertisement to confirm — a phone scanner accepts both, so
it cannot distinguish them.

### 11.3 Pairing succeeds, and the full flow is observed

With legacy advertising, the console found the board and completed the whole
documented handshake on the first try:

```
I (7217) ble_gap: connected, set nintendo switch addr, addr=      <- console 40:44:f7:2b:12:97
I (8237) ble_gap: subscribe event; attr_handle=0x001a ...
I (8337) app: process cmd 0x07, subcmd 0x01
I (8477) app: process cmd 0x15, subcmd 0x01      <- 0x15 = Bluetooth Pairing
I (8507) app: process cmd 0x15, subcmd 0x04      <- LTK A1
I (8537) app: process cmd 0x15, subcmd 0x02      <- confirm LTK, B2/A2
I (8567) app: process cmd 0x15, subcmd 0x03      <- finalise pairing
I (8567) app: Injecting LTK for peer addr: 40:44:f7:2b:12:97
I (8657) ble_gap: encryption change event; status=0
I (9177) ble_gap: subscribe event; attr_handle=0x000e
I (9177) hid: controller report task start, interval: 15ms
```

This is the pseudo-OOB pairing of `bluetooth_interface.md` §Pairing, executed in
full, over GATT, with **no SMP** — matching the doc's warning that initiating
SMP would make the controller drop the link.

Pairing is persisted: after a reboot the firmware logs
`Pairing info injected to BLE context` and wake-advertises (`g_adv_opcode=0x81`)
to the console it paired with. The console reconnects on its own.

**So the ticket's step 3 is answered: yes, it enumerates as a Pro Controller and
the console accepts it.** HID input also reaches the console — but only briefly;
see §11.5.

### 11.4 The 5 ms link was NOT held: measured `conn_itvl=12` (15 ms)

`print_conn_desc()` logs the real parameters, but via `ESP_LOGD`, which is
compiled out at the default `CONFIG_LOG_MAXIMUM_LEVEL=INFO`. Raising it to DEBUG
(`CONFIG_LOG_MAXIMUM_LEVEL_DEBUG=y`) makes the measurement visible:

```
D (99660) ble_gap:  conn_itvl=12 conn_latency=0 supervision_timeout=200 encrypted=0 authenticated=0 bonded=0
D (101050) ble_gap:  conn_itvl=12 conn_latency=0 supervision_timeout=200 encrypted=1 authenticated=1 bonded=1
```

`conn_itvl=12` × 1.25 ms = **15 ms**, not 4 (5 ms). Supervision timeout is
200 × 10 ms = 2 s.

There were **zero `BLE_GAP_EVENT_CONN_UPDATE` events** across the whole session,
i.e. the console never moved the link down to 5 ms.

So, for the ticket's step 2/4 question: `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y`
is necessary but **was not exercised** — the console never asked for 5 ms. The
map's open question "whether the link actually holds under load against a real
console" is therefore **still open, and now measured as: not observed; the
console ran 15 ms.**

Two candidate causes, neither established: the console may only drop to 5 ms once
a controller is fully initialised (and §11.5 prevents that), and/or the
peripheral-side `ble_gap_update_params(itvl_min = 6, itvl_max = desc.conn_itvl)`
in `main/src/gap.c` pins the range (with `desc.conn_itvl = 12` this asks for
7.5–15 ms).

### 11.5 Next blocker: HID notifications exhaust the mbuf pool, then the link dies

Immediately after the console subscribes to HID (`attr_handle=0x000e`), HID
reports start failing permanently:

```
D (49483) ble_gatt: Notification sent, handle=0x000e, len=63     <- 197 of these
E (49543) ble_gatt: Failed to allocate mbuf, len=63              <- then 2840 of these, forever
E (49543) hid: controller report send failed, rc: 6              <- BLE_HS_ENOMEM
```

`gatt_notify()` (`main/src/gatt.c:600`) calls `ble_hs_mbuf_from_flat()`, which
returns NULL — the msys pool is exhausted after ~197 notifications and **never
recovers**, including across later reconnections. `notify_tx` events with
`status=0` for handle 14 show 490 notifications did reach the link before this,
so it is not a "never worked" case: it works, then leaks.

The consequence is the disconnect loop the operator sees ("几秒钟之后就会断"):

```
I (132010) ble_gap: disconnected, reason=520, restart advertising after 5s
```

`520` = `BLE_HS_ERR_HCI_BASE (0x200) + 0x08` = HCI **connection timeout**: the
link supervision timer (2 s) expires because traffic has stopped.

This is the README's documented known issue ("在NS2中开启'更改握法/顺序'界面
连上后HID报告会出现阻塞，暂未查明原因") reproduced on the bench, with a precise
mechanism and a heap symptom attached to it.

#### Root cause: a pool-selection bug in the NimBLE port

Not a leak, and not actually a shortage of memory. `_os_msys_find_pool()` picks
the first pool whose **size** fits, and never looks at `mp_num_free`:

```c
STAILQ_FOREACH(pool, &g_msys_pool_list, omp_next) {
    if (dsize <= pool->omp_databuf_len) { break; }   // no free-block check
}
```

MSYS_1 is 150 blocks of 128 B; MSYS_2 is 30 blocks of 512 B. Small allocations
always target **MSYS_1**, so while MSYS_1 is momentarily drained,
`ble_hs_mbuf_from_flat()` fails outright and **cannot fall back to MSYS_2's idle
blocks**. `os_msys_num_free()` sums *all* pools, so the existing `< 5` guard
passed while allocation was failing.

Measured, not inferred - every failure printed the free count, and it was always
the same number:

```
E (124310) ble_gatt: Failed to allocate mbuf, len=63 (msys free=30)
```

`30` is exactly MSYS_2's block count, i.e. **MSYS_1 completely drained, MSYS_2
completely idle**. The failures are *transient* (4684 allocations still succeeded
after the first one) because blocks return as the console resumes reading - but
while it is not reading, the link starves and dies with HCI 0x08.

Two red herrings along the way, both disproved by measurement:

- in-flight notification accounting is useless here, because
  `BLE_GAP_EVENT_NOTIFY_TX` fires when the notification leaves the **host**, not
  when the peer receives it (ATT notifications are unacknowledged);
- there is no leak: a reconnect ran **7140 notifications with the pool steady at
  `free=180 of 180`**.

#### Fix

`gatt_notify()` (`main/src/gatt.c`) declines to queue a report when the pool has
less than `GATT_NOTIFY_MSYS_HEADROOM` (40) free blocks. Since a count at or below
30 means "MSYS_1 is empty", 40 keeps real slack in MSYS_1 and stops this path
from ever draining it. The HID task treats the resulting `BLE_HS_EBUSY` as a
dropped report, not an error.

Verified under the identical condition (console parked on the grip-order screen,
not draining):

| | before | after |
| --- | --- | --- |
| `Failed to allocate mbuf` | 894 | **0** |
| disconnects (`reason=520`) | 1 | **0** |
| pool at peak pressure | `free=30` (MSYS_1 empty) | `free=38` (guard engaged) |
| notifications delivered | - | 8101, link stable |

Dropping input while the console refuses to drain is the correct trade against a
dead link. On a draining link nothing is dropped: the healthy run sat at
`free=180`.

Still worth doing upstream: the pool lookup should skip pools with no free
blocks. That is an ESP-IDF change, not something this repo can ship.

### 11.6 Steps that still require a person

- Watching the console screen (the operator did this).
- The pairing flow is initiated by the console; nothing on the board can force
  it.

### 11.7 Corrections to §1–§8 that the bench produced

- §1 "Not attempted" → steps 3–4 **were** attempted; see §11.
- §5 "PSRAM risk unverified" → still unverified, and still disabled; but the
  board's PSRAM is confirmed present and working (§9).
- §5 "flash mode `dio`" → the board's previous firmware booted QIO at 80 MHz, so
  QIO is available; still left alone.
- The 5 ms config path is confirmed *enabled* in the build
  (`CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` present in the generated
  `build/config/sdkconfig.h`), but the console never asked for 5 ms (§11.4).
