# Plan identity is a container-owned hash, and the cache is memory-only

A plan's identity is a truncated SHA-256 over its compiled payload, computed by the container
and **echoed** by the device — the device never hashes anything. The container caches compiled
plans in memory keyed by that hash, discarded on restart.

The alternative is a device-generated identity, or a durable cache. A device-side hash would
mean the two sides can disagree about what is loaded and neither is authoritative; a durable
cache would put compiled macro content on disk for no benefit, since the whole library
recompiles in well under a second.

## Consequences

- Hashing the *output* bytes means a compiler or format change yields a new identity, so a
  stale plan can never be mistaken for current — the container must re-upload after any
  compiler change.
- `STATUS` reports the device's echoed hash next to the frame count and current frame, and the
  container compares rather than trusting.
- A rejected macro stays visible in the container's library list with its reason; it is never
  silently hidden and never partially uploaded.
