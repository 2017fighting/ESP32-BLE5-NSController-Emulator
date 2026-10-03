#ifndef HID_CONTROLLER_H
#define HID_CONTROLLER_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "device.h"

#ifdef __cplusplus
extern "C" {
#endif

// memory barrier
#define MEMORY_BARRIER() __sync_synchronize()

// HID Report Interval(ms) - now configurable via menuconfig
#ifndef CONFIG_HID_REPORT_INTERVAL
#define CONFIG_HID_REPORT_INTERVAL 15
#endif
#define HID_REPORT_INTERVAL     CONFIG_HID_REPORT_INTERVAL

#define NS2_NOTIFICATION_HANDLE    0x000e

/* The controller state the plan executor writes as one run of bytes (§5.3):
 * buttons[3] ‖ left stick[3] ‖ right stick[3], contiguous at the report's 0x02.
 * A HID-layer constant rather than the control layer's
 * `CONTROL_EXECUTOR_STATE_BYTES`, because this layer must not depend on that one;
 * `control_parser_init` compares the two once and says so loudly if they differ. */
#define CONTROLLER_STATE_BYTES 9u

typedef struct controller_handle controller_handle_t;

/*
 * §7.5's report-cadence instrument (#35, §12.2 validation 3). The plan executor
 * knows when a record was *applied*; only this layer knows when a report
 * actually left the radio, so the measurement needs an observer on this side of
 * the seam. The base firmware installs none and carries no meter; the CONTROL
 * layer installs one at init (`control_parser.c`), which is what keeps
 * `hid_controller.c` free of both the control plane and a `#ifdef` about it.
 *
 * `state` is the report's nine state bytes at 0x02 (buttons ‖ left ‖ right,
 * §5.3) and is NULL when the report did not go out — there is no state to
 * report. `us` is `esp_timer_get_time()` at the attempt: the moment the
 * notification was handed to the host stack, not the moment the radio carried
 * it, which is the bound the link's own interval sets.
 */
typedef enum {
    CONTROLLER_REPORT_SENT = 0,   /* gatt_notify accepted it */
    CONTROLLER_REPORT_DROPPED_MSYS, /* the msys pool was low: input was skipped */
    CONTROLLER_REPORT_FAILED,     /* any other refusal */
} controller_report_outcome_t;

typedef void (*controller_report_observer_t)(void *ctx, controller_report_outcome_t outcome,
                                             const uint8_t state[CONTROLLER_STATE_BYTES],
                                             uint32_t us);

typedef struct {
    controller_type_t type;
    void *report;
} controller_hid_report_t;

// HID device specific operations (pro2, joycon, etc.)
typedef struct {
    const char *name;
    void (*report_init)(controller_hid_report_t *report);
    void (*set_button)(controller_hid_report_t *report, uint16_t btn_id, bool pressed);
    void (*set_button_custom)(controller_hid_report_t *report, uint8_t *data, size_t len);
    void (*set_left_stick)(controller_hid_report_t *report, uint16_t x, uint16_t y);
    void (*set_right_stick)(controller_hid_report_t *report, uint16_t x, uint16_t y);
    /* The plan executor's one write (§5.3, #24): the 9 state bytes of a plan
     * record — buttons[3] ‖ left[3] ‖ right[3] — placed contiguously into the
     * report. One op rather than three setters because the record's layout *is*
     * the report's layout, and splitting it would invite the two to drift. */
    void (*set_state)(controller_hid_report_t *report, const uint8_t state[9]);
    /* The NFC state byte (#25, §4.9): report offset 0x0C, driven by the NFC tag
     * server as a placement, an unplacement or the tag-absent gap moves it. NULL
     * for a HID type that has no NFC — a no-op, so no caller needs a type check. */
    void (*set_nfc_state)(controller_hid_report_t *report, uint8_t state);
    uint8_t* (*next_report)(controller_hid_report_t *report);
    size_t (*report_size)(void);
} controller_hid_ops_t;

// Controller management operations
//
// `commit_idle` is how a caller asks "has the reporter consumed the last commit?"
// without reaching into `controller_handle.buffer` itself. The plan executor needs
// it to honour §4.6 (the neutral must be *transmitted*, not merely written into the
// back buffer) while keeping §5.4's loop timing, and the double buffer's single
// `swap_request` bit is the only observable proof. It is a read, so it never blocks
// the report task.
typedef struct {
    const char *name;
    int  (*init)(controller_handle_t *ctrl, controller_type_t type);
    void (*deinit)(controller_handle_t *ctrl);
    int  (*start_task)(controller_handle_t *ctrl);
    void (*stop_task)(controller_handle_t *ctrl);
    controller_hid_report_t* (*get_back_buffer)(controller_handle_t *ctrl);
    void (*hid_commit)(controller_handle_t *ctrl);
    void (*hid_reset)(controller_handle_t *ctrl);
    bool (*commit_idle)(controller_handle_t *ctrl);
    /* A NULL observer (the default) means the report task tells nobody. */
    void (*set_report_observer)(controller_handle_t *ctrl, controller_report_observer_t observer,
                                void *ctx);
    /* #25: the NFC state byte of §4.9, written into *both* report buffers so the
     * console cannot be told the old value by a buffer the reporter has not
     * swapped away yet. A HID type without NFC ignores it. */
    void (*set_nfc_state)(controller_handle_t *ctrl, uint8_t state);
} controller_ops_t;

struct controller_handle {
    const controller_ops_t *ops;
    const controller_hid_ops_t *hid_ops;
    controller_type_t type;

    struct {
        controller_hid_report_t *front_buffer;
        controller_hid_report_t *back_buffer;
        volatile uint32_t swap_request;
    } buffer;

    TaskHandle_t task_handle;
    uint16_t     ns2_notification_handle;

    /* The report-cadence observer (#35); see `controller_report_observer_t`. */
    controller_report_observer_t report_observer;
    void *report_observer_ctx;
};

// Global controller instance
extern controller_handle_t g_hid_controller;

// Global controller operations
extern const controller_ops_t controller_ops;

// 12 bits stick data packed into 3 bytes
static inline void pack_stick_data(uint8_t out[3], uint16_t x, uint16_t y) {
    x &= 0xFFF;
    y &= 0xFFF;
    out[0] = x & 0xFF;
    out[1] = ((y & 0x0F) << 4) | ((x >> 8) & 0x0F);
    out[2] = (y >> 4) & 0xFF;
}

static inline void unpack_stick_data(const uint8_t in[3], uint16_t *x, uint16_t *y) {
    *x = in[0] | ((in[1] & 0x0F) << 8);
    *y = ((in[1] >> 4) & 0x0F) | (in[2] << 4);
    *x &= 0xFFF;
    *y &= 0xFFF;
}

#ifdef __cplusplus
}
#endif

#endif // HID_CONTROLLER_H
