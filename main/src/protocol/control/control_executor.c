/*
 * The plan executor and the neutral template (§7.3 step 5, §4.6, §5.4, §3.3,
 * §2.9; issue #24). Portable C; the host suite links this file directly.
 *
 * See `control_executor.h` for the two properties this file exists to keep —
 * the compiled-in neutral on every exit path, and the `LOOP_COMPLETED` rate
 * limit — and for why the handoff state exists at all.
 *
 * Three things are deliberate and easy to get wrong:
 *
 *  - **The loop clock is exact milliseconds, and a boundary advances it by
 *    `loop_ms`, not by the time the tick noticed.** Advancing to the observed
 *    time instead would quantise every loop up to the 10 ms tick and compound:
 *    a 25 ms loop would measure 30 ms and the 20th loop would be late by 100 ms.
 *  - **A record is not barred by `commit_idle`; the neutral is.** Report density
 *    is not playback speed (ADR-0009), so an intermediate record that a slow
 *    report period skips is a report that was never owed. The neutral is
 *    different: §4.6 says it is *committed and transmitted*, so the boundary
 *    waits for the reporter to take it before the next record overwrites the
 *    buffer it is sitting in.
 *  - **At most one loop boundary per step.** A plan whose holds are zero, or
 *    whose `loop_ms` is degenerate, must not spin the task.
 */

#include "protocol/control/control_executor.h"

#include <string.h>

#include "protocol/control/control_bulk.h"
#include "protocol/control/control_events.h"
#include "protocol/control/control_mode.h"
#include "protocol/plan.h"

/*
 * §4.6's template: the fixed neutral state. It is the same neutral
 * `pro2_report_init` builds — every button byte zero (which is what makes the
 * guarantee stronger than the reference player's 18 named buttons: a zeroed
 * Pro2 field cannot miss `GR`/`GL`/`C`), and both sticks at `PRO2_STICK_CENTER`
 * (`0x800`) in `pack_stick_data` bit order. The firmware adapter asserts this
 * against the Pro2 report's own field offsets.
 */
const uint8_t control_executor_neutral[CONTROL_EXECUTOR_STATE_BYTES] = {
    0x00, 0x00, 0x00, /* buttons[3]: every Pro2 button bit clear */
    0x00, 0x08, 0x80, /* left stick[3]: pack_stick_data(0x800, 0x800) */
    0x00, 0x08, 0x80, /* right stick[3]: pack_stick_data(0x800, 0x800) */
};

/* The nine state bytes of record @p i, read in place from the committed plan —
 * `plan_slots = 1` means the committed plan *is* these bytes (§7.4). */
static const uint8_t *record_state_at(const control_executor_t *ex, uint16_t i)
{
    return ex->plan + PLAN_HEADER_SIZE + (uint32_t)i * PLAN_RECORD_SIZE;
}

static uint16_t record_hold(const control_executor_t *ex, uint16_t i)
{
    return control_rd_le16(record_state_at(ex, i) + offsetof(plan_record_t, hold_ms));
}

static bool apply_state(control_executor_t *ex, const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES])
{
    if (!ex->io_set || ex->io.apply_state == NULL) {
        return false;
    }
    return ex->io.apply_state(ex->io.ctx, state);
}

/* Best effort by construction: if there is no report to write to, then there is
 * nothing the neutral can be committed into, and the mode still ends. */
static void apply_neutral(control_executor_t *ex)
{
    (void)apply_state(ex, control_executor_neutral);
}

/* The handoff is over when the reporter has taken the commit, or when the
 * safety bound says it never will. A NULL `commit_idle` is "always ready": what
 * a test that does not model the reporter wants, and never what the firmware
 * installs. */
static bool handoff_ready(const control_executor_t *ex, uint32_t now_ms)
{
    if (!ex->io_set || ex->io.commit_idle == NULL) {
        return true;
    }
    if (ex->io.commit_idle(ex->io.ctx)) {
        return true;
    }
    return (int32_t)(now_ms - ex->deadline_ms) >= 0;
}

/* Applies record @p i and reports it. A refused write is the caller's to turn
 * into a fault — the one place `apply_state` can fail. */
static bool apply_record(control_executor_t *ex, control_state_t *st, uint16_t i)
{
    if (ex->plan == NULL || i >= ex->count) {
        return false;
    }
    if (!apply_state(ex, record_state_at(ex, i))) {
        return false;
    }
    ex->frame = i;
    ex->has_applied = true;
    if (st != NULL) {
        st->status.current_frame = i;
    }
    return true;
}

/* §4.6's fault path. The neutral is the last write of the mode however the mode
 * ends, so it is attempted here — but the fault is *logical* first: the walk
 * stops before anything is written again, so a failed write cannot cascade.
 *
 * `last_stop_reason` is deliberately left `NONE` and `last_error` carries the
 * reason (§3.4's NONE+set row). The §2.5 code set is closed and §2.5 forbids
 * inventing a code for a fault it cannot express, so there is no new wire value
 * to learn. The encoded `ERROR` reply has no addressee — nothing asked — and is
 * therefore discarded; the `ERROR_RAISED` event and the next `STATUS` poll are
 * how the container learns, which is §3.1's rule (STATUS is the truth).
 * §2.5 types every code's `detail`, and `BAD_PLAN`'s is 0 — the same 0 §2.7's
 * commit path sends. The offending frame index would be a nicer diagnostic and
 * is deliberately not carried: it would mean reopening a closed, fully typed
 * `detail` table for a case the arm's own validation makes unreachable, and the
 * index is not what the container acts on (it reloads).
 */
static void executor_fault(control_executor_t *ex, control_state_t *st, uint8_t code)
{
    ex->state = CONTROL_EX_STOPPED;
    ex->has_applied = false;
    ex->plan = NULL;
    ex->count = 0;
    ex->loop_ms = 0;
    ex->handoff = CONTROL_EX_HANDOFF_NONE;
    /* The neutral is written here rather than left to `control_mode_exit`'s
     * `fx.stop`, because this module must guarantee §4.6 on its own: on the
     * firmware both run (the exit's `stop` effect reaches `control_executor_stop`,
     * which writes the same nine bytes again), and on the host the verb layer's
     * `stop` is a recording double that does not touch the report at all. A
     * second identical commit is harmless; a missing one is the invariant broken.
     */
    apply_neutral(ex);
    if (st != NULL) {
        uint8_t scratch[CONTROL_WIRE_MAX];
        control_raise_error(st, code, 0, scratch, sizeof(scratch));
        control_mode_exit(st, CONTROL_STOP_NONE);
    }
}

void control_executor_init(control_executor_t *ex, const control_executor_io_t *io)
{
    if (ex == NULL) {
        return;
    }
    memset(ex, 0, sizeof(*ex));
    if (io != NULL) {
        ex->io = *io;
        ex->io_set = true;
    }
    ex->state = CONTROL_EX_STOPPED;
}

bool control_executor_running(const control_executor_t *ex)
{
    return ex != NULL && ex->state != CONTROL_EX_STOPPED;
}

/*
 * Resolves a pending handoff by applying the record the loop should be on.
 * `ARM` and `LOOP` both start the loop at frame 0 — `LOOP` from the *ideal*
 * boundary time, so no tick quantisation accumulates, and `ARM` from now,
 * because that is when the loop's clock starts (§4.3: "ACK once armed").
 * `RESUME` re-applies the current frame and keeps the clock and the frame,
 * which is §4.7's "continues at its current frame".
 */
static bool resolve_handoff(control_executor_t *ex, control_state_t *st, uint32_t now_ms)
{
    ex->state = CONTROL_EX_RUNNING;
    if (ex->handoff == CONTROL_EX_HANDOFF_RESUME) {
        /* §4.7's re-subscribe continues the same loop: the clock and the frame
         * survive, so this is not a restart and must not be counted as one. */
        return apply_record(ex, st, ex->frame);
    }
    ex->base_ms = (ex->handoff == CONTROL_EX_HANDOFF_LOOP) ? (ex->base_ms + ex->loop_ms) : now_ms;
    ex->frame = 0;
    /* §5.4: `sum(hold_ms) == loop_ms`, and the last record's hold runs to
     * `loop_ms` — so a one-record plan's frame 0 ends at the boundary. */
    ex->frame_end_ms = ex->base_ms +
                       ((ex->count == 1) ? ex->loop_ms : record_hold(ex, 0));
    ex->has_applied = false;
    bool applied = apply_record(ex, st, 0);
    if (applied && ex->io_set && ex->io.loop_restarted != NULL) {
        /* After the write, so the interval a bench measures is the one the
         * report carries rather than the one the executor intended. */
        ex->io.loop_restarted(ex->io.ctx, now_ms);
    }
    return applied;
}

uint8_t control_executor_arm(control_executor_t *ex, control_state_t *st, uint32_t now_ms)
{
    if (ex == NULL || st == NULL || !ex->io_set || ex->io.apply_state == NULL) {
        return CONTROL_ERR_BAD_PLAN;
    }
    if (!st->plan_committed || st->plan == NULL) {
        return CONTROL_ERR_BAD_PLAN;
    }

    /* The same structural check the commit ran, so "structurally valid" keeps
     * one definition — and a plan that somehow reached the device without one
     * is refused here rather than replayed as garbage (§5.3). */
    uint16_t records = 0;
    uint8_t code = control_plan_check(st->plan, st->plan_len, st->plan_stage_cap, &records);
    if (code != CONTROL_ERR_NONE) {
        return code;
    }
    if (records == 0) {
        /* Structurally valid (len == 12) and unrunnable. The compiler refuses to
         * emit one (§5.5), but a hand-built plan can reach the device, and a
         * refusal here is a typed ERROR instead of a mode entered and abandoned.
         */
        return CONTROL_ERR_BAD_PLAN;
    }

    /* §2.3: a rejection must not leave state touched, so nothing is written
     * until the arm can be honoured — and this is the one write that can refuse
     * (the report may not exist yet). */
    uint8_t saved_state = ex->state;
    ex->plan = st->plan;
    ex->count = records;
    ex->loop_ms = control_rd_le32(st->plan + offsetof(plan_header_t, loop_ms));
    ex->frame = 0;
    ex->has_applied = false;
    ex->handoff = CONTROL_EX_HANDOFF_ARM;
    ex->deadline_ms = now_ms + CONTROL_EXECUTOR_HANDOFF_MAX_MS;
    ex->state = CONTROL_EX_HANDOFF;

    /* §4.3 "neutral first": it is committed before any record. It is also the
     * last write of the mode being re-entered, so an arm after a stop re-writes
     * the same bytes — harmless, and cheaper than a second state to track. */
    if (!apply_state(ex, control_executor_neutral)) {
        ex->state = saved_state;
        ex->plan = NULL;
        ex->count = 0;
        ex->loop_ms = 0;
        ex->handoff = CONTROL_EX_HANDOFF_NONE;
        return CONTROL_ERR_BAD_PLAN;
    }
    return CONTROL_ERR_NONE;
}

void control_executor_stop(control_executor_t *ex)
{
    if (ex == NULL) {
        return;
    }
    /* Halting the walk is idempotent; **committing the neutral is not
     * conditional on having been running.** §4.6 makes the neutral the last
     * write of *the mode*, not of the walk — and the mode can end while this
     * executor was never armed at all, which is exactly the `AMIIBO` case:
     * §4.3's `AMIIBO` + `STOP` row requires a neutral release, and the executor
     * is `STOPPED` throughout `AMIIBO` (nothing arms it). An early return here
     * would leave that row unmet.
     *
     * Calling it repeatedly is safe rather than silent: it rewrites the same
     * nine bytes, so the verb layer's `stop` effect, the BOOT stop and a fault
     * cannot disagree about what the last write was. That is a stronger and more
     * useful property than "does nothing when already stopped", which is what it
     * used to mean and which is what let the `AMIIBO` gap through.
     */
    ex->state = CONTROL_EX_STOPPED;
    ex->has_applied = false;
    ex->plan = NULL;
    ex->count = 0;
    ex->loop_ms = 0;
    ex->handoff = CONTROL_EX_HANDOFF_NONE;
    apply_neutral(ex);
}

void control_executor_rearm(control_executor_t *ex, control_state_t *st, uint32_t now_ms)
{
    if (ex == NULL || st == NULL || ex->state == CONTROL_EX_STOPPED) {
        return;
    }
    /* §4.6 lists the console re-subscribe among the neutrals; §4.7 says the loop
     * then continues at its current frame, so the clock and the frame survive
     * the neutral. */
    if (!apply_state(ex, control_executor_neutral)) {
        executor_fault(ex, st, CONTROL_ERR_BAD_PLAN);
        return;
    }
    ex->handoff = CONTROL_EX_HANDOFF_RESUME;
    ex->state = CONTROL_EX_HANDOFF;
    ex->deadline_ms = now_ms + CONTROL_EXECUTOR_HANDOFF_MAX_MS;
    ex->has_applied = false;
}

void control_executor_step(control_executor_t *ex, control_state_t *st, uint32_t now_ms)
{
    if (ex == NULL || st == NULL || ex->state == CONTROL_EX_STOPPED) {
        return;
    }

    if (ex->state == CONTROL_EX_HANDOFF) {
        if (!handoff_ready(ex, now_ms)) {
            return;
        }
        if (!resolve_handoff(ex, st, now_ms)) {
            executor_fault(ex, st, CONTROL_ERR_BAD_PLAN);
            return;
        }
    }

    for (;;) {
        /* §4.6's frame-index guard. The plan is validated at arm, so this is
         * defence in depth — and it is what makes the invariant assertable
         * rather than assumed. */
        if (!ex->has_applied || ex->frame >= ex->count) {
            executor_fault(ex, st, CONTROL_ERR_BAD_PLAN);
            return;
        }
        if ((int32_t)(now_ms - ex->frame_end_ms) < 0) {
            return; /* the frame's hold has not elapsed */
        }

        if ((uint16_t)(ex->frame + 1) < ex->count) {
            uint16_t next = (uint16_t)(ex->frame + 1);
            /* The last record's hold runs to `loop_ms` (§5.4), so it is set from
             * the boundary rather than from the record's own `hold_ms` — a plan
             * whose holds do not sum to `loop_ms` then still loops on the plan's
             * own clock instead of drifting. */
            if ((uint16_t)(next + 1) == ex->count) {
                ex->frame_end_ms = ex->base_ms + ex->loop_ms;
            } else {
                ex->frame_end_ms += record_hold(ex, next);
            }
            if (!apply_record(ex, st, next)) {
                executor_fault(ex, st, CONTROL_ERR_BAD_PLAN);
                return;
            }
            continue;
        }

        /*
         * The loop boundary. Neutral first and committed (§4.6); then the
         * boundary's two read-outs — the rate-limited `LOOP_COMPLETED` (§3.3,
         * which sets `loop_count` exactly) and the `CONFIG` that waited for a
         * safe moment (§2.9) — and then the restart.
         */
        control_event_loop_completed(st, st->status.loop_count + 1);
        control_config_apply_at_boundary(st);
        if (!apply_state(ex, control_executor_neutral)) {
            executor_fault(ex, st, CONTROL_ERR_BAD_PLAN);
            return;
        }
        ex->state = CONTROL_EX_HANDOFF;
        ex->handoff = CONTROL_EX_HANDOFF_LOOP;
        ex->deadline_ms = now_ms + CONTROL_EXECUTOR_HANDOFF_MAX_MS;
        ex->has_applied = false;

        /* If the reporter has already taken the neutral, restart in this same
         * step: that is §5.4's "no inter-loop gap". Otherwise the next tick
         * finishes the handoff, which is the cost of transmitting the neutral
         * rather than a gap the design scheduled. Either way, one boundary per
         * step, so a degenerate `loop_ms` cannot spin the task. */
        if (!handoff_ready(ex, now_ms)) {
            return;
        }
        if (!resolve_handoff(ex, st, now_ms)) {
            executor_fault(ex, st, CONTROL_ERR_BAD_PLAN);
        }
        return;
    }
}
