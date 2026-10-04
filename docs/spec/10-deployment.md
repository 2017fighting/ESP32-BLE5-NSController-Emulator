# 10 · Deployment

Two artifacts, deployed separately: the firmware is flashed to the board by hand, and the
container runs next to the board with the device passed through and three mounts.

## 10.1 Build the firmware

**The ESP-IDF floor is `v5.5.5`.** This is a hard floor, not a preference, and the failure
mode is silent: on an earlier version the assignment
`CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` produces only
`warning: unknown kconfig symbol … assigned to 'y'`, the symbol is absent from `sdkconfig`
and never `#define`d, **and the build still succeeds** with a firmware that rejects the
console's 5 ms interval at run time. Confirmed at binary level:
`ble_min_conn_interval_enable` is absent from v5.5.4's `libbtdm_app.a` for `esp32s3` and
present in v5.5.5's. (A research record claims v5.5.4+; it is wrong, and it is corrected here
rather than carried.)

```sh
git clone -b v5.5.5 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf-v5.5.5
cd ~/esp/esp-idf-v5.5.5 && ./install.sh esp32s3 && . ./export.sh

cd <repo>
idf.py set-target esp32s3      # pulls sdkconfig.defaults + sdkconfig.defaults.esp32s3
idf.py build
python scripts/package_firmware_v5.py    # -> release/ns-controller-esp32s3n16.bin
```

**`patch/patch_nimble_lib.py` must not be run for S3.** It rewrites `ble_ll_conn.c.o` in a
RISC-V `libble_app.a` for C6/C61-class targets, and it rejects `esp32s3` itself. S3 needs no
patch: the Kconfig symbol alone suffices at build level.

**Use `package_firmware_v5.py`, not `package_firmware.py`.** The non-`v5` script matches
`--flash-size` while `flash_args` emits `--flash_size`, so it prints
`Error: Could not extract flash size` and **exits 0** — a packaging failure that looks like
success and would ship an `n8` image name for a 16 MB module. *(Two corrections to the earlier
research recipe, both recorded rather than silently applied: it names a `v5.5.4` floor and
tells you to run the broken script.)*

**Config the S3 must keep** (`sdkconfig.defaults.esp32s3` already sets all of it):

| Setting | Value | Why |
| --- | --- | --- |
| `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE` | `y` | the sub-spec path; harmless and unexercised today (§7.6) |
| `CONFIG_BT_NIMBLE_EXT_ADV` | **not set** | **the console cannot see the board with extended advertising** (below) |
| `CONFIG_TRANSPORT_LAYER_USB_CDC` | `y` | the native OTG path, reserved (ADR-0001) |
| `CONFIG_ESPTOOLPY_FLASHSIZE_16MB` + `partitions_16mb_s3.csv` | | the board is 16 MB; the old 8 MB/1500 K layout was sized for the wrong module |
| `CONFIG_ESP_CONSOLE_SECONDARY_NONE` | `y` | |
| `CONFIG_HID_REPORT_INTERVAL` | `15` | quantised to 10 ms at 100 Hz (§7.5) |

**PSRAM stays off** until something allocates from it, verified against the board (§7.4).

## 10.2 Flash the board

**Flashing is manual, and it requires the container stopped.** One wire carries flash, log and
control (ADR-0001), so `idf.py flash` and the container cannot both hold the port — and a
holder that asserts DTR holds the board in reset. OTA is out of scope; the `ota_1` slot exists
in the layout and nothing uses it.

```sh
# stop the container first
docker compose down
idf.py -p /dev/ttyACM0 flash
# or, for a fresh board with no toolchain:
esptool.py -p /dev/ttyACM0 write_flash 0x0 release/ns-controller-esp32s3n16.bin

# on the macOS bench host the port is the resolved node, not /dev/ttyACM0 (§10.7):
export NS2_PORT="$(python3 scripts/find_serial_port.py)"
idf.py -p "$NS2_PORT" flash
```

Then start the container again. Nothing needs to be re-paired after a flash, because the bond
lives in NVS on the board — but see the stale-bond trap in §10.6.

## 10.3 Bring-up traps, all measured

| Trap | What it looks like | Fix |
| --- | --- | --- |
| **DTR/RTS asserted** | zero bytes from the port, forever; `SerialException: device reports readiness to read but returned no data` | open with `dsrdtr=False, rtscts=False`, `setDTR(False)`/`setRTS(False)`, and again in `finally` (§8.3) |
| **Extended advertising** | the board advertises, a phone connects, nRF Connect shows byte-correct Nintendo manufacturer data — and the console's grip-order screen **never lists it** | legacy advertising; `# CONFIG_BT_NIMBLE_EXT_ADV is not set` (already set for S3) |
| **Stale NVS bond** | the board wake-advertises (`g_adv_opcode = 0x81`) to the console it remembers, so a **different** console — or the same one sitting on the grip-order screen — never lists it. `PAIR_UNPAIR` clears the bond but **does not fix this**: `ble_advertise()` early-returns while an advert is active, so the stale bytes keep going out | erase the bond **and let the board reboot**, so it re-advertises pairable: `python -m esptool --chip esp32s3 -p <port> erase_region 0x9000 0x6000` (or `idf.py -p <port> erase-flash`), then re-pair from the grip-order screen. Measured this session: `link-drop-bench.md` §5 |
| **Wrong port** | `/dev/ttyUSB0` is present and `idf.py monitor` shows nothing useful | the S3's control plane is the **CH9102 bridge** (`1a86:55d3` → `/dev/ttyACM0`); `/dev/ttyUSB0` on the bench host was an unrelated ESP32-D0WDQ6 |
| **Wrong port, macOS** | a `usbmodem*` node is opened and yields nothing, or the container is handed the wrong one | macOS has neither `/dev/ttyACM*` nor `/dev/ttyUSB0`, and this host carries **two** `usbmodem` nodes — the board and an unrelated AV adapter. Resolve by USB Serial Number, never by glob (§10.7) |
| **The old firmware's identity** | the port enumerates as `057e:2009 Nintendo Pro Controller` | that is the previous **unrelated** firmware; `Hello`'s `fw_version` is what answers "which firmware is this", not VID/PID |

## 10.4 Run the container

Compose is the documented default, because the product *is* its mounts and a run line with
four flags is a copy-paste hazard every time. The `docker run` equivalent ships beside it —
one person will always want it.

**Two files are committed, not one.** `container/compose.yaml` is the Linux canonical form
shown below: the block is the file's `services:` section byte for byte, and
`container/tests/test_compose.py` fails if the two ever part. The file's own header comments
sit above `services:` in the file itself and are not repeated here.
`container/compose.macos.yaml` is an override that changes **only** the `devices:` mapping, so
the platform difference is versioned rather than buried in prose. It takes the host node from `$NS2_PORT` rather than naming it, because the macOS node is host-specific and must
be resolved, not guessed (§10.7):

> Compose merges `devices:` **per container-side target**: an entry with the same target replaces
the base's, one with a different target is appended beside it. So the override is correct *because*
it keeps the target `/dev/ttyACM0` — changing the container-side path would silently retain the
base's `/dev/ttyACM0:/dev/ttyACM0` and fail on a host device macOS does not have. Measured on
Compose v5.1.2.

```sh
# Linux
docker compose -f container/compose.yaml up

# macOS
export NS2_PORT="$(python3 scripts/find_serial_port.py)"
docker compose -f container/compose.yaml -f container/compose.macos.yaml up
```

Either way the container sees `/dev/ttyACM0`.

```yaml
services:
  controller:
    build:
      context: ..
      dockerfile: container/Dockerfile
    image: ${NS2_IMAGE:-ghcr.io/2017fighting/ns2-controller:latest}
    devices:
      - /dev/ttyACM0:/dev/ttyACM0
    volumes:
      - "$HOME/clone/switch-controller-macro/宏:/library/macros:ro"
      - "$HOME/clone/Amiibo:/library/amiibo:ro"
      - "$HOME/clone/Amiibo/!Essential Files/key_retail.bin:/keys/key_retail.bin:ro"
    ports:
      - "8080:8080"
    restart: unless-stopped
```

**`image:` is a substitution, and its default is the published name.** With `NS2_IMAGE`
unset the build tags `ghcr.io/2017fighting/ns2-controller:latest`, so a host that has never
pulled the image builds its own and `up` finds what it built, with no hand-tagging step;
`NS2_IMAGE=ns2-controller:dev docker compose … up --build` pins a bench tag instead. The
`<owner>` this line carried is not in Docker's reference character set, and Compose hands
`image:` to the builder as `-t`, so the committed file could not build its own image at all.
The research record the block came from (`container-and-web-ui.md` §4) carries the literal
`ghcr.io/2017fighting/ns2-controller:latest` and is **left as written** — a research record
is the evidence trail, not the specification (§00) — which is why that is the default rather
than a fresh local name.

**The two `docker run` lines below keep the `<owner>` placeholder**: they are lines a reader
types and substitutes by hand, where the name is a value to replace, and `NS2_IMAGE` is the
compose spelling of that same substitution. Replacing it there is not optional — the line as
written never reaches Docker. `<` and `>` are redirections to the shell, which stops at
`owner: No such file or directory`.

```sh
docker run --rm -it \
  --device=/dev/serial/by-id/usb-1a86_USB_Single_Serial_*-if00:/dev/ttyACM0 \
  -v "$HOME/clone/switch-controller-macro/宏":/library/macros:ro \
  -v "$HOME/clone/Amiibo":/library/amiibo:ro \
  -v "$HOME/clone/Amiibo/!Essential Files/key_retail.bin":/keys/key_retail.bin:ro \
  -p 8080:8080 ghcr.io/<owner>/ns2-controller:latest
```

On the macOS bench host the `--device` argument takes the resolved node instead, and the
container still sees `/dev/ttyACM0` (§10.7):

```sh
# NS2_PORT is the resolved macOS node, exported above
docker run --rm -it \
  --device="$NS2_PORT:/dev/ttyACM0" \
  -v "$HOME/clone/switch-controller-macro/宏":/library/macros:ro \
  -v "$HOME/clone/Amiibo":/library/amiibo:ro \
  -v "$HOME/clone/Amiibo/!Essential Files/key_retail.bin":/keys/key_retail.bin:ro \
  -p 8080:8080 ghcr.io/<owner>/ns2-controller:latest
```

The UI is then at `http://localhost:8080`.

| Mount | Read-only | Holds |
| --- | --- | --- |
| `/library/macros` | yes | the macro library (`*.json`) |
| `/library/amiibo` | yes | the figure library (`.bin` canonical) |
| `/keys/key_retail.bin` | yes | the user's own retail key — one fixed path, no environment variable (ADR-0012) |
| `--device` | — | the CH9102 port; never `--privileged` |

**Host access is the host's problem, and it must be solved before the container runs.** Docker
hands the container the host node with the host's ownership, so the user must already be able
to read it — `dialout` on Debian/Arch, `uucp` or `plugdev` elsewhere — or the container needs
`--group-add`. Diagnose with `id -nG` and `ls -l /dev/ttyACM0`.

On macOS the container's copy of the node arrives as `root:dialout` mode `0660`, so a
container that runs as a non-root user needs membership of `dialout` — the host's own
ownership does not carry over, and the macOS user is in no serial group at all.

**No udev rule is required.** The CH9102 is a stock CDC/ACM device, so udev already creates
`/dev/serial/by-id/…`; a rule is only wanted for a fixed friendly name. **macOS has no
`/dev/serial/by-id`** and no udev to make one; the board is pinned by USB Serial Number
instead (§10.7).

## 10.5 Operational rules

These are not optional and each one reads as a bug in six weeks:

| Rule | Why |
| --- | --- |
| **DTR/RTS deasserted on open, and in `finally`** | the CH9102 wires DTR→GPIO0 / RTS→EN; asserted, the board sits in reset and presents as "no device" |
| **Flashing requires the container stopped** | one wire carries flash, log and control |
| **One process per port** | a second holder is a deployment error, surfaced as `Port busy`, never retried through |
| **The port is configuration; the default is `/dev/ttyACM0`** | so a replugged board can be pinned by `/dev/serial/by-id` on Linux. There is no `/dev/serial/by-id` on macOS: the node is the USB Serial Number and is resolved, not globbed, from `SER=5C93063985` (§10.7) |
| **On macOS the node is forwarded, never attached** | `orb usb attach` detaches the board from macOS and breaks host-side flashing; plain `--device` forwards it and leaves both sides usable (§10.7) |
| **Baud 115200** | a bench fact (G-1 closed, #33): 921600 degenerates §2.7's window blast — 12–15 retries per 4 KB, zero corruption; `baud-bench.md` §3 |
| **The container never writes the key anywhere** | ADR-0012 |
| **No WiFi, no host networking, no privileged mode, no named volumes** | ADR-0002 |
| **Log at `INFO` or lower for any timing work** | on a DEBUG build the report rate is set by the UART log budget, not by `CONFIG_HID_REPORT_INTERVAL` (§7.5) |
| **To let the console sleep, the board must be off or unpaired** | a powered bonded board wake-advertises 3 s after every drop, and a sleeping console keeps scanning for its bonded controllers; it reconnects, its own resumed reports wake it to the lock screen, and it sleeps again — so the console cannot stay asleep while the board advertises. The device watches neither link (ADR-0008), so no container policy can intervene (`console-lifecycle-bench.md` §3.4; `link-drop-bench.md` §2) |

## 10.6 First run, in order

1. `idf.py -p /dev/ttyACM0 erase-flash` (`-p "$NS2_PORT"` on the macOS bench host, §10.7) if
   the board has ever been paired to a *different* console, then flash and pair fresh from the
   console's controller menu.
2. Start the container and confirm the Connection screen reads the port, the bond, and a
   `fw_version` — not VID/PID.
3. Mount the macro library and press `Rescan`; the four real macros should list with their
   measured plan sizes (§5.7).
4. Mount the key; the Settings screen must read `KEY_OK`, or `KEY_UNVERIFIED` if no library is
   mounted (§6.7).
5. Start a macro with the console connected and confirm the console acts on it. The board has
   **no physical buttons**, so with nothing driving it a neutral controller is correct
   behaviour, not a fault.
6. Watch for a `boot_id` change over a sleep/wake cycle. It is expected (§9.2), and the
   recovery path is what must be checked, not the reboot.

## 10.7 The macOS bench host

The bench host is an arm64 Mac (macOS 27.2) running **OrbStack**, with no Docker Desktop and
**no WCH `CH34x` driver**. This section is the only place the macOS divergence lives; where an
earlier section needs it, it points here (ADR-0014).

**The path is forwarding.** The macOS node is mapped onto the container's default port, and
the board stays on macOS:

```sh
# resolves the node by USB Serial Number and fails on 0 or >1 matches, rather than globbing
export NS2_PORT="$(python3 scripts/find_serial_port.py)"

# the container sees /dev/ttyACM0 — the §10.5 default, unchanged, so no container code moves
docker compose -f container/compose.yaml -f container/compose.macos.yaml up

# flashing stays host-side, with the container stopped (§10.2)
idf.py -p "$NS2_PORT" flash
```

Forwarding is **non-exclusive**: macOS keeps the node while the container holds it, which is
precisely what lets flashing remain a host-side act. `orb usb attach` is the wrong tool for
this path — it *detaches* the device from macOS and so breaks the flash step; it is never used.

**The node name, and how to pin it.** macOS has no `/dev/ttyACM*`, no `/dev/ttyUSB*` and no
`/dev/serial/by-id`. The node is the **USB Serial Number** plus the interface number:

| Device | `USB Serial Number` | Node |
| --- | --- | --- |
| The board | `5C93063985` | `/dev/cu.usbmodem5C930639851` |
| An unrelated AV adapter on the same host | `000000000000` | `/dev/cu.usbmodem0000001` |

That is better than a topology-derived name — it survives moving the board to another port —
but it makes a `usbmodem*` glob ambiguous on a host that has two, so the node is resolved by
serial number, never guessed. `scripts/find_serial_port.py` is the resolver: it reads the USB
Serial Number out of `ioreg`, matches it against the nodes that exist, and **fails loudly** on
zero matches (exit 2) and on more than one (exit 3) rather than picking one.

**No WCH driver is required.** Apple's `com.apple.iokit.IOSerialFamily` claims the bridge as
`/dev/cu.usbmodem*`, and the whole flash path runs on it: stub upload, baud change to 460800,
`flash-id`, and a 256 KiB incompressible write round-tripped against the unclaimed `storage`
region. The WCH `CH34x` DriverKit package installs a system extension to buy nothing this
effort needs. (`bench-transport-macos.md` §2 claims otherwise and is refuted in
`macos-bench-path.md` §5–§7a; that record is left as written, per the `s3-bringup.md` §7
precedent for closed research records.)

**`115200` is the baud, measured on this host.** The G-1 bench (#33) ran both bauds on this
Mac and through the OrbStack-forwarded container path: 115200 survives every transfer to
the 65528 B capacity maximum with zero retries on both host stacks; 921600 carries the
control plane and the log flood without one corrupted frame but degenerates every
multi-window bulk transfer (the device drains ≈14–20 KiB/s; §2.1 holds the whole
measurement). `docs/research/baud-bench.md` is the record.
