#!/usr/bin/env python3
"""Resolve the NS2 controller board's serial port on macOS.

Why this exists: §10.5 pins a re-plugged board by `/dev/serial/by-id` on Linux, and macOS
has no `devfs` equivalent — no `udev`, no `/dev/serial/by-id`, no `/dev/ttyACM*`. What macOS
does give is a node named from the device's **USB Serial Number** plus its interface number:

    USB Serial Number   5C93063985
    node                /dev/cu.usbmodem5C930639851

That name is stable across physical ports, but a `usbmodem*` glob is *not* a safe way to find
the board: an arm64 Mac commonly carries more than one such node (a Type-C AV adapter is a
second one), and picking the wrong one presents as the "wrong port" trap in §10.3. So the
board is resolved by serial number, and this script fails loudly on zero matches and on more
than one rather than guessing.

macOS-only, and deliberately dependency-free: it reads `ioreg` rather than pyserial, so it
works on a host with no Python packages installed.

    scripts/find_serial_port.py                  # the board, by its pinned serial
    scripts/find_serial_port.py --serial 5C93063985
    scripts/find_serial_port.py --json           # for a wrapper

Exit codes: 0 = exactly one node, printed; 2 = none found; 3 = more than one found.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# The CH343/CH9102-class bridge on the S3-N16R8 board (§10.7).
DEFAULT_VENDOR_ID = 0x1A86
DEFAULT_PRODUCT_ID = 0x55D3

# The board this repo is benched on. Pinning the serial is the macOS form of `/dev/serial/by-id`.
DEFAULT_SERIAL = "5C93063985"

# `+-o USB Single Serial@01110000  <class IOUSBHostDevice, id …>`
_DEVICE_RE = re.compile(
    r"^\s*[|+ ]*\+-o\s+(?P<name>.+?)@(?P<location>[0-9A-Fa-f]+)\s+<class\s",
)
# `  |   |     "idVendor" = 6790`
_PROPERTY_RE = re.compile(r'^\s*[|+ ]*"(?P<key>[^"]+)"\s*=\s*(?P<value>.*?)\s*$')


@dataclass(frozen=True)
class UsbDevice:
    """One `+-o <name>@<location>` block from `ioreg -p IOUSB -w0 -l`."""

    name: str
    location_id: int
    properties: Mapping[str, object]

    def _int(self, key: str) -> int | None:
        value = self.properties.get(key)
        return value if isinstance(value, int) else None

    @property
    def vendor_id(self) -> int | None:
        return self._int("idVendor")

    @property
    def product_id(self) -> int | None:
        return self._int("idProduct")

    @property
    def serial(self) -> str | None:
        # macOS exposes both spellings; `USB Serial Number` is the documented one.
        for key in ("USB Serial Number", "kUSBSerialNumberString"):
            value = self.properties.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def product(self) -> str | None:
        value = self.properties.get("USB Product Name")
        return value if isinstance(value, str) else None


def _parse_value(raw: str) -> object:
    """`6790` -> int, `"5C93063985"` -> str, `{...}`/`<...>`/`(...)` -> raw text."""
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    if raw.isdigit():
        return int(raw)
    return raw


def parse_usb_devices(ioreg_text: str) -> list[UsbDevice]:
    """Parse `ioreg -p IOUSB -w0 -l` output.

    Properties attach to the most recent device header, which is how ioreg prints them: a
    device's dict follows its own header, and a child device's header follows its parent's
    dict. Nested one-line values (`{...}`, `<...>`, `(...)`) are kept verbatim.
    """
    devices: list[UsbDevice] = []
    name: str | None = None
    location: int | None = None
    props: dict[str, object] = {}

    def flush() -> None:
        if name is not None and location is not None:
            devices.append(UsbDevice(name=name, location_id=location, properties=dict(props)))

    for line in ioreg_text.splitlines():
        header = _DEVICE_RE.match(line)
        if header:
            flush()
            name = header.group("name").strip()
            location = int(header.group("location"), 16)
            props = {}
            continue
        if name is None:
            continue
        prop = _PROPERTY_RE.match(line)
        if prop:
            props[prop.group("key")] = _parse_value(prop.group("value"))

    flush()
    return devices


def match_devices(
    devices: Iterable[UsbDevice],
    vendor_id: int = DEFAULT_VENDOR_ID,
    product_id: int = DEFAULT_PRODUCT_ID,
    serial: str | None = None,
) -> list[UsbDevice]:
    """USB devices matching the bridge's VID:PID, optionally narrowed to one serial."""
    return [
        device
        for device in devices
        if device.vendor_id == vendor_id
        and device.product_id == product_id
        and (serial is None or device.serial == serial)
    ]


def node_for_serial(serial: str, dev_dir: Path = Path("/dev")) -> list[Path]:
    """The callout nodes macOS created for a device with this USB Serial Number.

    macOS names them `cu.usbmodem<serial><interface>`. The suffix is not derived here — it is
    *matched*, so a change in macOS's naming shows up as "no node" rather than as a wrong
    path. `/dev/cu.*` and never `/dev/tty.*`: the latter blocks on open until carrier detect.
    """
    if not serial or "/" in serial:
        raise ValueError(f"refusing to glob on a suspicious serial number: {serial!r}")
    return sorted(dev_dir.glob(f"cu.usbmodem{serial}*"))


def resolve(
    devices: Sequence[UsbDevice],
    vendor_id: int = DEFAULT_VENDOR_ID,
    product_id: int = DEFAULT_PRODUCT_ID,
    serial: str | None = None,
    dev_dir: Path = Path("/dev"),
) -> list[Path]:
    """Nodes for every matching USB device, deduplicated and ordered."""
    found: list[Path] = []
    for device in match_devices(devices, vendor_id, product_id, serial):
        for node in node_for_serial(device.serial or "", dev_dir):
            if node not in found:
                found.append(node)
    return found


def read_ioreg() -> str:
    result = subprocess.run(
        ["ioreg", "-p", "IOUSB", "-w0", "-l"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help="USB Serial Number to pin")
    parser.add_argument(
        "--vid", type=lambda v: int(v, 0), default=DEFAULT_VENDOR_ID, help="USB vendor id"
    )
    parser.add_argument(
        "--pid", type=lambda v: int(v, 0), default=DEFAULT_PRODUCT_ID, help="USB product id"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the path")
    parser.add_argument("--ioreg-file", type=Path, help="parse a saved ioreg dump instead")
    parser.add_argument(
        "--dev-dir", type=Path, default=Path("/dev"), help="directory to look for nodes in"
    )
    args = parser.parse_args(argv)

    text = args.ioreg_file.read_text() if args.ioreg_file else read_ioreg()
    devices = parse_usb_devices(text)
    bridged = match_devices(devices, args.vid, args.pid)
    nodes = resolve(devices, args.vid, args.pid, args.serial, args.dev_dir)

    if args.json:
        json.dump(
            {
                "serial": args.serial,
                "nodes": [str(node) for node in nodes],
                "matches": [
                    {
                        "name": device.name,
                        "serial": device.serial,
                        "product": device.product,
                        "location_id": device.location_id,
                    }
                    for device in bridged
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    elif nodes:
        print(nodes[0])

    if len(nodes) == 1:
        return 0
    if not nodes:
        print(
            f"find_serial_port: no node for {args.vid:#06x}:{args.pid:#06x} "
            f"serial={args.serial!r}. {len(bridged)} bridge device(s) on the bus. "
            "Is the board plugged in, and is it on the port that supplies a serial number?",
            file=sys.stderr,
        )
        return 2
    print(
        f"find_serial_port: ambiguous — {len(nodes)} nodes match serial {args.serial!r}: "
        + ", ".join(str(node) for node in nodes),
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
