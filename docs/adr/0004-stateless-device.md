# Stateless device: no durable state beyond the bond

The device persists exactly one thing across a power cycle: the BLE pairing keys for the
console. A compiled plan, a placed tag, the mode and all frame counters are volatile, die on
reboot, and are re-uploaded by the container on reconnect.

"Stateless" is read precisely, because the loose reading is wrong: the device very much does
hold *volatile execution state* — plan bytes, mode, frame index, loop count — and it reports
that state on `STATUS`. What it never does is accumulate durable state or make a decision
that depends on history it stored.

The alternative — flash the plan, cache tags, let the device boot into the last mode — is the
obvious convenience and was rejected on three counts: a device that holds a library has to be
kept in sync with the container, a persisted plan can be replayed after the container has
forgotten it exists, and every durable byte on the board is another thing the user's data can
outlive.

## Consequences

- After power-up nothing resumes: `mode=IDLE`, `plan=none`, `tag=none`. The container treats
  a new `boot_id` as "discard everything and re-upload".
- The tag is sealed in the container, so the retail key never reaches the board and no durable
  storage on the device can hold it (ADR-0012).
- The ~10 MB `storage` partition has no owner; it must be claimed with a reason or dropped.
- Recovery from a device reboot mid-session is ordinary operation, not an error path — the
  console's own sleep/wake cycle reboots this board on roughly half of slow cycles.
