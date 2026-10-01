# Research: Bench transport on macOS arm64 (OrbStack, CH9102, port paths)

**Scope:** how the container reaches the board on an arm64 Mac (macOS 27.2, T8103) running OrbStack — the control link's port, driver and passthrough path, and what changes from the x86-64 Linux bench.
**Date:** 2026-10-02. **Wayfinder ticket:** #16. **Hardware on test host:** none — the board was not attached, so every hardware-dependent claim below is unverified and marked as such.
**Sourcing:** web sources are cited by URL; in-repo claims cite `path:line`. The OrbStack *capability* is from first-party docs; the version history is from OrbStack's issue tracker and is lower-confidence.


## Summary
OrbStack v2.2.0+ supports USB serial transport to Docker containers either via automatic serial forwarding (`--device=/dev/tty.<node>:/dev/ttyACM0`) or via dedicated USB passthrough (`orb usb attach <id>`). On macOS 27.x arm64, Apple's built-in CDC-ACM driver claims the CH9102 (`1a86:55d3`) as `/dev/cu.usbmodem*`, but reliable firmware flashing with `esptool` requires the WCH vendor DriverKit package `CH34XSER_MAC.ZIP` (`CH34xVCPDriver.app`), creating `/dev/cu.wchusbserial*`. The spec's mandatory DTR/RTS deassert rule applies identically on macOS because pySerial on Darwin executes the same `TIOCMBIS` ioctls on open, pulling EN low and holding the ESP32-S3 in reset.

---

## Findings

### 1. OrbStack Container Passthrough for USB Serial
1. **Claim:** OrbStack can pass USB serial devices into Docker containers; starting in v2.2.0, serial/UART devices are automatically forwarded to Linux, and containers mount them via `devices:` / `--device`.
   **Sources:** [OrbStack USB Documentation](https://docs.orbstack.dev/features/usb); [OrbStack Issue #2366](https://github.com/orbstack/orbstack/issues/2366); [OrbStack Issue #2511](https://github.com/orbstack/orbstack/issues/2511); [OrbStack Release Notes v2.2.0–v2.2.3](https://docs.orbstack.dev/release-notes).
   **Support:** direct evidence.
   **Confidence:** high.
   **Explanation:**
   - In OrbStack v2.0.5, attempting `devices: - /dev/tty.usbserial-*:...` failed with `Error response from daemon: ... not a device node` ([Issue #2366](https://github.com/orbstack/orbstack/issues/2366)).
   - In OrbStack v2.2.0 (June 4, 2026), OrbStack added USB passthrough and serial forwarding ([Release Notes v2.2.0](https://docs.orbstack.dev/release-notes#v2-2-0-jun-4)).
   - OrbStack provides two distinct USB modes:
     1. **Forwarded (default for serial/UART):** The serial port is shared between macOS and the Linux VM without disconnecting it from macOS. In the Linux VM guest, it appears as a character device preserving the macOS tty name (e.g. `/dev/tty.usbmodem*` or `/dev/tty.wchusbserial*`).
     2. **Dedicated Passthrough:** The raw USB device is detached from macOS and passed into the Linux kernel via `orb usb attach <id>` (or OrbStack GUI > Devices > Attach). In dedicated mode, the Linux kernel `cdc_acm` driver claims `1a86:55d3` natively as `/dev/ttyACM0`.
   - **Docker configuration for forwarded mode:** The character device is mapped into the container using standard Docker device syntax:
     ```yaml
     services:
       controller:
         devices:
           - "/dev/tty.wchusbserial<ID>:/dev/ttyACM0"
     ```
     or CLI:
     ```sh
     docker run --device=/dev/tty.wchusbserial<ID>:/dev/ttyACM0 ...
     ```
   - **Bug and fix history:** In OrbStack v2.2.1, accessing forwarded serial devices inside Docker containers triggered `*** stack smashing detected ***` ([Issue #2511](https://github.com/orbstack/orbstack/issues/2511)). This was resolved in v2.2.2 (August 2, 2026), and v2.2.3 (August 7, 2026) added fixes for `pyserial` tools opening forwarded serial devices.

---

### 2. Driver Requirements for CH9102 (`1a86:55d3`) on macOS 27.x
2. **Claim:** Apple's built-in CDC-ACM driver (`com.apple.driver.usb.cdc.acm` / `AppleUSBACM`) claims the CH9102 automatically, but firmware uploading via `esptool` requires the WCH vendor driver (`CH34XSER_MAC.ZIP`).
   **Sources:** [WCH CH34XSER_MAC Driver](https://www.wch.cn/downloads/CH34XSER_MAC_ZIP.html); [WCHSoftGroup/ch34xser_macos](https://github.com/WCHSoftGroup/ch34xser_macos); [PlatformIO Community #26229](https://community.platformio.org/t/mac-upload-fails-to-dev-tty-usbmodem52d60049421/26229); [LilyGO Issue #139](https://github.com/Xinyuan-LilyGO/LilyGo-T-Call-SIM800/issues/139); [Espressif esptool Issue #1059](https://github.com/espressif/esptool/issues/1059).
   **Support:** direct evidence.
   **Confidence:** high.
   **Explanation:**
   - The WCH CH9102 (USB VID `0x1a86`, PID `0x55d3`) provides dual USB descriptors: standard USB Communication Device Class (CDC-ACM) and WCH vendor-specific serial.
   - Out-of-the-box on macOS (macOS 11 through macOS 26/27+), Apple's built-in `AppleUSBACM` driver claims the device without requiring third-party software.
   - **Flashing defect under Apple's CDC driver:** While serial monitoring works under Apple's driver, running `esptool.py` or `idf.py flash` against Apple's `/dev/cu.usbmodem*` node fails during stub transmission with:
     `A fatal error occurred: Failed to write to target RAM (result was 01070000: Operation timed out)`
   - **Required driver package:** Nanjing Qinheng Microelectronics (WCH) provides `CH34XSER_MAC.ZIP` ([wch.cn/downloads/CH34XSER_MAC_ZIP.html](https://www.wch.cn/downloads/CH34XSER_MAC_ZIP.html)). On macOS 11+ and Apple Silicon (ARM64), this installs `CH34xVCPDriver.app`, which registers a DriverKit Dext (user-space system extension).
   - Once `CH34xVCPDriver` is loaded, it overrides Apple's CDC driver and creates `/dev/cu.wchusbserial*`, which handles high-speed block transfers and flasher stub execution reliably.

---

### 3. Device Node Naming (`/dev/cu.*` vs `/dev/tty.*`) and Path Conventions
3. **Claim:** Outgoing serial connections on macOS must use `/dev/cu.*`; macOS `devfs` has no `/dev/serial/by-id` symlinks, deriving node names from physical USB topology instead.
   **Sources:** [Apple OpenSource IOSerialBSDClient.cpp](https://opensource.apple.com); [pySerial `list_ports_osx.py`](https://github.com/pyserial/pyserial/blob/master/serial/tools/list_ports_osx.py); [ESP-IDF Establish Serial Connection Guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/establish-serial-connection.html); `docs/spec/10-deployment.md:85-115`.
   **Support:** direct evidence.
   **Confidence:** high.
   **Explanation:**
   - **Node naming by driver:**
     - Apple CDC-ACM driver: `/dev/cu.usbmodem<LocationID>` and `/dev/tty.usbmodem<LocationID>`
     - WCH vendor driver: `/dev/cu.wchusbserial<LocationID>` and `/dev/tty.wchusbserial<LocationID>`
   - **/dev/cu.* vs /dev/tty.*:** In BSD/macOS termios, `/dev/tty.*` is the terminal/dial-in device and blocks on `open()` until the RS-232 Carrier Detect (DCD) line transitions high. `/dev/cu.*` (Calling Unit / call-out device) ignores DCD on open and opens immediately. Espressif official docs explicitly require `/dev/cu.*`.
   - **Implication for `/dev/ttyACM0 by default`:** macOS does not instantiate `/dev/ttyACM*`. When running natively on the Mac host, `/dev/ttyACM0` throws `FileNotFoundError`. When running under OrbStack containerization, mapping `/dev/tty.wchusbserial...:/dev/ttyACM0` maintains `/dev/ttyACM0` inside the container namespace without code changes.
   - **Implication for `/dev/serial/by-id`:** Linux `/dev/serial/by-id` is generated by `systemd-udevd`. macOS uses `devfs`, an in-kernel synthetic filesystem that does not support udev rules or persistent symlink aliasing.
   - **macOS persistent identification:** macOS encodes USB physical topology into the 32-bit Location ID (bus and port hierarchy, e.g. `LocationID 0x11100000` -> `usbmodem11101`). A board replugged into the *same physical port* retains the same name; moving ports changes the name. Persistent programmatic device identification on macOS is done via IOKit (`IOUSBHostDevice` properties `idVendor`, `idProduct`, `USB Serial Number`), accessible via `python -m serial.tools.list_ports -v` or `ioreg -p IOUSB -l`.

---

### 4. Flashing Tools (`idf.py`/`esptool`) and the DTR/RTS Reset Trap on macOS
4. **Claim:** `idf.py -p /dev/cu.<node> flash` and `esptool.py` operate on arm64 macOS, and the spec's DTR/RTS deassert rule is equally mandatory on macOS.
   **Sources:** [pySerial `serialposix.py:310-330`](https://github.com/pyserial/pyserial/blob/master/serial/serialposix.py); `docs/spec/08-container-architecture.md:55-80`; `docs/spec/10-deployment.md:120-135`; `docs/research/s3-bringup.md:400-435`.
   **Support:** direct evidence.
   **Confidence:** high.
   **Explanation:**
   - On arm64 macOS (Apple Silicon), ESP-IDF v5.5.5 and esptool run natively in Python 3. When targeting `/dev/cu.wchusbserial*`, flashing operates normally with standard auto-reset.
   - **Hardware reset wiring:** As measured in `s3-bringup.md:405-420`, the CH9102 bridge wires DTR to ESP32-S3 `GPIO0` and RTS to `EN`.
   - **Software behavior:** In pySerial POSIX implementation (`serial/serialposix.py:311`), `Serial.open()` automatically asserts DTR and RTS on open via `fcntl.ioctl(self.fd, TIOCMBIS, TIOCM_DTR_str)` and `TIOCMBIS TIOCM_RTS_str`.
   - On Darwin/macOS, `TIOCMBIS` is supported by the kernel and serial drivers. When both lines are asserted, the CH9102 pulls EN low, holding the ESP32-S3 in continuous hardware reset.
   - The failure mode on macOS is identical to Linux: zero bytes read from the port, followed by `SerialException: device reports readiness to read but returned no data`.
   - The spec's fixed open sequence (`docs/spec/08-container-architecture.md:65-75` and `docs/spec/10-deployment.md:125`) remains strictly required on macOS:
     ```python
     port = serial.Serial(PATH, BAUD, timeout=0.2, dsrdtr=False, rtscts=False)
     port.setDTR(False)
     port.setRTS(False)
     try:
         ...
     finally:
         port.setDTR(False)
         port.setRTS(False)
         port.close()
     ```

---

### 5. Transport Fallbacks and Costs
5. **Claim:** Four candidate fallbacks exist if container passthrough fails, each carrying a concrete architectural or operational cost.
   **Sources:** `docs/spec/01-system-overview.md`; `docs/spec/10-deployment.md`; [Espressif esptool VM Troubleshooting](https://docs.espressif.com/projects/esptool/en/latest/esp32s3/troubleshooting.html).
   **Support:** interpretation.
   **Confidence:** high.
   **Cost breakdown:**
   1. **Native Python venv on macOS (`aiohttp` + `pyserial-asyncio`):**
      - *Cost:* Breaches the container isolation contract (violates spec §10 deployment architecture), leaks the retail key path onto the host filesystem, and requires managing host-side native Python dependencies.
   2. **Full Linux VM (UTM / VMware Fusion with USB Controller Passthrough):**
      - *Cost:* Adds 1–2 GB dedicated RAM overhead, CPU virtualization tax, separate disk image lifecycle, and hypervisor management complexity.
   3. **`socat` / TCP serial bridge (macOS host daemon -> container):**
      - *Cost:* Strips raw USB descriptors (breaking tool VID/PID discovery), introduces TCP socket latency and jitter into the 100 Hz HID control link, and drops modem control line toggling unless RFC 2217 is fully implemented.
   4. **Separate dedicated Linux host (Raspberry Pi or bench PC):**
      - *Cost:* Requires additional physical hardware, power, cabling, and a separate remote deployment target detached from the local Mac workstation.

---

## Contradictions
None found. Community reports that claimed "CH9102 works without drivers on macOS" were referring exclusively to serial monitoring/CDC console output; those same reports confirm that high-speed flash uploads fail with `01070000` until WCH's `CH34xVCPDriver.app` is installed.

---

## Missing evidence
1. **Empirical verification of macOS 27.2 CDC flasher behavior:** Whether macOS 27.2's built-in `AppleUSBACM` resolves the `01070000` transfer timeout on the specific hardware revision of this dev board cannot be verified without physical hardware plugged into the Mac.
2. **Exact Location ID:** The precise device node string (e.g. `/dev/cu.wchusbserial11101`) depends on the physical USB-C port topology of the T8103 machine and cannot be read without the board connected.
3. **OrbStack forwarded ioctl fidelity:** Whether OrbStack's userspace serial forwarder introduces edge-timing jitter during rapid DTR/RTS toggles must be benchmarked on hardware.

These three items are handed over to the bench validation ticket once hardware is attached.

---

## Sources
- Kept: [OrbStack USB Devices Documentation](https://docs.orbstack.dev/features/usb) — Primary documentation for OrbStack forwarded serial and dedicated USB passthrough mechanisms.
- Kept: [OrbStack Issue #2366](https://github.com/orbstack/orbstack/issues/2366) and [Issue #2511](https://github.com/orbstack/orbstack/issues/2511) — Primary evidence for Docker compose device mapping support and container stack-smashing fixes.
- Kept: [OrbStack Release Notes](https://docs.orbstack.dev/release-notes) — Definitive chronology of USB passthrough, serial support, and pyserial fixes (v2.2.0–v2.2.3).
- Kept: [WCH Official Driver Download](https://www.wch.cn/downloads/CH34XSER_MAC_ZIP.html) and [WCHSoftGroup/ch34xser_macos](https://github.com/WCHSoftGroup/ch34xser_macos) — Authoritative driver distribution and DriverKit installation instructions for CH9102 on macOS 11+.
- Kept: [pySerial POSIX implementation (`serialposix.py`)](https://github.com/pyserial/pyserial/blob/master/serial/serialposix.py) — Authoritative source code showing default assertion of DTR/RTS via `TIOCMBIS` on macOS Darwin.
- Kept: [Espressif esptool Troubleshooting Guide](https://docs.espressif.com/projects/esptool/en/latest/esp32s3/troubleshooting.html) — Official documentation on VM/container serial forwarding limits and USB descriptor visibility.
- Kept: `docs/spec/10-deployment.md` and `docs/research/s3-bringup.md` — In-tree bench facts and locked deployment rules for the CH9102 bridge.
- Rejected: Unofficial driver repackages on third-party blogs — Deprioritized in favor of WCH official releases.

---

## Next steps
1. When the board is plugged in, run `orb serial list` and `ls -l /dev/cu.*` on the host to capture the assigned Location ID.
2. Test whether `esptool.py --port /dev/cu.usbmodem<ID> flash_id` works under Apple's built-in driver, or if `CH34XSER_MAC.ZIP` is required to avoid the `01070000` write error.
3. Validate that Compose `devices: - "/dev/tty.<ID>:/dev/ttyACM0"` boots cleanly without resetting the board when `dsrdtr=False, rtscts=False` is enforced.
