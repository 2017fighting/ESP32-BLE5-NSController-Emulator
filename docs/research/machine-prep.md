# Bench facts: the prepared machine (issue #18)

**Scope:** put this machine into a state where the firmware is buildable and the canonical
corpus is readable — ESP-IDF v5.5.5 on `esp32s3`, the six pinned reference clones under
`$REFERENCE_ROOT`, and the locations of the macro library, the amiibo library and the
user-supplied key. **Date:** 2026-10-02. **Hardware on test host:** none (build-time work
only; nothing was flashed or paired here).

This records **observed facts, not conclusions**, per the ticket. Every number below comes
from a command run in this session; the command is given so it can be re-run. Anything not
actually run is in §8 and is not a result.

The one code change this ticket carries is §7: a packaging defect that made §10.1's documented
recipe fail *after* a successful build, found by running that recipe and fixed with a test.

---

## 1. Host and toolchain

| Item | Observed value | How |
| --- | --- | --- |
| macOS / arch | `27.2` (build `26B5091g`), `arm64` (T8103) | `sw_vers`, `uname -m` |
| ESP-IDF | **v5.5.5** (`b774170ff46`, tag `v5.5.5`) at `~/esp/esp-idf-v5.5.5` (§10.1's path) | `git describe --tags`, `idf.py --version` |
| Activation helper | `~/esp/idf-env-5.5.5.sh` — prepends Python 3.12.8, exports `IDF_PATH`, sources `export.sh` | `cat`, then one `source` per build below |
| `IDF_TOOLS_PATH` | `~/.espressif` | `echo` after activation |
| IDF Python venv | `~/.espressif/python_env/idf5.5_py3.12_env` (Python `3.12.8`) | `which python` |
| Cross toolchain | `xtensa-esp32s3-elf-gcc` `14.2.0` (`crosstool-NG esp-14.2.0_20260121`) under `~/.espressif/tools/xtensa-esp-elf/esp-14.2.0_20260121/` | `which`, `--version` |
| cmake / ninja | `4.4.3` (Homebrew), `1.13.2` (Homebrew) | `--version` |
| `patch/patch_nimble_lib.py` | **not run**, as §10.1 requires; no `*.original` backup exists anywhere under `esp-idf/components/bt/` | `find esp-idf/components/bt -name '*.original'` |

`./install.sh esp32s3` was **not re-run for this ticket** — it had already been run on this host
by the session that produced `s3-bringup.md`/`s3-build-and-link.md`, and the builds below prove
the `esp32s3` toolset and Python env are present and usable (`xtensa-esp32s3-elf-gcc` resolves,
`idf.py build` completes). The version floor is what this ticket had to re-establish, and it is
re-established at binary level in §3.

The floor is met and it is met **at binary level**, not by "the build succeeded" — §3.

**Two arm64/macOS caveats, both already recorded in §10.1 and both confirmed here:**

1. **Python 3.12 is mandatory.** IDF v5.5 refuses to activate under 3.13+ and this host's
   default `python3` is 3.14.7 (mise). `export.sh` fails outright rather than warning, so the
   helper prepends the checkout's own 3.12 before sourcing it. Without the helper, nothing
   in this record runs.
2. **`esptool` is not the console script's name on a stock IDF v5.5.5.** See §7 — this was a
   real failure of §10.1's recipe, now fixed in the repo.

**One documentation conflict, for the record:** §10.1 puts IDF at `~/esp/esp-idf-v5.5.5`,
`s3-bringup.md` §2 says `~/esp/v5.5.5`. Both resolve on this host because `~/esp/v5.5.5` is a
symlink to `~/esp/esp-idf-v5.5.5`, but the two documents should agree. `macos-bench-path.md` §1
already records the `~/esp/esp-idf-v5.5.5` spelling, which is the one used here.

## 2. The clean-master build

`master` and `origin/master` are both `657853d`. The build ran in a **detached worktree** of
`origin/master` (`git worktree add --detach /tmp/ns18-master origin/master`) so that the
branch carrying the spec and this record was never touched. `managed_components/` and
`dependencies.lock` are gitignored and were copied in from the working tree so the only
variable under test is the firmware tree.

```sh
cd /tmp/ns18-master
source ~/esp/idf-env-5.5.5.sh
idf.py set-target esp32s3
idf.py build
```

| Fact | Value |
| --- | --- |
| Build result | `EXIT=0`, "Project build complete" |
| App binary | `build/ESP32-BLE5-NSController-Emulator.bin` = **549,472 B** (`0x86260`) |
| App binary SHA-256 | `aed9792eca602058d43c36d79dd39206ea5248af2c1d1dd40410cac0a28b5f5f` |
| Smallest app partition | `0x177000` (1500 K `factory`); `0xf0da0` (64%) free |
| Bootloader | `0x5160` B; 36% free |
| Config as built | `CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y`, `CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions_n2c.csv"`, `CONFIG_BT_NIMBLE_EXT_ADV=y` |
| Compiler/Kconfig warnings | 0 compiler warnings; **3** `unknown kconfig symbol` warnings |
| `idf.py size` total image | 549,357 B (the `.bin` is padded larger) |

The 3 Kconfig warnings are the esp32-only `BTDM_CTRL_MODE_{BLE_ONLY,BR_EDR_ONLY,BTDM}`
assignments in `sdkconfig.defaults.esp32s3`:

```
warning: unknown kconfig symbol 'BTDM_CTRL_MODE_BLE_ONLY' assigned to 'y' in .../sdkconfig.defaults.esp32s3
warning: unknown kconfig symbol 'BTDM_CTRL_MODE_BR_EDR_ONLY' assigned to 'n' in .../sdkconfig.defaults.esp32s3
warning: unknown kconfig symbol 'BTDM_CTRL_MODE_BTDM' assigned to 'n' in .../sdkconfig.defaults.esp32s3
```

They are inert for the BTDM controller (the symbol that matters, §3, is defined) and the branch
already removes them — §10.1's count of "3 → 0" is confirmed both ways.

**Note that this is the 8 MB / `partitions_n2c.csv` configuration**, because that is what
`master` ships. §10.1's table wants `CONFIG_ESPTOOLPY_FLASHSIZE_16MB` +
`partitions_16mb_s3.csv`, and that is a **branch** change, not a `master` one (§4).

## 3. The Kconfig symbol, verified at binary level

The failure mode §10.1 warns about is silent: the assignment is ignored, the build succeeds,
and the firmware rejects the console's 5 ms interval at run time. Three independent checks,
in both trees:

| # | Check | master `657853d` | branch `df2e5fb` |
| --- | --- | --- | --- |
| 1 | `sdkconfig` | `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` (line 1045) | `=y` (line 1038) |
| 2 | `build/config/sdkconfig.h` | `#define CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE 1` (line 637) | `1` |
| 3 | `xtensa-esp32s3-elf-nm` on the S3 blob `$IDF_PATH/components/bt/controller/lib_esp32c3_family/esp32s3/libbtdm_app.a` | `0000000c T ble_min_conn_interval_enable` | same |
| 4 | `xtensa-esp32s3-elf-nm` on the linked ELF | `42027854 T ble_min_conn_interval_enable` | `42026a80 T ble_min_conn_interval_enable` |

Check 3 is the one that distinguishes v5.5.5 from v5.5.4 — §10.1's own binary-level finding,
which this run reproduces for v5.5.5 but did **not** re-measure against v5.5.4 (it is not
installed on this host); check 4 is the one that proves the symbol survives into the
application rather than merely existing in an archive member nothing pulls in.

## 4. The image size: 549,472 B, not 553,504 B

§7.4 records "553,504 B in a 3 MB `ota_0` slot — 82% free" and cites `s3-bringup.md` §11.1.
**That citation does not support the number.** `s3-bringup.md` §4 records
`0x86260` = **549,472 B**, and §11.1 states no size at all. What two clean builds measure:

| Tree | Config | App `.bin` | SHA-256 |
| --- | --- | --- | --- |
| clean `master` `657853d` | 8 MB, `partitions_n2c.csv`, `EXT_ADV=y` | **549,472 B** (`0x86260`) | `aed9792e…` |
| branch `df2e5fb` | 16 MB, `partitions_16mb_s3.csv`, `EXT_ADV` off | **545,136 B** (`0x85170`) | `756755e9…` |

Free space, for §7.4's "82% free" claim: the branch's 545,136 B sits in a 3 MB `ota_0` with
`0x27ae90` (83%) free, and `master`'s 549,472 B sits in the 1500 K `factory` slot with `0xf0da0`
(64%) free. The 3 MB slot number is reproducible; only the byte count in §7.4 is not.

549,472 B is exactly `s3-bringup.md` §4's `0x86260`, reproduced to the byte two days later on
`master` at `657853d`. The branch is 4,336 B *smaller*, which is what its code delta predicts:
the branch's `main/` diff against `master` is `73 insertions(+), 112 deletions(-)`, dominated
by `device.c` (`21 +/102 -`) losing the extended-advertising path (the `s3-bringup.md` §11.2
legacy-advertising change) and gaining the NFC/codec work.

So the three numbers are: **549,472** (pre-`EXT_ADV`-change, either layout — the partition
table does not change the app's code size), **545,136** (branch, `EXT_ADV` off), and
**553,504**, which no record and no build on this machine produces. The prior session's
545,136 B measurement is reproducible exactly, in a second clean worktree.

*(The branch's merged flashable artifact is `release/ns-controller-esp32s3n16.bin`, 676,208 B,
SHA-256 `133259d1…`, whose length equals the app's end offset. The earlier comment on the
ticket recorded the same 676,208 B.)*

## 5. The pinned corpus

`$REFERENCE_ROOT` is unset, so the default `~/clone` applies — paths below are written as
`$REFERENCE_ROOT/<alias>/…` for that reason. Each repo was cloned
(`git clone --filter=blob:none --no-tags`) and then checked out **detached at its pin**, so the
citations in `docs/references.md` resolve at the recorded lines.

| alias | pin (`docs/references.md`) | clone `HEAD` | verdict |
| --- | --- | --- | --- |
| `switch2_controller_research` | `a3306b473acff0d6844fb1e288883a3940df0baf` | same | **match** |
| `switch-controller-macro` | `202e512206ab955361aa40e8e2a2735cbe142175` | same | **match** |
| `Amiibo` | `58cf4558de56863575421844bc27695ef1ba56ad` | same | **match** |
| `emuiibo` | `28b357d5ce4aa373891c5294127f79137e0917ff` | same | **match** |
| `UARTSwitchCon` | `ad772477c19306feae61f9d4d3538c1ec0a6feb6` | same | **match** |
| `EasyMCU_ESP32S3` | `1a39ca50d50f06a1a7c523c775f551dda75b996f` | same | **match** |

**No mismatches.** In every case the clone's default-branch HEAD *was* the pin, so nothing had
to be fetched by SHA. `amiitool` is deliberately not cloned, per `docs/references.md`, and
`~/clone/amiitool` does not exist.

Two working-tree notes:

- `Amiibo` shows 2 "modified-or-untracked" paths (`Gakuto Sōgetsu.bin`, `Tatsuhisa “Luke”
  Kamijō.bin`) that are **not** a tree difference: `git -c core.precomposeunicode=false status`
  is clean. It is macOS NFC-vs-NFD filename normalisation on non-ASCII names, and it does not
  affect the file contents the container indexes.
- The `Amiibo` clone is a partial (`--filter=blob:none`) clone; every file that matters to the
  library is materialised on disk, which is what §6 counts.

## 6. The libraries and the key

Locations only; nothing here is a copy, and the key is deliberately described without ever
reading its bytes.

| Thing | Location | Notes |
| --- | --- | --- |
| Macro library | `$REFERENCE_ROOT/switch-controller-macro/宏/*.json` | 4 macros: `天妇罗巢穴宏1.json`, `天妇罗巢穴风扇-感谢群友分享.json`, `杏仁巢穴宏.json`, `纠错宏.json`; `.png`/`.jpg` companions sit beside them and §8.4 says to ignore those. This is the mount source for `/library/macros`. |
| Amiibo library (`.bin`, canonical) | `$REFERENCE_ROOT/Amiibo/Amiibo Bin/**/*.bin` | 955 files, grouped in figure/series directories. Canonical per §8.5; this is the mount source for `/library/amiibo`. |
| Amiibo library (`.nfc`) | `$REFERENCE_ROOT/Amiibo/Amiibo NFC/**/*.nfc` | 964 files, same figures. §8.5 accepts `.nfc`; not needed for the first index. |
| Retail key | `$REFERENCE_ROOT/Amiibo/Amiibo Bin/!Essential Files/key_retail.bin` | **User-supplied (HITL).** It is the corpus's copy because that is the user's own file on this machine; its bytes were not read, hashed or copied. |
| Key, two-file spelling | `$REFERENCE_ROOT/Amiibo/Amiibo Bin/!Essential Files/{unfixed-info.bin,locked-secret.bin}` | Also present, and accepted by ADR-0012 in that concatenation order. `ally-all-in-839.bin` in the same directory is unrelated. |

The key is the amiibo half's input, not the firmware's, and it reaches the container only as a
read-only mount at the fixed path `/keys/key_retail.bin` (§6.7, ADR-0012) — no environment
variable, and nothing in this ticket mounts it.

## 7. The packaging defect, and the fix

§10.1's recipe ends with `python scripts/package_firmware_v5.py`. On a stock IDF v5.5.5 venv
that dies **after a successful build**:

```
$ python scripts/package_firmware_v5.py
Packaging firmware for esp32s3 (16MB)...
Command: esptool --chip esp32s3 merge_bin …
Error: esptool not found. Make sure esptool is installed and in PATH.   # rc=1, no artifact
```

The cause is a version collision: the script shelled out to a bare `esptool` (pre-fix
`scripts/package_firmware_v5.py:135`), but **esptool v4 installs its console script as
`esptool.py`; the rename to `esptool` lands in v5.** IDF v5.5.5's venv ships esptool
**v4.12.0** (`$IDF_PYTHON_ENV_PATH/bin/esptool.py`), and `esptool` is not on PATH.

The fix runs esptool as a module of the running interpreter — `python -m esptool` — which is
version-proof across v4/v5 and is what ESP-IDF's own flash epilogue uses:

```
or
 python -m esptool --chip esp32s3 -b 460800 … write_flash "@flash_args"
```

`esptool_argv()` and `build_merge_command()` are the new seams; `scripts/test_package_firmware_v5.py`
pins the launcher (not merely a string in the command), the chip/subcommand, the flash
parameters, the `-o` path and the offset/file pairs. Before/after, same tree, same venv, with
the host-side symlink workaround removed:

| script | shim | result |
| --- | --- | --- |
| `HEAD` version | removed | `Error: esptool not found…`, `rc=1`, **no artifact** |
| fixed version | removed | `rc=0`, `Wrote 0xa5170 bytes…`, 676,208 B artifact |

`python3 -m unittest discover -s scripts -p 'test_*.py'` → 29 tests, `OK` (1 skip under a host
Python without esptool; the same suite under the IDF venv Python 3.12.8 is 29 tests with **no**
skip).

**§10.1's recipe is now true as written**, and the `esptool` → `esptool.py` symlink in the
host-side `~/esp/idf-env-5.5.5.sh` is no longer load-bearing (it was removed and the packaging
step re-run to prove it).

## 8. Not attempted

- **Nothing was flashed or paired.** This is a build-time record; the board was not attached
  for it. §10.2's flashing and §10.3's bring-up traps are untouched here.
- **The 8 MB `n2c` layout was not flashed-compared**, and the 16 MB layout was not re-measured
  against hardware.
- **The container was not involved.** No mount was created, and the key was not read.
- **`scripts/package_firmware.py` (the unsuffixed, G-10 script) was not touched** — it is still
  the broken one §10.1 says not to use. Its exit-0 failure mode is G-10's, not this ticket's.
- **The `553,504 B` figure was not corrected in §7.4.** That is a spec edit and belongs to
  [Correct the spec where the research contradicts it](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/40),
  which this ticket has surfaced it to.
