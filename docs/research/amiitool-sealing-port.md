# Research: Porting amiitool: The Sealing Flow and the Two Coordinate Systems (G-9)

**Scope:** the exact `unpack`/`pack` sealing flow to port container-side, the two NTAG215 coordinate systems, and the minimum faithful port surface — resolving known gap G-9.
**Date:** 2026-10-02. **Wayfinder ticket:** #28. **Result:** G-9 resolved; §6.3's tag-image region table is wrong and must be corrected.
**Sourcing:** `amiitool` and `Amiibo` cited by alias against the pins in `docs/references.md`; in-repo claims cite `path:line`.


## Summary
The amiibo subsystem relies on two distinct coordinate systems: **tag-image coordinates** (540 bytes, matching physical NTAG215 pages 0–134) and **internal/decrypted coordinates** (520 bytes, amiitool's memory representation for cryptographic operations). The container's plaintext cache is held strictly in **internal coordinates**, where the 8-byte identity block sits at offset `0x1D4` (not `0x000`), Data HMAC sits at `0x008` (tag `0x080`), and Tag HMAC sits at `0x1B4` (tag `0x034`). Every placement performs a pure re-seal: deep-copy the 520-byte plaintext cache, overwrite the identity block at `0x1D4` and `BCC1` at `0x000`, derive fresh AES/HMAC keys via DRBG, calculate Tag HMAC and streaming Data HMAC, encrypt payload with AES-128-CTR, remap to tag-image coordinates, and append the verbatim 20-byte configuration trailer (pages 130–134).

---

## Findings

### 1. Coordinate Systems and Plaintext Cache Architecture
**Claim:** The NTAG215 dump exists in two coordinate systems; the container's plaintext cache uses amiitool's 520-byte internal coordinate system where the identity block is located at `0x1D4`.  
**Sources:** `amiitool/amiibo.c:38-60`, `amiitool/include/nfc3d/amiibo.h:17`, `docs/spec/06-amiibo.md:60-78`.  
**Support:** direct evidence.  
**Confidence:** high.

amiitool operates on a 520-byte buffer (`NFC3D_AMIIBO_SIZE = 520`, 130 pages × 4 bytes). The remaining 20 bytes of an NTAG215 dump (pages 130–134, tag offsets `0x208`–`0x21B`) are not part of the encrypted/signed amiibo structure; they are preserved verbatim outside amiitool's core transformations (`amiitool/amiitool.c:134-138`).

The mapping between tag-image coordinates and internal coordinates is executed by `nfc3d_amiibo_tag_to_internal` (`amiitool/amiibo.c:38-46`) and inverted by `nfc3d_amiibo_internal_to_tag` (`amiitool/amiibo.c:48-56`). The complete side-by-side mapping across all slices is:

| Slice | Tag-Image Pages | Tag-Image Offset | Internal Offset | Length | Description & Cryptographic Role |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | 2–3 | `0x008`–`0x00F` | `0x000`–`0x007` | 8 B | Header: `BCC1` (byte 0), internal byte `0x48` (byte 1), static lock bits `0xE0 0x0F` (bytes 2–3), Capability Container CC `0xF1 0x10 0xFF 0xEE` (bytes 4–7). Unencrypted. |
| **2** | 32–39 | `0x080`–`0x09F` | `0x008`–`0x027` | 32 B | **Data HMAC** (`HMAC_POS_DATA`). Stored here; computed over Data + Tag HMAC + Identity/Tail. |
| **3a** | 4 | `0x010`–`0x013` | `0x028`–`0x02B` | 4 B | Unencrypted amiibo header / write counter prefix. |
| **3b** | 5–12 | `0x014`–`0x033` | `0x02C`–`0x04B` | 32 B | Encrypted payload (part 1 of AES-128-CTR). |
| **4** | 40–129 | `0x0A0`–`0x207` | `0x04C`–`0x1B3` | 360 B | Encrypted payload (part 2 of AES-128-CTR). In internal coordinates, slices 3b and 4 form a contiguous 392-byte (`0x188`) ciphertext block (`0x02C`–`0x1B3`). |
| **5** | 13–20 | `0x034`–`0x053` | `0x1B4`–`0x1D3` | 32 B | **Tag HMAC** (`HMAC_POS_TAG`). Stored here; computed over Identity + Plaintext Tail. |
| **6** | 0–1 | `0x000`–`0x007` | `0x1D4`–`0x1DB` | 8 B | **Identity Block**: `UID[0..2]`, `BCC0` (byte 3), `UID[3..6]` (bytes 4–7). Serves as seed for KDF and input to Tag HMAC. |
| **7** | 21–31 | `0x054`–`0x07F` | `0x1DC`–`0x207` | 44 B | Plaintext settings tail. Together with Slice 6, forms the 52-byte block (`0x1D4`–`0x207`) hashed by Tag HMAC. |
| **Trailer** | 130–134 | `0x208`–`0x21B` | *N/A* | 20 B | Dynamic lock bits, `CFG0`, `CFG1`, `PWD`, `PACK`. Not handled by internal layout; copied verbatim. |

*Reconciliation of Spec Note (G-9):* The table in `docs/spec/06-amiibo.md:64-67` placed the Data HMAC at pages 85–92 (`0x154`–`0x173`) and Tag HMAC at pages 93–116 (`0x174`–`0x1D3`). The canonical amiitool source proves that on the physical tag image:
- Tag HMAC sits at `0x034`–`0x053` (pages 13–20).
- Data HMAC sits at `0x080`–`0x09F` (pages 32–39).
- Identity sits at `0x000`–`0x007` (pages 0–1).
In the internal coordinate system (and thus the container's plaintext cache):
- Data HMAC sits at `0x008`–`0x027`.
- Tag HMAC sits at `0x1B4`–`0x1D3`.
- Identity sits at `0x1D4`–`0x1DB`.

---

### 2. The Exact Unpack and Pack (Sealing) Sequences
**Claim:** Sealing is a pure function that mints a fresh identity, updates the internal plaintext buffer at `0x1D4` and `0x000`, derives fresh cryptographic keys via DRBG, computes Tag and Data HMACs, encrypts the payload, converts to tag coordinates, and appends configuration trailer pages.  
**Sources:** `amiitool/amiibo.c:58-118`, `amiitool/amiitool.c:113-145`, `docs/spec/06-amiibo.md:80-99`.  
**Support:** direct evidence.  
**Confidence:** high.

#### A. The Unpack Step Sequence (`nfc3d_amiibo_unpack`)
Executed once when indexing or lazily loading a figure dump to create the plaintext cache:
1. **Intake and Slicing:** Accept raw 540-byte dump (`tag`). Take the first 520 bytes (`tag[0..519]`). Store the 20-byte trailer (`tag[520..539]`) alongside the cache for verbatim output copying.
2. **Coordinate Transformation:** Call `nfc3d_amiibo_tag_to_internal(tag, internal)`.
3. **Key Derivation:**
   - Extract 64-byte seed via `nfc3d_amiibo_calc_seed(internal, seed)`:
     - `seed[0x00..0x01] = internal[0x029..0x02A]`
     - `seed[0x02..0x0F] = 0x00` (14 zero bytes)
     - `seed[0x10..0x17] = internal[0x1D4..0x1DB]` (identity block)
     - `seed[0x18..0x1F] = internal[0x1D4..0x1DB]` (identity block repeated)
     - `seed[0x20..0x3F] = internal[0x1E8..0x207]` (32 tail bytes)
   - Derive `dataKeys` (AES key, AES IV, HMAC key) using `amiiboKeys->data` and `seed` via `nfc3d_keygen`.
   - Derive `tagKeys` (AES key, AES IV, HMAC key) using `amiiboKeys->tag` and `seed` via `nfc3d_keygen`.
4. **Decryption (`nfc3d_amiibo_cipher`):**
   - Decrypt 392 bytes (`0x188`) starting at `internal + 0x02C` into `plain + 0x02C` using AES-128-CTR with `dataKeys.aesKey` and `dataKeys.aesIV`.
   - Copy unencrypted sections: `plain[0x000..0x007] = internal[0x000..0x007]`, `plain[0x028..0x02B] = internal[0x028..0x02B]`, and `plain[0x1D4..0x207] = internal[0x1D4..0x207]`.
5. **Tag HMAC Verification:**
   - Compute HMAC-SHA256 using `tagKeys.hmacKey` (16 bytes) over `plain + 0x1D4`, length `0x34` (52 bytes: identity at `0x1D4` + tail at `0x1DC`).
   - Store digest at `plain + 0x1B4`.
   - Verify `memcmp(plain + 0x1B4, internal + 0x1B4, 32) == 0`.
6. **Data HMAC Verification:**
   - Compute HMAC-SHA256 using `dataKeys.hmacKey` (16 bytes) over `plain + 0x029`, length `0x1DF` (479 bytes: payload `0x029..0x1B3` + Tag HMAC at `0x1B4..0x1D3` + identity and tail at `0x1D4..0x207`).
   - Store digest at `plain + 0x008`.
   - Verify `memcmp(plain + 0x008, internal + 0x008, 32) == 0`.
7. **Cache Retention:** Retain `plain` (520 bytes, in internal coordinates) in process memory.

#### B. The Sealing / Rotation Pack Sequence (`nfc3d_amiibo_pack`)
Executed on every placement to generate a fresh 540-byte sealed tag:
1. **Buffer Isolation:** Copy the 520-byte cached plaintext into a working buffer `plain`. (Never modify the cache in place; `docs/spec/06-amiibo.md:120-125`).
2. **Identity Minting and Overwrite:**
   - Generate 7-byte UID: `UID[0] = 0x04` (NXP manufacturer prefix), `UID[1..6]` from CSPRNG.
   - Calculate check bytes:
     $$\text{BCC0} = 0\text{x}88 \oplus \text{UID}[0] \oplus \text{UID}[1] \oplus \text{UID}[2]$$
     $$\text{BCC1} = \text{UID}[3] \oplus \text{UID}[4] \oplus \text{UID}[5] \oplus \text{UID}[6]$$
   - Overwrite 8-byte identity at `plain + 0x1D4`:
     - `plain[0x1D4] = UID[0]`
     - `plain[0x1D5] = UID[1]`
     - `plain[0x1D6] = UID[2]`
     - `plain[0x1D7] = BCC0`
     - `plain[0x1D8] = UID[3]`
     - `plain[0x1D9] = UID[4]`
     - `plain[0x1DA] = UID[5]`
     - `plain[0x1DB] = UID[6]`
   - Update `BCC1` at `plain[0x000]`:
     - `plain[0x000] = BCC1`
3. **Key Derivation:**
   - Extract 64-byte seed from `plain` via `nfc3d_amiibo_calc_seed(plain, seed)`. The new UID at `0x1D4` is incorporated into `seed[0x10..0x1F]`.
   - Derive `tagKeys` via `nfc3d_keygen(&amiiboKeys->tag, seed, &tagKeys)`.
   - Derive `dataKeys` via `nfc3d_keygen(&amiiboKeys->data, seed, &dataKeys)`.
4. **Tag HMAC Generation:**
   - Compute HMAC-SHA256 with key `tagKeys.hmacKey` over `plain + 0x1D4`, length `0x34` (52 bytes).
   - Store digest at `cipher + 0x1B4` (`HMAC_POS_TAG`, 32 bytes).
5. **Data HMAC Generation:**
   - Compute HMAC-SHA256 with key `dataKeys.hmacKey` in streaming mode:
     - `hmac_update(plain + 0x029, 0x18B)` (395 bytes: payload `0x029`–`0x1B3`).
     - `hmac_update(cipher + 0x1B4, 0x20)` (32 bytes: Tag HMAC computed in step 4).
     - `hmac_update(plain + 0x1D4, 0x34)` (52 bytes: identity block + tail).
     Total data hashed = $0\text{x}18B + 0\text{x}20 + 0\text{x}34 = 0\text{x}1DF$ (479 bytes).
   - Store digest at `cipher + 0x008` (`HMAC_POS_DATA`, 32 bytes).
6. **Payload Encryption (`nfc3d_amiibo_cipher`):**
   - Encrypt 392 bytes (`0x188`) starting at `plain + 0x02C` into `cipher + 0x02C` using AES-128-CTR with `dataKeys.aesKey` and `dataKeys.aesIV`.
   - Copy passthrough blocks into `cipher`:
     - `cipher[0x000..0x007] = plain[0x000..0x007]` (preserves `BCC1` at `0x000`, internal byte `0x48`, lock bits, CC).
     - `cipher[0x028..0x02B] = plain[0x028..0x02B]` (write counter prefix).
     - `cipher[0x1D4..0x207] = plain[0x1D4..0x207]` (identity block and tail).
     *(Note: `cipher[0x008..0x027]` already holds Data HMAC; `cipher[0x1B4..0x1D3]` already holds Tag HMAC).*
7. **Coordinate Transformation to Tag Image:**
   - Call `nfc3d_amiibo_internal_to_tag(cipher, tag)`.
   - Result: 520 bytes in tag coordinates (`tag[0x000..0x207]`), with new identity at `tag[0x000..0x007]`, `BCC1` at `tag[0x008]`, Tag HMAC at `tag[0x034..0x053]`, and Data HMAC at `tag[0x080..0x09F]`.
8. **Append Trailer Pages:**
   - Copy 20 bytes from source dump: `memcpy(tag + 0x208, original + 0x208, 20)` (pages 130–134: dynamic locks, `CFG0`, `CFG1`, `PWD`, `PACK`).
9. **Result:** A valid, sealed 540-byte NTAG215 tag ready for transfer over the control link.

---

### 3. Minimum Faithful Port Surface
**Claim:** The complete sealing module requires only five cryptographic/structural primitives, eliminating dependencies on full external crypto libraries beyond standard AES-128 and SHA-256.  
**Sources:** `amiitool/keygen.c:13-39`, `amiitool/drbg.c:13-60`, `amiitool/include/nfc3d/keygen.h:16-30`.  
**Support:** direct evidence.  
**Confidence:** high.

The sealing port implementation surface consists of:
1. **Masterkey Representation (160 Bytes):**
   ```c
   typedef struct {
       uint8_t hmacKey[16];
       char    typeString[14];
       uint8_t rfu;
       uint8_t magicBytesSize;
       uint8_t magicBytes[16];
       uint8_t xorPad[32];
   } nfc3d_keygen_masterkeys; // 80 bytes

   typedef struct {
       nfc3d_keygen_masterkeys data; // 80 bytes (unfixed-info)
       nfc3d_keygen_masterkeys tag;  // 80 bytes (locked-secret)
   } nfc3d_amiibo_keys;           // 160 bytes total
   ```
   Validation ladder check: file size == 160 bytes, `data.magicBytesSize <= 16`, and `tag.magicBytesSize <= 16` (`amiitool/amiibo.c:132-136`).
2. **Key Derivation (KDF / DRBG):**
   - **Seed Preparation (`nfc3d_keygen_prepare_seed`):** Concatenate:
     1. `typeString` up to and including the null terminator `\0`.
     2. Leading `(16 - magicBytesSize)` bytes of `baseSeed`.
     3. `magicBytes` (`magicBytesSize` bytes).
     4. Bytes `0x10..0x1F` of `baseSeed` (16 bytes: the identity repeated twice).
     5. Bytes `0x20..0x3F` of `baseSeed` XORed with `xorPad[0..31]` (32 bytes).
   - **DRBG Generation (`nfc3d_drbg_generate_bytes`):**
     - Big-endian 16-bit counter prepended to prepared seed: `[iteration_hi, iteration_lo, seed...]`.
     - HMAC-SHA256 using `masterkeys.hmacKey` (16 bytes).
     - Iteration 0 produces 32 bytes: `aesKey` (16 bytes) + `aesIV` (16 bytes).
     - Iteration 1 produces 32 bytes: first 16 bytes are `hmacKey` (16 bytes).
3. **AES-128-CTR:**
   - Standard 128-bit counter mode applied across 392 bytes (`0x188`) at internal offset `0x02C`.
4. **HMAC-SHA256:**
   - Tag HMAC: single-pass over 52 bytes (`plain + 0x1D4`).
   - Data HMAC: streaming (starts, update 3 chunks, finish) over 479 bytes.
5. **Static and Dynamic Configuration Pages:**
   - Static config (tag pages 2–3, `0x008`–`0x00F`): `BCC1`, internal byte `0x48`, static lock bits `0xE0 0x0F`, CC `0xF1 0x10 0xFF 0xEE`. (Passed through via `plain[0x000..0x007]`).
   - Dynamic config / trailer (tag pages 130–134, `0x208`–`0x21B`): dynamic lock bits, `CFG0`, `CFG1`, `PWD`, `PACK`. Copied verbatim from source dump.

---

### 4. Sanity Check Against Real Library Tag (`Samus.nfc`)
**Claim:** Derivation of check bytes `BCC0 = 0x63` and `BCC1 = 0x75` matches both ISO 14443-A standards and the physical dump `Samus.nfc`.  
**Sources:** `Amiibo/Amiibo NFC/Metroid/Metroid_Dread/Samus.nfc:1-26`, `docs/spec/06-amiibo.md:44-50`, `amiitool/amiibo.c:38-56`.  
**Support:** direct evidence.  
**Confidence:** high.

Inspection of the primary source file `Amiibo NFC/Metroid/Metroid_Dread/Samus.nfc` (pinned at `AmiiboDB/Amiibo@58cf4558de56863575421844bc27695ef1ba56ad`) confirms the opening pages:
```text
Page 0: 04 11 FE 63
Page 1: CA 52 6C 81
Page 2: 75 48 0F E0
Page 3: F1 10 FF EE
```

#### Step-by-Step Derivation:
1. **7-Byte UID Extraction:**
   - `UID[0] = 0x04` (NXP manufacturer code)
   - `UID[1] = 0x11`
   - `UID[2] = 0xFE`
   - `UID[3] = 0xCA`
   - `UID[4] = 0x52`
   - `UID[5] = 0x6C`
   - `UID[6] = 0x81`
   *(Full 7-byte UID: `04 11 FE CA 52 6C 81`).*
2. **BCC0 Derivation (Cascade Tag CT = 0x88):**
   $$\text{BCC0} = 0\text{x}88 \oplus \text{UID}[0] \oplus \text{UID}[1] \oplus \text{UID}[2]$$
   $$0\text{x}88 \oplus 0\text{x}04 = 0\text{x}8C$$
   $$0\text{x}8C \oplus 0\text{x}11 = 0\text{x}9D$$
   $$0\text{x}9D \oplus 0\text{x}FE = 0\text{x}63$$
   Byte 3 at tag offset `0x003` is exactly `0x63`.
3. **BCC1 Derivation:**
   $$\text{BCC1} = \text{UID}[3] \oplus \text{UID}[4] \oplus \text{UID}[5] \oplus \text{UID}[6]$$
   $$0\text{x}CA \oplus 0\text{x}52 = 0\text{x}98$$
   $$0\text{x}98 \oplus 0\text{x}6C = 0\text{xF4}$$
   $$0\text{xF4} \oplus 0\text{x}81 = 0\text{x}75$$
   Byte 8 at tag offset `0x008` (Page 2, Byte 0) is exactly `0x75`.

*Typographical Note in Spec:* In `docs/spec/06-amiibo.md:46`, the text states:
> `Samus.nfc` opens `04 11 fe 63 ca 52 6c 81`, so `UID = 04 11 FE 63 CA 52 6C`, `BCC0 = 0x88^0x04^0x11^0xFE = 0x63` at offset 3, and `BCC1 = 0xCA^0x52^0x6C^0x81 = 0x75` at offset 8.

The formula and the check bytes are completely correct. The prose string `UID = 04 11 FE 63 CA 52 6C` accidentally included `63` (`BCC0`) and dropped `81` (`UID[6]`). The actual 7-byte UID is `04 11 FE CA 52 6C 81`.

---

## Contradictions
1. **Spec Table vs Canonical amiitool Coordinates (G-9):**
   - `docs/spec/06-amiibo.md:64-67` placed Data HMAC at tag offsets `0x154`–`0x173` and Tag HMAC at `0x174`–`0x1D3`.
   - `amiitool/amiibo.c:38-56` establishes that in tag coordinates, Tag HMAC is at `0x034`–`0x053` and Data HMAC is at `0x080`–`0x09F`. In internal coordinates, Data HMAC is at `0x008`–`0x027` and Tag HMAC is at `0x1B4`–`0x1D3`. The spec explicitly flagged this as known gap G-9; the amiitool source resolves it definitively.
2. **UID String Representation in Spec 06.3:**
   - `docs/spec/06-amiibo.md:46` wrote the 7-byte UID as `04 11 FE 63 CA 52 6C`, whereas `Samus.nfc` and the BCC equations prove the 7-byte UID is `04 11 FE CA 52 6C 81`.

---

## Missing Evidence
None. All algorithms, key formats, memory layouts, offsets, and check bytes have been verified directly against the pinned source trees of `socram8888/amiitool` and `AmiiboDB/Amiibo`.

---

## Sources
- **Kept:**
  - `amiitool/amiibo.c` (`socram8888/amiitool@4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3`): `nfc3d_amiibo_tag_to_internal`, `nfc3d_amiibo_internal_to_tag`, `nfc3d_amiibo_cipher`, `nfc3d_amiibo_unpack`, `nfc3d_amiibo_pack`, `nfc3d_amiibo_calc_seed`.
  - `amiitool/keygen.c` (`socram8888/amiitool@4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3`): `nfc3d_keygen_prepare_seed`, `nfc3d_keygen`.
  - `amiitool/drbg.c` (`socram8888/amiitool@4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3`): DRBG counter initialization, step, and stream generation.
  - `amiitool/include/nfc3d/amiibo.h` and `keygen.h` (`socram8888/amiitool@4fe80a1de5ae19e1a1a6a7faeca645dafd0189c3`): Structure layouts for `nfc3d_keygen_masterkeys` (80 B) and `nfc3d_amiibo_keys` (160 B).
  - `Amiibo/Amiibo NFC/Metroid/Metroid_Dread/Samus.nfc` (`AmiiboDB/Amiibo@58cf4558de56863575421844bc27695ef1ba56ad`): Primary reference dump verifying opening pages and check byte derivations.
  - `docs/spec/06-amiibo.md:38-125` and `docs/spec/12-handoff.md:86-88`: Repo specifications defining the sealing model and known gap G-9.
- **Rejected/Deprioritized:**
  - `emuiibo`: Deprioritized because it uses decrypted JSON projections without cryptographic signatures or raw tag packing.

---

## Next Steps
1. Implement the sealing module container-side in Python/C conforming strictly to the 8-slice mapping table and step sequences documented above.
2. Build an offline unit test verifying that `pack(unpack(Samus.bin))` produces an image byte-identical to `Samus.bin`, and that sealing with a randomized UID passes both HMAC verifications.
