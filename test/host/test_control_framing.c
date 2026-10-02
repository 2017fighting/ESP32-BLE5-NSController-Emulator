/*
 * Host-side assertions for the CONTROL framing and the HELLO/STATUS dispatch
 * (spec §2.2, §2.5, §2.6, §2.8, §2.10, §3.2; issue #21).
 *
 * No ESP-IDF, no device. This is what makes "a log flood costs frames but never
 * resets the link" a property asserted in CI rather than a hope for the bench.
 *
 * Build:
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include \
 *      -o test_control_framing \
 *      test/host/test_control_framing.c \
 *      main/src/protocol/control/control_frame.c \
 *      main/src/protocol/control/control_dispatch.c \
 *      main/src/protocol/control/control_log.c
 * Run:
 *   ./test_control_framing
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "protocol/control/control_log.h"
#include "protocol/control/control_protocol.h"

static int g_failures;
static int g_checks;

#define CHECK(cond, ...)                            \
    do {                                            \
        g_checks++;                                 \
        if (!(cond)) {                              \
            g_failures++;                           \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
            fprintf(stderr, __VA_ARGS__);           \
            fprintf(stderr, "\n");                  \
        }                                           \
    } while (0)

/* --------------------------------------------------------------- raw builder */

/* Mirrors control_encode() but with an explicit header `ver`, so the version
 * check can be exercised without a corrupting byte flip. */
static size_t wire_build(uint8_t ver, uint8_t type, uint8_t verb, const uint8_t *payload,
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
static int wire_parse(const uint8_t *wire, size_t n, control_frame_t *out, uint8_t *storage,
                      size_t storage_cap)
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

/* ------------------------------------------------------------------- harness */

typedef struct {
    control_decoder_t dec;
    control_hello_t hello;
    control_status_t status;
    uint8_t tx[CONTROL_WIRE_MAX];
    size_t tx_len;
    uint8_t tx_verb;
    int replies;
    int errors;
    uint8_t err_code;
    uint32_t err_detail;
} harness_t;

static void harness_init(harness_t *h)
{
    memset(h, 0, sizeof(*h));
    control_decoder_reset(&h->dec);
    control_hello_default(&h->hello, 0x11223344u);
    control_status_default(&h->status);
}

/* Every emitted wire frame is parsed back, so the test always inspects what
 * actually left the device rather than an intermediate struct. */
static void harness_emit(harness_t *h)
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
    }
}

static void harness_feed(harness_t *h, const uint8_t *bytes, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        control_dec_result_t r = control_decoder_feed(&h->dec, bytes[i]);
        if (r == CONTROL_DEC_FRAME) {
            h->tx_len = control_handle_request(&h->dec.last, &h->hello, &h->status, h->tx,
                                               sizeof(h->tx));
            harness_emit(h);
        } else if (r == CONTROL_DEC_REJECT) {
            h->tx_len = control_encode_error(h->dec.reject_code, h->dec.reject_detail, h->tx,
                                             sizeof(h->tx));
            harness_emit(h);
        }
    }
}

/* Reads an ERROR reply payload (code u8, detail le32) out of the wire bytes. */
static int decode_error_reply(const uint8_t *wire, size_t wire_len, uint8_t *code,
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

/* ---------------------------------------------------------------- CRC / COBS */

static void test_crc(void)
{
    const uint8_t check[] = "123456789";
    CHECK(control_crc16_update(CONTROL_CRC_INIT, check, 9) == 0x29B1u,
          "CRC check value is 0x%04X, expected 0x29B1", control_crc16_update(CONTROL_CRC_INIT, check, 9));

    /* The two-segment form must equal the concatenated one. */
    uint8_t hdr[5] = {1, 2, 5, 3, 0};
    uint8_t payload[3] = {0xAA, 0x00, 0xBB};
    uint8_t joined[8];
    memcpy(joined, hdr, 5);
    memcpy(joined + 5, payload, 3);
    CHECK(control_crc16_frame(hdr, payload, 3) == control_crc16_update(CONTROL_CRC_INIT, joined, 8),
          "two-segment CRC disagrees with the joined CRC");
}

static void test_cobs_vectors(void)
{
    uint8_t enc[1024];
    uint8_t dec[1024];
    size_t enc_len;
    size_t dec_len;

    /* Empty -> {0x01}; the framer, not COBS, adds the delimiters. */
    enc_len = control_cobs_encode(NULL, 0, enc, sizeof(enc));
    CHECK(enc_len == 0, "encode(NULL,0) should fail, got %zu", enc_len);
    enc_len = control_cobs_encode((const uint8_t *)"", 0, enc, sizeof(enc));
    CHECK(enc_len == 1 && enc[0] == 0x01, "COBS(empty) = {%zu,%02X}", enc_len, enc[0]);

    /* {0x00} -> {0x01,0x01}. */
    uint8_t zero = 0x00;
    enc_len = control_cobs_encode(&zero, 1, enc, sizeof(enc));
    CHECK(enc_len == 2 && enc[0] == 0x01 && enc[1] == 0x01, "COBS(0) = {%zu,%02X,%02X}",
          enc_len, enc[0], enc[1]);

    /* "A\0B" -> {0x02,'A',0x02,'B'}. */
    const uint8_t anb[] = {'A', 0x00, 'B'};
    enc_len = control_cobs_encode(anb, 3, enc, sizeof(enc));
    CHECK(enc_len == 4 && enc[0] == 0x02 && enc[1] == 'A' && enc[2] == 0x02 && enc[3] == 'B',
          "COBS(A\\0B) = {%zu,%02X,%02X,%02X,%02X}", enc_len, enc[0], enc[1], enc[2], enc[3]);

    CHECK(control_cobs_decode(enc, enc_len, dec, sizeof(dec), &dec_len) == CONTROL_COBS_OK &&
              dec_len == 3 && dec[0] == 'A' && dec[1] == 0x00 && dec[2] == 'B',
          "COBS decode round trip failed");

    /* A long run without zeros must round-trip at the 254 boundary. */
    uint8_t run[600];
    for (size_t i = 0; i < sizeof(run); i++) {
        run[i] = (uint8_t)((i % 253) + 1); /* nonzero */
    }
    enc_len = control_cobs_encode(run, sizeof(run), enc, sizeof(enc));
    CHECK(enc_len > 0, "long COBS encode failed");
    CHECK(control_cobs_decode(enc, enc_len, dec, sizeof(dec), &dec_len) == CONTROL_COBS_OK &&
              dec_len == sizeof(run) && memcmp(dec, run, sizeof(run)) == 0,
          "long COBS round trip failed");

    /* A truncated block (code promises more bytes than exist) is invalid. */
    uint8_t bad[] = {0x05, 'A'};
    CHECK(control_cobs_decode(bad, sizeof(bad), dec, sizeof(dec), &dec_len) == CONTROL_COBS_INVALID,
          "a truncated block must be invalid");

    /* Overflow reports how far it got. */
    uint8_t small[2];
    CHECK(control_cobs_decode(enc, enc_len, small, sizeof(small), &dec_len) == CONTROL_COBS_OVERFLOW,
          "decode past the output cap must overflow");
}

/* ------------------------------------------------------------------- framing */

static void test_frame_round_trip(void)
{
    uint8_t payload[] = {1};
    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, payload, 1, wire,
                              sizeof(wire));
    CHECK(n >= 2 && wire[0] == 0x00 && wire[n - 1] == 0x00,
          "wire form must be 0x00 · COBS · 0x00 (got %zu bytes)", n);

    harness_t h;
    harness_init(&h);
    harness_feed(&h, wire, n);
    CHECK(h.replies == 1 && h.tx_verb == CONTROL_VERB_HELLO, "HELLO len=1 did not get a HELLO reply");
}

static void test_leading_delimiter_is_mandatory(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t payload[] = {1};
    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, payload, 1, wire,
                              sizeof(wire));
    /* Drop the leading delimiter: the frame must not be recognised. */
    harness_feed(&h, wire + 1, n - 1);
    CHECK(h.replies == 0, "a frame without its leading delimiter must be ignored");
}

static void test_empty_segments_ignored(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t payload[] = {1};
    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, payload, 1, wire,
                              sizeof(wire));
    uint8_t stream[CONTROL_WIRE_MAX + 2];
    stream[0] = 0x00;
    stream[1] = 0x00;
    memcpy(&stream[2], wire, n);
    harness_feed(&h, stream, n + 2);
    CHECK(h.replies == 1, "two consecutive delimiters must be an ignored empty segment");
}

static void test_crc_failure_is_silent_and_resyncs(void)
{
    uint8_t payload[] = {1};
    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, payload, 1, wire,
                              sizeof(wire));
    /* Flip a byte inside the COBS block (not a delimiter). */
    wire[2] ^= 0x10;

    harness_t h;
    harness_init(&h);
    harness_feed(&h, wire, n);
    CHECK(h.replies == 0 && h.errors == 0, "a CRC failure must produce no reply at all");
    CHECK(control_decoder_silent_drops(&h.dec) == 1, "CRC failure must count as a silent drop");

    /* The next good frame is recovered. */
    n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_STATUS, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.replies == 1 && h.tx_verb == CONTROL_VERB_STATUS, "the frame after a CRC failure was lost");
}

/* ------------------------------------------------------------- log flood */

/* Log text never contains 0x00, so it is one COBS block that fails the CRC. */
static const char *const LOG_NOISE =
    "I (12345) transport_layer: UART RX activated foo bar baz qux quux corge grault\n";

static void test_log_flood_costs_frames_not_the_link(void)
{
    harness_t h;
    harness_init(&h);

    uint8_t hello[CONTROL_WIRE_MAX];
    uint8_t status[CONTROL_WIRE_MAX];
    size_t hello_n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, (const uint8_t *)"",
                                    0, hello, sizeof(hello));
    size_t status_n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_STATUS, NULL, 0, status,
                                     sizeof(status));

    uint8_t stream[4096];
    size_t at = 0;
    int frames = 0;
    for (int i = 0; i < 40; i++) {
        size_t noise = strlen(LOG_NOISE);
        memcpy(&stream[at], LOG_NOISE, noise);
        at += noise;
        memcpy(&stream[at], (i % 2 == 0) ? hello : status, (i % 2 == 0) ? hello_n : status_n);
        at += (i % 2 == 0) ? hello_n : status_n;
        frames++;
    }
    harness_feed(&h, stream, at);
    CHECK(h.replies == frames, "log flood: %d/%d frames answered", h.replies, frames);
    CHECK(h.errors == 0, "log flood must not draw a single ERROR reply (got %d)", h.errors);

    /* A log line injected mid-frame costs exactly that frame, never the link. */
    harness_t g;
    harness_init(&g);
    uint8_t interleaved[CONTROL_WIRE_MAX * 2 + 128];
    size_t k = 0;
    memcpy(&interleaved[k], hello, hello_n / 2);
    k += hello_n / 2;
    size_t noise = strlen(LOG_NOISE);
    memcpy(&interleaved[k], LOG_NOISE, noise);
    k += noise;
    memcpy(&interleaved[k], hello + hello_n / 2, hello_n - hello_n / 2);
    k += hello_n - hello_n / 2;
    memcpy(&interleaved[k], status, status_n);
    k += status_n;
    harness_feed(&g, interleaved, k);
    CHECK(g.replies == 1 && g.tx_verb == CONTROL_VERB_STATUS,
          "a frame corrupted mid-flight must not stop the next frame (replies=%d)", g.replies);
}

/* ------------------------------------------------------------------ HELLO */

static void test_hello_tolerant(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t wire[CONTROL_WIRE_MAX];

    /* len = 0 is a version probe. */
    size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.replies == 1 && h.tx_verb == CONTROL_VERB_HELLO, "HELLO len=0 must get capabilities");

    /* len = 1, same version. */
    uint8_t same[] = {CONTROL_PROTO_VER};
    n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, same, 1, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.replies == 2, "HELLO with a matching proto_ver must get capabilities");

    /* len = 1, different version -> VER_MISMATCH, detail = the device's version. */
    uint8_t other[] = {2};
    n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, other, 1, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_VER_MISMATCH &&
              h.err_detail == CONTROL_PROTO_VER,
          "HELLO version mismatch must be VER_MISMATCH detail=%u (got code=%u detail=%u)",
          CONTROL_PROTO_VER, h.err_code, h.err_detail);

    /* len = 4 with trailing junk: bytes past the first are ignored. */
    uint8_t junk[] = {CONTROL_PROTO_VER, 0xDE, 0xAD, 0xBE};
    n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, junk, sizeof(junk), wire,
                       sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.replies == 4 && h.errors == 1, "HELLO must ignore payload bytes past the first");
}

static void test_bad_header_version(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t wire[CONTROL_WIRE_MAX];
    uint8_t payload[] = {CONTROL_PROTO_VER};
    size_t n = wire_build(2, CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, payload, 1, wire,
                          sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_VER_MISMATCH,
          "header ver != 1 must be VER_MISMATCH");
}

/* ----------------------------------------------------------------- STATUS */

static void test_status_strict_and_layout(void)
{
    harness_t h;
    harness_init(&h);
    h.status.mode = CONTROL_MODE_IDLE;
    h.status.uptime_ms = 0x01020304u;
    h.status.plan_frame_count = 0xBEEFu;
    h.status.last_stop_reason = CONTROL_STOP_BOOT_LOCAL;

    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_STATUS, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.replies == 1 && h.tx_verb == CONTROL_VERB_STATUS, "STATUS len=0 must get a STATUS reply");

    /* The reply payload is exactly 47 bytes, at the §3.2 offsets. */
    control_frame_t reply;
    uint8_t storage[CONTROL_WIRE_MAX];
    CHECK(wire_parse(h.tx, h.tx_len, &reply, storage, sizeof(storage)) == 0, "STATUS reply must parse");
    CHECK(reply.len == 47, "STATUS payload must be exactly 47 bytes");
    CHECK(reply.payload[2] == CONTROL_MODE_IDLE, "STATUS.mode must be at offset 2");
    CHECK(reply.payload[20] == 0xEF && reply.payload[21] == 0xBE,
          "STATUS.plan_frame_count must be le16 at offset 20");
    CHECK(reply.payload[42] == CONTROL_STOP_BOOT_LOCAL, "STATUS.last_stop_reason must be at offset 42");
    CHECK(reply.payload[43] == 0x04 && reply.payload[44] == 0x03 &&
              reply.payload[45] == 0x02 && reply.payload[46] == 0x01,
          "STATUS.uptime_ms must be le32 at offset 43");

    /* Strict: a payload is BAD_LENGTH. */
    uint8_t one[] = {0x00};
    n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_STATUS, one, 1, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_LENGTH && h.err_detail == 1,
          "STATUS with a payload must be BAD_LENGTH detail=1");
}

/* ------------------------------------------------------- errors and dispatch */

static void test_unknown_type_and_verb(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t wire[CONTROL_WIRE_MAX];

    size_t n = wire_build(CONTROL_PROTO_VER, 7, CONTROL_VERB_STATUS, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_UNKNOWN_TYPE && h.err_detail == 7,
          "type 7 must be UNKNOWN_TYPE detail=7");

    n = wire_build(CONTROL_PROTO_VER, CONTROL_TYPE_REQUEST, 11, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_UNKNOWN_SUBCMD && h.err_detail == 11,
          "verb 11 must be UNKNOWN_SUBCMD detail=11");

    /* Verb 0 is an EVENT's, never a request's. */
    n = wire_build(CONTROL_PROTO_VER, CONTROL_TYPE_REQUEST, 0, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.errors == 3 && h.err_code == CONTROL_ERR_UNKNOWN_SUBCMD, "verb 0 as a REQUEST must reject");
}

static void test_reply_and_event_at_device_are_silent(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t wire[CONTROL_WIRE_MAX];

    size_t n = wire_build(CONTROL_PROTO_VER, CONTROL_TYPE_REPLY, CONTROL_VERB_STATUS, NULL, 0, wire,
                          sizeof(wire));
    harness_feed(&h, wire, n);
    n = wire_build(CONTROL_PROTO_VER, CONTROL_TYPE_EVENT, 0, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.replies == 0 && h.errors == 0,
          "REPLY/EVENT at the device must be discarded silently (replies=%d errors=%d)", h.replies,
          h.errors);
}

static void test_unimplemented_verb_stub(void)
{
    harness_t h;
    harness_init(&h);
    h.status.mode = CONTROL_MODE_IDLE;
    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_START, NULL, 0, wire, sizeof(wire));
    harness_feed(&h, wire, n);
    CHECK(h.replies == 1, "a staged-out verb must still be answered");
    uint8_t code = 0;
    uint32_t detail = 0;
    CHECK(decode_error_reply(h.tx, h.tx_len, &code, &detail) == 0 && code == CONTROL_ERR_BAD_STATE &&
              detail == CONTROL_MODE_IDLE,
          "START is an interim BAD_STATE(mode) stub until #22-#25");
}

static void test_error_reply_shape(void)
{
    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_encode_error(CONTROL_ERR_PLAN_TOO_LARGE, 0x00012345u, wire, sizeof(wire));
    uint8_t code = 0;
    uint32_t detail = 0;
    CHECK(decode_error_reply(wire, n, &code, &detail) == 0, "ERROR reply must decode");
    CHECK(code == CONTROL_ERR_PLAN_TOO_LARGE && detail == 0x00012345u, "ERROR detail must be le32");
}

static void test_max_frame_bounds(void)
{
    uint8_t payload[CONTROL_MAX_PAYLOAD + 1];
    memset(payload, 0x5A, sizeof(payload));
    uint8_t wire[CONTROL_WIRE_MAX + 64];

    size_t ok = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_LOAD_PLAN, payload,
                               CONTROL_MAX_PAYLOAD, wire, sizeof(wire));
    CHECK(ok > 0 && ok <= CONTROL_WIRE_MAX, "a max_frame payload must encode within CONTROL_WIRE_MAX");

    size_t too_big = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_LOAD_PLAN, payload,
                                    CONTROL_MAX_PAYLOAD + 1, wire, sizeof(wire));
    CHECK(too_big == 0, "a payload past max_frame must not encode");
}

static void test_hello_payload_layout(void)
{
    control_hello_t hello;
    control_hello_default(&hello, 0x04030201u);
    uint8_t out[20];
    CHECK(control_hello_payload(&hello, out, sizeof(out)) == 20, "HELLO payload is 20 bytes");
    CHECK(out[0] == CONTROL_PROTO_VER, "HELLO.proto_ver at 0");
    CHECK(out[1] == CONTROL_FW_MAJOR && out[2] == CONTROL_FW_MINOR && out[3] == CONTROL_FW_PATCH &&
              out[4] == CONTROL_FW_BUILD,
          "HELLO.fw_version at 1..4");
    CHECK(out[5] == 0x01 && out[6] == 0x02 && out[7] == 0x03 && out[8] == 0x04,
          "HELLO.boot_id must be le32 at 5");
    CHECK(out[9] == 0x00 && out[10] == 0x02, "HELLO.max_frame = 512 at 9");
    CHECK(out[11] == 0x00 && out[12] == 0x01, "HELLO.chunk_size = 256 at 11");
    CHECK(out[13] == 0x00 && out[14] == 0x00 && out[15] == 0x01 && out[16] == 0x00,
          "HELLO.plan_capacity_bytes = 65536 at 13");
    CHECK(out[17] == 1, "HELLO.plan_slots at 17");
    CHECK(out[18] == 0x00 && out[19] == 0x00, "HELLO.features = 0 in stage 1");
}

/* A block longer than max_frame is untrusted and must be silent (§2.8): it must
 * not be able to forge a `len` and draw an ERROR. */
static void test_oversize_block_is_silent(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t stream[1024];
    size_t at = 0;

    stream[at++] = 0x00;
    for (int i = 0; i < 700; i++) {
        stream[at++] = (uint8_t)((i % 250) + 1); /* nonzero: one over-long COBS block */
    }
    stream[at++] = 0x00;

    uint8_t status[CONTROL_WIRE_MAX];
    size_t sn = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_STATUS, NULL, 0, status,
                               sizeof(status));
    memcpy(&stream[at], status, sn);
    at += sn;

    harness_feed(&h, stream, at);
    CHECK(h.replies == 1 && h.tx_verb == CONTROL_VERB_STATUS,
          "an over-long block must be silent and the next frame recovered");
    CHECK(h.errors == 0, "an over-long block must not draw an ERROR");
    CHECK(control_decoder_silent_drops(&h.dec) >= 1, "the over-long block must count as a silent drop");
}

/* ------------------------------------------------------------ random resync */

static uint32_t rnd_state = 0x12345678u;
static uint32_t rnd(void)
{
    rnd_state ^= rnd_state << 13;
    rnd_state ^= rnd_state >> 17;
    rnd_state ^= rnd_state << 5;
    return rnd_state;
}

/* Noise never resets the link: 100 random log-ish lines and randomly corrupted
 * frames later, every intact frame is still answered. */
static void test_random_noise_resync(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t stream[16384];
    size_t at = 0;
    int intact = 0;

    for (int i = 0; i < 100; i++) {
        int noise = 1 + (int)(rnd() % 44);
        for (int k = 0; k < noise; k++) {
            stream[at++] = (uint8_t)((rnd() % 255u) + 1u); /* log text never has 0x00 */
        }

        uint8_t payload[24];
        size_t plen = rnd() % (sizeof(payload) + 1);
        if (plen > 0) {
            payload[0] = CONTROL_PROTO_VER;
            for (size_t k = 1; k < plen; k++) {
                payload[k] = (uint8_t)((rnd() % 255u) + 1u);
            }
        }
        uint8_t wire[CONTROL_WIRE_MAX];
        size_t n = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_HELLO, payload, plen, wire,
                                  sizeof(wire));

        bool corrupt = (rnd() % 4u) == 0u;
        if (corrupt && n > 4) {
            wire[2 + (rnd() % (n - 4))] ^= (uint8_t)(1u << (rnd() % 8u));
        } else {
            intact++;
        }
        memcpy(&stream[at], wire, n);
        at += n;
    }

    harness_feed(&h, stream, at);
    CHECK(h.errors == 0, "random noise drew %d ERROR replies", h.errors);
    CHECK(h.replies == intact, "random resync: %d/%d intact frames answered", h.replies, intact);

    /* The link is still alive after all of it. */
    uint8_t status[CONTROL_WIRE_MAX];
    size_t sn = control_encode(CONTROL_TYPE_REQUEST, CONTROL_VERB_STATUS, NULL, 0, status,
                               sizeof(status));
    int before = h.replies;
    harness_feed(&h, status, sn);
    CHECK(h.replies == before + 1 && h.tx_verb == CONTROL_VERB_STATUS,
          "the link was reset by the noise stream");
}

/* ---------------------------------------------------------------- log policy */

static void test_log_rate_policy(void)
{
    control_log_policy_t p;
    control_log_policy_init(&p);

    /* Not bulk: every line is allowed. */
    CHECK(control_log_policy_allow(&p, false, 1000), "non-bulk lines are never suppressed");
    CHECK(control_log_policy_allow(&p, false, 1001), "non-bulk lines are never suppressed");

    /* Bulk: one line, then suppressed until the interval passes. */
    CHECK(control_log_policy_allow(&p, true, 100000), "the first bulk line is allowed");
    CHECK(!control_log_policy_allow(&p, true, 100001), "a line inside the interval is suppressed");
    CHECK(!control_log_policy_allow(&p, true, 100000 + CONTROL_LOG_BULK_MIN_INTERVAL_US - 1),
          "a line just inside the interval is suppressed");
    CHECK(control_log_policy_allow(&p, true, 100000 + CONTROL_LOG_BULK_MIN_INTERVAL_US),
          "a line at the interval is allowed");
    CHECK(control_log_policy_dropped(&p) == 2, "two lines were dropped");
}

int main(void)
{
    test_crc();
    test_cobs_vectors();
    test_frame_round_trip();
    test_leading_delimiter_is_mandatory();
    test_empty_segments_ignored();
    test_crc_failure_is_silent_and_resyncs();
    test_log_flood_costs_frames_not_the_link();
    test_hello_tolerant();
    test_bad_header_version();
    test_status_strict_and_layout();
    test_unknown_type_and_verb();
    test_reply_and_event_at_device_are_silent();
    test_unimplemented_verb_stub();
    test_error_reply_shape();
    test_max_frame_bounds();
    test_hello_payload_layout();
    test_oversize_block_is_silent();
    test_random_noise_resync();
    test_log_rate_policy();

    if (g_failures == 0) {
        printf("control framing ok: %d checks\n", g_checks);
        return 0;
    }
    fprintf(stderr, "control framing FAILED: %d/%d checks\n", g_failures, g_checks);
    return 1;
}
