#ifndef PROTOCOL_CONTROL_MODE_H
#define PROTOCOL_CONTROL_MODE_H

/*
 * The five axes of §4.1, the mode transitions, and the panic stop of §4.5
 * (issue #23).
 *
 * Only one of the five axes is a mode, and only `mode` has transitions. The
 * others are orthogonal and are the reason the design works:
 *
 *   mode          IDLE / MACRO / AMIIBO   — moved by container verbs, the BOOT button
 *   console link  ADVERTISING / CONNECTED — moved by BLE events
 *   bonding       PAIRED / UNPAIRED       — moved by the firmware (ADR-0013)
 *   staging       NONE / PLAN / TAG       — bulk verbs, commit or abort
 *   control link  UP / DOWN               — **absent here on purpose**: the device
 *                                           cannot report it (§3.3), so the
 *                                           container infers it from the port
 *
 * `mode`, `console_link`, `bond` and `staging` live in `control_state_t`; the
 * verb layer already moves `staging`. This module is the one place a mode moves,
 * so `MODE_CHANGED` has exactly one emission site, and it owns the two-tier
 * BOOT stop: **the stop fires on the press edge, the forget on release** (§4.5).
 *
 * The panic stop is a pure state machine over `(level, now_ms)` samples, so the
 * host asserts the edges, the debounce and the CH9102's DTR→GPIO0 case without a
 * device. The firmware's 100 Hz GPIO0 task is the thin adapter in
 * `control_parser.c`.
 */

#include <stdbool.h>
#include <stdint.h>

#include "protocol/control/control_verbs.h"

#ifdef __cplusplus
extern "C" {
#endif

/* §4.5: ≤ 30 ms debounce, short < 1.5 s, long ≥ 1.5 s. The poll is 100 Hz. */
#define CONTROL_PANIC_DEBOUNCE_MS 30u
#define CONTROL_PANIC_LONG_MS 1500u
#define CONTROL_PANIC_POLL_MS 10u

typedef struct {
    bool started;       /* the first sample only establishes the reference */
    bool low;           /* the last raw level */
    bool candidate;     /* a high->low edge is being debounced */
    uint32_t low_since; /* when the current low run began */
    bool pressed;       /* a debounced press is in progress */
    uint32_t press_ms;  /* when that press began — the edge, not the confirm */
} control_panic_t;

typedef enum {
    CONTROL_PANIC_NONE = 0,
    CONTROL_PANIC_STOPPED, /* the press edge exited a mode */
    CONTROL_PANIC_FORGOT,  /* the release edge of a long press discarded */
} control_panic_action_t;

void control_panic_init(control_panic_t *panic);

/*
 * Feeds one raw `GPIO0` sample. @p level_low is the pin's level, @p now_ms a
 * monotonic millisecond clock.
 *
 * The first sample is a reference, never a press: **a level already low at boot
 * fires nothing**, because that is the CH9102's DTR→GPIO0 wiring looking like a
 * BOOT button that never gets released (§4.5). After that, a high→low edge
 * starts the debounce; the press is confirmed once the pin has been low for
 * `CONTROL_PANIC_DEBOUNCE_MS`, and the stop fires then. The low→high edge ends
 * it, and a press held for `CONTROL_PANIC_LONG_MS` escalates the release into
 * the forget.
 */
control_panic_action_t control_panic_step(control_state_t *st, control_panic_t *panic,
                                          bool level_low, uint32_t now_ms);

/* The press edge of §4.5: neutral release, unplace any tag, `IDLE`, and
 * `last_stop_reason=BOOT_LOCAL`. An idempotent no-op in `IDLE`. */
control_panic_action_t control_panic_stop(control_state_t *st);

/* The release edge of §4.5's long press: plan→none, tag bytes discarded, an
 * in-flight staging buffer aborted, **bond kept**. */
void control_panic_forget(control_state_t *st);

/*
 * The one place `status.mode` moves: sets the field and emits `MODE_CHANGED` on
 * a real edge (§3.3). `IDLE` also zeroes `current_frame`.
 */
void control_mode_enter(control_state_t *st, uint8_t mode);

/*
 * The one place a mode ends: `STOP` and the BOOT press edge share it, so the
 * neutral release, the tag unplacement and the mode edge cannot drift between
 * the container's stop and the device's. @p stop_reason is the §3.2 value
 * (`CONTAINER_STOP` / `BOOT_LOCAL`); it is written only when a mode was active.
 * `UNPLACE_AMIIBO` is deliberately not this — it is a tag operation, not a stop,
 * and it sets no stop reason (§4.3).
 */
void control_mode_exit(control_state_t *st, uint8_t stop_reason);

/* The one place a tag stops answering (§3.3). Clears `tag_state` and
 * `tag_identity` and emits `TAG_UNPLACED` only when a tag was placed, so
 * `UNPLACE_AMIIBO` on an idle device and a panic stop share one definition.
 * The 540 bytes are the caller's to keep or discard (§4.3 vs §4.5). */
void control_tag_unplace(control_state_t *st);

/* §4.1's console-link and bonding axes. `console_link` takes §3.3's `which` and
 * maps it onto the §3.2 value; `bond` has no event kind and is reported only. */
void control_set_console_link(control_state_t *st, uint8_t which, uint16_t reason);
void control_set_bond(control_state_t *st, uint8_t bond);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_MODE_H */
