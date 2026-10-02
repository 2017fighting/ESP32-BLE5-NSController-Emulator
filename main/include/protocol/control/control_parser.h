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

#ifdef __cplusplus
extern "C" {
#endif

#if defined(CONFIG_PROTOCOL_LAYER_CONTROL)

extern protocol_instance_t control_protocol_instance;

/* Generates this boot's `boot_id` (§2.6) and readies the decoder. Call once,
 * before the protocol task starts. */
void control_parser_init(void);

#endif /* CONFIG_PROTOCOL_LAYER_CONTROL */

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_PARSER_H */
