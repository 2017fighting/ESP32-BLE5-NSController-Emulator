# Firmware owns pairing; the container observes

Bonding is the firmware's: it runs the console's out-of-band handshake, stores the keys in
NVS, and reconnects on its own. The container never pairs, never sees key material, and can
only *request* an unpair, which the device may act on.

The alternative is a container-owned pairing flow, which would mean the container holds and
manages bonding state and the device exposes enough of the BLE stack for the container to
drive it. It was rejected because the console's pairing is a stateful handshake inside the
GATT layer that the firmware is already in the middle of, because the bond is the one durable
thing the device keeps (ADR-0004) and splitting ownership of it across two processes is how
it gets lost, and because the container has nothing to gain: it needs to know *whether* the
console is paired, not to perform the pairing.

## Consequences

- The UI states the link and the bond as observed facts and offers no "pair now" button it
  cannot honour; pairing is started from the console's own controller menu.
- A paired device wake-advertises to the console it paired with, which makes a stale bond
  invisible to a *different* console — a bring-up trap that cost real bench time, and the
  reason an unpair request exists.
- Pairing survives a device reboot, so a `boot_id` change does not imply re-pairing.
