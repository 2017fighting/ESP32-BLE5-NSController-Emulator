/*
 * The console's NFC traffic recorder (spec §6.6, §12.2 validations 4/5; issue
 * #36). Portable C; the host suite links this file directly. See
 * `nfc_trace.h` for why the reads are never logged inline and for the line
 * grammar.
 */

#include "protocol/control/nfc_trace.h"

#include <stdio.h>
#include <string.h>

#include "protocol/control/control_protocol.h"

static uint16_t trace_rd_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint16_t trace_crc(const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0) {
        return 0;
    }
    /* §2.2's call, the same one every control frame carries, so the host's
     * reconstruction checks against a CRC it already knows how to compute. */
    return control_crc16_update(0xffffu, data, len);
}

nfc_trace_slot_t nfc_trace_slot(uint8_t sub)
{
    switch (sub) {
    case 0x03:
        return NFC_TRACE_SUB_03;
    case 0x04:
        return NFC_TRACE_SUB_04;
    case 0x05:
        return NFC_TRACE_SUB_05;
    case 0x06:
        return NFC_TRACE_SUB_06;
    case 0x14:
        return NFC_TRACE_SUB_14;
    case 0x15:
        return NFC_TRACE_SUB_15;
    default:
        return NFC_TRACE_SUB_COUNT;
    }
}

void nfc_trace_init(nfc_trace_t *t)
{
    if (t == NULL) {
        return;
    }
    memset(t, 0, sizeof(*t));
}

/* The coalescing key: a subcommand plus whichever of (offset, status) it has.
 * `0x05` keyed on the status byte means the two answers a scan produces —
 * "no tag" while waiting, "detected" once placed — are two entries, not one
 * entry whose meaning changed halfway. */
static bool trace_same_key(const nfc_trace_event_t *e, uint8_t sub, uint16_t off,
                           uint8_t status)
{
    return e->sub == sub && e->off == off && e->status == status;
}

static nfc_trace_event_t *trace_at(nfc_trace_t *t, size_t i)
{
    return &t->ring[(t->head + i) % NFC_TRACE_RING];
}

static nfc_trace_event_t *trace_find(nfc_trace_t *t, uint8_t sub, uint16_t off, uint8_t status)
{
    for (size_t i = 0; i < t->used; i++) {
        nfc_trace_event_t *e = trace_at(t, i);
        if (trace_same_key(e, sub, off, status)) {
            return e;
        }
    }
    return NULL;
}

static nfc_trace_event_t *trace_open(nfc_trace_t *t, uint8_t sub, uint16_t off, uint8_t status,
                                     uint32_t t_ms)
{
    nfc_trace_event_t *e = trace_find(t, sub, off, status);
    if (e != NULL) {
        /* A repeat never rewrites the first occurrence's timestamp: the entry
         * says when the console first asked, `reps` says it kept asking. */
        e->reps++;
        t->reps++;
        return e;
    }
    if (t->used >= NFC_TRACE_RING) {
        t->drops++;
        return NULL;
    }
    e = trace_at(t, t->used);
    t->used++;
    memset(e, 0, sizeof(*e));
    e->sub = sub;
    e->off = off;
    e->status = status;
    e->t_ms = t_ms;
    return e;
}

void nfc_trace_feed(nfc_trace_t *t, uint8_t sub, const uint8_t *payload, size_t len,
                    const uint8_t *rsp, size_t rsp_len, uint32_t t_ms)
{
    if (t == NULL || nfc_trace_slot(sub) == NFC_TRACE_SUB_COUNT) {
        return;
    }
    t->counts[nfc_trace_slot(sub)]++;
    t->total++;
    t->dirty = true;

    switch (sub) {
    case 0x03: {
        nfc_trace_event_t *e = trace_open(t, sub, 0, 0, t_ms);
        if (e == NULL) {
            return;
        }
        e->want = (uint16_t)len;
        size_t n = len < NFC_TRACE_CFG_MAX ? len : NFC_TRACE_CFG_MAX;
        if (payload != NULL && n > 0) {
            memcpy(e->cfg, payload, n);
        }
        e->cfg_len = (uint8_t)n;
        return;
    }
    case 0x04:
        trace_open(t, sub, 0, 0, t_ms);
        return;
    case 0x05: {
        uint8_t status = (rsp != NULL && rsp_len > 0) ? rsp[0] : 0xffu;
        nfc_trace_event_t *e = trace_open(t, sub, 0, status, t_ms);
        if (e == NULL) {
            return;
        }
        e->n = (uint16_t)rsp_len;
        e->crc = trace_crc(rsp, rsp_len);
        return;
    }    case 0x06: {
        nfc_trace_event_t *e = trace_open(t, sub, 0, 0, t_ms);
        if (e == NULL) {
            return;
        }
        e->want = (uint16_t)len;
        return;
    }
    case 0x14: {
        if (payload == NULL || len < 4) {
            trace_open(t, sub, 0xffffu, 0, t_ms);
            return;
        }
        uint16_t off = trace_rd_le16(payload);
        uint16_t want = trace_rd_le16(&payload[2]);
        nfc_trace_event_t *e = trace_open(t, sub, off, 0, t_ms);
        if (e == NULL) {
            return;
        }
        e->want = want;
        size_t avail = len - 4u;
        size_t n = avail < NFC_TRACE_DATA_MAX ? avail : NFC_TRACE_DATA_MAX;
        memcpy(e->data, &payload[4], n);
        e->data_len = (uint16_t)n;
        e->got = (uint16_t)n;
        e->crc = trace_crc(e->data, n);
        return;
    }
    case 0x15: {
        uint16_t off = (payload != NULL && len >= 2) ? trace_rd_le16(payload) : 0xffffu;
        nfc_trace_event_t *e = trace_open(t, sub, off, 0, t_ms);
        if (e == NULL) {
            return;
        }
        e->n = (uint16_t)rsp_len;
        e->crc = trace_crc(rsp, rsp_len);
        return;
    }
    default:
        return;
    }
}

/* ─────────────────────────────────────────────────────────── the text lines */

static size_t trace_hex(const uint8_t *data, size_t len, char *out, size_t cap)
{
    size_t written = 0;
    for (size_t i = 0; i < len; i++) {
        if (cap < 3) {
            break;
        }
        int n = snprintf(&out[written], cap, "%02x", data[i]);
        if (n < 0) {
            break;
        }
        written += (size_t)n;
        cap -= (size_t)n;
    }
    return written;
}

/* Appends ` key=value`; returns the new length, or the original length when it
 * did not fit. */
static size_t trace_kv_u(char *out, size_t len, size_t cap, const char *key, unsigned value)
{
    int n = snprintf(out + len, cap - len, " %s=%u", key, value);
    return (n < 0 || (size_t)n >= cap - len) ? len : len + (size_t)n;
}

/* The same, for a four-hex-digit value — the wire offsets and the CRCs. */
static size_t trace_kv_x4(char *out, size_t len, size_t cap, const char *key, unsigned value)
{
    int n = snprintf(out + len, cap - len, " %s=%04x", key, value);
    return (n < 0 || (size_t)n >= cap - len) ? len : len + (size_t)n;
}

static size_t trace_event_line(const nfc_trace_event_t *e, char *out, size_t cap)
{
    int n = snprintf(out, cap, "console nfc: t=%lu sub=%02x", (unsigned long)e->t_ms,
                     (unsigned)e->sub);
    size_t len = (n < 0 || (size_t)n >= cap) ? 0 : (size_t)n;
    if (len == 0) {
        return 0;
    }
    switch (e->sub) {
    case 0x03:
        len = trace_kv_u(out, len, cap, "len", e->want);
        if (cap - len > 5 + (size_t)e->cfg_len * 2u) {
            n = snprintf(out + len, cap - len, " cfg=");
            if (n > 0) {
                len += (size_t)n + trace_hex(e->cfg, e->cfg_len, out + len + (size_t)n,
                                             cap - len - (size_t)n);
            }
        }
        break;
    case 0x04:
        break;
    case 0x05:
        n = snprintf(out + len, cap - len, " status=%02x n=%u crc=%04x reps=%u",
                     (unsigned)e->status, (unsigned)e->n, (unsigned)e->crc, (unsigned)e->reps);
        return (n < 0 || (size_t)n >= cap - len) ? len : len + (size_t)n;
    case 0x06:
        len = trace_kv_u(out, len, cap, "len", e->want);
        break;
    case 0x14:
        len = trace_kv_x4(out, len, cap, "off", e->off);
        len = trace_kv_u(out, len, cap, "want", e->want);
        len = trace_kv_u(out, len, cap, "got", e->got);
        len = trace_kv_x4(out, len, cap, "crc", e->crc);
        if (cap - len > 6 + (size_t)e->data_len * 2u) {
            n = snprintf(out + len, cap - len, " data=");
            if (n > 0) {
                len += (size_t)n + trace_hex(e->data, e->data_len, out + len + (size_t)n,
                                             cap - len - (size_t)n);
            }
        }
        break;
    case 0x15:
        len = trace_kv_x4(out, len, cap, "off", e->off);
        len = trace_kv_u(out, len, cap, "n", e->n);
        len = trace_kv_x4(out, len, cap, "crc", e->crc);
        len = trace_kv_u(out, len, cap, "reps", e->reps);
        break;
    default:
        break;
    }
    return len;
}

size_t nfc_trace_format(nfc_trace_t *t, char *out, size_t cap)
{
    if (t == NULL || out == NULL || cap == 0) {
        return 0;
    }

    if (t->used > 0) {
        const nfc_trace_event_t e = t->ring[t->head];
        t->head = (t->head + 1u) % NFC_TRACE_RING;
        t->used--;
        return trace_event_line(&e, out, cap);
    }

    if (!t->dirty) {
        return 0;
    }

    int n = snprintf(out, cap,
                     "console nfc: scan cmds=%lu [03=%lu 04=%lu 05=%lu 06=%lu 14=%lu 15=%lu] "
                     "reps=%lu drops=%u",
                     (unsigned long)t->total,
                     (unsigned long)t->counts[NFC_TRACE_SUB_03],
                     (unsigned long)t->counts[NFC_TRACE_SUB_04],
                     (unsigned long)t->counts[NFC_TRACE_SUB_05],
                     (unsigned long)t->counts[NFC_TRACE_SUB_06],
                     (unsigned long)t->counts[NFC_TRACE_SUB_14],
                     (unsigned long)t->counts[NFC_TRACE_SUB_15],
                     (unsigned long)t->reps, t->drops);

    /* The drain's end: the summary was the last line, so the trace resets —
     * the next scan starts a clean page. */
    nfc_trace_init(t);
    if (n < 0) {
        return 0;
    }
    return (size_t)n >= cap ? cap - 1u : (size_t)n;
}
