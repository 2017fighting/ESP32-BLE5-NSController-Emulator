/*
 * CONTROL dispatch: the ten verbs of §2.4 over the §2.7 bulk path, the HELLO
 * (tolerant) and STATUS (strict) replies, and the canonical ERROR table.
 * Portable C; the host test links this file directly (issue #22).
 *
 * The mode axis is read from `st->status.mode` and moved only by an explicit
 * verb (ADR-0007). The physical halves of the moves — arming the executor,
 * releasing neutral, the NFC state byte, the BLE bond — are `control_effects_t`
 * callbacks owned by #23-#25; this file owns what the wire can observe.
 */

#include "protocol/control/control_verbs.h"

#include <string.h>

#include "protocol/control/control_bulk.h"

static uint16_t rd_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

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

/* -------------------------------------------------------------------- state */

void control_state_init(control_state_t *st, uint32_t boot_id, uint8_t *plan_stage,
                        size_t plan_stage_cap)
{
    if (st == NULL) {
        return;
    }
    memset(st, 0, sizeof(*st));
    control_hello_default(&st->hello, boot_id);
    control_status_default(&st->status);
    st->stage = CONTROL_STAGE_NONE;
    st->plan_stage = plan_stage;
    st->plan_stage_cap = plan_stage_cap;
}

void control_state_set_effects(control_state_t *st, const control_effects_t *fx)
{
    if (st == NULL || fx == NULL) {
        return;
    }
    st->fx = *fx;
}

void control_clear_error(control_state_t *st)
{
    if (st == NULL) {
        return;
    }
    st->status.last_error_code = CONTROL_ERR_NONE;
    st->status.last_error_detail = 0;
}

size_t control_reject(control_state_t *st, uint8_t code, uint32_t detail, uint8_t *out,
                      size_t out_cap)
{
    if (st != NULL) {
        /* §3.2: the pair is recorded so a rejection is legible from the next
         * STATUS poll as well as from the ERROR reply. */
        st->status.last_error_code = code;
        st->status.last_error_detail = detail;
    }
    return control_encode_error(code, detail, out, out_cap);
}

size_t control_ack(control_state_t *st, uint8_t verb, const uint8_t *payload, size_t len,
                   uint8_t *out, size_t out_cap)
{
    control_clear_error(st);
    return control_encode(CONTROL_TYPE_REPLY, verb, payload, len, out, out_cap);
}

/* ---------------------------------------------------------- HELLO / STATUS */

static size_t reply_hello(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                          size_t out_cap)
{
    /* §2.8: tolerant, and exactly this much. len = 0 is a version probe;
     * len >= 1 compares the first byte and ignores the rest. */
    if (frame->len >= 1 && frame->payload != NULL && frame->payload[0] != st->hello.proto_ver) {
        return control_reject(st, CONTROL_ERR_VER_MISMATCH, st->hello.proto_ver, out, out_cap);
    }
    uint8_t payload[sizeof(control_hello_t)];
    size_t n = control_hello_payload(&st->hello, payload, sizeof(payload));
    if (n == 0) {
        return 0;
    }
    return control_ack(st, CONTROL_VERB_HELLO, payload, n, out, out_cap);
}

static size_t reply_status(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                           size_t out_cap)
{
    /* Strict: no payload, exactly (§2.4). */
    if (frame->len != 0) {
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }
    uint8_t payload[sizeof(control_status_t)];
    size_t n = control_status_payload(&st->status, payload, sizeof(payload));
    if (n == 0) {
        return 0;
    }
    /* §3.2: `last_error` is cleared by the next successful *verb*, not by a read
     * — and STATUS is the read. Clearing it here would make the 2 Hz poll erase
     * the very error the poll is reporting. */
    return control_encode(CONTROL_TYPE_REPLY, CONTROL_VERB_STATUS, payload, n, out, out_cap);
}

/* ------------------------------------------------------------------- verbs */

/* The five no-payload requests: START, STOP, UNPLACE_AMIIBO, PAIR_UNPAIR and
 * STATUS (§2.4). Enforced once here rather than per handler. */
static bool has_no_payload(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                           size_t out_cap, size_t *reply_len)
{
    if (frame->len != 0) {
        *reply_len = control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
        return false;
    }
    return true;
}

static size_t reply_start(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                          size_t out_cap)
{
    size_t rejected = 0;
    if (!has_no_payload(st, frame, out, out_cap, &rejected)) {
        return rejected;
    }
    switch (st->status.mode) {
    case CONTROL_MODE_IDLE:
        if (!st->plan_committed) {
            return control_reject(st, CONTROL_ERR_NO_PLAN, 0, out, out_cap);
        }
        if (st->fx.start_macro != NULL) {
            st->fx.start_macro(st->fx.ctx);
        }
        st->status.mode = CONTROL_MODE_MACRO;
        st->status.current_frame = 0;
        st->status.loop_count = 0;
        return control_ack(st, CONTROL_VERB_START, NULL, 0, out, out_cap);
    case CONTROL_MODE_MACRO:
        return control_reject(st, CONTROL_ERR_ALREADY_RUNNING, 0, out, out_cap);
    default:
        /* §4.3: AMIIBO + START is BAD_STATE, and nothing is queued. */
        return control_reject(st, CONTROL_ERR_BAD_STATE, st->status.mode, out, out_cap);
    }
}

static size_t reply_stop(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                         size_t out_cap)
{
    size_t rejected = 0;
    if (!has_no_payload(st, frame, out, out_cap, &rejected)) {
        return rejected;
    }
    if (st->status.mode == CONTROL_MODE_IDLE) {
        /* §4.4: reducing activity is safe, so STOP in IDLE is an idempotent ACK
         * and does not rewrite a stop reason that did not happen. */
        return control_ack(st, CONTROL_VERB_STOP, NULL, 0, out, out_cap);
    }

    if (st->fx.stop != NULL) {
        st->fx.stop(st->fx.ctx, CONTROL_STOP_CONTAINER);
    }
    if (st->status.mode == CONTROL_MODE_AMIIBO) {
        st->tag_placed = false;
        st->status.tag_state = CONTROL_TAG_NONE;
        memset(st->status.tag_identity, 0, sizeof(st->status.tag_identity));
    }
    st->status.mode = CONTROL_MODE_IDLE;
    st->status.current_frame = 0;
    st->status.last_stop_reason = CONTROL_STOP_CONTAINER;
    return control_ack(st, CONTROL_VERB_STOP, NULL, 0, out, out_cap);
}

static size_t reply_unplace_amiibo(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                                   size_t out_cap)
{
    size_t rejected = 0;
    if (!has_no_payload(st, frame, out, out_cap, &rejected)) {
        return rejected;
    }
    if (st->status.mode == CONTROL_MODE_AMIIBO) {
        if (st->fx.unplace_tag != NULL) {
            st->fx.unplace_tag(st->fx.ctx);
        }
        /* §4.3: the tag stops answering and the 540 bytes are retained. */
        st->tag_placed = false;
        st->status.tag_state = CONTROL_TAG_NONE;
        memset(st->status.tag_identity, 0, sizeof(st->status.tag_identity));
        st->status.mode = CONTROL_MODE_IDLE;
    }
    return control_ack(st, CONTROL_VERB_UNPLACE_AMIIBO, NULL, 0, out, out_cap);
}

static size_t reply_pair_unpair(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                                size_t out_cap)
{
    size_t rejected = 0;
    if (!has_no_payload(st, frame, out, out_cap, &rejected)) {
        return rejected;
    }
    /* ADR-0013: always *forget the bond and go pairable*, never a toggle, and
     * accepted in every mode — its effect is on the console link. */
    if (st->fx.pair_unpair != NULL) {
        st->fx.pair_unpair(st->fx.ctx);
    }
    return control_ack(st, CONTROL_VERB_PAIR_UNPAIR, NULL, 0, out, out_cap);
}

static size_t reply_config(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                           size_t out_cap)
{
    /* §2.9: exactly three bytes, both fields always present. */
    if (frame->len != CONTROL_CONFIG_SIZE) {
        return control_reject(st, CONTROL_ERR_BAD_LENGTH, frame->len, out, out_cap);
    }
    uint16_t report_interval_ms = rd_le16(frame->payload);
    uint8_t led = frame->payload[2];
    st->config_report_interval_ms = report_interval_ms;
    st->config_led = led;
    if (st->status.mode == CONTROL_MODE_MACRO) {
        /* A report-interval change mid-macro would perturb the one thing ADR-0003
         * says is consistent, so it waits for the loop boundary and the executor
         * calls control_config_apply_at_boundary(). */
        st->config_pending = true;
    } else {
        st->config_pending = false;
        if (st->fx.apply_config != NULL) {
            st->fx.apply_config(st->fx.ctx, report_interval_ms, led);
        }
    }
    return control_ack(st, CONTROL_VERB_CONFIG, NULL, 0, out, out_cap);
}

void control_config_apply_at_boundary(control_state_t *st)
{
    if (st == NULL || !st->config_pending) {
        return;
    }
    st->config_pending = false;
    if (st->fx.apply_config != NULL) {
        st->fx.apply_config(st->fx.ctx, st->config_report_interval_ms, st->config_led);
    }
}

/* ---------------------------------------------------------------- dispatch */

size_t control_handle_request(control_state_t *st, const control_frame_t *frame, uint8_t *out,
                              size_t out_cap)
{
    if (st == NULL || frame == NULL || out == NULL) {
        return 0;
    }

    switch (frame->verb) {
    case CONTROL_VERB_HELLO:
        return reply_hello(st, frame, out, out_cap);
    case CONTROL_VERB_LOAD_PLAN:
    case CONTROL_VERB_PLACE_AMIIBO:
        return control_bulk_request(st, frame, out, out_cap);
    case CONTROL_VERB_START:
        return reply_start(st, frame, out, out_cap);
    case CONTROL_VERB_STOP:
        return reply_stop(st, frame, out, out_cap);
    case CONTROL_VERB_STATUS:
        return reply_status(st, frame, out, out_cap);
    case CONTROL_VERB_UNPLACE_AMIIBO:
        return reply_unplace_amiibo(st, frame, out, out_cap);
    case CONTROL_VERB_PAIR_UNPAIR:
        return reply_pair_unpair(st, frame, out, out_cap);
    case CONTROL_VERB_CONFIG:
        return reply_config(st, frame, out, out_cap);
    default:
        /* `ERROR` is device->container only (§2.4) and the decoder already
         * refuses verbs outside 1-10; this arm is the belt to that braces. */
        return control_reject(st, CONTROL_ERR_UNKNOWN_SUBCMD, frame->verb, out, out_cap);
    }
}
