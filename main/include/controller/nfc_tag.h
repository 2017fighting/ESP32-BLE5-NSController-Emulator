#ifndef CONTROLLER_NFC_TAG_H
#define CONTROLLER_NFC_TAG_H

/*
 * The NFC state machine and the tag server (spec §4.9, §6.5, §6.6, §7.3 step 6;
 * issue #25).
 *
 * The device is a byte-sink here (ADR-0011): it holds one 540-byte NTAG215 image
 * in RAM, slices it when the console asks, and does no cryptography. Two halves
 * meet in this module:
 *
 *  - the **control plane's** `PLACE_AMIIBO`/`UNPLACE_AMIIBO` effects, which put
 *    the sealed bytes in and take them out (`nfc_tag_place`/`nfc_tag_unplace`);
 *  - the **console's** command `0x01` subcommands `0x03`/`0x04`/`0x05`/`0x06`/
 *    `0x14`/`0x15`, which are the reads and writes over that buffer
 *    (`nfc_tag_command`).
 *
 * It is portable C on purpose: the whole state machine — placement, the
 * tag-absent gap, the polling edges and the page-wise slice — is asserted on the
 * host (`test/host/test_nfc_tag.c`) instead of inferred from a bench capture.
 * The ESP-IDF glue is `control_parser.c` (the singleton, the report byte and the
 * `SCAN_ENDED` event) and `ns2_codec.c` (the command router).
 *
 * **Two outputs, and the difference matters (§4.9 against §3.2).** The module
 * produces:
 *
 *  - `nfc_tag_state()` — the **report byte**, HID input report `0x09` offset
 *    `0x0C`. It is the mode's physical expression: `0x00` while nothing is in
 *    the field (no tag, or the §6.5 gap), and the **reader's event counter**
 *    while a tag is placed — `0x01`–`0x07`, advanced on every reader event
 *    (tag presented, scan ready `0x03`, operation ready `0x06`, write complete
 *    `0x08`, tag removed) and wrapping `0x07 → 0x01` so that `0x00` stays
 *    reserved for "no tag in field" (amended by #48; §4.9 carries the rule and
 *    the reference evidence). A console that polls while the device is in
 *    `IDLE` or `MACRO` still changes nothing here, which is what keeps "only one
 *    mode at a time" a property of the wire.
 *  - `nfc_tag_polling()` — `STATUS.console_polling`, the console's own level
 *    (`IDLE`/`POLLING`/`TAG_DETECTED`). It moves on the console's `0x03`/`0x04`/
 *    `0x05` regardless of mode, because a game may open its amiibo menu at any
 *    time, and the container rotates on its fall to `IDLE` (§3.2, §6.5).
 *
 * **The status answer is the reader's lifecycle (#48).** The `0x05` answer's
 * first byte is the state, and its second is the detail byte the captured flags
 * carry as `0x00`:
 *
 *  - `0x09` — a tag is in the field and nothing is armed (the capture's value);
 *  - `0x04` — `0x06`'s read is armed, answered as a **level** on every ask of
 *    the window (the NS2 translation of the NS1 P3 trailer, and the answer the
 *    bench measured unlocking the console's first `0x15` pull);
 *  - `0x05` — a `0x08` commit landed, scan-scoped until `0x04` ends it;
 *  - `0x07` + detail `0x41` — **post-eject**: `0x04` ended a scan whose read
 *    had reached the end of the served space, so the field is empty for the
 *    rest of the placement. A `0x04` after an incomplete read answers `0x09`.
 *
 * Whether the *never-placed* answer should also be `0x07 41` is open (G-19);
 * this module keeps answering `0x00` there.
 *
 * Collapsing the two would either lie about the mode or lose the container's
 * rotation edge; keeping them separate is why a `POLLING` byte value is legal in
 * the vocabulary and never on the wire (a placed tag is the only `AMIIBO` state,
 * so the byte is `0x00` or `0x02` and nothing between).
 *
 * **The served shapes are the capture's (§6.6, #46).** The `0x15` answer's
 * three-byte head is `last` u8 · `len` u16 (LE); one chunk is 70 bytes; and the
 * request's offset addresses a `[60 B framing][540 B image]` space, so
 * `image = wire − 0x3C` (`switch2_controller_research/commands.md:68` — the
 * request's own `46 00` is the 70 bytes already consumed). The framing head is
 * built per placement and per `0x06`: the placed UID at `8`, the 32-byte
 * constant both captures carry at `19`, and the request's own nine bytes echoed
 * at `51`:
 *
 *     0x15 request   offset u16 (LE)
 *     0x15 response  last u8 · len u16 (LE) · len bytes of the space, ≤ 70
 *     0x06 request   d0 07 · UID(7) · 01 · the page ranges (its [10..18] rides the head)
 *     0x14 request   offset u16 (LE) · len u16 (LE) · len bytes  → the staging stream
 *     0x08            commit the stream: its records → the volatile image, status 0x05
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* §6.2: the tag is the raw 540-byte NTAG215 image — 135 pages x 4 B. */
#define NFC_TAG_SIZE 540u

/* §7.7/§7.4: the capture's `0x15` response is 8 + 3 + 70 = 81 bytes on the wire
 * and carries 70 bytes, so the whole 600-byte read is nine round trips (§6.6). */
#define NFC_TAG_READ_CHUNK 70u

/* The chunk head's own length: `last` u8 · `len` u16 (LE). */
#define NFC_TAG_READ_HEAD_SIZE 3u

/* §6.6: the framing head — the UID, the 32-byte constant and the echoed request
 * — and the space it opens onto the image. */
#define NFC_TAG_FRAME_SIZE 60u

/* The comparison compile (spec §6.6, #46): the superseded plain-offset view —
 * the image at wire 0 in a 540-byte space, no framing head. Off by default; a
 * bench build flips it to compare the two shapes in one reflash, and it is a
 * comparison, not a candidate. */
#ifndef NFC_TAG_READ_PLAIN_VIEW
#define NFC_TAG_READ_PLAIN_VIEW 0
#endif
#if NFC_TAG_READ_PLAIN_VIEW
#define NFC_TAG_SERVED_SIZE NFC_TAG_SIZE
#else
#define NFC_TAG_SERVED_SIZE (NFC_TAG_FRAME_SIZE + NFC_TAG_SIZE)
#endif

/* §6.6: the framing head echoes the `0x06` request's own `payload[10..18]` at the
 * head's `[51..59]` — the nine bytes of the canonical read request (`01 03` and
 * its three page ranges). */
#define NFC_TAG_READ_OP_OFFSET 10u
#define NFC_TAG_READ_OP_SIZE 9u

/* §4.9's report byte while a tag is placed (#48): the reader's event counter.
 * `0x00` is reserved for "no tag in field", so the counter runs `0x01`–`0x07`
 * and wraps `0x07 → 0x01` rather than through zero. The reference's own byte is
 * `(previous + 1) & 0x07` on the same events (`virtual_controller.cpp:195-266`,
 * context tier); the reserved zero is this device's addition, and it is what
 * keeps §4.9's "only one mode at a time" claim true after the amendment. */
#define NFC_REPORT_COUNTER_MIN 0x01u
#define NFC_REPORT_COUNTER_MAX 0x07u

/* The whole-tag push after `0x06` (the register-screen session, 2026-10-04):
 * with the byte at `0x04` and a full-log tee, the console sent *nothing* for
 * the whole 3.0 s window (`d0 07` reads as a 2000 ms deadline) — it waits for
 * a device-initiated notification, and pushing the whole tag as `0x15`-shaped
 * notifications is what unlocked its own `0x15` pulls, the first ever
 * observed. The push opens with the status answer (`0x06` has armed the read by
 * then, so it is the `04` level) and follows with 70-byte chunks of the served
 * space; the *closing*-trailer order is the one that crashed the console twice
 * (`2011-0301`, G-18), so the tree builds the safe order. A status-only push
 * variant existed and was falsified — the console drops unsolicited sub-0x05
 * frames — and was removed from the tree (the record: `register-screen-bench.md`
 * take 4). Default OFF; device-side only; the console's own `0x15` asks still
 * take the normal response path. */
#ifndef NFC_TAG_PUSH_READ_DATA
#define NFC_TAG_PUSH_READ_DATA 0
#endif

/* §6.1/§6.3: the identity is the seven-byte NFC UID. */
#define NFC_TAG_UID_SIZE 7u

/* §3.3's `0x05` response: status, flags, UID length and UID in the captured
 * layout (`switch2_controller_research/commands.md:63`). */
#define NFC_STATUS_RESPONSE_SIZE 61u

/* §6.5's tag-absent gap, held on the wire before the new tag answers. Two report
 * periods at the 10 ms grid (`CONFIG_FREERTOS_HZ=100`, §7.5), so the console
 * necessarily samples `0x00` — a gap the reporter can lose is not a guarantee.
 * The designer's note that the requirement is unproven (G-6) is about the console
 * needing the gap, not about whether this device emits one. */
#define NFC_TAG_GAP_MS 20u

/* §3.2's `console_polling` vocabulary — the console's own level, and the only
 * thing the enum names. The HID report byte is no longer a vocabulary (§4.9):
 * `0x00` is "no tag in the field" and `0x01`–`0x07` are the reader's event
 * counter, so `NFC_STATE_TAG_DETECTED` must not be compared against it. */
enum {
    NFC_STATE_IDLE = 0,         /* no tag in the field / the console is not asking */
    NFC_STATE_POLLING = 1,      /* the console is asking, no tag in the field */
    NFC_STATE_TAG_DETECTED = 2, /* the console is asking and a tag is answering */
};

/* §6.6's `0x05` answer bytes, the status lifecycle (#48): `0x09` = tag in
 * field, `0x04` = a read is armed, `0x05` = a write was committed and the
 * console has not ended the scan yet, `0x00` = no tag (the never-placed answer,
 * which G-19 leaves open), and `0x07` + detail `0x41` = post-eject. The first
 * byte is the status the console reads; the second is the detail byte the
 * captured flags carry as `0x00`. */
#define NFC_STATUS_TAG_DETECTED 0x09u
#define NFC_STATUS_NO_TAG 0x00u
#define NFC_STATUS_READ_ARMED 0x04u
#define NFC_STATUS_WRITE_COMMITTED 0x05u
#define NFC_STATUS_EJECTED 0x07u
#define NFC_STATUS_EJECTED_DETAIL 0x41u

/* §6.6's G-17 frame: the `0x14` chunks fill a 454-byte staging stream (the
 * capture's `4c 00` = 76, six chunks), and `0x08` commits it. Coverage is a
 * bitmap over the same bytes, so a partial stream can never commit. */
#define NFC_TAG_WRITE_STAGING_SIZE 454u
#define NFC_TAG_WRITE_COVERAGE_BYTES ((NFC_TAG_WRITE_STAGING_SIZE + 7u) / 8u)
/* The bitmap's last byte carries padding bits no stream byte can set, so
 * coverage is complete when every byte is full but that one's tail. */
#define NFC_TAG_WRITE_COVERAGE_TAIL_MASK ((uint8_t)((1u << (NFC_TAG_WRITE_STAGING_SIZE % 8u)) - 1u))
#define NFC_TAG_WRITE_HEADER_OFFSET 17u /* 4 bytes → image[16..19] */
#define NFC_TAG_WRITE_HEADER_TARGET 16u
#define NFC_TAG_WRITE_RECORD_COUNT_OFFSET 21u
#define NFC_TAG_WRITE_MAX_RECORDS 16u
/* Pages 0–3 are the identity, the lock bytes and the capability container, so no
 * record may target them. Page 4 is legal: it is the page the frame's own header
 * word writes, and a record aimed there lands **after** it (the commit writes the
 * header first), which is the order the reference implementation applies too. */
#define NFC_TAG_WRITE_FIRST_PAGE_OFFSET 16u

/* §2's command `0x01` subcommands this server answers. `0x0C` (the PN7160
 * capability probe) stays with `ns2_codec.c` — it is a constant, not state. */
enum {
    NFC_CMD_START_POLLING = 0x03,
    NFC_CMD_STOP_POLLING = 0x04,
    NFC_CMD_GET_STATUS = 0x05,
    NFC_CMD_READ_DEVICE = 0x06,
    NFC_CMD_COMMIT_WRITE = 0x08,
    NFC_CMD_WRITE_BUFFER = 0x14,
    NFC_CMD_READ_BUFFER = 0x15,
};

/*
 * The two effects this module cannot perform itself. Both are edges, never
 * levels, and a NULL callback is a no-op (the host's state-only tests).
 */
typedef struct {
    void *ctx;
    /* The report byte changed: write it into the HID report (§4.9). Fires on
     * each of the reader's five events — tag presented (a placement or the gap's
     * close), scan ready (`0x03`), operation ready (`0x06`), write complete
     * (`0x08`) and tag removed (an unplacement, or the `0x04` that ejects a
     * completed read) — and on the gap's open, which is a tag-removed event. */
    void (*state_changed)(void *ctx, uint8_t state);
    /* The console stopped asking for a tag (§3.3 `SCAN_ENDED`, §6.5) — the
     * container's cue to mint and push the next identity. */
    void (*scan_ended)(void *ctx);
} nfc_tag_events_t;

typedef struct {
    uint8_t tag[NFC_TAG_SIZE];
    /* The `0x06` request's own `payload[10..18]`, echoed by the framing head's
     * `[51..59]`. Cleared on placement and on a new scan, so a stale echo can
     * never ride into the next tag (§6.6). */
    uint8_t read_req_echo[NFC_TAG_READ_OP_SIZE];
    /* §6.6's G-17 frame: the `0x14` staging stream and its coverage bitmap, plus
     * whether a stream is in flight and whether `0x08` committed it. One noun —
     * the write transaction — which is why the four travel together. */
    struct {
        uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];
        uint8_t coverage[NFC_TAG_WRITE_COVERAGE_BYTES];
        bool active;
        bool committed;
    } write;
    /* §6.6's status lifecycle (#48), the `0x05` answer's state and detail bytes:
     * `0x09` tag in field, `0x04` a read is armed, `0x07` + `0x41` post-eject.
     * `write.committed` carries the `0x05` write state, which is scan-scoped. */
    struct {
        bool armed;         /* `0x06` armed the read; `0x03`/`0x04` end it */
        bool read_complete; /* a `0x15` reached the end of the served space */
        bool ejected;       /* post-eject: `0x04` ended a completed read */
    } lifecycle;
    bool placed;     /* the tag is answering reads */
    bool staged;     /* a replacement's bytes are committed and the gap is open */
    uint8_t counter; /* the reader-event counter behind `state` (0x01–0x07) */
    uint8_t state;   /* the report byte: 0x00, or the counter while a tag is placed */
    uint8_t polling; /* the console's level: NFC_STATE_IDLE/POLLING/TAG_DETECTED */
    uint32_t gap_until_ms;
    nfc_tag_events_t events;
} nfc_tag_t;

/* §4.8's boot state: nothing placed, the byte `0x00`, the console silent. */
void nfc_tag_init(nfc_tag_t *nfc);

void nfc_tag_set_events(nfc_tag_t *nfc, const nfc_tag_events_t *events);

/*
 * §4.3's `PLACE_AMIIBO` physical half: commit @p tag. @p len must be
 * `NFC_TAG_SIZE`; anything else is refused. @p now_ms is the tick clock the gap's
 * deadline is measured on (`nfc_tag_tick`).
 *
 * **The gap lives here (§6.5).** A replace drops the report byte to `0x00` — and
 * `0x05`/`0x15` answer "no tag" — for `NFC_TAG_GAP_MS` before the new tag
 * answers, so the console samples a tag-absent field rather than an atomic
 * swap. A device with no tag yet has nothing to remove, so a first placement has
 * no gap. Whether the console *needs* the gap is an open question (G-6); the
 * design's answer is to emit it as the safe superset, and this is that emission.
 */
void nfc_tag_place(nfc_tag_t *nfc, const uint8_t *tag, size_t len, uint32_t now_ms);

/*
 * Advances the gap. Called from the firmware's 10 ms control task
 * (`control_parser.c`); a no-op when no replace is pending. The deadline is
 * `now_ms`-based, not an accumulated tick count, so a late tick cannot extend the
 * gap, only observe it as over.
 */
void nfc_tag_tick(nfc_tag_t *nfc, uint32_t now_ms);

/*
 * §4.3's `UNPLACE_AMIIBO`/mode-exit physical half: the tag stops answering and the
 * report byte returns to `0x00`. The bytes are retained (a later `place`
 * overwrites them) and any `0x14` write-back is discarded with them.
 */
void nfc_tag_unplace(nfc_tag_t *nfc);

/* The HID report's `nfc_state` byte (§4.9). */
uint8_t nfc_tag_state(const nfc_tag_t *nfc);

/* `STATUS.console_polling` (§3.2): the console's own level. */
uint8_t nfc_tag_polling(const nfc_tag_t *nfc);

/* §6.6: a placement is committed and its bytes are in RAM. A completed read's
 * `0x04` ejects it from the *reader's* field — `0x05` then answers `07 41` and
 * `0x15` answers nothing — until the next placement. */
bool nfc_tag_placed(const nfc_tag_t *nfc);

/* §6.3: `UID[0..2]` then `UID[3..6]`, skipping the `BCC0` check byte at byte 3. */
void nfc_tag_identity(const nfc_tag_t *nfc, uint8_t out[NFC_TAG_UID_SIZE]);

/*
 * Handles one command `0x01` subcommand. @p payload is the bytes *after* the
 * 8-byte command header and @p len is how many of them arrived. Returns the
 * response length written to @p out, or 0 when the subcommand has no response or
 * is not one this server owns.
 */
size_t nfc_tag_command(nfc_tag_t *nfc, uint8_t subcmd, const uint8_t *payload, size_t len,
                       uint8_t *out, size_t out_cap);

#ifdef __cplusplus
}
#endif

#endif /* CONTROLLER_NFC_TAG_H */
