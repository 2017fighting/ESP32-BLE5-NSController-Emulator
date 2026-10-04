/*
 * The NFC state machine and the tag server (spec §4.9, §6.5, §6.6, §7.3 step 6;
 * issue #25). Portable C; the host suite links this file directly.
 *
 * See `nfc_tag.h` for the two halves that meet here, for the two *outputs* this
 * module produces — the report byte (`nfc_tag_state`) and the console's level
 * (`nfc_tag_polling`) — and for the shapes of the `0x14`/`0x15` payloads.
 */

#include "controller/nfc_tag.h"

#include <string.h>

/* The captured `0x05` flags at payload offsets 1-7
 * (`switch2_controller_research/commands.md:63`), reproduced verbatim: the
 * console branches on the status byte at offset 0 (which the second
 * implementation moves `09`→`04`→`07`; spec §6.6), and inventing values for
 * fields nobody has decoded would be worse than echoing what was observed. */
static const uint8_t nfc_status_flags[7] = {0x00, 0x00, 0x00, 0x01, 0x01, 0x02, 0x00};

#if !NFC_TAG_READ_PLAIN_VIEW
/* §6.6: the framing head's 32-byte constant — the bytes both captures carry at
 * head offset 19 (`switch2_controller_research/commands.md:68`'s real read
 * buffer and `elmagnificogi_nsre`'s NS1 P1 packet hold the same 32, and the
 * second implementation ships them too). */
static const uint8_t nfc_frame_constant[32] = {
    0x7d, 0xfd, 0xf0, 0x79, 0x36, 0x51, 0xab, 0xd7, 0x46, 0x6e,
    0x39, 0xc1, 0x91, 0xba, 0xbe, 0xb8, 0x56, 0xce, 0xed, 0xf1,
    0xce, 0x44, 0xcc, 0x75, 0xea, 0xfb, 0x27, 0x09, 0x4d, 0x08,
    0x7a, 0xe8,
};

/* The 60-byte framing head, rebuilt per placement and per `0x06`: the placed
 * tag's UID (whose seven bytes skip the `BCC0` check byte), the constant, and
 * the nine bytes the console's own read request put at its `[10..18]`. A pure
 * function of the server's state, so a rotation can never leave a stale head
 * behind. */
static void nfc_build_frame(const nfc_tag_t *nfc, uint8_t out[NFC_TAG_FRAME_SIZE])
{
    memset(out, 0, NFC_TAG_FRAME_SIZE);
    out[0] = 0x04;
    out[4] = 0x01;
    out[5] = 0x02;
    out[7] = 0x07;
    nfc_tag_identity(nfc, &out[8]);
    memcpy(&out[19], nfc_frame_constant, sizeof(nfc_frame_constant));
    memcpy(&out[51], nfc->read_req_echo, sizeof(nfc->read_req_echo));
}
#endif

static uint16_t nfc_rd_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

/* §6.6: the head's echoed request is per scan — a new one carries none, so a
 * stale `0x06` can never ride into the next tag's framing. */
static void nfc_clear_read_op(nfc_tag_t *nfc)
{
    memset(nfc->read_req_echo, 0, sizeof(nfc->read_req_echo));
}

/* §6.6: a placement is virgin — the `0x14` stream, its coverage and the
 * committed status all die with it. */
static void nfc_reset_write(nfc_tag_t *nfc)
{
    memset(nfc->write.stream, 0, sizeof(nfc->write.stream));
    memset(nfc->write.coverage, 0, sizeof(nfc->write.coverage));
    nfc->write.active = false;
    nfc->write.committed = false;
}

/* The report byte's one writer, so `state_changed` fires exactly when the byte
 * moves and never on a polling change (§4.9). */
static void nfc_set_state(nfc_tag_t *nfc, uint8_t state)
{
    if (nfc->state == state) {
        return;
    }
    nfc->state = state;
    if (nfc->events.state_changed != NULL) {
        nfc->events.state_changed(nfc->events.ctx, state);
    }
}

/* §6.6: what the *reader* has in the field — a placement that a completed read
 * has not ejected. The HID byte, the console's level and the `0x15` answer are
 * all read from this, so a post-eject field can never advertise a tag the
 * `0x05` answer says is not there. */
static bool nfc_field_present(const nfc_tag_t *nfc)
{
    return nfc->placed && !nfc->lifecycle.ejected;
}

/*
 * §4.9's one reader event (#48), and the counter's only writer: the counter
 * advances from wherever the reader left it, wrapping `0x07 → 0x01` so `0x00`
 * stays reserved for "no tag in the field" — which is also what a tag-removed
 * event leaves on the wire. A tag-presented, scan-ready, operation-ready,
 * write-complete or tag-removed edge calls this exactly once.
 */
static void nfc_reader_event(nfc_tag_t *nfc)
{
    const uint8_t next = (uint8_t)((nfc->counter % NFC_REPORT_COUNTER_MAX) + 1u);
    nfc->counter = next;
    nfc_set_state(nfc, nfc_field_present(nfc) ? next : NFC_STATE_IDLE);
}

/* §6.6: a placement, an unplacement and the gap's close all end whatever read
 * was in flight — the lifecycle is per placement and per scan. */
static void nfc_reset_lifecycle(nfc_tag_t *nfc)
{
    nfc->lifecycle.armed = false;
    nfc->lifecycle.read_complete = false;
    nfc->lifecycle.ejected = false;
}

void nfc_tag_init(nfc_tag_t *nfc)
{
    if (nfc == NULL) {
        return;
    }
    memset(nfc, 0, sizeof(*nfc));
    nfc->state = NFC_STATE_IDLE;
    nfc->polling = NFC_STATE_IDLE;
}

void nfc_tag_set_events(nfc_tag_t *nfc, const nfc_tag_events_t *events)
{
    if (nfc == NULL) {
        return;
    }
    if (events == NULL) {
        nfc_tag_events_t none = {0};
        nfc->events = none;
        return;
    }
    nfc->events = *events;
}

uint8_t nfc_tag_state(const nfc_tag_t *nfc)
{
    return nfc == NULL ? NFC_STATE_IDLE : nfc->state;
}

uint8_t nfc_tag_polling(const nfc_tag_t *nfc)
{
    return nfc == NULL ? NFC_STATE_IDLE : nfc->polling;
}

bool nfc_tag_placed(const nfc_tag_t *nfc)
{
    return nfc != NULL && nfc->placed;
}

/* §6.3: the seven-byte UID out of any tag image — `UID[0..2]` then `UID[3..6]`,
 * skipping the NTAG215 `BCC0` check byte at byte 3 (§6.1). */
static void nfc_uid_from_image(const uint8_t *image, uint8_t out[NFC_TAG_UID_SIZE])
{
    out[0] = image[0];
    out[1] = image[1];
    out[2] = image[2];
    out[3] = image[4];
    out[4] = image[5];
    out[5] = image[6];
    out[6] = image[7];
}

void nfc_tag_identity(const nfc_tag_t *nfc, uint8_t out[NFC_TAG_UID_SIZE])
{
    if (nfc == NULL || out == NULL) {
        return;
    }
    nfc_uid_from_image(nfc->tag, out);
}

void nfc_tag_place(nfc_tag_t *nfc, const uint8_t *tag, size_t len, uint32_t now_ms)
{
    if (nfc == NULL || tag == NULL || len != NFC_TAG_SIZE) {
        return;
    }
    /* §6.5: the tag-absent gap on every tag change, the atomic replace included.
     * If something was answering (or a previous gap was still open), the byte
     * returns to `0x00` — and `0x05`/`0x15` answer "no tag" — until
     * `nfc_tag_tick` promotes the new bytes. A first placement has nothing to
     * remove, so it answers at once. */
    bool had_a_tag = nfc->placed || nfc->staged;
    memcpy(nfc->tag, tag, NFC_TAG_SIZE);
    nfc_clear_read_op(nfc);
    nfc_reset_write(nfc);
    nfc_reset_lifecycle(nfc);
    nfc->repeat.defer_eject = false;
#if NFC_TAG_DEFER_READ_EJECT
    /* §6.6's re-presentation rule: the reference keys it on the identity of the
     * *completed read*, so only a placement that re-presents that UID inside
     * 30 s of the read defers; the first placement of every UID is unaffected. */
    nfc->repeat.now_ms = now_ms;
    {
        uint8_t incoming[NFC_TAG_UID_SIZE];
        nfc_uid_from_image(tag, incoming);
        nfc->repeat.defer_eject =
            nfc->repeat.valid && memcmp(incoming, nfc->repeat.uid, sizeof(incoming)) == 0 &&
            (uint32_t)(now_ms - nfc->repeat.at_ms) <= NFC_TAG_REPRESENT_WINDOW_MS;
    }
#endif

    if (had_a_tag) {
        nfc->placed = false;
        nfc->staged = true;
        nfc->gap_until_ms = now_ms + NFC_TAG_GAP_MS;
        if (nfc->polling == NFC_STATE_TAG_DETECTED) {
            nfc->polling = NFC_STATE_POLLING;
        }
        nfc_reader_event(nfc); /* the old tag leaves the field (§4.9) */
        return;
    }

    nfc->placed = true;
    nfc->staged = false;
    /* The console's level follows a placement: if it was asking, the answer is
     * now "tag detected". It does not create an ask where there was none — the
     * report byte is what prompts the console, and it is separate (§4.9). */
    if (nfc->polling == NFC_STATE_POLLING) {
        nfc->polling = NFC_STATE_TAG_DETECTED;
    }
    nfc_reader_event(nfc); /* tag presented (§4.9) */
}

void nfc_tag_tick(nfc_tag_t *nfc, uint32_t now_ms)
{
    if (nfc == NULL) {
        return;
    }
#if NFC_TAG_DEFER_READ_EJECT
    /* The clock a `0x04` dates its completed read with, kept current here and
     * in `nfc_tag_place` — the only two ticks the module is given. */
    nfc->repeat.now_ms = now_ms;
#endif
    if (!nfc->staged) {
        return;
    }
    if ((int32_t)(now_ms - nfc->gap_until_ms) < 0) {
        return;
    }
    nfc->staged = false;
    nfc->placed = true;
    if (nfc->polling == NFC_STATE_POLLING) {
        nfc->polling = NFC_STATE_TAG_DETECTED;
    }
    nfc_reader_event(nfc); /* tag presented (§4.9) */
}

void nfc_tag_unplace(nfc_tag_t *nfc)
{
    if (nfc == NULL || (!nfc->placed && !nfc->staged)) {
        /* Unplacing nothing is a no-op: a `MACRO` exit runs through here too,
         * and it must not disturb a console that is polling for a tag it has
         * not been given. */
        return;
    }
    nfc->placed = false;
    nfc->staged = false;
    nfc_clear_read_op(nfc);
    nfc_reset_write(nfc);
    nfc_reset_lifecycle(nfc);
    nfc->repeat.defer_eject = false; /* the continuation is per presentation */
    /* The console is still asking; it just has nothing to scan now. */
    if (nfc->polling == NFC_STATE_TAG_DETECTED) {
        nfc->polling = NFC_STATE_POLLING;
    }
    /* The bytes stay in the buffer (§4.3 "tag data retained") and are
     * overwritten by the next placement, which is what discards a `0x14`
     * write-back. */
    nfc_reader_event(nfc); /* the tag leaves the field (§4.9) */
}

/* ------------------------------------------------------------------ 0x05 */

static size_t nfc_reply_status(nfc_tag_t *nfc, uint8_t *out, size_t out_cap)
{
    if (out == NULL || out_cap < NFC_STATUS_RESPONSE_SIZE) {
        return 0;
    }
    memset(out, 0, NFC_STATUS_RESPONSE_SIZE);

    if (!nfc->placed) {
        /* The console asked a get-status, so it is asking; with no tag in the
         * field the answer is "no tag". Whether the *never-placed* answer should
         * be the post-eject `07 41` instead is G-19's open half; this module
         * keeps `0x00`. */
        nfc->polling = NFC_STATE_POLLING;
        out[0] = NFC_STATUS_NO_TAG;
        return NFC_STATUS_RESPONSE_SIZE;
    }

    /* §6.6's post-eject answer: the scan's read completed and `0x04` ended it, so
     * the reader's field is empty. The second implementation answers this as a
     * bare `07 41` with everything else zero (`s2_nfc_codec.cpp:775-792`, context
     * tier), and that is what the console reads back after a whole tag. */
    if (nfc->lifecycle.ejected) {
        out[0] = NFC_STATUS_EJECTED;
        out[1] = NFC_STATUS_EJECTED_DETAIL;
        /* Nothing is in the field, so the console is asking for a tag it does
         * not have — the same level the never-placed read reports. */
        nfc->polling = NFC_STATE_POLLING;
        return NFC_STATUS_RESPONSE_SIZE;
    }

    /* §6.6's lifecycle, in the reference's own precedence: a committed write's
     * `0x05` outranks an armed read, which outranks the tag-in-field `0x09`.
     * The armed `04` is a *level*: every ask of the window carries it, which is
     * the answer the bench measured unlocking the console's first pull. */
    out[0] = nfc->write.committed     ? NFC_STATUS_WRITE_COMMITTED
             : nfc->lifecycle.armed ? NFC_STATUS_READ_ARMED
                                    : NFC_STATUS_TAG_DETECTED;
    /* Offsets 1–7 are the captured flags, whose first byte is the detail byte
     * (always `0x00` outside the post-eject answer). */
    memcpy(&out[1], nfc_status_flags, sizeof(nfc_status_flags));
    out[8] = (uint8_t)NFC_TAG_UID_SIZE;
    nfc_tag_identity(nfc, &out[9]);
    nfc->polling = NFC_STATE_TAG_DETECTED;
    return NFC_STATUS_RESPONSE_SIZE;
}

/* ------------------------------------------------------------- 0x15 / 0x14 */

static size_t nfc_reply_read(nfc_tag_t *nfc, const uint8_t *payload, size_t len, uint8_t *out,
                             size_t out_cap)
{
    if (payload == NULL || len < 2) {
        return 0;
    }
    /* An unplaced tag — or one a completed read has ejected — answers nothing:
     * the console reaches `0x15` only after a `0x05` that named a tag, so this
     * is the gap's other half (§6.5). */
    if (!nfc_field_present(nfc) || out == NULL || out_cap < NFC_TAG_READ_HEAD_SIZE) {
        return 0;
    }
    const uint16_t wire = nfc_rd_le16(payload);
    if (wire >= NFC_TAG_SERVED_SIZE) {
        /* Past the served space: the bare last-chunk marker. Silence is what
         * ended the register-screen sessions at the console's third probe
         * (`0x2c0`), so the answer is a well-formed "nothing more" — a
         * deliberate deviation from the reference, whose own handler refuses
         * the ask (`s2_nfc_codec.cpp:831-846` guards the marker path behind
         * `offset < op_buffer.size()`). It does **not** complete the read: the
         * read's end is the final chunk of the space, and nothing else. */
        out[0] = 0x01;
        out[1] = 0x00;
        out[2] = 0x00;
        return NFC_TAG_READ_HEAD_SIZE;
    }

    size_t chunk = NFC_TAG_SERVED_SIZE - wire;
    if (chunk > NFC_TAG_READ_CHUNK) {
        chunk = NFC_TAG_READ_CHUNK;
    }
    if (out_cap < NFC_TAG_READ_HEAD_SIZE + chunk) {
        return 0;
    }
    /* The chunk that reaches the space's end is the read's completion (§6.6,
     * #48): the console's `0x04` after one is the post-eject answer, and after
     * anything less it is not. */
    if (nfc->lifecycle.armed && wire + chunk >= NFC_TAG_SERVED_SIZE) {
        nfc->lifecycle.read_complete = true;
    }
    /* The head the console reads: `last` u8 · `len` u16 (LE)
     * (`switch2_controller_research/commands.md:68`). */
    out[0] = (wire + chunk >= NFC_TAG_SERVED_SIZE) ? 0x01 : 0x00;
    out[1] = (uint8_t)(chunk & 0xFFu);
    out[2] = (uint8_t)(chunk >> 8);

#if NFC_TAG_READ_PLAIN_VIEW
    /* The comparison compile: the image at wire 0, no head. */
    memcpy(&out[NFC_TAG_READ_HEAD_SIZE], &nfc->tag[wire], chunk);
#else
    /* The capture's space: 60 bytes of framing head, then the image, so a
     * chunk that opens in the head and closes in the image bridges both. */
    if (wire < NFC_TAG_FRAME_SIZE) {
        uint8_t frame[NFC_TAG_FRAME_SIZE];
        size_t from_frame = NFC_TAG_FRAME_SIZE - wire;
        if (from_frame > chunk) {
            from_frame = chunk;
        }
        nfc_build_frame(nfc, frame);
        memcpy(&out[NFC_TAG_READ_HEAD_SIZE], &frame[wire], from_frame);
        if (from_frame < chunk) {
            memcpy(&out[NFC_TAG_READ_HEAD_SIZE + from_frame], nfc->tag, chunk - from_frame);
        }
    } else {
        memcpy(&out[NFC_TAG_READ_HEAD_SIZE], &nfc->tag[wire - NFC_TAG_FRAME_SIZE], chunk);
    }
#endif
    return NFC_TAG_READ_HEAD_SIZE + chunk;
}

/* ------------------------------------------------------------------ 0x08 */

/* §6.6's G-17 frame: `0x14` fills a 454-byte staging stream whose coverage is a
 * bitmap, so a stream with a hole in it can be refused rather than half-applied
 * (`switch2_controller_research/commands.md:67`). */
static bool nfc_stage_write(nfc_tag_t *nfc, const uint8_t *payload, size_t len)
{
    if (!nfc->placed || payload == NULL || len < 4u) {
        /* Discarded, not refused: the write is to a placement that no longer
         * exists (§6.5). */
        return false;
    }
    const uint16_t offset = nfc_rd_le16(payload);
    uint16_t want = nfc_rd_le16(&payload[2]);
    const size_t available = len - 4u;
    if ((size_t)want > available) {
        want = (uint16_t)available; /* clamped, never read past the arrival */
    }
    if (want == 0 || offset >= NFC_TAG_WRITE_STAGING_SIZE) {
        return false;
    }
    size_t n = NFC_TAG_WRITE_STAGING_SIZE - offset;
    if (n > want) {
        n = want;
    }
    memcpy(&nfc->write.stream[offset], &payload[4], n);
    for (size_t i = 0; i < n; i++) {
        const size_t bit = (size_t)offset + i;
        nfc->write.coverage[bit / 8u] |= (uint8_t)(1u << (bit % 8u));
    }
    nfc->write.active = true;
    return true;
}

static bool nfc_coverage_complete(const nfc_tag_t *nfc)
{
    for (size_t i = 0; i + 1u < sizeof(nfc->write.coverage); i++) {
        if (nfc->write.coverage[i] != 0xFFu) {
            return false;
        }
    }
    return (nfc->write.coverage[sizeof(nfc->write.coverage) - 1u] &
            NFC_TAG_WRITE_COVERAGE_TAIL_MASK) == NFC_TAG_WRITE_COVERAGE_TAIL_MASK;
}

/* One parsed record: where it lands in the image and where it sits in the
 * stream. Parsed once and then applied, so validation and the write cannot
 * drift apart — and a refused stream leaves the image byte-identical. */
typedef struct {
    uint16_t address;
    uint16_t source;
    uint8_t length;
} nfc_write_record_t;

static bool nfc_parse_records(const uint8_t *stream, nfc_write_record_t *records,
                              uint8_t *count_out)
{
    const uint8_t count = stream[NFC_TAG_WRITE_RECORD_COUNT_OFFSET];
    if (count == 0u || count > NFC_TAG_WRITE_MAX_RECORDS) {
        return false;
    }
    size_t cursor = NFC_TAG_WRITE_RECORD_COUNT_OFFSET + 1u;
    for (uint8_t i = 0; i < count; i++) {
        if (cursor + 2u > NFC_TAG_WRITE_STAGING_SIZE) {
            return false;
        }
        const uint8_t page = stream[cursor];
        const uint8_t length = stream[cursor + 1u];
        cursor += 2u;
        const size_t address = (size_t)page * 4u;
        if (page == 0u || length == 0u || address < NFC_TAG_WRITE_FIRST_PAGE_OFFSET ||
            address + length > NFC_TAG_SIZE || cursor + length > NFC_TAG_WRITE_STAGING_SIZE) {
            return false;
        }
        records[i].address = (uint16_t)address;
        records[i].source = (uint16_t)cursor;
        records[i].length = length;
        cursor += length;
    }
    for (size_t i = cursor; i < NFC_TAG_WRITE_STAGING_SIZE; i++) {
        if (stream[i] != 0u) {
            return false; /* the padding after the records is part of the frame */
        }
    }
    *count_out = count;
    return true;
}

/* The commit, and the only writer of a placed image after placement. */
static bool nfc_commit_write(nfc_tag_t *nfc)
{
    if (!nfc->placed || !nfc->write.active || !nfc_coverage_complete(nfc)) {
        return false;
    }
    const uint8_t *stream = nfc->write.stream;
    if (stream[0] != 0xD0u || stream[1] != 0x07u) {
        return false;
    }
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(nfc, uid);
    if (memcmp(&stream[2], uid, NFC_TAG_UID_SIZE) != 0) {
        return false;
    }
    nfc_write_record_t records[NFC_TAG_WRITE_MAX_RECORDS];
    uint8_t count = 0;
    if (!nfc_parse_records(stream, records, &count)) {
        return false;
    }

    /* Accepted: the frame's own header word lands first, then each record at its
     * page — so a record aimed at page 4 (the page the header word writes) wins,
     * which is the order the reference implementation applies them in. */
    memcpy(&nfc->tag[NFC_TAG_WRITE_HEADER_TARGET], &stream[NFC_TAG_WRITE_HEADER_OFFSET], 4u);
    for (uint8_t i = 0; i < count; i++) {
        memcpy(&nfc->tag[records[i].address], &stream[records[i].source], records[i].length);
    }
    nfc->write.committed = true;
    nfc->write.active = false;
    nfc->lifecycle.armed = false;
    nfc->lifecycle.read_complete = false;
    /* The write is the read's continuation's endpoint: a later presentation is
     * a fresh read, not another deferral (the reference clears its recent-read
     * record here too). */
#if NFC_TAG_DEFER_READ_EJECT
    nfc->repeat.valid = false;
#endif
    nfc->repeat.defer_eject = false;
    nfc_reader_event(nfc); /* write complete (§4.9) */
    return true;
}

/* -------------------------------------------------------------- the command */

size_t nfc_tag_command(nfc_tag_t *nfc, uint8_t subcmd, const uint8_t *payload, size_t len,
                       uint8_t *out, size_t out_cap)
{
    if (nfc == NULL) {
        return 0;
    }
    switch (subcmd) {
    case NFC_CMD_START_POLLING: {
        /* §3.2: the console is asking. A tag already in the field is the answer;
         * with none (including during a gap) the level is `polling`. A poll in
         * `IDLE`/`MACRO` moves no report byte; a poll over a placed tag is the
         * reader's scan-ready event and advances the counter (§4.9, #48). */
        /* A staged stream does not survive a new scan: a re-arm is a new
         * transaction (§6.6's G-17 flow), and the head's echoed request is
         * per scan. A committed write keeps its status until the scan ends —
         * that is what the console reads back before it stops polling. */
        nfc_clear_read_op(nfc);
        if (nfc->write.active && !nfc->write.committed) {
            nfc_reset_write(nfc);
        }
        /* The read lifecycle is per scan: a re-arm ends the armed read and its
         * completion, which is what puts the status answer back to `09`. The
         * post-eject state is *not* cleared here — the reader's field stays
         * empty until a new placement (§6.6, #48). */
        nfc->lifecycle.armed = false;
        nfc->lifecycle.read_complete = false;
        nfc->polling = nfc_field_present(nfc) ? NFC_STATE_TAG_DETECTED : NFC_STATE_POLLING;
        if (nfc_field_present(nfc)) {
            nfc_reader_event(nfc);
        }
        return 0;
    }
    case NFC_CMD_STOP_POLLING: {
        /* §3.3/§6.5: the console stopped asking. That is `SCAN_ENDED`, and it is
         * what the container rotates on — but only for a tag that was in the
         * field to be scanned. */
        bool was_active = nfc->polling != NFC_STATE_IDLE;
        bool had_tag = nfc->placed;
        nfc->polling = NFC_STATE_IDLE;
        nfc->lifecycle.armed = false;
        /* §6.6 (#48): a stop that ends a *completed* read leaves the reader's
         * field empty — the `0x05` answer is `07 41` from here until a new
         * placement. A stop after an incomplete read is not that: it is the
         * tag-in-field `09`, which is where the bench's no-crash cycles ended.
         *
         * The completed read is also what the re-presentation rule keys on (the
         * reference records the read UID here, not at placement), so a tag that
         * was never read can never arm the defer. */
        if (nfc->lifecycle.read_complete) {
#if NFC_TAG_DEFER_READ_EJECT
            if (!nfc->write.committed) {
                nfc_tag_identity(nfc, nfc->repeat.uid);
                nfc->repeat.at_ms = nfc->repeat.now_ms;
                nfc->repeat.valid = true;
            }
#endif
            if (!nfc->repeat.defer_eject) {
                nfc->lifecycle.ejected = true;
                nfc_reader_event(nfc);
            }
        }
        /* A *deferred* stop (the same identity re-presented, §6.6) leaves the
         * tag in the field: the status stays `09` and the console's write
         * continuation (`0x14`/`0x08`) finds a tag to write. */
        nfc->lifecycle.read_complete = false;
        /* The write-back dies with the scan's status; its bytes stay in the
         * volatile tag until the placement does. */
        nfc_reset_write(nfc);
        if (was_active && had_tag && nfc->events.scan_ended != NULL) {
            nfc->events.scan_ended(nfc->events.ctx);
        }
        return 0;
    }
    case NFC_CMD_GET_STATUS:
        return nfc_reply_status(nfc, out, out_cap);
    case NFC_CMD_READ_DEVICE: {
        /* The captured exchange is an ACK with no payload
         * (`switch2_controller_research/commands.md:64`), and the request's own
         * nine bytes are remembered: the framing head echoes `payload[10..18]`
         * at its `[51..59]` (§6.6). Only the canonical shape is remembered — a
         * malformed ask must not ride into the next head. */
        const bool canonical =
            payload != NULL && len >= NFC_TAG_READ_OP_OFFSET + NFC_TAG_READ_OP_SIZE &&
            payload[0] == 0xD0u && payload[1] == 0x07u;
        memset(nfc->read_req_echo, 0, sizeof(nfc->read_req_echo));
        if (canonical) {
            memcpy(nfc->read_req_echo, &payload[NFC_TAG_READ_OP_OFFSET], NFC_TAG_READ_OP_SIZE);
        }
        /* §6.6 (#48): this is the read command, and it arms the status
         * lifecycle — `0x05` answers `04` for the whole window. The reference
         * accepts only that canonical request shape (19 bytes at the console's
         * own length), and only while something is in the field; a post-eject
         * reader has no tag to arm (`s2_nfc_codec.cpp:795-830`, context tier). */
        if (canonical && nfc_field_present(nfc)) {
            nfc->lifecycle.armed = true;
            nfc->lifecycle.read_complete = false;
            nfc_reader_event(nfc); /* operation ready (§4.9) */
        }
        return 0;
    }
    case NFC_CMD_COMMIT_WRITE:
        nfc_commit_write(nfc);
        return 0;
    case NFC_CMD_WRITE_BUFFER:
        nfc_stage_write(nfc, payload, len);
        return 0;
    case NFC_CMD_READ_BUFFER:
        return nfc_reply_read(nfc, payload, len, out, out_cap);
    default:
        return 0;
    }
}
