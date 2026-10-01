# Amiibo identity is minted container-side and the device is a byte-sink

The container owns the amiibo model — Figure → Identity → Tag — and mints an identity per
placement. The device holds one already-sealed 540-byte tag in RAM, serves it to the console
over the console link, and does nothing else with it: no key, no key derivation, no
re-encryption, no signature. Its whole obligation is a frame CRC and a length.

The alternative is to make the device the amiibo engine: derive keys on the board, re-sign
each rotation there, cache plaintext figures. The board has the hardware to do it — roughly
1.5 ms for a full re-sign against a ~90 ms console read — so the argument is not performance.
It was rejected because it would require the user's retail key to be resident on the device,
which is a redistribution and persistence problem the container does not have, because a
device-side derivation path is a second implementation of the cryptography, and because
rotating a tag then becomes a firmware feature rather than a policy the container can change.

## Considered options

- **Replay a stored dump unchanged**: rejected outright. It is the one failure mode that looks
  like success, and it cannot satisfy the freshness requirement.
- **Precompute a variant set** of many pre-sealed identities for a figure: rejected because
  finite identities repeat (which the console can see), because it costs RAM per figure, and
  because it saves only the ~7 ms push against the ~90 ms the console imposes anyway.

## Consequences

- The retail key never reaches the board, so `HELLO` advertises no key capability and no key
  can be persisted in NVS or in the `storage` partition (ADR-0004).
- The device cannot verify a tag's crypto, and does not try; a bad tag is the container's bug
  and shows up as a console rejection, not a device error.
- Rotation is just another upload: no device verb, no firmware change, and per-placement
  identity minting stays free to change.
