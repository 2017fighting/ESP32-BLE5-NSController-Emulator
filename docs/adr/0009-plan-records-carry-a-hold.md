# Plan records carry a hold, not a fixed timestep

A compiled plan is a header plus one record per distinct controller state, and every record
carries its own `hold_ms`. The device replays records against the HID tick's wall clock: hold
the record, move to the next, and at the end of the loop emit neutral and start again.

The obvious sketch is a dense frame grid — one record per report tick, "state at 10 ms",
"state at 20 ms" — which is what a recording of a controller naturally looks like and makes
the executor a simple index-and-increment. It was rejected on size and on fidelity: a 100 Hz
grid for the four real macros costs roughly 140 KB against the 3.4 KB the event-preserving
plan needs, and a grid silently quantises the event stream to the chosen tick. Because holds
are milliseconds, playback speed is independent of `CONFIG.report_interval_ms` — changing the
HID tick changes report density, not the macro.

## Consequences

- Plan capacity is advertised in `HELLO` and a whole plan either fits or is rejected; there is
  no partial plan and no streaming.
- The device validates the header structurally (magic, format version, record size, and that
  the payload length matches the record count) before committing, so a truncated-but-CRC-valid
  transfer cannot replay as garbage.
- Because a hold is a u16, a single record cannot hold for more than 65535 ms; the compiler
  rejects a macro that needs one.
