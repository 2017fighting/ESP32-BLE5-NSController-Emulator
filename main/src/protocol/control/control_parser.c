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
#include "controller/hid_controller.h"
#include "controller/hid_controller_pro2.h"
#include "controller/nfc_tag.h"
#include "protocol/control/nfc_trace.h"
#include "protocol/control/control_events.h"
#include "protocol/control/control_executor.h"
#include "protocol/control/control_link.h"
#include "protocol/control/control_meter.h"
#include "protocol/control/control_mode.h"
#include "protocol/control/control_protocol.h"
#include "protocol/control/control_verbs.h"
#include "transport/transport.h"

/* §4.5: GPIO0 is the BOOT strap the CH9102's DTR also drives. */
#define CONTROL_PANIC_GPIO 0

/*
 * A small queue in front of the wire (§3.3). Two reasons it exists rather than
 * writing the event straight from `control_event_emit`:
 *
 *  - **Order.** The parser task submits a reply only after the handler returns,
 *    so an event raised *inside* a handler would otherwise hit the wire first.
 *    §11 trace A is explicit that `START` ACKs and *then* emits `MODE_CHANGED`.
 *    The queue is drained on the parser's next turn, after the reply.
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
    control_executor_t ex;
    control_event_queue_t events;
    /* #25: the tag the console is served from. Its `state` is the HID report's
     * `nfc_state` byte and `STATUS.console_polling` at once (§4.9). */
    nfc_tag_t nfc;
    /* #36: the console's NFC traffic, drained as INFO text at the scan's edge
     * (§6.6, §12.2 validations 4/5) — never logged inline, because the ~9
     * round trips are exactly what the bench times. */
    nfc_trace_t nfc_trace;
    uint8_t tx[CONTROL_WIRE_MAX];
} control_parser_state_t;

static control_parser_state_t s_control;

/* The §7.5 meter's announce-time snapshot of the decoder's silent drops, so
 * the staging-close line can report the transfer's delta (see the instrument
 * comment in control_parser_parse_frame). Written by the protocol task only. */
static uint32_t s_drops_at_open;

/* #36: the NFC trace owes its readout. Set under the state lock wherever a
 * scan's edge is observed (the console's `0x04`, the container's unplace),
 * drained by the 10 ms task outside it — the two NFC paths that could flush
 * both hold the lock, and a readout is up to ~16 INFO lines of UART, which is
 * not lock-holding time. */
static volatile bool s_nfc_flush_pending;

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

/*
 * §4.6's template is the executor's compiled-in constant, and it must equal the
 * neutral the Pro2 report is initialised with — otherwise the neutral the design
 * guarantees and the report a fresh controller carries could disagree. The two
 * headers `static_assert` their own offsets, but neither can compare two
 * translation units, and a wrong neutral is exactly the kind of defect that is
 * invisible on the wire. So it is checked once, at init, and said loudly.
 */
static bool executor_neutral_matches_the_report(void)
{
    return memcmp(control_executor_neutral, pro2_neutral_state,
                  CONTROL_EXECUTOR_STATE_BYTES) == 0;
}

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
 * The one field that is a live clock, and the one that is the NFC server's:
 * `console_link` and `bond` are moved by `control_notify_console_link()` /
 * `control_notify_bond()`; the mode, plan, tag, `last_error` and
 * `last_stop_reason` fields are owned by the portable layer; `console_polling`
 * is the NFC server's *console* level (`#25`) — distinct from the report byte,
 * which is the mode's expression and lives in the HID layer (§4.9).
 */
static void control_fill_status(control_state_t *st)
{
    st->status.uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
    st->status.console_polling = nfc_tag_polling(&s_control.nfc);
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
     * transport task every turn instead. That also keeps the reply of the request
     * in hand ahead of the events it raised (§11 trace A). */
    uint8_t byte;
    while (rsp->len == 0 && zc_read_byte(rb, &byte)) {
        control_dec_result_t result = control_decoder_feed(&s->dec, byte);
        if (result == CONTROL_DEC_FRAME) {
            control_ctl_lock();
            control_fill_status(&s->ctl);
            bool was_staging = s->ctl.stage != CONTROL_STAGE_NONE;
            uint32_t staged_total = s->ctl.stage_total;
            rsp->len = (uint32_t)control_handle_request(&s->ctl, &s->dec.last, s->tx,
                                                        sizeof(s->tx));
            /* §2.2/§7.3 step 2: logging is suppressed to a bounded rate for as
             * long as a staging buffer is open. The post-state is read under
             * the same lock: the panic task can abort staging concurrently
             * (control_mode.c), and the meter must not miss that close. */
            bool now_staging = s->ctl.stage != CONTROL_STAGE_NONE;
            uint32_t stage_next_after = s->ctl.stage_next;
            uint32_t drops_now = control_decoder_silent_drops(&s->dec);
            control_link_set_bulk_active(now_staging);
            control_ctl_unlock();

            /*
             * The §7.5 ring instrument (§12.2 validation row 2, bench #34): one
             * INFO line per bulk transfer, printed after the staging window
             * closes so the §2.2 bulk rate limit no longer applies. What it
             * reports, and why each number is the one that answers "does the
             * ring absorb the §2.7 window":
             *
             *  - `ring hw` — the zc ring's occupancy high-water for this
             *    transfer (the meter is reset on the announce that opened
             *    staging, including one that supersedes an open transfer:
             *    `stage_next == 0` is the fresh-announce mark). High under a
             *    blast is the steady state, not a hazard — a chunk frame
             *    out-sizes the ring.
             *  - `spins` — producer iterations that found the ring full; the
             *    yield-loop's volume, throughput context only.
             *  - `backlog` — the UART driver RX backlog high-water against the
             *    driver ring's own size: the tier that actually drops bytes.
             *    ≪ capacity is the parser keeping pace; == capacity is the
             *    §7.5 cliff.
             *  - `drops` — the CONTROL decoder's silent-drop delta for the
             *    transfer (§2.8): frames that arrived but could not be
             *    trusted — a corrupted chunk, whatever the cause. This is the
             *    number that separates "ring full but nothing lost" from the
             *    data loss §7.5 warns about.
             *
             * Log text ahead of the reply's leading delimiter is exactly the
             * noise §2.2 framing exists to survive, and the reply itself is
             * never split (shared TX lock).
             */
            bool fresh_transfer = now_staging && stage_next_after == 0;
            if (fresh_transfer) {
                transport_rx_ring_reset();
                s_drops_at_open = drops_now;
            } else if (was_staging && !now_staging) {
                transport_rx_ring_stats_t ring;
                transport_rx_ring_get(&ring);
                ESP_LOGI("control",
                         "staging closed: total=%uB ring hw=%u/%u spins=%u "
                         "backlog=%u/%u drops=%u",
                         (unsigned)staged_total, (unsigned)ring.high_water,
                         (unsigned)ring.capacity, (unsigned)ring.spins,
                         (unsigned)ring.backlog_hw,
                         (unsigned)ring.backlog_capacity,
                         (unsigned)(drops_now - s_drops_at_open));
            }
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

/* The one clock the executor and the status field share. `esp_timer_get_time()`
 * is microseconds; the executor works in exact milliseconds. */
static uint32_t control_now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static uint32_t control_now_us(void)
{
    return (uint32_t)esp_timer_get_time();
}

/*
 * The plan executor's physical half (#24). These three functions are the whole
 * boundary between the portable walk and the report: write nine bytes and
 * commit, and answer whether the reporter has taken the commit. They run with
 * the control lock held (§7.2's callback discipline), so they touch nothing but
 * the HID layer — no event emission, no verb handling.
 */
static bool executor_apply_state(void *ctx, const uint8_t state[CONTROL_EXECUTOR_STATE_BYTES])
{
    (void)ctx;
    if (g_hid_controller.hid_ops == NULL || g_hid_controller.hid_ops->set_state == NULL) {
        return false;
    }
    controller_hid_report_t *back = g_hid_controller.ops->get_back_buffer(&g_hid_controller);
    if (back == NULL) {
        return false;
    }
    g_hid_controller.hid_ops->set_state(back, state);
    g_hid_controller.ops->hid_commit(&g_hid_controller);
    /* §7.5's instrument (#35): every write — the neutral included — advances the
     * generation a notification is labelled with, which is how the bench tells
     * an input that reached the console from one the report period skipped. */
    macro_meter_apply(state);
    return true;
}

/* The one handoff wait in flight (#35); cleared at every `START`, so a run stopped
 * mid-handoff cannot lend its start time to the next run's first boundary. */
static struct {
    uint32_t since_us;
    bool waiting;
} s_handoff;

static bool executor_commit_idle(void *ctx)
{
    (void)ctx;
    /* §4.6 against §5.4: the reporter's one `swap_request` bit is the only
     * observable proof that the neutral left the back buffer — which is what
     * "committed and transmitted" means in this firmware. It is a *read*, so the
     * wait never blocks the report task.
     *
     * **There is nothing to wait for when no console has subscribed.** The
     * report task skips the swap entirely without a notify-enabled subscriber
     * (`hid_controller.c`), so `swap_request` would stay set forever and every
     * loop boundary would pay the full 50 ms bound — a 1000 ms loop measured
     * +51.9 ms per loop on the bench (G-4's "is a macro run harmless
     * against an absent console?" answered in the code). Nobody is subscribed
     * means nobody is owed the neutral, so the handoff is vacuously complete.
     */
    uint32_t us = control_now_us();
    g_subscribe_state_t *sub = subscribe_entry_get(NS2_NOTIFICATION_HANDLE);
    bool idle;
    if (sub == NULL || !sub->notify_enabled || sub->conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        idle = true;
    } else {
        /* Via the ops table, not the buffer: "has the reporter taken the commit"
         * is the HID layer's question to answer, not the control layer's to
         * assume. */
        idle = g_hid_controller.ops->commit_idle(&g_hid_controller);
    }

    /* The handoff's cost, measured where the executor feels it (#35): the wait
     * runs from this callback's first `not yet` to the `taken`. #24 could only
     * bound it (≤ 50 ms) and bound it by construction; with a console subscribed
     * this is the number. Only one wait can be open at a time — the executor
     * resolves a handoff before it starts the next — so one tracker is enough. */
    if (idle) {
        if (s_handoff.waiting) {
            macro_meter_handoff(us - s_handoff.since_us);
            s_handoff.waiting = false;
        }
    } else if (!s_handoff.waiting) {
        s_handoff.since_us = us;
        s_handoff.waiting = true;
    }
    return idle;
}

/* §5.4's loop period, as the report carries it (#35): the interval between two
 * record-0 applies. The plan's own clock is exact by construction, so the plan
 * cannot show what the boundary's neutral costs; this is the measurement. */
static void executor_loop_restarted(void *ctx, uint32_t now_ms)
{
    (void)ctx;
    (void)now_ms;
    macro_meter_restart(control_now_us());
}

static uint8_t executor_start_macro(void *ctx)
{
    (void)ctx;
    /* Armed *before* the arm, so the arm's own neutral is generation 1 rather
     * than an uncounted write, and undone if the arm refuses (§2.3: a rejection
     * leaves state untouched — including the instrument's). */
    s_handoff.waiting = false;
    macro_meter_arm(control_now_us());
    uint8_t code = control_executor_arm(&s_control.ex, &s_control.ctl, control_now_ms());
    if (code != CONTROL_ERR_NONE) {
        macro_meter_disarm();
    }
    return code;
}

static void executor_stop(void *ctx, uint8_t reason)
{
    (void)ctx;
    (void)reason;
    /* §4.3: the neutral, the tag unplacement and the mode edge are
     * `control_mode_exit`'s; this is only the executor's half, so the
     * container's STOP and the BOOT stop cannot drift. Every exit from `AMIIBO`
     * lands here too, which is where the tag stops answering (#25). */
    control_executor_stop(&s_control.ex);
    nfc_tag_unplace(&s_control.nfc);
    /* Every exit from the mode lands here — `STOP`, the BOOT stop, the fault
     * path and the `AMIIBO` exits — so the run's readout has one site. It prints
     * nothing when the meter was never armed. */
    macro_meter_report(control_now_us());
}

/*
 * The report task's view of what actually left the radio (§7.5, #35). Installed
 * at init and never removed; the HID layer calls it with the lock *not* held, so
 * it touches the meter and nothing else — no verbs, no events (§7.2's callback
 * discipline).
 */
static void control_report_observer(void *ctx, controller_report_outcome_t outcome,
                                    const uint8_t state[9], uint32_t us)
{
    (void)ctx;
    switch (outcome) {
    case CONTROLLER_REPORT_SENT:
        if (state != NULL) {
            macro_meter_notify(state, us);
        }
        break;
    case CONTROLLER_REPORT_DROPPED_MSYS:
        macro_meter_dropped(true, us);
        break;
    case CONTROLLER_REPORT_FAILED:
        macro_meter_dropped(false, us);
        break;
    }
}

/*
 * PAIR_UNPAIR is the only effect #23 owns: bonding is one of the five axes and
 * the firmware owns it (ADR-0013). It always means *forget the bond and go
 * pairable*, never a toggle, and it changes no mode (§4.3). The `nfc_state`
 * byte arrives with #25.
 */
static void control_pair_unpair_effect(void *ctx)
{
    (void)ctx;
    controller_pairing_info_erase();
    control_set_bond(&s_control.ctl, CONTROL_BOND_UNPAIRED);
}

/*
 * The NFC tag server's two effects (#25). Both run with the control lock held
 * (§7.2's callback discipline), so they touch the tag and the report and must
 * not call back into the control layer.
 *
 * `state_changed` is the §4.9 mechanism: the report byte *is* the mode's
 * physical expression, so every placement, unplacement and gap writes it. The
 * scan-ended edge rides the control event queue instead, because it is a §3.3
 * promise to the container rather than a byte the console reads — and it is
 * emitted from the console's own command path, never from a `place`/`unplace`
 * effect (`SCAN_ENDED` is "the console stopped polling", §6.5).
 */
static void nfc_state_changed(void *ctx, uint8_t state)
{
    (void)ctx;
    if (g_hid_controller.ops != NULL && g_hid_controller.ops->set_nfc_state != NULL) {
        g_hid_controller.ops->set_nfc_state(&g_hid_controller, state);
    }
}

/* The same write, forced: used where the HID report has just been re-initialised
 * under us, so "the byte did not change" is not the same as "the byte is on the
 * wire" (§4.7's console re-subscribe). */
static void nfc_assert_state_byte(void)
{
    if (g_hid_controller.ops != NULL && g_hid_controller.ops->set_nfc_state != NULL) {
        g_hid_controller.ops->set_nfc_state(&g_hid_controller, nfc_tag_state(&s_control.nfc));
    }
}

static void nfc_scan_ended(void *ctx)
{
    (void)ctx;
    control_event_scan_ended(&s_control.ctl);
}

static void control_place_tag_effect(void *ctx, const uint8_t *tag, size_t len)
{
    (void)ctx;
    /* `control_now_ms()` is the same clock the executor works on, so the §6.5 gap
     * deadline cannot drift from the loop's. */
    nfc_tag_place(&s_control.nfc, tag, len, control_now_ms());
}

static void control_unplace_tag_effect(void *ctx)
{
    (void)ctx;
    nfc_tag_unplace(&s_control.nfc);
    /* The unplace is a scan edge too (§6.5): whatever the console did with the
     * tag is a complete story once the tag leaves the field. */
    s_nfc_flush_pending = true;
}

/* ---------------------------------------------------------- the executor task */

/* #36: drain the NFC trace as INFO text. One line per lock hold — formatting
 * is a slot's worth of work — so the state lock is never held across a UART
 * write, and the drain can interleave with a console that starts a new scan
 * mid-readout (a new event simply appends and the loop continues). */
static void nfc_trace_flush_log(void)
{
    char line[NFC_TRACE_LINE_MAX];
    control_ctl_lock();
    size_t n = nfc_trace_format(&s_control.nfc_trace, line, sizeof(line));
    control_ctl_unlock();
    while (n > 0) {
        ESP_LOGI("control", "%s", line);
        control_ctl_lock();
        n = nfc_trace_format(&s_control.nfc_trace, line, sizeof(line));
        control_ctl_unlock();
    }
}

/*
 * §7.3 step 5: the task that walks the plan. `10 ms` is one tick at
 * `CONFIG_FREERTOS_HZ = 100`; the report density is a separate axis (§7.5,
 * ADR-0009), so a smaller period here would only spin and a larger one would
 * only delay *when* a due frame is observed, never how long it is held.
 */
static void control_executor_task(void *arg)
{
    (void)arg;
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        control_ctl_lock();
        control_fill_status(&s_control.ctl);
        uint32_t now_ms = control_now_ms();
        control_executor_step(&s_control.ex, &s_control.ctl, now_ms);
        /* §6.5's gap: the new tag starts answering once the console has had a
         * report period to sample the absent field. One tick late is an observed
         * deadline, never an extended gap. */
        nfc_tag_tick(&s_control.nfc, now_ms);
        control_ctl_unlock();
        /* #36: the NFC trace's deferred readout, on the tick after the edge that
         * owed it — see `s_nfc_flush_pending`. */
        if (s_nfc_flush_pending) {
            s_nfc_flush_pending = false;
            nfc_trace_flush_log();
        }
        vTaskDelayUntil(&last, pdMS_TO_TICKS(CONTROL_EXECUTOR_TICK_MS));
    }
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
    nfc_tag_init(&s_control.nfc);
    nfc_trace_init(&s_control.nfc_trace);
    s_nfc_flush_pending = false;
    const nfc_tag_events_t nfc_events = {
        .ctx = NULL,
        .state_changed = nfc_state_changed,
        .scan_ended = nfc_scan_ended,
    };
    nfc_tag_set_events(&s_control.nfc, &nfc_events);

    if (!executor_neutral_matches_the_report()) {
        ESP_LOGE("control", "the executor's neutral does not match the Pro2 report's");
    }

    const control_executor_io_t io = {
        .ctx = NULL,
        .apply_state = executor_apply_state,
        .commit_idle = executor_commit_idle,
        .loop_restarted = executor_loop_restarted,
    };
    control_executor_init(&s_control.ex, &io);

    /* §7.5's report-cadence instrument (#35): installed rather than called, so
     * the HID layer needs no knowledge of the control plane and a build without
     * it carries no meter. */
    g_hid_controller.ops->set_report_observer(&g_hid_controller, control_report_observer, NULL);

    const control_effects_t fx = {
        .ctx = NULL,
        .start_macro = executor_start_macro,
        .stop = executor_stop,
        .place_tag = control_place_tag_effect,
        .unplace_tag = control_unplace_tag_effect,
        .pair_unpair = control_pair_unpair_effect,
    };
    control_state_set_effects(&s_control.ctl, &fx);

    /* §7.3 step 5. Created here, before the transport can carry a `START`, so an
     * arm cannot depend on a task that has not started. */
    if (xTaskCreate(control_executor_task, "control_exec", 3072, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE("control", "failed to start the plan executor task");
    }

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
    if (which == CONTROL_CONSOLE_EVENT_RESUBSCRIBED) {
        /* §4.6/§4.7: the re-subscribe is one of the neutral's exit paths, and
         * the loop then continues at its current frame rather than restarting.
         * `gap.c` still calls `hid_reset` for the report buffers; this is the
         * executor's half of the same edge. */
        control_executor_rearm(&s_control.ex, &s_control.ctl, control_now_ms());
        /* `hid_reset` re-initialised the report buffers, which resets the NFC
         * byte to `0x00`; a placed tag must go back on the wire or the console
         * would never see it again (§4.9, #25). */
        nfc_assert_state_byte();
    }
    control_ctl_unlock();
}

void control_notify_bond(uint8_t bond)
{
    control_ctl_lock();
    control_set_bond(&s_control.ctl, bond);
    control_ctl_unlock();
}

size_t control_nfc_command(uint8_t subcmd, const uint8_t *payload, size_t len, uint8_t *out,
                           size_t out_cap)
{
    /* The NFC state is shared with the `PLACE_AMIIBO`/`UNPLACE_AMIIBO` effects,
     * which run under the control lock, and with `STATUS`'s `console_polling`,
     * which is read under it — so the console's own commands take it too rather
     * than introducing a second lock and a lock order. */
    control_ctl_lock();
    size_t n = nfc_tag_command(&s_control.nfc, subcmd, payload, len, out, out_cap);
    /* #36: record the exchange (the CRC is over exactly the `n` bytes served)
     * and, on the scan's end, owe the readout — logged a tick later, off this
     * path entirely, so the ~9 round trips the bench times never pay for their
     * own instrumentation (§6.6, `nfc_trace.h`). */
    nfc_trace_feed(&s_control.nfc_trace, subcmd, payload, len, out, n, control_now_ms());
    if (subcmd == NFC_CMD_STOP_POLLING) {
        s_nfc_flush_pending = true;
    }
    control_ctl_unlock();
    return n;
}

void control_notify_console_interval(int conn_itvl)
{
    /* Deliberately unchecked. The NS2 sets `conn_itvl=4` — 5.0 ms — on every
     * connection, which is *below* the 6-unit minimum a compliant central would
     * request (that is why `ble_gap_update_params` fails on every connect, §9.1);
     * a "sanity" bound here would have suppressed the one number validation 3
     * needs. This function's whole job is to report what the console did. */
    ESP_LOGI("control", "console link: conn_itvl=%d conn_itvl_ms=%u", conn_itvl,
             (unsigned)((conn_itvl * 125u) / 100u));
}

#endif /* CONFIG_PROTOCOL_LAYER_CONTROL */
