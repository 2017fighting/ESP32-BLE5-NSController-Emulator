/*
 * The five axes, the mode transitions and the panic stop (spec §4.1, §4.3,
 * §4.5). Portable C; the host suite links this file directly (issue #23).
 *
 * `control_mode_enter()` and `control_mode_exit()` are the only places a mode
 * moves, so `MODE_CHANGED` has one emission site and the container's `STOP` and
 * the device's BOOT press share the neutral release, the tag unplacement and the
 * mode edge instead of two code paths that can drift.
 *
 * The panic machine is a pure function of `(level, now_ms)`: the firmware's
 * 100 Hz GPIO0 task is a thin adapter, and the host asserts the edges, the
 * debounce and the CH9102 case without a device.
 */

#include "protocol/control/control_mode.h"

#include <string.h>

#include "protocol/control/control_bulk.h"
#include "protocol/control/control_events.h"

void control_panic_init(control_panic_t *panic)
{
    if (panic != NULL) {
        memset(panic, 0, sizeof(*panic));
    }
}

void control_set_console_link(control_state_t *st, uint8_t which, uint16_t reason)
{
    if (st == NULL) {
        return;
    }
    /* §3.2's `console_link` is a level; §3.3's `which` is the edge that moved it.
     * A re-subscribe is still CONNECTED. */
    st->status.console_link = (which == CONTROL_CONSOLE_EVENT_DISCONNECTED)
                                  ? CONTROL_CONSOLE_ADVERTISING
                                  : CONTROL_CONSOLE_CONNECTED;
    control_event_console_link(st, which, reason);
}

void control_set_bond(control_state_t *st, uint8_t bond)
{
    if (st != NULL) {
        /* No event kind exists for the bond (§3.3); it is reported, not announced. */
        st->status.bond = bond;
    }
}

void control_mode_enter(control_state_t *st, uint8_t mode)
{
    if (st == NULL || st->status.mode == mode) {
        return;
    }
    st->status.mode = mode;
    if (mode == CONTROL_MODE_IDLE) {
        st->status.current_frame = 0;
    }
    control_event_mode_changed(st);
}

void control_mode_exit(control_state_t *st, uint8_t stop_reason)
{
    if (st == NULL || st->status.mode == CONTROL_MODE_IDLE) {
        return;
    }
    /* The physical half: neutral release, executor stopped, `nfc_state` byte to
     * idle. It is the same callback `STOP` uses, so the release invariant (§4.6)
     * has one owner. */
    if (st->fx.stop != NULL) {
        st->fx.stop(st->fx.ctx, stop_reason);
    }
    control_tag_unplace(st);
    st->status.current_frame = 0;
    control_mode_enter(st, CONTROL_MODE_IDLE);
    st->status.last_stop_reason = stop_reason;
}

void control_tag_unplace(control_state_t *st)
{
    if (st == NULL) {
        return;
    }
    bool was_placed = st->tag_placed;
    st->tag_placed = false;
    st->status.tag_state = CONTROL_TAG_NONE;
    memset(st->status.tag_identity, 0, sizeof(st->status.tag_identity));
    /* §3.3: the event marks a tag that stopped answering, not a field that was
     * already clear. */
    if (was_placed) {
        control_event_tag_unplaced(st);
    }
}

void control_panic_forget(control_state_t *st)
{
    if (st == NULL) {
        return;
    }
    /* §4.5: the long press discards the committed plan and the loaded tag bytes,
     * and keeps the bond. The §3.3 event fires only for the plan, because that
     * is the only thing the container can see was there. */
    bool had_plan = st->plan_committed;
    control_plan_discard(st);
    control_tag_unplace(st);
    memset(st->tag, 0, sizeof(st->tag));

    /* "Forget" includes bytes that never committed (§2.7 rule 7). */
    control_stage_abort(st);

    if (had_plan) {
        control_event_plan_discarded(st);
    }
}

control_panic_action_t control_panic_stop(control_state_t *st)
{
    if (st == NULL || st->status.mode == CONTROL_MODE_IDLE) {
        return CONTROL_PANIC_NONE;
    }
    control_mode_exit(st, CONTROL_STOP_BOOT_LOCAL);
    return CONTROL_PANIC_STOPPED;
}

control_panic_action_t control_panic_step(control_state_t *st, control_panic_t *panic,
                                          bool level_low, uint32_t now_ms)
{
    if (st == NULL || panic == NULL) {
        return CONTROL_PANIC_NONE;
    }

    if (!panic->started) {
        /* §4.5: the application reacts to *edges*. A level already low at boot —
         * the ROM strap, or a container that asserted DTR before the device came
         * up — establishes the reference and fires nothing; only a later
         * high->low edge starts a candidate. */
        panic->started = true;
        panic->low = level_low;
        panic->candidate = false;
        panic->low_since = now_ms;
        return CONTROL_PANIC_NONE;
    }

    if (level_low && !panic->low) {
        panic->low = true;
        panic->candidate = true;
        panic->low_since = now_ms;
        return CONTROL_PANIC_NONE;
    }

    if (!level_low && panic->low) {
        panic->low = false;
        panic->candidate = false;
        if (!panic->pressed) {
            /* A bounce, or a candidate that never confirmed. */
            return CONTROL_PANIC_NONE;
        }
        panic->pressed = false;
        /* §4.5: the forget is release-gated, so a press that never gets released
         * (the DTR hazard) can never silently discard the plan. */
        if ((uint32_t)(now_ms - panic->press_ms) >= CONTROL_PANIC_LONG_MS) {
            control_panic_forget(st);
            return CONTROL_PANIC_FORGOT;
        }
        return CONTROL_PANIC_NONE;
    }

    if (panic->candidate && !panic->pressed &&
        (uint32_t)(now_ms - panic->low_since) >= CONTROL_PANIC_DEBOUNCE_MS) {
        panic->pressed = true;
        panic->candidate = false;
        /* The press began at the edge, not at the confirmation, so the 1.5 s
         * window is measured from the same instant the stop fired. */
        panic->press_ms = panic->low_since;
        return control_panic_stop(st);
    }

    return CONTROL_PANIC_NONE;
}
