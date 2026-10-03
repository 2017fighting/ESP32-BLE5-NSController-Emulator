/*
 * Host-side assertions for the NFC state machine and the tag server (spec §4.9,
 * §6.5, §6.6, §7.3 step 6; issue #25).
 *
 * The acceptance property of the ticket lives here: a placed tag is served page
 * by page, the state byte reflects placement, unplacement and the tag-absent gap,
 * the console's polling edges drive `SCAN_ENDED`, and a console write-back is
 * taken into the volatile tag and dropped on unplace. None of it needs a device
 * or a console.
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
    uint8_t states[16];
    size_t count;
    size_t scan_ended;
    /* When `probe`, every callback records `nfc_tag_placed()` at that instant —
     * which is how the test proves the gap is not merely ordered but *seen* as
     * tag-absent. */
    bool placed_at_callback[16];
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

static size_t read_buffer(nfc_tag_t *nfc, uint16_t offset, uint8_t *out, size_t cap)
{
    const uint8_t req[2] = {(uint8_t)(offset & 0xFFu), (uint8_t)(offset >> 8)};
    return nfc_tag_command(nfc, NFC_CMD_READ_BUFFER, req, sizeof(req), out, cap);
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
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_TAG_DETECTED,
          "placement drives the state byte to tag-detected (§4.9)");
    CHECK(obs.count == 1 && obs.states[0] == NFC_STATE_TAG_DETECTED,
          "the placement announces the state byte exactly once");

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
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_TAG_DETECTED,
          "answering 0x05 leaves a placed tag tag-detected");
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

static void test_read_buffer_slices_page_wise(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    uint8_t out[3 + NFC_TAG_READ_CHUNK];
    size_t n = read_buffer(&nfc, 0, out, sizeof(out));
    CHECK(n == 3 + NFC_TAG_READ_CHUNK, "0x15 serves a 64-byte chunk, got %zu", n);
    CHECK(out[0] == 0x00 && out[1] == 0x00 && out[2] == 0x00,
          "0x15 echoes the requested offset behind a leading zero");
    CHECK(memcmp(&out[3], tag, NFC_TAG_READ_CHUNK) == 0, "0x15 serves the image from the offset");

    n = read_buffer(&nfc, 0x40, out, sizeof(out));
    CHECK(n == 3 + NFC_TAG_READ_CHUNK, "a second page-aligned read is a full chunk");
    CHECK(out[1] == 0x40 && out[2] == 0x00, "the echo carries the offset back");
    CHECK(memcmp(&out[3], &tag[0x40], NFC_TAG_READ_CHUNK) == 0, "the slice starts at 0x40");

    /* The last partial chunk is 540 - 512 = 28 bytes, not a padded 64. */
    n = read_buffer(&nfc, 512, out, sizeof(out));
    CHECK(n == 3 + 28, "the tail is served short, got %zu", n);
    CHECK(memcmp(&out[3], &tag[512], 28) == 0, "the tail carries the last 28 bytes");

    CHECK(read_buffer(&nfc, NFC_TAG_SIZE, out, sizeof(out)) == 0,
          "an offset at the image's end has nothing to serve");
    CHECK(nfc_tag_command(&nfc, NFC_CMD_READ_BUFFER, (const uint8_t[]){0x01}, 1, out, sizeof(out)) ==
              0,
          "a truncated 0x15 request is refused");
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

    /* Only a placement moves the byte, and only an unplacement moves it back. */
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_TAG_DETECTED, "place writes 0x02");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_IDLE,
          "a placement does not invent a console that was not asking");
    CHECK(obs.count == 1 && obs.states[0] == NFC_STATE_TAG_DETECTED,
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
    CHECK(obs.count == 1 && obs.states[0] == NFC_STATE_TAG_DETECTED,
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
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_TAG_DETECTED, "the new tag answers at the deadline");
    CHECK(obs.count == 3 && obs.states[2] == NFC_STATE_TAG_DETECTED,
          "the gap closes with one tag-detected edge");

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
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_TAG_DETECTED,
          "a placement while polling reads tag-detected");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_TAG_DETECTED,
          "the console's level follows the placement");

    CHECK(nfc_tag_command(&nfc, NFC_CMD_STOP_POLLING, NULL, 0, NULL, 0) == 0,
          "0x04 answers with no payload");
    CHECK(nfc_tag_polling(&nfc) == NFC_STATE_IDLE, "0x04 returns the console's level to idle");
    CHECK(nfc_tag_state(&nfc) == NFC_STATE_TAG_DETECTED,
          "...but the tag is still placed, so the report byte stays tag-detected");
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

/* -------------------------------------------------------------------- 0x14 */

static void test_write_buffer_is_volatile(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    /* `00 10` offset 0x10, `04 00` length 4, then the bytes. */
    const uint8_t req[8] = {0x10, 0x00, 0x04, 0x00, 0xDE, 0xAD, 0xBE, 0xEF};
    CHECK(nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, req, sizeof(req), NULL, 0) == 0,
          "0x14 answers with no payload");

    uint8_t out[3 + NFC_TAG_READ_CHUNK];
    read_buffer(&nfc, 0x10, out, sizeof(out));
    const uint8_t written[4] = {0xDE, 0xAD, 0xBE, 0xEF};
    CHECK(memcmp(&out[3], written, sizeof(written)) == 0,
          "the write-back is served while the tag is placed");

    /* The next placement is virgin by design, so the write-back is gone. */
    nfc_tag_unplace(&nfc);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);
    read_buffer(&nfc, 0x10, out, sizeof(out));
    CHECK(memcmp(&out[3], &tag[0x10], sizeof(written)) == 0,
          "a write-back is discarded on unplace (§6.5)");
}

static void test_write_buffer_is_bounded(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);
    uint8_t tag[NFC_TAG_SIZE];
    build_tag(tag, 0x20);
    nfc_tag_place(&nfc, tag, sizeof(tag), g_now++);

    /* A length that overruns what arrived is clamped, not read past. */
    const uint8_t lying[5] = {0x00, 0x00, 0x40, 0x00, 0xAA};
    CHECK(nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, lying, sizeof(lying), NULL, 0) == 0,
          "an over-long 0x14 is accepted and clamped");
    uint8_t out[3 + NFC_TAG_READ_CHUNK];
    read_buffer(&nfc, 0, out, sizeof(out));
    CHECK(out[3] == 0xAA, "the one byte that did arrive landed at offset 0");
    CHECK(out[4] == tag[1], "the bytes that did not arrive were left alone");

    /* An offset past the image writes nothing. */
    const uint8_t past[5] = {NFC_TAG_SIZE & 0xFFu, NFC_TAG_SIZE >> 8, 0x01, 0x00, 0x55};
    nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, past, sizeof(past), NULL, 0);
    read_buffer(&nfc, NFC_TAG_SIZE - 4, out, sizeof(out));
    CHECK(memcmp(&out[3], &tag[NFC_TAG_SIZE - 4], 4) == 0, "an out-of-range write is dropped");

    /* A truncated request is dropped rather than read past. */
    const uint8_t truncated[3] = {0x00, 0x00, 0x04};
    nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, truncated, sizeof(truncated), NULL, 0);
    CHECK(true, "a truncated 0x14 does not read past its payload");
}

static void test_write_buffer_needs_a_tag(void)
{
    nfc_tag_t nfc;
    nfc_tag_init(&nfc);

    const uint8_t req[8] = {0x10, 0x00, 0x04, 0x00, 0xDE, 0xAD, 0xBE, 0xEF};
    CHECK(nfc_tag_command(&nfc, NFC_CMD_WRITE_BUFFER, req, sizeof(req), NULL, 0) == 0,
          "0x14 with no tag is discarded, not refused");
}

/* --------------------------------------------------------------------- main */

int main(void)
{
    test_boot_state();
    test_place_serves_the_tag();
    test_status_without_a_tag();
    test_read_buffer_slices_page_wise();
    test_read_buffer_needs_a_tag();
    test_unplace_clears_the_state();
    test_the_report_byte_is_mode_gated();
    test_replace_emits_the_gap();
    test_start_and_stop_polling();
    test_scan_ended_needs_a_placed_tag();
    test_write_buffer_is_volatile();
    test_write_buffer_is_bounded();
    test_write_buffer_needs_a_tag();

    if (g_failures != 0) {
        fprintf(stderr, "%d/%d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("nfc tag: %d checks, OK\n", g_checks);
    return 0;
}
