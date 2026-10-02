/*
 * Host-side assertion of the plan golden fixture (§5.7, G-8, G-13).
 *
 * This is the firmware half of the drift guard: it compiles the frozen bytes of
 * `fixtures/plan/correction.plan.hex` through `main/include/protocol/plan.h`
 * and demands the header fields, the record geometry, the magic bytes and the
 * full SHA-256 back. The `static_assert`s in that header are the other half —
 * a struct drift fails to compile here rather than replaying as garbage on the
 * bench.
 *
 * Build (no ESP-IDF, no board):
 *   cc -std=c11 -Wall -Wextra -Werror -Imain/include \
 *      -o test_plan_fixture test/host/test_plan_fixture.c
 * Run:
 *   ./test_plan_fixture [fixture-dir]        # verifies, exit 0/1
 *   ./test_plan_fixture [fixture-dir] --selftest   # proves a flipped byte fails
 */

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "protocol/plan.h"

/* Suppressed only while the selftest deliberately breaks the plan. */
static int g_quiet;
#define REPORT(...)                                \
    do {                                           \
        if (!g_quiet) fprintf(stderr, __VA_ARGS__); \
    } while (0)

/* ------------------------------------------------------------------ SHA-256 */

typedef struct {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t data[64];
    size_t datalen;
} sha256_ctx;

static const uint32_t SHA256_K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

#define ROTR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_transform(sha256_ctx *ctx, const uint8_t block[64]) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)block[i * 4] << 24) | ((uint32_t)block[i * 4 + 1] << 16) |
               ((uint32_t)block[i * 4 + 2] << 8) | (uint32_t)block[i * 4 + 3];
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROTR(w[i - 15], 7) ^ ROTR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROTR(w[i - 2], 17) ^ ROTR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2], d = ctx->state[3];
    uint32_t e = ctx->state[4], f = ctx->state[5], g = ctx->state[6], h = ctx->state[7];
    for (int i = 0; i < 64; i++) {
        uint32_t s1 = ROTR(e, 6) ^ ROTR(e, 11) ^ ROTR(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + s1 + ch + SHA256_K[i] + w[i];
        uint32_t s0 = ROTR(a, 2) ^ ROTR(a, 13) ^ ROTR(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = s0 + maj;
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    ctx->state[0] += a;
    ctx->state[1] += b;
    ctx->state[2] += c;
    ctx->state[3] += d;
    ctx->state[4] += e;
    ctx->state[5] += f;
    ctx->state[6] += g;
    ctx->state[7] += h;
}

static void sha256_init(sha256_ctx *ctx) {
    ctx->datalen = 0;
    ctx->bitlen = 0;
    ctx->state[0] = 0x6a09e667;
    ctx->state[1] = 0xbb67ae85;
    ctx->state[2] = 0x3c6ef372;
    ctx->state[3] = 0xa54ff53a;
    ctx->state[4] = 0x510e527f;
    ctx->state[5] = 0x9b05688c;
    ctx->state[6] = 0x1f83d9ab;
    ctx->state[7] = 0x5be0cd19;
}

static void sha256_update(sha256_ctx *ctx, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        ctx->data[ctx->datalen++] = data[i];
        if (ctx->datalen == 64) {
            sha256_transform(ctx, ctx->data);
            ctx->bitlen += 512;
            ctx->datalen = 0;
        }
    }
}

static void sha256_final(sha256_ctx *ctx, uint8_t out[32]) {
    size_t i = ctx->datalen;
    ctx->data[i++] = 0x80;
    if (i > 56) {
        while (i < 64) {
            ctx->data[i++] = 0x00;
        }
        sha256_transform(ctx, ctx->data);
        i = 0;
    }
    while (i < 56) {
        ctx->data[i++] = 0x00;
    }
    ctx->bitlen += (uint64_t)ctx->datalen * 8;
    for (int b = 0; b < 8; b++) {
        ctx->data[63 - b] = (uint8_t)(ctx->bitlen >> (8 * b));
    }
    sha256_transform(ctx, ctx->data);
    for (i = 0; i < 8; i++) {
        out[i * 4] = (uint8_t)(ctx->state[i] >> 24);
        out[i * 4 + 1] = (uint8_t)(ctx->state[i] >> 16);
        out[i * 4 + 2] = (uint8_t)(ctx->state[i] >> 8);
        out[i * 4 + 3] = (uint8_t)(ctx->state[i]);
    }
}

static void sha256_hex(const uint8_t *data, size_t len, char out[65]) {
    uint8_t digest[32];
    sha256_ctx ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, data, len);
    sha256_final(&ctx, digest);
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        out[i * 2] = hex[digest[i] >> 4];
        out[i * 2 + 1] = hex[digest[i] & 0x0F];
    }
    out[64] = '\0';
}

/* --------------------------------------------------------------- file input */

static int read_file(const char *path, char **out, size_t *out_len) {
    FILE *fp = fopen(path, "rb");
    if (fp == NULL) {
        fprintf(stderr, "cannot open %s: %s\n", path, strerror(errno));
        return -1;
    }
    if (fseek(fp, 0, SEEK_END) != 0) {
        fclose(fp);
        return -1;
    }
    long size = ftell(fp);
    if (size < 0 || fseek(fp, 0, SEEK_SET) != 0) {
        fclose(fp);
        return -1;
    }
    char *buf = malloc((size_t)size + 1);
    if (buf == NULL) {
        fclose(fp);
        return -1;
    }
    size_t got = fread(buf, 1, (size_t)size, fp);
    fclose(fp);
    buf[got] = '\0';
    *out = buf;
    *out_len = got;
    return 0;
}

static int hex_nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Whitespace is ignored, so the hex file may be wrapped. */
static int parse_hex(const char *text, uint8_t **out, size_t *out_len) {
    size_t capacity = strlen(text) / 2 + 1;
    uint8_t *bytes = malloc(capacity);
    if (bytes == NULL) return -1;
    size_t count = 0;
    int high = -1;
    for (const char *p = text; *p != '\0'; p++) {
        if (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t') continue;
        int value = hex_nibble(*p);
        if (value < 0) {
            fprintf(stderr, "invalid hex character %02x\n", (unsigned char)*p);
            free(bytes);
            return -1;
        }
        if (high < 0) {
            high = value;
        } else {
            bytes[count++] = (uint8_t)((high << 4) | value);
            high = -1;
        }
    }
    if (high >= 0) {
        fprintf(stderr, "hex file has an odd number of digits\n");
        free(bytes);
        return -1;
    }
    *out = bytes;
    *out_len = count;
    return 0;
}

/* ------------------------------------------------------------------- checks */

/* Returns 0 when the plan satisfies the frozen contract, -1 otherwise. */
static int verify_plan(const uint8_t *plan, size_t len, const char *expected_sha_hex) {
    int failures = 0;

    if (len < PLAN_HEADER_SIZE) {
        REPORT("plan is %zu bytes, shorter than the %u-byte header\n", len,
                (unsigned)PLAN_HEADER_SIZE);
        return -1;
    }

    plan_header_t header;
    memcpy(&header, plan, sizeof(header));

    if (header.magic != PLAN_MAGIC) {
        REPORT("magic is 0x%08X, expected 0x%08X\n", (unsigned)header.magic,
                (unsigned)PLAN_MAGIC);
        failures++;
    }
    if (!(plan[0] == 'N' && plan[1] == 'S' && plan[2] == 'P' && plan[3] == 'L')) {
        REPORT("magic bytes are not 'NSPL'\n");
        failures++;
    }
    if (header.format_version != PLAN_FORMAT_VERSION) {
        REPORT("format_version is %u, expected %u\n", (unsigned)header.format_version,
                (unsigned)PLAN_FORMAT_VERSION);
        failures++;
    }
    if (header.record_size != PLAN_RECORD_SIZE) {
        REPORT("record_size is %u, expected %u\n", (unsigned)header.record_size,
                (unsigned)PLAN_RECORD_SIZE);
        failures++;
    }

    uint64_t expected_len = (uint64_t)PLAN_HEADER_SIZE +
                            (uint64_t)PLAN_RECORD_SIZE * header.record_count;
    if (expected_len != len) {
        REPORT("payload is %zu bytes, expected %llu for %u records\n", len,
                (unsigned long long)expected_len, (unsigned)header.record_count);
        failures++;
    }

    /* Never read past the bytes we actually have, even on a corrupt header. */
    size_t available = (len - PLAN_HEADER_SIZE) / PLAN_RECORD_SIZE;
    uint64_t sum_hold = 0;
    for (size_t i = 0; i < available; i++) {
        plan_record_t record;
        memcpy(&record, plan + PLAN_HEADER_SIZE + i * PLAN_RECORD_SIZE, sizeof(record));
        if ((record.buttons[2] & 0xE0) != 0) {
            REPORT("record %zu sets reserved button bits: byte2=0x%02X\n", i,
                    (unsigned)record.buttons[2]);
            failures++;
        }
        sum_hold += record.hold_ms;
    }
    if (sum_hold != header.loop_ms) {
        REPORT("sum(hold_ms)=%llu, loop_ms=%u\n", (unsigned long long)sum_hold,
                (unsigned)header.loop_ms);
        failures++;
    }

    char actual_sha[65];
    sha256_hex(plan, len, actual_sha);
    if (strcmp(actual_sha, expected_sha_hex) != 0) {
        REPORT("sha256 is %s, expected %s\n", actual_sha, expected_sha_hex);
        failures++;
    } else {
        printf("sha256 %s\n", actual_sha);
    }

    if (failures > 0) {
        return -1;
    }
    printf("plan ok: %u records, loop_ms=%u, %zu bytes\n", (unsigned)header.record_count,
           (unsigned)header.loop_ms, len);
    return 0;
}

static char *join_path(const char *dir, const char *name) {
    size_t len = strlen(dir) + strlen(name) + 2;
    char *path = malloc(len);
    if (path == NULL) return NULL;
    snprintf(path, len, "%s/%s", dir, name);
    return path;
}

/* --------------------------------------------------------------------- main */

int main(int argc, char **argv) {
    const char *fixture_dir = "fixtures/plan";
    const char *mode = NULL;
    int flip_offset = -1;

    int argi = 1;
    if (argi < argc && strcmp(argv[argi], "--selftest") != 0 &&
        strcmp(argv[argi], "--flip") != 0) {
        fixture_dir = argv[argi++];
    }
    if (argi < argc) {
        mode = argv[argi++];
    }
    if (mode != NULL && strcmp(mode, "--flip") == 0) {
        if (argi >= argc) {
            fprintf(stderr, "--flip needs a byte offset\n");
            return 2;
        }
        flip_offset = atoi(argv[argi++]);
    } else if (mode != NULL && strcmp(mode, "--selftest") != 0) {
        fprintf(stderr, "unknown argument %s\n", mode);
        return 2;
    }

    char *hex_path = join_path(fixture_dir, "correction.plan.hex");
    char *sha_path = join_path(fixture_dir, "correction.sha256");
    if (hex_path == NULL || sha_path == NULL) {
        fprintf(stderr, "out of memory\n");
        return 2;
    }

    char *hex_text = NULL;
    size_t hex_len = 0;
    char *sha_text = NULL;
    size_t sha_len = 0;
    int rc = 2;
    uint8_t *plan = NULL;
    size_t plan_len = 0;

    if (read_file(hex_path, &hex_text, &hex_len) != 0) goto cleanup;
    if (read_file(sha_path, &sha_text, &sha_len) != 0) goto cleanup;

    /* Trim the digest file to its first token. */
    for (size_t i = 0; i < sha_len; i++) {
        if (sha_text[i] == '\n' || sha_text[i] == '\r' || sha_text[i] == ' ') {
            sha_text[i] = '\0';
            break;
        }
    }

    if (parse_hex(hex_text, &plan, &plan_len) != 0) goto cleanup;

    if (mode != NULL && strcmp(mode, "--selftest") == 0) {
        g_quiet = 1;
        const size_t offsets[] = {0, 4, PLAN_HEADER_SIZE, PLAN_HEADER_SIZE + 3,
                                  plan_len - 1};
        for (size_t i = 0; i < sizeof(offsets) / sizeof(offsets[0]); i++) {
            uint8_t *copy = malloc(plan_len);
            if (copy == NULL) goto cleanup;
            memcpy(copy, plan, plan_len);
            copy[offsets[i]] ^= 0x01;
            if (verify_plan(copy, plan_len, sha_text) == 0) {
                fprintf(stderr, "selftest FAILED: flipped byte at %zu was not caught\n",
                        offsets[i]);
                free(copy);
                goto cleanup;
            }
            free(copy);
        }
        g_quiet = 0;
        printf("selftest: %zu flipped bytes all detected\n",
               sizeof(offsets) / sizeof(offsets[0]));
        rc = 0;
        goto cleanup;
    }

    if (flip_offset >= 0) {
        if ((size_t)flip_offset >= plan_len) {
            fprintf(stderr, "--flip %d is outside the %zu-byte plan\n", flip_offset,
                    plan_len);
            goto cleanup;
        }
        plan[flip_offset] ^= 0x01;
        if (verify_plan(plan, plan_len, sha_text) == 0) {
            fprintf(stderr, "flip at %d was not detected\n", flip_offset);
            goto cleanup;
        }
        printf("flip at %d detected\n", flip_offset);
        rc = 0;
        goto cleanup;
    }

    rc = verify_plan(plan, plan_len, sha_text) == 0 ? 0 : 1;

cleanup:
    free(plan);
    free(hex_text);
    free(sha_text);
    free(hex_path);
    free(sha_path);
    return rc;
}
