# The macOS bench path is OrbStack forwarding, and the host keeps the port

The container reaches the board by **forwarding** the macOS serial node into it —
`--device=/dev/cu.usbmodem<serial><iface>:/dev/ttyACM0` on OrbStack — not by detaching the
board into a VM and not by running the container's code as a macOS process. Inside the
container the port is `/dev/ttyACM0`, the path the rest of the design already assumes, and
on macOS the node keeps working the whole time, so flashing stays a host-side act with the
container stopped.

Three alternatives were live, each with a real cost:

- **A native venv on macOS** (`aiohttp` + `pyserial-asyncio`). Shortest path to the wire and
  no virtualisation, but it makes the container *optional on the only bench host there is*.
  `docker compose` would then never be exercised where the work happens, the retail key
  would have to exist on the host filesystem (ADR-0012 is written against a read-only mount,
  not a bare path), and ADR-0002's deployment story — a `--device` passthrough plus read-only
  binds — would go unvalidated until some later day.
- **`orb usb attach` (dedicated passthrough).** The Linux kernel would claim `1a86:55d3`
  natively as `/dev/ttyACM0` with no forwarding layer at all. Rejected on a measured fact:
  attaching **detaches the device from macOS**, and the design requires the host to flash the
  board on the same wire (§10.2). Every flash would become an attach/detach dance, and a
  failed detach would present as "the board vanished" rather than as a port error.
- **A Linux host or VM.** The Mac would only flash, and every bench loop in §12.2 would cost
  a second machine. Kept as a documented fallback, not as the assumed path.

And one that was considered and refused outright: a `socat`/TCP serial bridge, which strips
the USB descriptors discovery depends on and puts a socket in the middle of a 5 ms link.

The trade-off the forwarding path accepts is a dependency on OrbStack's userspace forwarder
and a macOS-specific compose override. Both are cheap next to the alternatives, and the
thing they buy is that **no code and no chapter has to change**: the container's port, its
open sequence, and its mount rules are exactly what §10 already says.

The decision rests on measurement, not on the vendor documentation, because the earlier
research record on this subject was written with no board attached and got two of its
hardware-dependent claims wrong. `docs/research/macos-bench-path.md` holds the evidence:
serial forwarding puts a real `cdc_acm` node in the container (§4), the port opens with
DTR/RTS deasserted and an RTS pulse resets the board *from inside the container* (§7b), the
macOS node survives the container's use so the host can still flash (§7c), a 256 KiB
incompressible write to flash round-trips on Apple's own driver (§7a), and the node name is
derived from the USB Serial Number rather than from USB topology (§2).

## Consequences

- **Two compose files, both committed.** `container/compose.yaml` stays the Linux canonical
  form with `/dev/ttyACM0` passed straight through; `container/compose.macos.yaml` is an
  override that changes only the `devices:` mapping. The platform difference is versioned
  rather than buried in a prose paragraph that drifts.
- **The WCH `CH34x` DriverKit package is not required and is not installed.** Stub upload,
  baud change, `flash-id`, a 256 KiB write and a 256 KiB read all succeed on Apple's
  `com.apple.iokit.IOSerialFamily` against `/dev/cu.usbmodem*`. This refutes
  `bench-transport-macos.md` §2, whose `01070000` failure did not reproduce. Nothing on this
  host needs admin approval, a system extension, or a reboot to flash.
- **`orb usb attach` must not be used on this path.** It detaches the board from macOS and
  breaks host-side flashing. The correct target is OrbStack's *forwarded* serial mode,
  reached by plain `--device`, which keeps both sides usable.
- **macOS has no `/dev/serial/by-id`, so §10.5's pin-by-id rule needs a macOS counterpart.**
  The node is `<USB Serial Number><iface>`, so the board is pinned **by serial number**
  (`SER=5C93063985`), resolved through `scripts/find_serial_port.py`. A `usbmodem*` glob is
  not an option: this host already has two such nodes, and the ticket that says "pin the
  re-plugged board" is exactly the situation a glob gets wrong.
- **`921600` remains untested.** Every measurement here is at `460800`; G-1 still owns the
  baud question.
- **Docker Desktop is not the runtime on this host** — OrbStack is — while the documented
  deployment stays the Linux compose. The macOS override is the only place the two diverge.
- **The Linux bench host stays reachable but is not the assumed path.** A bench ticket that
  needs a second machine is now a scoping act rather than a default.
- **Node-name stability across a replug is predicted, not measured.** The serial-number
  derivation and the AV-adapter control support it; no replug was performed
  (`macos-bench-path.md` §9).
