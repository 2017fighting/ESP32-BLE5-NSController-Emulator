/*
 * Shared host-test harness for the CONTROL layer (spec §2, §3, issue #21/#22).
 *
 * Header-only and `static inline` so the framing suite and the verb suite can
 * both link it without duplicate definitions or unused-function warnings. Each
 * translation unit gets its own `g_failures`/`g_checks` and its own `main()`.
 */

#ifndef CONTROL_TEST_UTIL_H
#define CONTROL_TEST_UTIL_H

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "protocol/control/control_protocol.h"
#include "protocol/control/control_verbs.h"

static int g_failures;
static int g_checks;

#define CHECK(cond, ...)                                          \
    do {                                                          \
        g_checks++;                                               \
        if (!(cond)) {                                            \
            g_failures++;                                         \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);  \
            fprintf(stderr, __VA_ARGS__);                         \
            fprintf(stderr, "\n");                                \
        }                                                         \
    } while (0)

/* A plan staging buffer smaller than the advertised capacity, so a transfer that
 * exceeds the device's real buffer is reachable in a test; two ACK windows wide,
 * so the window boundary is reachable too. */
#define CONTROL_TEST_PLAN_CAP 8192u

/* --------------------------------------------------------------- raw builder */

/* Mirrors control_encode() but with an explicit header `ver`, so the version
 * check can be exercised without a corrupting byte flip. */
static inline size_t wire_build(uint8_t ver, uint8_t type, uint8_t verb, const uint8_t *payload,
                                size_t len, uint8_t *out, size_t out_cap)
{
    uint8_t scratch[CONTROL_MAX_FRAME];
    scratch[0] = ver;
    scratch[1] = type;
    scratch[2] = verb;
    scratch[3] = (uint8_t)(len & 0xFFu);
    scratch[4] = (uint8_t)((len >> 8) & 0xFFu);
    uint16_t crc = control_crc16_frame(scratch, payload, len);
    scratch[5] = (uint8_t)(crc & 0xFFu);
    scratch[6] = (uint8_t)(crc >> 8);
    if (len > 0) {
        memcpy(&scratch[7], payload, len);
    }
    out[0] = 0x00;
    size_t enc = control_cobs_encode(scratch, 7 + len, &out[1], out_cap - 1);
    out[enc + 1] = 0x00;
    return enc + 2;
}

/* Host-side parse of a wire frame, accepting any type (the device decoder
 * deliberately discards REPLY/EVENT). Used to inspect what the device sent. */
static inline int wire_parse(const uint8_t *wire, size_t n, control_frame_t *out,
                             uint8_t *storage, size_t storage_cap)
{
    if (n < 3 || wire[0] != 0x00 || wire[n - 1] != 0x00) {
        return -1;
    }
    size_t dec_len = 0;
    if (control_cobs_decode(&wire[1], n - 2, storage, storage_cap, &dec_len) != CONTROL_COBS_OK) {
        return -1;
    }
    if (dec_len < CONTROL_HEADER_SIZE) {
        return -1;
    }
    out->ver = storage[0];
    out->type = storage[1];
    out->verb = storage[2];
    out->len = (uint16_t)(storage[3] | (storage[4] << 8));
    if ((size_t)CONTROL_HEADER_SIZE + out->len != dec_len) {
        return -1;
    }
    uint16_t crc = (uint16_t)(storage[5] | (storage[6] << 8));
    if (control_crc16_frame(storage, storage + CONTROL_HEADER_SIZE, out->len) != crc) {
        return -1;
    }
    out->payload = storage + CONTROL_HEADER_SIZE;
    return 0;
}

/* Reads an ERROR reply payload (code u8, detail le32) out of the wire bytes. */
static inline int decode_error_reply(const uint8_t *wire, size_t wire_len, uint8_t *code,
                                     uint32_t *detail)
{
    control_frame_t frame;
    uint8_t storage[CONTROL_WIRE_MAX];
    if (wire_parse(wire, wire_len, &frame, storage, sizeof(storage)) != 0) {
        return -1;
    }
    if (frame.verb != CONTROL_VERB_ERROR || frame.len != 5) {
        return -1;
    }
    *code = frame.payload[0];
    *detail = (uint32_t)frame.payload[1] | ((uint32_t)frame.payload[2] << 8) |
              ((uint32_t)frame.payload[3] << 16) | ((uint32_t)frame.payload[4] << 24);
    return 0;
}

/* ------------------------------------------------------------------- harness */

typedef struct {
    control_decoder_t dec;
    control_state_t ctl;
    uint8_t plan_stage[CONTROL_TEST_PLAN_CAP];

    uint8_t tx[CONTROL_WIRE_MAX];
    size_t tx_len;
    uint8_t tx_verb;
    int replies; /* every frame the device emitted */
    int acks;    /* REPLY frames that were not ERROR */
    int errors;
    uint8_t err_code;
    uint32_t err_detail;

    /* The effects double of control_effects_t (#22). */
    int start_calls;
    int stop_calls;
    int place_calls;
    int unplace_calls;
    int pair_calls;
    int config_calls;
    uint8_t last_stop_reason;
    uint16_t last_config_ms;
    uint8_t last_config_led;
    uint8_t last_placed[CONTROL_TAG_SIZE];
    size_t last_placed_len;
} harness_t;

static inline void harness_fx_start(void *ctx)
{
    ((harness_t *)ctx)->start_calls++;
}

static inline void harness_fx_stop(void *ctx, uint8_t reason)
{
    harness_t *h = (harness_t *)ctx;
    h->stop_calls++;
    h->last_stop_reason = reason;
}

static inline void harness_fx_place(void *ctx, const uint8_t *tag, size_t len)
{
    harness_t *h = (harness_t *)ctx;
    h->place_calls++;
    h->last_placed_len = len < sizeof(h->last_placed) ? len : sizeof(h->last_placed);
    memcpy(h->last_placed, tag, h->last_placed_len);
}

static inline void harness_fx_unplace(void *ctx)
{
    ((harness_t *)ctx)->unplace_calls++;
}

static inline void harness_fx_pair(void *ctx)
{
    ((harness_t *)ctx)->pair_calls++;
}

static inline void harness_fx_config(void *ctx, uint16_t report_interval_ms, uint8_t led)
{
    harness_t *h = (harness_t *)ctx;
    h->config_calls++;
    h->last_config_ms = report_interval_ms;
    h->last_config_led = led;
}

static inline void harness_init(harness_t *h)
{
    memset(h, 0, sizeof(*h));
    control_decoder_reset(&h->dec);
    control_state_init(&h->ctl, 0x11223344u, h->plan_stage, sizeof(h->plan_stage));
    control_effects_t fx = {
        .ctx = h,
        .start_macro = harness_fx_start,
        .stop = harness_fx_stop,
        .place_tag = harness_fx_place,
        .unplace_tag = harness_fx_unplace,
        .pair_unpair = harness_fx_pair,
        .apply_config = harness_fx_config,
    };
    control_state_set_effects(&h->ctl, &fx);
}

/* Every emitted wire frame is parsed back, so the test always inspects what
 * actually left the device rather than an intermediate struct. */
static inline void harness_emit(harness_t *h)
{
    if (h->tx_len == 0) {
        return;
    }
    h->replies++;
    control_frame_t reply;
    uint8_t storage[CONTROL_WIRE_MAX];
    CHECK(wire_parse(h->tx, h->tx_len, &reply, storage, sizeof(storage)) == 0,
          "reply did not parse as a well-formed frame");
    h->tx_verb = reply.verb;
    if (reply.verb == CONTROL_VERB_ERROR && reply.len == 5) {
        h->errors++;
        h->err_code = reply.payload[0];
        h->err_detail = (uint32_t)reply.payload[1] | ((uint32_t)reply.payload[2] << 8) |
                        ((uint32_t)reply.payload[3] << 16) | ((uint32_t)reply.payload[4] << 24);
    } else {
        h->acks++;
    }
}

static inline void harness_feed(harness_t *h, const uint8_t *bytes, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        control_dec_result_t r = control_decoder_feed(&h->dec, bytes[i]);
        if (r == CONTROL_DEC_FRAME) {
            h->tx_len = control_handle_request(&h->ctl, &h->dec.last, h->tx, sizeof(h->tx));
            harness_emit(h);
        } else if (r == CONTROL_DEC_REJECT) {
            h->tx_len = control_encode_error(h->dec.reject_code, h->dec.reject_detail, h->tx,
                                             sizeof(h->tx));
            harness_emit(h);
        }
    }
}

/* Encodes one request and feeds it; returns the number of replies it drew. */
static inline int harness_request(harness_t *h, uint8_t verb, const uint8_t *payload, size_t len)
{
    uint8_t wire[CONTROL_WIRE_MAX];
    int before = h->replies;
    size_t n = control_encode(CONTROL_TYPE_REQUEST, verb, payload, len, wire, sizeof(wire));
    harness_feed(h, wire, n);
    return h->replies - before;
}

#endif /* CONTROL_TEST_UTIL_H */
