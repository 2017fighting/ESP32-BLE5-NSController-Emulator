#ifndef HID_CONTROLLER_PRO2_H
#define HID_CONTROLLER_PRO2_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include "controller/hid_controller.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PRO2_STICK_CENTER   0x800

// pro2 button format
// switch2_controller_research/hid_reports.md#button-format-3
typedef struct {
    // Buttons Byte 0
    uint8_t B        : 1; // 0x01
    uint8_t A        : 1; // 0x02
    uint8_t Y        : 1; // 0x04
    uint8_t X        : 1; // 0x08
    uint8_t R        : 1; // 0x10
    uint8_t ZR       : 1; // 0x20
    uint8_t Plus     : 1; // 0x40
    uint8_t RClick   : 1; // 0x80

    // Buttons Byte 1
    uint8_t Down     : 1; // 0x01
    uint8_t Right    : 1; // 0x02
    uint8_t Left     : 1; // 0x04
    uint8_t Up       : 1; // 0x08
    uint8_t L        : 1; // 0x10
    uint8_t ZL       : 1; // 0x20
    uint8_t Minus    : 1; // 0x40
    uint8_t LClick   : 1; // 0x80

    // Buttons Byte 2
    uint8_t Home     : 1; // 0x01
    uint8_t Capture  : 1; // 0x02
    uint8_t GR       : 1; // 0x04
    uint8_t GL       : 1; // 0x08
    uint8_t C        : 1; // 0x10
    uint8_t reserved0: 3; // 0x20-0x80 Placeholder
} btn_bits_pro2_t;

typedef enum {
    // btn_bits_pro2_t Byte 0
    B,      // bit 0
    A,      // bit 1
    Y,      // bit 2
    X,      // bit 3
    R,      // bit 4
    ZR,     // bit 5
    Plus,   // bit 6
    RClick, // bit 7

    // btn_bits_pro2_t Byte 1
    Down,   // bit 0
    Right,  // bit 1
    Left,   // bit 2
    Up,     // bit 3
    L,      // bit 4
    ZL,     // bit 5
    Minus,  // bit 6
    LClick, // bit 7

    // btn_bits_pro2_t Byte 2
    Home,   // bit 0
    Capture,// bit 1
    GR,     // bit 2
    GL,     // bit 3
    C,      // bit 4
    // bit 5-7 reserved
} btns_pro2;

typedef struct __attribute__((packed)) {
    uint8_t counter;            // 0x00 0x01 Counter
    uint8_t power_info;         // 0x01 0x01 Power Info
    btn_bits_pro2_t buttons;    // 0x02 0x03 Buttons
    uint8_t left_stick[3];      // 0x04 0x03 Left Analog Stick
    uint8_t right_stick[3];     // 0x07 0x03 Right Analog Stick
    uint8_t unknown_0x0b;       // 0x0B 0x01 Unknown Always 0x38?
    uint8_t nfc_state;          // 0x0C 0x01 NFC processor state (§4.9); 0x00 idle
    uint8_t headset_flag;       // 0x0D 0x01 Headset Flags
    uint8_t motion_data_len;    // 0x0E 0x01 Motion Data Length Always 0x28
    uint8_t motion_data[0x28];  // 0x0F 0x28 Motion Data
    uint8_t reserved[8];        // 0x37 0x08 Placeholder Default 0x00
} hid_report_pro2_t;
static_assert(sizeof(hid_report_pro2_t) == 63);

/* The plan executor writes a record's nine state bytes in one memcpy from
 * offset 0x02 (§5.3 says the state bytes *are* the report's own buttons ‖
 * left_stick ‖ right_stick layout). These pin that, and they are why the
 * off-by-one in the comments beside `left_stick`/`right_stick` (0x04/0x07) is
 * harmless: the fields below are the ones at 0x0B/0x0C, so the real offsets are
 * 0x02, 0x05 and 0x08 and the three are contiguous. The NFC state byte sits at
 * 0x0C (§4.9) and is driven by `controller_ops_t.set_nfc_state` (#25), not by
 * the executor's nine state bytes. */
static_assert(offsetof(hid_report_pro2_t, buttons) == 0x02,
              "plan state byte 0 must land at report 0x02");
static_assert(offsetof(hid_report_pro2_t, left_stick) == 0x05,
              "plan state byte 3 must land at report 0x05");
static_assert(offsetof(hid_report_pro2_t, right_stick) == 0x08,
              "plan state byte 6 must land at report 0x08");
static_assert(offsetof(hid_report_pro2_t, unknown_0x0b) == 0x0B,
              "the NFC state byte follows the sticks with no padding");
static_assert(offsetof(hid_report_pro2_t, nfc_state) == 0x0C,
              "the NFC state byte is report offset 0x0C (§4.9)");

extern controller_hid_ops_t controller_pro2_ops;

/* §4.6's neutral state, in the report's own nine-byte layout (buttons ‖ left ‖
 * right at offset 0x02). `pro2_report_init` uses it, so the report a fresh
 * controller carries and the release the executor guarantees share one
 * definition. The executor's template is asserted equal to it at init
 * (`control_parser.c`), which is the check two translation units cannot make at
 * compile time. */
extern const uint8_t pro2_neutral_state[9];

#define PRO2_FIRMWARE_INFO_SIZE 12
extern const uint8_t pro2_firmware_info[12];

#ifdef __cplusplus
}
#endif

#endif // HID_CONTROLLER_PRO2_H
