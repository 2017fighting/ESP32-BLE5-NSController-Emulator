/*
 * The §3.3 EVENT surface: encode, emit and the LOOP_COMPLETED rate limit.
 * Portable C; the host suite links this file directly (issue #23).
 *
 * See `control_events.h` for the one rule this file exists to keep — `STATUS` is
 * the truth, `EVENT` is a hint — and for why LOOP_COMPLETED is the only kind that
 * needs a limit.
 */

#include "protocol/control/control_events.h"

#include <string.h>

size_t control_event_encode(uint8_t kind, const uint8_t *payload, size_t len, uint8_t *out,
                            size_t cap)
{
    if (out == NULL || len > CONTROL_EVENT_PAYLOAD_MAX) {
        return 0;
    }
    uint8_t body[1u + CONTROL_EVENT_PAYLOAD_MAX];
    body[0] = kind;
    if (len > 0 && payload != NULL) {
        memcpy(&body[1], payload, len);
    }
    /* `verb` is 0 on an EVENT, reserved so an event is never mistaken for a
     * verb (§2.4, §3.3). */
    return control_encode(CONTROL_TYPE_EVENT, 0, body, 1u + len, out, cap);
}

void control_event_emit(control_state_t *st, uint8_t kind, const uint8_t *payload, size_t len)
{
    if (st == NULL || st->events.write == NULL) {
        return;
    }
    st->events.write(st->events.ctx, kind, payload, len);
}

void control_event_mode_changed(control_state_t *st)
{
    if (st == NULL) {
        return;
    }
    control_event_emit(st, CONTROL_EVENT_MODE_CHANGED, &st->status.mode, 1);
}

void control_event_plan_committed(control_state_t *st, const uint8_t hash[16])
{
    control_event_emit(st, CONTROL_EVENT_PLAN_COMMITTED, hash, 16);
}

void control_event_plan_discarded(control_state_t *st)
{
    control_event_emit(st, CONTROL_EVENT_PLAN_DISCARDED, NULL, 0);
}

void control_event_tag_placed(control_state_t *st, const uint8_t identity[7])
{
    control_event_emit(st, CONTROL_EVENT_TAG_PLACED, identity, 7);
}

void control_event_tag_unplaced(control_state_t *st)
{
    control_event_emit(st, CONTROL_EVENT_TAG_UNPLACED, NULL, 0);
}

void control_event_scan_ended(control_state_t *st)
{
    control_event_emit(st, CONTROL_EVENT_SCAN_ENDED, NULL, 0);
}

void control_event_console_link(control_state_t *st, uint8_t which, uint16_t reason)
{
    /* §3.3: `which` u8 then `reason` u16. The reason is 16 bits because the one
     * value that matters on this hardware is 531 = 0x0213 and does not fit a
     * byte. */
    uint8_t payload[3];
    payload[0] = which;
    control_wr_le16(&payload[1], reason);
    control_event_emit(st, CONTROL_EVENT_CONSOLE_LINK, payload, sizeof(payload));
}

void control_event_boot(control_state_t *st)
{
    if (st == NULL) {
        return;
    }
    uint8_t payload[4];
    control_wr_le32(payload, st->hello.boot_id);
    control_event_emit(st, CONTROL_EVENT_BOOT, payload, sizeof(payload));
}

bool control_event_loop_completed(control_state_t *st, uint32_t loop_count)
{
    if (st == NULL) {
        return false;
    }
    /* §3.2: this field is always exact, whatever the event does. */
    st->status.loop_count = loop_count;

    /* §3.3: emit only when the previous LOOP_COMPLETED has been superseded by a
     * STATUS reply, so a short macro cannot saturate the link. Suppressed, not
     * queued: loop_count already carries the truth. */
    if (st->loop_event_pending) {
        return false;
    }
    st->loop_event_pending = true;
    uint8_t payload[4];
    control_wr_le32(payload, loop_count);
    control_event_emit(st, CONTROL_EVENT_LOOP_COMPLETED, payload, sizeof(payload));
    return true;
}

void control_event_note_status_served(control_state_t *st)
{
    if (st != NULL) {
        st->loop_event_pending = false;
    }
}

size_t control_raise_error(control_state_t *st, uint8_t code, uint32_t detail, uint8_t *out,
                           size_t cap)
{
    if (st != NULL) {
        /* §3.2: `last_error` is read the same way whichever surface carries it,
         * and cleared by the next successful verb — not by this raise. */
        st->status.last_error_code = code;
        st->status.last_error_detail = detail;
        uint8_t payload[5];
        payload[0] = code;
        control_wr_le32(&payload[1], detail);
        control_event_emit(st, CONTROL_EVENT_ERROR_RAISED, payload, sizeof(payload));
    }
    return control_encode_error(code, detail, out, cap);
}
