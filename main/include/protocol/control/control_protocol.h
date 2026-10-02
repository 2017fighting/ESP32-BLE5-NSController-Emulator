#ifndef PROTOCOL_CONTROL_PROTOCOL_H
#define PROTOCOL_CONTROL_PROTOCOL_H

/*
 * The CONTROL plane framing and dispatch (spec §2, chapter 7 step 3).
 *
 * This header and the .c files beside it are deliberately free of ESP-IDF
 * dependencies: the whole framing contract is exercised on the host by
 * `test/host/test_control_framing.c`, so a byte-layout regression fails in CI
 * rather than on the bench. The ESP-IDF glue is `control_parser.c` (the router
 * layer) and `control/control_link.c` (the shared TX lock and the log hook).
 *
 * Framing (§2.2): a frame on the wire is
 *
 *     0x00 · COBS(ver|type|verb|len(le16)|crc(le16) | payload) · 0x00
 *
 * — the leading delimiter is mandatory, the header is a fixed seven bytes, and
 * the CRC is CRC-16/CCITT-FALSE over header bytes 0-4 then the payload. The
 * `crc` field itself is skipped because a field cannot cover itself.
 */

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ wire size */

#define CONTROL_PROTO_VER 1u
#define CONTROL_MAX_FRAME 512u /* §2.2: decoded header + payload ceiling */
#define CONTROL_HEADER_SIZE 7u
#define CONTROL_MAX_PAYLOAD (CONTROL_MAX_FRAME - CONTROL_HEADER_SIZE)

/* Worst-case COBS of a max_frame block, plus the two mandatory delimiters.
 * NOTE: §7.4 budgets "max_frame for RX + TX", which undercounts the wire form;
 * the encoded form is larger than the decoded one. These are the real bounds. */
#define CONTROL_COBS_MAX (CONTROL_MAX_FRAME + CONTROL_MAX_FRAME / 254u + 1u)
#define CONTROL_WIRE_MAX (CONTROL_COBS_MAX + 2u)

/* §2.6 advertised capabilities. */
#define CONTROL_CHUNK_SIZE 256u
#define CONTROL_PLAN_CAPACITY_BYTES 65536u
#define CONTROL_PLAN_SLOTS 1u

/* §2.6 `features` bits. A bit is a promise the build can honour end to end, and
 * the physical halves of all three — the executor (#24), the tag server (#25)
 * and the applied-CONFIG boundary (#23) — are `control_effects_t` no-ops on this
 * build, so the honest advertisement is "nothing yet". #23-#25 flip the bits as
 * their effects land, one line each. */
#define CONTROL_FEATURES_NONE 0x0000u
#define CONTROL_FEATURE_MACRO 0x0001u
#define CONTROL_FEATURE_AMIIBO 0x0002u
#define CONTROL_FEATURE_CONFIG 0x0004u
#define CONTROL_FEATURES CONTROL_FEATURES_NONE

/* §2.6 `fw_version` is 4x u8 major.minor.patch.build. `build` is free for a
 * monotonic per-flash counter later. */
#define CONTROL_FW_MAJOR 0u
#define CONTROL_FW_MINOR 1u
#define CONTROL_FW_PATCH 0u
#define CONTROL_FW_BUILD 0u

/* --------------------------------------------------------------- frame types */

enum {
    CONTROL_TYPE_REQUEST = 1,
    CONTROL_TYPE_REPLY = 2,
    CONTROL_TYPE_EVENT = 3,
};

/* Verb wire values (§2.4). `ERROR` is a REPLY direction and is never a request. */
enum {
    CONTROL_VERB_HELLO = 1,
    CONTROL_VERB_LOAD_PLAN = 2,
    CONTROL_VERB_START = 3,
    CONTROL_VERB_STOP = 4,
    CONTROL_VERB_STATUS = 5,
    CONTROL_VERB_PLACE_AMIIBO = 6,
    CONTROL_VERB_UNPLACE_AMIIBO = 7,
    CONTROL_VERB_PAIR_UNPAIR = 8,
    CONTROL_VERB_CONFIG = 9,
    CONTROL_VERB_ERROR = 10,
};

/* The canonical, closed ERROR table (§2.5). 0 is STATUS-only `NONE`. */
enum {
    CONTROL_ERR_NONE = 0,
    CONTROL_ERR_VER_MISMATCH = 1,
    CONTROL_ERR_UNKNOWN_TYPE = 2,
    CONTROL_ERR_UNKNOWN_SUBCMD = 3,
    CONTROL_ERR_BAD_LENGTH = 4,
    CONTROL_ERR_BAD_STATE = 5,
    CONTROL_ERR_NO_PLAN = 6,
    CONTROL_ERR_ALREADY_RUNNING = 7,
    CONTROL_ERR_BAD_PLAN = 8,
    CONTROL_ERR_PLAN_TOO_LARGE = 9,
};

/* Status/event values used by the two implemented verbs (§3.2). */
enum {
    CONTROL_MODE_IDLE = 0,
    CONTROL_MODE_MACRO = 1,
    CONTROL_MODE_AMIIBO = 2,
};
enum {
    CONTROL_CONSOLE_ADVERTISING = 0,
    CONTROL_CONSOLE_CONNECTED = 1,
};
enum {
    CONTROL_BOND_UNPAIRED = 0,
    CONTROL_BOND_PAIRED = 1,
};
enum {
    CONTROL_PLAN_NONE = 0,
    CONTROL_PLAN_COMMITTED = 1,
};
enum {
    CONTROL_TAG_NONE = 0,
    CONTROL_TAG_PLACED = 1,
};
enum {
    CONTROL_POLLING_IDLE = 0,
    CONTROL_POLLING_POLLING = 1,
    CONTROL_POLLING_TAG_DETECTED = 2,
};
enum {
    CONTROL_STOP_NONE = 0,
    CONTROL_STOP_CONTAINER = 1,
    CONTROL_STOP_BOOT_LOCAL = 2,
};

/* ---------------------------------------------------------------------- COBS */

/* Returns the encoded length, or 0 when @p out_cap is too small. The delimiters
 * are added by the framer, never by COBS itself (§2.2). */
size_t control_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap);

enum {
    CONTROL_COBS_OK = 0,
    CONTROL_COBS_INVALID = 1,  /* malformed block */
    CONTROL_COBS_OVERFLOW = 2, /* decoded past out_cap; *out_len = bytes written */
};

/* Decodes one COBS block (no delimiters). On OVERFLOW the caller still has the
 * first *out_len bytes, which is what lets the decoder name `BAD_LENGTH` for an
 * over-long frame (§2.10). */
int control_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap,
                        size_t *out_len);

/* ----------------------------------------------------------------------- CRC */

#define CONTROL_CRC_INIT 0xFFFFu

/* CRC-16/CCITT-FALSE: poly 0x1021, init 0xffff, no reflection, xorout 0.
 * Numerically identical to the device's §2.2 call, `~esp_rom_crc16_be(~0xffff, …)`;
 * the check value over "123456789" is 0x29B1. */
uint16_t control_crc16_update(uint16_t crc, const uint8_t *data, size_t len);

/* The two-segment form the protocol uses: header bytes 0-4, then the payload. */
uint16_t control_crc16_frame(const uint8_t *header5, const uint8_t *payload, size_t len);

/* --------------------------------------------------------------------- frames */

typedef struct {
    uint8_t ver;
    uint8_t type;
    uint8_t verb;
    uint16_t len;
    const uint8_t *payload; /* into the decoder's frame buffer */
} control_frame_t;

typedef enum {
    CONTROL_DEC_IN_PROGRESS = 0, /* consumed a byte; nothing (or silent noise) */
    CONTROL_DEC_FRAME,           /* dec->frame is a trusted frame */
    CONTROL_DEC_REJECT,          /* trusted enough to name an ERROR (§2.5) */
} control_dec_result_t;

typedef struct {
    uint8_t block[CONTROL_COBS_MAX];
    size_t block_len;
    /* The leading delimiter is mandatory (§2.2): bytes before the first 0x00
     * are ignored, so a partial frame left over from a previous attach cannot
     * be mistaken for one. After that, every 0x00 ends a segment and starts the
     * next — which is what makes "advance to the next 0x00" recover after a
     * corrupted block instead of swallowing the frame behind it. */
    bool awaiting_first;
    bool overflow;

    uint8_t frame[CONTROL_COBS_MAX]; /* decoded block, bounded by max_frame */
    control_frame_t last;

    uint8_t reject_code;
    uint32_t reject_detail;

    /* Diagnostics: blocks seen and blocks discarded without an answer. */
    uint32_t n_blocks;
    uint32_t n_drops;
} control_decoder_t;

void control_decoder_reset(control_decoder_t *dec);

/*
 * Feed one byte of the control-link stream. A log line arrives as a COBS block
 * that fails to decode or fails the CRC and yields IN_PROGRESS — never an
 * ERROR and never a reset (§2.2, §2.8).
 */
control_dec_result_t control_decoder_feed(control_decoder_t *dec, uint8_t byte);

/* Feedback for the host test / diagnostics. */
uint32_t control_decoder_blocks(const control_decoder_t *dec);
uint32_t control_decoder_silent_drops(const control_decoder_t *dec);

/* Encodes a frame to its wire form: 0x00 · COBS(hdr||payload) · 0x00.
 * Returns the wire length, or 0 when it cannot fit in @p out_cap. */
size_t control_encode(uint8_t type, uint8_t verb, const uint8_t *payload, size_t len,
                      uint8_t *out, size_t out_cap);

size_t control_encode_error(uint8_t code, uint32_t detail, uint8_t *out, size_t out_cap);

/* ------------------------------------------------------- HELLO / STATUS (§2.6, §3.2) */

typedef struct __attribute__((packed)) {
    uint8_t proto_ver;            /* 0x00 */
    uint8_t fw_version[4];        /* 0x01 major, minor, patch, build */
    uint32_t boot_id;             /* 0x05 */
    uint16_t max_frame;           /* 0x09 */
    uint16_t chunk_size;          /* 0x0b */
    uint32_t plan_capacity_bytes; /* 0x0d */
    uint8_t plan_slots;           /* 0x11 */
    uint16_t features;            /* 0x12 */
} control_hello_t;                /* 20 bytes */

typedef struct __attribute__((packed)) {
    uint8_t console_link;        /* 0x00 ADVERTISING / CONNECTED */
    uint8_t bond;                /* 0x01 UNPAIRED / PAIRED */
    uint8_t mode;                /* 0x02 IDLE / MACRO / AMIIBO */
    uint8_t plan_state;          /* 0x03 NONE / COMMITTED */
    uint8_t plan_hash[16];       /* 0x04 */
    uint16_t plan_frame_count;   /* 0x14 */
    uint16_t current_frame;      /* 0x16 */
    uint32_t loop_count;         /* 0x18 */
    uint8_t tag_state;           /* 0x1c NONE / PLACED */
    uint8_t tag_identity[7];     /* 0x1d */
    uint8_t console_polling;     /* 0x24 */
    uint8_t last_error_code;     /* 0x25 */
    uint32_t last_error_detail;  /* 0x26 */
    uint8_t last_stop_reason;    /* 0x2a */
    uint32_t uptime_ms;          /* 0x2b */
} control_status_t;              /* 47 bytes */

_Static_assert(sizeof(control_hello_t) == 20, "HELLO payload must be 20 bytes");
_Static_assert(sizeof(control_status_t) == 47, "STATUS payload must be 47 bytes");

/* Serialise to the wire payload, little-endian, explicitly field by field so a
 * big-endian host would still produce the same bytes. Returns bytes written, or
 * 0 when @p cap is too small. */
size_t control_hello_payload(const control_hello_t *hello, uint8_t *out, size_t cap);
size_t control_status_payload(const control_status_t *status, uint8_t *out, size_t cap);

/* A zeroed status with the boot defaults of §4.8: IDLE, nothing staged. */
void control_status_default(control_status_t *status);

/* Fills a HELLO capability record with the §2.6 limits and CONTROL_FEATURES. */
void control_hello_default(control_hello_t *hello, uint32_t boot_id);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_CONTROL_PROTOCOL_H */
