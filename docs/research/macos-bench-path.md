# Bench facts: the macOS bench path (issue #17)

**Scope:** how the container reaches the S3 on this arm64 Mac, measured with the board
attached. **Date:** 2026-10-02. **Hardware on test host:** ESP32-S3-N16R8 on
`/dev/cu.usbmodem5C930639851`, plus a Type-C AV adapter and an unidentified
`0777:0777 PITC Pro2 Controller` on the same bus.

This records **observed facts, not conclusions**, per the ticket. Every number below comes
from a command run in this session; the command is given so it can be re-run. Anything not
actually run is in §9 and is not a result.

It is the measured counterpart to `bench-transport-macos.md` (issue #16), which was written
with **no board attached** and therefore marks every hardware-dependent claim unverified.
Two of those claims are **refuted** here in §6 and §7a; both survive in that record and are
corrected in §10 of the spec rather than edited into it (the same treatment
`s3-bringup.md` §7 gives its own corrections).

---

## 1. Host inventory

| Item | Observed value | How |
| --- | --- | --- |
| macOS / arch | `27.2`, `arm64` | `sw_vers -productVersion`, `uname -m` |
| Container runtime | **OrbStack 2.2.3** (`2020300`), `docker` server `29.4.0`, driver `overlay2` | `orb version`, `docker info --format …` |
| Docker Desktop | **not installed** (`/Applications/Docker.app` absent) | `ls -d /Applications/Docker.app` |
| Reached via `docker` | yes — `/usr/local/bin/docker` talks to OrbStack | `docker info` returns `OrbStack` |
| WCH `CH34x` driver | **not installed and not loaded** | see §5 |
| esptool | `v5.4.0` (standalone venv); `v4.12.0` inside the IDF venv | `esptool version` |
| ESP-IDF | `v5.5.5` at `~/esp/esp-idf-v5.5.5` | `idf.py --version` |

## 2. The board's USB identity, and what determines the node name

`ioreg -p IOUSB -w0 -l`:

| Field | Value |
| --- | --- |
| idVendor:idProduct | `0x1A86:0x55D3` (QinHeng) |
| USB Product Name | `USB Single Serial` |
| USB Serial Number | `5C93063985` |
| LocationID | `17891328` (`0x1110000`) |
| Device Speed | `1` (Full Speed) |
| Callout node | `/dev/cu.usbmodem5C930639851` |

`bench-transport-macos.md` §3 claims macOS derives the node name from the **LocationID**
(`LocationID 0x11100000` → `usbmodem11101`). **It does not.** The node is the **USB Serial
Number** plus the interface number:

| Device | USB Serial Number | LocationID | Observed node |
| --- | --- | --- | --- |
| The board | `5C93063985` | `0x1110000` | `/dev/cu.usbmodem5C930639851` |
| Type-C Digital AV Adapter | `000000000000` | `0x1140000` | `/dev/cu.usbmodem0000001` |

The adapter is the control: if the name were topology-derived it would contain `11400000`;
it contains `000000000000` instead. **Consequence, and it is the opposite of the record's:**
a serial-number-derived name is stable across *physical ports*, so it is *better* than the
record claimed for pinning a re-plugged board — see §9, item 2, for what is still untested.

This matters because there are **two** `usbmodem` nodes on this host (the board and the AV
adapter), so a `usbmodem*` glob is ambiguous and must not be used as discovery.

## 3. OrbStack sees the board as a USB device

```
$ orb usb list
01110000  1a86:55d3  QinHeng Electronics USB Single Serial
01130000  0777:0777  PITC Pro2 Controller
01140000  343c:0000  xxxxxxxx USB Type-C Digital AV Adapter
```

The id column is the LocationID in hex without the `0x`, matching §2's `0x1110000`.

## 4. The macOS node maps into a container as `/dev/ttyACM0`

```
$ docker run --rm --device=/dev/cu.usbmodem5C930639851:/dev/ttyACM0 alpine \
      sh -c 'ls -l /dev/tty*; ls -l /dev/serial/by-id/'
crw-rw-rw- 1 root root     5,   0 /dev/tty
crw-rw---- 1 root dialout 238,  0 /dev/ttyACM0
ls: /dev/serial/by-id/: No such file or directory
```

- `238,0` is the Linux `cdc_acm` major/minor, so OrbStack presents a real device node, not a
  proxy.
- **The container-internal path is `/dev/ttyACM0`** — the spec's default (§10.5), unchanged.
- No `/dev/serial/by-id/` inside the container either, so the pin-by-id affordance the spec
  names for Linux has no macOS-side equivalent *or* container-side substitute.

## 5. No WCH driver is installed, and none is needed

Three independent checks, all negative:

```
$ systemextensionsctl list | grep -i -E 'ch34|wch'   # no match
$ ls /Library/Extensions | grep -i -E 'ch34|wch'     # no match
$ kextstat | grep -i -E 'ch34|wch'                   # no match
```

The matching driver for the node is Apple's own:

```
$ ioreg -c IOSerialBSDClient -w0 | grep -A2 usbmodem5C930639851
"IOTTYDevice"          = "usbmodem5C930639851"
"IOCalloutDevice"      = "/dev/cu.usbmodem5C930639851"
"CFBundleIdentifierKernel" = "com.apple.iokit.IOSerialFamily"
```

So the node is `usbmodem*` (Apple CDC-ACM), not `wchusbserial*` (WCH DriverKit).

## 6. Flashing works on the Apple driver — refuting the `01070000` claim

`bench-transport-macos.md` §2 states that under Apple's CDC driver "running `esptool.py` or
`idf.py flash` … fails during stub transmission with `A fatal error occurred: Failed to
write to target RAM (result was 01070000: Operation timed out)`" and that reliable flashing
"requires the WCH vendor DriverKit package".

With **no** WCH driver present (§5), against the `usbmodem` node:

```
$ esptool -p /dev/cu.usbmodem5C930639851 -b 460800 --after hard-reset flash-id
Connecting.... Detecting chip type... ESP32-S3
Uploading stub flasher...
Running stub flasher.                 <-- the step the record says fails
Changing baud rate to 460800...
Changed.
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Crystal frequency:  40MHz
MAC:                84:fc:e6:58:57:88
Detected flash size: 16MB
Hard resetting via RTS pin...
```

Stub upload — the operation that *is* a write to target RAM — completes, at 460800.

## 7a. A full 256 KiB write to flash, incompressible, round-tripped

`flash-id` only proves a small write. `storage` (offset `0x620000`, 10048 KiB) is
**unclaimed** by design (G-3, §7.8), so it is the one region a destructive test may touch.
A read of 256 KiB was saved first, then random data was written, read back, and the original
bytes restored.

```
$ head -c 262144 /dev/urandom > /tmp/rand.bin
$ esptool -p … -b 460800 write-flash 0x620000 /tmp/rand.bin
Writing at 0x00660000 … 100.0% 256.00kB/256.00kB [6s]
Wrote 262144 bytes at 0x00620000 in 6.2 seconds (338.4 kbit/s).
Hash of data verified.
$ esptool -p … -b 460800 read-flash 0x620000 0x40000 /tmp/rand_read.bin
$ cmp /tmp/rand.bin /tmp/rand_read.bin     # identical

# restore
$ esptool -p … -b 460800 write-flash 0x620000 /tmp/before.bin
Wrote 262144 bytes (2657 compressed) at 0x00620000 in 1.6 seconds.
Hash of data verified.
$ esptool -p … -b 460800 read-flash 0x620000 0x40000 /tmp/restored.bin
$ shasum -a 256 before.bin restored.bin
02841dd1a36a46ecfe6a836cc172a0b99db3043711e114943b6d928192daf8a0  before.bin
02841dd1a36a46ecfe6a836cc172a0b99db3043711e114943b6d928192daf8a0  restored.bin
```

Notes that matter for reading this:
- esptool refused to compress the random payload (`Compressed size 262230 >= uncompressed
  262144 … will flash uncompressed`), so 262144 B genuinely crossed host→target. An earlier
  attempt with the *existing* (mostly-`0xFF`) content did **not** prove this: it compressed
  to 2657 B over the wire.
- Both the write and the read ran at `-b 460800`. `921600` is **not** tested here; that is
  G-1's question.
- `Hash of data verified` is esptool's own post-write check; the `cmp` and `sha256` are
  independent.

## 7b. The container opens the port, deasserts DTR/RTS, resets the board and reads its log

Run with `python3` + `py3-pyserial` inside `alpine`, using the spec's §10.5 open sequence
(`dsrdtr=False, rtscts=False`, `setDTR(False)`, `setRTS(False)`, and again in `finally`):

```
$ docker run --rm --device=/dev/cu.usbmodem5C930639851:/dev/ttyACM0 \
      -v probe.py:/probe.py:ro alpine sh -c 'apk add …; python3 /probe.py'
IDLE_BYTES=4337
BOOT_BYTES=4106
BOOT_HEAD=b'I (24) boot: ESP-IDF v5.5.5 2nd stage bootloader\r\n…'
```

So all three of `bench-transport-macos.md`'s open questions about ioctl fidelity are
answered here: the port opens with the lines deasserted, a 150 ms RTS pulse resets the
board, and 115200 log bytes come back — **from inside the container**, through OrbStack,
through Apple's driver.

## 7c. The mapping is non-exclusive: macOS keeps the node

After the container in §7b exited:

```
$ ls -l /dev/cu.usbmodem5C930639851      # still present
$ esptool -p /dev/cu.usbmodem5C930639851 -b 460800 --after hard-reset flash-id
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
MAC:                84:fc:e6:58:57:88
Hard resetting via RTS pin...
```

`--device` is therefore **forwarding, not attachment**: the board stays on macOS, so the
host can still flash it. This is the property that lets §10.5's "container stopped to flash"
rule survive unchanged, and it means `orb usb attach` (which *detaches* from macOS) must
**not** be used on this path.

## 8. What the board is currently running

From §7b's boot log — recorded because it answers "what does flashing overwrite":

```
I (24)  boot: ESP-IDF v5.5.5 2nd stage bootloader
I (25)  boot: compile time Sep 30 2026 20:05:36
I (35)  boot.esp32s3: SPI Mode : DIO
I (39)  boot.esp32s3: SPI Flash Size : 16MB
I (255) app_init: Project name:     ESP32-BLE5-NSController-Emulato
I (261) app_init: App version:      7164f28-dirty
I (265) app_init: Compile time:     Sep 30 2026 20:43:59
I (274) app_init: ESP-IDF:          v5.5.5
I (355) transport_layer: USB CDC transport initialized
W (355) transport_layer: No protocol instance available for current configuration
```

Its partition table matches `partitions_16mb_s3.csv` row for row (including
`storage` at `0x620000`), so the board carries an **S3 build of this repo** at `7164f28`,
not the `esp32-joycontrol` image `s3-bringup.md` §1 recorded — §11 of that record's flashing
on 2026-09-30 supersedes it.

Two observations that belong to other tickets and are **not** followed up here:
`No protocol instance available` means nothing answers the CONTROL protocol on this image
(that is #21/#22's work), and the enabled transport is `USB CDC`, whose port is the **native
USB** one and is not the `usbmodem` node measured above.

## 9. Not attempted

1. **`921600`** — every measurement here is at `460800`. G-1 owns the baud question (#33).
2. **Node-name stability across a replug or a port change** — the serial-number derivation
   in §2 predicts stability and the adapter is a control, but no replug was performed.
3. **Two holders of the port at once** — §10.5 forbids it; it was not tested, and neither
   was what OrbStack does when the container holds a port the host also opens.
4. **`idf.py flash` / `idf.py monitor` on macOS**, as opposed to bare `esptool` — §6/§7a used
   esptool directly. The IDF-side path is exercised by the first bench ticket that flashes.
5. **The `0777:0777 PITC Pro2 Controller`** on the bus (§3) — unidentified, and not attached
   or interrogated. It may be a third-party Pro Controller clone; if so it is a candidate
   confounder for the console's grip-order screen and belongs to whichever bench ticket
   looks at discovery.
6. **Whether the `storage` region is non-`0xFF` anywhere below the 256 KiB read** — only
   `0x620000`–`0x660000` was read and restored.
