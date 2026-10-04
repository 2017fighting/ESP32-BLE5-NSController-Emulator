/*
 * Host-side assertions for the NFC state machine and the tag server (spec §4.9,
 * §6.5, §6.6, §7.3 step 6; issue #25).
 *
 * The acceptance property of the tickets lives here: a placed tag is served in
 * the capture's own read shapes — a 600-byte `[60 B framing][540 B image]` space
 * in 70-byte chunks under a `last` · `len` u16 head (§6.6, #46) — the status
 * answer is the reader's lifecycle (`0x09` → `0x04` while `0x06`'s read is armed
 * → `0x09`, or `0x07` + `0x41` once `0x04` ends a completed read) and the HID
 * report byte is the reader's event counter (§6.6/§4.9, #48), the console's
 * polling edges drive `SCAN_ENDED`, and a console write stages the captured
 * `0x14` frame, commits on `0x08` and dies with the placement (#47). None of it
 * needs a device or a console.
 *
 * Build:
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include -Itest/host \
 *      -o test_nfc_tag test/host/test_nfc_tag.c main/src/controller/nfc_tag.c
 * Run:
 *   ./test_nfc_tag
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "controller/nfc_tag.h"

static int g_failures;
static int g_checks;

/* The tests drive the §6.5 gap's deadline explicitly: a wall clock would make
 * them flaky, and the gap is a `now_ms` deadline rather than a duration. */
static uint32_t g_now;

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

/* The state-byte edges an observer sees, in order. The gap is a *sequence*
 * (§6.5), so a set of callback invocations is not enough to assert it. */
typedef struct {
    nfc_tag_t *nfc;
    uint8_t states[32];
    size_t count;
    size_t scan_ended;
    /* When `probe`, every callback records `nfc_tag_placed()` at that instant —
     * which is how the test proves the gap is not merely ordered but *seen* as
     * tag-absent. */
    bool placed_at_callback[32];
} observer_t;

static void observer_state(void *ctx, uint8_t state)
{
    observer_t *o = (observer_t *)ctx;
    if (o->count < sizeof(o->states)) {
        o->states[o->count] = state;
        o->placed_at_callback[o->count] = nfc_tag_placed(o->nfc);
        o->count++;
    }
}

static void observer_scan_ended(void *ctx)
{
    observer_t *o = (observer_t *)ctx;
    o->scan_ended++;
}

static void observer_attach(nfc_tag_t *nfc, observer_t *o)
{
    memset(o, 0, sizeof(*o));
    o->nfc = nfc;
    const nfc_tag_events_t events = {
        .ctx = o,
        .state_changed = observer_state,
        .scan_ended = observer_scan_ended,
    };
    nfc_tag_set_events(nfc, &events);
}

/* The §6.3 worked example: `UID = 04 11 FE CA 52 6C 81`, so the tag image opens
 * `04 11 FE 63 CA 52 6C 81` with `BCC0 = 0x63` at byte 3. */
static void build_tag(uint8_t tag[NFC_TAG_SIZE], uint8_t seed)
{
    memset(tag, 0, NFC_TAG_SIZE);
    tag[0] = 0x04;
    tag[1] = 0x11;
    tag[2] = 0xFE;
    tag[3] = 0x63; /* BCC0 */
    tag[4] = 0xCA;
    tag[5] = 0x52;
    tag[6] = 0x6C;
    tag[7] = 0x81;
    for (size_t i = 8; i < NFC_TAG_SIZE; i++) {
        tag[i] = (uint8_t)(seed + (uint8_t)i);
    }
}

/* The canonical full-tag read request (`switch2_controller_research/commands.md:64`):
 * `d0 07`, seven zero UID bytes (read any tag), `01 03` and the three page
 * ranges that cover all 135 pages. The framing head echoes the request's own
 * `payload[10..18]` at its `[51..59]` (§6.6), which is how the suite checks that
 * the request reached the head; the lifecycle tests use it in both compiles,
 * because `0x06` arms a read whatever the served space's shape. */
static const uint8_t k_read_request[19] = {
    0xD0, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x03, 0x00, 0x3B, 0x3C, 0x77, 0x78, 0x86, 0x00, 0x00,
};

#if !NFC_TAG_READ_PLAIN_VIEW
/* The 32-byte constant both captures carry at head offset 19 — the NS2 read
 * buffer's and the NS1 P1 packet's `magic` blob are the same bytes (§6.6). */
static const uint8_t k_frame_constant[32] = {
    0x7D, 0xFD, 0xF0, 0x79, 0x36, 0x51, 0xAB, 0xD7, 0x46, 0x6E, 0x39, 0xC1,
    0x91, 0xBA, 0xBE, 0xB8, 0x56, 0xCE, 0xED, 0xF1, 0xCE, 0x44, 0xCC, 0x75,
    0xEA, 0xFB, 0x27, 0x09, 0x4D, 0x08, 0x7A, 0xE8,
};
#endif

/* The wire address of an image byte: the image begins at `0x3C` in the capture's
 * 600-byte served space, and at 0 in the comparison compile (§6.6, #46). The
 * suite speaks image offsets; this is the one place the wire space appears. */
static uint16_t wire_of(uint16_t image_off)
{
#if NFC_TAG_READ_PLAIN_VIEW
    return image_off;
#else
    return (uint16_t)(image_off + NFC_TAG_FRAME_SIZE);
#endif
}

static size_t read_buffer(nfc_tag_t *nfc, uint16_t image_off, uint8_t *out, size_t cap)
{
    const uint16_t wire = wire_of(image_off);
    const uint8_t req[2] = {(uint8_t)(wire & 0xFFu), (uint8_t)(wire >> 8)};
    return nfc_tag_command(nfc, NFC_CMD_READ_BUFFER, req, sizeof(req), out, cap);
}

/* A raw wire-address read, for the asks the console makes in its own
 * coordinate space. */
static size_t read_wire(nfc_tag_t *nfc, uint16_t wire, uint8_t *out, size_t cap)
{
    const uint8_t req[2] = {(uint8_t)(wire & 0xFFu), (uint8_t)(wire >> 8)};
    return nfc_tag_command(nfc, NFC_CMD_READ_BUFFER, req, sizeof(req), out, cap);
}

/* The chunk head as the console reads it: `last` u8 · `len` u16 (LE). */
static bool chunk_head(const uint8_t *out, size_t n, bool *last, size_t *len)
{
    if (n < NFC_TAG_READ_HEAD_SIZE || (out[0] != 0x00u && out[0] != 0x01u)) {
        return false;
    }
    *last = out[0] == 0x01u;
    *len = (size_t)out[1] | ((size_t)out[2] << 8);
    return *len <= n - NFC_TAG_READ_HEAD_SIZE;
}

/* Reconstruct the served space from the console's own pulls: start at 0, take
 * each chunk's declared length, stop at the chunk that says it is last. This is
 * the acceptance property (#46): nothing here reads the server's innards, so a
 * head the console cannot parse fails the suite — and the space has to *end*
 * where the console says it does, so a run that reaches the cap without a last
 * chunk is not a reconstruction. */
static size_t pull_space(nfc_tag_t *nfc, uint8_t *space, size_t cap)
{
    size_t filled = 0;
    uint8_t out[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    while (filled < cap) {
        size_t n = read_wire(nfc, (uint16_t)filled, out, sizeof(out));
        bool last = false;
        size_t len = 0;
        if (!chunk_head(out, n, &last, &len) || len == 0) {
            return 0;
        }
        memcpy(&space[filled], &out[NFC_TAG_READ_HEAD_SIZE], len);
        filled += len;
        if (last) {
            return filled == cap ? filled : 0;
        }
    }
    return 0; /* the space ran out of room without a last chunk */
}

/* Pull the whole space — which is what completes the read, because the last
 * chunk served is the one that reaches its end (§6.6, #48). The bytes are not
 * the point here, only that the read got there. */
static bool read_to_the_end(nfc_tag_t *nfc)
{
    uint8_t space[NFC_TAG_SERVED_SIZE];
    return pull_space(nfc, space, sizeof(space)) == sizeof(space);
}

/* The image as the console reads it back over the wire, past the framing head. */
static bool read_image(nfc_tag_t *nfc, uint8_t image[NFC_TAG_SIZE])
{
    uint8_t space[NFC_TAG_SERVED_SIZE];
    if (pull_space(nfc, space, sizeof(space)) != sizeof(space)) {
        return false;
    }
    memcpy(image, &space[NFC_TAG_SERVED_SIZE - NFC_TAG_SIZE], NFC_TAG_SIZE);
    return true;
}

/* --------------------------------------------------------------------- boot */

static void test_boot_state(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);

    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "a fresh device is idle (§4.8)");
    CHECK(!nfc_tag_placed(&nfc), "a fresh device has no tag placed");

    uint8_t out[128];
    CHECK(nfc_tag_command(&nfc, 0x0C, NULL, 0, out, sizeof(out)) == 0,
          "0x0C is the capability probe and is not the tag server's");
    CHECK(nfc_tag_command(&nfc, 0x99, NULL, 0, out, sizeof(out)) == 0,
          "an unknown subcommand has no response");
}

/* -------------------------------------------------------------------- 0x05 */

static void test_place_serves_the_tag(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    observer_t obs;
    observer_attach(&nfc, &obs);

    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    CHECK(nfc_tag_placed(&nfc), "place commits the tag");
    CHECK(nfc_tag_state(&nfc) == NFC_REPORT_COUNTER_MIN,
          "placement is the reader's first event: the byte is the counter (§4.9, #48)");
    CHECK(obs.count == 1 && obs.states[0] == NFC_REPORT_COUNTER_MIN,
          "the placement announces the report byte exactly once");

    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);
    const uint8_t want[NFC_TAG_UID_SIZE] = {0x04, 0x11, 0xFE, 0xCA, 0x52, 0x6C, 0x81};
    CHECK(memcmp(uid, want, sizeof(want)) == 0,
          "the identity skips BCC0 (§6.3): got %02x%02x%02x%02x%02x%02x%02x", uid[0], uid[1],
          uid[2], uid[3], uid[4], uid[5], uid[6]);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    size_t n = nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(n == NFC_STATUS_RESPONSE_SIZE, "0x05 replies its fixed 61 bytes, got %zu", n);
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED, "0x05 reports tag-detected, got 0x%02x", out[0]);
    CHECK(out[1] == 0x00 && out[2] == 0x00 && out[3] == 0x00 && out[4] == 0x01 && out[5] == 0x01 &&
              out[6] == 0x02 && out[7] == 0x00,
          "0x05 echoes the captured flags");
    CHECK(out[8] == 0x07, "0x05 declares a seven-byte UID, got 0x%02x", out[8]);
    CHECK(memcmp(&out[9], want, sizeof(want)) == 0, "0x05 carries the UID");
    CHECK(nfc_tag_state(&nfc) == NFC_REPORT_COUNTER_MIN,
          "answering 0x05 is not a reader event and leaves the byte alone");
}

static void test_status_without_a_tag(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    size_t n = nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(n == NFC_STATUS_RESPONSE_SIZE, "0x05 answers with no tag too");
    CHECK(out[0] == NFC_STATUS_NO_TAG, "0x05 reports no tag, got 0x%02x", out[0]);
    CHECK(out[8] == 0x00, "no UID length is claimed without a tag");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_POLLING,
          "a get-status with no tag leaves the console asking (console_polling)");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE,
          "...and leaves the report byte 0x00: only a tag drives it (§4.9)");
}

/* -------------------------------------------------------------------- 0x15 */

/* The capture's own numbers: an 81-byte answer, 8 + 3 + 70, over a 600-byte
 * served space whose first 60 bytes are the framing head
 * (`switch2_controller_research/commands.md:68`). */
static void test_read_serves_the_capture_space(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t space[NFC_TAG_SERVED_SIZE];
    CHECK(pull_space(&nfc, space, sizeof(space)) == sizeof(space),
          "the pulls reconstruct the whole %u-byte space", (unsigned)sizeof(space));
    CHECK(memcmp(&space[NFC_TAG_SERVED_SIZE - NFC_TAG_SIZE], tag, NFC_TAG_SIZE) == 0,
          "the image rides the served space verbatim");

#if !NFC_TAG_READ_PLAIN_VIEW
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);
    CHECK(space[0] == 0x04 && space[4] == 0x01 && space[5] == 0x02 && space[7] == 0x07,
          "the head opens with the reference layout");
    CHECK(memcmp(&space[8], uid, NFC_TAG_UID_SIZE) == 0,
          "the head carries the placed tag's seven-byte UID");
    CHECK(space[15] == 0x00 && space[16] == 0x00 && space[17] == 0x00 && space[18] == 0x00,
          "four zero bytes sit between the UID and the constant");
    CHECK(memcmp(&space[19], k_frame_constant, sizeof(k_frame_constant)) == 0,
          "the 32-byte constant sits at head offset 19");

    /* The capture's own exchange: the request at wire 0x46 answers image 0x0A. */
    uint8_t out[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    bool last = false;
    size_t len = 0;
    size_t n = read_wire(&nfc, 0x46, out, sizeof(out));
    CHECK(chunk_head(out, n, &last, &len) && !last && len == NFC_TAG_READ_CHUNK,
          "wire 0x46 carries one full chunk of 70, got %zu", n);
    CHECK(memcmp(&out[NFC_TAG_READ_HEAD_SIZE], &tag[0x0A], NFC_TAG_READ_CHUNK) == 0,
          "wire 0x46 serves image 0x0A — the capture's own exchange");

    /* The seam: a chunk that opens in the head and closes in the image. */
    n = read_wire(&nfc, (uint16_t)(NFC_TAG_FRAME_SIZE - 2), out, sizeof(out));
    CHECK(n == NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK, "the seam chunk is full, got %zu", n);
    CHECK(out[NFC_TAG_READ_HEAD_SIZE] == space[NFC_TAG_FRAME_SIZE - 2] &&
              memcmp(&out[NFC_TAG_READ_HEAD_SIZE + 2], tag, NFC_TAG_READ_CHUNK - 2) == 0,
          "a chunk bridges the head into the image");
#endif
}

static void test_read_chunks_stop_at_the_space(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    /* The space is not a multiple of the chunk, so the tail is short and the
     * only chunk that carries `last`. */
    const size_t tail = NFC_TAG_SERVED_SIZE % NFC_TAG_READ_CHUNK;
    uint8_t out[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    bool last = false;
    size_t len = 0;
    size_t n = read_wire(&nfc, (uint16_t)(NFC_TAG_SERVED_SIZE - tail), out, sizeof(out));
    CHECK(chunk_head(out, n, &last, &len) && last && len == tail,
          "the tail is served short (%zu) and flagged last, got %zu", tail, n);
    CHECK(n == NFC_TAG_READ_HEAD_SIZE + tail, "the tail's answer is head + %zu", tail);

    n = read_wire(&nfc, (uint16_t)(NFC_TAG_SERVED_SIZE - 1), out, sizeof(out));
    CHECK(chunk_head(out, n, &last, &len) && last && len == 1,
          "the last byte is a one-byte last chunk");

    CHECK(nfc_tag_command(&nfc, NFC_CMD_READ_BUFFER, (const uint8_t[]){0x01}, 1, out, sizeof(out)) ==
              0,
          "a truncated 0x15 request is refused");
}

#if !NFC_TAG_READ_PLAIN_VIEW
/* #46's criterion read as one sequence: the console's own `0x06`, then a whole
 * pull of the space, asserting all three regions at once — the image verbatim,
 * the 32-byte constant and the request's own nine bytes. (The comparison compile
 * has no head to carry, so it has nothing to assert here.) */
static void test_read_after_the_request_carries_all_three_regions(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);

    CHECK(nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL,
                          0) == 0,
          "0x06 answers with no payload");
    uint8_t space[NFC_TAG_SERVED_SIZE];
    CHECK(pull_space(&nfc, space, sizeof(space)) == sizeof(space),
          "the space reconstructs after the request");
    CHECK(memcmp(&space[NFC_TAG_SERVED_SIZE - NFC_TAG_SIZE], tag, NFC_TAG_SIZE) == 0,
          "the image rides the space verbatim past the head");
    CHECK(memcmp(&space[8], uid, NFC_TAG_UID_SIZE) == 0, "the head carries the UID");
    CHECK(memcmp(&space[19], k_frame_constant, sizeof(k_frame_constant)) == 0,
          "the head carries the 32-byte constant");
    CHECK(memcmp(&space[51], &k_read_request[10], 9) == 0,
          "the head echoes the request's own nine bytes at 51");
    CHECK(space[0] == 0x04 && space[7] == 0x07, "and the head's own bytes are intact");
}
#endif

static void test_read_head_echoes_its_own_request(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t space[NFC_TAG_SERVED_SIZE];
    CHECK(pull_space(&nfc, space, sizeof(space)) == sizeof(space), "the space is served");
#if !NFC_TAG_READ_PLAIN_VIEW
    CHECK(memcmp(&space[51], ((const uint8_t[9]){0}), 9) == 0,
          "with no 0x06 the head's last nine bytes are zero");

    /* The canonical full-tag read, then the head it produced. */
    CHECK(nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL,
                          0) == 0,
          "0x06 answers with no payload");
    CHECK(pull_space(&nfc, space, sizeof(space)) == sizeof(space), "the space is served again");
    CHECK(memcmp(&space[51], &k_read_request[10], 9) == 0,
          "the head echoes the request's own nine bytes at 51");
    CHECK(space[0] == 0x04 && space[7] == 0x07, "...and nothing else in the head moved");

    /* A stale echo cannot ride into the next placement: the head is rebuilt
     * per placement and per request. */
    nfc_tag_unplace(&nfc);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    CHECK(pull_space(&nfc, space, sizeof(space)) == sizeof(space), "the new placement serves");
    CHECK(memcmp(&space[51], ((const uint8_t[9]){0}), 9) == 0,
          "a new placement's head carries no stale request");
#endif
}

static void test_read_past_the_space_is_a_last_chunk(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    /* The bare marker, not silence: the console's third probe in the register
     * sessions (`0x2c0`) was answered with nothing and ended the cycle. */
    const uint16_t past[4] = {(uint16_t)NFC_TAG_SERVED_SIZE, (uint16_t)(NFC_TAG_SERVED_SIZE + 1),
                              0x2C0u, 0xFFFFu};
    for (size_t i = 0; i < 4; i++) {
        uint8_t out[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
        uint8_t req[2] = {(uint8_t)(past[i] & 0xFFu), (uint8_t)(past[i] >> 8)};
        size_t n = nfc_tag_command(&nfc, NFC_CMD_READ_BUFFER, req, sizeof(req), out, sizeof(out));
        CHECK(n == NFC_TAG_READ_HEAD_SIZE && out[0] == 0x01 && out[1] == 0x00 && out[2] == 0x00,
              "wire 0x%04x answers the bare last-chunk marker, got %zu", past[i], n);
    }
}

#if NFC_TAG_READ_PLAIN_VIEW
/* The comparison compile: the superseded reading, kept so the shapes can be
 * compared in one reflash. The image is at wire 0 and there is no head. */
static void test_the_comparison_view_serves_the_image_at_zero(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t out[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    size_t n = read_wire(&nfc, 0, out, sizeof(out));
    CHECK(n == NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK, "wire 0 is a full chunk, got %zu", n);
    CHECK(memcmp(&out[NFC_TAG_READ_HEAD_SIZE], tag, NFC_TAG_READ_CHUNK) == 0,
          "the plain view serves the image at wire 0");
    uint8_t space[NFC_TAG_SIZE];
    CHECK(pull_space(&nfc, space, sizeof(space)) == sizeof(space),
          "the plain space is the image's own 540 bytes");
    CHECK(memcmp(space, tag, NFC_TAG_SIZE) == 0, "...byte for byte");
    CHECK(read_wire(&nfc, NFC_TAG_SERVED_SIZE, out, sizeof(out)) == NFC_TAG_READ_HEAD_SIZE,
          "past the image the marker still answers — but the comparison build must not be\n"
          "flashed: it is a comparison, not a candidate (spec §6.6, #46)");
}
#endif

/* ------------------------------------------------ the status lifecycle (#48) */

/* The console's reader contract, as the second implementation reads it
 * (`ns_pc_control/server/src/s2_nfc_codec.cpp:731-900`, context tier) and as
 * this device's own bench corroborates: `0x06` arms a read and the status answer
 * says so — `0x04` for as long as the read is armed, answered as a *level* on
 * every `0x05` of the window — and `0x04` ends the scan, `0x09` when the read
 * never completed and `0x07` + detail `0x41` once it did. The flags, the UID
 * length and the UID keep their captured positions throughout. */
static void test_the_read_lifecycle_is_a_level(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x11);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    size_t n = nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(n == NFC_STATUS_RESPONSE_SIZE && out[0] == NFC_STATUS_TAG_DETECTED,
          "a placed tag answers 09 before the read is armed, got 0x%02x", out[0]);

    CHECK(nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL,
                          0) == 0,
          "0x06 answers with no payload");

    /* The level: every ask of the window carries the armed state, not only the
     * first — the console polls `0x05` repeatedly through the read. */
    for (int i = 0; i < 3; i++) {
        n = nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
        CHECK(n == NFC_STATUS_RESPONSE_SIZE && out[0] == NFC_STATUS_READ_ARMED,
              "ask %d of the armed window answers 04, got 0x%02x", i, out[0]);
        CHECK(memcmp(&out[1], ((const uint8_t[]){0x00, 0x00, 0x00, 0x01, 0x01, 0x02, 0x00}), 7) ==
                  0,
              "the captured flags ride the armed answer");
        CHECK(out[8] == 0x07 && memcmp(&out[9], uid, NFC_TAG_UID_SIZE) == 0,
              "the armed answer carries the UID at its captured position");
    }

    CHECK(read_to_the_end(&nfc), "the read reaches the final chunk");
    CHECK(nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0) == 0,
          "0x04 answers with no payload");
    n = nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(n == NFC_STATUS_RESPONSE_SIZE && out[0] == NFC_STATUS_EJECTED &&
              out[1] == NFC_STATUS_EJECTED_DETAIL,
          "a completed read's stop answers 07 + 41, got %02x %02x", out[0], out[1]);
    bool zeroed = true;
    for (size_t i = 2; i < NFC_STATUS_RESPONSE_SIZE; i++) {
        zeroed = zeroed && out[i] == 0x00;
    }
    CHECK(zeroed, "the post-eject answer carries nothing else");
}

/* The other half of the same rule: `0x04` after a scan whose read never reached
 * the end is *not* post-eject. It goes back to the tag-in-field `09`, which is
 * what the second implementation's `read_complete` gate says, and what every
 * no-crash bench cycle saw. */
static void test_an_incomplete_read_is_not_post_eject(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x11);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    uint8_t chunk[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL, 0);
    CHECK(read_wire(&nfc, wire_of(0), chunk, sizeof(chunk)) > 0,
          "one chunk of the read is served");
    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED,
          "a scan whose read never completed answers 09 after 0x04, got 0x%02x", out[0]);
    CHECK(out[1] == 0x00 && out[8] == 0x07, "and keeps the captured positions");
}

/* The bench's third probe (`0x2c0`) falls past the served space, and the bare
 * `01 00 00` marker is a well-formed "nothing more" (#46). It is **not** the
 * read's end: the reference's own handler refuses that ask outright
 * (`s2_nfc_codec.cpp:831-846` guards the marker path behind
 * `offset < op_buffer.size()`), and the ticket's rule is the final chunk. */
static void test_the_marker_past_the_space_is_not_the_reads_end(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x11);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    uint8_t chunk[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL, 0);
    CHECK(read_wire(&nfc, 0x2C0u, chunk, sizeof(chunk)) == NFC_TAG_READ_HEAD_SIZE &&
              chunk[0] == 0x01 && chunk[1] == 0x00 && chunk[2] == 0x00,
          "wire 0x2c0 answers the bare last-chunk marker");
    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED,
          "the marker does not complete the read, so the stop is still 09, got 0x%02x", out[0]);
}

/* The lifecycle is per scan, not per placement: a `0x03` re-arm puts an armed
 * read back to `09`, exactly as the reference's own `0x03` does, and a fresh
 * placement clears the post-eject state. */
static void test_the_lifecycle_is_per_scan(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x11);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_READ_ARMED, "the read is armed");
    nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED,
          "a 0x03 re-arm puts the status back to 09, got 0x%02x", out[0]);

    /* A completed read's eject is sticky until a new placement: the reader's
     * field is empty, and the console's next cycle is a new scan of a new tag. */
    nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL, 0);
    CHECK(read_to_the_end(&nfc), "the second read reaches the final chunk");
    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_EJECTED && out[1] == NFC_STATUS_EJECTED_DETAIL,
          "the completed scan is post-eject");
    nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_EJECTED,
          "a re-arm of the same placement stays ejected — nothing is in the field");

    nfc_tag_unplace(&nfc);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED && out[8] == 0x07,
          "a fresh placement answers 09 with its UID again");
}

/* -------------------------------------------- the report byte moves (#48) */

/* The other half of G-18's isolated pair: the HID report byte is the reader's
 * *event counter*, not the placement-only `0x00`/`0x02`, so it moves at every
 * stage of a read instead of resting on a value. The reference drives its own
 * byte the same way (`(previous + 1) & 0x07` on tag-presented / scan-ready /
 * operation-ready / write-complete / tag-removed,
 * `ns_pc_control/server/src/virtual_controller.cpp:195-266`, context tier); the
 * one difference here is the wrap, which goes `0x07 → 0x01` so that `0x00` stays
 * reserved for "no tag in field" and §4.9's mode meaning survives. */
static void test_the_report_byte_moves_with_the_read(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    observer_t obs;
    observer_attach(&nfc, &obs);

    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "no tag, no report byte");

    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    const uint8_t after_place = nfc_tag_state(&nfc);
    CHECK(after_place != NFC_STATE_IDLE, "the placement puts the byte on the wire");

    nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
    const uint8_t after_scan = nfc_tag_state(&nfc);
    nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL, 0);
    const uint8_t after_arm = nfc_tag_state(&nfc);
    CHECK(after_place != after_scan && after_scan != after_arm,
          "each reader event advances the byte: %02x -> %02x -> %02x", after_place, after_scan,
          after_arm);

    /* The armed window itself: the byte is not the value it held before the read
     * began, and the eject that ends it empties the reader's field — the byte
     * falls to `0x00`, which is not a value the counter can produce. */
    CHECK(read_to_the_end(&nfc), "the read reaches the final chunk");
    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    const uint8_t after_stop = nfc_tag_state(&nfc);
    CHECK(after_stop == NFC_STATE_IDLE && after_stop != after_arm,
          "the eject empties the field (%02x -> %02x)", after_arm, after_stop);

    /* A new placement puts a field back, and the counter's range is 0x01–0x07:
     * it wraps rather than passing through the reserved `0x00`. */
    nfc_tag_unplace(&nfc);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    for (size_t i = 0; i < 3 * NFC_REPORT_COUNTER_MAX; i++) {
        nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
        CHECK(nfc_tag_state(&nfc) >= NFC_REPORT_COUNTER_MIN &&
                  nfc_tag_state(&nfc) <= NFC_REPORT_COUNTER_MAX,
              "the counter stays inside 0x01..0x07, got 0x%02x", nfc_tag_state(&nfc));
    }

    nfc_tag_unplace(&nfc);
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE,
          "the tag leaving the field returns the byte to 00");
    CHECK(obs.states[obs.count - 1] == NFC_STATE_IDLE, "and the edge is announced");
}

/* The post-eject field, from the reader's side: `0x05` says nothing is in the
 * field, so nothing else may advertise a tag either — the HID byte, the
 * console's level, a `0x03` re-arm and a `0x15` read all agree with it. */
static void test_a_post_eject_field_is_empty_to_the_reader(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x11);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    uint8_t chunk[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL, 0);
    CHECK(read_to_the_end(&nfc), "the read reaches the final chunk");
    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);

    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_EJECTED, "the field is post-eject");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "and the HID byte says no tag in the field");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_POLLING,
          "the console is asking for a tag the field does not have");

    /* A re-arm of the same placement is a scan of an empty field: no reader
     * event, no level change, and the status still says post-eject. */
    const uint8_t before = nfc_tag_state(&nfc);
    nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
    CHECK(nfc_tag_state(&nfc) == before, "a re-arm of an empty field is not a reader event");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_POLLING, "and leaves the console asking");
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_EJECTED && out[1] == NFC_STATUS_EJECTED_DETAIL,
          "the post-eject answer survives the re-arm");

    /* And nothing may be read out of it. */
    CHECK(nfc_tag_command(&nfc, NFC_CMD_READ_DEVICE, k_read_request, sizeof(k_read_request), NULL,
                          0) == 0,
          "0x06 on an ejected field answers with no payload");
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_EJECTED, "...and arms nothing");
    CHECK(read_wire(&nfc, wire_of(0), chunk, sizeof(chunk)) == 0,
          "0x15 answers nothing on an ejected field");

    /* The placement itself still stands until the container says otherwise. */
    CHECK(nfc_tag_placed(&nfc), "the placement is committed, only the reader's field is empty");
}


static void test_read_buffer_needs_a_tag(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);

    uint8_t out[3 + NFC_TAG_READ_CHUNK];
    CHECK(read_buffer(&nfc, 0, out, sizeof(out)) == 0,
          "0x15 answers nothing while no tag is placed (§6.5's gap)");
}


/* ------------------------------------------------------------- place/unplace */

/*
 * §4.9's exclusivity: the report byte is the *mode's* expression, so a console
 * that polls while the device is in `IDLE` or `MACRO` must move
 * `STATUS.console_polling` and not the byte. The two are one vocabulary and two
 * signals, and collapsing them is how a `MACRO` would look like `AMIIBO` on the
 * wire.
 */
static void test_the_report_byte_is_mode_gated(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    observer_t obs;
    observer_attach(&nfc, &obs);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];

    /* A whole console exchange with no placement — the `IDLE`/`MACRO` case. */
    nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_POLLING, "console_polling tracks the console");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "the report byte stays 0x00 without a tag");
    CHECK(obs.count == 0, "no report-byte edge without a placement");

    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_IDLE, "0x04 drops console_polling to idle");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "...and still writes no report byte");
    CHECK(obs.scan_ended == 0, "there was no placed tag, so no SCAN_ENDED");

    /* The byte moves on a placement and back on an unplacement; a console that
     * is not asking writes nothing at all. */
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    CHECK(nfc_tag_state(&nfc) == NFC_REPORT_COUNTER_MIN, "place advances the byte counter");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_IDLE,
          "a placement does not invent a console that was not asking");
    CHECK(obs.count == 1 && obs.states[0] == NFC_REPORT_COUNTER_MIN,
          "the placement is the one byte edge");

    nfc_tag_unplace(&nfc);
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "unplace writes 0x00");
}

static void test_unplace_clears_the_state(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    observer_t obs;
    observer_attach(&nfc, &obs);

    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    nfc_tag_unplace(&nfc);

    CHECK(!nfc_tag_placed(&nfc), "unplace stops the tag answering");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "unplace returns the byte to 0x00 (§6.5)");
    CHECK(obs.states[obs.count - 1] == NFC_STATE_IDLE, "the unplacement is announced");

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_NO_TAG, "an unplaced tag is not reported");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE,
          "the get-status does not resurrect the report byte");

    /* Unplacing nothing is a no-op, so a `MACRO` exit cannot disturb a console. */
    size_t before = obs.count;
    nfc_tag_unplace(&nfc);
    nfc_tag_unplace(&nfc);
    CHECK(obs.count == before, "unplacing nothing announces nothing");
}

static void test_replace_emits_the_gap(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    observer_t obs;
    observer_attach(&nfc, &obs);

    uint8_t first[NFC_TAG_SIZE];
    uint8_t second[NFC_TAG_SIZE];
    build_tag(first, 0x20);
    build_tag(second, 0x40);
    second[4] = 0x11; /* a different identity, so the answer is distinguishable */

    const uint32_t t0 = 1000;
    nfc_tag_place(&nfc, first, sizeof(first), t0);
    CHECK(nfc_tag_placed(&nfc), "a first placement answers at once — nothing to remove");
    CHECK(obs.count == 1 && obs.states[0] == NFC_REPORT_COUNTER_MIN,
          "...with one byte edge");

    /* The atomic replace: the byte returns to 0x00 and the tag stops answering. */
    nfc_tag_place(&nfc, second, sizeof(second), t0 + 10);
    CHECK(obs.count == 2 && obs.states[1] == NFC_STATE_IDLE, "the replace opens the gap");
    CHECK(obs.placed_at_callback[1] == false, "the gap is observed tag-absent");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "the report byte is 0x00 for the gap");

    uint8_t status[NFC_STATUS_RESPONSE_SIZE];
    uint8_t page[3 + NFC_TAG_READ_CHUNK];
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, status, sizeof(status));
    CHECK(status[0] == NFC_STATUS_NO_TAG, "0x05 reports no tag during the gap");
    CHECK(read_buffer(&nfc, 0, page, sizeof(page)) == 0,
          "0x15 answers nothing during the gap");

    /* The gap is a deadline: early is early, due is due. */
    nfc_tag_tick(&nfc, t0 + 10 + NFC_TAG_GAP_MS - 1);
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "the gap is not closed early");
    nfc_tag_tick(&nfc, t0 + 10 + NFC_TAG_GAP_MS);
    CHECK(nfc_tag_state(&nfc) != NFC_STATE_IDLE, "the new tag answers at the deadline");
    CHECK(obs.count == 3 && obs.states[2] == nfc_tag_state(&nfc),
          "the gap closes with one tag-presented edge");

    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, status, sizeof(status));
    CHECK(status[0] == NFC_STATUS_TAG_DETECTED, "the new tag answers after the gap");
    const uint8_t want[NFC_TAG_UID_SIZE] = {0x04, 0x11, 0xFE, 0x11, 0x52, 0x6C, 0x81};
    CHECK(memcmp(&status[9], want, sizeof(want)) == 0,
          "the served UID is the new placement's");
}

/* ------------------------------------------------------------- polling edges */

static void test_start_and_stop_polling(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    observer_t obs;
    observer_attach(&nfc, &obs);

    /* `0x03` with no tag in the field says the console is asking — and moves no
     * report byte, because there is no tag and no `AMIIBO` (§4.9). */
    CHECK(nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0) == 0,
          "0x03 answers with no payload");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_POLLING, "0x03 with no tag reads polling (§3.2)");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_IDLE, "...and leaves the report byte 0x00");
    CHECK(obs.count == 0, "a poll with no tag writes no report byte");

    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    CHECK(nfc_tag_state(&nfc) == NFC_REPORT_COUNTER_MIN,
          "a placement while polling advances the byte");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_TAG_DETECTED,
          "the console's level follows the placement");

    /* `0x03` on a placed tag is the reader's scan-ready event: one more step. */
    const uint8_t before_scan = nfc_tag_state(&nfc);
    nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
    CHECK(nfc_tag_state(&nfc) > before_scan, "0x03 advances the report byte");

    CHECK(nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0) == 0,
          "0x04 answers with no payload");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_IDLE, "0x04 returns the console's level to idle");
    CHECK(nfc_tag_state(&nfc) != NFC_STATE_IDLE,
          "...but the tag is still placed, so the report byte stays on the wire");
    CHECK(obs.scan_ended == 1, "0x04 on a placed tag is one SCAN_ENDED, got %zu", obs.scan_ended);

    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    CHECK(obs.scan_ended == 1, "a second stop while idle is not another SCAN_ENDED");
}

static void test_scan_ended_needs_a_placed_tag(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    observer_t obs;
    observer_attach(&nfc, &obs);

    /* Polling with nothing to scan is not a scan that ended (§3.3: "stops polling
     * a placed tag"). */
    nfc_tag_command(&nfc, NFC_CMD_START_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    CHECK(obs.scan_ended == 0, "0x04 with no tag is not SCAN_ENDED, got %zu", obs.scan_ended);
}

/* ------------------------------------------------------------ 0x14 / 0x08 */

/* The canonical `0x14` chunk (`switch2_controller_research/commands.md:67`):
 * offset 0, declared `4c 00` = 76, and a staging stream opening `d0 07` ·
 * UID(7) · `01 00 01 04` · `ff ff ff ff` · `a5 f9 a5 00` · record count 3 ·
 * the first record's `05 20` (page 5, 32 bytes). The document truncates that
 * example at 24 staging bytes, so the record bodies below are the suite's;
 * every byte the document carries is the document's. 22 + 6 + 424 + 2 = 454,
 * which is why the stream ends with two padding bytes. */
#define WRITE_CHUNK 76u

static void build_write_stream(uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE],
                               const uint8_t uid[NFC_TAG_UID_SIZE])
{
    static const struct {
        uint8_t page, len, fill;
    } records[3] = {{5, 32, 0x11}, {32, 240, 0x22}, {92, 152, 0x33}};

    memset(stream, 0, NFC_TAG_WRITE_STAGING_SIZE);
    stream[0] = 0xD0;
    stream[1] = 0x07;
    memcpy(&stream[2], uid, NFC_TAG_UID_SIZE);
    stream[9] = 0x01;
    stream[10] = 0x00;
    stream[11] = 0x01;
    stream[12] = 0x04;
    memset(&stream[13], 0xFF, 4);
    stream[17] = 0xA5; /* the write counter, copied to image[16..19] */
    stream[18] = 0xF9;
    stream[19] = 0xA5;
    stream[20] = 0x00;
    stream[NFC_TAG_WRITE_RECORD_COUNT_OFFSET] = 3;

    size_t cursor = NFC_TAG_WRITE_RECORD_COUNT_OFFSET + 1u;
    for (size_t i = 0; i < 3; i++) {
        stream[cursor++] = records[i].page;
        stream[cursor++] = records[i].len;
        memset(&stream[cursor], records[i].fill, records[i].len);
        cursor += records[i].len;
    }
}

/* Stage a whole stream in the capture's own chunks of 76. `skip` withholds one
 * chunk index, or is `(size_t)-1` for a complete stream. */
static size_t stage_stream(nfc_tag_t *nfc, const uint8_t *stream, size_t total, size_t skip)
{
    size_t sent = 0;
    size_t chunk_index = 0;
    uint8_t chunk[4 + WRITE_CHUNK];
    while (sent < total) {
        size_t n = total - sent;
        if (n > WRITE_CHUNK) {
            n = WRITE_CHUNK;
        }
        if (chunk_index != skip) {
            chunk[0] = (uint8_t)(sent & 0xFFu);
            chunk[1] = (uint8_t)(sent >> 8);
            chunk[2] = (uint8_t)(n & 0xFFu);
            chunk[3] = (uint8_t)(n >> 8);
            memcpy(&chunk[4], &stream[sent], n);
            CHECK(nfc_tag_command(nfc, NFC_CMD_WRITE_BUFFER, chunk, 4u + n, NULL, 0) == 0,
                  "0x14 answers with no payload");
        }
        sent += n;
        chunk_index++;
    }
    return sent;
}

/* A fresh placement on the same server: the write-back is dropped on unplace
 * (§6.5), so each case starts with no stream and no committed status. */
static void replace_placement(nfc_tag_t *nfc, const uint8_t *tag, uint32_t at)
{
    nfc_tag_unplace(nfc);
    nfc_tag_place(nfc, tag, NFC_TAG_SIZE, at);
}

static bool image_is(nfc_tag_t *nfc, const uint8_t *want)
{
    uint8_t image[NFC_TAG_SIZE];
    return read_image(nfc, image) && memcmp(image, want, NFC_TAG_SIZE) == 0;
}

static void test_the_captured_write_commits(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);

    uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];
    build_write_stream(stream, uid);
    CHECK(stage_stream(&nfc, stream, sizeof(stream), (size_t)-1) == NFC_TAG_WRITE_STAGING_SIZE,
          "the 454-byte stream stages in six chunks");
    CHECK(nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0) == 0,
          "0x08 answers with no payload");

    uint8_t image[NFC_TAG_SIZE];
    CHECK(read_image(&nfc, image), "the committed image is readable over 0x15");
    CHECK(memcmp(&image[16], ((const uint8_t[]){0xA5, 0xF9, 0xA5, 0x00}), 4) == 0,
          "the staging header's four bytes landed at image 16");
    bool ok = true;
    for (size_t i = 20; i < 52; i++) {
        ok = ok && image[i] == 0x11;
    }
    CHECK(ok, "record 1 wrote 32 bytes at page 5");
    ok = true;
    for (size_t i = 128; i < 368; i++) {
        ok = ok && image[i] == 0x22;
    }
    CHECK(ok, "record 2 wrote 240 bytes at page 32");
    ok = true;
    for (size_t i = 368; i < 520; i++) {
        ok = ok && image[i] == 0x33;
    }
    CHECK(ok, "record 3 wrote 152 bytes at page 92");
    CHECK(memcmp(&image[0], &tag[0], 16) == 0, "the identity and lock pages are never written");
    CHECK(memcmp(&image[52], &tag[52], 128 - 52) == 0, "the gap between the records is untouched");
    CHECK(memcmp(&image[520], &tag[520], NFC_TAG_SIZE - 520) == 0,
          "past the last record is untouched");

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    size_t n = nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(n == NFC_STATUS_RESPONSE_SIZE && out[0] == NFC_STATUS_WRITE_COMMITTED,
          "0x05 reports the commit, got 0x%02x", out[0]);
    CHECK(out[8] == 0x07 && memcmp(&out[9], uid, NFC_TAG_UID_SIZE) == 0,
          "the committed answer still carries the UID");
}

static void test_a_partial_stream_refuses_the_commit(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);
    uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];
    build_write_stream(stream, uid);

    /* One byte short: coverage is the gate, not the byte count. */
    stage_stream(&nfc, stream, sizeof(stream) - 1u, (size_t)-1);
    nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0);
    CHECK(image_is(&nfc, tag), "a one-byte-short stream leaves the image untouched");
    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED, "no commit, so 0x05 still reports the tag");

    /* A withheld middle chunk is the same refusal, with the rest complete. */
    replace_placement(&nfc, tag, g_now++);
    stage_stream(&nfc, stream, sizeof(stream), 2);
    nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0);
    CHECK(image_is(&nfc, tag), "a missing chunk leaves the image untouched");
}

static void test_a_foreign_uid_refuses_the_commit(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);

    uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];
    build_write_stream(stream, uid);
    stream[8] ^= 0x01; /* the staging UID is not the placed tag's */
    stage_stream(&nfc, stream, sizeof(stream), (size_t)-1);
    nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0);
    CHECK(image_is(&nfc, tag), "a staging UID that is not the placed tag's refuses the commit");
}

static void test_bad_records_refuse_the_commit(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    nfc_tag_identity(&nfc, uid);

    uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];

    /* Each case perturbs one field of an otherwise complete stream; the commit
     * must refuse without touching a byte of the image. */
    static const struct {
        const char *what;
        size_t at;
        uint8_t value;
    } cases[] = {
        {"a staging stream that is not the captured frame", 0, 0xD1},
        {"a record count of zero", NFC_TAG_WRITE_RECORD_COUNT_OFFSET, 0x00},
        {"a record count past the reference's bound", NFC_TAG_WRITE_RECORD_COUNT_OFFSET, 0x11},
        {"a record that targets page 0", 22, 0x00},
        {"a record with a zero length", 23, 0x00},
        {"a record that runs past the image", 22, 0x86},
        {"a non-zero byte after the last record", NFC_TAG_WRITE_STAGING_SIZE - 1u, 0x01},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        build_write_stream(stream, uid);
        stream[cases[i].at] = cases[i].value;
        replace_placement(&nfc, tag, g_now++);
        stage_stream(&nfc, stream, sizeof(stream), (size_t)-1);
        nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0);
        CHECK(image_is(&nfc, tag), "%s refuses the commit", cases[i].what);
    }
}

static void test_staging_ignores_what_it_cannot_hold(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);

    /* A declared length beyond what arrived is clamped to the arrival. */
    const uint8_t lying[5] = {0x00, 0x00, 0x40, 0x00, 0x00};
    CHECK(nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, lying, sizeof(lying), NULL, 0) == 0,
          "an over-long 0x14 is accepted and clamped");
    /* An offset past the staging stream stages nothing. */
    const uint8_t past[5] = {NFC_TAG_WRITE_STAGING_SIZE & 0xFFu,
                             NFC_TAG_WRITE_STAGING_SIZE >> 8, 0x01, 0x00, 0x55};
    nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, past, sizeof(past), NULL, 0);
    /* A truncated request is dropped rather than read past. */
    const uint8_t truncated[3] = {0x00, 0x00, 0x04};
    nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, truncated, sizeof(truncated), NULL, 0);

    /* None of the three wrote into the stream: the capture's own stream still
     * stages and commits afterwards. */
    uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];
    build_write_stream(stream, uid);
    stage_stream(&nfc, stream, sizeof(stream), (size_t)-1);
    nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0);
    uint8_t image[NFC_TAG_SIZE];
    CHECK(read_image(&nfc, image) && image[20] == 0x11,
          "a stream staged after the bad chunks still commits");
}

static void test_the_write_dies_with_the_placement(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    uint8_t fresh[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    build_tag(fresh, 0x60);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);
    uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];
    build_write_stream(stream, uid);
    stage_stream(&nfc, stream, sizeof(stream), (size_t)-1);
    nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0);

    uint8_t out[NFC_STATUS_RESPONSE_SIZE];
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_WRITE_COMMITTED,
          "the commit is reported while the placement stands");

    /* The console stopping the scan ends the write's *status*; the bytes stay
     * until the placement does (§6.5: the write is volatile, not reverted). */
    nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED, "0x04 ends the committed status");

    /* Asked directly, because the criterion is about the *placement* edge: the
     * reads stop there, and re-placing the same tag serves its virgin bytes. */
    nfc_tag_unplace(&nfc);
    uint8_t chunk[NFC_TAG_READ_HEAD_SIZE + NFC_TAG_READ_CHUNK];
    CHECK(read_wire(&nfc, wire_of(NFC_TAG_WRITE_HEADER_TARGET), chunk, sizeof(chunk)) == 0,
          "0x15 answers nothing after unplace");
    nfc_tag_place(&nfc, tag, NFC_TAG_SIZE, g_now++);
    uint8_t image[NFC_TAG_SIZE];
    CHECK(read_image(&nfc, image) && memcmp(image, tag, NFC_TAG_SIZE) == 0,
          "re-placing the same tag serves its own bytes, not the write's");

    /* The next placement is virgin by design. */
    replace_placement(&nfc, fresh, g_now++);
    nfc_tag_command(&nfc, NFC_CMD_GET_STATUS, NULL, 0, out, sizeof(out));
    CHECK(out[0] == NFC_STATUS_TAG_DETECTED, "a new placement answers 09");
    CHECK(image_is(&nfc, fresh), "a new placement's bytes are its own, not the write's");
}

/* Page 4 is the page the frame's own header word writes, so the commit's order
 * — header first, then records — is observable rather than implied: a record
 * aimed at page 4 lands after the header word and wins. That is the order the
 * reference implementation applies, and the only one the capture can have meant. */
static void test_a_page_four_record_lands_after_the_header_word(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    uint8_t uid[NFC_TAG_UID_SIZE];
    nfc_tag_identity(&nfc, uid);

    uint8_t stream[NFC_TAG_WRITE_STAGING_SIZE];
    build_write_stream(stream, uid);
    stream[22] = 4; /* record 1 targets page 4 instead of page 5 */
    stage_stream(&nfc, stream, sizeof(stream), (size_t)-1);
    CHECK(nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0) == 0,
          "0x08 answers with no payload");

    uint8_t image[NFC_TAG_SIZE];
    CHECK(read_image(&nfc, image), "the committed image is readable over 0x15");
    CHECK(memcmp(&image[16], ((const uint8_t[]){0x11, 0x11, 0x11, 0x11}), 4) == 0,
          "the page-4 record overrode the header word at image 16");
}

static void test_write_needs_a_tag(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);

    const uint8_t chunk[8] = {0x10, 0x00, 0x04, 0x00, 0xDE, 0xAD, 0xBE, 0xEF};
    CHECK(nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, chunk, sizeof(chunk), NULL, 0) == 0,
          "0x14 with no tag is discarded, not refused");
    CHECK(nfc_tag_command(&nfc, NFC_CMD_COMMIT_WRITE, NULL, 0, NULL, 0) == 0,
          "0x08 with no tag is discarded too");

    /* A placement after the discarded stream is untouched by it. */
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    CHECK(image_is(&nfc, tag), "the discarded stream left the next placement alone");
}

/* --------------------------------------------------------------------- main */

int main(void)
{
    test_boot_state();
    test_place_serves_the_tag();
    test_status_without_a_tag();
    test_read_serves_the_capture_space();
    test_read_chunks_stop_at_the_space();
#if !NFC_TAG_READ_PLAIN_VIEW
    test_read_after_the_request_carries_all_three_regions();
#endif
    test_read_head_echoes_its_own_request();
    test_read_past_the_space_is_a_last_chunk();
    test_read_buffer_needs_a_tag();
#if NFC_TAG_READ_PLAIN_VIEW
    test_the_comparison_view_serves_the_image_at_zero();
#endif
    test_the_read_lifecycle_is_a_level();
    test_an_incomplete_read_is_not_post_eject();
    test_the_marker_past_the_space_is_not_the_reads_end();
    test_the_lifecycle_is_per_scan();
    test_the_report_byte_moves_with_the_read();
    test_a_post_eject_field_is_empty_to_the_reader();
    test_unplace_clears_the_state();
    test_the_report_byte_is_mode_gated();
    test_replace_emits_the_gap();
    test_start_and_stop_polling();
    test_scan_ended_needs_a_placed_tag();
    test_the_captured_write_commits();
    test_a_partial_stream_refuses_the_commit();
    test_a_foreign_uid_refuses_the_commit();
    test_bad_records_refuse_the_commit();
    test_staging_ignores_what_it_cannot_hold();
    test_a_page_four_record_lands_after_the_header_word();
    test_the_write_dies_with_the_placement();
    test_write_needs_a_tag();

    if (g_failures != 0) {
        fprintf(stderr, "%d/%d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("nfc tag: %d checks, OK\n", g_checks);
    return 0;
}
