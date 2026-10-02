#ifndef PROTOCOL_CONTROL_EVENTS_H
#define PROTOCOL_CONTROL_EVENTS_H

/*
 * The §3.3 EVENT surface (issue #23).
 *
 * One rule governs everything here: **`STATUS` is the truth, `EVENT` is a hint
 * that the truth changed** (§3.1). So an event carries almost no payload, marks
 * an edge and never a level, and losing one costs a poll. The kinds are numbered
 * 1-10 in §3.3's table order and their payload widths are owned by that table.
 *
 * The module is portable C: it encodes the frame and hands it to a
 * `control_event_sink_t` (declared in `control_protocol.h`), so the host suite
 * asserts kind and payload without a device, and the firmware adapter is
 * `control_link_write` under the shared TX lock.
 *
 * **`LOOP_COMPLETED` is the one kind that needs a limit.** A macro can loop many
 * times per second, so the event is emitted at most at the `STATUS` poll rate:
 * a boundary emits only when the previous event has been superseded by a poll
 * (§3.3). `loop_count` in `STATUS` stays exact either way.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "protocol/control/control_verbs.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Encodes one EVENT frame — `0x00 · COBS(ver|3|0|len|crc|kind|payload) · 0x00`
 * (§2.2) — with @p kind as the first payload byte. Returns the wire length, or 0
 * when @p out_cap is too small or @p len exceeds CONTROL_EVENT_PAYLOAD_MAX. The
 * firmware adapter uses this; a test uses it to pin the wire shape. */
size_t control_event_encode(uint8_t kind, const uint8_t *payload, size_t len, uint8_t *out,
                            size_t cap);

/* Hands one event to the installed sink; a no-op without one. */
void control_event_emit(control_state_t *st, uint8_t kind, const uint8_t *payload, size_t len);

/* The ten kinds, one helper each, so a caller never assembles a payload by hand.
 * Each takes the value §3.3 defines for its kind. */
void control_event_mode_changed(control_state_t *st);
void control_event_plan_committed(control_state_t *st, const uint8_t hash[16]);
void control_event_plan_discarded(control_state_t *st);
void control_event_tag_placed(control_state_t *st, const uint8_t identity[7]);
void control_event_tag_unplaced(control_state_t *st);
void control_event_scan_ended(control_state_t *st);
void control_event_console_link(control_state_t *st, uint8_t which, uint16_t reason);
void control_event_boot(control_state_t *st);

/* The rate-limited kind. Sets `STATUS.loop_count` to @p loop_count (the field is
 * always exact) and returns true when the event was emitted, false when a
 * previous `LOOP_COMPLETED` is still un-superseded by a poll. */
bool control_event_loop_completed(control_state_t *st, uint32_t loop_count);

/* §3.2: a served `STATUS` reply supersedes a pending `LOOP_COMPLETED`, which is
 * what rate-limits it. Called by the dispatch on every STATUS reply. */
void control_event_note_status_served(control_state_t *st);

/*
 * §3.3: `ERROR_RAISED` is for an `ERROR` the device raises on its own — the
 * executor fault path of §4.6 — not for the typed `ERROR` that answers the
 * container's request in hand (`control_reject()`, which is the reply itself).
 * Records the §3.2 pair, emits the event, and returns the encoded `ERROR` reply
 * for the caller to write.
 */
size_t control_raise_error(control_state_t *st, uint8_t code, uint32_t detail, uint8_t *out,
                           size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_EVENTS_H */
