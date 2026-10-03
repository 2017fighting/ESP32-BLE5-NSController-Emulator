#ifndef PROTOCOL_CONTROL_METER_H
#define PROTOCOL_CONTROL_METER_H

/*
 * The report-cadence meter (§7.5, §12.2 validation 3, gaps G-4 and G-16;
 * issue #35).
 *
 * §12.2 asks whether the plan executor holds timing **against the console**, and
 * ADR-0003 is vindicated or shown to need a report-period change by that answer.
 * The executor alone cannot answer it: it knows when a record was *applied*, not
 * when a report carrying that record was *notified*. Only the report task knows
 * the second, so this instrument sits across that seam:
 *
 *   * **the core** (`control_meter.c`, `control_meter_t`) is portable C: a small
 *     RAM ring of the newest distinct input states with the microsecond interval
 *     each arrived after, the exact extremes over the whole run, and the counters
 *     for reports the radio refused. The host suite drives it directly
 *     (`test/host/test_control_meter.c`).
 *   * **the device half** (`control_meter_device.c`, `macro_meter_*`) is the
 *     firmware's single instance behind `CONFIG_PROTOCOL_LAYER_CONTROL`, with
 *     no-ops in its place otherwise — so `hid_controller.c` may call it
 *     unconditionally, and a build without the control plane carries neither the
 *     meter nor a conditional about it.
 *
 * **Why the number is the interval between distinct states.** The report task
 * notifies the front buffer every report period whether or not its content
 * changed, so the console sees each input held for a whole number of periods.
 * Input-to-input latency — the thing a macro's `hold_ms` claims — is therefore
 * the interval between the *first* notification of one distinct state and the
 * first notification of the next. The meter records exactly those, and nothing
 * per report.
 *
 * **`changes` is the other half of the measurement.** The share of the executor's
 * input changes that the console was actually notified is the only collapse
 * metric that works: matching notified bytes back to plan records cannot, because
 * a plan may revisit a state (every synthetic case on the bench does, and so does
 * the real 纠错宏 — 47 distinct states across 71 records).
 *
 * **Nothing is logged per report** (§7.5). A DEBUG build's report rate is set by
 * the UART log budget rather than by `CONFIG_HID_REPORT_INTERVAL`, so a log line
 * per notification would *be* the measurement's floor instead of its subject.
 * The meter accumulates in RAM and the exit prints it once — the discipline the
 * §2.7 ring instrument of #34 established (`staging closed: …`).
 *
 * The clock is `esp_timer_get_time()`, microseconds, truncated to 32 bits: a
 * 71-minute wrap is irrelevant to a run and its deltas survive it.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The nine state bytes a plan record carries (§5.3) — the same nine the
 * executor writes and the report carries at 0x02. One definition, reused. */
#include "protocol/control/control_executor.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The ring holds the newest 64 distinct inputs, and the exit prints all of them.
 * 64 is a *bound on the burst after the run*, not a coverage choice: the log is
 * synchronous UART at 115200, so 64 lines cost ~0.4 s of the link, where 512
 * would cost ~3 s and could outlast the container's 2 s request timeout. The
 * numbers that must be complete are not in the ring — the counters and the exact
 * extremes cover every input of the run, and the ring is the newest window of
 * evidence behind them. The bench says so when it reports a distribution from it. */
#define CONTROL_METER_CAPACITY 64u

typedef struct {
    uint32_t us;       /* the clock at the notification that first carried it */
    uint32_t delta_us; /* since the previous distinct input; 0 for the first */
    uint32_t gen;      /* the apply generation the reporter labelled it with */
    bool neutral;      /* §4.6's template: the loop boundary, not a plan record */
    uint8_t state[CONTROL_EXECUTOR_STATE_BYTES];
} control_meter_input_t;

/* A run's min/mean/max over one shape of interval: §4.6's handoff wait and §5.4's
 * loop period are the same accumulator, so they share one. */
typedef struct {
    uint32_t count;
    uint32_t min_us;
    uint32_t max_us;
    uint32_t sum_us;
} control_meter_stat_t;

typedef struct {
    volatile bool armed; /* false ignores every note; the two tasks race on it, benignly */
    uint32_t arm_us;

    /* Written by the executor task (an apply), read by the report task (the
     * label a notification carries). An aligned 32-bit load is atomic on this
     * core, and the state bytes — not this label — are what identifies an input. */
    volatile uint32_t gen;
    uint32_t applied; /* == gen, kept for the readout where `gen` is volatile */

    /* The executor side's ground truth: how many *distinct* states it wrote. The
     * difference against `inputs` is the console-visible input loss. */
    uint32_t changes;
    bool has_applied;
    uint8_t applied_last[CONTROL_EXECUTOR_STATE_BYTES];

    /* Written by the report task. */
    uint32_t inputs;   /* distinct states seen, including ones the ring dropped */
    uint32_t notified; /* successful notifications, repeats of one state included */
    uint32_t dropped;  /* gatt_notify could not carry it: the msys pool was low */
    uint32_t failed;   /* gatt_notify refused it for any other reason */

    uint16_t head; /* the ring's write cursor */
    uint16_t held; /* entries in the ring, ≤ CONTROL_METER_CAPACITY */
    bool wrapped;  /* the oldest entries were overwritten */
    bool has_last;
    uint32_t last_us;
    uint8_t last[CONTROL_EXECUTOR_STATE_BYTES];

    /* The exact extremes over the whole run; the ring is the sample. */
    control_meter_stat_t delta;

    /* §4.6's handoff: how long the neutral held the walk before the reporter took
     * it. #24 could bound it at 50 ms by construction; this is the number with a
     * console subscribed, where the bound is not the expected wait, and the zero
     * with nobody subscribed. */
    control_meter_stat_t handoff;

    /* §5.4's loop period as the report carries it: the interval between two
     * record-0 applies. It need not be `loop_ms` — the boundary's neutral costs a
     * report period before record 0 can replace it. */
    control_meter_stat_t loop;
    bool has_restart;
    uint32_t last_restart_us;

    control_meter_input_t ring[CONTROL_METER_CAPACITY];
} control_meter_t;

/* `START`: clear the run and begin counting. */
void control_meter_arm(control_meter_t *m, uint32_t us);

/* The mode exit, after the readout: further notes are ignored until the next
 * arm. Called before printing, so the report task cannot append to a ring the
 * readout is walking. */
void control_meter_disarm(control_meter_t *m);

/* One record applied to the report's back buffer. Returns the new generation.
 * @p state is what was written, so the meter can count the state *changes* the
 * executor produced — the denominator of the delivery ratio. */
uint32_t control_meter_apply(control_meter_t *m, const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES]);

/* One report successfully notified. A state identical to the last one is a
 * repeat: counted, not recorded. `gen` labels the apply the reporter believes it
 * carried, and @p neutral marks §4.6's template — which is how a loop boundary is
 * told apart from a plan record without guessing from its bytes. */
void control_meter_note(control_meter_t *m, uint32_t us, uint32_t gen,
                        const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES], bool neutral);

/* A report that did not leave the radio: `msys` true for `BLE_HS_EBUSY` (the
 * msys pool was low, `gatt_notify` skipped it), false for any other refusal.
 * These are console-visible input losses, so they are counted rather than
 * deduced from the deltas. */
void control_meter_note_dropped(control_meter_t *m, bool msys);

/* One completed handoff: the neutral was committed and the reporter took
 * @p wait_us later. A distribution rather than a single sample, because the
 * interesting fact is its shape — a whole report period, once per loop boundary. */
void control_meter_handoff(control_meter_t *m, uint32_t wait_us);

/* A loop's record 0 has just been applied (@p us). The first one only starts the
 * clock; each later one is one loop period, measured where the report carries it. */
void control_meter_restart(control_meter_t *m, uint32_t us);

/* Reads one ring entry, oldest first (`i` = 0 is the oldest). False when `i` is
 * past the end. An accessor rather than a copy-out, because the readout walks the
 * ring and a second copy of it is a real cost for nothing. */
bool control_meter_input_at(const control_meter_t *m, size_t i, control_meter_input_t *out);

/*
 * ---------------------------------------------------------------- the wiring
 *
 * The firmware's single instance, driven from the executor task
 * (`control_parser.c`) and the report task (`hid_controller.c`). Every one of
 * these is a no-op in a build without `CONFIG_PROTOCOL_LAYER_CONTROL`, which is
 * what lets the report task call the meter unconditionally.
 */

/* `START`'s physical half succeeded: arm the meter. */
void macro_meter_arm(uint32_t us);

/* A `START` that was refused, or any path that abandons the run before it can be
 * read: discard what was armed. */
void macro_meter_disarm(void);

/* One record applied (the neutral included — the console sees it too). */
void macro_meter_apply(const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES]);

/* The generation a notification is about to carry. */
uint32_t macro_meter_gen(void);

/* A report left the radio, carrying @p state. */
void macro_meter_notify(const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES], uint32_t us);

/* A report did not leave the radio (`msys` as `control_meter_note_dropped`). */
void macro_meter_dropped(bool msys, uint32_t us);

/* A handoff the executor measured (`control_meter_handoff`). */
void macro_meter_handoff(uint32_t wait_us);

/* A loop restarted: record 0 was just applied (`control_meter_restart`). */
void macro_meter_restart(uint32_t us);

/* The mode exit: print the run at INFO, then disarm. Prints nothing when the
 * mode left without a run (an `AMIIBO` exit, for instance). */
void macro_meter_report(uint32_t us);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_METER_H */
