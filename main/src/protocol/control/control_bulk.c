/*
 * The §2.7 bulk staging machine: announce / chunk / commit for `LOAD_PLAN` and
 * `PLACE_AMIIBO`, the windowed ACK, and the atomic commit. Portable C; the host
 * test links this file directly (issue #22).
 *
 * Two properties this file exists to guarantee:
 *
 *  - **Commit is atomic.** Bytes become the active plan or placed tag only when
 *    the final offset and the whole-transfer check pass, so `STATUS` never
 *    advertises a transfer that has not landed and a failed one changes nothing.
 *  - **A truncated-but-CRC-valid plan is refused, not replayed.** The §5.3
 *    structural check runs at commit on the staged bytes, and the frame CRC
 *    cannot stand in for it — a CRC-valid prefix of a plan is still a prefix.
 */

#include "protocol/control/control_bulk.h"

#include <string.h>

#include "protocol/plan.h"

static uint16_t rd_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t rd_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void wr_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

/* ----------------------------------------------------------------- plan check */

uint8_t control_plan_check(const uint8_t *bytes, size_t len, size_t capacity,
                           uint16_t *record_count_out)
{
    if (record_count_out != NULL) {
        *record_count_out = 0;
    }
    if (bytes == NULL) {
        return CONTROL_ERR_BAD_PLAN;
    }
    if (len > capacity || len > CONTROL_PLAN_CAPACITY_BYTES) {
        return CONTROL_ERR_PLAN_TOO_LARGE;
    }
    if (len < PLAN_HEADER_SIZE) {
        return CONTROL_ERR_BAD_PLAN;
    }
    if (rd_le32(bytes) != PLAN_MAGIC) {
        return CONTROL_ERR_BAD_PLAN;
    }
    if (bytes[4] != PLAN_FORMAT_VERSION) {
        return CONTROL_ERR_BAD_PLAN;
    }
    if (bytes[5] != PLAN_RECORD_SIZE) {
        return CONTROL_ERR_BAD_PLAN;
    }
    uint16_t records = rd_le16(&bytes[6]);
    if ((uint32_t)len != PLAN_PAYLOAD_SIZE(records)) {
        return CONTROL_ERR_BAD_PLAN;
    }
    if (record_count_out != NULL) {
        *record_count_out = records;
    }
    return CONTROL_ERR_NONE;
}

/* ------------------------------------------------------------------ staging */

static void stage_reset(control_state_t *st)
{
    st->stage = CONTROL_STAGE_NONE;
    st->stage_total = 0;
    st->stage_next = 0;
    st->stage_acked = 0;
    st->stage_has_hash = false;
    memset(st->stage_hash, 0, sizeof(st->stage_hash));
}

/*
 * §2.7 rule 6 and `plan_slots = 1`: an announce reports `plan=none` until the
 * commit lands, and the one plan buffer is the one being staged over — so the
 * previous plan is superseded here, not at the commit.
 */
static void plan_supersede(control_state_t *st)
{
    st->plan_committed = false;
    st->plan = NULL;
    st->plan_len = 0;
    st->plan_records = 0;
    memset(st->plan_hash, 0, sizeof(st->plan_hash));
    st->status.plan_state = CONTROL_PLAN_NONE;
    memset(st->status.plan_hash, 0, sizeof(st->status.plan_hash));
    st->status.plan_frame_count = 0;
}

/* The §6.1 Identity is the seven-byte NFC UID. In the §6.3 tag image it is
 * `UID[0..2]` at bytes 0-2, then `BCC0` at byte 3, then `UID[3..6]` at bytes
 * 4-7 — so the check byte is skipped, not included. */
static void tag_identity(const uint8_t tag[CONTROL_TAG_SIZE], uint8_t out[7])
{
    out[0] = tag[0];
    out[1] = tag[1];
    out[2] = tag[2];
    out[3] = tag[4];
    out[4] = tag[5];
    out[5] = tag[6];
    out[6] = tag[7];
}

static size_t bulk_ack(control_state_t *st, uint8_t verb, uint32_t offset, uint8_t *out,
                       size_t out_cap)
{
    uint8_t payload[4];
    wr_le32(payload, offset);
    return control_ack(st, verb, payload, sizeof(payload), out, out_cap);
}

/* --------------------------------------------------------------- frame arms */

static size_t handle_announce(control_state_t *st, const control_frame_t *frame,
                              const uint8_t *payload, uint8_t *out, size_t out_cap)
{
    bool is_plan = frame->verb == CONTROL_VERB_LOAD_PLAN;

    /* §2.7's frame table fixes `len`; anything else is a shape this verb does
     * not define. */
    size_t expected = is_plan ? 21u : 5u;
    if (frame->len != expected) {
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }

    uint32_t total = rd_le32(&payload[1]);

    if (is_plan) {
        /* ADR-0007: a plan is Loaded in IDLE only. */
        if (st->status.mode != CONTROL_MODE_IDLE) {
            return control_reject(st, CONTROL_ERR_BAD_STATE, st->status.mode, out, out_cap);
        }
        size_t capacity = st->plan_stage_cap;
        if (capacity > CONTROL_PLAN_CAPACITY_BYTES) {
            capacity = CONTROL_PLAN_CAPACITY_BYTES;
        }
        if (total > capacity) {
            return control_reject(st, CONTROL_ERR_PLAN_TOO_LARGE, total, out, out_cap);
        }
        plan_supersede(st);
    } else {
        /* ADR-0007: a tag is Placed in IDLE or AMIIBO; never mid-macro. The
         * tag's size is fixed, so a different `total_len` is a length error
         * (there is no tag equivalent of BAD_PLAN: the device is a byte-sink and
         * runs no structural check, §6.6). */
        if (st->status.mode == CONTROL_MODE_MACRO) {
            return control_reject(st, CONTROL_ERR_BAD_STATE, st->status.mode, out, out_cap);
        }
        if (total != CONTROL_TAG_SIZE) {
            return control_reject(st, CONTROL_ERR_BAD_LENGTH, total, out, out_cap);
        }
    }

    st->stage = is_plan ? CONTROL_STAGE_PLAN : CONTROL_STAGE_TAG;
    st->stage_total = total;
    st->stage_next = 0;
    st->stage_acked = 0;
    st->stage_has_hash = is_plan;
    if (is_plan) {
        memcpy(st->stage_hash, &payload[5], sizeof(st->stage_hash));
    }

    /* "Zero after an announce means start at zero" (§2.7). */
    return bulk_ack(st, frame->verb, 0, out, out_cap);
}

static size_t handle_chunk(control_state_t *st, const control_frame_t *frame, const uint8_t *payload,
                           uint8_t *out, size_t out_cap)
{
    control_stage_t want =
        (frame->verb == CONTROL_VERB_LOAD_PLAN) ? CONTROL_STAGE_PLAN : CONTROL_STAGE_TAG;
    if (st->stage != want) {
        return control_reject(st, CONTROL_ERR_BAD_STATE, st->status.mode, out, out_cap);
    }

    /* §2.7: `5 + n`, n <= chunk_size. */
    if (frame->len < 5u || (size_t)(frame->len - 5u) > CONTROL_CHUNK_SIZE) {
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }

    uint32_t offset = rd_le32(&payload[1]);
    size_t n = (size_t)frame->len - 5u;
    const uint8_t *chunk = &payload[5];

    /*
     * Resume is free and offset-keyed: the container resends from the ACK'd
     * offset, so a duplicate, a rewind or a gap is answered with the offset the
     * device actually wants next and writes nothing (§2.7 rules 4-5).
     */
    if (offset != st->stage_next) {
        return bulk_ack(st, frame->verb, st->stage_next, out, out_cap);
    }
    if ((uint64_t)offset + n > st->stage_total) {
        /* The chunk overruns the announced transfer: a payload length this verb
         * cannot place. */
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }

    uint8_t *dst = (st->stage == CONTROL_STAGE_PLAN) ? st->plan_stage : st->tag_stage;
    if (n > 0) {
        memcpy(&dst[offset], chunk, n);
    }
    st->stage_next += (uint32_t)n;
    control_clear_error(st);

    /* ACK at the window boundary, or as soon as the transfer is complete. The
     * device may also ACK earlier under ring pressure (§2.7); the window is the
     * deterministic policy the host asserts. */
    if (st->stage_next == st->stage_total ||
        st->stage_next - st->stage_acked >= CONTROL_BULK_ACK_WINDOW) {
        st->stage_acked = st->stage_next;
        return bulk_ack(st, frame->verb, st->stage_next, out, out_cap);
    }
    return 0;
}

static size_t handle_commit(control_state_t *st, const control_frame_t *frame, const uint8_t *payload,
                            uint8_t *out, size_t out_cap)
{
    bool is_plan = frame->verb == CONTROL_VERB_LOAD_PLAN;

    control_stage_t want = is_plan ? CONTROL_STAGE_PLAN : CONTROL_STAGE_TAG;
    if (st->stage != want) {
        return control_reject(st, CONTROL_ERR_BAD_STATE, st->status.mode, out, out_cap);
    }

    size_t expected = is_plan ? 21u : 5u;
    if (frame->len != expected) {
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }

    uint32_t total = rd_le32(&payload[1]);
    uint32_t done = st->stage_total;

    if (is_plan) {
        /* Re-check the mode: a panic stop between announce and commit must not
         * turn into an implicit mode change here (ADR-0007). */
        if (st->status.mode != CONTROL_MODE_IDLE) {
            stage_reset(st);
            return control_reject(st, CONTROL_ERR_BAD_STATE, st->status.mode, out, out_cap);
        }
        /* The whole-transfer check: the final offset, the re-sent total, the
         * re-sent hash, and then the §5.3 structural check on the bytes. */
        if (st->stage_next != st->stage_total || total != st->stage_total ||
            memcmp(&payload[5], st->stage_hash, sizeof(st->stage_hash)) != 0) {
            stage_reset(st);
            return control_reject(st, CONTROL_ERR_BAD_PLAN, 0, out, out_cap);
        }
        uint16_t records = 0;
        uint8_t code = control_plan_check(st->plan_stage, st->stage_total, st->plan_stage_cap,
                                          &records);
        if (code != CONTROL_ERR_NONE) {
            stage_reset(st);
            uint32_t detail = (code == CONTROL_ERR_PLAN_TOO_LARGE) ? done : 0;
            return control_reject(st, code, detail, out, out_cap);
        }

        /* Atomic: the staged bytes become the active plan with no copy (§7.4). */
        st->plan = st->plan_stage;
        st->plan_len = st->stage_total;
        st->plan_records = records;
        st->plan_committed = true;
        memcpy(st->plan_hash, st->stage_hash, sizeof(st->plan_hash));
        st->status.plan_state = CONTROL_PLAN_COMMITTED;
        memcpy(st->status.plan_hash, st->stage_hash, sizeof(st->status.plan_hash));
        st->status.plan_frame_count = records;
    } else {
        if (st->status.mode == CONTROL_MODE_MACRO) {
            stage_reset(st);
            return control_reject(st, CONTROL_ERR_BAD_STATE, st->status.mode, out, out_cap);
        }
        if (st->stage_next != st->stage_total || total != st->stage_total ||
            st->stage_total != CONTROL_TAG_SIZE) {
            stage_reset(st);
            return control_reject(st, CONTROL_ERR_BAD_LENGTH, done, out, out_cap);
        }

        /* The 540-byte copy is what makes the replace atomic (§4.3): the old tag
         * answers until this instant, then `place_tag` owns the §6.5 gap. */
        memcpy(st->tag, st->tag_stage, CONTROL_TAG_SIZE);
        st->tag_placed = true;
        tag_identity(st->tag, st->tag_identity);
        st->status.tag_state = CONTROL_TAG_PLACED;
        memcpy(st->status.tag_identity, st->tag_identity, sizeof(st->status.tag_identity));
        st->status.mode = CONTROL_MODE_AMIIBO;
        control_clear_error(st);
        if (st->fx.place_tag != NULL) {
            st->fx.place_tag(st->fx.ctx, st->tag, CONTROL_TAG_SIZE);
        }
    }

    stage_reset(st);
    return bulk_ack(st, frame->verb, done, out, out_cap);
}

size_t control_bulk_request(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                            size_t out_cap)
{
    if (st == NULL || frame == NULL || frame->payload == NULL) {
        return 0;
    }
    if (frame->len < 1) {
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }

    const uint8_t *payload = frame->payload;
    switch (payload[0]) {
    case CONTROL_BULK_OP_ANNOUNCE:
        return handle_announce(st, frame, payload, out, out_cap);
    case CONTROL_BULK_OP_CHUNK:
        return handle_chunk(st, frame, payload, out, out_cap);
    case CONTROL_BULK_OP_COMMIT:
        return handle_commit(st, frame, payload, out, out_cap);
    default:
        /* `op` is reserved-or-unknown: the payload is not a shape this verb
         * defines. There is no code for a bad discriminator in the closed §2.5
         * set, so the frame's own length is the detail — surfaced in the ticket,
         * not invented as a new code here. */
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }
}
