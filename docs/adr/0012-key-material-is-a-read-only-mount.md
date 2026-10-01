# Key material is a read-only mount the container never writes and never forwards

The retail key file is the user's own, handed to the container through a read-only bind mount
at one fixed path, read once at startup into process memory, validated, and held for the
process lifetime. The container never writes it anywhere, never serves it over HTTP, and never
frames it onto the control link. There is no environment variable for the path.

The alternative is to make the key a piece of application state: an upload endpoint, a named
volume, a path from configuration, a "refuse to start without a key" mode. Each was rejected
for the same reason — a mount makes the key's lifetime "the container never owns it" rather
than "the container owns it carefully". The path is fixed rather than configurable because the
locked state has to *name* where the key is expected, and "whatever the variable says" is a
path the UI cannot print truthfully.

## Considered options

- **Hot-plug the key** (re-read it per placement): rejected — a per-placement `stat()` on a
  mounted host path sits inside the ~7 ms placement budget, and "the key changed between scan
  N and scan N+1" has no good answer.
- **A strict mode that refuses to start without a key**: rejected — macros are unaffected by
  the key, so it would fail a working half of the product.
- **A stored-dump fallback when the key is missing**: rejected — see ADR-0011.

## Consequences

- Finding the key after startup requires a container restart; this is deliberate.
- Two spellings are accepted (a single 160-byte file, or the two-file pair concatenated in
  order), because the reference corpus ships the pair and refusing it would make a correct key
  look invalid.
- Three unavailability states stay distinct — key state, empty library, and firmware without
  the NFC path — because each has a different fix and collapsing them sends the user to the
  wrong one two times in three.
- The deployment doc must carry a copy-pasteable mount line, and the UI must be able to print
  it verbatim.
