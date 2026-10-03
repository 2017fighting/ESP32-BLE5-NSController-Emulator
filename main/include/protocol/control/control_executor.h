#ifndef PROTOCOL_CONTROL_EXECUTOR_H
#define PROTOCOL_CONTROL_EXECUTOR_H

/*
 * The plan executor and the neutral template (§7.3 step 5, §4.6, §5.4, §2.9;
 * issue #24).
 *
 * A pure state machine over `(control_state_t, now_ms)`. It walks the committed
 * plan's records against an exact-millisecond clock, writes each record's nine
 * state bytes through a small IO vtable, and owns the two things the design puts
 * on the device rather than in the data:
 *
 *  - **The neutral template (§4.6).** `control_executor_neutral` is compiled in
 *    and is never derived from plan bytes. It is applied on every exit path —
 *    arm, the loop boundary, `STOP`, the BOOT stop, the console re-subscribe
 *    re-arm and the fault path — and always through `apply_state`, so it is
 *    *committed*, not merely written into the back buffer. (A neutral that lived
 *    in the data could be omitted by a malformed plan; §5.2 relies on this.)
 *  - **The `LOOP_COMPLETED` rate limit (§3.3).** A boundary calls
 *    `control_event_loop_completed`, which suppresses the event until a `STATUS`
 *    reply has superseded the last one. `loop_count` stays exact either way.
 *
 * **The handoff (§4.6 against §5.4).** The reporter's double buffer carries one
 * `swap_request` bit (`hid_controller.c`), so writing neutral and then record 0
 * back to back loses the neutral: the reporter swaps once and only record 0
 * reaches the wire. The machine therefore has an explicit `HANDOFF` state. It
 * commits the neutral, then waits for `commit_idle()` — the adapter's view of
 * "the reporter has consumed that commit" — or for
 * `CONTROL_EXECUTOR_HANDOFF_MAX_MS`, and only then writes the next record.
 *
 * The wait is the transmission cost of the neutral itself, not a scheduled gap,
 * and §5.4's "no inter-loop gap" survives as the property it was meant to name:
 * the loop's own clock starts when its record 0 is applied, so
 * `sum(hold_ms) == loop_ms` holds of the loop's timeline. The reference player
 * pays the same cost — `web_ui.py::_play` awaits `_release_all()` and only then
 * sets `start = time.time()`.
 *
 * **Timing is absolute milliseconds, never an accumulated tick count.** Holds
 * are summed in the same clock `now_ms` comes from, so tick quantisation cannot
 * accumulate drift. `CONFIG_FREERTOS_HZ = 100` still quantises *when* the task
 * observes a deadline (§7.5) — a hold that is not a multiple of 10 ms lands on
 * the nearest tick — but it does not change how long a loop lasts. A plan's
 * `loop_ms` is authoritative: the last record's hold runs to `loop_ms` even if
 * the record says otherwise (§5.4).
 *
 * The module is portable C and carries no ESP-IDF; the host suite
 * (`test/host/test_control_executor.c`) drives it with a double reporter. The
 * task, the IO vtable and the wiring into `control_effects_t` are
 * `control_parser.c`.
 *
 * **Locking is the caller's.** `arm`/`stop`/`rearm` run from the verb layer and
 * the BLE callback, which already hold the one control lock; `step` runs from
 * the executor task, which takes it. This module takes nothing, and an IO
 * callback runs with that lock held, so it must not call back into the control
 * layer (§7.2's callback discipline).
 */

#include <stdbool.h>
#include <stdint.h>

#include "protocol/control/control_verbs.h"

#ifdef __cplusplus
extern "C" {
#endif

/* §5.3: the nine state bytes a record carries — buttons[3] ‖ left[3] ‖ right[3],
 * the report's own layout, so one memcpy reaches both. */
#define CONTROL_EXECUTOR_STATE_BYTES 9u

/* The bound on the handoff wait. It exists so an absent console — whose report
 * task never reaches the swap when notifications are disabled
 * (`hid_controller.c:44`) — cannot stall a replay, which is G-4's question
 * answered in code rather than by assumption. It is a safety bound, not the
 * expected wait: a live reporter consumes a commit within one report period. */
#define CONTROL_EXECUTOR_HANDOFF_MAX_MS 50u

/* The executor task's period. `CONFIG_FREERTOS_HZ = 100` makes this one tick;
 * `CONFIG_HID_REPORT_INTERVAL` is a separate axis (§7.5, ADR-0009), so a
 * smaller value here would spin and a larger one would only delay when a due
 * frame is observed, never how long it is held. */
#define CONTROL_EXECUTOR_TICK_MS 10u

/*
 * §4.6's template: the fixed neutral state. Zero button bytes (which is what
 * makes the guarantee stronger than the reference player's 18 named
 * buttons — a zeroed Pro2 field cannot miss `GR`/`GL`/`C`), and both sticks at
 * `PRO2_STICK_CENTER` (`0x800`) in `pack_stick_data` bit order.
 */
extern const uint8_t control_executor_neutral[CONTROL_EXECUTOR_STATE_BYTES];

/*
 * The physical half, supplied by the firmware adapter and by the host double.
 * Deliberately two functions: writing bytes and answering "has the reporter
 * taken them" are different questions, and only the second is what the handoff
 * needs.
 */
typedef struct {
    void *ctx;
    /* Write @p state into the report's back buffer and commit it, so the
     * reporter transmits on its next wake it (`controller_hid_commit`). Returns false
     * when there is no report to write to. */
    bool (*apply_state)(void *ctx, const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES]);
    /* True once the reporter has consumed the last commit. A NULL means "always
     * ready", which is what a test that does not model the reporter wants. */
    bool (*commit_idle)(void *ctx);
    /* Fired when a loop's record 0 has just been applied — `ARM` and the loop
     * boundary, never the `RESUME` of a console re-subscribe. The interval
     * between two of them is the loop period *as the report carries it*, which is
     * the only place §5.4's "no inter-loop gap" can be measured: the boundary's
     * neutral costs a report period before record 0 can replace it (§4.6), and
     * the plan's own clock cannot show that. NULL is "nobody is counting". */
    void (*loop_restarted)(void *ctx, uint32_t now_ms);
} control_executor_io_t;

typedef enum {
    CONTROL_EX_STOPPED = 0,
    CONTROL_EX_RUNNING,  /* walking records */
    CONTROL_EX_HANDOFF,  /* neutral committed, waiting to hand it to the reporter */
} control_executor_state_t;

typedef enum {
    CONTROL_EX_HANDOFF_NONE = 0,
    CONTROL_EX_HANDOFF_ARM,    /* `START`: the loop clock starts when frame 0 lands */
    CONTROL_EX_HANDOFF_LOOP,   /* the loop boundary: same, and no gap beyond the handoff */
    CONTROL_EX_HANDOFF_RESUME, /* console re-subscribe: the loop keeps its clock and frame */
} control_executor_handoff_t;

typedef struct {
    control_executor_io_t io; /* owned by value, so a caller's stack frame cannot dangle */
    bool io_set;
    control_executor_state_t state;
    control_executor_handoff_t handoff;

    /* The committed plan, read in place — `plan_slots = 1` means these *are* the
     * staged bytes (§7.4, no copy). */
    const uint8_t *plan;
    uint16_t count;
    uint32_t loop_ms;

    uint16_t frame;   /* the record the clock says is current */
    uint16_t applied; /* the record whose bytes are in the report */
    bool has_applied;

    uint32_t base_ms;      /* when the current loop's frame 0 was applied */
    uint32_t frame_end_ms; /* `base_ms + Σhold` through the current frame — exact ms */
    uint32_t deadline_ms;  /* the handoff's safety bound */
} control_executor_t;

void control_executor_init(control_executor_t *ex, const control_executor_io_t *io);

/*
 * `START`'s physical half (§4.3: "arm at frame 0; neutral first; ACK once
 * armed"). Validates the plan with the same `control_plan_check()` the commit
 * ran, so "structurally valid" keeps one definition. On a refusal it returns the
 * §2.5 code and **touches nothing** — a rejection must not leave state touched
 * (§2.3) — so the caller sends a typed `ERROR` and does not enter `MACRO`.
 *
 * **A zero-record plan is refused here.** It is structurally valid (`len == 12`
 * satisfies §5.3's check) and unrunnable, and the two are different questions:
 * shape is `LOAD_PLAN`'s, "can this device walk it" is this function's. §5.5's
 * compiler never emits one, so this is only reachable by a hand-built transfer —
 * and a typed `ERROR` before the mode moves beats a `MACRO` entered and abandoned.
 * The `frame_index` guard inside `control_executor_step` is the same rule for the
 * case where the plan goes bad after a successful arm.
 */
uint8_t control_executor_arm(control_executor_t *ex, control_state_t *st, uint32_t now_ms);

/* `STOP`/BOOT/any mode exit's physical half. Commits the neutral and halts.
 *
 * **Not conditional on having been running**: §4.6 makes the neutral the last
 * write of the *mode*, and the mode can end while this executor was never armed —
 * `AMIIBO` + `STOP` (§4.3) is exactly that, and requires a neutral release. So
 * the call is safe to make repeatedly and always leaves the neutral as the last
 * commit, which is what lets the verb layer's `stop` effect, the BOOT stop, the
 * `UNPLACE_AMIIBO` exit and a fault all share it without disagreeing. */
void control_executor_stop(control_executor_t *ex);

/*
 * §4.7: a console re-subscribe re-arms neutral and the loop continues at its
 * current frame. The neutral is committed here; the current record is re-applied
 * once the reporter has taken it, so the re-arm cannot lose either.
 */
void control_executor_rearm(control_executor_t *ex, control_state_t *st, uint32_t now_ms);

/* One observation of the clock. Idempotent and cheap when stopped. */
void control_executor_step(control_executor_t *ex, control_state_t *st, uint32_t now_ms);

bool control_executor_running(const control_executor_t *ex);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_EXECUTOR_H */
