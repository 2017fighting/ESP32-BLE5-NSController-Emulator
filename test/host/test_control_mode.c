/*
 * Host-side assertions for the mode state model, the panic stop and the
 * STATUS/EVENT surface (spec chapter 3, chapter 4, §7.3 step 4; issue #23).
 *
 * The acceptance property of the ticket lives here: every rejection in §4.3's
 * transition table is exercised, the panic stop stops on the press edge and
 * forgets on the release edge of a long press, and STATUS and EVENT match
 * chapter 3 field for field.
 *
 * Build:
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include -Itest/host \
 *      -o test_control_mode \
 *      test/host/test_control_mode.c \
 *      main/src/protocol/control/control_frame.c \
 *      main/src/protocol/control/control_dispatch.c \
 *      main/src/protocol/control/control_bulk.c \
 *      main/src/protocol/control/control_events.c \
 *      main/src/protocol/control/control_mode.c
 * Run:
 *   ./test_control_mode
 */

#include "control_test_util.h"

#include "protocol/control/control_events.h"
#include "protocol/control/control_mode.h"

static uint8_t g_plan[CONTROL_TEST_PLAN_CAP];

/* --------------------------------------------------------------- assertions */

static void check_last_event(harness_t *h, uint8_t kind, const uint8_t *payload, size_t len)
{
    CHECK(h->event_count > 0, "expected a %u event, none was emitted", kind);
    if (h->event_count == 0) {
        return;
    }
    int i = h->event_count - 1;
    CHECK(h->event_kinds[i] == kind, "expected event kind %u last, got %u", kind,
          h->event_kinds[i]);
    CHECK(h->event_lens[i] == len, "event kind %u must carry %zu payload bytes, got %u", kind,
          len, h->event_lens[i]);
    if (payload != NULL && h->event_lens[i] == len) {
        CHECK(memcmp(h->event_payloads[i], payload, len) == 0,
              "event kind %u payload differs", kind);
    }
}

/* STATUS read out of the harness' last reply, for the §3.2 offset assertions. */
static const uint8_t *status_payload(harness_t *h)
{
    static uint8_t storage[CONTROL_WIRE_MAX];
    control_frame_t f;
    if (read_status(h, &f, storage, sizeof(storage)) != 0 || f.len != 47) {
        return NULL;
    }
    return f.payload;
}

/* ----------------------------------------------------------- mode and events */

static void test_mode_events(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t hash[16];
    fill_hash(hash, 0x11);
    uint32_t total = build_plan(g_plan, 2, 100);

    /* §3.3: a commit emits PLAN_COMMITTED carrying the plan hash. */
    plan_announce(&h, total, hash);
    plan_chunks(&h, g_plan, total);
    plan_commit(&h, total, hash);
    check_last_event(&h, CONTROL_EVENT_PLAN_COMMITTED, hash, 16);

    /* IDLE -> MACRO. */
    int before = h.event_count;
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.ctl.status.mode == CONTROL_MODE_MACRO, "START must enter MACRO");
    CHECK(h.event_count == before + 1 && h.event_kinds[h.event_count - 1] ==
              CONTROL_EVENT_MODE_CHANGED,
          "START must emit exactly one MODE_CHANGED");
    uint8_t macro = CONTROL_MODE_MACRO;
    check_last_event(&h, CONTROL_EVENT_MODE_CHANGED, &macro, 1);

    /* MACRO -> IDLE, CONTAINER_STOP. */
    harness_request(&h, CONTROL_VERB_STOP, NULL, 0);
    uint8_t idle = CONTROL_MODE_IDLE;
    check_last_event(&h, CONTROL_EVENT_MODE_CHANGED, &idle, 1);
    CHECK(h.ctl.status.last_stop_reason == CONTROL_STOP_CONTAINER,
          "STOP must set CONTAINER_STOP");

    /* IDLE -> AMIIBO, with the tag placement before the mode edge. */
    uint8_t tag[CONTROL_TAG_SIZE];
    build_tag(tag, 0x11, 0x22);
    before = h.event_count;
    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.event_count == before + 2, "a fresh placement must emit TAG_PLACED then MODE_CHANGED");
    CHECK(h.event_kinds[before] == CONTROL_EVENT_TAG_PLACED &&
              h.event_kinds[before + 1] == CONTROL_EVENT_MODE_CHANGED,
          "TAG_PLACED must precede the mode edge");
    const uint8_t expect_uid[7] = {0x04, 0x11, 0xFE, 0xCA, 0x22, 0x6C, 0x81};
    CHECK(h.event_lens[before] == 7 &&
              memcmp(h.event_payloads[before], expect_uid, 7) == 0,
          "TAG_PLACED must carry the seven-byte identity");
    uint8_t amiibo = CONTROL_MODE_AMIIBO;
    check_last_event(&h, CONTROL_EVENT_MODE_CHANGED, &amiibo, 1);

    /* AMIIBO -> IDLE by UNPLACE_AMIIBO: TAG_UNPLACED then MODE_CHANGED. */
    before = h.event_count;
    harness_request(&h, CONTROL_VERB_UNPLACE_AMIIBO, NULL, 0);
    CHECK(h.event_count == before + 2 && h.event_kinds[before] == CONTROL_EVENT_TAG_UNPLACED &&
              h.event_kinds[before + 1] == CONTROL_EVENT_MODE_CHANGED,
          "UNPLACE_AMIIBO must emit TAG_UNPLACED then MODE_CHANGED");
    check_last_event(&h, CONTROL_EVENT_MODE_CHANGED, &idle, 1);
}

static void test_amiibo_replace_events(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t tag_a[CONTROL_TAG_SIZE];
    uint8_t tag_b[CONTROL_TAG_SIZE];
    build_tag(tag_a, 0x11, 0x22);
    build_tag(tag_b, 0x33, 0x44);

    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag_a, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);

    /* §11 trace B step 8: the atomic replace emits TAG_UNPLACED then TAG_PLACED,
     * and no MODE_CHANGED — the mode never left AMIIBO. */
    int before = h.event_count;
    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag_b, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.event_count == before + 2, "a replace must emit exactly two tag events");
    CHECK(h.event_kinds[before] == CONTROL_EVENT_TAG_UNPLACED &&
              h.event_kinds[before + 1] == CONTROL_EVENT_TAG_PLACED,
          "the replace gap must emit TAG_UNPLACED then TAG_PLACED");
    const uint8_t expect_uid[7] = {0x04, 0x33, 0xFE, 0xCA, 0x44, 0x6C, 0x81};
    CHECK(memcmp(h.event_payloads[before + 1], expect_uid, 7) == 0,
          "the replace's TAG_PLACED must carry the new identity");
}

/* Every rejection in §4.3's table: a typed ERROR and state untouched. */
static void test_transition_table_rejections(void)
{
    struct row {
        uint8_t mode;
        uint8_t verb;
        uint8_t code;
        uint32_t detail;
    };
    const struct row rows[] = {
        {CONTROL_MODE_IDLE, CONTROL_VERB_START, CONTROL_ERR_NO_PLAN, 0},
        {CONTROL_MODE_MACRO, CONTROL_VERB_START, CONTROL_ERR_ALREADY_RUNNING, 0},
        {CONTROL_MODE_AMIIBO, CONTROL_VERB_START, CONTROL_ERR_BAD_STATE, CONTROL_MODE_AMIIBO},
    };

    for (size_t i = 0; i < sizeof(rows) / sizeof(rows[0]); i++) {
        harness_t h;
        harness_init(&h);
        h.ctl.status.mode = rows[i].mode;
        int before_events = h.event_count;
        harness_request(&h, rows[i].verb, NULL, 0);
        CHECK(h.errors == 1 && h.err_code == rows[i].code && h.err_detail == rows[i].detail,
              "mode %u + verb %u must be code %u detail %u, got %u/%u", rows[i].mode,
              rows[i].verb, rows[i].code, rows[i].detail, h.err_code, h.err_detail);
        CHECK(h.ctl.status.mode == rows[i].mode, "a rejection must not change the mode");
        CHECK(h.event_count == before_events, "a rejection must emit no event");
    }

    /* LOAD_PLAN while MACRO and while AMIIBO: BAD_STATE(mode). */
    const uint8_t modes[2] = {CONTROL_MODE_MACRO, CONTROL_MODE_AMIIBO};
    for (int i = 0; i < 2; i++) {
        harness_t h;
        harness_init(&h);
        uint8_t hash[16];
        fill_hash(hash, 0x21);
        h.ctl.status.mode = modes[i];
        plan_announce(&h, 64, hash);
        CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_STATE && h.err_detail == modes[i],
              "LOAD_PLAN while mode %u must be BAD_STATE(mode)", modes[i]);
        CHECK(h.ctl.stage == CONTROL_STAGE_NONE, "a rejected announce must not open staging");
    }

    /* PLACE_AMIIBO while MACRO: BAD_STATE(mode), the crux of ADR-0007. */
    {
        harness_t h;
        harness_init(&h);
        h.ctl.status.mode = CONTROL_MODE_MACRO;
        tag_announce(&h, CONTROL_TAG_SIZE);
        CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_BAD_STATE &&
                  h.err_detail == CONTROL_MODE_MACRO,
              "PLACE_AMIIBO while MACRO must be BAD_STATE(mode)");
        CHECK(!h.ctl.tag_placed && h.ctl.status.mode == CONTROL_MODE_MACRO,
              "the rejection must leave the macro running and no tag placed");
    }

    /* Nothing was queued: the same START still works once the mode is legal and
     * a plan is committed. */
    {
        harness_t h;
        harness_init(&h);
        h.ctl.status.mode = CONTROL_MODE_MACRO;
        harness_request(&h, CONTROL_VERB_START, NULL, 0);
        CHECK(h.err_code == CONTROL_ERR_ALREADY_RUNNING, "START while MACRO is ALREADY_RUNNING");
        h.ctl.status.mode = CONTROL_MODE_IDLE;
        commit_plan(&h, g_plan, 1, 0x31);
        harness_request(&h, CONTROL_VERB_START, NULL, 0);
        CHECK(h.ctl.status.mode == CONTROL_MODE_MACRO && h.start_calls == 1,
              "a rejected START must not have been queued into a later one");
    }
}

/* -------------------------------------------------------- LOOP_COMPLETED limit */

static void test_loop_completed_rate_limit(void)
{
    harness_t h;
    harness_init(&h);

    CHECK(control_event_loop_completed(&h.ctl, 1), "the first loop boundary must emit");
    check_last_event(&h, CONTROL_EVENT_LOOP_COMPLETED, (const uint8_t[]){1, 0, 0, 0}, 4);
    CHECK(h.ctl.status.loop_count == 1, "loop_count in STATUS must stay exact");

    CHECK(!control_event_loop_completed(&h.ctl, 2),
          "a boundary before a poll must be suppressed, not queued");
    CHECK(h.ctl.status.loop_count == 2, "the suppressed boundary must still advance loop_count");
    CHECK(harness_find_event(&h, CONTROL_EVENT_LOOP_COMPLETED, 0) == h.event_count - 1,
          "the suppression must not have emitted a second event");

    /* A STATUS reply supersedes the pending event. */
    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    CHECK(control_event_loop_completed(&h.ctl, 3), "a poll must re-open the rate limit");
    check_last_event(&h, CONTROL_EVENT_LOOP_COMPLETED, (const uint8_t[]){3, 0, 0, 0}, 4);

    /* The rate limit is per poll, not per loop: the second boundary after the
     * poll is suppressed again. */
    CHECK(!control_event_loop_completed(&h.ctl, 4), "the limit must re-close after one emit");

    /* A new run must not inherit the previous run's un-superseded event. */
    harness_t g;
    harness_init(&g);
    commit_plan(&g, g_plan, 1, 0x35);
    harness_request(&g, CONTROL_VERB_START, NULL, 0);
    CHECK(control_event_loop_completed(&g.ctl, 1), "the new run's first boundary must emit");
    CHECK(!control_event_loop_completed(&g.ctl, 2), "the limit holds within the run");
    harness_request(&g, CONTROL_VERB_STOP, NULL, 0);
    harness_request(&g, CONTROL_VERB_START, NULL, 0);
    CHECK(control_event_loop_completed(&g.ctl, 1),
          "a new START must not inherit the previous run's suppression");
}

/* -------------------------------------------------------------- the ten kinds */

static void test_event_catalogue(void)
{
    harness_t h;
    harness_init(&h);

    /* BOOT carries the boot id (§2.6, §3.3). */
    control_event_boot(&h.ctl);
    check_last_event(&h, CONTROL_EVENT_BOOT, (const uint8_t[]){0x44, 0x33, 0x22, 0x11}, 4);

    /* PLAN_DISCARDED and TAG_UNPLACED and SCAN_ENDED carry nothing. */
    control_event_plan_discarded(&h.ctl);
    check_last_event(&h, CONTROL_EVENT_PLAN_DISCARDED, NULL, 0);
    control_event_tag_unplaced(&h.ctl);
    check_last_event(&h, CONTROL_EVENT_TAG_UNPLACED, NULL, 0);
    control_event_scan_ended(&h.ctl);
    check_last_event(&h, CONTROL_EVENT_SCAN_ENDED, NULL, 0);

    /* CONSOLE_LINK's reason is a u16: 531 (the measured disconnect) must fit. */
    control_event_console_link(&h.ctl, CONTROL_CONSOLE_EVENT_DISCONNECTED, 531);
    const uint8_t console[3] = {CONTROL_CONSOLE_EVENT_DISCONNECTED, 0x13, 0x02};
    check_last_event(&h, CONTROL_EVENT_CONSOLE_LINK, console, 3);

    /* Every event is an EVENT frame with `verb` 0 (§2.4, §3.3). */
    uint8_t wire[CONTROL_WIRE_MAX];
    size_t n = control_event_encode(CONTROL_EVENT_BOOT, (const uint8_t[]){1, 2, 3, 4}, 4, wire,
                                    sizeof(wire));
    control_frame_t f;
    uint8_t storage[CONTROL_WIRE_MAX];
    CHECK(n > 0 && wire_parse(wire, n, &f, storage, sizeof(storage)) == 0 &&
              f.type == CONTROL_TYPE_EVENT && f.verb == 0 && f.len == 5,
          "an EVENT must be type 3, verb 0, and carry kind + payload");
}

/* ------------------------------------------------------------ ERROR_RAISED */

static void test_error_raised(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t out[CONTROL_WIRE_MAX];

    /* The container's request in hand is answered by control_reject(), which is
     * the reply and raises no event. */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.errors == 1 && h.err_code == CONTROL_ERR_NO_PLAN, "START without a plan is NO_PLAN");
    CHECK(!harness_has_event(&h, CONTROL_EVENT_ERROR_RAISED),
          "an ERROR that answers the request in hand must not raise ERROR_RAISED");

    /* A device-side fault (§4.6) is the event's case. */
    size_t n = control_raise_error(&h.ctl, CONTROL_ERR_BAD_PLAN, 7, out, sizeof(out));
    CHECK(n > 0, "control_raise_error must return an ERROR reply");
    uint8_t code = 0;
    uint32_t detail = 0;
    CHECK(decode_error_reply(out, n, &code, &detail) == 0 && code == CONTROL_ERR_BAD_PLAN &&
              detail == 7,
          "the raised ERROR must be the typed reply");
    const uint8_t raised[5] = {CONTROL_ERR_BAD_PLAN, 7, 0, 0, 0};
    check_last_event(&h, CONTROL_EVENT_ERROR_RAISED, raised, 5);
    CHECK(h.ctl.status.last_error_code == CONTROL_ERR_BAD_PLAN &&
              h.ctl.status.last_error_detail == 7,
          "a raised ERROR must be legible from the next STATUS poll (§3.2)");
}

/* ------------------------------------------------- console link and bonding */

static void test_console_link_and_bond(void)
{
    harness_t h;
    harness_init(&h);

    control_set_console_link(&h.ctl, CONTROL_CONSOLE_EVENT_CONNECTED, 0);
    CHECK(h.ctl.status.console_link == CONTROL_CONSOLE_CONNECTED,
          "CONNECTED must set the §3.2 console_link value");
    const uint8_t connected[3] = {CONTROL_CONSOLE_EVENT_CONNECTED, 0, 0};
    check_last_event(&h, CONTROL_EVENT_CONSOLE_LINK, connected, 3);

    control_set_console_link(&h.ctl, CONTROL_CONSOLE_EVENT_RESUBSCRIBED, 0);
    CHECK(h.ctl.status.console_link == CONTROL_CONSOLE_CONNECTED,
          "a re-subscribe keeps the link CONNECTED");
    const uint8_t resub[3] = {CONTROL_CONSOLE_EVENT_RESUBSCRIBED, 0, 0};
    check_last_event(&h, CONTROL_EVENT_CONSOLE_LINK, resub, 3);

    /* The measured console-sleep reason is 531 = 0x0213, which needs the u16. */
    control_set_console_link(&h.ctl, CONTROL_CONSOLE_EVENT_DISCONNECTED, 531);
    CHECK(h.ctl.status.console_link == CONTROL_CONSOLE_ADVERTISING,
          "DISCONNECTED must return the link to ADVERTISING");
    const uint8_t gone[3] = {CONTROL_CONSOLE_EVENT_DISCONNECTED, 0x13, 0x02};
    check_last_event(&h, CONTROL_EVENT_CONSOLE_LINK, gone, 3);

    /* The bond has no event kind: it is reported, not announced. */
    int before = h.event_count;
    control_set_bond(&h.ctl, CONTROL_BOND_PAIRED);
    CHECK(h.ctl.status.bond == CONTROL_BOND_PAIRED, "the bond axis must be PAIRED");
    CHECK(h.event_count == before, "moving the bond must emit no event");
}

/* --------------------------------------------------------------- panic stop */

/* Drives a press to its debounced confirmation. The first high sample is the
 * reference §4.5 requires, so this is a real high->low edge. */
static uint32_t panic_press(harness_t *h, uint32_t t0)
{
    CHECK(control_panic_step(&h->ctl, &h->panic, false, t0) == CONTROL_PANIC_NONE,
          "a high sample must be a no-op");
    CHECK(control_panic_step(&h->ctl, &h->panic, true, t0 + 10) == CONTROL_PANIC_NONE,
          "the press edge must start the debounce, not fire");
    return t0 + 10 + CONTROL_PANIC_DEBOUNCE_MS;
}

static void test_panic_short_press_macro(void)
{
    harness_t h;
    harness_init(&h);
    commit_plan(&h, g_plan, 2, 0x41);
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.ctl.status.mode == CONTROL_MODE_MACRO, "the fixture must be in MACRO");
    control_set_bond(&h.ctl, CONTROL_BOND_PAIRED);

    uint32_t t = panic_press(&h, 0);
    int before = h.event_count;
    CHECK(control_panic_step(&h.ctl, &h.panic, true, t) == CONTROL_PANIC_STOPPED,
          "the debounced press edge must stop");

    /* §4.3/§4.5: IDLE, neutral release, plan retained, BOOT_LOCAL, bond kept. */
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE, "the press edge must return to IDLE");
    CHECK(h.ctl.status.last_stop_reason == CONTROL_STOP_BOOT_LOCAL,
          "the BOOT stop must set BOOT_LOCAL");
    CHECK(h.stop_calls == 1 && h.last_stop_reason == CONTROL_STOP_BOOT_LOCAL,
          "the effects double must see BOOT_LOCAL (the neutral release)");
    CHECK(h.ctl.plan_committed, "a short press must retain the committed plan");
    CHECK(h.ctl.status.bond == CONTROL_BOND_PAIRED, "the panic stop must never unpair");
    CHECK(h.event_count == before + 1 &&
              h.event_kinds[h.event_count - 1] == CONTROL_EVENT_MODE_CHANGED,
          "the mode edge must emit MODE_CHANGED and nothing else");
    uint8_t idle = CONTROL_MODE_IDLE;
    check_last_event(&h, CONTROL_EVENT_MODE_CHANGED, &idle, 1);

    /* The release of a short press forgets nothing. */
    CHECK(control_panic_step(&h.ctl, &h.panic, false, t + CONTROL_PANIC_LONG_MS - 100) ==
              CONTROL_PANIC_NONE,
          "a short release must be a no-op");
    CHECK(h.ctl.plan_committed && !harness_has_event(&h, CONTROL_EVENT_PLAN_DISCARDED),
          "a short press must not discard the plan");
}

static void test_panic_long_press_forgets(void)
{
    harness_t h;
    harness_init(&h);
    commit_plan(&h, g_plan, 2, 0x51);
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    control_set_bond(&h.ctl, CONTROL_BOND_PAIRED);
    uint32_t t = panic_press(&h, 0);

    CHECK(control_panic_step(&h.ctl, &h.panic, true, t) == CONTROL_PANIC_STOPPED,
          "the press edge must stop first");
    CHECK(control_panic_step(&h.ctl, &h.panic, true, t + CONTROL_PANIC_LONG_MS) ==
              CONTROL_PANIC_NONE,
          "still pressed: nothing else happens until release");
    int before = h.event_count;
    CHECK(control_panic_step(&h.ctl, &h.panic, false,
                             t + CONTROL_PANIC_LONG_MS + CONTROL_PANIC_POLL_MS) ==
              CONTROL_PANIC_FORGOT,
          "the release of a long press must forget");

    CHECK(!h.ctl.plan_committed && h.ctl.plan == NULL && h.ctl.plan_len == 0,
          "the long press must discard the committed plan");
    CHECK(h.ctl.status.plan_state == CONTROL_PLAN_NONE, "STATUS must report plan=none");
    CHECK(h.event_count == before + 1 &&
              h.event_kinds[h.event_count - 1] == CONTROL_EVENT_PLAN_DISCARDED,
          "the forget must emit PLAN_DISCARDED");
    CHECK(h.ctl.status.bond == CONTROL_BOND_PAIRED, "the long press must still keep the bond");
    CHECK(h.ctl.status.last_stop_reason == CONTROL_STOP_BOOT_LOCAL,
          "the stop reason stays BOOT_LOCAL after the forget");

    /* A retained plan would re-arm with one START; a forgotten one cannot. */
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    CHECK(h.err_code == CONTROL_ERR_NO_PLAN, "a discarded plan must need a fresh LOAD_PLAN");
}

static void test_panic_amiibo_short_press(void)
{
    harness_t h;
    harness_init(&h);
    uint8_t tag[CONTROL_TAG_SIZE];
    build_tag(tag, 0x11, 0x22);
    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.ctl.status.mode == CONTROL_MODE_AMIIBO, "the fixture must be in AMIIBO");

    uint32_t t = panic_press(&h, 0);
    CHECK(control_panic_step(&h.ctl, &h.panic, true, t) == CONTROL_PANIC_STOPPED,
          "the press edge must stop AMIIBO");
    CHECK(h.ctl.status.mode == CONTROL_MODE_IDLE && h.ctl.status.tag_state == CONTROL_TAG_NONE,
          "the press must unplace the tag and return to IDLE");
    CHECK(h.stop_calls >= 1, "the tag server must be told to stop answering (through the stop effect)");
    CHECK(h.ctl.tag[4] == 0xCA, "a short press must retain the tag bytes");
    CHECK(harness_has_event(&h, CONTROL_EVENT_TAG_UNPLACED),
          "the unplacement must emit TAG_UNPLACED");

    /* §4.3's AMIIBO BOOT-short row: the release forgets nothing. */
    CHECK(control_panic_step(&h.ctl, &h.panic, false, t + 100) == CONTROL_PANIC_NONE,
          "a short release must be a no-op");
    CHECK(h.ctl.tag[4] == 0xCA && !harness_has_event(&h, CONTROL_EVENT_PLAN_DISCARDED),
          "a short press in AMIIBO must retain the tag bytes and discard nothing");
}

/* §4.3's AMIIBO BOOT-long row: plan and tag bytes both go; the bond stays. */
static void test_panic_amiibo_long_press(void)
{
    harness_t h;
    harness_init(&h);
    commit_plan(&h, g_plan, 2, 0x45);
    control_set_bond(&h.ctl, CONTROL_BOND_PAIRED);
    uint8_t tag[CONTROL_TAG_SIZE];
    build_tag(tag, 0x11, 0x22);
    tag_announce(&h, CONTROL_TAG_SIZE);
    tag_chunks(&h, tag, CONTROL_TAG_SIZE);
    tag_commit(&h, CONTROL_TAG_SIZE);
    CHECK(h.ctl.plan_committed && h.ctl.status.mode == CONTROL_MODE_AMIIBO,
          "a tag placed from IDLE must keep the committed plan");

    uint32_t t = panic_press(&h, 0);
    CHECK(control_panic_step(&h.ctl, &h.panic, true, t) == CONTROL_PANIC_STOPPED,
          "the press edge must stop AMIIBO");
    CHECK(control_panic_step(&h.ctl, &h.panic, false, t + CONTROL_PANIC_LONG_MS + 10) ==
              CONTROL_PANIC_FORGOT,
          "a long release must forget");
    CHECK(!h.ctl.plan_committed && h.ctl.status.plan_state == CONTROL_PLAN_NONE,
          "the long press must discard the plan retained across the placement");
    CHECK(harness_has_event(&h, CONTROL_EVENT_PLAN_DISCARDED),
          "the forgotten plan must emit PLAN_DISCARDED");
    bool tag_zero = true;
    for (size_t i = 0; i < CONTROL_TAG_SIZE; i++) {
        tag_zero = tag_zero && h.ctl.tag[i] == 0;
    }
    CHECK(tag_zero, "the long press must discard the loaded tag bytes");
    CHECK(h.ctl.status.bond == CONTROL_BOND_PAIRED, "the panic stop must keep the bond");
}

static void test_panic_idle_and_boot_low(void)
{
    harness_t h;
    harness_init(&h);

    /* IDLE short press: a no-op, no events, no mode change. */
    uint32_t t = panic_press(&h, 0);
    int before = h.event_count;
    CHECK(control_panic_step(&h.ctl, &h.panic, true, t) == CONTROL_PANIC_NONE,
          "a short press in IDLE must be a no-op");
    CHECK(h.stop_calls == 0 && h.event_count == before, "IDLE must emit nothing");
    CHECK(control_panic_step(&h.ctl, &h.panic, false, t + 10) == CONTROL_PANIC_NONE,
          "the release must be a no-op too");

    /* IDLE long press still clears (§4.5). */
    commit_plan(&h, g_plan, 1, 0x61);
    t = panic_press(&h, 1000);
    CHECK(control_panic_step(&h.ctl, &h.panic, true, t) == CONTROL_PANIC_NONE,
          "IDLE has no mode to stop");
    CHECK(control_panic_step(&h.ctl, &h.panic, false, t + CONTROL_PANIC_LONG_MS + 10) ==
              CONTROL_PANIC_FORGOT,
          "a long press in IDLE must still clear");
    CHECK(!h.ctl.plan_committed && harness_has_event(&h, CONTROL_EVENT_PLAN_DISCARDED),
          "the IDLE long press must discard the plan and say so");

    /* §4.5: a level already low at boot fires nothing, so the CH9102's
     * DTR->GPIO0 wiring cannot become a press (and its release cannot forget). */
    harness_t d;
    harness_init(&d);
    commit_plan(&d, g_plan, 1, 0x71);
    CHECK(control_panic_step(&d.ctl, &d.panic, true, 0) == CONTROL_PANIC_NONE,
          "the first low sample is a reference, not a press");
    CHECK(control_panic_step(&d.ctl, &d.panic, true, CONTROL_PANIC_LONG_MS + 5000) ==
              CONTROL_PANIC_NONE,
          "a low level from boot must never confirm a press");
    CHECK(control_panic_step(&d.ctl, &d.panic, false, CONTROL_PANIC_LONG_MS + 5010) ==
              CONTROL_PANIC_NONE,
          "its release must not forget");
    CHECK(d.ctl.plan_committed, "the DTR case must never silently discard the plan");
    CHECK(!harness_has_event(&d, CONTROL_EVENT_PLAN_DISCARDED), "no discard event may fire");
}

static void test_panic_debounce(void)
{
    harness_t h;
    harness_init(&h);
    commit_plan(&h, g_plan, 1, 0x81);
    harness_request(&h, CONTROL_VERB_START, NULL, 0);

    /* A bounce shorter than the debounce window is not a press. */
    CHECK(control_panic_step(&h.ctl, &h.panic, false, 0) == CONTROL_PANIC_NONE, "reference");
    CHECK(control_panic_step(&h.ctl, &h.panic, true, 10) == CONTROL_PANIC_NONE, "candidate");
    CHECK(control_panic_step(&h.ctl, &h.panic, false, 20) == CONTROL_PANIC_NONE,
          "a bounce must not confirm a press");
    CHECK(h.ctl.status.mode == CONTROL_MODE_MACRO && h.stop_calls == 0,
          "the bounce must not have stopped the macro");

    /* A real press confirms once the pin has been low for the debounce window. */
    CHECK(control_panic_step(&h.ctl, &h.panic, true, 30) == CONTROL_PANIC_NONE, "candidate again");
    CHECK(control_panic_step(&h.ctl, &h.panic, true, 55) == CONTROL_PANIC_NONE,
          "just under the debounce is not enough");
    CHECK(control_panic_step(&h.ctl, &h.panic, true, 60) == CONTROL_PANIC_STOPPED,
          "30 ms of a low pin must confirm the press");
}

/* ------------------------------------------------------- STATUS field-for-field */

static void test_status_fields(void)
{
    harness_t h;
    harness_init(&h);

    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    const uint8_t *s = status_payload(&h);
    CHECK(s != NULL, "STATUS must be the fixed 47-byte payload");
    if (s == NULL) {
        return;
    }
    /* §3.2's offsets, on the §4.8 boot state. */
    CHECK(s[0] == CONTROL_CONSOLE_ADVERTISING && s[1] == CONTROL_BOND_UNPAIRED,
          "boot: console ADVERTISING, bond UNPAIRED");
    CHECK(s[2] == CONTROL_MODE_IDLE && s[3] == CONTROL_PLAN_NONE,
          "boot: mode IDLE, plan NONE");
    bool hash_zero = true;
    for (int i = 4; i < 20; i++) {
        hash_zero = hash_zero && s[i] == 0;
    }
    CHECK(hash_zero, "boot: plan_hash must be zero");
    CHECK(s[20] == 0 && s[21] == 0 && s[22] == 0 && s[23] == 0,
          "boot: plan_frame_count and current_frame are zero");
    CHECK(s[24] == 0 && s[25] == 0 && s[26] == 0 && s[27] == 0, "boot: loop_count is zero");
    CHECK(s[28] == CONTROL_TAG_NONE, "boot: tag_state NONE");
    CHECK(s[36] == CONTROL_POLLING_IDLE, "boot: console_polling IDLE");
    CHECK(s[37] == CONTROL_ERR_NONE && s[38] == 0 && s[39] == 0 && s[40] == 0 && s[41] == 0,
          "boot: last_error is the five-byte NONE pair");
    CHECK(s[42] == CONTROL_STOP_NONE, "boot: last_stop_reason is NONE, not unknown (§3.4)");

    /* The axes are visible where §3.2 puts them. */
    control_set_console_link(&h.ctl, CONTROL_CONSOLE_EVENT_CONNECTED, 0);
    control_set_bond(&h.ctl, CONTROL_BOND_PAIRED);
    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    s = status_payload(&h);
    CHECK(s != NULL && s[0] == CONTROL_CONSOLE_CONNECTED && s[1] == CONTROL_BOND_PAIRED,
          "console_link and bond must report what the BLE callbacks set");

    /* CONTAINER_STOP and BOOT_LOCAL are both readable off the poll (§3.4). */
    commit_plan(&h, g_plan, 1, 0x91);
    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    harness_request(&h, CONTROL_VERB_STOP, NULL, 0);
    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    s = status_payload(&h);
    CHECK(s != NULL && s[42] == CONTROL_STOP_CONTAINER, "STATUS must carry CONTAINER_STOP");

    harness_request(&h, CONTROL_VERB_START, NULL, 0);
    uint32_t t = panic_press(&h, 0);
    control_panic_step(&h.ctl, &h.panic, true, t);
    harness_request(&h, CONTROL_VERB_STATUS, NULL, 0);
    s = status_payload(&h);
    CHECK(s != NULL && s[42] == CONTROL_STOP_BOOT_LOCAL, "STATUS must carry BOOT_LOCAL");
}

int main(void)
{
    test_mode_events();
    test_amiibo_replace_events();
    test_transition_table_rejections();
    test_loop_completed_rate_limit();
    test_event_catalogue();
    test_error_raised();
    test_console_link_and_bond();
    test_panic_short_press_macro();
    test_panic_long_press_forgets();
    test_panic_amiibo_short_press();
    test_panic_amiibo_long_press();
    test_panic_idle_and_boot_low();
    test_panic_debounce();
    test_status_fields();

    if (g_failures == 0) {
        printf("control mode ok: %d checks\n", g_checks);
        return 0;
    }
    fprintf(stderr, "control mode FAILED: %d/%d checks\n", g_failures, g_checks);
    return 1;
}
