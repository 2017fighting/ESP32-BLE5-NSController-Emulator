# The device owns replay timing

The container compiles a macro into a plan once and uploads the bytes; the device replays
that plan from RAM against the HID tick's own wall clock and loops it. The container sends
`START` and `STOP` and then only observes.

The alternative is a streamed control plane — the container keeps the clock and pushes each
controller state as it comes due — which is how a naive design and every "macro player"
script works. It was rejected because it makes the replay's correctness a property of USB
scheduling and host load, turns a dropped or reordered message into a wrong input, and puts
the container on the critical path of a 5 ms BLE link. Device-owned timing costs one upload
per macro and buys a replay whose fidelity does not depend on the host at all.

## Consequences

- The plan is compiled, not interpreted: no JSON engine and no event scheduler on the device
  (see ADR-0009 for what the compiler emits).
- A plan's timing is in milliseconds, so changing the report interval changes report density
  and not playback speed, and no recompile is needed.
- Upload is the only thing retried, addressed by offset; control verbs are never retransmitted.
- A replay survives the container's death and the console's sleep (ADR-0008).
- The container cannot see progress except by polling `STATUS`; there is no per-frame channel.
