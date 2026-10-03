#ifndef PROTOCOL_CONTROL_PARSER_H
#define PROTOCOL_CONTROL_PARSER_H

/*
 * The CONTROL layer as a `protocol_router` parser (spec §7.2, §7.3 step 3).
 *
 * Unlike the EasyCon parsers, this one is a *streaming* parser: a control frame
 * is up to 512 bytes and the RX ring is 256, so nothing can wait for a whole
 * frame to be contiguous. It drains the ring byte by byte through the portable
 * decoder in `control_protocol.h` and returns one reply per completed frame.
 */

#include "protocol/protocol.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(CONFIG_PROTOCOL_LAYER_CONTROL)

extern protocol_instance_t control_protocol_instance;

/* Generates this boot's `boot_id` (§2.6), readies the decoder, installs the
 * §3.3 event sink and starts the §4.5 panic-stop task. Call once, before the
 * protocol task starts. */
void control_parser_init(void);

/* Emits the §3.3 BOOT event. Separate from init because it must go out once the
 * control link's TX lock exists: the container may still hold the port across a
 * reboot, and that is exactly the case BOOT is for. */
void control_parser_boot_event(void);

/*
 * Pops one pending §3.3 event as an encoded wire frame into @p out, or 0 when
 * none is queued. The transport task calls this on every pass: the protocol
 * router only reaches a parser when the RX ring has a byte, and an unsolicited
 * event must not wait for the host to send one. Draining here — after the
 * previous pass submitted its reply — is also what keeps a request's reply ahead
 * of the events it raised (§11 trace A).
 */
size_t control_parser_poll_event(uint8_t *out, size_t cap);

/* The §4.1 console-link and bonding axes, moved by the BLE callbacks. The
 * console-link `which` is §3.3's (0 disconnected, 1 connected, 2 re-subscribed)
 * and `bond` is §3.2's. */
void control_notify_console_link(uint8_t which, uint16_t reason);
void control_notify_bond(uint8_t bond);

/*
 * The console's own connection interval, in 1.25 ms units, straight from the BLE
 * layer (#35, §12.2 validation 3). It is logged at INFO on the `control` tag and
 * nowhere else, for two reasons that make it a control-plane fact rather than a
 * `ble_gap` one: `main.c` raises only `control` above `WARN`, and validation 3
 * compares the macro's input-to-input latency *against this link*, so both halves
 * of the comparison have to be in the same INFO capture.
 */
void control_notify_console_interval(int conn_itvl);

#endif /* CONFIG_PROTOCOL_LAYER_CONTROL */

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_PARSER_H */
