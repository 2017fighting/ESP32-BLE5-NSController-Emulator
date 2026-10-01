# The mode is a single field moved only by an explicit verb

The mode is one field holding one of `IDLE`, `MACRO` or `AMIIBO`, and it changes only when
the container sends a verb that says so or a human presses BOOT. No verb has a second,
hidden effect on the mode: `PLACE_AMIIBO` received while `MACRO` is running is rejected with
a typed error and **the macro keeps running**, not stopped, not queued.

The convenient instinct is the opposite — let `PLACE_AMIIBO` stop the run first, since the
container was going to send `STOP` anyway. It was rejected because a 2 Hz poll cannot
distinguish "my `PLACE_AMIIBO` succeeded" from "my `PLACE_AMIIBO` silently killed the run I
am watching", because a duplicated or stray verb would end a farm run on an accidental
click, and because sequencing `STOP` then `PLACE_AMIIBO` costs one round trip over an idle
link in exchange for a state machine that is a pure function of `(mode, verb)` and therefore
testable.

## Considered options

- **Implicit stop**: one round trip fewer, a lying status poll, and a hidden second effect on
  a verb that is supposed to be one action.
- **Queue the request**: a deferred mode change that fires at an unknown later loop boundary,
  with a memory — exactly what the stateless design dislikes.

## Consequences

- Illegal-but-harmless verbs stay idempotent successes (`STOP` in `IDLE`, `UNPLACE_AMIIBO`
  with no tag placed), because they reduce activity; verbs that start or switch activity are
  the ones that reject.
- `LOAD_PLAN` while `MACRO` rejects for a sharper reason than symmetry: if a commit could land
  mid-replay, "atomic" would stop meaning anything and neither side could say which bytes
  were running.
- The container's Control screen must send two verbs where a user perceives one action.
