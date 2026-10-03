/*
 * The meter's device half (§7.5, §12.2 validation 3; issue #35).
 *
 * The one instance, the ESP-IDF logging, and the no-ops a build without the
 * CONTROL layer links instead. It is a separate translation unit from the
 * portable core for the reason every other portable module here is: the host
 * suites compile `control_meter.c` with `-Wall -Wextra -Werror` and no ESP-IDF
 * on the include path, and a file cannot be both.
 *
 * `hid_controller.c` is upstream-owned base firmware (ADR-0005) compiled in every
 * configuration, and it calls `macro_meter_notify` unconditionally — which is
 * what the stubs below are for. Nothing else in the base build knows the meter
 * exists.
 */

#include "protocol/control/control_meter.h"

#include <string.h>

#if defined(CONFIG_PROTOCOL_LAYER_CONTROL)

#include "esp_log.h"
#include "esp_timer.h"

static control_meter_t s_meter;

/* The one log line per run, after the run — never per report (§7.5). The `control`
 * tag is the one INFO the deployment build does not silence. */
static const char *METER_TAG = "control";

void macro_meter_arm(uint32_t us)
{
    control_meter_arm(&s_meter, us);
}

void macro_meter_disarm(void)
{
    control_meter_disarm(&s_meter);
}

void macro_meter_apply(const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES])
{
    (void)control_meter_apply(&s_meter, state);
}

uint32_t macro_meter_gen(void)
{
    return s_meter.gen;
}

void macro_meter_notify(const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES], uint32_t us)
{
    /* The neutral is recognised here, where §4.6's template is already in scope,
     * rather than by comparing bytes to a record the core cannot see. */
    bool neutral = memcmp(state, control_executor_neutral, CONTROL_EXECUTOR_STATE_BYTES) == 0;
    control_meter_note(&s_meter, us, s_meter.gen, state, neutral);
}

void macro_meter_dropped(bool msys, uint32_t us)
{
    (void)us;
    control_meter_note_dropped(&s_meter, msys);
}

void macro_meter_handoff(uint32_t wait_us)
{
    control_meter_handoff(&s_meter, wait_us);
}

void macro_meter_restart(uint32_t us)
{
    control_meter_restart(&s_meter, us);
}

void macro_meter_report(uint32_t us)
{
    if (!s_meter.armed) {
        return; /* the mode left without a run: an AMIIBO exit, a stop in IDLE */
    }
    /* Disarm before reading, so the report task stops appending to a ring this
     * function is walking. The neutral it notifies next is the next run's. */
    control_meter_disarm(&s_meter);

    uint32_t elapsed_us = us - s_meter.arm_us;
    ESP_LOGI(METER_TAG,
             "macro meter: applied=%u changes=%u inputs=%u notified=%u dropped=%u "
             "failed=%u ring=%u/%u%s elapsed=%uus",
             (unsigned)s_meter.applied, (unsigned)s_meter.changes, (unsigned)s_meter.inputs,
             (unsigned)s_meter.notified, (unsigned)s_meter.dropped,
             (unsigned)s_meter.failed, (unsigned)s_meter.held,
             (unsigned)CONTROL_METER_CAPACITY, s_meter.wrapped ? " (wrapped)" : "",
             (unsigned)elapsed_us);

    /* Exact extremes over the whole run. No percentiles: the ring is a sample of
     * 64, and a bucket bound reported as a median would be a wrong number that
     * looks like a right one (this instrument's first cut did that). The bench
     * computes whatever distribution it wants from the per-input lines below. */
    if (s_meter.delta.count > 0u) {
        ESP_LOGI(METER_TAG, "macro meter: delta n=%u min=%uus max=%uus",
                 (unsigned)s_meter.delta.count, (unsigned)s_meter.delta.min_us,
                 (unsigned)s_meter.delta.max_us);
    }

    /* The handoff and the loop period are noisy per sample and stable in the
     * mean, so the mean is the number and min/max say whether it is stable. */
    if (s_meter.handoff.count > 0u) {
        ESP_LOGI(METER_TAG, "macro meter: handoff n=%u min=%uus mean=%uus max=%uus",
                 (unsigned)s_meter.handoff.count, (unsigned)s_meter.handoff.min_us,
                 (unsigned)(s_meter.handoff.sum_us / s_meter.handoff.count),
                 (unsigned)s_meter.handoff.max_us);
    }
    if (s_meter.loop.count > 0u) {
        ESP_LOGI(METER_TAG, "macro meter: loop n=%u min=%uus mean=%uus max=%uus",
                 (unsigned)s_meter.loop.count, (unsigned)s_meter.loop.min_us,
                 (unsigned)(s_meter.loop.sum_us / s_meter.loop.count),
                 (unsigned)s_meter.loop.max_us);
    }

    for (size_t i = 0; i < s_meter.held; i++) {
        control_meter_input_t in;
        if (!control_meter_input_at(&s_meter, i, &in)) {
            break;
        }
        /* No absolute time in the line: it is the running sum of the deltas, and
         * the deltas are what the ticket is about. Short lines are the point —
         * this block is the only place the instrument touches the link. */
        ESP_LOGI(METER_TAG,
                 "macro meter: #%u d=%uus g=%u%s s=%02x%02x%02x%02x%02x%02x%02x%02x%02x",
                 (unsigned)i, (unsigned)in.delta_us, (unsigned)in.gen,
                 in.neutral ? " n=1" : "", in.state[0], in.state[1], in.state[2], in.state[3],
                 in.state[4], in.state[5], in.state[6], in.state[7], in.state[8]);
    }
}

#else /* the base firmware: the meter is not built, and nothing calls it */

void macro_meter_arm(uint32_t us)
{
    (void)us;
}

void macro_meter_disarm(void)
{
}

void macro_meter_apply(const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES])
{
    (void)state;
}

uint32_t macro_meter_gen(void)
{
    return 0;
}

void macro_meter_notify(const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES], uint32_t us)
{
    (void)state;
    (void)us;
}

void macro_meter_dropped(bool msys, uint32_t us)
{
    (void)msys;
    (void)us;
}

void macro_meter_handoff(uint32_t wait_us)
{
    (void)wait_us;
}

void macro_meter_restart(uint32_t us)
{
    (void)us;
}

void macro_meter_report(uint32_t us)
{
    (void)us;
}

#endif /* CONFIG_PROTOCOL_LAYER_CONTROL */
