# Research: The 0x01/0x0C Response Asymmetry (G-14 Investigation)

**Scope:** whether the `0x91`→`0x01` rewrite in `main/src/ns2_codec.c` is handler-specific drift or protocol framing, and whether known gap G-14 exists at all.
**Date:** 2026-10-02. **Wayfinder ticket:** #26. **Result:** G-14 is a phantom; the spec's §7.3 step 8 statement about `0x02/0x04` is false.
**Sourcing:** in-repo code cites `path:line`; corpus cites `switch2_controller_research` by pin.


**Corpus Pin:** `switch2_controller_research` at `a3306b473acff0d6844fb1e288883a3940df0baf` (`ndeadly/switch2_controller_research@a3306b473acff0d6844fb1e288883a3940df0baf`, not available locally; open at https://github.com/ndeadly/switch2_controller_research/blob/a3306b473acff0d6844fb1e288883a3940df0baf/).

---

## Summary

The premise of Known Gap G-14 (recorded in `docs/spec/12-handoff.md:86` and `docs/spec/07-firmware-architecture.md:86-90`)—that response byte 1 is rewritten from `0x91` to `0x01` for `0x01/0x0C` but left as `0x91` for calibration read `0x02/0x04`—is a **false premise arising from a misreading of the codebase**. In `main/src/ns2_codec.c:641-643`, the rewrite `0x91 -> 0x01` is executed centrally inside `cmd_process()` for **every command** dispatched over GATT handle `0x0016`, replacing the inbound Host->Device Direction byte (`0x91`) with the outbound Device->Host Direction byte (`0x01`) as mandated by `switch2_controller_research/commands.md:20`. Both `0x01/0x0C` (NFC probe) and `0x02/0x04` (flash calibration read) are rewritten identically to `0x01`, which the console accepts across 140/140 observed calibration transactions; no asymmetry exists in code or in the protocol, and G-14 is a phantom defect that can be retired.

---

## Findings

### 1. The rewrite is deliberate protocol framing, not handler-specific drift
- **Claim:** The rewrite of byte 1 from `0x91` to `0x01` is not local to `0x01/0x0C` and is not unjustified drift; it is the universal NS2 command Direction byte translation required by the protocol.
- **Sources:** `main/src/ns2_codec.c:638-644`, `switch2_controller_research/commands.md:15-25`.
- **Support:** Direct evidence.
- **Confidence:** High.
- **Explanation:**
  - In `main/src/ns2_codec.c:204-216`, `cmd_0x01_handler` handles subcommand `0x0C` by returning 4 payload bytes (`61 12 50 10`). It does **not** touch the 8-byte command header or byte 1.
  - In `main/src/ns2_codec.c:638-644`, `cmd_process()` constructs the response packet:
    ```c
    // set rsp cmd bytes
    memcpy(result + PRO2_DATA_EMPTY_LEN, data_in, 4);
    // set rsp magic bytes
    memcpy(result + PRO2_DATA_EMPTY_LEN + 4, rsp_magic, 4);
    if (result[PRO2_DATA_EMPTY_LEN + 1] == 0x91) {
        result[PRO2_DATA_EMPTY_LEN + 1] = 0x01;
    }
    ```
  - Per `switch2_controller_research/commands.md:20`, byte 1 of the 8-byte Command Header is `Direction`:
    - `0x91` = Host -> Device (inbound request)
    - `0x01` = Device -> Host (outbound response)
  - `cmd_process()` echoes the first 4 bytes of `data_in` (`cmd`, `direction`, `transport`, `subcmd`), so `result[PRO2_DATA_EMPTY_LEN + 1]` initially holds the request's `0x91`. Lines 641–643 explicitly flip `Direction` from request (`0x91`) to response (`0x01`). This is universal across all commands handled by `cmd_process()`.

### 2. The console's NFC probe strictly expects Direction 0x01
- **Claim:** The console's NFC probe (`0x01/0x0C`) requires response byte 1 to be `0x01`.
- **Sources:** `docs/research/ns2-console-lifecycle.md:520-525`, `docs/research/ns2-amiibo-path.md:88`, `switch2_controller_research/commands.md:48`.
- **Support:** Direct evidence (corpus and bench log).
- **Confidence:** High.
- **Explanation:**
  - On the bench (`docs/research/ns2-console-lifecycle.md:520-525`), on 19 of 20 connections the console sent request `01 91 01 0c 00 00 00 00` (Command `0x01`, Direction `0x91`, Transport BLE `0x01`, Subcommand `0x0C`).
  - The firmware answered with `01 01 01 0c 10 78 00 00 61 12 50 10` (Command `0x01`, Direction `0x01`, Transport BLE `0x01`, Subcommand `0x0C`, Magic `10 78 00 00`, NXP PN7160 ID `61 12 50 10`).
  - In `switch2_controller_research/commands.md:48`, the documented response from an official controller is `01 01 01 0c 10 78 00 00 61 12 50 0d`. Both the captured official response and firmware response carry `0x01` in byte 1.
  - If byte 1 were left as `0x91`, the packet would represent an inbound request rather than a response, violating the NS2 command transport contract.

### 3. Calibration (0x02/0x04) is already rewritten to 0x01 in firmware and on the bench
- **Claim:** Flash calibration read `0x02/0x04` is NOT left as `0x91`; it is rewritten to `0x01` by `cmd_process()`, exactly matching `0x01/0x0C` and the canonical protocol specification.
- **Sources:** `main/src/ns2_codec.c:220-245,641-643`, `docs/research/ns2-console-lifecycle.md:139-141`, `switch2_controller_research/commands.md:144`.
- **Support:** Direct evidence (code audit, corpus, and bench log).
- **Confidence:** High.
- **Explanation:**
  - In `main/src/ns2_codec.c:220-245`, `cmd_0x02_handler` processes subcommand `0x04`. It populates `data_out` with payload data: `read_len` (byte 0), zeroes out the padding `data_out[1] = 0x00` (which was `0x7E` in the request payload `data_in[9]`), appends the 4-byte address, and appends the simulated flash bytes from `read_memory()`. It returns `read_len + 8`.
  - When `cmd_0x02_handler` returns to `cmd_process()`, lines 638–644 format the 8-byte command header. Because `data_in[1]` was `0x91`, `result[PRO2_DATA_EMPTY_LEN + 1]` is `0x91` and triggers the `if` check at line 641, rewriting byte 1 to `0x01`.
  - On the bench (`docs/research/ns2-console-lifecycle.md:139-141`), the console issues ~7 `0x02/0x04` reads per connection (140 total across 20 connections). All 140 were successfully answered by `cmd_process()` with header `02 01 01 04 10 78 00 00`, and the console successfully accepted calibration and moved to the active HID streaming state.
  - In `switch2_controller_research/commands.md:144`, the canonical response is `02 01 01 04 10 78 00 00 10 00 00 00 40 30 01 00 [16 bytes data]`. Header byte 1 is `0x01`, never `0x91`.

### 4. How the misunderstanding in G-14 originated
- **Claim:** The assertion of an "asymmetry" in G-14 stemmed from confusing payload byte 1 (`data_out[1] = 0x00` in `cmd_0x02_handler`) with command header byte 1 (`Direction` in `cmd_process`), or mistakenly assuming lines 641–643 applied only to `cmd_0x01_handler`.
- **Sources:** `main/src/ns2_codec.c:231-233,641-643`, `docs/spec/07-firmware-architecture.md:86-90`, `docs/spec/12-handoff.md:86`.
- **Support:** Researcher inference based on codebase structure.
- **Confidence:** High.
- **Explanation:**
  - In `cmd_0x02_handler` (`main/src/ns2_codec.c:231-233`), the handler does:
    ```c
    memcpy(data_out, data_in + 8, 8);
    data_out[1] = 0x00;
    ```
    This clears byte 1 of the *command payload* (turning the host request's `0x7E` marker into `0x00`, matching `switch2_controller_research/commands.md:144`).
  - A casual reader looking at `cmd_0x01_handler` (lines 204–216) and `cmd_0x02_handler` (lines 220–245) without tracing the downstream assembly in `cmd_process` (lines 638–644) could easily conflate `data_out[1]` with `result[PRO2_DATA_EMPTY_LEN + 1]`.
  - Alternatively, because `cmd_0x01_handler` is the first handler registered, a reader seeing `if (result[PRO2_DATA_EMPTY_LEN + 1] == 0x91)` in `cmd_process` may have erroneously assumed it was special-casing command `0x01`.

### 5. No subcommand in the NS2 protocol ever uses 0x91 in a response
- **Claim:** There is no subcommand in the entire NS2 protocol where the controller sends `0x91` in byte 1 of the response header; all subcommands (current and future AMIIBO commands) require `0x01`.
- **Sources:** `switch2_controller_research/commands.md:15-350`, `docs/research/ns2-amiibo-path.md:88`.
- **Support:** Direct evidence.
- **Confidence:** High.
- **Explanation:**
  - Across every command in `switch2_controller_research/commands.md` (`0x01` NFC, `0x02` Flash, `0x03` Init, `0x07` Unknown, `0x08` Grip, `0x09` Player LEDs, `0x0A` Vibration, `0x0B` Battery, `0x0C` Feature Select, `0x0D` Firmware Update, `0x10` Firmware Info, `0x11` Unknown, `0x15` Pairing, `0x16`, `0x18`), all example responses show byte 1 as `0x01`.
  - When implementing Stage 3 / Step 6 AMIIBO commands (`0x01/0x03`, `0x01/0x04`, `0x01/0x05`, `0x01/0x06`, `0x01/0x14`, `0x01/0x15`), their responses will automatically route through `cmd_process()` and receive `0x01` in byte 1 without any special-case handling.

---

## Contradictions

| Topic | Assertion in Spec / Ticket (G-14) | Reality in Code & Corpus | Status |
| --- | --- | --- | --- |
| **Response byte 1 in `0x02/0x04`** | `docs/spec/07-firmware-architecture.md:86` states: *"main/src/ns2_codec.c rewrites response byte 1 from 0x91 to 0x01 for this subcommand, while 0x02/0x04 (the flash read the console uses for calibration) also sets 0x91 and is not rewritten."* | **False.** `main/src/ns2_codec.c:641-643` rewrites byte 1 for **all** commands processed by `cmd_process()`. In `0x02/0x04`, byte 1 is rewritten to `0x01`, which is verified by 140 successful transactions in `ns2-console-lifecycle.md:139`. | Contradiction resolved: the spec statement is an error of code inspection. |
| **Header Byte 1 vs Payload Byte 1** | Ticket assumes an asymmetry between `cmd_0x01_handler` and `cmd_0x02_handler` | Handlers only populate `data_out` (payload). Command header byte 1 is generated in `cmd_process()`, which treats all commands uniformly. | Resolved. |

---

## Missing evidence

- **Console tolerance to a malformed direction byte (`0x91` in a response):** While the corpus and protocol definition prove `0x01` is required and expected, only a bench experiment deliberately hacking `cmd_process()` to output `0x91` could determine whether the console silently ignores the response, drops the BLE connection, or crashes. Because `0x01` is working and valid across 140 calibration reads and 19 NFC probes, testing console error handling for an intentionally broken packet is unnecessary for implementation.

---

## Sources

### Kept
- `main/src/ns2_codec.c:204-245,607-650` — Proves `cmd_0x01_handler` and `cmd_0x02_handler` only manipulate payload buffers, and `cmd_process()` universally rewrites `data_in[1]` (`0x91`) to `result[PRO2_DATA_EMPTY_LEN + 1] = 0x01`.
- `main/src/gatt.c:300-335,420-435` — Proves GATT write-no-rsp on handle `0x0016` dispatches through `cmd_process()` and responds via notification on handle `0x001e`.
- `switch2_controller_research/commands.md:15-350` (pinned commit `a3306b4`) — Canonical protocol reference defining Command Header offset `0x1` as `Direction` (`0x91` request, `0x01` response) across all command types.
- `switch2_controller_research/bluetooth_interface.md:380-450` (pinned commit `a3306b4`) — Explains GATT attribute handles, 14-byte padding for notify `0x001e`, and BLE command framing.
- `docs/research/ns2-console-lifecycle.md:130-150,520-530` — Bench measurements documenting console lifecycle: 19/20 connections execute `0x01/0x0C` and receive `01 01 01 0c 10 78 00 00...`; ~7 `0x02/0x04` reads per connection (140 total) are executed and answered successfully.
- `docs/research/ns2-amiibo-path.md:85-95` — Protocol analysis confirming NS2 command exchange is strictly console-led with Direction `0x91` from host and `0x01` from device.
- `docs/spec/07-firmware-architecture.md:86-90` & `docs/spec/12-handoff.md:86` — Origin of Known Gap G-14.

### Rejected / Deprioritized
- `esp32-joycontrol` — Untrusted repository; uses NS1-era subcommands not applicable to NS2 BLE GATT command framing.

---

## Next steps

1. **Retire Known Gap G-14:** Update `docs/spec/12-handoff.md` (§12.3) and `docs/spec/07-firmware-architecture.md` (§7.3 step 8) to strike G-14, noting that the `0x91 -> 0x01` Direction rewrite in `main/src/ns2_codec.c:641-643` is universal, protocol-mandated, and applies symmetrically to both `0x01/0x0C` and `0x02/0x04`.
2. **Proceed with Stage 3 / Step 6 AMIIBO Implementation:** When adding subcommands `0x01/0x03`, `0x01/0x04`, `0x01/0x05`, `0x01/0x06`, `0x01/0x14`, `0x01/0x15` into `cmd_0x01_handler`, implementers do not need to manage response byte 1; `cmd_process()` will handle the Direction byte `0x01` automatically and correctly.
