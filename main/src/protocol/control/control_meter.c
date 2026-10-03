/*
 * The report-cadence meter (§7.5, §12.2 validation 3, G-4/G-16; issue #35).
 *
 * Portable C, no ESP-IDF: the arithmetic the bench reads is asserted on the host
 * (`test/host/test_control_meter.c`) rather than trusted from a serial capture —
 * the ring arithmetic of #34's instrument is what a review caught, and it is
 * cheaper to pin here than to distrust later.
 *
 * See `control_meter.h` for what is measured and why the interval between
 * *distinct* notifications is the number that answers the ticket.
 *
 * **No percentiles live here on purpose.** A 1 ms histogram cannot resolve a
 * 26-second loop's holds, and a bucket bound reported as a median is a wrong
 * number wearing a right one's clothes — the first cut of this instrument did
 * exactly that (100.11 ms read as a "p50"). The meter keeps the run's *exact*
 * extremes and the newest inputs, with their microsecond intervals; the bench
 * computes any distribution it wants from those, and says that it is the newest
 * 64 doing the talking.
 */

#include "protocol/control/control_meter.h"

#include <string.h>

/* How many `inputs` the ring holds is `CONTROL_METER_CAPACITY`; the counters and
 * the extremes cover the whole run either way. */

static void meter_reset(control_meter_t *m)
{
    memset(m, 0, sizeof(*m));
}

void control_meter_arm(control_meter_t *m, uint32_t us)
{
    if (m == NULL) {
        return;
    }
    meter_reset(m);
    m->armed = true;
    m->arm_us = us;
}

void control_meter_disarm(control_meter_t *m)
{
    if (m == NULL) {
        return;
    }
    /* Only the flag: the run's numbers stay readable after the mode has left,
     * which is exactly when the readout happens. */
    m->armed = false;
}

uint32_t control_meter_apply(control_meter_t *m, const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES])
{
    if (m == NULL || !m->armed || state == NULL) {
        return 0;
    }
    m->gen++;
    m->applied = m->gen;
    /* The first write counts: the console has no prior state, so it is an input
     * like any other. Symmetric with `inputs`, which counts its first too. */
    if (!m->has_applied ||
        memcmp(m->applied_last, state, CONTROL_EXECUTOR_STATE_BYTES) != 0) {
        m->changes++;
    }
    memcpy(m->applied_last, state, CONTROL_EXECUTOR_STATE_BYTES);
    m->has_applied = true;
    return m->gen;
}

/* One min/mean/max accumulator, because the handoff and the loop period are the
 * same shape and were written twice before the review said so. */
static void meter_stat_add(control_meter_stat_t *stat, uint32_t value)
{
    if (stat->count == 0u || value < stat->min_us) {
        stat->min_us = value;
    }
    if (value > stat->max_us) {
        stat->max_us = value;
    }
    stat->count++;
    stat->sum_us += value;
}

void control_meter_note(control_meter_t *m, uint32_t us, uint32_t gen,
                        const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES], bool neutral)
{
    if (m == NULL || !m->armed || state == NULL) {
        return;
    }
    /* A successful notification either repeats the state the console already
     * has — the report period simply elapsed — or carries a new one. Only the
     * second is an input the console has to react to. */
    m->notified++;
    if (m->has_last && memcmp(m->last, state, CONTROL_EXECUTOR_STATE_BYTES) == 0) {
        return;
    }

    control_meter_input_t *slot = &m->ring[m->head];
    slot->us = us;
    slot->gen = gen;
    slot->neutral = neutral;
    memcpy(slot->state, state, CONTROL_EXECUTOR_STATE_BYTES);
    slot->delta_us = m->has_last ? (us - m->last_us) : 0u;

    m->head = (uint16_t)((m->head + 1u) % CONTROL_METER_CAPACITY);
    if (m->held == CONTROL_METER_CAPACITY) {
        m->wrapped = true;
    } else {
        m->held++;
    }
    m->inputs++;

    if (m->has_last) {
        meter_stat_add(&m->delta, slot->delta_us);
    }

    memcpy(m->last, state, CONTROL_EXECUTOR_STATE_BYTES);
    m->last_us = us;
    m->has_last = true;
}

void control_meter_note_dropped(control_meter_t *m, bool msys)
{
    if (m == NULL || !m->armed) {
        return;
    }
    if (msys) {
        m->dropped++;
    } else {
        m->failed++;
    }
}

void control_meter_handoff(control_meter_t *m, uint32_t wait_us)
{
    if (m == NULL || !m->armed) {
        return;
    }
    meter_stat_add(&m->handoff, wait_us);
}

void control_meter_restart(control_meter_t *m, uint32_t us)
{
    if (m == NULL || !m->armed) {
        return;
    }
    if (m->has_restart) {
        meter_stat_add(&m->loop, us - m->last_restart_us);
    }
    m->last_restart_us = us;
    m->has_restart = true;
}

bool control_meter_input_at(const control_meter_t *m, size_t i, control_meter_input_t *out)
{
    if (m == NULL || out == NULL || i >= m->held) {
        return false;
    }
    size_t first = (size_t)(m->head + CONTROL_METER_CAPACITY - m->held) % CONTROL_METER_CAPACITY;
    *out = m->ring[(first + i) % CONTROL_METER_CAPACITY];
    return true;
}
