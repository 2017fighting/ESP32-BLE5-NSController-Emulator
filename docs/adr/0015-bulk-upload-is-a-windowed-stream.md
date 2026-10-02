# Bulk upload is a windowed stream, exempt from "one outstanding request"

A bulk transfer — `LOAD_PLAN` or `PLACE_AMIIBO` — is a **windowed stream**: the container
keeps at most one window (4096 bytes) of un-ACKed chunk bytes in flight, sends no other verb
for the life of the transfer, and resumes from the offset the ACK names. It is the one
exception to §2.3's "one outstanding request", and the exception is confined to bulk.

The rule as originally written — the container sends a `REQUEST` and waits for its `REPLY`
before sending another — has no room for the "windowed ACK … not one ACK per chunk" that
§2.7 and the worked trace both require. Strictly alternating would make a 64 KiB plan 256
round trips and would ACK bytes the 256-byte RX ring has already had to absorb; a sliding
window keeps the same guarantee (nothing is assumed written before an ACK says so) at 1/16th
the round trips. The alternative was to keep §2.3 literal and give up windowing, which
contradicts §2.7, trace A step 8, and §12.2's validation-2 arithmetic.

## Consequences

- §2.3 states the rule for **control** requests and points here; §2.7 owns the bulk pacing.
- The ACK is an asynchronous, offset-keyed `REPLY`, not an answer to one frame. There is still
  exactly one interpreter of it — the container's `device` seam — and no request ids.
- Ordered sessions survive: a transfer is atomic from the container's side, so "one
  outstanding" becomes "one outstanding window, and no other verb until the transfer ends".
- A device may ACK earlier than a window (ring pressure), so the container must never depend
  on an ACK arriving *only* at a window boundary.
- The window is a constant of §2.7, not a `HELLO` field: `chunk_size` is already advertised,
  so validation 2 can be answered by changing the constant on both sides.
