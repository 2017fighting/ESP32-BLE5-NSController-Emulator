/*
 * The CONTROL layer's router glue: drain the RX ring through the portable
 * decoder and answer each completed frame (spec §7.2, §7.3 step 3).
 */

#include "protocol/control/control_parser.h"

#if defined(CONFIG_PROTOCOL_LAYER_CONTROL)

#include <string.h>

#include "esp_random.h"
#include "esp_timer.h"

#include "protocol/control/control_protocol.h"

typedef struct {
    control_decoder_t dec;
    control_hello_t hello;
    control_status_t status;
    uint8_t tx[CONTROL_WIRE_MAX];
} control_parser_state_t;

static control_parser_state_t s_control;

/*
 * STATUS field values are the work of #23 (the mode state model). Stage 1
 * reports the boot state of §4.8 — IDLE, nothing staged, nothing placed — plus
 * a live uptime so a bench reader can tell a reply from a timeout. `console_link`
 * and `bond` stay 0 (ADVERTISING/UNPAIRED) until #23 wires them to the BLE state.
 */
static void control_fill_status(control_status_t *status)
{
    control_status_default(status);
    status->uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
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
            control_fill_status(&s->status);
            rsp->len = (uint32_t)control_handle_request(&s->dec.last, &s->hello, &s->status,
                                                        s->tx, sizeof(s->tx));
        } else if (result == CONTROL_DEC_REJECT) {
            rsp->len = (uint32_t)control_encode_error(s->dec.reject_code, s->dec.reject_detail,
                                                      s->tx, sizeof(s->tx));
        }
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
    control_status_default(&s_control.status);
    /* §2.6: `boot_id` is fresh on every power cycle; the container keys the
     * new-power recovery case of §2.8 on it. */
    control_hello_default(&s_control.hello, esp_random());
}

#endif /* CONFIG_PROTOCOL_LAYER_CONTROL */
