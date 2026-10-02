/*
 * The CONTROL layer's firmware glue: drain the RX ring through the portable
 * decoder, answer each completed frame, queue the §3.3 events, and read the BOOT
 * button (spec §7.2, §7.3 steps 3-4, issue #23).
 *
 * The portable halves — framing, verbs, the mode model, the panic machine and
 * the EVENT surface — are the `.c` files beside this one and are asserted on the
 * host. This file is the thin adapter that owns ESP-IDF: the event queue, the
 * 100 Hz GPIO0 task, and the BLE state the axes report.
 */

#include "protocol/control/control_parser.h"

#if defined(CONFIG_PROTOCOL_LAYER_CONTROL)

#include <string.h>

#include "esp_random.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "driver/gpio.h"

#include "controller/controller.h"
#include "protocol/control/control_events.h"
#include "protocol/control/control_link.h"
#include "protocol/control/control_mode.h"
#include "protocol/control/control_protocol.h"
#include "protocol/control/control_verbs.h"

/* §4.5: GPIO0 is the BOOT strap the CH9102's DTR also drives. */
#define CONTROL_PANIC_GPIO 0

/*
 * A small queue in front of the wire (§3.3). Two reasons it exists rather than
 * writing the event straight from `control_event_emit`:
 *
 *  - **Order.** The parser task submits a reply only after the handler returns,
 *    so an event raised *inside* a handler would otherwise hit the wire first.
 *    §11 trace A is explicit that `START` ACKs and *then* emits `MODE_CHANGED`.
 *    The queue is drained on the parser's next pass, after the reply.
 *  - **Other tasks.** A console-link event comes from the NimBLE host task and a
 *    panic edge from the GPIO task; both enqueue under the lock and the parser
 *    task writes, so there is one writer.
 *
 * A full queue drops the event. That is the §3.1 bargain: an event is a hint, and
 * `STATUS` is the truth the hint would have pointed at.
 */
#define CONTROL_EVENT_QUEUE_LEN 8u

typedef struct {
    uint8_t kind;
    uint8_t len;
    uint8_t payload[CONTROL_EVENT_PAYLOAD_MAX];
} control_pending_event_t;

typedef struct {
    control_pending_event_t items[CONTROL_EVENT_QUEUE_LEN];
    size_t head;
    size_t count;
    SemaphoreHandle_t lock;
} control_event_queue_t;

typedef struct {
    control_decoder_t dec;
    control_state_t ctl;
    control_panic_t panic;
    control_event_queue_t events;
    uint8_t tx[CONTROL_WIRE_MAX];
} control_parser_state_t;

static control_parser_state_t s_control;

/*
 * One lock for the state model, because three tasks reach it: the parser task
 * (verbs), the 100 Hz panic task (§4.5) and the NimBLE host task (console-link
 * and bond edges). The event queue has its own lock and is taken *inside* this
 * one, never the other way round, so the two cannot deadlock.
 *
 * A `control_effects_t` callback runs while this lock is held, so a callback must
 * not call back into the control layer; it is the physical half (the executor of
 * #24, the `nfc_state` byte of #25), not a second verb handler.
 */
static SemaphoreHandle_t s_ctl_lock;

static void control_ctl_lock(void)
{
    if (s_ctl_lock != NULL) {
        xSemaphoreTake(s_ctl_lock, portMAX_DELAY);
    }
}

static void control_ctl_unlock(void)
{
    if (s_ctl_lock != NULL) {
        xSemaphoreGive(s_ctl_lock);
    }
}

/*
 * The plan staging buffer of §7.4: one plan slot, allocated for the life of the
 * process rather than per announce, because the committed plan *is* these bytes
 * (§5.3, no copy) and `plan_stage_cap` is what a too-large transfer is measured
 * against.
 */
static uint8_t s_plan_stage[CONTROL_PLAN_CAPACITY_BYTES];

/* ------------------------------------------------------------------- events */

static void control_event_sink_write(void *ctx, uint8_t kind, const uint8_t *payload, size_t len)
{
    control_event_queue_t *q = (control_event_queue_t *)ctx;
    if (q == NULL || q->lock == NULL) {
        return;
    }
    if (len > CONTROL_EVENT_PAYLOAD_MAX) {
        len = CONTROL_EVENT_PAYLOAD_MAX;
    }
    xSemaphoreTake(q->lock, portMAX_DELAY);
    if (q->count < CONTROL_EVENT_QUEUE_LEN) {
        size_t slot = (q->head + q->count) % CONTROL_EVENT_QUEUE_LEN;
        q->items[slot].kind = kind;
        q->items[slot].len = (uint8_t)len;
        if (len > 0 && payload != NULL) {
            memcpy(q->items[slot].payload, payload, len);
        }
        q->count++;
    }
    xSemaphoreGive(q->lock);
}

/* Pops one pending event as an encoded wire frame; 0 when none is queued. */
static size_t control_event_queue_pop(control_event_queue_t *q, uint8_t *out, size_t cap)
{
    size_t n = 0;
    if (q == NULL || q->lock == NULL) {
        return 0;
    }
    xSemaphoreTake(q->lock, portMAX_DELAY);
    if (q->count > 0) {
        control_pending_event_t *e = &q->items[q->head];
        n = control_event_encode(e->kind, e->payload, e->len, out, cap);
        q->head = (q->head + 1u) % CONTROL_EVENT_QUEUE_LEN;
        q->count--;
    }
    xSemaphoreGive(q->lock);
    return n;
}

/* ------------------------------------------------------------------- status */

/*
 * The one field that is a live clock. `console_link` and `bond` are moved by
 * `control_notify_console_link()` / `control_notify_bond()`; the mode, plan, tag,
 * `last_error` and `last_stop_reason` fields are owned by the portable layer.
 */
static void control_fill_status(control_state_t *st)
{
    st->status.uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
}

/* ------------------------------------------------------------------- layers */

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

    /* Unsolicited §3.3 events are deliberately NOT drained here. The router only
     * calls parse_frame() when the RX ring has a byte, and an event must not wait
     * for the host to send one; `control_parser_poll_event()` is drained by the
     * transport task every pass instead. That also keeps the reply of the request
     * in hand ahead of the events it raised (§11 trace A). */
    uint8_t byte;
    while (rsp->len == 0 && zc_read_byte(rb, &byte)) {
        control_dec_result_t result = control_decoder_feed(&s->dec, byte);
        if (result == CONTROL_DEC_FRAME) {
            control_ctl_lock();
            control_fill_status(&s->ctl);
            rsp->len = (uint32_t)control_handle_request(&s->ctl, &s->dec.last, s->tx,
                                                        sizeof(s->tx));
            /* §2.2/§7.3 step 2: logging is suppressed to a bounded rate for as
             * long as a staging buffer is open. */
            control_link_set_bulk_active(s->ctl.stage != CONTROL_STAGE_NONE);
            control_ctl_unlock();
        } else if (result == CONTROL_DEC_REJECT) {
            control_ctl_lock();
            rsp->len = (uint32_t)control_reject(&s->ctl, s->dec.reject_code,
                                                s->dec.reject_detail, s->tx, sizeof(s->tx));
            control_ctl_unlock();
        }
    }

    return rsp->len > 0 ? PARSE_OK : PARSE_NEED_MORE;
}

size_t control_parser_poll_event(uint8_t *out, size_t cap)
{
    return control_event_queue_pop(&s_control.events, out, cap);
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

/* -------------------------------------------------------------- panic stop */

/*
 * §4.5: 100 Hz, `gpio_get_level(GPIO0)`, no ISR. The machine below is the
 * portable one; this task only samples. GPIO0 is the BOOT strap pin, but the
 * strap is sampled by the ROM bootloader at reset and the machine reacts to
 * *edges*, so a level already low at boot fires nothing.
 */
static void control_panic_task(void *arg)
{
    (void)arg;
    const gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << CONTROL_PANIC_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);

    TickType_t last = xTaskGetTickCount();
    for (;;) {
        bool low = gpio_get_level(CONTROL_PANIC_GPIO) == 0;
        uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
        control_ctl_lock();
        control_panic_step(&s_control.ctl, &s_control.panic, low, now_ms);
        control_ctl_unlock();
        vTaskDelayUntil(&last, pdMS_TO_TICKS(CONTROL_PANIC_POLL_MS));
    }
}

/* --------------------------------------------------------------- the effects */

/*
 * PAIR_UNPAIR is the only effect #23 owns: bonding is one of the five axes and
 * the firmware owns it (ADR-0013). It always means *forget the bond and go
 * pairable*, never a toggle, and it changes no mode (§4.3). The executor and the
 * `nfc_state` byte arrive with #24 and #25.
 */
static void control_pair_unpair_effect(void *ctx)
{
    (void)ctx;
    controller_pairing_info_erase();
    control_set_bond(&s_control.ctl, CONTROL_BOND_UNPAIRED);
}
/* ------------------------------------------------------------- the glue API */

void control_parser_init(void)
{
    memset(&s_control, 0, sizeof(s_control));
    control_decoder_reset(&s_control.dec);
    /* §2.6: `boot_id` is fresh on every power cycle; the container keys the
     * new-power recovery case of §2.8 on it. */
    control_state_init(&s_control.ctl, esp_random(), s_plan_stage, sizeof(s_plan_stage));

    s_control.events.lock = xSemaphoreCreateMutex();
    s_ctl_lock = xSemaphoreCreateMutex();
    const control_event_sink_t sink = {
        .ctx = &s_control.events,
        .write = control_event_sink_write,
    };
    control_state_set_event_sink(&s_control.ctl, &sink);
    control_panic_init(&s_control.panic);

    const control_effects_t fx = {
        .ctx = NULL,
        .pair_unpair = control_pair_unpair_effect,
    };
    control_state_set_effects(&s_control.ctl, &fx);

    /* Read in every mode, with the control link up or down, and needing no ACK
     * (§4.5). It is the only halt that does not come from the container. */
    if (xTaskCreate(control_panic_task, "control_panic", 2560, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE("control", "failed to start the panic-stop task");
    }
}

void control_parser_boot_event(void)
{
    /* §3.3: emitted once the link can carry it, so an *in-session* reboot is
     * noticed in milliseconds rather than at the next 2 Hz poll. */
    control_ctl_lock();
    control_event_boot(&s_control.ctl);
    control_ctl_unlock();
}

void control_notify_console_link(uint8_t which, uint16_t reason)
{
    control_ctl_lock();
    control_set_console_link(&s_control.ctl, which, reason);
    control_ctl_unlock();
}

void control_notify_bond(uint8_t bond)
{
    control_ctl_lock();
    control_set_bond(&s_control.ctl, bond);
    control_ctl_unlock();
}

#endif /* CONFIG_PROTOCOL_LAYER_CONTROL */
