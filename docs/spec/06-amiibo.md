# 6 · Amiibo: figures, identities, tags and freshness

The container's amiibo model is **Figure → Identity → Tag**, minted container-side, **one
identity per placement**, pushed as a whole 540-byte tag. The device holds no key and does no
identity work (ADR-0011).

## 6.1 The three nouns

| Noun | Is | Cardinality |
| --- | --- | --- |
| **Figure** | a character and series — what a tag reproduces | the library's unit; one figure, many identities |
| **Identity** | the seven-byte NFC UID | one per placement |
| **Tag** | one 540-byte NTAG215 image: a figure's data encrypted and signed under one identity | what the device holds and serves |

**Rotation** replaces a placed tag with a fresh identity of the same *figure*. That is the
whole freshness mechanism, and it is why a rotation is not a small change: the identity is an
input to the tag's cryptography (§6.3).

## 6.2 Input formats and the library

- **`.bin` (540 B) is canonical.** It is the raw NTAG215 dump: 135 pages × 4 bytes.
- **`.nfc` (3352 B) is the same tag in a Flipper Zero ASCII wrapper** — `UID: …` then
  `Page 0:` … `Page 134:`, carrying the identical UID and pages. It is decoded to the same
  540 bytes or ignored; the container **ignores** it and reads `.bin` (chapter 8).
- **emuiibo-style JSON is never an input.** It is a *decrypted projection* — mii, name, dates,
  areas — with no ciphertext and no HMACs, so it cannot be re-signed at all.
- `!Essential Files/` is excluded from the figure index: it is the key, not a figure.

**Three intake surprises the index must handle deliberately** (they were found while
resolving the key policy and are recorded so they are not rediscovered):

| Surprise | Consequence |
| --- | --- |
| 940 of the `.bin` files are exactly 540 B and **11 are 572 B** (540 + a 32-byte trailer that is *not* a hash of the 540) | the reader **slices the first 540 B** rather than trusting file size; reading one as a tag silently mis-reads it |
| 846 distinct amiibo IDs across 955 files, four IDs carrying multiple distinct UIDs | the index is **keyed by file**, grouped by figure and series — never by ID, which is not unique |
| `.nfc` and `.bin` filenames line up only for 844 of 964 | never pair the two formats by stem |

## 6.3 The identity is load-bearing twice, which is why a UID swap is not enough

The seven-byte UID is the rotating unit and it is *cryptographically* involved, so "rotate the
UID" cannot be a field patch:

- **Key derivation.** The keygen seed is read from the tag's own identity block — the **8 bytes
  at tag offset `0x000`**, which are `UID[0..2]`, then the NTAG215 check byte `BCC0` at byte 3,
  then `UID[3..6]`. There is no eighth UID byte. A new UID therefore changes the derived
  AES-128 key/IV *and* both HMAC keys.
- **Signature.** The tag HMAC is computed over the plaintext with the identity block appended
  first, and the data HMAC chains it.

So every placement is a full **re-seal**: AES-128-CTR over the encrypted body plus both
HMAC-SHA256 signatures. That is a property of the tag format, not an implementation choice,
and it is why the container runs a real sealing routine rather than patching bytes.

**Identity minting, precisely:** 7 bytes — `0x04` (NXP manufacturer) followed by 6 CSPRNG
bytes — with `BCC0` and `BCC1` recomputed:

```text
BCC0 = 0x88 ^ UID[0] ^ UID[1] ^ UID[2]     (byte 3)
BCC1 = UID[3] ^ UID[4] ^ UID[5] ^ UID[6]   (byte 8)
```

Verified against a library tag: `Samus.nfc` opens `04 11 fe 63 ca 52 6c 81`,
so `UID = 04 11 FE 63 CA 52 6C`, `BCC0 = 0x88^0x04^0x11^0xFE = 0x63` at offset 3, and
`BCC1 = 0xCA^0x52^0x6C^0x81 = 0x75` at offset 8.

**Identities are never recorded and never reused deliberately.** The space is 2^48, so reuse
is a non-issue; no store of issued identities exists, and none should be added.

### The NTAG215 image

135 pages of 4 bytes, so page *n* starts at tag-image offset `4n`. The regions the design
cares about, **in tag-image coordinates**:

| Pages | Offset | Contents |
| --- | --- | --- |
| 0–1 | `0x000`–`0x007` | `UID[0..2]`, `BCC0` (byte 3), `UID[3..6]` — **the 8-byte KDF seed region** |
| 2 | `0x008` | `BCC1`, internal byte `0x48`, static lock bits `E0 0F`, capability container `F1 10 FF EE` (pages 2–3) |
| 4–12 | `0x010`–`0x033` | amiibo header: write counter, character ID, amiibo ID |
| 13–84 | `0x034`–`0x153` | data section 1, AES-128-CTR ciphertext |
| 85–92 | `0x154`–`0x173` | **data HMAC**, 32 bytes |
| 93–116 | `0x174`–`0x1D3` | **tag HMAC**, 32 bytes |
| 117–129 | `0x1D4`–`0x207` | data section 2 / application save area |
| 130–134 | `0x208`–`0x21B` | dynamic lock bits, `CFG0`, `CFG1`, password, `PACK` — **copied verbatim from the source `.bin`**: neither identity-derived nor HMAC-covered |

**Two coordinate systems exist and this table uses only one of them.** The pinned corpus also
names the HMACs by their *decrypted/internal* offsets (`0x034` and `0x1B4`), which do **not**
land on those same tag-image bytes — `0x1B4` is page 109, inside the tag-HMAC span only under
the internal layout. The design rests on exactly one fact from either system — that the seed
region is identity-derived and HMAC-covered, which is what makes a UID patch impossible — and
that fact holds in both. **Reconciling the two systems against the pinned `amiitool` source is
a known gap (G-9); nothing in this spec depends on which is preferred.**

One consequence the container must respect: the plaintext cache produced by the sealing
routine uses the library's own layout, in which the tag's identity block appears at `0x1D4`.
That index is **the cache's**, not this table's, and using one where the other belongs is the
bug this note exists to prevent.

## 6.4 Sealing, and why per-placement is cheap enough

**Sealing is a pure function** with no device, no HTTP and no side effects:

```text
seal(figure's raw 540 B dump, key material) -> sealed 540 B Tag + minted Identity
```

Every placement is `unpack(source) → overwrite the identity → pack()`. **Synthesis is
per-placement, never precomputed**, and the arithmetic is what settles it:

| Step | Cost |
| --- | --- |
| Container re-seal (`pack`) | sub-millisecond on the host |
| Push 540 B over the control link (256 B chunks, ~1 ACK) | **~7 ms** |
| Device re-seal, were it done there | ~1.5 ms |
| Console reads 540 B over BLE (9 × 64 B round trips) | **~90 ms** |

The push is ~8% of the cost the console imposes anyway. **Precomputed variant sets are
rejected**: finite identities that repeat (visible to a game), 540 B of device RAM per
variant, all to save the ~7 ms.

**Plaintext lifetime:** unpack lazily on the first placement of a figure, cache the 520-byte
plaintext in **process memory only** (~494 KB worst case for the whole library), and **copy
it before rotation** — the cache still carries the source tag's original UID at `0x1D4`, so
sealing the cache in place would corrupt it. Never on disk: it is decrypted save-data and Mii
content, so the rule that applies to the key applies at least as strongly.

## 6.5 The placement lifecycle, and the gap

```text
container                          device                         console
   │  PLACE_AMIIBO (540 B) ─────────►│  tag committed
   │                                │  nfc_state → tag detected ───►│  sees a tag
   │                                │                               │  0x01/0x05 status + UID
   │                                │◄──────────────────────────────│  0x01/0x15 read pages …
   │                                │  EVENT SCAN_ENDED ────────────►│  0x01/0x04 stop polling
   │◄───────────────────────────────│
   │  seal the next identity         │
   │  PLACE_AMIIBO (next 540 B) ────►│  atomic replace: gap, then new tag
```

- **A placement is place → console reads → unplace → place fresh.**
- **The device emits a tag-absent gap on every tag change**, including the atomic-replace
  `PLACE_AMIIBO` while `AMIIBO` is active: the NFC state byte returns to `0x00` and
  `0x01/0x05` reports no tag, before the new tag answers. **This is a design guarantee, not a
  proven console requirement.** Something has to leave the field for repeated scanning to work
  on NS1 (emuiibo's manual disconnect exists for exactly that), so a gap is the safe superset;
  whether the NS2 needs it is unobserved (G-6).
- **The gap lives inside `AMIIBO`**, so `AMIIBO` is *presents one tag at a time* rather than
  "always a tag placed". The control plane never sees a mode oscillation: the rotation is one
  atomic replace.
- **Refresh is edge-driven.** The device emits `SCAN_ENDED` when the console stops polling, and
  the container mints the next tag and pushes it, so a virgin identity is already staged before
  the next scan. No polling loop, no new verb.
- **One placement per scan.** The container does not oscillate `UNPLACE`/`PLACE`, which would
  exit and re-enter the mode for no reason.
- **Console write-back is accepted and discarded.** A `0x01/0x14` write is taken into the
  volatile tag **while it is placed** — the current placement is not lied to about what it
  wrote — and dropped on unplace, because the next placement is virgin by design.
- **Console link dropped while `AMIIBO`**: the placement is kept and the tag is **rotated on
  reconnect**, so the console cannot re-scan an identity it already saw (chapter 9).

## 6.6 What the device does with a tag

Almost nothing, deliberately:

- It accepts the 540 bytes over the same chunked, windowed, atomic path as a plan, holds them
  in RAM, and serves them page by page when the console asks.
- It drives the NFC state byte in HID input report `0x09` (§4.9) so the console learns a tag
  exists.
- **It cannot verify a tag's cryptography** and does not try: it has no key. A bad tag is the
  container's bug and shows up as a console rejection.
- It reports `tag_state`, `tag_identity` and `console_polling` on `STATUS` so the container can
  drive rotation.

**Firmware work this implies** (chapter 7): the NFC subcommands `0x03`/`0x04`/`0x05`/`0x06`/
`0x14`/`0x15` under command `0x01` are stubs today (`main/src/ns2_codec.c:204-214` handles
only `0x0C`), and byte `0x0C` of the report is hardcoded `0x00`. Both are prerequisites for
`AMIIBO`, and both are firmware changes rather than protocol changes.

## 6.7 Key policy

`key_retail.bin` is **the user's own file**, handed to the container through a read-only bind
mount, read once at startup, validated, held in process memory, and never sent anywhere
(ADR-0012).

| | |
| --- | --- |
| Path | **`/keys/key_retail.bin`** — fixed, no environment variable |
| Also accepted | `/keys/` containing `unfixed-info.bin` + `locked-secret.bin`, concatenated in that order (byte-identical to the single file) |
| Read | **once at startup**; finding it afterwards means restarting the container |
| Written by the container | **never** — no named volume, no temp file, no derived image, no copy into a data directory |
| Read from inside the library mount | **never** |
| Sent to the device | **never** |

**Validation ladder** — length → masterkey structure → a real cryptographic round trip:

1. **160 bytes**, or the two-file spelling.
2. The masterkey structure (Nintendo's `typeString`s and `magicBytes` regions), which a random
   160-byte file fails.
3. An `unpack` round trip against one tag from the mounted library, **requiring both HMACs**.

**States:** `KEY_OK`, `KEY_ABSENT`, `KEY_INVALID`, `KEY_UNVERIFIED`. `KEY_UNVERIFIED` exists
because a user may mount the key without a library, and the honest answer is then "held but
unproven" rather than "valid" (a lie) or "invalid" (a false accusation). **With no library tag
at startup, verification completes at the first placement**; a failure there sets
`KEY_INVALID` and refuses the placement. It **never** falls back to replaying a stored dump
unchanged — that is the one failure mode that looks like success.

**No key is not a startup failure.** The container starts, macros are unaffected, `AMIIBO` is
offered but locked. There is **no strict mode** and no fail-fast flag: it would break a
working half of the product.

**The lock keeps three states distinct**, because each has a different fix and collapsing them
sends the user to the wrong one two times in three:

| State | Cause | The one action that fixes it |
| --- | --- | --- |
| `KEY_ABSENT` / `KEY_INVALID` / `KEY_UNVERIFIED` | no key, wrong key, unproven key | mount the user's own `key_retail.bin` at `/keys/key_retail.bin` and restart |
| `LIBRARY_EMPTY` | no library mounted, or no index built | mount the library and index it |
| device `features.amiibo == false` | firmware without the NFC path | flash a firmware build that has it |

*The state list is this chapter's, and the container's own local vocabulary for two of these
names (`LIBRARY_EMPTY`, the `KEY_*` set) is deliberately shared with the screens rather than
duplicated: the wire has no such codes at all (§2.5).*

**Shipping is three layers:** a startup log line naming the expected path, a persistent UI
badge naming the single action, and a copy-pasteable mount line in the deployment doc. The
**one startup log line** carries the resolved path, which spelling was used, the byte length,
a truncated SHA-256 fingerprint of the 160 bytes, and the resulting state — **never the
bytes**. The fingerprint exists because "which key is loaded" is otherwise unanswerable from a
log, and two different wrong keys are otherwise indistinguishable from each other.

## 6.8 The feasibility branch

The map carries an explicit branch: **if amiibo emulation proves impossible, the effort still
delivers a locked macro-mode design plus a written statement of why, and what the alternatives
are.** The research answered the feasibility question affirmatively (`ns2-amiibo-path.md`), so
the branch is **not taken** — but it is not deleted either, because two of its premises are
§9.4's open facts rather than measurements (G-6).

The branch triggers if, on the bench, the console does not start polling when the NFC state
byte moves (validation 4, §12.2), or if it reads the tag and then rejects every rotated
identity as unfresh. **The macro half is unaffected either way**: not one line of chapters 2–5
and 7–8 depends on the amiibo half existing, which is exactly what makes the branch cheap.

The documented fallbacks, in the order to try them:

1. **The console's own reader with physical tags.** Zero firmware cost: the device does 100%
macro replay, and the user taps a real amiibo on the console. Decouples macro timing from the
entire NFC stack. The cost is that freshness is then the user's problem, not the software's.
2. **An external NFC module (PN532 or ST25DV) on the board's I²C bus.** Presents emulated tags
over real RF to the console's reader. Costs a few dollars of BOM and abandons the BLE NFC path
entirely, but it needs no reverse-engineered PN7160 encapsulation.
3. **Wire the same controller protocol over USB.** The console supports Pro Controller 2 over
USB with full NFC support. This is a **different transport for the whole effort**, not a patch
to it, and the control plane would move with it.

**What must not happen under the branch:** falling back to replaying a stored dump unchanged.
That is the one failure mode that looks like success — the console reads a tag, the game sees
the same amiibo it saw yesterday, and everything appears to work (ADR-0011).
