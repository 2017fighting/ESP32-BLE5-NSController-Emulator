/*
 * The CONTROL layer's router glue: drain the RX ring through the portable
 * decoder and answer each completed frame (spec §7.2, §7.3 step 3).
 */

#include "protocol/control/control_parser.h"

#if defined(CONFIG_PROTOCOL_LAYER_CONTROL)

#include <string.h>

#include "esp_random.h"
#include "esp_timer.h"

#include "protocol/control/control_link.h"
#include "protocol/control/control_protocol.h"
#include "protocol/control/control_verbs.h"

typedef struct {
    control_decoder_t dec;
    control_state_t ctl;
    uint8_t tx[CONTROL_WIRE_MAX];
} control_parser_state_t;

static control_parser_state_t s_control;

/*
 * The plan staging buffer of §7.4: one plan slot, allocated for the life of the
 * process rather than per announce, because the committed plan *is* these bytes
 * (§5.3, no copy) and `plan_stage_cap` is what a too-large transfer is measured
 * against. #23/#24 own the final memory shape; 64 KiB fits internal SRAM.
 */
static uint8_t s_plan_stage[CONTROL_PLAN_CAPACITY_BYTES];

/*
 * STATUS field values are the work of #23 (the mode state model). The verb layer
 * keeps the fields it owns (mode, plan, tag, last_error, last_stop_reason); this
 * fills the one field that is a live clock, and `console_link`/`bond` stay 0
 * (ADVERTISING/UNPAIRED) until #23 wires them to the BLE state.
 */
static void control_fill_status(control_state_t *st)
{
    st->status.uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
}

static void control_parser_reset(void *state)
{
    control_decoder_reset(&((control_parser_state_t *)state)->dec);
}

static bool control_parser_probe(void *state, uint8_t *head_ptr, uint32_t head_len,
                                 uint8_t *wrap_ptr, uint32_t wrap_len)
{
    (void)state;
    (void)head_ptr;
    (void)head_len;
    (void)wrap_ptr;
    (void)wrap_len;
    /* The CONTROL layer claims the whole byte stream: everything that is not a
     * frame is noise, and the decoder is what tells them apart (§2.2). */
    return true;
}

static parse_result_t control_parser_parse_frame(void *state, zc_ringbuf_t *rb,
                                                 parser_rsp_t *rsp)
{
    control_parser_state_t *s = (control_parser_state_t *)state;
    rsp->data = s->tx;
    rsp->len = 0;

    uint8_t byte;
    while (rsp->len == 0 && zc_read_byte(rb, &byte)) {
        control_dec_result_t result = control_decoder_feed(&s->dec, byte);
        if (result == CONTROL_DEC_FRAME) {
            control_fill_status(&s->ctl);
            rsp->len = (uint32_t)control_handle_request(&s->ctl, &s->dec.last, s->tx,
                                                        sizeof(s->tx));
        } else if (result == CONTROL_DEC_REJECT) {
            rsp->len = (uint32_t)control_reject(&s->ctl, s->dec.reject_code,
                                                s->dec.reject_detail, s->tx, sizeof(s->tx));
        }
        /* §2.2/§7.3 step 2: logging is suppressed to a bounded rate for as long
         * as a staging buffer is open. */
        control_link_set_bulk_active(s->ctl.stage != CONTROL_STAGE_NONE);
    }

    return rsp->len > 0 ? PARSE_OK : PARSE_NEED_MORE;
}

static const protocol_parser_ops_t control_parser_ops = {
    .name = "control",
    .min_peek_len = 1,
    .reset = control_parser_reset,
    .probe = control_parser_probe,
    .parse_frame = control_parser_parse_frame,
};

static protocol_parser_t s_control_parser = {
    .ops = &control_parser_ops,
    .state = &s_control,
};

protocol_instance_t control_protocol_instance = {
    .name = "control",
    .max_peek_len = 1,
    .parser_count = 1,
    .parsers = {&s_control_parser},
};

void control_parser_init(void)
{
    memset(&s_control, 0, sizeof(s_control));
    control_decoder_reset(&s_control.dec);
    /* §2.6: `boot_id` is fresh on every power cycle; the container keys the
     * new-power recovery case of §2.8 on it. */
    control_state_init(&s_control.ctl, esp_random(), s_plan_stage, sizeof(s_plan_stage));
    /* `control_effects_t` is left zeroed: the physical halves of START/STOP, the
     * tag server and the BLE bond arrive with #23, #24 and #25, and a NULL
     * callback is the honest no-op until then. */
}

#endif /* CONFIG_PROTOCOL_LAYER_CONTROL */
