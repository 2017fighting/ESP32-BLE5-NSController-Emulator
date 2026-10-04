# The NFC report byte is the reader's event counter

`hid_report_pro2_t` byte `0x0C` (`nfc_state`, spec §4.9) stays `0x00` while nothing is in the
reader's field, and while a tag is in the field it is the **reader's event counter** — `0x01`–`0x07`,
advanced once on each of the reader's five events (tag presented, scan ready `0x03`,
operation ready `0x06`, write complete `0x08`, tag removed) and wrapping `0x07 → 0x01` rather
than through the reserved `0x00`.

The byte was placement-only — `0x00`/`0x02`, "the mode's physical expression" — until the
G-18 crash ledger isolated the shape that kills the console's amiibo module: a byte **held** at
one value across a whole armed read, together with a `0x05` answer that always says `04`
(`register-screen-bench.md` §7). The second implementation does both halves the other way — it
answers the `04` as a *level* **and** drives this byte as an event counter,
`(previous + 1) & 0x07`, on exactly those five events
(`ns_pc_control/server/src/virtual_controller.cpp:195-266`, context tier) — and reports no crash.
With the NS2's read completion carried on the `0x05` answer (§6.6), the input report's byte is
the only remaining channel a console can read a *change* from, so it carries the reader's
sequence: `switch2_controller_research/hid_reports.md:178`'s `0x00`–`0x07` range read as a
sequence rather than as a vocabulary.

The rejected alternative is to keep the closed vocabulary and the placement-only write. It is
what five of the bench's crashed configurations ran, and it leaves the byte pinned through the
one window the console's module spends the read in — the conjunction G-18 isolates. The cost
accepted here is that the byte is no longer a readable *vocabulary* (`0x02` no longer says
"tag detected"); the mode's exclusivity is preserved instead by the reserved zero, which the
placement's own first advance moves off and the wrap never re-enters, and which a completed
read's eject returns to because the reader's field is then empty.

## Consequences

- §4.9 owns the rule and the events; `nfc_tag.c` is the counter's one writer and the host suite
  asserts the movement (`test/host/test_nfc_tag.c`). The reference's own advances are spaced
  (~40 ms after `0x03`/`0x06`); this device advances on the event edge, with the HID report's
  10 ms grid carrying each to the wire.
- `0x00` means **no tag in the reader's field** — `IDLE`, `MACRO`, the §6.5 gap, or a post-eject
  read — and never appears while a tag is in the field. The two modes still cannot overlap at the
  hardware level, so §4.9's exclusivity argument stands unchanged.
- `STATUS.console_polling` keeps its own `IDLE`/`POLLING`/`TAG_DETECTED` vocabulary (§3.2); it
  is the console's level, not the device's activity, and the two remain two signals.
- The byte is a bench-visible fact: the firmware logs `nfc byte: <old> -> <new>` on every
  change (`control_parser.c`) so a session can see what the console saw.
- This is the first amendment to reverse a reviewed correction of #25 (the placement-only byte
  was one), which is why it is an ADR as well as the §4.9 edit.
