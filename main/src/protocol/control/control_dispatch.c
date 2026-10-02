/*
 * CONTROL dispatch: HELLO (tolerant) and STATUS (strict), the canonical ERROR
 * reply, and the HELLO/STATUS payload serialisers. Portable C (spec §2.5, §2.6,
 * §2.8, §3.2).
 */

#include "protocol/control/control_protocol.h"

#include <string.h>

static void wr_le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
}

static void wr_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

/* ------------------------------------------------------------------ payloads */

void control_hello_default(control_hello_t *hello, uint32_t boot_id)
{
    if (hello == NULL) {
        return;
    }
    memset(hello, 0, sizeof(*hello));
    hello->proto_ver = CONTROL_PROTO_VER;
    hello->fw_version[0] = CONTROL_FW_MAJOR;
    hello->fw_version[1] = CONTROL_FW_MINOR;
    hello->fw_version[2] = CONTROL_FW_PATCH;
    hello->fw_version[3] = CONTROL_FW_BUILD;
    hello->boot_id = boot_id;
    hello->max_frame = CONTROL_MAX_FRAME;
    hello->chunk_size = CONTROL_CHUNK_SIZE;
    hello->plan_capacity_bytes = CONTROL_PLAN_CAPACITY_BYTES;
    hello->plan_slots = CONTROL_PLAN_SLOTS;
    hello->features = CONTROL_FEATURES;
}

void control_status_default(control_status_t *status)
{
    if (status == NULL) {
        return;
    }
    /* §4.8: boot state is IDLE with nothing staged and nothing placed. */
    memset(status, 0, sizeof(*status));
    status->mode = CONTROL_MODE_IDLE;
    status->plan_state = CONTROL_PLAN_NONE;
    status->tag_state = CONTROL_TAG_NONE;
    status->console_polling = CONTROL_POLLING_IDLE;
    status->last_error_code = CONTROL_ERR_NONE;
    status->last_stop_reason = CONTROL_STOP_NONE;
}

size_t control_hello_payload(const control_hello_t *hello, uint8_t *out, size_t cap)
{
    if (hello == NULL || out == NULL || cap < sizeof(control_hello_t)) {
        return 0;
    }
    out[0] = hello->proto_ver;
    memcpy(&out[1], hello->fw_version, 4);
    wr_le32(&out[5], hello->boot_id);
    wr_le16(&out[9], hello->max_frame);
    wr_le16(&out[11], hello->chunk_size);
    wr_le32(&out[13], hello->plan_capacity_bytes);
    out[17] = hello->plan_slots;
    wr_le16(&out[18], hello->features);
    return 20;
}

size_t control_status_payload(const control_status_t *status, uint8_t *out, size_t cap)
{
    if (status == NULL || out == NULL || cap < sizeof(control_status_t)) {
        return 0;
    }
    out[0] = status->console_link;
    out[1] = status->bond;
    out[2] = status->mode;
    out[3] = status->plan_state;
    memcpy(&out[4], status->plan_hash, 16);
    wr_le16(&out[20], status->plan_frame_count);
    wr_le16(&out[22], status->current_frame);
    wr_le32(&out[24], status->loop_count);
    out[28] = status->tag_state;
    memcpy(&out[29], status->tag_identity, 7);
    out[36] = status->console_polling;
    out[37] = status->last_error_code;
    wr_le32(&out[38], status->last_error_detail);
    out[42] = status->last_stop_reason;
    wr_le32(&out[43], status->uptime_ms);
    return 47;
}

/* ------------------------------------------------------------------ dispatch */

static size_t reply_hello(const control_frame_t *frame, const control_hello_t *hello,
                          uint8_t *out, size_t out_cap)
{
    /* §2.8: tolerant, and exactly this much. len = 0 is a version probe;
     * len >= 1 compares the first byte and ignores the rest. */
    if (frame->len >= 1 && frame->payload != NULL && frame->payload[0] != hello->proto_ver) {
        return control_encode_error(CONTROL_ERR_VER_MISMATCH, hello->proto_ver, out, out_cap);
    }
    uint8_t payload[sizeof(control_hello_t)];
    size_t n = control_hello_payload(hello, payload, sizeof(payload));
    if (n == 0) {
        return 0;
    }
    return control_encode(CONTROL_TYPE_REPLY, CONTROL_VERB_HELLO, payload, n, out, out_cap);
}

static size_t reply_status(const control_frame_t *frame, const control_status_t *status,
                           uint8_t *out, size_t out_cap)
{
    /* Strict: no payload, exactly (§2.4). */
    if (frame->len != 0) {
        return control_encode_error(CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }
    uint8_t payload[sizeof(control_status_t)];
    size_t n = control_status_payload(status, payload, sizeof(payload));
    if (n == 0) {
        return 0;
    }
    return control_encode(CONTROL_TYPE_REPLY, CONTROL_VERB_STATUS, payload, n, out, out_cap);
}

size_t control_handle_request(const control_frame_t *frame, const control_hello_t *hello,
                              const control_status_t *status, uint8_t *out, size_t out_cap)
{
    if (frame == NULL || hello == NULL || status == NULL || out == NULL) {
        return 0;
    }

    switch (frame->verb) {
    case CONTROL_VERB_HELLO:
        return reply_hello(frame, hello, out, out_cap);
    case CONTROL_VERB_STATUS:
        return reply_status(frame, status, out, out_cap);
    case CONTROL_VERB_ERROR:
        /* `ERROR` is device->container only (§2.4); there is no ERROR request. */
        return control_encode_error(CONTROL_ERR_UNKNOWN_SUBCMD, frame->verb, out, out_cap);
    default:
        /*
         * Stage 1 implements HELLO and STATUS only; LOAD_PLAN, START, STOP,
         * PLACE_AMIIBO, UNPLACE_AMIIBO, PAIR_UNPAIR and CONFIG are the work of
         * #22-#25/#23. Their interim answer is `BAD_STATE` carrying the current
         * mode: the code set of §2.5 is closed and has no "not implemented", and
         * an unanswered request would hang the one-outstanding conversation of
         * §2.3. `features` advertises none of them (CONTROL_FEATURES is 0), so a
         * correct container does not send them in this stage. The real handlers
         * replace this arm verb by verb.
         */
        return control_encode_error(CONTROL_ERR_BAD_STATE, status->mode, out, out_cap);
    }
}
