/*
 * Host-side assertions for the report-cadence meter (§7.5, §12.2 validation 3,
 * G-4/G-16; issue #35).
 *
 * The acceptance property is what the bench instrument rests on: the interval
 * the meter reports is the interval between *distinct* notifications, a repeat
 * of the same nine bytes is not an input, a dropped report is counted rather
 * than hidden, the ring keeps the newest inputs when it wraps, and the
 * percentile comes from the millisecond buckets it says it does. Those are the
 * numbers `docs/research/macro-timing-bench.md` reads off the wire, so they are
 * pinned here rather than trusted to the bench's arithmetic.
 *
 * Build:
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include -Itest/host \
 *      -o test_control_meter \
 *      test/host/test_control_meter.c \
 *      main/src/protocol/control/control_meter.c
 * Run:
 *   ./test_control_meter
 */

#include "control_test_util.h"

#include "protocol/control/control_meter.h"

#include <stdlib.h>

/* The neutral, and three distinct inputs, in the report's own layout (§4.6). */
static const uint8_t NEUTRAL[9] = { 0x00, 0x00, 0x00, 0x00, 0x08, 0x80, 0x00, 0x08, 0x80 };
static const uint8_t BUTTON_A[9] = { 0x02, 0x00, 0x00, 0x00, 0x08, 0x80, 0x00, 0x08, 0x80 };
static const uint8_t BUTTON_B[9] = { 0x01, 0x00, 0x00, 0x00, 0x08, 0x80, 0x00, 0x08, 0x80 };
static const uint8_t STICK_UP[9] = { 0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x08, 0x80 };

static void test_arm_resets_every_run(void)
{
    control_meter_t m;
    control_meter_arm(&m, 1000u);
    CHECK(m.armed, "arming sets the flag");
    CHECK(m.arm_us == 1000u, "arm_us=%u", (unsigned)m.arm_us);

    (void)control_meter_apply(&m, BUTTON_A);
    control_meter_note(&m, 2000u, 1u, BUTTON_A, false);
    control_meter_note_dropped(&m, true);
    CHECK(m.inputs == 1u && m.applied == 1u && m.dropped == 1u, "one of each recorded");

    control_meter_arm(&m, 9000u);
    CHECK(m.gen == 0u && m.applied == 0u && m.inputs == 0u && m.notified == 0u,
          "a new arm clears the previous run's counts");
    CHECK(m.dropped == 0u && m.failed == 0u && m.held == 0u && !m.wrapped && !m.has_last,
          "and its counters and ring");
    CHECK(m.arm_us == 9000u, "and rebases the clock");
}

static void test_repeats_are_not_inputs(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);

    /* Five report periods carrying one unchanged state. The console has one
     * input, and the first entry has no predecessor to be late against. */
    for (uint32_t i = 0; i < 5u; i++) {
        control_meter_note(&m, 1000u * i, 1u, BUTTON_A, false);
    }
    CHECK(m.notified == 5u, "all five notifications counted: %u", (unsigned)m.notified);
    CHECK(m.inputs == 1u, "one distinct input: %u", (unsigned)m.inputs);
    CHECK(m.held == 1u, "one ring entry: %u", (unsigned)m.held);

    control_meter_input_t in;
    CHECK(control_meter_input_at(&m, 0u, &in), "the entry is readable");
    CHECK(in.delta_us == 0u, "the first input has no delta: %u", (unsigned)in.delta_us);
    CHECK(memcmp(in.state, BUTTON_A, 9) == 0, "and carries the state");
    CHECK(in.gen == 1u, "and the generation label");
}

static void test_delta_is_first_notification_to_first_notification(void)
{
    control_meter_t m;
    control_meter_arm(&m, 500u);

    /* A 25 ms intended hold, notified on a 10 ms grid: the old state is repeated
     * twice, and the input change is seen 30 ms after the previous one. */
    control_meter_note(&m, 500u, 1u, NEUTRAL, true);
    control_meter_note(&m, 10500u, 1u, NEUTRAL, true);
    control_meter_note(&m, 20500u, 1u, NEUTRAL, true);
    control_meter_note(&m, 30500u, 2u, BUTTON_A, false);
    control_meter_note(&m, 40500u, 2u, BUTTON_A, false);
    control_meter_note(&m, 50500u, 3u, NEUTRAL, true);

    CHECK(m.inputs == 3u, "three distinct inputs: %u", (unsigned)m.inputs);
    control_meter_input_t in;
    CHECK(control_meter_input_at(&m, 1u, &in), "second input readable");
    CHECK(in.delta_us == 30000u, "delta is 30 ms, not 10: %u", (unsigned)in.delta_us);
    CHECK(in.gen == 2u, "labelled with the apply that changed it: %u", (unsigned)in.gen);
    CHECK(control_meter_input_at(&m, 2u, &in), "third input readable");
    CHECK(in.delta_us == 20000u, "and the next gap is 20 ms: %u", (unsigned)in.delta_us);
    CHECK(m.delta.min_us == 20000u && m.delta.max_us == 30000u,
          "min=%u max=%u", (unsigned)m.delta.min_us, (unsigned)m.delta.max_us);
}

static void test_dropped_and_failed_reports_are_counted(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    control_meter_note(&m, 1000u, 1u, BUTTON_A, false);
    control_meter_note_dropped(&m, true);
    control_meter_note_dropped(&m, true);
    control_meter_note_dropped(&m, false);
    CHECK(m.dropped == 2u, "msys drops counted separately: %u", (unsigned)m.dropped);
    CHECK(m.failed == 1u, "other refusals counted: %u", (unsigned)m.failed);
    CHECK(m.inputs == 1u, "a drop is not an input change: %u", (unsigned)m.inputs);
    /* The gap a drop leaves is a longer delta, not a missing entry — which is
     * why the delta is what the bench reads and the counter is the context. */
    control_meter_note(&m, 41000u, 2u, BUTTON_B, false);
    control_meter_input_t in;
    CHECK(control_meter_input_at(&m, 1u, &in) && in.delta_us == 40000u,
          "the next input carries the wider gap");
}

static void test_ring_keeps_the_newest_and_counts_the_rest(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    uint8_t state[9];
    memcpy(state, NEUTRAL, 9);

    const uint32_t total = CONTROL_METER_CAPACITY + 10u;
    for (uint32_t i = 0; i < total; i++) {
        /* A distinct state each time: one bit of the counter in the second
         * button byte, so every notification is a new input. */
        state[1] = (uint8_t)(i & 0xFFu);
        state[2] = (uint8_t)((i >> 8) & 0x0Fu);
        control_meter_note(&m, 1000u * (i + 1u), i + 1u, state, false);
    }

    CHECK(m.inputs == total, "every input counted: %u", (unsigned)m.inputs);
    CHECK(m.held == CONTROL_METER_CAPACITY, "the ring holds its capacity: %u", (unsigned)m.held);
    CHECK(m.wrapped, "and says it wrapped");

    control_meter_input_t in;
    CHECK(control_meter_input_at(&m, 0u, &in), "the oldest held entry is readable");
    CHECK(in.gen == 11u, "the oldest ten were overwritten: gen=%u", (unsigned)in.gen);
    CHECK(control_meter_input_at(&m, CONTROL_METER_CAPACITY - 1u, &in), "the newest is readable");
    CHECK(in.gen == total, "and is the last one recorded: gen=%u", (unsigned)in.gen);
    CHECK(!control_meter_input_at(&m, CONTROL_METER_CAPACITY, &in), "one past the end is refused");

    /* Order survives the wrap: the walk is oldest-first, so each entry's clock
     * is later than the one before it. */
    uint32_t prev = 0;
    bool ordered = true;
    for (size_t i = 0; i < m.held; i++) {
        CHECK(control_meter_input_at(&m, i, &in), "entry %u readable", (unsigned)i);
        if (in.us <= prev) {
            ordered = false;
            break;
        }
        prev = in.us;
    }
    CHECK(ordered, "the ring reads oldest-first across the wrap");
}

static void test_the_extremes_cover_the_whole_run(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    uint8_t state[9];
    memcpy(state, NEUTRAL, 9);

    /* Eleven deltas, nine of 7 ms and one of 30 ms, plus one of 3 s. The ring
     * holds only the newest 64 either way; the extremes are exact. */
    uint32_t t = 0;
    state[1] = 0u;
    control_meter_note(&m, t, 1u, state, false);
    for (uint32_t i = 0; i < 9u; i++) {
        t += 7000u;
        state[1] = (uint8_t)(i + 1u);
        control_meter_note(&m, t, i + 2u, state, false);
    }
    t += 30000u;
    state[1] = 0x50u;
    control_meter_note(&m, t, 20u, state, false);
    t += 3000000u;
    state[1] = 0x51u;
    control_meter_note(&m, t, 21u, state, false);

    CHECK(m.inputs == 12u, "twelve inputs: %u", (unsigned)m.inputs);
    CHECK(m.delta.count == 11u, "eleven intervals: %u", (unsigned)m.delta.count);
    CHECK(m.delta.min_us == 7000u, "the smallest is exact: %u", (unsigned)m.delta.min_us);
    CHECK(m.delta.max_us == 3000000u,
          "a three-second gap is not clamped to a histogram bucket: %u",
          (unsigned)m.delta.max_us);
    /* 9 x 7 ms + 30 ms + 3 s over 11 intervals. */
    CHECK(m.delta.sum_us / m.delta.count == 281181u,
          "mean=%u", (unsigned)(m.delta.sum_us / m.delta.count));
}

static void test_disarm_stops_recording(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    control_meter_note(&m, 1000u, 1u, NEUTRAL, true);
    control_meter_disarm(&m);
    CHECK(!m.armed, "disarmed");

    /* The report task keeps notifying the neutral after the mode leaves; none of
     * it may land in a run the readout has already printed. */
    control_meter_note(&m, 2000u, 2u, BUTTON_A, false);
    control_meter_note_dropped(&m, true);
    (void)control_meter_apply(&m, BUTTON_A);
    CHECK(m.inputs == 1u && m.notified == 1u && m.dropped == 0u && m.applied == 0u,
          "nothing after the disarm is recorded");
}

static void test_unarmed_meter_records_nothing(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    control_meter_disarm(&m);
    control_meter_note(&m, 1000u, 1u, BUTTON_A, false);
    CHECK(m.inputs == 0u && m.notified == 0u && m.held == 0u, "an unarmed meter is inert");
    CHECK(m.delta.count == 0u, "and has no intervals");
}

static void test_delivery_is_inputs_against_applied_changes(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    /* The grid-5ms shape: the executor walks forty alternating states while the
     * report period notifies about one of them. `changes` is the denominator the
     * bench divides by, so it must count state changes rather than writes. */
    for (unsigned i = 0; i < 40u; i++) {
        (void)control_meter_apply(&m, (i % 2u) ? NEUTRAL : BUTTON_A);
    }
    CHECK(m.applied == 40u, "every apply is counted: %u", (unsigned)m.applied);
    CHECK(m.changes == 40u, "and every one of them changed the state: %u",
          (unsigned)m.changes);
    control_meter_note(&m, 1000u, 40u, BUTTON_A, false);
    CHECK(m.inputs == 1u, "one input reached the console: %u", (unsigned)m.inputs);

    /* A repeated write is not a change: the loop boundary's neutral, when it is
     * byte-identical to the record that preceded it, must not inflate the
     * denominator — that would read as lost input. */
    control_meter_arm(&m, 0u);
    (void)control_meter_apply(&m, NEUTRAL);
    (void)control_meter_apply(&m, NEUTRAL);
    (void)control_meter_apply(&m, BUTTON_A);
    CHECK(m.applied == 3u && m.changes == 2u,
          "three writes, two changes (applied=%u changes=%u)",
          (unsigned)m.applied, (unsigned)m.changes);
}

static void test_loop_periods_are_measured_between_restarts(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    /* The arm's restart only opens the clock; each later one is one period. */
    control_meter_restart(&m, 1000u);
    control_meter_restart(&m, 340000u);
    control_meter_restart(&m, 700000u);
    CHECK(m.loop.count == 2u, "two periods from three restarts: %u", (unsigned)m.loop.count);
    CHECK(m.loop.min_us == 339000u && m.loop.max_us == 360000u,
          "min=%u max=%u", (unsigned)m.loop.min_us, (unsigned)m.loop.max_us);
    CHECK(m.loop.sum_us / m.loop.count == 349500u,
          "mean=%u", (unsigned)(m.loop.sum_us / m.loop.count));

    /* A restart after the disarm belongs to the next run. */
    control_meter_disarm(&m);
    control_meter_restart(&m, 900000u);
    CHECK(m.loop.count == 2u, "a disarmed meter records nothing");
}

static void test_the_handoff_wait_shares_the_interval_accumulator(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    control_meter_handoff(&m, 9900u);
    control_meter_handoff(&m, 10050u);
    control_meter_handoff(&m, 9950u);
    CHECK(m.handoff.count == 3u, "three waits: %u", (unsigned)m.handoff.count);
    CHECK(m.handoff.min_us == 9900u && m.handoff.max_us == 10050u,
          "min=%u max=%u", (unsigned)m.handoff.min_us, (unsigned)m.handoff.max_us);
    CHECK(m.handoff.sum_us / m.handoff.count == 9966u,
          "mean=%u", (unsigned)(m.handoff.sum_us / m.handoff.count));
    control_meter_disarm(&m);
    control_meter_handoff(&m, 50000u);
    CHECK(m.handoff.count == 3u, "a disarmed meter records nothing");
}

static void test_the_neutral_is_flagged_on_the_input(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    control_meter_note(&m, 0u, 1u, BUTTON_A, false);
    control_meter_note(&m, 10000u, 2u, NEUTRAL, true);
    control_meter_input_t in;
    CHECK(control_meter_input_at(&m, 0u, &in) && !in.neutral, "a record is not the neutral");
    CHECK(control_meter_input_at(&m, 1u, &in) && in.neutral,
          "the loop boundary is flagged, so the bench can name the handoff interval");
}

static void test_distinct_states_are_compared_byte_for_byte(void)
{
    control_meter_t m;
    control_meter_arm(&m, 0u);
    control_meter_note(&m, 100u, 1u, NEUTRAL, true);
    control_meter_note(&m, 200u, 2u, STICK_UP, false);
    control_meter_note(&m, 300u, 3u, BUTTON_A, false);
    control_meter_note(&m, 400u, 4u, BUTTON_B, false);
    CHECK(m.inputs == 4u, "a changed stick or button byte is a new input: %u",
          (unsigned)m.inputs);
    control_meter_input_t in;
    CHECK(control_meter_input_at(&m, 2u, &in) && memcmp(in.state, BUTTON_A, 9) == 0,
          "the byte-for-byte state is kept");
}

int main(void)
{
    test_arm_resets_every_run();
    test_repeats_are_not_inputs();
    test_delta_is_first_notification_to_first_notification();
    test_dropped_and_failed_reports_are_counted();
    test_ring_keeps_the_newest_and_counts_the_rest();
    test_the_extremes_cover_the_whole_run();
    test_disarm_stops_recording();
    test_unarmed_meter_records_nothing();
    test_delivery_is_inputs_against_applied_changes();
    test_loop_periods_are_measured_between_restarts();
    test_the_handoff_wait_shares_the_interval_accumulator();
    test_the_neutral_is_flagged_on_the_input();
    test_distinct_states_are_compared_byte_for_byte();

    if (g_failures != 0) {
        fprintf(stderr, "control meter FAILED: %d of %d checks\n", g_failures, g_checks);
        return 1;
    }
    printf("control meter ok: %d checks\n", g_checks);
    return 0;
}
