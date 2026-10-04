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

#if NFC_TAG_BUFFER_P1_PREFIX != 0
/* The served space's 60-byte framing head: the NS1 P1 packet's prefix minus
 * its UID field (15 header + 4 zeros + 32 magic + 9 echoed page-ranges),
 * byte-for-byte from `elmagnificogi_nsre`'s real capture. The magic blob is
 * the constant both the real controller and Poohl's emulation carry; the
 * ranges echo the canonical full-tag read the console's `0x06` asks for.
 *
 * **Superseded candidate (spec §6.6):** the 60-byte *space* is settled —
 * `image = wire - 0x3C` — but this P1-derived *content* is the variant to
 * replace: a genuine PC2 read buffer of the same length exists
 * (`ns_pc_control/server/src/s2_nfc_codec.cpp:158-199`, context tier) and is
 * the first candidate to test. Both put the 32-byte blob at 19 and the 9 echoed
 * bytes at 51; they differ in their first 19. */
static const uint8_t nfc_p1_prefix[NFC_TAG_P1_PREFIX_SIZE] = {
    0x3a, 0x00, 0x07, 0x01, 0x00, 0x01, 0x31, 0x02, 0x00, 0x00,
    0x00, 0x01, 0x02, 0x00, 0x07,
    0x00, 0x00, 0x00, 0x00,
    0x7d, 0xfd, 0xf0, 0x79, 0x36, 0x51, 0xab, 0xd7, 0x46, 0x6e,
    0x39, 0xc1, 0x91, 0xba, 0xbe, 0xb8, 0x56, 0xce, 0xed, 0xf1,
    0xce, 0x44, 0xcc, 0x75, 0xea, 0xfb, 0x27, 0x09, 0x4d, 0x08,
    0x7a, 0xe8,
    0x03, 0x00, 0x3b, 0x3c, 0x77, 0x78, 0x86, 0x00, 0x00,
};
#endif

static uint16_t nfc_rd_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

/* The `0x15` response's three-byte head: a leading `0x00` then the *wire*
 * offset echoed little-endian. **Superseded shape (spec §6.6, G-19):** the
 * capture's head is `last` u8 · `len` u16 (LE)
 * (`switch2_controller_research/commands.md:68`); this echo is the older reading
 * of the same three bytes, kept — shared by the pad path and the slice path —
 * until G-19's fix lands. */
static void nfc_reply_echo_wire(uint8_t *out, uint16_t wire)
{
    out[0] = 0x00;
    out[1] = (uint8_t)(wire & 0xFFu);
    out[2] = (uint8_t)(wire >> 8);
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

#if NFC_TAG_STATUS_DONE_WHEN_READ
void nfc_tag_set_read_done(nfc_tag_t *nfc, bool done)
{
    if (nfc != NULL) {
        nfc->read_done = done;
    }
}
#endif

void nfc_tag_identity(const nfc_tag_t *nfc, uint8_t out[NFC_TAG_UID_SIZE])
{
    if (nfc == NULL || out == NULL) {
        return;
    }
    /* §6.3: the seven-byte UID is `UID[0..2]` then `UID[3..6]`; the byte between
     * them is the NTAG215 `BCC0` check byte and is not part of the UID (§6.1). */
    out[0] = nfc->tag[0];
    out[1] = nfc->tag[1];
    out[2] = nfc->tag[2];
    out[3] = nfc->tag[4];
    out[4] = nfc->tag[5];
    out[5] = nfc->tag[6];
    out[6] = nfc->tag[7];
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

    if (had_a_tag) {
        nfc->placed = false;
        nfc->staged = true;
        nfc->gap_until_ms = now_ms + NFC_TAG_GAP_MS;
        if (nfc->polling == NFC_STATE_TAG_DETECTED) {
            nfc->polling = NFC_STATE_POLLING;
        }
        nfc_set_state(nfc, NFC_STATE_IDLE);
        return;
    }

    nfc->placed = true;
    nfc->staged = false;
#if NFC_TAG_STATUS_DONE_WHEN_READ
    nfc->read_done = false;
#endif
    /* The console's level follows a placement: if it was asking, the answer is
     * now "tag detected". It does not create an ask where there was none — the
     * report byte is what prompts the console, and it is separate (§4.9). */
    if (nfc->polling == NFC_STATE_POLLING) {
        nfc->polling = NFC_STATE_TAG_DETECTED;
    }
    nfc_set_state(nfc, NFC_STATE_TAG_DETECTED);
}

void nfc_tag_tick(nfc_tag_t *nfc, uint32_t now_ms)
{
    if (nfc == NULL || !nfc->staged) {
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
    nfc_set_state(nfc, NFC_STATE_TAG_DETECTED);
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
#if NFC_TAG_STATUS_DONE_WHEN_READ
    nfc->read_done = false;
#endif
    /* The console is still asking; it just has nothing to scan now. */
    if (nfc->polling == NFC_STATE_TAG_DETECTED) {
        nfc->polling = NFC_STATE_POLLING;
    }
    /* The bytes stay in the buffer (§4.3 "tag data retained") and are
     * overwritten by the next placement, which is what discards a `0x14`
     * write-back. */
    nfc_set_state(nfc, NFC_STATE_IDLE);
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
         * field the answer is "polling". */
        nfc->polling = NFC_STATE_POLLING;
        out[0] = NFC_STATUS_NO_TAG;
        return NFC_STATUS_RESPONSE_SIZE;
    }

    out[0] = NFC_STATUS_TAG_DETECTED;
#if NFC_TAG_STATUS_DONE_WHEN_READ
    /* The bench's answer lifecycle (see `nfc_tag.h`): once the armed read's
     * data has been served, the status the console is polling for flips to
     * the read-done state — the NS1 P3 trailer, answered rather than pushed. */
    if (nfc->read_done) {
        out[0] = (uint8_t)NFC_TAG_READ_DONE_STATE;
#if NFC_TAG_STATUS_DONE_ONCE
        /* #45: the same signal as an edge — the reference sends its `04` once
         * per read and its status answers never rest there, so the state is
         * consumed by the ask that carried it. */
        nfc->read_done = false;
#endif
    }
#endif
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
    /* An unplaced tag answers nothing: the console reaches `0x15` only after a
     * `0x05` that named a tag, so this is the gap's other half (§6.5). */
    if (!nfc->placed) {
        return 0;
    }
    uint16_t wire = nfc_rd_le16(payload);
#if NFC_TAG_READ_WIRE_BASE != 0
    /* §6.6's mapping, no longer a hypothesis: the served space is
     * `[60 B framing][540 B image]`, so a wire base of 0x3C answers wire 0x46
     * with image 0x0A and the capture's `46 00` is that same 70. The knob stays
     * so a bench build can still serve plain offsets until G-19 flips the
     * default. */
    if (wire < NFC_TAG_READ_WIRE_BASE) {
        return 0;
    }
    uint16_t offset = (uint16_t)(wire - NFC_TAG_READ_WIRE_BASE);
#else
    uint16_t offset = wire;
#endif
    if (offset >= NFC_TAG_SERVED_SIZE) {
#if NFC_TAG_READ_PAD_TO != 0
        /* The bench pad (§6.6's open offset space, `nfc_tag.h`): the console
         * samples past the image, and an empty answer aborts its cycle — so
         * in-space asks beyond the image get zero-filled chunks instead. */
        if (offset >= NFC_TAG_READ_PAD_TO) {
            return 0;
        }
        size_t chunk = NFC_TAG_READ_PAD_TO - offset;
        if (chunk > NFC_TAG_READ_CHUNK) {
            chunk = NFC_TAG_READ_CHUNK;
        }
        if (out == NULL || out_cap < 3u + chunk) {
            return 0;
        }
        nfc_reply_echo_wire(out, wire);
        memset(&out[3], 0, chunk);
        return 3u + chunk;
#else
        return 0;
#endif
    }
    size_t chunk = NFC_TAG_SERVED_SIZE - offset;
    if (chunk > NFC_TAG_READ_CHUNK) {
        chunk = NFC_TAG_READ_CHUNK;
    }
    if (out == NULL || out_cap < 3u + chunk) {
        return 0;
    }
    /* Superseded head (G-19): the capture's little-endian pair here is the
     * *length* served, not the offset
     * (`switch2_controller_research/commands.md:68`). */
    nfc_reply_echo_wire(out, wire);
#if NFC_TAG_BUFFER_P1_PREFIX != 0
    /* The view: 60 bytes of P1 framing, then the image — a chunk may straddle
     * the seam, so both halves are copied explicitly. */
    {
        size_t from_prefix = 0u;
        if (offset < NFC_TAG_P1_PREFIX_SIZE) {
            from_prefix = NFC_TAG_P1_PREFIX_SIZE - offset;
            if (from_prefix > chunk) {
                from_prefix = chunk;
            }
            memcpy(&out[3], &nfc_p1_prefix[offset], from_prefix);
        }
        if (from_prefix < chunk) {
            memcpy(&out[3 + from_prefix],
                   &nfc->tag[offset + from_prefix - NFC_TAG_P1_PREFIX_SIZE],
                   chunk - from_prefix);
        }
    }
#else
    memcpy(&out[3], &nfc->tag[offset], chunk);
#endif
    return 3u + chunk;
}

static void nfc_write_buffer(nfc_tag_t *nfc, const uint8_t *payload, size_t len)
{
    if (!nfc->placed) {
        /* Discarded, not refused: the write is to a placement that no longer
         * exists (§6.5). */
        return;
    }
    if (payload == NULL || len < 4) {
        return;
    }
    uint16_t offset = nfc_rd_le16(payload);
    uint16_t want = nfc_rd_le16(&payload[2]);
    size_t available = len - 4u;
    if ((size_t)want > available) {
        want = (uint16_t)available;
    }
    if (offset >= NFC_TAG_SIZE) {
        return;
    }
    size_t n = NFC_TAG_SIZE - offset;
    if (n > want) {
        n = want;
    }
    /* §6.5: taken into the *volatile* tag while it is placed, so the placement is
     * not lied to about what it wrote; dropped on unplace because the next
     * placement is virgin by design. */
    memcpy(&nfc->tag[offset], &payload[4], n);
}

/* -------------------------------------------------------------- the command */

size_t nfc_tag_command(nfc_tag_t *nfc, uint8_t subcmd, const uint8_t *payload, size_t len,
                       uint8_t *out, size_t out_cap)
{
    if (nfc == NULL) {
        return 0;
    }
    switch (subcmd) {
    case NFC_CMD_START_POLLING:
        /* §3.2: the console is asking. A tag already in the field is the answer;
         * with none (including during a gap) the level is `polling`. The report
         * byte is untouched — a poll in `IDLE`/`MACRO` must not look like
         * `AMIIBO` (§4.9). */
#if NFC_TAG_STATUS_DONE_WHEN_READ
        nfc->read_done = false;
#endif
        nfc->polling = nfc->placed ? NFC_STATE_TAG_DETECTED : NFC_STATE_POLLING;
        return 0;
    case NFC_CMD_STOP_POLLING: {
        /* §3.3/§6.5: the console stopped asking. That is `SCAN_ENDED`, and it is
         * what the container rotates on — but only for a tag that was in the
         * field to be scanned. */
        bool was_active = nfc->polling != NFC_STATE_IDLE;
        bool had_tag = nfc->placed;
        nfc->polling = NFC_STATE_IDLE;
        if (was_active && had_tag && nfc->events.scan_ended != NULL) {
            nfc->events.scan_ended(nfc->events.ctx);
        }
        return 0;
    }
    case NFC_CMD_GET_STATUS:
        return nfc_reply_status(nfc, out, out_cap);
    case NFC_CMD_READ_DEVICE:
        /* The captured exchange is an ACK with no payload
         * (`switch2_controller_research/commands.md:64`). */
        return 0;
    case NFC_CMD_WRITE_BUFFER:
        nfc_write_buffer(nfc, payload, len);
        return 0;
    case NFC_CMD_READ_BUFFER:
        return nfc_reply_read(nfc, payload, len, out, out_cap);
    default:
        return 0;
    }
}
