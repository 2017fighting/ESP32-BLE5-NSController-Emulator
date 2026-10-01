# NS2 controller emulator — locked design

The design of two artifacts that work together: an **ESP32-S3** board that a Nintendo
Switch 2 sees as a Pro Controller, and a **container** that owns the macro and amiibo
libraries and drives the board over USB serial.

This spec is the design lock. It settles what to build and why; it does not contain
implementation. Everything here traces to a resolved decision in the wayfinding map,
whose evidence lives in `docs/research/`. Where a question stayed open, the spec says
so and hands it to the implementation effort as a **known gap** rather than inventing an
answer.

## Read in this order

| # | Chapter | What it settles |
| --- | --- | --- |
| 1 | [system-overview.md](01-system-overview.md) | The two artifacts, the two links, the cast, and the shape of each half |
| 2 | [control-plane-protocol.md](02-control-plane-protocol.md) | Framing, the ten verbs, `HELLO` capabilities, the canonical `ERROR` table, versioning, recovery |
| 3 | [status-and-events.md](03-status-and-events.md) | What `STATUS` reports, when `EVENT` fires, and how the container reads a stop |
| 4 | [mode-state-machine.md](04-mode-state-machine.md) | The five axes, the full transition table, panic stop, the release invariant, link loss |
| 5 | [plan-format.md](05-plan-format.md) | Macro ingestion, compilation and the `NSPL` binary layout |
| 6 | [amiibo.md](06-amiibo.md) | Figure → Identity → Tag, sealing, the 540-byte layout, freshness, key policy, the feasibility branch |
| 7 | [firmware-architecture.md](07-firmware-architecture.md) | The code that exists, what the changes are, and the memory budget |
| 8 | [container-architecture.md](08-container-architecture.md) | One process, four seams, four screens, the HTTP API |
| 9 | [console-lifecycle.md](09-console-lifecycle.md) | What the real NS2 does, what the container does about it, and what is unverified |
| 10 | [deployment.md](10-deployment.md) | Build, flash, run, and the operational rules that read as bugs if forgotten |
| 11 | [worked-traces.md](11-worked-traces.md) | Three end-to-end sequences, used to check the chapters agree |
| 12 | [handoff.md](12-handoff.md) | The order to build in, what needs the bench first, and the known gaps |

## Decisions of record

`docs/adr/` holds the choices that are hard to reverse, surprising without context, and
the result of a real trade-off. The spec assumes them; it does not restate them.

| ADR | Decision |
| --- | --- |
| [0001](../adr/0001-control-plane-shares-the-console-wire.md) | The control plane shares the console/log wire, with log-proof framing |
| [0002](../adr/0002-usb-serial-over-wifi.md) | USB serial, never WiFi |
| [0003](../adr/0003-device-owns-replay-timing.md) | The device owns replay timing; the container sends a compiled plan |
| [0004](../adr/0004-stateless-device.md) | No durable device state beyond the bond |
| [0005](../adr/0005-master-firmware-base.md) | Upstream `master` as the firmware base; nothing reused from the prior attempt |
| [0006](../adr/0006-cobs-framing.md) | One COBS + CRC framing for control and bulk |
| [0007](../adr/0007-explicit-mode-transitions.md) | The mode moves only on an explicit verb |
| [0008](../adr/0008-device-watches-neither-link.md) | The device watches neither link |
| [0009](../adr/0009-plan-records-carry-a-hold.md) | Plan records carry a hold, not a fixed timestep |
| [0010](../adr/0010-container-owned-plan-identity.md) | Plan identity is a container-owned hash, memory-only cache |
| [0011](../adr/0011-amiibo-identity-minted-container-side.md) | Identity is minted container-side; the device is a byte-sink |
| [0012](../adr/0012-key-material-is-a-read-only-mount.md) | Key material is a read-only mount, never written, never forwarded |
| [0013](../adr/0013-firmware-owns-pairing.md) | The firmware owns pairing; the container observes |

## Vocabulary

`CONTEXT.md` at the repo root is the glossary and the only authority on what a term means.
The spec uses its words exactly: *mode*, `MACRO`/`AMIIBO`/`IDLE`, *macro* (the JSON),
*plan* (the compiled bytes), *plan frame*, *hold*, *loop*, *tag*, *identity*, *figure*,
*sealing*, *placement*, *scan*, *rotation*, *staging*, *committed*, *neutral*, *verb*,
*frame*, *frame type*, *control link*, *console link*, *bond*, *boot id*, *panic stop*.

## Consistency pass

Every resolved ticket was re-read against the others and against the map's locked constraints
(Q1–Q17), and the written spec was checked for silent re-introductions of ruled-out scope.
**No two resolutions contradict**, but eleven places needed an explicit recording rather than a
quiet choice, because each is a place where an earlier statement was superseded or a term was
ambiguous. All eleven are annotated *in the chapter that owns them*, so the resolution is
visible where the claim is made:

| # | What changed, and where it is recorded |
| --- | --- |
| 1 | **Control rides the console/log wire**, inverting #4's "physically separate" gist — ADR-0001, and §1.2's link table names the port |
| 2 | **The device cannot report the control link** (a report about it would have to travel over it) — §3.3; the container infers it |
| 3 | **`plan_committed` dropped as redundant**; `plan_state` is kept because zero is a legal hash prefix — §3.2 |
| 4 | **`NO_TAG` does not exist** — `PLACE_AMIIBO` carries its own bytes — §2.5 |
| 5 | **`KEY_MISSING` left the wire** when sealing moved container-side; the container's local lock states carry it — §2.5, §6.7 |
| 6 | **`last_stop_reason` gained `NONE`** — a device that booted and has not stopped — §3.4 |
| 7 | **The staging axis dropped `KEY`**, a state nothing can reach — §4.1 |
| 8 | **`STATUS` gained the amiibo fields** (`tag_state`, `tag_identity`, `console_polling`), named as an extension rather than smuggled in — §3.2 |
| 9 | **`LOOP_COMPLETED` is rate-limited to the poll rate**, because a short macro can otherwise saturate the link — §3.3 |
| 10 | **The IDF floor is v5.5.5, not v5.5.4+**, and `package_firmware.py` exits 0 on failure — §10.1 |
| 11 | **#13 declined an ADR for its own key policy and this spec wrote one anyway** — §00, ADR-0012, with the disagreement recorded instead of silently resolved |

Four things ruled out elsewhere were checked for re-introduction and are **not** present anywhere
in the spec: WiFi or an on-device HTTP server; OTA or flashing over the control link; a
controller-recording or amiibo-authoring surface; and any reuse of the untrusted prior attempt
as a code source. Two more are present only as refusals: the OTG passthrough (reserved, §1.6)
and the storage/OTA partitions (unclaimed, §7.8).

## ADR selection

Sixteen candidates were raised across the map. They were judged against the bar — hard to
reverse, surprising without context, the result of a real trade-off — and the three that did
not clear all three were **declined rather than written as padding**. The record, because
"why isn't there an ADR for X" is its own question later:

| Candidate | From | Judgement |
| --- | --- | --- |
| Control plane shares the console/log wire | #5 | **Accepted** — ADR-0001 |
| USB serial over WiFi | map Q2 | **Accepted** — ADR-0002 |
| Device owns replay timing | map Q6 | **Accepted** — ADR-0003 |
| Stateless device (Q9) | map Q9 | **Accepted** — ADR-0004 |
| Master firmware base (Q5) | map Q5 | **Accepted** — ADR-0005 |
| COBS framing (Q11) | map Q11 | **Accepted** — ADR-0006 |
| No verb implicitly changes the mode | #6 | **Accepted** — ADR-0007 |
| The device watches neither link | #6 | **Accepted** — ADR-0008 |
| Plan records carry a hold, not a fixed timestep | #8 | **Accepted** — ADR-0009 |
| Container-owned in-memory plan cache keyed by the compiled hash | #7 | **Accepted** — ADR-0010 |
| Amiibo identity minted container-side; the device is a byte-sink | #9 | **Accepted** — ADR-0011 |
| Key material is a read-only mount, never written, never forwarded | #13 | **Accepted** — ADR-0012, *overriding #13's own judgement* (below) |
| Firmware owns pairing (Q12) | map Q12 | **Accepted** — ADR-0013 |
| Single-process container | #7 | **Declined** — the seam that matters is the module interface (§8.2), not a process boundary; splitting later changes no protocol and no interface. Not hard to reverse |
| The port as configuration, with Q2's literal path as the default | #7 | **Declined** — one constant to change; a reader would not wonder why. It stays a chapter rule (§8.3) rather than a decision of record |
| `esp32-joycontrol` is untrusted (Q13) | map Q13 | **Declined** — a sourcing rule, not an architectural choice. It lives in `docs/references.md` and ADR-0005 |

**The one override, stated plainly.** #13 considered an ADR for its key policy and **declined
it**, on the grounds that "never persisted" and "never reaches the device" are inherited
consequences of Q9 rather than a fresh trade-off. That reasoning is right about the
consequences and wrong about the decision: the *mount-versus-upload* half is a genuine
choice with a rejected alternative and a surprising outcome (a container that can never write
the key it depends on), and it is what the research record had to be corrected for. ADR-0012
was therefore written anyway, with #13's flag recorded here so the disagreement is visible
rather than silently resolved.

## Citations

External material is cited by alias against the pinned corpus in
[`docs/references.md`](../references.md) — for example
`switch2_controller_research/commands.md:47-100`. Never by absolute path. Claims about
this repo cite `path:line`. Research records under `docs/research/` are cited by section
(`s3-bringup.md` §11.4) and are the evidence trail, not the specification.

## Where a change goes

The protocol is described across three chapters, and that is a real cost: one protocol change
touches two or three of them. It is paid deliberately, because the alternative — one chapter
holding framing, verbs, status and the state machine — is a chapter nobody finishes reading,
and the split follows the grain of the thing: **the protocol is verbs, status and state, and
they fail independently.**

To keep the three in step, each fact lives in exactly one chapter and the others point at it.
When changing the protocol, use this as the checklist and edit only the owning chapter:

| Fact | Owner | Others may reference, never restate |
| --- | --- | --- |
| Framing, the verb list, `HELLO`, `ERROR` codes, bulk upload, versioning, `CONFIG` | **§2** | 3, 4 |
| Which `STATUS` field exists, its values, `EVENT` kinds, rate limits, the stop reading | **§3** | 2, 4, 8, 9 |
| The five axes, the transition table, panic stop, the release invariant, link-loss rules | **§4** | 2, 3, 7, 9 |
| The plan's byte layout and compilation | **§5** | 2, 7, 8 |
| The tag's lifecycle and identity model | **§6** | 2, 3, 8, 9 |

**A fact stated in two chapters will drift.** Where a second chapter needs it, it cites the
section number and adds only the consequence that belongs to its own subject — chapter 9
reciting a field's *values* rather than pointing at §3.2 is the drift this table exists to
prevent. The worked traces (chapter 11) are deliberately an exception: they restate sequences
on purpose, because a trace that only points is not a trace.
