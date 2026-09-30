# Bench facts: S3 master bring-up (issue #2)

**Scope:** host-side bring-up of `master` for `esp32s3` on the ESP32-S3-N16R8.
**Date:** 2026-09-30. **Hardware on test host:** none (see "Not attempted").

This records **observed facts, not conclusions**, per the ticket. Every number
below comes from a command run in this session; the command is given so it can
be re-run. Anything not actually run is in "Not attempted" and is not a result.

---

## 1. What could not be attempted

The ticket's steps 3 and 4c ("pair with a real Switch 2", "observe HID report
interval / console-side rejection") are physical bench work. They were **not
attempted** and are **not** claimed.

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
- whether `CONFIG_SPIRAM_MODE_OCT=y` would boot on this board (not attempted at
  all — the block is commented out, see §5.2);

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

PSRAM is **not** enabled — see §5.2.

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
  hardware is the same class of risk as §5.2 with no upside.
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

Its PSRAM recommendation is **not** taken: see §5.2 for why that block is now
commented out rather than enabled.

The v5.5.4/v5.4.4 errors are the same failure Q17 warns about in the map
("never propagate a secondary summary"): `release/v5.5` is not `v5.5.4`.
The build-recipe section should pin **v5.5.5**.

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

**Next physical step** (needs the N16R8 attached): flash
`release/ns-controller-esp32s3n16.bin` at `0x0` or `idf.py -p <port> flash`,
watch the UART/COM port for the `BLE_GAP_EVENT_CONNECT` line carrying
`conn_itvl`, and confirm whether it reads `4` (5 ms) or `6` (7.5 ms). That
single number decides both §3's residual question and §3's `gap.c` finding.
