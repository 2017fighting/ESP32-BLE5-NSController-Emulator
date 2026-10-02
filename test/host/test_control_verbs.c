/*
 * Host-side assertions for the ten verbs and the §2.7 bulk path (spec §2.4-§2.9,
 * §4.3, §5.3; issue #22).
 *
 * The acceptance property of the ticket lives here: every verb has an exercise,
 * every rejection returns its typed ERROR, and a truncated-but-CRC-valid plan is
 * refused rather than replayed.
 *
 * Build:
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include -Itest/host \
 *      -o test_control_verbs \
 *      test/host/test_control_verbs.c \
 *      main/src/protocol/control/control_frame.c \
 *      main/src/protocol/control/control_dispatch.c \
 *      main/src/protocol/control/control_bulk.c
 * Run:
 *   ./test_control_verbs
 */

#include "control_test_util.h"

#include "protocol/control/control_bulk.h"
#include "protocol/plan.h"

static uint8_t g_plan[CONTROL_TEST_PLAN_CAP];

/* ------------------------------------------------------------------ builders */

static void wr32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void fill_hash(uint8_t hash[16], uint8_t seed)
{
    for (int i = 0; i < 16; i++) {
        hash[i] = (uint8_t)(seed + i);
    }
}

/* A structurally valid §5.3 plan: header + @p records zeroed records. The bytes
 * are not a real macro, which is all the device's structural check sees. */
static uint32_t build_plan(uint8_t *buf, uint16_t records, uint32_t loop_ms)
{
    uint32_t len = PLAN_HEADER_SIZE + PLAN_RECORD_SIZE * (uint32_t)records;
    memset(buf, 0, len);
    buf[0] = 'N';
    buf[1] = 'S';
    buf[2] = 'P';
    buf[3] = 'L';
    buf[4] = PLAN_FORMAT_VERSION;
    buf[5] = PLAN_RECORD_SIZE;
    buf[6] = (uint8_t)(records & 0xFFu);
    buf[7] = (uint8_t)(records >> 8);
    wr32(&buf[8], loop_ms);
    return len;
}

static uint32_t last_ack_offset(const harness_t *h)
{
    control_frame_t f;
    uint8_t storage[CONTROL_WIRE_MAX];
    if (wire_parse(h->tx, h->tx_len, &f, storage, sizeof(storage)) != 0 || f.len != 4) {
        return 0xFFFFFFFFu;
    }
    return rd32(f.payload);
}

static void plan_announce(harness_t *h, uint32_t total, const uint8_t hash[16])
{
    uint8_t p[21];
    p[0] = CONTROL_BULK_OP_ANNOUNCE;
    wr32(&p[1], total);
    memcpy(&p[5], hash, 16);
    harness_request(h, CONTROL_VERB_LOAD_PLAN, p, sizeof(p));
}

static void plan_commit(harness_t *h, uint32_t total, const uint8_t hash[16])
{
    uint8_t p[21];
    p[0] = CONTROL_BULK_OP_COMMIT;
    wr32(&p[1], total);
    memcpy(&p[5], hash, 16);
    harness_request(h, CONTROL_VERB_LOAD_PLAN, p, sizeof(p));
}

/* Sends chunks covering [from, upto) exactly like the container would. */
static void plan_chunks_from(harness_t *h, const uint8_t *bytes, uint32_t from, uint32_t upto)
{
    for (uint32_t off = from; off < upto;) {
        size_t n = upto - off;
        if (n > CONTROL_CHUNK_SIZE) {
            n = CONTROL_CHUNK_SIZE;
        }
        uint8_t p[5 + CONTROL_CHUNK_SIZE];
        p[0] = CONTROL_BULK_OP_CHUNK;
        wr32(&p[1], off);
        memcpy(&p[5], &bytes[off], n);
        harness_request(h, CONTROL_VERB_LOAD_PLAN, p, 5 + n);
        off += (uint32_t)n;
    }
}

static void plan_chunks(harness_t *h, const uint8_t *bytes, uint32_t upto)
{
    plan_chunks_from(h, bytes, 0, upto);
}

static void tag_announce(harness_t *h, uint32_t total)
{
    uint8_t p[5];
    p[0] = CONTROL_BULK_OP_ANNOUNCE;
    wr32(&p[1], total);
    harness_request(h, CONTROL_VERB_PLACE_AMIIBO, p, sizeof(p));
}

static void tag_chunks_from(harness_t *h, const uint8_t *bytes, uint32_t from, uint32_t upto)
{
    for (uint32_t off = from; off < upto;) {
        size_t n = upto - off;
        if (n > CONTROL_CHUNK_SIZE) {
            n = CONTROL_CHUNK_SIZE;
        }
        uint8_t p[5 + CONTROL_CHUNK_SIZE];
        p[0] = CONTROL_BULK_OP_CHUNK;
        wr32(&p[1], off);
        memcpy(&p[5], &bytes[off], n);
        harness_request(h, CONTROL_VERB_PLACE_AMIIBO, p, 5 + n);
        off += (uint32_t)n;
    }
}

static void tag_chunks(harness_t *h, const uint8_t *bytes, uint32_t upto)
{
    tag_chunks_from(h, bytes, 0, upto);
}

static void tag_commit(harness_t *h, uint32_t total)
{
    uint8_t p[5];
    p[0] = CONTROL_BULK_OP_COMMIT;
    wr32(&p[1], total);
    harness_request(h, CONTROL_VERB_PLACE_AMIIBO, p, sizeof(p));
}

/* Reads STATUS out of the harness' last reply. */
static int read_status(const harness_t *h, control_frame_t *frame, uint8_t *storage,
                       size_t storage_cap)
{
    return wire_parse(h->tx, h->tx_len, frame, storage, storage_cap);
}

/* ----------------------------------------------------------------- plan check */

static void test_plan_check(void)
{
    uint16_t records = 0xFFFFu;
    uint32_t total = build_plan(g_plan, 2, 100);

    CHECK(control_plan_check(g_plan, total, sizeof(g_plan), &records) == CONTROL_ERR_NONE &&
              records == 2,
          "a valid plan must pass the §5.3 structural check");

    uint8_t bad[64];
    memcpy(bad, g_plan, total);
    bad[0] = 'X';
    CHECK(control_plan_check(bad, total, sizeof(g_plan), &records) == CONTROL_ERR_BAD_PLAN,
          "a bad magic must be BAD_PLAN");

    memcpy(bad, g_plan, total);
    bad[4] = 2;
    CHECK(control_plan_check(bad, total, sizeof(g_plan), &records) == CONTROL_ERR_BAD_PLAN,
          "a wrong format_version must be BAD_PLAN");

    memcpy(bad, g_plan, total);
    bad[5] = 12;
    CHECK(control_plan_check(bad, total, sizeof(g_plan), &records) == CONTROL_ERR_BAD_PLAN,
          "a wrong record_size must be BAD_PLAN");

    memcpy(bad, g_plan, total);
    bad[6] = 3; /* record_count says 3, so payload_len is now short */
    CHECK(control_plan_check(bad, total, sizeof(g_plan), &records) == CONTROL_ERR_BAD_PLAN,
          "payload_len != 12 + 11*record_count must be BAD_PLAN");

    CHECK(control_plan_check(g_plan, 11, sizeof(g_plan), &records) == CONTROL_ERR_BAD_PLAN,
          "a transfer shorter than the header must be BAD_PLAN");
    CHECK(control_plan_check(g_plan, total, 8, &records) == CONTROL_ERR_PLAN_TOO_LARGE,
          "a transfer past the device buffer must be PLAN_TOO_LARGE");
    CHECK(records == 0, "a failed check must zero record_count");
}

/* ------------------------------------------------------------- LOAD_PLAN path */

static void test_load_plan_happy_path(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash[16];
    fill_hash(hash, 0x40);
    uint32_t total = build_plan(g_plan, 2, 250);

    plan_announce(&h, total, hash);
    CHECK(h.acks == 1 && last_ack_offset(&h) == 0, "announce must ACK offset 0");
    CHECK(h.ctl.stage == CONTROL_STAGE_PLAN, "announce must open staging");
    CHECK(h.ctl.status.plan_state == CONTROL_PLAN_NONE,
          "until commit, STATUS reports plan=none (§2.7 rule 6)");

    plan_chunks(&h, g_plan, total);
    CHECK(h.ctl.stage_next == total, "all announced bytes must have landed");
    plan_commit(&h, total, hash);
    CHECK(h.errors == 0, "a valid plan must commit without an ERROR (got code %u)", h.err_code);
    CHECK(h.ctl.plan_committed && h.ctl.plan_len == total && h.ctl.plan_records == 2,
          "commit must make the staged bytes the active plan");
    CHECK(h.ctl.plan == h.ctl.plan_stage, "the active plan must alias the staging buffer (no copy)");
    CHECK(memcmp(h.ctl.plan_hash, hash, 16) == 0, "the commit hash must be retained");
    CHECK(h.ctl.stage == CONTROL_STAGE_NONE, "a commit must close staging");
    CHECK(last_ack_offset(&h) == total, "commit must ACK total_len");

    /* STATUS now reports the committed plan and its echoed identity. */
    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    control_frame_t f;
    uint8_t storage[CONTROL_WIRE_MAX];
    CHECK(read_status(&h, &f, storage, sizeof(storage)) == 0 && f.len == 47, "STATUS must decode");
    CHECK(f.payload[3] == CONTROL_PLAN_COMMITTED, "STATUS.plan_state must be COMMITTED");
    CHECK(memcmp(&f.payload[4], hash, 16) == 0, "STATUS.plan_hash must echo the commit");
    CHECK(f.payload[20] == 2 && f.payload[21] == 0, "STATUS.plan_frame_count must be record_count");
}

static void test_load_plan_windowed_ack(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash[16];
    fill_hash(hash, 0x50);
    uint32_t total = build_plan(g_plan, 500, 1000);
    CHECK(total == 5512, "the window fixture must span the 4096-byte ACK window");

    plan_announce(&h, total, hash);
    uint32_t acked[8];
    int n_acked = 0;
    for (uint32_t off = 0; off < total;) {
        size_t n = total - off;
        if (n > CONTROL_CHUNK_SIZE) {
            n = CONTROL_CHUNK_SIZE;
        }
        uint8_t p[5 + CONTROL_CHUNK_SIZE];
        p[0] = CONTROL_BULK_OP_CHUNK;
        wr32(&p[1], off);
        memcpy(&p[5], &g_plan[off], n);
        int before = h.acks;
        harness_request(&h, CONTROL_VERB_LOAD_PLAN, p, 5 + n);
        if (h.acks > before) {
            CHECK(n_acked < (int)(sizeof(acked) / sizeof(acked[0])), "too many ACKs");
            acked[n_acked++] = last_ack_offset(&h);
        }
        off += (uint32_t)n;
    }
    CHECK(n_acked == 2, "a 5512-byte transfer must draw two window ACKs, got %d", n_acked);
    CHECK(n_acked == 2 && acked[0] == CONTROL_BULK_ACK_WINDOW && acked[1] == total,
          "ACKs must be the window boundary then total_len (got %u, %u)",
          n_acked > 0 ? acked[0] : 0, n_acked > 1 ? acked[1] : 0);
}

static void test_load_plan_rejections(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash[16];
    fill_hash(hash, 0x60);
    uint32_t total = build_plan(g_plan, 2, 100);

    /* PLAN_TOO_LARGE: past the device's own buffer, and past the advertised
     * plan_capacity_bytes. */
    plan_announce(&h, (uint32_t)CONTROL_TEST_PLAN_CAP + 1, hash);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_PLAN_TOO_LARGE &&
              h.err_detail == CONTROL_TEST_PLAN_CAP + 1,
          "an over-large announce must be PLAN_TOO_LARGE(total_len)");

    plan_announce(&h, CONTROL_PLAN_CAPACITY_BYTES + 100, hash);
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_PLAN_TOO_LARGE &&
              h.err_detail == CONTROL_PLAN_CAPACITY_BYTES + 100,
          "a transfer past plan_capacity_bytes must be PLAN_TOO_LARGE");

    /* Bad plan bytes at commit: each header field in turn. */
    const uint8_t bad_fields[] = {0 /*magic*/, 4 /*version*/, 5 /*record_size*/,
                                  6 /*record_count*/};
    for (size_t i = 0; i < sizeof(bad_fields); i++) {
        harness_init(&h);
        uint32_t t = build_plan(g_plan, 2, 100);
        g_plan[bad_fields[i]] ^= 0xFF;
        plan_announce(&h, t, hash);
        plan_chunks(&h, g_plan, t);
        plan_commit(&h, t, hash);
        CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_PLAN && h.err_detail == 0,
              "bad header byte %u must be BAD_PLAN detail=0 (got code %u)", bad_fields[i],
              h.err_code);
        CHECK(h.ctl.stage == CONTROL_STAGE_NONE, "a failed commit must discard staging");
        CHECK(!h.ctl.plan_committed, "a failed commit must not commit a plan");
        g_plan[bad_fields[i]] ^= 0xFF;
    }

    /* A commit whose re-sent hash disagrees with the announce. */
    harness_init(&h);
    total = build_plan(g_plan, 2, 100);
    plan_announce(&h, total, hash);
    plan_chunks(&h, g_plan, total);
    uint8_t other[16];
    fill_hash(other, 0x70);
    plan_commit(&h, total, other);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_PLAN,
          "a commit hash that disagrees with the announce must be BAD_PLAN");

    /* Frame-shape rejections: a stub op, a short chunk, an over-long chunk. */
    harness_init(&h);
    uint8_t stub[6] = {1, 0, 0, 0, 0, 0};
    harness_request(&h, CONTROL_VERB_LOAD_PLAN, stub, sizeof(stub));
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_LENGTH && h.err_detail == 6,
          "an announce of the wrong len must be BAD_LENGTH(len)");

    uint8_t bad_op[6] = {9, 0, 0, 0, 0, 0};
    harness_request(&h, CONTROL_VERB_LOAD_PLAN, bad_op, sizeof(bad_op));
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_BAD_LENGTH,
          "an unknown bulk op must be BAD_LENGTH");

    harness_init(&h);
    plan_announce(&h, total, hash);
    uint8_t overlong[5 + CONTROL_CHUNK_SIZE + 1];
    overlong[0] = CONTROL_BULK_OP_CHUNK;
    wr32(&overlong[1], 0);
    harness_request(&h, CONTROL_VERB_LOAD_PLAN, overlong, sizeof(overlong));
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_LENGTH,
          "a chunk past chunk_size must be BAD_LENGTH");

    /* A chunk that overruns the announced transfer. */
    harness_init(&h);
    plan_announce(&h, total, hash);
    uint8_t over[5 + CONTROL_CHUNK_SIZE];
    over[0] = CONTROL_BULK_OP_CHUNK;
    wr32(&over[1], 0); /* at the expected offset, but 256 bytes into a 34-byte transfer */
    CHECK(harness_request(&h, CONTROL_VERB_LOAD_PLAN, over, 5 + CONTROL_CHUNK_SIZE) == 1 &&
              h.err_code == CONTROL_ERR_BAD_LENGTH,
          "a chunk that overruns total_len must be BAD_LENGTH");

    /* Out of sequence: a chunk and a commit with no announce. */
    harness_init(&h);
    uint8_t chunk[5 + 4] = {CONTROL_BULK_OP_CHUNK, 0, 0, 0, 0, 1, 2, 3, 4};
    harness_request(&h, CONTROL_VERB_LOAD_PLAN, chunk, sizeof(chunk));
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_STATE &&
              h.err_detail == CONTROL_MODE_IDLE,
          "a chunk with no announce must be BAD_STATE(mode)");
    plan_commit(&h, total, hash);
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_BAD_STATE,
          "a commit with no announce must be BAD_STATE(mode)");

    /* A chunk for the wrong verb while a plan is staging. */
    harness_init(&h);
    plan_announce(&h, total, hash);
    harness_request(&h, CONTROL_VERB_PLACE_AMIIBO, chunk, sizeof(chunk));
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_STATE,
          "a PLACE_AMIIBO chunk while a plan stages must be BAD_STATE");
}

/* The acceptance property: a truncated-but-CRC-valid plan is refused. */
static void test_truncated_plan_is_refused(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash[16];
    fill_hash(hash, 0x80);
    uint32_t total = build_plan(g_plan, 4, 400);

    /* Every frame that arrives is CRC-valid; the transfer is simply short. */
    plan_announce(&h, total, hash);
    plan_chunks(&h, g_plan, total - PLAN_RECORD_SIZE);
    plan_commit(&h, total, hash);

    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_PLAN,
          "a truncated transfer must be BAD_PLAN, not replayed");
    CHECK(h.ctl.stage == CONTROL_STAGE_NONE, "the staging buffer must be discarded");
    CHECK(!h.ctl.plan_committed && h.ctl.plan == NULL, "no plan may become active");
    CHECK(h.ctl.status.plan_state == CONTROL_PLAN_NONE, "STATUS must still report plan=none");

    /* And START cannot arm it. */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_NO_PLAN,
          "START after a refused transfer must be NO_PLAN (got %u)", h.err_code);
    CHECK(h.start_calls == 0, "the executor must not have been armed");
}

static void test_plan_replacement_supersedes(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash_a[16];
    uint8_t hash_b[16];
    fill_hash(hash_a, 0x90);
    fill_hash(hash_b, 0xA0);
    uint32_t total = build_plan(g_plan, 2, 100);

    plan_announce(&h, total, hash_a);
    plan_chunks(&h, g_plan, total);
    plan_commit(&h, total, hash_a);
    CHECK(h.ctl.plan_committed, "the first plan must commit");

    /* A second announce reports plan=none immediately (§2.7 rule 6, plan_slots=1)
     * and a failed replacement leaves no plan rather than the old one. */
    harness_init(&h);
    plan_announce(&h, total, hash_a);
    plan_chunks(&h, g_plan, total);
    plan_commit(&h, total, hash_a);
    plan_announce(&h, total, hash_b);
    CHECK(h.ctl.status.plan_state == CONTROL_PLAN_NONE,
          "a re-announce must report plan=none until the new commit lands");
    plan_chunks(&h, g_plan, total - 1);
    plan_commit(&h, total, hash_b);
    CHECK(h.err_code == CONTROL_ERR_BAD_PLAN && !h.ctl.plan_committed,
          "a failed replacement must not resurrect the superseded plan");
}

/* ------------------------------------------------------ START / STOP / CONFIG */

static void test_start_stop(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash[16];
    fill_hash(hash, 0xB0);
    uint32_t total = build_plan(g_plan, 2, 100);

    /* NO_PLAN first. */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_NO_PLAN && h.err_detail == 0,
          "START with no committed plan must be NO_PLAN detail=0");

    plan_announce(&h, total, hash);
    plan_chunks(&h, g_plan, total);
    plan_commit(&h, total, hash);

    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.errors == 1 && h.acks == 4 && h.start_calls == 1, "START must ACK and arm the executor");
    CHECK(h.ctl.status.mode == CONTROL_MODE_MACRO, "START must enter MACRO");

    /* A retried START is a second start, and is refused (ADR-0007). */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_ALREADY_RUNNING && h.err_detail == 0,
          "START while MACRO must be ALREADY_RUNNING");

    /* STOP: neutral + CONTAINER_STOP, plan retained. */
    harness_request(&h, CONTROL_VERB_STOP, NULL, 0);
    CHECK(h.errors == 2 && h.stop_calls == 1 &&
              h.ctl.status.last_stop_reason == CONTROL_STOP_CONTAINER,
          "STOP must release neutral, set CONTAINER_STOP and ACK");
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE, "STOP must return to IDLE");
    CHECK(h.ctl.plan_committed, "STOP must retain the committed plan");

    /* STOP in IDLE is an idempotent ACK and must not rewrite the reason. */
    h.ctl.status.last_stop_reason = CONTROL_STOP_BOOT_LOCAL;
    harness_request(&h, CONTROL_VERB_STOP, NULL, 0);
    CHECK(h.stop_calls == 1 && h.ctl.status.last_stop_reason == CONTROL_STOP_BOOT_LOCAL,
          "STOP in IDLE must be idempotent and leave last_stop_reason alone");

    /* START re-arms in one verb after a stop. */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.start_calls == 2 && h.ctl.status.mode == CONTROL_MODE_MACRO,
          "a retained plan must re-arm with one START");

    /* A START with a payload is BAD_LENGTH even in a legal mode. */
    uint8_t one[1] = {0};
    harness_request(&h, CONTROL_VERB_START, one, sizeof(one));
    CHECK(h.err_code == CONTROL_ERR_BAD_LENGTH && h.err_detail == 1,
          "START with a payload must be BAD_LENGTH(len)");
}

static void test_config(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t p[3] = {0x14, 0x00, 0x01}; /* 20 ms, led on */
    harness_request(&h, CONTROL_VERB_CONFIG, p, sizeof(p));
    CHECK(h.errors == 0 && h.config_calls == 1 && h.last_config_ms == 20 && h.last_config_led == 1,
          "CONFIG must deliver report_interval_ms le16 and led");

    /* Always legal: accepted in MACRO too (§4.3). */
    h.ctl.status.mode = CONTROL_MODE_MACRO;
    uint8_t q[3] = {0x0A, 0x00, 0x00};
    harness_request(&h, CONTROL_VERB_CONFIG, q, sizeof(q));
    CHECK(h.errors == 0 && h.config_calls == 2 && h.last_config_ms == 10 && h.last_config_led == 0,
          "CONFIG must be accepted in MACRO");

    harness_request(&h, CONTROL_VERB_CONFIG, p, 2);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_LENGTH && h.err_detail == 2,
          "CONFIG with two bytes must be BAD_LENGTH(len)");
    uint8_t four[4] = {0x14, 0x00, 0x01, 0x00};
    harness_request(&h, CONTROL_VERB_CONFIG, four, sizeof(four));
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_BAD_LENGTH,
          "CONFIG with four bytes must be BAD_LENGTH(len)");
}

/* ------------------------------------------------------------ PLACE_AMIIBO */

static void build_tag(uint8_t tag[CONTROL_TAG_SIZE], uint8_t uid0, uint8_t seed)
{
    memset(tag, 0, CONTROL_TAG_SIZE);
    /* §6.3: UID[0..2] at 0-2, BCC0 at 3, UID[3..6] at 4-7. */
    tag[0] = 0x04;
    tag[1] = uid0;
    tag[2] = 0xFE;
    tag[3] = (uint8_t)(0x88u ^ tag[0] ^ tag[1] ^ tag[2]);
    tag[4] = 0xCA;
    tag[5] = seed;
    tag[6] = 0x6C;
    tag[7] = 0x81;
    for (int i = 8; i < (int)CONTROL_TAG_SIZE; i++) {
        tag[i] = (uint8_t)(seed ^ (uint8_t)i);
    }
}

static void test_place_unplace(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t tag[CONTROL_TAG_SIZE];
    build_tag(tag, 0x11, 0x22);

    tag_announce(&h, CONTROL_TAG_SIZE);
    CHECK(h.acks == 1 && last_ack_offset(&h) == 0, "a tag announce must ACK offset 0");
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE,
          "a tag must not enter AMIIBO before the commit (atomic, §2.7 rule 6)");

    tag_chunks(&h, tag, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.errors == 0, "a 540-byte tag must commit (code %u)", h.err_code);
    CHECK(h.ctl.status.mode == CONTROL_MODE_AMIIBO, "a committed tag must enter AMIIBO");
    CHECK(h.ctl.status.tag_state == CONTROL_TAG_PLACED, "tag_state must be PLACED");
    CHECK(h.place_calls == 1 && h.last_placed_len == CONTROL_TAG_SIZE,
          "the tag server effect must receive the 540 bytes");

    /* The identity is the seven-byte UID: BCC0 at tag byte 3 is skipped (§6.3). */
    const uint8_t expect[7] = {0x04, 0x11, 0xFE, 0xCA, 0x22, 0x6C, 0x81};
    CHECK(memcmp(h.ctl.tag_identity, expect, 7) == 0, "tag_identity must be UID[0..2] + UID[3..6]");
    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    control_frame_t f;
    uint8_t storage[CONTROL_WIRE_MAX];
    CHECK(read_status(&h, &f, storage, sizeof(storage)) == 0, "STATUS must decode");
    CHECK(f.payload[2] == CONTROL_MODE_AMIIBO && f.payload[28] == CONTROL_TAG_PLACED &&
              memcmp(&f.payload[29], expect, 7) == 0,
          "STATUS must report AMIIBO, PLACED and the UID at §3.2 offsets");

    /* UNPLACE_AMIIBO: the tag stops answering, the 540 bytes are retained. */
    harness_request(&h, CONTROL_VERB_UNPLACE_AMIIBO, NULL, 0);
    CHECK(h.errors == 0 && h.unplace_calls == 1 && h.ctl.status.tag_state == CONTROL_TAG_NONE,
          "UNPLACE_AMIIBO must stop the tag and ACK");
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE, "UNPLACE_AMIIBO must return to IDLE");
    CHECK(h.ctl.tag[4] == 0xCA, "the tag bytes must be retained across unplace");
    uint8_t blank[7] = {0};
    CHECK(memcmp(h.ctl.status.tag_identity, blank, 7) == 0,
          "STATUS.tag_identity must be zero when no tag is placed");

    /* Idempotent in IDLE, and a no-op in MACRO. */
    harness_request(&h, CONTROL_VERB_UNPLACE_AMIIBO, NULL, 0);
    CHECK(h.unplace_calls == 1 && h.errors == 0, "UNPLACE_AMIIBO in IDLE must be an idempotent ACK");
    h.ctl.status.mode = CONTROL_MODE_MACRO;
    harness_request(&h, CONTROL_VERB_UNPLACE_AMIIBO, NULL, 0);
    CHECK(h.unplace_calls == 1 && h.ctl.status.mode == CONTROL_MODE_MACRO && h.errors == 0,
          "UNPLACE_AMIIBO in MACRO must be an idempotent ACK and change no mode");
}

static void test_place_rejections_and_replace(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t tag_a[CONTROL_TAG_SIZE];
    uint8_t tag_b[CONTROL_TAG_SIZE];
    build_tag(tag_a, 0x11, 0x22);
    build_tag(tag_b, 0x33, 0x44);

    /* The tag size is fixed; a different total_len is a length error, caught at
     * the announce so nothing is staged. */
    tag_announce(&h, CONTROL_TAG_SIZE - 1);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_LENGTH &&
              h.err_detail == CONTROL_TAG_SIZE - 1,
          "a tag announce with the wrong total_len must be BAD_LENGTH(total_len)");
    CHECK(h.ctl.stage == CONTROL_STAGE_NONE, "a rejected announce must not open staging");

    /* Short tag announce frame. */
    uint8_t short_announce[4] = {CONTROL_BULK_OP_ANNOUNCE, 0, 0, 0};
    harness_request(&h, CONTROL_VERB_PLACE_AMIIBO, short_announce, sizeof(short_announce));
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_BAD_LENGTH,
          "a PLACE_AMIIBO announce of the wrong frame len must be BAD_LENGTH");

    /* Commit with no announce. */
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.errors == 3 && h.err_code == CONTROL_ERR_BAD_STATE,
          "a tag commit with no announce must be BAD_STATE");

    /* A truncated tag transfer is refused. */
    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag_a, CONTROL_TAG_SIZE - 4);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.errors == 4 && h.err_code == CONTROL_ERR_BAD_LENGTH && !h.ctl.tag_placed,
          "a truncated tag must be refused and nothing placed");

    /* The happy path, then an atomic replace while AMIIBO. */
    harness_init(&h);
    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag_a, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.place_calls == 1 && h.ctl.tag_identity[1] == 0x11, "the first tag must be placed");

    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag_b, CONTROL_TAG_SIZE / 2);
    CHECK(h.ctl.tag_identity[1] == 0x11 && h.ctl.tag_placed,
          "the old tag must keep answering until the replace commits (§4.3)");
    /* Resume from the ACK'd offset, as §2.7 rule 5 requires. */
    tag_chunks_from(&h, tag_b, CONTROL_TAG_SIZE / 2, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.place_calls == 2 && h.ctl.tag_identity[1] == 0x33,
          "the replace must land atomically and place the new UID");

    /* ADR-0007: PLACE_AMIIBO while MACRO is BAD_STATE and changes nothing. */
    harness_init(&h);
    h.ctl.status.mode = CONTROL_MODE_MACRO;
    tag_announce(&h, CONTROL_TAG_SIZE);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_STATE &&
              h.err_detail == CONTROL_MODE_MACRO,
          "PLACE_AMIIBO while MACRO must be BAD_STATE(mode)");
    CHECK(h.ctl.status.mode == CONTROL_MODE_MACRO && !h.ctl.tag_placed,
          "a rejected placement must leave the mode untouched");
}

/* --------------------------------------------------------------- mode legality */

static void test_mode_legality(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash[16];
    fill_hash(hash, 0xC0);
    uint32_t total = build_plan(g_plan, 2, 100);

    /* LOAD_PLAN while MACRO is BAD_STATE. */
    h.ctl.status.mode = CONTROL_MODE_MACRO;
    plan_announce(&h, total, hash);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_STATE &&
              h.err_detail == CONTROL_MODE_MACRO,
          "LOAD_PLAN while MACRO must be BAD_STATE(mode)");

    /* LOAD_PLAN while AMIIBO is BAD_STATE too. */
    h.ctl.status.mode = CONTROL_MODE_AMIIBO;
    plan_announce(&h, total, hash);
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_BAD_STATE &&
              h.err_detail == CONTROL_MODE_AMIIBO,
          "LOAD_PLAN while AMIIBO must be BAD_STATE(mode)");

    /* START while AMIIBO is BAD_STATE, and queues nothing. */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.errors == 3 && h.err_code == CONTROL_ERR_BAD_STATE &&
              h.err_detail == CONTROL_MODE_AMIIBO && h.start_calls == 0,
          "START while AMIIBO must be BAD_STATE(mode) and arm nothing");

    /* STOP from AMIIBO: tag unplaced, tag data retained, CONTAINER_STOP. */
    h.ctl.tag_placed = true;
    h.ctl.status.tag_state = CONTROL_TAG_PLACED;
    harness_request(&h, CONTROL_VERB_STOP, NULL, 0);
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE && h.ctl.status.tag_state == CONTROL_TAG_NONE &&
              h.ctl.status.last_stop_reason == CONTROL_STOP_CONTAINER && h.stop_calls == 1,
          "STOP from AMIIBO must unplace the tag and return to IDLE");
    CHECK(h.ctl.tag[0] == 0, "STOP from AMIIBO must retain the tag bytes");
}

/* --------------------------------------------------------- always-legal verbs */

static void test_always_legal_verbs(void)
{
    harness_t h;
    harness_init(&h);

    /* PAIR_UNPAIR is accepted in every mode, changes no mode, and never toggles
     * (ADR-0013). */
    const uint8_t modes[3] = {CONTROL_MODE_IDLE, CONTROL_MODE_MACRO, CONTROL_MODE_AMIIBO};
    for (int i = 0; i < 3; i++) {
        h.ctl.status.mode = modes[i];
        harness_request(&h, CONTROL_VERB_PAIR_UNPAIR, NULL, 0);
        CHECK(h.errors == 0 && h.ctl.status.mode == modes[i],
              "PAIR_UNPAIR must be accepted in mode %u and change no mode", modes[i]);
    }
    CHECK(h.pair_calls == 3, "PAIR_UNPAIR must reach the pairing effect every time");

    int attempts = h.acks;
    harness_request(&h, CONTROL_VERB_PAIR_UNPAIR, (const uint8_t *)"x", 1);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_LENGTH && h.acks == attempts,
          "PAIR_UNPAIR with a payload must be BAD_LENGTH and must not act");

    /* HELLO is always legal, in any mode (§2.4). */
    h.ctl.status.mode = CONTROL_MODE_MACRO;
    uint8_t probe[1] = {CONTROL_PROTO_VER};
    harness_request(&h, CONTROL_VERB_HELLO, probe, sizeof(probe));
    CHECK(h.errors == 1 && h.acks == attempts + 1, "HELLO must be legal in MACRO");

    /* ERROR is a REPLY direction; a REQUEST carrying verb 10 is not a verb. */
    harness_request(&h, CONTROL_VERB_ERROR, NULL, 0);
    CHECK(h.errors == 2 && h.err_code == CONTROL_ERR_UNKNOWN_SUBCMD && h.err_detail == 10,
          "a REQUEST with verb ERROR must be UNKNOWN_SUBCMD(10)");
}

/* -------------------------------------------------------- last_error surfacing */

static void test_last_error_bookkeeping(void)
{
    harness_t h;
    harness_init(&h);

    /* A rejection records the pair in STATUS (§3.2). */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.ctl.status.last_error_code == CONTROL_ERR_NO_PLAN &&
              h.ctl.status.last_error_detail == 0,
          "a rejection must be recorded in STATUS.last_error");

    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    control_frame_t f;
    uint8_t storage[CONTROL_WIRE_MAX];
    CHECK(read_status(&h, &f, storage, sizeof(storage)) == 0 && f.payload[37] == CONTROL_ERR_NO_PLAN,
          "STATUS.last_error.code must be at offset 37");

    /* The next successful verb clears it, not a read (§3.2). */
    harness_request(&h, CONTROL_VERB_PAIR_UNPAIR, NULL, 0);
    CHECK(h.ctl.status.last_error_code == CONTROL_ERR_NONE &&
              h.ctl.status.last_error_detail == 0,
          "the next successful verb must clear last_error");
}

int main(void)
{
    test_plan_check();
    test_load_plan_happy_path();
    test_load_plan_windowed_ack();
    test_load_plan_rejections();
    test_truncated_plan_is_refused();
    test_plan_replacement_supersedes();
    test_start_stop();
    test_config();
    test_place_unplace();
    test_place_rejections_and_replace();
    test_mode_legality();
    test_always_legal_verbs();
    test_last_error_bookkeeping();

    if (g_failures == 0) {
        printf("control verbs ok: %d checks\n", g_checks);
        return 0;
    }
    fprintf(stderr, "control verbs FAILED: %d/%d checks\n", g_failures, g_checks);
    return 1;
}
