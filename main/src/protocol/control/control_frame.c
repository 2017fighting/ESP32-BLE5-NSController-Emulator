/*
 * CONTROL framing: COBS, the two-segment CRC-16/CCITT-FALSE, the streaming
 * decoder and the frame encoder. Portable C; the host test links this file
 * directly (spec §2.2, §2.8, §2.10).
 */

#include "protocol/control/control_protocol.h"

#include <string.h>

/* ---------------------------------------------------------------------- COBS */

size_t control_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap)
{
    size_t read_index = 0;
    size_t write_index = 1;
    size_t code_index = 0;
    uint8_t code = 1;

    if (out == NULL || in == NULL || out_cap < 1) {
        return 0;
    }

    while (read_index < len) {
        if (in[read_index] == 0x00) {
            if (code_index >= out_cap) {
                return 0;
            }
            out[code_index] = code;
            code = 1;
            if (write_index >= out_cap) {
                return 0;
            }
            code_index = write_index++;
            read_index++;
        } else {
            if (write_index >= out_cap) {
                return 0;
            }
            out[write_index++] = in[read_index++];
            if (++code == 0xFF) {
                if (code_index >= out_cap) {
                    return 0;
                }
                out[code_index] = code;
                code = 1;
                if (write_index >= out_cap) {
                    return 0;
                }
                code_index = write_index++;
            }
        }
    }

    if (code_index >= out_cap) {
        return 0;
    }
    out[code_index] = code;
    return write_index;
}

int control_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap,
                        size_t *out_len)
{
    size_t read_index = 0;
    size_t write_index = 0;

    *out_len = 0;

    while (read_index < len) {
        uint8_t code = in[read_index++];
        if (code == 0x00) {
            return CONTROL_COBS_INVALID; /* a COBS block never contains 0x00 */
        }
        for (uint8_t i = 1; i < code; i++) {
            if (read_index >= len) {
                return CONTROL_COBS_INVALID;
            }
            if (write_index >= out_cap) {
                *out_len = write_index;
                return CONTROL_COBS_OVERFLOW;
            }
            out[write_index++] = in[read_index++];
        }
        if (code < 0xFF && read_index < len) {
            if (write_index >= out_cap) {
                *out_len = write_index;
                return CONTROL_COBS_OVERFLOW;
            }
            out[write_index++] = 0x00;
        }
    }

    *out_len = write_index;
    return CONTROL_COBS_OK;
}

/* ----------------------------------------------------------------------- CRC */

uint16_t control_crc16_update(uint16_t crc, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 0x8000u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}

uint16_t control_crc16_frame(const uint8_t *header5, const uint8_t *payload, size_t len)
{
    uint16_t crc = control_crc16_update(CONTROL_CRC_INIT, header5, 5);
    if (payload != NULL && len > 0) {
        crc = control_crc16_update(crc, payload, len);
    }
    return crc;
}

/* ------------------------------------------------------------------- decoder */

void control_decoder_reset(control_decoder_t *dec)
{
    if (dec == NULL) {
        return;
    }
    memset(dec, 0, sizeof(*dec));
    dec->awaiting_first = true;
}

uint32_t control_decoder_blocks(const control_decoder_t *dec)
{
    return dec->n_blocks;
}

uint32_t control_decoder_silent_drops(const control_decoder_t *dec)
{
    return dec->n_drops;
}

static control_dec_result_t reject(control_decoder_t *dec, uint8_t code, uint32_t detail)
{
    dec->reject_code = code;
    dec->reject_detail = detail;
    return CONTROL_DEC_REJECT;
}

/*
 * A decoded block is a frame only when it is *trustworthy*, and the CRC is what
 * makes it so. §2.10 lists the version check before the CRC; this decoder
 * verifies the CRC first and then interprets, because §2.8 requires an
 * untrusted frame to produce no ERROR at all — and a log line that happens to
 * decode to seven plausible bytes is untrusted whatever its version byte says.
 * Without that ordering a log flood would draw VER_MISMATCH/BAD_LENGTH replies
 * for pure noise. The 7+len geometry is checked first because the CRC's length
 * is defined by it.
 */
static control_dec_result_t decode_block(control_decoder_t *dec)
{
    if (dec->block_len == 0) {
        return CONTROL_DEC_IN_PROGRESS; /* two delimiters in a row (§2.2) */
    }

    dec->n_blocks++;

    size_t n = 0;
    int rc = control_cobs_decode(dec->block, dec->block_len, dec->frame, CONTROL_MAX_FRAME, &n);
    if (rc == CONTROL_COBS_OVERFLOW) {
        /* Decoded past max_frame. Its CRC cannot be computed over the whole
         * 5 + len bytes, so the block is untrusted and silent, exactly as §2.8
         * requires of a block that "decodes past max_frame": a >512-byte noise
         * run must not be able to forge a `len` and draw a reply. This is why
         * §2.2/§2.10 no longer name BAD_LENGTH for an over-long frame — the
         * receiver never read the payload the CRC covers. */
        dec->n_drops++;
        return CONTROL_DEC_IN_PROGRESS;
    }
    if (rc != CONTROL_COBS_OK) {
        dec->n_drops++;
        return CONTROL_DEC_IN_PROGRESS; /* COBS failure: silent (§2.8) */
    }
    if (n < CONTROL_HEADER_SIZE) {
        dec->n_drops++;
        return CONTROL_DEC_IN_PROGRESS; /* too short to hold a header */
    }

    const uint8_t *f = dec->frame;
    uint8_t ver = f[0];
    uint8_t type = f[1];
    uint8_t verb = f[2];
    uint16_t len = control_rd_le16(&f[3]);
    uint16_t crc = control_rd_le16(&f[5]);

    if ((size_t)CONTROL_HEADER_SIZE + len != n || (size_t)CONTROL_HEADER_SIZE + len > CONTROL_MAX_FRAME) {
        dec->n_drops++;
        return CONTROL_DEC_IN_PROGRESS; /* geometry is not a real frame */
    }

    if (control_crc16_frame(f, f + CONTROL_HEADER_SIZE, len) != crc) {
        dec->n_drops++;
        return CONTROL_DEC_IN_PROGRESS; /* CRC failure: silent (§2.2, §2.8) */
    }

    /* From here the frame is trusted; an illegality is a typed ERROR. */
    if (ver != CONTROL_PROTO_VER) {
        return reject(dec, CONTROL_ERR_VER_MISMATCH, CONTROL_PROTO_VER);
    }
    if (type != CONTROL_TYPE_REQUEST && type != CONTROL_TYPE_REPLY && type != CONTROL_TYPE_EVENT) {
        return reject(dec, CONTROL_ERR_UNKNOWN_TYPE, type);
    }

    dec->last.ver = ver;
    dec->last.type = type;
    dec->last.verb = verb;
    dec->last.len = len;
    dec->last.payload = f + CONTROL_HEADER_SIZE;

    /* A REPLY or EVENT arriving at the device is well-framed but not a request;
     * §2.8 discards it silently rather than calling it UNKNOWN_TYPE. */
    if (type != CONTROL_TYPE_REQUEST) {
        return CONTROL_DEC_IN_PROGRESS;
    }

    if (verb < CONTROL_VERB_HELLO || verb > CONTROL_VERB_ERROR) {
        return reject(dec, CONTROL_ERR_UNKNOWN_SUBCMD, verb);
    }

    return CONTROL_DEC_FRAME;
}

control_dec_result_t control_decoder_feed(control_decoder_t *dec, uint8_t byte)
{
    if (dec == NULL) {
        return CONTROL_DEC_IN_PROGRESS;
    }

    if (dec->awaiting_first) {
        /* The leading delimiter is mandatory: bytes before it are noise. */
        if (byte == 0x00) {
            dec->awaiting_first = false;
            dec->block_len = 0;
            dec->overflow = false;
        }
        return CONTROL_DEC_IN_PROGRESS;
    }

    if (byte != 0x00) {
        if (dec->block_len < sizeof(dec->block)) {
            dec->block[dec->block_len++] = byte;
        } else {
            dec->overflow = true; /* unreadable; drain to the next delimiter */
        }
        return CONTROL_DEC_IN_PROGRESS;
    }

    /* A delimiter ends the segment and immediately starts the next one. */
    control_dec_result_t result = CONTROL_DEC_IN_PROGRESS;
    if (dec->overflow) {
        dec->n_drops++;
    } else {
        result = decode_block(dec);
    }
    dec->block_len = 0;
    dec->overflow = false;
    return result;
}

/* ------------------------------------------------------------------- encoder */

size_t control_encode(uint8_t type, uint8_t verb, const uint8_t *payload, size_t len,
                      uint8_t *out, size_t out_cap)
{
    if (out == NULL || out_cap < 2 || len > CONTROL_MAX_PAYLOAD) {
        return 0;
    }

    uint8_t scratch[CONTROL_MAX_FRAME];
    scratch[0] = CONTROL_PROTO_VER;
    scratch[1] = type;
    scratch[2] = verb;
    scratch[3] = (uint8_t)(len & 0xFFu);
    scratch[4] = (uint8_t)((len >> 8) & 0xFFu);

    uint16_t crc = control_crc16_frame(scratch, payload, len);
    scratch[5] = (uint8_t)(crc & 0xFFu);
    scratch[6] = (uint8_t)(crc >> 8);

    if (len > 0 && payload != NULL) {
        memcpy(&scratch[CONTROL_HEADER_SIZE], payload, len);
    }

    out[0] = 0x00;
    size_t enc = control_cobs_encode(scratch, CONTROL_HEADER_SIZE + len, &out[1], out_cap - 1);
    if (enc == 0 || enc + 2 > out_cap) {
        return 0;
    }
    out[enc + 1] = 0x00;
    return enc + 2;
}

size_t control_encode_error(uint8_t code, uint32_t detail, uint8_t *out, size_t out_cap)
{
    uint8_t payload[5];
    payload[0] = code;
    payload[1] = (uint8_t)(detail & 0xFFu);
    payload[2] = (uint8_t)((detail >> 8) & 0xFFu);
    payload[3] = (uint8_t)((detail >> 16) & 0xFFu);
    payload[4] = (uint8_t)((detail >> 24) & 0xFFu);
    return control_encode(CONTROL_TYPE_REPLY, CONTROL_VERB_ERROR, payload, sizeof(payload), out,
                          out_cap);
}
