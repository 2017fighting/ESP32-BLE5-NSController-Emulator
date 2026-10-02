/*
 * Host-side assertions for the plan executor and the neutral template (§7.3
 * step 5, §4.6, §5.4, §3.3, §2.9; issue #24).
 *
 * The acceptance property of the ticket lives here: a committed plan walks its frames
 * frame for frame against an exact-millisecond clock, the boundary emits the
 * compiled-in neutral and then restarts with no gap beyond the handoff, the
 * neutral is the last write on every exit path, `LOOP_COMPLETED` cannot outrun
 * the `STATUS` poll, and a fault exits to `IDLE` with the neutral still applied.
 *
 * Build:
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include -Itest/host \
 *      -o test_control_executor \
 *      test/host/test_control_executor.c \
 *      main/src/protocol/control/control_frame.c \
 *      main/src/protocol/control/control_dispatch.c \
 *      main/src/protocol/control/control_bulk.c \
 *      main/src/protocol/control/control_events.c \
 *      main/src/protocol/control/control_mode.c \
 *      main/src/protocol/control/control_executor.c
 * Run:
 *   ./test_control_executor
 */

#include "control_test_util.h"

#include "protocol/control/control_events.h"
#include "protocol/control/control_executor.h"
#include "protocol/control/control_mode.h"

#include <stdlib.h>

/* ------------------------------------------------------- the reporter double */

/* A faithful model of the HID double buffer's one `swap_request` bit: a write
 * lands in the back buffer and sets the bit, and the reporter clears it when it
 * has consumed what it found there. `refuse` models "there is no report at all",
 * which is the adapter's fatal case. */
typedef struct {
    uint8_t writes[64][CONTROL_EXECUTOR_STATE_BYTES];
    int n;
    bool busy;
    bool refuse;
    int commit_idle_calls;
} reporter_t;

static bool reporter_apply(void *ctx, const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES])
{
    reporter_t *r = (reporter_t *)ctx;
    if (r->refuse) {
        return false;
    }
    if (r->n < (int)(sizeof(r->writes) / sizeof(r->writes[0]))) {
        memcpy(r->writes[r->n], state, CONTROL_EXECUTOR_STATE_BYTES);
    }
    r->n++;
    r->busy = true;
    return true;
}

static bool reporter_idle(void *ctx)
{
    reporter_t *r = (reporter_t *)ctx;
    r->commit_idle_calls++;
    return !r->busy;
}

/* The reporter's wake: it saw the commit and cleared the bit. */
static void reporter_consume(reporter_t *r)
{
    r->busy = false;
}

static bool reporter_last_is(const reporter_t *r, const uint8_t expect[9])
{
    if (r->n == 0) {
        return false;
    }
    return memcmp(r->writes[r->n - 1], expect, 9) == 0;
}

/* ------------------------------------------------------------- plan building */

/* A §5.3 plan with real per-record states and holds. @p holds sums to loop_ms,
 * which is what the compiler guarantees (§5.4) and what the walk relies on. */
static uint32_t build_hold_plan(uint8_t *buf, const uint16_t *holds, uint16_t count,
                                uint32_t loop_ms)
{
    uint32_t len = PLAN_HEADER_SIZE + PLAN_RECORD_SIZE * (uint32_t)count;
    memset(buf, 0, len);
    buf[0] = 'N';
    buf[1] = 'S';
    buf[2] = 'P';
    buf[3] = 'L';
    buf[4] = PLAN_FORMAT_VERSION;
    buf[5] = PLAN_RECORD_SIZE;
    buf[6] = (uint8_t)(count & 0xFFu);
    buf[7] = (uint8_t)(count >> 8);
    wr32(&buf[8], loop_ms);
    for (uint16_t i = 0; i < count; i++) {
        uint8_t *rec = &buf[PLAN_HEADER_SIZE + PLAN_RECORD_SIZE * (uint32_t)i];
        /* A distinct, recognisable state per record: record i holds button byte
         * 0 = i+1 and both sticks at centre. */
        rec[0] = (uint8_t)(i + 1);
        rec[3] = 0x00;
        rec[4] = 0x08;
        rec[5] = 0x80;
        rec[6] = 0x00;
        rec[7] = 0x08;
        rec[8] = 0x80;
        rec[9] = (uint8_t)(holds[i] & 0xFFu);
        rec[10] = (uint8_t)(holds[i] >> 8);
    }
    return len;
}

static void record_state(uint8_t out[9], uint16_t index)
{
    memset(out, 0, 9);
    out[0] = (uint8_t)(index + 1);
    out[4] = 0x08;
    out[5] = 0x80;
    out[7] = 0x08;
    out[8] = 0x80;
}

/* Installs the executor over the harness' control state. The harness' `fx`
 * callbacks stay a double for the *verb* layer; the executor owns the report. */
static void ex_init(control_executor_t *ex, reporter_t *r, harness_t *h)
{
    memset(r, 0, sizeof(*r));
    const control_executor_io_t io = {
        .ctx = r,
        .apply_state = reporter_apply,
        .commit_idle = reporter_idle,
    };
    control_executor_init(ex, &io);
    (void)h;
}

/* Commits @p holds as the active plan and arms the executor. */
static void arm_plan(harness_t *h, control_executor_t *ex, uint8_t *buf, const uint16_t *holds,
                     uint16_t count, uint32_t loop_ms, uint32_t now_ms)
{
    uint8_t hash[16];
    fill_hash(hash, 0x5A);
    uint32_t total = build_hold_plan(buf, holds, count, loop_ms);
    plan_announce(h, total, hash);
    plan_chunks(h, buf, total);
    plan_commit(h, total, hash);
    uint8_t code = control_executor_arm(ex, &h->ctl, now_ms);
    CHECK(code == CONTROL_ERR_NONE, "arm of a valid plan must return NONE, got %u", code);
    /* §4.3: the ACK means "armed", and the mode edge follows it. `reply_start`
     * does this; mirroring it here keeps the verb layer's mode-dependent arms
     * (CONFIG's boundary rule, the fault exit) reachable in the host suite. */
    control_mode_enter(&h->ctl, CONTROL_MODE_MACRO);
}

/* ------------------------------------------------------------------- the tests */

/* §4.6: the template is compiled in, zeroes every button byte, and centres both
 * sticks — the same neutral `pro2_report_init` produces, so the two cannot
 * drift. */
static void test_neutral_template(void)
{
    const uint8_t expect[9] = {0x00, 0x00, 0x00, 0x00, 0x08, 0x80, 0x00, 0x08, 0x80};
    CHECK(memcmp(control_executor_neutral, expect, 9) == 0,
          "the neutral template must be the zeroed, centred Pro2 neutral");
    /* And it must not be plan data: a record's own bytes never equal it unless
     * the macro happened to be neutral, which is not what the test is about. */
    uint8_t nonneutral[9];
    record_state(nonneutral, 3);
    CHECK(memcmp(control_executor_neutral, nonneutral, 9) != 0, "sanity: states differ");
}

/* §2.3: a rejection must not leave state touched, so a refused arm writes
 * nothing and leaves the executor stopped. Also the wire half: the dispatcher
 * must return a typed ERROR and must not enter MACRO. */
static void test_arm_refuses_an_invalid_plan(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);

    /* No committed plan at all. */
    CHECK(control_executor_arm(&ex, &h.ctl, 0) == CONTROL_ERR_BAD_PLAN,
          "START with no plan must refuse");
    CHECK(r.n == 0, "a refused arm must not write the report");
    CHECK(!control_executor_running(&ex), "a refused arm must leave the executor stopped");

    /* A committed plan with no records: §5.3's structural check accepts it
     * (len == 12), and the compiler refuses to emit one (§5.5) — but a
     * hand-built plan can reach the device, so the executor refuses it rather
     * than walking zero records. */
    uint8_t zero_records[PLAN_HEADER_SIZE];
    uint8_t hash[16];
    fill_hash(hash, 0x33);
    uint32_t zero_len = build_hold_plan(zero_records, NULL, 0, 0);
    CHECK(zero_len == PLAN_HEADER_SIZE, "a zero-record plan is header-only");
    plan_announce(&h, zero_len, hash);
    plan_chunks(&h, zero_records, zero_len);
    plan_commit(&h, zero_len, hash);
    CHECK(control_plan_check(zero_records, sizeof(zero_records), sizeof(zero_records), NULL) ==
              CONTROL_ERR_NONE,
          "a zero-record plan is structurally valid, which is why the executor must refuse it");
    CHECK(control_executor_arm(&ex, &h.ctl, 0) == CONTROL_ERR_BAD_PLAN,
          "a zero-record plan must be refused at arm");
    CHECK(r.n == 0, "a refused zero-record arm must not write the report");
}

/* The wire half of the same rule: a refused arm reaches the container as a typed
 * ERROR, and the mode does not move. */
static void test_dispatcher_rejects_a_refused_arm(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t plan[CONTROL_TEST_PLAN_CAP];
    commit_plan(&h, plan, 2, 0x21);

    h.start_refusal = CONTROL_ERR_BAD_PLAN;
    int replies = harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(replies == 1 && h.errors == 1, "a refused arm must draw one ERROR reply");
    CHECK(h.err_code == CONTROL_ERR_BAD_PLAN, "the refusal's code must reach the wire, got %u",
          h.err_code);
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE,
          "a refused arm must not enter MACRO");
    CHECK(h.ctl.status.last_error_code == CONTROL_ERR_BAD_PLAN,
          "the refusal must be legible from STATUS (§3.2)");

    /* The plan is still committed, so a healthy arm still works. */
    h.start_refusal = CONTROL_ERR_NONE;
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.ctl.status.mode == CONTROL_MODE_MACRO, "a healthy arm must enter MACRO");
}

/* §4.3: "arm at frame 0; neutral first". The neutral is committed before the
 * first record, and the loop clock starts when record 0 lands. */
static void test_arm_emits_neutral_first(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[2] = {100, 200};

    arm_plan(&h, &ex, buf, holds, 2, 300, 1000);
    CHECK(r.n == 1, "arm must write exactly one report so far (the neutral), got %d", r.n);
    CHECK(reporter_last_is(&r, control_executor_neutral), "arm must write the neutral first");
    CHECK(control_executor_running(&ex), "arm must leave the executor running");

    /* The reporter consumes the neutral; the handoff then applies record 0. */
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 1000);
    uint8_t s0[9];
    record_state(s0, 0);
    CHECK(reporter_last_is(&r, s0), "record 0 must follow the neutral");
    CHECK(h.ctl.status.current_frame == 0, "record 0 must report current_frame = 0");
    CHECK(h.ctl.status.plan_frame_count == 2, "plan_frame_count must be record_count");
}

/* §5.3/§5.4: each record is held for its hold_ms, and the record changes only
 * once the hold has actually elapsed. A hold may span many ticks. */
static void test_walk_holds_each_record(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    /* 100 + 250 + 150 = 500, deliberately not multiples of the 10 ms tick. */
    const uint16_t holds[3] = {100, 250, 150};

    arm_plan(&h, &ex, buf, holds, 3, 500, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);

    uint8_t s[9];
    record_state(s, 0);
    CHECK(reporter_last_is(&r, s), "record 0 must be applied at arm");

    /* Still inside record 0's 100 ms hold. */
    control_executor_step(&ex, &h.ctl, 99);
    CHECK(reporter_last_is(&r, s), "record 0 must still be current at 99 ms");
    CHECK(h.ctl.status.current_frame == 0, "current_frame must not advance early");

    /* At exactly 100 ms the hold has elapsed. */
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 100);
    record_state(s, 1);
    CHECK(reporter_last_is(&r, s), "record 1 must be applied at exactly 100 ms");
    CHECK(h.ctl.status.current_frame == 1, "current_frame must be 1");

    /* A 250 ms hold spans many ticks without re-committing the report: a record
     * is held, not rewritten every tick. */
    int before = r.n;
    for (uint32_t t = 110; t < 350; t += 10) {
        control_executor_step(&ex, &h.ctl, t);
    }
    CHECK(r.n == before, "a hold must not re-commit on every tick (wrote %d extra)", r.n - before);
    CHECK(h.ctl.status.current_frame == 1, "record 1 must span the long hold");

    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 350);
    record_state(s, 2);
    CHECK(reporter_last_is(&r, s), "record 2 must be applied at 100 + 250 ms");
    CHECK(h.ctl.status.current_frame == 2, "current_frame must be 2");

    /* The last record's hold runs to `loop_ms` = 500, not to its own 150. */
    control_executor_step(&ex, &h.ctl, 499);
    CHECK(h.ctl.status.current_frame == 2, "the last record must run to loop_ms");
}

/* §4.6 vs §5.4: at the boundary the neutral is committed and transmitted, and
 * the restart follows with no gap beyond the handoff. The read-out is asserted
 * as the exact write order — neutral, then record 0 — with nothing between. */
static void test_boundary_neutral_then_restart(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[2] = {50, 50}; /* loop_ms = 100 */

    arm_plan(&h, &ex, buf, holds, 2, 100, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0); /* record 0 */

    int before = h.event_count;
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 50); /* record 1 */
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 100);

    /* The last write of the loop is the neutral, and it is committed. */
    CHECK(reporter_last_is(&r, control_executor_neutral),
          "the boundary must leave the neutral as the last committed write");
    CHECK(h.ctl.status.loop_count == 1, "the boundary must count the loop, got %u",
          h.ctl.status.loop_count);
    CHECK(harness_has_event(&h, CONTROL_EVENT_LOOP_COMPLETED),
          "the boundary must emit LOOP_COMPLETED");
    CHECK(h.event_count > before, "LOOP_COMPLETED must be a real emission");
    /* The event carries the exact count (§3.3). */
    int idx = harness_find_event(&h, CONTROL_EVENT_LOOP_COMPLETED, 0);
    CHECK(idx >= 0 && h.event_lens[idx] == 4 && rd32(h.event_payloads[idx]) == 1,
          "LOOP_COMPLETED must carry the exact loop_count");

    /* The loop restarts at the boundary: the reporter consumes the neutral, and
     * record 0 is applied immediately after — no other report in between. */
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 100);
    uint8_t s0[9];
    record_state(s0, 0);
    CHECK(reporter_last_is(&r, s0), "the loop must restart at record 0");
    CHECK(h.ctl.status.current_frame == 0, "the restart must reset current_frame");
    CHECK(h.ctl.status.loop_count == 1, "the restart must not bump loop_count again");
}

/* The whole point of exact-millisecond deadlines: tick quantisation moves *when*
 * a boundary is observed, never how long a loop lasts. A 25 ms loop observed on
 * a 10 ms grid must produce ~one loop per 25 ms of loop clock, not per 30 ms.
 * This assertion distinguishes the two: a drifted walk would count 20. */
static void test_no_drift_across_loops(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[2] = {7, 18}; /* loop_ms = 25 */

    arm_plan(&h, &ex, buf, holds, 2, 25, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);

    /* 600 ms of observations on a 10 ms grid. The boundary falls when the
     * observation reaches it, so 25·k is crossed at the next multiple of 10:
     * 24 boundaries fit in 600 ms. A walk that quantised each loop up to the
     * tick would count 20 (600/30). */
    for (uint32_t t = 10; t <= 600; t += 10) {
        reporter_consume(&r);
        control_executor_step(&ex, &h.ctl, t);
    }
    CHECK(h.ctl.status.loop_count == 24,
          "600 ms of a 25 ms loop is 24 boundaries (a drifted walk gives 20), got %u",
          h.ctl.status.loop_count);
}

/* §3.3: a short macro cannot saturate the link. Without a STATUS reply to
 * supersede it, at most one LOOP_COMPLETED leaves the device — while
 * `loop_count` stays exact. */
static void test_loop_completed_rate_limited(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[1] = {10}; /* loop_ms = 10, one loop per tick */

    arm_plan(&h, &ex, buf, holds, 1, 10, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);

    for (uint32_t t = 10; t <= 200; t += 10) {
        reporter_consume(&r);
        control_executor_step(&ex, &h.ctl, t);
    }
    CHECK(h.ctl.status.loop_count == 20, "loop_count must be exact: %u", h.ctl.status.loop_count);
    int emitted = 0;
    for (int i = 0; i < h.event_count; i++) {
        if (h.event_kinds[i] == CONTROL_EVENT_LOOP_COMPLETED) {
            emitted++;
        }
    }
    CHECK(emitted == 1, "20 boundaries with no poll must emit exactly one event, got %d",
          emitted);

    /* A STATUS reply supersedes it, so the next boundary may emit again. */
    control_event_note_status_served(&h.ctl);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 210);
    emitted = 0;
    for (int i = 0; i < h.event_count; i++) {
        if (h.event_kinds[i] == CONTROL_EVENT_LOOP_COMPLETED) {
            emitted++;
        }
    }
    CHECK(emitted == 2, "a served STATUS must re-arm the rate limit, got %d", emitted);
    CHECK(h.ctl.status.loop_count == 21, "loop_count must stay exact across the limit");
}

/* §2.9: a CONFIG accepted during MACRO waits for the loop boundary, and the
 * executor is what crosses it. */
static void test_config_applies_at_the_boundary(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[1] = {20};

    arm_plan(&h, &ex, buf, holds, 1, 20, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);

    uint8_t cfg[3] = {0x14, 0x00, 0x01}; /* 20 ms, led on */
    harness_request(&h, CONTROL_VERB_CONFIG, cfg, sizeof(cfg));
    CHECK(h.config_calls == 0, "CONFIG during MACRO must not apply immediately");
    CHECK(h.ctl.config_pending, "CONFIG during MACRO must be held pending");

    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 20);
    CHECK(h.config_calls == 1, "the loop boundary must apply the pending CONFIG");
    CHECK(h.last_config_ms == 20 && h.last_config_led == 1, "the applied values must be the sent ones");
    CHECK(!h.ctl.config_pending, "the boundary must clear the pending flag");
}

/* The bridge the firmware uses: the verb layer's `stop` effect is the executor's
 * own stop, so `STOP`, the BOOT stop and `UNPLACE_AMIIBO` all funnel into one
 * definition of "the mode ended". Declared here because the bridge is referenced
 * by the AMIIBO test below it. */
static void executor_stop_bridge(void *ctx, uint8_t reason)
{
    (void)reason;
    control_executor_stop((control_executor_t *)ctx);
}

/* §4.6: neutral is the last write of a mode. STOP commits it. */
static void test_stop_commits_the_neutral(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[2] = {500, 500};

    arm_plan(&h, &ex, buf, holds, 2, 1000, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);
    uint8_t s0[9];
    record_state(s0, 0);
    CHECK(reporter_last_is(&r, s0), "sanity: a record is held");

    control_executor_stop(&ex);
    CHECK(reporter_last_is(&r, control_executor_neutral), "STOP must leave the neutral last");
    CHECK(!control_executor_running(&ex), "STOP must halt the walk");

    /* Idempotent in *effect*: a second stop re-commits the same nine bytes, so
     * the last write is still the neutral either way. That is what lets the verb
     * layer's `stop`, the BOOT stop and the fault path share it — and it is
     * deliberately not "does nothing when already stopped", which is what let
     * the AMIIBO gap through (see the test below). */
    control_executor_stop(&ex);
    CHECK(reporter_last_is(&r, control_executor_neutral),
          "a second stop must still leave the neutral last");

    /* A stopped executor observes the clock as a no-op. */
    int writes = r.n;
    control_executor_step(&ex, &h.ctl, 5000);
    CHECK(r.n == writes, "a stopped executor must not write on step");
}

/*
 * §4.3's `AMIIBO` + `STOP` row requires a neutral release, and the executor is
 * `STOPPED` throughout `AMIIBO` because nothing arms it. So the release must not
 * be conditional on having been running — that is the gap an early return in
 * `control_executor_stop` opens, and it is invisible on the HID wire.
 */
static void test_stop_releases_from_amibo(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);

    /* Wire the verb layer's `stop` effect to the real executor, as the firmware
     * does, so the AMIIBO exit reaches `control_executor_stop`. */
    control_effects_t fx = h.ctl.fx;
    fx.ctx = &ex;
    fx.stop = executor_stop_bridge;
    control_state_set_effects(&h.ctl, &fx);

    /* Place a tag so the device is in AMIIBO with no executor ever armed. */
    uint8_t tag[CONTROL_TAG_SIZE];
    build_tag(tag, 0x21, 0x42);
    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.ctl.status.mode == CONTROL_MODE_AMIIBO, "sanity: a placement enters AMIIBO");
    CHECK(!control_executor_running(&ex), "nothing arms the executor in AMIIBO");

    /* §4.3: AMIIBO + STOP -> IDLE, tag unplaced, neutral released. */
    int before = r.n;
    harness_request(&h, CONTROL_VERB_STOP, NULL, 0);
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE, "AMIIBO + STOP must return to IDLE");
    CHECK(h.ctl.status.last_stop_reason == CONTROL_STOP_CONTAINER,
          "AMIIBO + STOP sets CONTAINER_STOP");
    CHECK(r.n > before, "AMIIBO + STOP must write the neutral, not skip it");
    CHECK(reporter_last_is(&r, control_executor_neutral),
          "the AMIIBO exit must leave the neutral as the last write (§4.6)");
}

/* §4.7: a console re-subscribe re-arms neutral, and the loop CONTINUES at its
 * current frame rather than restarting at frame 0. */
static void test_rearm_continues_at_the_current_frame(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[3] = {100, 100, 100};

    arm_plan(&h, &ex, buf, holds, 3, 300, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 100); /* now on record 1 */
    CHECK(h.ctl.status.current_frame == 1, "sanity: on record 1");

    control_executor_rearm(&ex, &h.ctl, 150);
    CHECK(reporter_last_is(&r, control_executor_neutral),
          "the re-subscribe re-arm must commit the neutral");

    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 150);
    uint8_t s1[9];
    record_state(s1, 1);
    CHECK(reporter_last_is(&r, s1), "the loop must resume at its current frame, not frame 0");
    CHECK(h.ctl.status.current_frame == 1, "the resume must not move current_frame");
    CHECK(h.ctl.status.loop_count == 0, "a re-arm must not count a loop");
}

/* §4.6's fault path: malformed plan, frame index out of range, commit check
 * mismatch. It exits to IDLE with the neutral applied, `last_stop_reason` left
 * at NONE, and `last_error` carrying the reason (§3.4's NONE+set row) — no new
 * wire value, because §2.5's code set is closed. */
static void test_fault_exits_idle_with_neutral(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[2] = {50, 50};

    arm_plan(&h, &ex, buf, holds, 2, 100, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);

    /* Force the frame-index fault the walk's guard exists for. The report is
     * healthy, so §4.6's neutral can be — and must be — committed. */
    ex.frame = 9;
    int before = h.event_count;
    control_executor_step(&ex, &h.ctl, 50);

    CHECK(!control_executor_running(&ex), "a fault must halt the walk");
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE, "a fault must exit to IDLE");
    CHECK(h.ctl.status.last_stop_reason == CONTROL_STOP_NONE,
          "a fault is not a container or BOOT stop: NONE, disambiguated by last_error");
    CHECK(h.ctl.status.last_error_code == CONTROL_ERR_BAD_PLAN,
          "a fault must set last_error to BAD_PLAN, got %u", h.ctl.status.last_error_code);
    CHECK(h.ctl.status.last_error_detail == 0,
          "§2.5 types BAD_PLAN's detail as 0, got %u", h.ctl.status.last_error_detail);
    int idx = harness_find_event(&h, CONTROL_EVENT_ERROR_RAISED, before);
    CHECK(idx >= 0, "a fault must emit ERROR_RAISED");
    if (idx >= 0) {
        CHECK(h.event_lens[idx] == 5 && h.event_payloads[idx][0] == CONTROL_ERR_BAD_PLAN,
              "ERROR_RAISED must carry the same code as STATUS.last_error");
    }
    CHECK(reporter_last_is(&r, control_executor_neutral),
          "the fault path must have committed the neutral last");

    /* And it is not resumable without a fresh arm. */
    int writes = r.n;
    control_executor_step(&ex, &h.ctl, 500);
    CHECK(r.n == writes, "a faulted executor must stay halted");
}

/* A report that cannot be written at all — the adapter's fatal case. The mode
 * still ends and the walk still halts; §4.6's neutral is attempted and cannot
 * land, because there is nothing to land in. The honest reading of the
 * invariant is "neutral is the last write of the mode", and here there is no
 * write to make. */
static void test_unwritable_report_still_exits_idle(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[2] = {50, 50};

    arm_plan(&h, &ex, buf, holds, 2, 100, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);
    int writes = r.n;

    r.refuse = true;
    control_executor_step(&ex, &h.ctl, 50);

    CHECK(!control_executor_running(&ex), "an unwritable report must halt the walk");
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE, "an unwritable report must exit to IDLE");
    CHECK(h.ctl.status.last_error_code == CONTROL_ERR_BAD_PLAN,
          "an unwritable report is a fault, got %u", h.ctl.status.last_error_code);
    CHECK(r.n == writes, "an unwritable report must not fabricate a write");
}

/* A degenerate plan must not spin the task: at most one loop boundary per step,
 * whatever loop_ms is. §5.5 refuses `loop_ms = 0` in the compiler; the walk
 * stays bounded anyway. */
static void test_degenerate_zero_loop_is_bounded(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[1] = {0};

    arm_plan(&h, &ex, buf, holds, 1, 0, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);
    for (uint32_t t = 10; t <= 100; t += 10) {
        reporter_consume(&r);
        control_executor_step(&ex, &h.ctl, t);
    }
    /* 10 steps, each crossing at most one boundary. */
    CHECK(h.ctl.status.loop_count <= 11,
          "one boundary per step at most, got %u in 10 steps", h.ctl.status.loop_count);
}

/* The out-of-range guard, exercised directly: the walk must fault rather than
 * read past the plan. This is the invariant behind §4.6's "frame index out of
 * range", kept reachable so it cannot rot. */
static void test_frame_index_out_of_range_faults(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);
    uint8_t buf[CONTROL_TEST_PLAN_CAP];
    const uint16_t holds[2] = {50, 50};

    arm_plan(&h, &ex, buf, holds, 2, 100, 0);
    reporter_consume(&r);
    control_executor_step(&ex, &h.ctl, 0);
    control_mode_enter(&h.ctl, CONTROL_MODE_MACRO);

    /* Force the inconsistency the guard exists for. */
    ex.count = 1;
    ex.frame = 7;
    control_executor_step(&ex, &h.ctl, 50);

    CHECK(!control_executor_running(&ex), "an out-of-range frame must halt");
    CHECK(h.ctl.status.last_error_code == CONTROL_ERR_BAD_PLAN,
          "an out-of-range frame is a bad plan, got %u", h.ctl.status.last_error_code);
    CHECK(h.ctl.status.last_error_detail == 0,
          "§2.5 types BAD_PLAN's detail as 0, got %u", h.ctl.status.last_error_detail);
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE, "the guard must exit to IDLE");
}

/* A step while stopped is inert. A stop while stopped is *not*: it still commits
 * the neutral, because §4.6 makes the neutral the last write of the mode and the
 * mode can end without this executor ever having been armed (`AMIIBO` + `STOP`,
 * §4.3). Re-writing the same nine bytes is cheap; a missing release is the
 * invariant broken. */
static void test_stopped_is_inert(void)
{
    harness_t h;
    harness_init(&h);
    reporter_t r;
    control_executor_t ex;
    ex_init(&ex, &r, &h);

    control_executor_step(&ex, &h.ctl, 100);
    CHECK(r.n == 0, "a never-armed executor must not write on step");
    control_executor_rearm(&ex, &h.ctl, 100);
    CHECK(r.n == 0, "a re-arm while stopped must be a no-op");

    int before = r.n;
    control_executor_stop(&ex);
    CHECK(r.n > before && reporter_last_is(&r, control_executor_neutral),
          "a stop while stopped must still commit the neutral (§4.6, the AMIIBO case)");

    int writes = r.n;
    control_executor_step(&ex, &h.ctl, 200);
    CHECK(r.n == writes, "a stopped executor must not write on step");
}

int main(void)
{
    test_neutral_template();
    test_arm_refuses_an_invalid_plan();
    test_dispatcher_rejects_a_refused_arm();
    test_arm_emits_neutral_first();
    test_walk_holds_each_record();
    test_boundary_neutral_then_restart();
    test_no_drift_across_loops();
    test_loop_completed_rate_limited();
    test_config_applies_at_the_boundary();
    test_stop_commits_the_neutral();
    test_stop_releases_from_amibo();
    test_rearm_continues_at_the_current_frame();
    test_fault_exits_idle_with_neutral();
    test_unwritable_report_still_exits_idle();
    test_degenerate_zero_loop_is_bounded();
    test_frame_index_out_of_range_faults();
    test_stopped_is_inert();
    if (g_failures != 0) {
        fprintf(stderr, "control executor: %d/%d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("control executor ok: %d checks\n", g_checks);
    return 0;
}
