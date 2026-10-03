/*
 * Host-side assertions for the NFC console-traffic trace (spec §6.6, §12.2
 * validations 4 and 5; issue #36).
 *
 * The trace's product is the *text the bench parses*, so that is what this
 * suite pins: the line grammar, the drain order, the coalescing that keeps a
 * held-open scan screen from overflowing the ring, the drop accounting, and
 * the CRC the host will re-compute over its own reconstruction of a served
 * response. None of it needs a device or a console.
 *
 * Build:
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include -Itest/host \
 *      -o test_nfc_trace test/host/test_nfc_trace.c \
 *      main/src/protocol/control/nfc_trace.c \
 *      main/src/protocol/control/control_frame.c
 * Run:
 *   ./test_nfc_trace
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "protocol/control/control_protocol.h"
#include "protocol/control/nfc_trace.h"

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

/* Drains the whole trace and returns the lines, so order is assertable. */
static size_t drain(nfc_trace_t *t, char lines[][NFC_TRACE_LINE_MAX], size_t max)
{
    size_t count = 0;
    while (count < max) {
        size_t n = nfc_trace_format(t, lines[count], NFC_TRACE_LINE_MAX);
        if (n == 0) {
            break;
        }
        lines[count][n] = '\0';
        count++;
    }
    return count;
}

static bool starts_with(const char *line, const char *prefix)
{
    return strncmp(line, prefix, strlen(prefix)) == 0;
}

static bool contains(const char *line, const char *needle)
{
    return strstr(line, needle) != NULL;
}

static uint16_t le16(uint16_t v, uint8_t *p)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)(v >> 8);
    return v;
}

/* ---------------------------------------------------------- the line grammar */

static void test_the_five_shapes(void)
{
    nfc_trace_t t;
    nfc_trace_init(&t);
    uint8_t payload[16];
    uint8_t rsp[80];

    /* 0x03 with the captured configuration. */
    const uint8_t cfg[5] = {0x00, 0xe8, 0x03, 0x2c, 0x01};
    nfc_trace_feed(&t, 0x03, cfg, sizeof(cfg), NULL, 0, 4312);

    /* 0x05: the served response carries the status byte the console branched
     * on, so the line has to carry it too. */
    memset(rsp, 0, sizeof(rsp));
    rsp[0] = 0x09;
    rsp[8] = 0x07;
    nfc_trace_feed(&t, 0x05, NULL, 0, rsp, 61, 4314);

    /* 0x06: length only — the 19-byte configuration is not a measurement. */
    nfc_trace_feed(&t, 0x06, payload, 19, NULL, 0, 4315);

    /* 0x14: offset + declared length + captured bytes. */
    uint8_t write[12];
    le16(0x0000, write);
    le16(4, &write[2]);
    memcpy(&write[4], "\xd0\x07\x04\x8a", 4);
    nfc_trace_feed(&t, 0x14, write, 8, NULL, 0, 4318);

    /* 0x15: the wire offset asked and the response served. */
    le16(0x0046, payload);
    rsp[0] = 0x00;
    rsp[1] = 0x46;
    rsp[2] = 0x00;
    memset(&rsp[3], 0xAB, 64);
    nfc_trace_feed(&t, 0x15, payload, 2, rsp, 67, 4320);

    /* 0x04: the scan's close. */
    nfc_trace_feed(&t, 0x04, NULL, 0, NULL, 0, 4390);

    char lines[8][NFC_TRACE_LINE_MAX];
    size_t count = drain(&t, lines, 8);
    CHECK(count == 7, "six events plus the summary drain as seven lines, got %zu", count);
    if (count != 7) {
        return;
    }
    CHECK(starts_with(lines[0], "console nfc: t=4312 sub=03 len=5 cfg=00e8032c01"),
          "the 0x03 line is the captured shape: '%s'", lines[0]);
    CHECK(contains(lines[1], "sub=05 status=09 n=61 crc="), "the 0x05 line names what was served: '%s'",
          lines[1]);
    CHECK(contains(lines[2], "sub=06 len=19"), "the 0x06 line is a length: '%s'", lines[2]);
    CHECK(contains(lines[3], "sub=14 off=0000 want=4 got=4 crc="),
          "the 0x14 line names the write's offset and bytes: '%s'", lines[3]);
    CHECK(contains(lines[3], "data=d007048a"), "the 0x14 line carries the written bytes: '%s'",
          lines[3]);
    CHECK(contains(lines[4], "sub=15 off=0046 n=67 crc="),
          "the 0x15 line names the wire offset asked: '%s'", lines[4]);
    CHECK(starts_with(lines[5], "console nfc: t=4390 sub=04"), "the 0x04 line is the scan's close: '%s'",
          lines[5]);
    CHECK(starts_with(lines[6], "console nfc: scan cmds=6 [03=1 04=1 05=1 06=1 14=1 15=1] reps=0 drops=0"),
          "the summary counts every subcommand: '%s'", lines[6]);
}

/* ------------------------------------------------------- the CRC is checkable */

static void test_the_crc_is_over_what_was_served(void)
{
    nfc_trace_t t;
    nfc_trace_init(&t);

    /* A 0x15 response is `00 · offset LE · slice`; the host's reconstruction of
     * the same bytes must CRC to the line's value, or the bench's integrity
     * check is checking nothing. */
    uint8_t payload[2] = {0x46, 0x00};
    uint8_t rsp[67] = {0x00, 0x46, 0x00};
    for (int i = 0; i < 64; i++) {
        rsp[3 + i] = (uint8_t)i;
    }
    nfc_trace_feed(&t, 0x15, payload, sizeof(payload), rsp, sizeof(rsp), 100);

    char line[NFC_TRACE_LINE_MAX];
    size_t n = nfc_trace_format(&t, line, sizeof(line));
    CHECK(n > 0, "a fed 0x15 formats");
    uint16_t want = control_crc16_update(0xffffu, rsp, sizeof(rsp));
    char crc_text[16];
    snprintf(crc_text, sizeof(crc_text), "crc=%04x", want);
    CHECK(contains(line, crc_text), "the line's crc is §2.2's over the whole response: '%s' wants %s",
          line, crc_text);

    /* The 0x14 crc is over the captured write bytes, not the request head. */
    nfc_trace_init(&t);
    uint8_t write[8];
    le16(0x0008, write);
    le16(4, &write[2]);
    memcpy(&write[4], "\x01\x02\x03\x04", 4);
    nfc_trace_feed(&t, 0x14, write, sizeof(write), NULL, 0, 101);
    n = nfc_trace_format(&t, line, sizeof(line));
    CHECK(n > 0, "a fed 0x14 formats");
    snprintf(crc_text, sizeof(crc_text), "crc=%04x", control_crc16_update(0xffffu, &write[4], 4));
    CHECK(contains(line, crc_text), "the 0x14 crc is over the data bytes: '%s' wants %s", line,
          crc_text);
}

/* ------------------------------------------------------------- the coalescing */

static void test_repeats_coalesce_on_the_key(void)
{
    nfc_trace_t t;
    nfc_trace_init(&t);
    uint8_t rsp[61];

    /* A scan screen held open: the console asks 0x05 with no tag over and
     * over, then the placement lands and it asks with a tag. Two distinct
     * answers, not one entry whose meaning changed, and not 30 entries. */
    memset(rsp, 0, sizeof(rsp));
    rsp[0] = 0x00;
    for (int i = 0; i < 29; i++) {
        nfc_trace_feed(&t, 0x05, NULL, 0, rsp, 61, 1000 + (uint32_t)i);
    }
    rsp[0] = 0x09;
    nfc_trace_feed(&t, 0x05, NULL, 0, rsp, 61, 1300);

    char lines[4][NFC_TRACE_LINE_MAX];
    size_t count = drain(&t, lines, 4);
    CHECK(count == 3, "two distinct 0x05 answers and a summary, got %zu", count);
    if (count == 3) {
        CHECK(contains(lines[0], "status=00") && contains(lines[0], "reps=28"),
              "the waiting ask keeps its first timestamp and counts its repeats: '%s'", lines[0]);
        CHECK(contains(lines[0], "t=1000 "), "the entry says when the console first asked: '%s'",
              lines[0]);
        CHECK(contains(lines[1], "status=09") && contains(lines[1], "reps=0"),
              "the detected ask is its own entry: '%s'", lines[1]);
        CHECK(contains(lines[2], "cmds=30") && contains(lines[2], "05=30") &&
              contains(lines[2], "reps=28"),
              "the summary counts commands and repeats separately: '%s'", lines[2]);
    }}

static void test_the_ring_counts_its_drops(void)
{
    nfc_trace_t t;
    nfc_trace_init(&t);
    uint8_t payload[2];

    /* 20 distinct read offsets into a 16-slot ring: four are counted as
     * dropped, not silently evicted — a lost offset is a lost measurement. */
    for (unsigned i = 0; i < 20; i++) {
        le16((uint16_t)(i * 4u), payload);
        nfc_trace_feed(&t, 0x15, payload, 2, NULL, 0, 2000 + i);
    }
    char lines[NFC_TRACE_RING + 2][NFC_TRACE_LINE_MAX];
    size_t count = drain(&t, lines, NFC_TRACE_RING + 2);
    CHECK(count == NFC_TRACE_RING + 1, "16 events plus the summary, got %zu", count);
    if (count == NFC_TRACE_RING + 1) {
        CHECK(contains(lines[count - 1], "drops=4"), "the summary owns the drop count: '%s'",
              lines[count - 1]);
    }
    /* And the counts stay honest: all 20 commands, not the 16 that fit. */
    CHECK(contains(lines[count - 1], "15=20"), "the counter counts commands, not entries: '%s'",
          lines[count - 1]);
}

/* ------------------------------------------------------------ the drain's ends */

static void test_an_empty_trace_owes_nothing(void)
{
    nfc_trace_t t;
    nfc_trace_init(&t);
    char line[NFC_TRACE_LINE_MAX];
    CHECK(nfc_trace_format(&t, line, sizeof(line)) == 0,
          "a never-fed trace formats nothing — no empty summary");
    nfc_trace_feed(&t, 0x04, NULL, 0, NULL, 0, 5);
    size_t n = nfc_trace_format(&t, line, sizeof(line));
    CHECK(n > 0 && starts_with(line, "console nfc: t=5 sub=04"), "a lone 0x04 is one line: '%s'",
          line);
    n = nfc_trace_format(&t, line, sizeof(line));
    CHECK(n > 0 && starts_with(line, "console nfc: scan cmds=1 [03=0 04=1 05=0 06=0 14=0 15=0]"),
          "its summary follows: '%s'", line);
    CHECK(nfc_trace_format(&t, line, sizeof(line)) == 0, "the drain then ends");
    /* The reset is real: a second scan starts at zero. */
    uint8_t payload[2] = {0x46, 0x00};
    nfc_trace_feed(&t, 0x15, payload, 2, NULL, 0, 6);
    n = nfc_trace_format(&t, line, sizeof(line));
    CHECK(n > 0 && contains(line, "sub=15") && !contains(line, "04=1"),
          "the trace after a drain carries only the new scan: '%s'", line);
}

static void test_only_the_six_subcommands_are_traced(void)
{
    nfc_trace_t t;
    nfc_trace_init(&t);
    CHECK(nfc_trace_slot(0x0c) == NFC_TRACE_SUB_COUNT,
          "the 0x0c probe is not the trace's (it is ns2_codec's constant)");
    CHECK(nfc_trace_slot(0x03) == NFC_TRACE_SUB_03, "0x03 has a slot");
    CHECK(nfc_trace_slot(0x15) == NFC_TRACE_SUB_15, "0x15 has a slot");
    nfc_trace_feed(&t, 0x0c, NULL, 0, NULL, 0, 7);
    nfc_trace_feed(&t, 0x99, NULL, 0, NULL, 0, 7);
    char line[NFC_TRACE_LINE_MAX];
    CHECK(nfc_trace_format(&t, line, sizeof(line)) == 0,
          "an untraced subcommand owes no readout");
}

static void test_a_malformed_write_is_still_an_event(void)
{
    nfc_trace_t t;
    nfc_trace_init(&t);
    /* A truncated 0x14 (no offset/len head) must not be lost — the console sent
     * *something*, and the bench's "exact point it failed" wants it counted. */
    nfc_trace_feed(&t, 0x14, NULL, 2, NULL, 0, 8);
    char line[NFC_TRACE_LINE_MAX];
    size_t n = nfc_trace_format(&t, line, sizeof(line));
    CHECK(n > 0 && contains(line, "sub=14 off=ffff want=0 got=0"),
          "a headless 0x14 is recorded at an impossible offset: '%s'", line);
}

int main(void)
{
    test_the_five_shapes();
    test_the_crc_is_over_what_was_served();
    test_repeats_coalesce_on_the_key();
    test_the_ring_counts_its_drops();
    test_an_empty_trace_owes_nothing();
    test_only_the_six_subcommands_are_traced();
    test_a_malformed_write_is_still_an_event();

    if (g_failures != 0) {
        fprintf(stderr, "%d/%d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("nfc trace: %d checks, OK\n", g_checks);
    return 0;
}
