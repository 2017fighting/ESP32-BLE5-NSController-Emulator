# Control plane shares the console/log wire

The container drives the device over UART0, the CH9102 "COM" bridge at `/dev/ttyACM0`,
with `ESP_LOG` multiplexed on the same wire. The native OTG port is left free for a
future USB-host passthrough and macro recorder.

This inverts the earlier finding that the control plane would ride USB/OTG and logs would
ride COM/UART as physically separate interfaces. The trade-off is real and was taken
deliberately: `CONFIG_ESP_CONSOLE_NONE` would have given a clean wire, but the boot log is
the instrument that found both defects in the base bring-up (extended advertising the
console cannot see, and the msys pool-selection bug that killed the link), and giving it up
to save a wire nobody was short of is a bad exchange. The costs are paid in the firmware
and the container rather than hidden:

- Framing must survive log text, so frames are COBS-delimited with a CRC'd header and
  resynchronisation scans to the next delimiter; a log line lands as CRC-failing noise.
- The device must not interleave log output with a frame's bytes, so `ESP_LOG` and control
  replies share one TX lock and logging is rate-suppressed while a bulk transfer is active.
- DTR/RTS must be deasserted by the container, because the CH9102 wires DTR→GPIO0 and
  RTS→EN and a host that asserts them holds the board in reset.
- Flashing and the container cannot both hold the port, so **flashing requires the container
  stopped**. Flashing stays manual and OTA stays out of scope.

## Consequences

The port name in the earlier constraint (`--device=/dev/ttyACM0`) stays correct as written —
on this host that node is the CH9102 (`1a86:55d3`), not the native OTG port.
