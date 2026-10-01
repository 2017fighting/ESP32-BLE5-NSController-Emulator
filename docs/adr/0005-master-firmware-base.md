# Master firmware as the base, with no code taken from the prior attempt

The firmware is the upstream `master` tree plus targeted additions. The prior attempt at the
same goal — a WiFi-exposed, wired-controller build that was developed with AI assistance,
tested at length against a Switch 1, and never worked — is not a code source, not a
reference implementation, and not a starting point.

Its written conclusions are not evidence either. Its central negative claim ("a controller
cannot inject tag data") was audited against primary sources and does not survive: the
project's own notes record four framing defects (a one-byte MCU offset error, a missing
CRC8, a heartbeat silenced on entering report mode `0x31`, and a battery byte that made the
console treat the pad as wired and disable wireless NFC), and its push was sent while the
console was still listening in the wrong report mode. It is kept only as a source of ideas
and failure modes, deliberately unpinned in the reference corpus so no pin implies its tree
is reproducible.

## Consequences

`docs/references.md` classes it as untrusted, and no spec chapter, ADR or code comment may
rest a claim on it. Rebuilding on `master` also means the S3 bring-up is real on this
hardware: the base firmware pairs with a real console and moves HID input, which is a
better foundation than a tree with four known framing bugs.
