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

Verified against a library tag: `Samus.nfc` opens `04 11 fe 63 ca 52 6c 81` — `UID[0..2] =
04 11 FE`, then `BCC0` at byte 3, then `UID[3..6] = CA 52 6C 81` — so the seven-byte UID is
`04 11 FE CA 52 6C 81`, `BCC0 = 0x88^0x04^0x11^0xFE = 0x63` at offset 3, and
`BCC1 = 0xCA^0x52^0x6C^0x81 = 0x75` at offset 8.

**Identities are never recorded and never reused deliberately.** The space is 2^48, so reuse
is a non-issue; no store of issued identities exists, and none should be added.

### The NTAG215 image

135 pages of 4 bytes, so page *n* starts at tag-image offset `4n`. The regions the design
cares about, **in tag-image coordinates**:

| Pages | Offset | Contents |
| --- | --- | --- |
| 0–1 | `0x000`–`0x007` | `UID[0..2]`, `BCC0` (byte 3), `UID[3..6]` — **the 8-byte KDF seed region** |
| 2–3 | `0x008`–`0x00F` | `BCC1` (byte 8), internal byte `0x48`, static lock bits `E0 0F`, capability container `F1 10 FF EE` |
| 4 | `0x010`–`0x013` | amiibo header / write-counter prefix — **unencrypted** |
| 5–12 | `0x014`–`0x033` | AES-128-CTR ciphertext, first 32 bytes |
| 13–20 | `0x034`–`0x053` | **tag HMAC**, 32 bytes |
| 21–31 | `0x054`–`0x07F` | plaintext settings tail, 44 bytes |
| 32–39 | `0x080`–`0x09F` | **data HMAC**, 32 bytes |
| 40–129 | `0x0A0`–`0x207` | AES-128-CTR ciphertext, remaining 360 bytes |
| 130–134 | `0x208`–`0x21B` | dynamic lock bits, `CFG0`, `CFG1`, password, `PACK` — **copied verbatim from the source `.bin`**: neither identity-derived nor HMAC-covered |

**Two coordinate systems exist, and the pinned `amiitool` source fixes the mapping between
them** (`amiitool/amiibo.c:38-56`, `nfc3d_amiibo_tag_to_internal` /
`nfc3d_amiibo_internal_to_tag`). The table above is in tag-image coordinates — what the device
holds and serves and what the console reads. The sealing routine's plaintext cache is in the
library's *internal/decrypted* layout (520 bytes, 130 pages), where the same bytes sit at
different offsets:

| Region | Tag-image | Internal cache |
| --- | --- | --- |
| identity block (the KDF seed region) | `0x000`–`0x007` | `0x1D4`–`0x1DB` |
| tag HMAC | `0x034`–`0x053` | `0x1B4`–`0x1D3` |
| data HMAC | `0x080`–`0x09F` | `0x008`–`0x027` |

The cryptographically load-bearing fact is the same in both systems — the identity block is
the KDF seed and the tag HMAC covers it, which is what makes a UID byte-patch impossible — and
the permutation above says where each region sits. Reconciling the two systems against the
pinned source was **G-9**; it is settled, not preferred (§12.3, `amiitool-sealing-port.md`
§1, verified against `amiitool/amiibo.c`).

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
| Console reads 540 B over BLE (9 × 70 B round trips over the 600-byte space) | **~90 ms** |

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
   │                                │  nfc_state → reader event ───►│  sees a tag
   │                                │                               │  0x01/0x05 status + UID
   │                                │◄──────────────────────────────│  0x01/0x15 read pages …
   │                                │  EVENT SCAN_ENDED ────────────►│  0x01/0x04 stop polling
   │◄───────────────────────────────│
   │  seal the next identity         │
   │  PLACE_AMIIBO (next 540 B) ────►│  atomic replace: gap, then new tag
```

- **A placement is place → console reads → unplace → place fresh.**
- **The device emits a tag-absent gap on every tag change**, including the atomic-replace
  `PLACE_AMIIBO` while `AMIIBO` is active: the NFC state byte returns to `0x00` (tag removed,
  §4.9's counter) and `0x01/0x05` reports no tag, before the new tag answers. **This is a design
  guarantee, not a proven console requirement.** Something has to leave the field for repeated
  scanning to work on NS1 (emuiibo's manual disconnect exists for exactly that), so a gap is the
  safe superset; whether the NS2 needs it is unobserved (G-6).
- **The gap is held, not instantaneous.** The state byte is `0x00` and `0x05`/`0x15` answer
  nothing for **two report periods — 20 ms at the 10 ms grid** (`NFC_TAG_GAP_MS`,
  `main/src/controller/nfc_tag.c`) — because a gap the reporter can lose in a buffer swap is
  not a guarantee. The deadline is absolute milliseconds on the control task's clock, so a late
  tick observes the gap as over rather than extending it. A **first** placement has nothing to
  remove and opens no gap; only a tag *change* does.
- **The gap lives inside `AMIIBO`**, so `AMIIBO` is *presents one tag at a time* rather than
  "always a tag placed". The control plane never sees a mode oscillation: the rotation is one
  atomic replace.
- **Refresh is edge-driven.** The device emits `SCAN_ENDED` when the console stops polling, and
  the container mints the next tag and pushes it, so a virgin identity is already staged before
  the next scan. No polling loop, no new verb.
- **One placement per scan.** The container does not oscillate `UNPLACE`/`PLACE`, which would
  exit and re-enter the mode for no reason.
- **Console write-back is staged, committed, then discarded.** The `0x14` chunks fill a
  454-byte staging stream, and `0x08` commits it into the volatile tag **while it is placed** —
  the current placement is not lied to about what it wrote. A stream with a hole in it, a UID
  that is not the placed tag's, or a record that runs outside the image commits *nothing*;
  the bytes are dropped on unplace, because the next placement is virgin by design (#47).
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
`0x14`/`0x15` under command `0x01` were stubs (`main/src/ns2_codec.c:204-214` handled
only `0x0C`) and byte `0x0C` of the report was hardcoded `0x00`. **Both landed with #25**,
and they are firmware changes rather than protocol changes:

- The server is `main/src/controller/nfc_tag.c`, a portable state machine asserted on the host
  (`test/host/test_nfc_tag.c`); `control_parser.c` owns the singleton, the report byte and the
  `SCAN_ENDED` event, and `ns2_codec.c` routes the subcommands to it.
- **The read is nine round trips over a 600-byte buffer, in 70-byte chunks.** The `0x15`
  answer in the canonical capture (`switch2_controller_research/commands.md:68`) is an 81-byte
  response: the 8-byte command header, a **three-byte head** `00 46 00`, then **70 data bytes**.
  The head is `last` u8 · `len` u16 (LE) — `00` = not the final chunk, `0x46` = 70 = exactly what
  was served — and the request's own payload `46 00` is offset **70**, i.e. one 70-byte chunk had
  already been consumed. The served space is therefore `[60 B framing][540 B image]` — **600
  bytes, the image beginning at serving offset `0x3C`** — and the whole tag is nine round trips at
  either size (§7.7). *Landed with #46; the plain image-at-0 view survives only as the host
  suite's comparison compile (`NFC_TAG_READ_PLAIN_VIEW`), never as a candidate.*

  The head's 60 bytes, in serving coordinates:

  | Offset | Contents |
  | --- | --- |
  | `0` | `04` — the opening byte the second implementation serves |
  | `1`–`3` | zero |
  | `4` | `01` |
  | `5` | `02` |
  | `6` | zero |
  | `7` | `07` — the UID length, as the `0x05` answer declares it |
  | `8`–`14` | the placed tag's seven-byte UID |
  | `15`–`18` | zero |
  | `19`–`50` | the 32-byte constant both captures carry |
  | `51`–`59` | the `0x06` request's own `payload[10..18]`, echoed |

  | Command | Request payload | Response payload |
  | --- | --- | --- |
  | `0x15` read buffer | `offset` u16 (LE) | `last` u8 · `len` u16 (LE) · `len` bytes of the 600-byte read buffer, ≤ 70 |
  | `0x14` write buffer | `offset` u16 (LE) · `len` u16 (LE) · `len` bytes | — (staged into the 454-byte frame below) |
  | `0x08` commit write | — | — (commits the staged frame: its records land in the volatile image and the status then answers `0x05` for the rest of the scan) |
  | `0x05` get status | — | 61 B: status byte (the lifecycle above) · detail byte · the captured flags `00 00 01 01 02 00` · `0x07` · the 7-byte UID; the post-eject answer is `07 41` and zeros |
  | `0x03`/`0x04`/`0x06` | `0x03`: 5 B poll config; `0x06`: **the read command** — marker `d0`, UID length `07`, UID (`00×7` = read any tag), `01` NTAG215-only, `03` page ranges `00–3b/3c–77/78–86` = all 135 pages (`ns1-nfc-read-decode.md` §1) | — (ACK), and its `payload[10..18]` rides the next head's `[51..59]` |

  The status byte is the lifecycle channel rather than a constant, and **it is the reader's
  contract as of #48**: `0x09` tag in field (the capture's value) → `0x04` for as long as
  `0x06`'s read is armed, answered as a **level** on every `0x05` of that window → back to
  `0x09` (`0x03` or `0x04`), or `0x07` + detail `0x41` once `0x04` ends a scan whose read reached
  the end of the served space. The flags, the `0x07` UID length and the UID keep their captured
  positions; the detail byte is offset `1`, where the captured flags carry `0x00`. That `0x05`
  (a committed write) is **scan-scoped**: `0x04` (or the placement's own end) puts it back to
  `0x09`, because a status that never resolves is the shape G-18's ledger isolates — a `0x03`
  re-arm keeps it, so the console can still read back what it wrote. The **post-eject** answer is
  one shape the capture does not carry: the second implementation answers `07` with detail `41`
  and nothing else once a scan's read has completed and `0x04` ended it, where an incomplete
  read's stop is the tag-in-field `09`. The shipped firmware collapses every non-placed state to
  status `0x00` with the rest zero; whether the *never-placed* answer should also be `07 41` is
  open (G-19), and this device keeps `0x00` there.

- **`0x06` decoded, and what it implies for the read gate.** The NS1 capture of the same
  exchange (`elmagnificogi_nsre`, context tier) shows the payload commands the *reader* to
  read the whole tag into its buffer; the console then waits for a signal before collecting it
  with `0x15`. On NS1 that signal is the status line's state byte moving to `04` (the
  `09 31 04` trailer after the pushed data); on NS2 the same channel survives as the **first
  byte of the `0x05` answer**, and the second implementation reads its lifecycle as `0x09`
  (tag in field) → `0x04` for as long as `0x06`'s read is armed → back to `0x09`, or `0x07`
  when `0x04` ends the scan with the read complete (`ns_pc_control/server/src/s2_nfc_codec.cpp:731-900`,
  context tier). That matches the bench's own finding exactly — **a `0x05` answer carrying `04`
  is what unlocked the first console pull** (`register-screen-bench.md`) — and the second
  implementation needs no push at all. **Landed by #48:** the lifecycle is a *level* — `04` on
  every `0x05` of the armed window, the state the console polls for — and the read completes when
  a `0x15` serves the **final chunk of the served space** (the reference's own
  `offset + payload_len − 3 >= buffer.size()` check, `ns_pc_control/server/src/s2_nfc_codec.cpp:837-844`,
  context tier); the bare marker past the space does not complete it.
  The `NFC_TAG_READ_DONE_*`/`NFC_TAG_STATUS_DONE_*` knobs are retired with it (`nfc_tag.h`), the
  `0x03`-after-`0x06` guess was retired earlier, and the interleaving with the report byte is
  §4.9's event counter. The `0x05` flags and the bare `0x06` ACK stay as weakened fallback
  suspects (`amiibo-game-surface-bench.md` §4).
- **The read pipeline opens — measured on the register screen (`register-screen-bench.md`).**
  The register screen binds its read to the controller that pressed A into it (the prompt's
  icon; #36's "console's own reader" was an artefact of the operator's own navigation), and
  re-arms the full read every ~3.1 s. Three facts now order the flow: the console sends
  **nothing** in its ~3 s post-`0x06` window; a **whole-tag push
  of `0x15`-shaped notifications** unlocks the console's own `0x15` pulls — the first ever
  observed against this device; and a `0x05` **answer** carrying the read-done state `04`
  triggers the first pull within 30 ms (the same bytes pushed unsolicited are ignored — the
  console's response parser correlates by subcommand). The silence is the observation; *"it
  waits on device pushes"* was the day's reading and is the weaker one now, since the answers
  the device gave it were the superseded shapes (the head, the 64-byte chunk and the
  image-at-0 space above). **The gate's framing half is closed by the run (#45):** with the
  corrected shapes the console pulled every chunk it asked for — eight sequential chunks from
  wire `0x46` to `0x230`, no crash — and then re-prompted instead of advancing. The continuation
  is the *re-presentation lifecycle* (the same identity placed again, the reference's
  continue-for-write): that placement armed a read the console never pulled, and the
  unsolicited whole-tag push was followed by the console's `2011-0301` force-reboot. **Six** frame
  combinations across three sessions have now
  **crashed the console's amiibo module** (`2011-0301`, forced reboot — the ledger and its
  isolated factors are G-18's, §12.3; the record is `register-screen-bench.md` §3, §7, §9). The bench
  knobs are `NFC_TAG_PUSH_READ_DATA` (default OFF; status-first, the safe order —
  the push now serves the status the lifecycle arms rather than overriding it) and, since the run,
  `NFC_TAG_DEFER_READ_EJECT` (default OFF; the reference's re-presentation rule, whose same-UID take
  crashed the console — §12.3 G-18). The knob family
  the sessions used to pace the answer and the byte is **gone**: `NFC_TAG_STATUS_DONE_WHEN_READ`
  and `NFC_TAG_STATUS_DONE_ONCE` are #48's landed lifecycle, `NFC_TAG_READ_DONE_BYTE`/`MS` are
  §4.9's event counter, and `NFC_TAG_READ_PAD_TO`/`NFC_TAG_BUFFER_P1_PREFIX` were already deleted
  by #46. The plain-offset view survives only as the host suite's comparison compile
  (`NFC_TAG_READ_PLAIN_VIEW`).
- **The `0x14` payload is framed, and the device stages it (#47).** The canonical write
  capture opens `d0 07 <uid7> 01 …` — write-setup framing, not image bytes at an offset — and
  the device accumulates it into a 454-byte staging frame (the capture's `4c 00` = 76, six
  chunks) that `0x08` commits. The frame, in staging coordinates:

  | Offset | Contents |
  | --- | --- |
  | `0`–`1` | `d0 07` — the marker; anything else refuses the commit |
  | `2`–`8` | the placed tag's seven-byte UID; a mismatch refuses the commit |
  | `9`–`16` | opaque (the capture carries `01 00 01 04 ff ff ff ff`, which nothing reads) |
  | `17`–`20` | four bytes copied to tag-image `[16..19]` — page 4, the header/write-counter word (the capture's `a5 f9 a5 00`) |
  | `21` | record count, `1`–`16` |
  | `22`… | the records: `page` u8 · `length` u8 · `length` bytes, each landing at tag-image `page × 4`; pages `0`–`3` (identity, lock bits, capability container) are refused |
  | …–`453` | zero padding, required: a non-zero byte here refuses the commit |

  The count, the header and the first record (page `5`, length `32`) all line up with
  `switch2_controller_research/commands.md:67`'s example, and the decode was written down from
  the second implementation's capture-derived staging (`ns_pc_control/server/src/s2_nfc_codec.cpp:201-278`,
  context tier) before it was ported. The commit writes the `17`–`20` word **first and the
  records after it**, so a record aimed at page `4` — the page that word writes — wins; that
  order is the reference implementation's, and the host suite asserts it rather than leaving it
  implicit.

- **The console-facing *offset* space is `image = wire − 0x3C`, and arithmetic settles it.**
  The canonical capture reads at wire offset `0x46` and the returned data opens on the image's
  static-lock/capability bytes at tag-image offset `0x0A` — the corpus's own page 2 carries
  `0F E0`, so the capture's leading `0f e0` is the image **verbatim** and the earlier "first
  16-bit word transposed" reading was wrong. The shift is a constant **60 = 0x3C**, which is
  exactly the framing head's length: the wire space *is* the 600-byte read buffer, so there is
  no permutation and no table to invent. The **out-of-range** case is a deliberate deviation
  from the reference: the bench's third probe (`0x2c0` = 704, beyond even 600) was answered
  with nothing and the cycle stopped, so #46 chose the bare last-chunk marker `01 00 00`
  instead of silence — where the second implementation *refuses* the ask (no payload, status
  `07 41`), because its handler guards the marker path behind `offset < op_buffer.size()`
  (`ns_pc_control/server/src/s2_nfc_codec.cpp:831-846`, context tier). The marker is not the
  read's end (#48): only the chunk that reaches the served space's end completes a read. The
  plain-offset server (`NFC_TAG_READ_PLAIN_VIEW`, `nfc_tag.h`, default `0`) exists only as the
  host suite's comparison compile; writes address the staging stream, which the captured
  `0x14` at offset `0` confirms.
- **What the framing head's 60 bytes contain: two parts the capture fixes, one it does not.**
  The UID at `8` and the nine-byte echo of the console's own `0x06` request at `51` are
  read off the exchange itself. Between them sits a 32-byte constant that **both** captures
  carry at `19` — the canonical NS2 read buffer's bytes and the NS1 P1 packet's `magic` blob
  are the same 32, from two different sessions and two different consoles' eras, which is the
  strongest single piece of evidence in this chapter. The head's opening **eight** bytes —
  `04 00 00 00 01 02 00 07`, offsets `0`–`7`, the UID starting at `8` — come from one source
  only: the second implementation's genuine
  PC2 read buffer (`ns_pc_control/server/src/s2_nfc_codec.cpp:158-199`, context tier). They are
  built as of #46 because no other source has them at all, and the NS1 P1 prefix the bench
  builds carried is **deleted**, not kept as an alternative: it was the wrong console's packet
  header, never a candidate content. **The bench has since served it** (#45): the console pulled
  the whole 600-byte space on the register screen (`register-screen-bench.md` §9.1), so the head
  is accepted — what the console does *after* a whole read is the open gate.
- **Every console NFC exchange is traced, never logged inline.** `nfc_trace.h`/`.c` (portable,
  host-tested) records each distinct `(subcommand, offset)` with the CRC of exactly the bytes
  served — §2.2's call — and `control_parser.c` drains it as `console nfc:` INFO lines at the
  *scan's* edge (the console's `0x04`, the container's unplace), one line per lock hold, so the
  ~9 round trips never pay for their own instrumentation. The `0x0C` probe logs its one line
  inline (`ns2_codec.c`): once per console connect, never inside a scan. The bench that reads
  these lines is `scripts/bench_amiibo_read.py`.
- **The probe answers the documented `61 12 50 0d`.** The shipped firmware's `…10` was A/B-
  tested against it on the bench — identical console behaviour to the subcommand on every
  surface reached — and the documented value is now what the device answers. A second,
  independent genuine-Pro-Controller-2 capture reads `61 12 50 10`
  (`ns_pc_control/server/src/switch2_native.cpp:661-666`, context tier), so the byte is a
  firmware-version question rather than a wrong-value one. G-12 stays open with its question
  narrowed (§12.3): the probe/status path is byte-neutral, the read-start gate is the untested
  half.
- The state byte is the `nfc_state` field of `hid_report_pro2_t` (§4.9), written into both report
  buffers by `controller_ops_t.set_nfc_state`; §4.9 owns its values and the reader events that
  move it (ADR-0016). `STATUS.console_polling` is the server's *other* output — the console's own
  level, which moves on `0x03`/`0x04`/`0x05` in any mode, which §4.9 and §3.2 keep as two signals.

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
3. An `unpack` round trip against the mounted library, **requiring both HMACs** — against a
   bounded sample of figures (eight, in the index's own order) rather than one, because the
   corpus ships files whose own signatures do not verify (`Pikmin Amiibo/Pikmin.bin`, which the
   pinned `amiitool` refuses too), and a single sample would accuse a good key of being the
   wrong one.

**States:** `KEY_OK`, `KEY_ABSENT`, `KEY_INVALID`, `KEY_UNVERIFIED`. `KEY_UNVERIFIED` exists
because a user may mount the key without a library, and the honest answer is then "held but
unproven" rather than "valid" (a lie) or "invalid" (a false accusation). **With no library tag
at startup, verification completes at the first placement**; a failure there sets
`KEY_INVALID` and refuses the placement. A failure against a key that already verified is the
*figure's*, not the key's: that placement is refused, the key keeps `KEY_OK`, and the three
locks stay distinct. It **never** falls back to replaying a stored dump
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
