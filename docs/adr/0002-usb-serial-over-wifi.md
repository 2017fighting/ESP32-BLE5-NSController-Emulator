# USB serial over WiFi

The container and the device talk over a USB serial link and nothing else. There is no
WiFi, no on-device HTTP server, and no network control path — not even as a fallback.

The obvious alternative for an ESP32 is a network control plane: the chip has WiFi, the
board already ran a WiFi-exposed command shell before this effort, and a web UI could talk
to the device directly instead of to a container. It was rejected because the console link
is a 5 ms BLE connection that must not compete with a WiFi radio for the same 2.4 GHz band
on the same chip, because a serial wire is a trust boundary that a network is not, and
because the container has to exist anyway to own the macro and amiibo libraries and the
retail key.

## Consequences

Deployment is a `--device` passthrough plus read-only bind mounts rather than a network
topology. The device has no listening socket, so its whole attack surface is the serial
link. A future USB-host passthrough on the OTG port is the only other wired path, and it is
out of scope.
