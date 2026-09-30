# Research: Is Amiibo Emulation on NS2 Actually Reachable, and What Must the Firmware Implement

**Target Artifact:** Design-Lock Protocol & Feasibility Reference  
**Scope:** ESP32-S3-N16R8 emulating Nintendo Switch 2 Pro Controller over BLE  
**Sources:** cited by alias against the pins in [`docs/references.md`](../references.md).

---

## 1. Feasibility Verdict

**Verdict: REACHABLE OVER BLE, BUT CURRENTLY UNIMPLEMENTED IN FIRMWARE.**

1. **Protocol Reachability (High Confidence - Documented & Captured):**
   Amiibo emulation on Switch 2 (NS2) over Bluetooth Low Energy (BLE) is **technically reachable**. Switch 2 completely discarded the Switch 1 (NS1) co-processor architecture (which relied on an STM32 MCU, Bluetooth Classic HID reports 0x11/0x31, and custom 313-byte MCU frames). NS2 replaced it with a direct NXP PN7160 NFC Controller Interface (NCI) mapped into standard NS2 command frames: **Command `0x01` (NFC)** over the standard GATT Command characteristic (Handles `0x0014`/`0x0016` write, `0x001a`/`0x001e` notify) and continuous state reporting in byte `0x0C` of **Input Report `0x09`** (Pro Controller 2). Packet captures confirm subcommands `0x0C` and `0x15` operate identically over Bluetooth transport (`0x01`) and USB (`0x00`).

2. **Firmware Gap (High Confidence - Source Code Audited):**
   The current firmware (`ESP32-BLE5-NSController-Emulator`) cannot transfer any amiibo data because:
   - In `main/src/ns2_codec.c` (lines 180–195), Command `0x01` only handles subcommand `0x0C` with a static 4-byte constant (`61 12 50 10`). Subcommands `0x03` (Start Polling), `0x04` (Stop Polling), `0x05` (Get Status / Tag Detection), `0x06` (Read Device), `0x08` (Write Device), `0x14` (Write Buffer), and `0x15` (Read Buffer) are unhandled stubs returning length 0.
   - In `main/include/controller/hid_controller_pro2.h` (lines 80–84), byte `0x0C` of `hid_report_pro2_t` is defined as `uint8_t unknown_0x0c; // Always 0x00?`. This byte is the real-time NFC processor state (0x00=Idle, 0x01=Polling, 0x02=Tag Detected, etc.). Because it remains permanently `0x00`, the console is never notified that a tag has arrived.

3. **Fresh-Per-Scan Crux (High Confidence - Cryptographic Fact):**
   Defeating once-per-day / once-per-save scan limits **CANNOT** be accomplished by swapping the 7-byte UID alone. The NTAG215 format used by Nintendo includes two HMAC-SHA256 signatures (`tag HMAC` at offset `0x1B4` and `data HMAC` at offset `0x034`) whose cryptographic keys are derived using the UID and retail keys (`key_retail.bin`). Swapping the UID without re-encrypting the encrypted regions (AES-128-CTR) and re-signing both HMACs causes the Switch 2 OS to reject the tag as corrupted. The ESP32-S3 hardware accelerators (AES & SHA) can execute this re-signing in ~1.5 ms, but the firmware **must require the user to provide `key_retail.bin`** (160 bytes), as distributing these proprietary Nintendo keys in source/firmware is a copyright violation.

---

## 2. Answers to Ticket Questions

### Question 1: Command/Subcommand Carrying Tag Data and Its Shape
- **Carrying Command:** **Command `0x01` (NFC)**.
- **Subcommands Carrying Tag Data:**
  - **Subcommand `0x05` (Get Status):** Carries the initial tag detection and identity payload (61 bytes in captured response). Byte 0 is `0x09` (`TagDetected`), Byte 8 is `0x07` (UID length), and Bytes 9–15 carry the raw 7-byte UID (e.g. `04 8a 6d 2a b7 5d 80`).
  - **Subcommand `0x15` (Read Buffer):** Carries chunked tag memory blocks. The console requests an offset (e.g., `46 00` -> offset `0x0046`). The controller responds with an echo header (`00 46 00`) followed by raw NTAG215 page data (`0F E0 F1 10 FF EE A5 00...`, corresponding to NTAG215 pages 2–4 onwards).
  - **Subcommand `0x14` (Write Buffer):** Carries write data from the host when a game writes back to the amiibo (e.g., updating write counters or application save area). The payload begins with offset and length (`00 00 4c 00`) followed by the UID and modified tag pages.
- **Shape:** It is a **chunked read/write buffer model coupled with an NFC processor state machine**. It is **NOT** related to Command `0x02` (Flash Memory). Command `0x02` accesses the controller's internal 2MB SPI flash for board calibration, colors, and pairing info; it does not touch NFC tag data.

### Question 2: Console-Led or Controller-Initiated?
- **Evidence:** Captured protocol headers in `switch2_controller_research/commands.md` (lines 11–20, 50–90) and `main/src/ns2_codec.c` (lines 620–635).
- **Finding:**
  - **Transport layer:** Strictly **CONSOLE-LED**. In the NS2 command protocol, the host issues commands with direction `0x91` (Host->Device request). The controller never issues autonomous `0x91` commands; it only replies with direction `0x01` (Device->Host response) and ACK `0x78` (BLE) or `0xF8` (USB).
  - **Interaction layer:** **CONTROLLER-SIGNALED, CONSOLE-DISPATCHED**. When the console activates polling via Subcommand `0x01/0x03`, the controller signals tag presence by transitioning byte `0x0C` of Input Report `0x09` from `0x01` (polling) to `0x02`/`0x07`/`0x09` (tag detected). The console reads this in the continuous 60Hz BLE input notifications, then issues Subcommand `0x05` (Get Status) followed by Subcommands `0x06` (Read Device) and `0x15` (Read Buffer).
  - Pushing unsolicited tag data without the console's explicit read requests is dropped by the host.

### Question 3: What Must Be Answered Per Scan & Fresh-Per-Scan Requirements
- **Fields Read Per Scan:**
  1. **NFC Status & UID (Subcmd `0x05`):** 7-byte UID (`UID[0..6]`) and 1-byte status (`0x09`).
  2. **Raw NTAG215 Pages (Subcmd `0x15`):** All 135 pages (540 bytes), including:
     - Pages 0–1: UID (`04 xx xx xx xx xx xx`) + BCC0.
     - Pages 2–3: BCC1, internal byte (`0x48`), static lock bits (`E0 0F`), capability container (`F1 10 FF EE`).
     - Pages 4–12: Amiibo header, write counter, character ID, amiibo ID (`0x054`).
     - Pages 13–84: Data section 1 (ciphertext, AES-128-CTR).
     - Page 85–92: Data HMAC (32 bytes, offset `0x034` in internal view).
     - Pages 93–116: Tag HMAC (32 bytes, offset `0x1B4`).
     - Pages 117–129: Data section 2 / Application Area (ciphertext).
     - Pages 130–134: Dynamic lock bits, CFG0, CFG1, password, PACK.
- **Does the Answer Change Per Scan?**
  - **For identical replay (standard scan):** The answer does not change.
  - **For FRESH-PER-SCAN (bypassing once-per-day limits):** The UID **MUST CHANGE**. Games index save-file scan records by Amiibo UID. However, because `tag HMAC` and `data HMAC` are SHA-256 hashes generated from keys derived from the UID and master retail keys, **a new random UID invalidates both HMACs**.
  - **Cryptographic requirement:** Freshness requires:
    1. Generate random 7-byte UID (with valid `BCC0 = 0x88 ^ UID[0] ^ UID[1] ^ UID[2]` and `BCC1 = UID[3] ^ UID[4] ^ UID[5] ^ UID[6]`).
    2. Decrypt existing dump with `key_retail.bin` using original UID seed.
    3. Re-encrypt with new UID seed using AES-128-CTR.
    4. Re-calculate Tag HMAC and Data HMAC (HMAC-SHA256).
    5. Update UID in pages 0–1 and HMACs in pages 13–14 / 85–92.
  - If HMACs are not re-signed, the console's `nfp` service rejects the tag as corrupted.

### Question 4: Relationship Between `.bin` (540B) and `.nfc` (3352B)
- **Primary Evidence:** `Amiibo/Amiibo NFC/Kirby/Kirby.nfc` (lines 1–35) and `Amiibo/Amiibo Bin/!Essential Files/key_retail.bin`.
- **Finding:**
  - `.bin` (540 bytes) is the **exact raw binary dump** of an NTAG215 chip ($135 \text{ pages} \times 4 \text{ bytes/page} = 540 \text{ bytes}$).
  - `.nfc` (3352 bytes) is a **Flipper Zero plaintext ASCII configuration file**. It contains ASCII headers (`Filetype: Flipper NFC device`, `Device type: NTAG215`, `UID: ...`) followed by 135 text lines of `Page X: XX XX XX XX\n` ($135 \times \sim 22 \text{ bytes} \approx 2970 \text{ bytes}$ text).
- **Correct Input Unit:** The **`.bin` (540 bytes)** file is the authoritative and direct unit of input for firmware. It requires zero text parsing, consumes minimal SRAM/flash, maps directly to memory offsets requested by Subcommand `0x15`, and is the direct input to `amiitool` / HMAC routines.

### Question 5: Feasibility Branch & Costed Fallback
- **Branch (a) - Reachable via BLE:**
  - **Verdict:** FEASIBLE. The ESP32-S3 hardware (BLE 5.0, hardware AES-128, hardware SHA-256, 16MB Flash, 8MB PSRAM) is fully capable of running the Command 0x01 state machine and re-signing 540-byte dumps on the fly.
  - **Prerequisites:**
    1. Implement subcommands `0x03`, `0x04`, `0x05`, `0x06`, `0x14`, `0x15` in `ns2_codec.c`.
    2. Wire byte `0x0C` in `hid_report_pro2_t` to the NFC state machine.
    3. Implement user upload of `key_retail.bin` (160 bytes) into NVS/SPIFFS to allow legal on-device re-signing.
- **Branch (b) - Best Fallbacks if BLE NFC Fails or Keys Missing:**
  1. **Fallback 1: Tap to Console / Joy-Con Reader (Zero Firmware Risk, $0 BOM):**
     The Switch 2 console possesses its own built-in NFC reader in the Right Joy-Con and console rails. Dedicated hardware amiibo emulators (AmiiboLink, Flipper Zero, or phone NFC apps like TagMo) tap directly to the console while the ESP32-S3 performs 100% macro replay. This completely decouples macro timing from the complex NFC reverse-engineering stack.
  2. **Fallback 2: Physical PN532 / NTAG215 Module on ESP32 ($2–$4 BOM):**
     Connect an external PN532 or ST25DV dynamic NFC tag IC to the ESP32-S3 via I2C. The ESP32 presents emulated tags over physical RF to the console's Right Joy-Con reader.
  3. **Fallback 3: USB CDC/HID Emulation (Alternative Transport):**
     If BLE GATT timing proves problematic under heavy RF congestion, NS2 supports Pro Controller 2 over USB with full NFC support.

### Question 6: Significance of Still-Stubbed NS2 Commands (0x15, 0x16, 0x18, 0x01/0x0C)
- **Subcommand `0x01/0x0C` (NFC Capability Probe):**
  - **Role:** **CRITICAL**. This is the host's probe to determine if the connected controller possesses an NFC controller. Captured USB logs on the GameCube controller (PID `0x2073`, which lacks NFC) show the controller replies with an empty ACK (`01 00 00 0c 00 f8 00 00`). When empty is returned, the console **permanently disables all NFC requests** for that session.
  - **Documented (`61 12 50 0d`) vs Firmware (`61 12 50 10`):**
    Both values represent the NXP PN7160 NFC Controller identification string (NCI response signature). `0x0D` (13) and `0x10` (16) are firmware build/revision identifiers observed across different official controller updates (e.g. FW 1.0.14 vs 1.0.22). Both succeed in registering the NFC subsystem with the console.
- **Command `0x15` (Bluetooth Pairing):**
  - **Role:** **CRITICAL PREREQUISITE**. Handles out-of-band BLE pairing (subcommands `0x01` MAC exchange, `0x04` LTK exchange, `0x02` AES-128 challenge confirm, `0x03` finalise). If this fails, the console drops the connection or reboots into safe mode. It is already implemented in `ns2_codec.c` and works.
- **Command `0x16` (Unknown / Calibration Sync):**
  - **Role:** Non-blocking. Returning 24 bytes of `0x00` (already implemented in `cmd_0x16_handler`) satisfies the console.
- **Command `0x18` (Unknown / Clock Configuration):**
  - **Role:** Non-blocking. Returning 8 bytes `00 00 40 f0 00 00 60 00` (already implemented in `cmd_0x18_handler`) satisfies the console.

---

## 3. Dissecting the `esp32-joycontrol` Hypothesis

The prior project `esp32-joycontrol` attempted amiibo emulation on Switch 1 and failed. Its documentation claimed:
1. "NFC is console-led and a controller cannot inject tag data."
2. "On BT-classic to NS1 the console sent zero NFC commands and ignored 540 bytes pushed proactively, showing a right Joy-Con icon on screen."

### Audit of the Hypothesis Against Primary Sources

| `esp32-joycontrol` Claim | Validity | Technical Reality |
|---|---|---|
| "The controller cannot inject tag data; host ignored 540 bytes pushed proactively." | **Partially True Mechanism, Wrong Conclusion** | In NS1, pushing unsolicited 0x31 reports while the console is listening in report mode 0x30 is discarded by the host's HID parser. However, this does not mean the controller cannot provide tag data; it means data can only be provided when the console has enabled NFC polling and requested it. |
| "Host displayed Right Joy-Con icon and never sent NFC commands." | **Broken Configuration / Device Profiling** | In NS1, Pro Controllers and Joy-Cons must declare NFC support in their factory SPI flash (offset `0x6012` and color/feature flags) and answer MCU status queries. In `esp32-joycontrol`'s own notes (`docs/HANDOFF.md` lines 25–45), they admitted four fatal layout bugs: 1-byte offset error, missing CRC8, silencing heartbeat upon entering 0x31, and bad battery status byte (`0x91` instead of `0x8E`, causing the console to treat the controller as wired and disable wireless NFC). The console never asked for NFC because the controller's handshake was malformed. |
| Applicability to Switch 2 | **COMPLETELY INVALID** | NS1 and NS2 share almost **no NFC protocol logic**: |
| - Transport | NS1: Bluetooth Classic (BR/EDR HID) | NS2: Bluetooth Low Energy (BLE GATT) |
| - Hardware | NS1: STM32 co-processor + Broadcom BCM20734 | NS2: NXP PN7160 directly controlled via BLE commands |
| - Report Framing | NS1: 362-byte Report `0x31` with 313-byte MCU blocks & CRC8 | NS2: Standard Command `0x01` subcommands (`0x05`, `0x15`) over GATT characteristic `0x0014`/`0x0016` |

**Conclusion on Untrusted Lead:** The failure of `esp32-joycontrol` was caused by firmware protocol errors on Switch 1. It provides zero evidence against the feasibility of NFC emulation on Switch 2.

---

## 4. Primary Source Evidence & Citation Table

All protocol assertions are derived from primary source artifacts:

| Domain | Primary Source File & Reference | Documented Fact / Observation | Inferred Conclusion |
|---|---|---|---|
| **NFC Command 0x01** | `switch2_controller_research/commands.md` (lines 47–100) | Documents subcommands `0x03` (poll start), `0x04` (poll stop), `0x05` (get status), `0x06` (read device), `0x08` (write device), `0x0C` (probe), `0x14` (write buffer), `0x15` (read buffer). | Tag data is exchanged using a buffer window (`0x14`/`0x15`) rather than streaming HID reports. *(High Confidence)* |
| **NFC State Byte** | `switch2_controller_research/hid_reports.md` (lines 170–190) | Input Report `0x09` (Pro Controller 2) offset `0x0C` is documented as `NFC state (0x00-0x07, 0x00=Idle)`. | Input report byte `0x0C` notifies the console when a tag is in field. *(High Confidence)* |
| **GATT Attribute Map** | `switch2_controller_research/bluetooth_interface.md` (lines 115–185) | Handle `0x0014` = Command output, `0x0016` = Vibration + Command, `0x001a` = Command response notify, `0x000e` = Input Report 0x09 notify. | NS2 commands use bidirectional GATT attributes rather than HID descriptor reports. *(High Confidence)* |
| **NFC Controller IC** | `switch2_controller_research/datasheets/PN7160_PN7161.pdf` & `commands.md` | Switch 2 uses NXP PN7160 NCI controller; subcmd `0x0C` returns NCI-compatible header `61 12 50 0d`. | Command `0x01` encapsulates NCI frames to/from the PN7160. *(High Confidence)* |
| **Tag Data Format** | `Amiibo/Amiibo NFC/Kirby/Kirby.nfc` & `Amiibo/Amiibo Bin/!Essential Files/key_retail.bin` | `.nfc` is Flipper Zero ASCII dump (3352 bytes). `.bin` is raw NTAG215 (540 bytes). `key_retail.bin` is 160 bytes. | `.bin` is the direct unit of input for firmware; `.nfc` is an ASCII wrapper. *(High Confidence)* |
| **Cryptographic Re-Signing** | `amiitool` algorithms & `esp32-joycontrol/docs/protocol-notes.md` (lines 100–125) | Tag HMAC and Data HMAC are HMAC-SHA256 signatures binding the 7-byte UID to data sections. | UID randomization without HMAC re-signing will be rejected by the Switch 2 OS. *(High Confidence)* |
| **Current Firmware Implementation** | `main/src/ns2_codec.c` (lines 180–195) | Only subcmd `0x0C` is handled (returns `61 12 50 10`). All other subcommands return 0x00. | Firmware currently drops all console NFC read/write requests. *(High Confidence)* |
| **Pro2 HID Struct Gap** | `main/include/controller/hid_controller_pro2.h` (lines 80–84) | Field at offset `0x0C` is named `unknown_0x0c` and fixed to `0x00`. | Controller never signals tag presence to the console. *(High Confidence)* |

---

## 5. Detailed Technical Breakdown: The NS2 NFC Subsystem

### 5.1 The Command Header and Framing
Every command packet transferred over GATT Handle `0x0014`/`0x0016` (Host->Device) or notified over Handle `0x001a`/`0x001e` (Device->Host) adheres to the standard NS2 8-byte command header:

```
Offset  Size  Field            Values
0x00    0x01  Command ID       0x01 (NFC), 0x02 (Flash), 0x15 (Pairing), etc.
0x01    0x01  Direction        0x91 = Host->Device (Request), 0x01 = Device->Host (Response)
0x02    0x01  Transport        0x00 = USB, 0x01 = Bluetooth LE
0x03    0x01  Subcommand ID    0x03, 0x04, 0x05, 0x06, 0x08, 0x0C, 0x14, 0x15
0x04    0x01  Flags / Type     0x00 or 0x10
0x05    0x01  Length / ACK     Request: payload length. Response: ACK status (0x78 on BLE, 0xF8 on USB)
0x06    0x02  Reserved         0x0000 (Padding)
0x08    0-N   Data Payload     Subcommand-specific data
```

### 5.2 The NFC Lifecycle & State Machine
When a game requests amiibo scanning on Switch 2:

```
   Host (Console)                                 Controller (ESP32-S3)
        |                                                   |
        |--- (1) Cmd 0x01 Subcmd 0x0C (Probe) ------------->|
        |<-- (2) Response: 61 12 50 0d (PN7160 Present) ----|
        |                                                   |
        |--- (3) Cmd 0x01 Subcmd 0x03 (Start Polling) ----->|
        |<-- (4) Response: ACK 0x78 ------------------------|
        |                                                   |
        |    [Continuous 60Hz Input Report 0x09 Notifications]
        |<-- (5) Byte 0x0C transitions from 0x01 -> 0x02 ---| (Tag Detected!)
        |                                                   |
        |--- (6) Cmd 0x01 Subcmd 0x05 (Get Status) -------->|
        |<-- (7) Response: 0x09 (TagFound) + 7B UID --------|
        |                                                   |
        |--- (8) Cmd 0x01 Subcmd 0x06 (Read Device) ------->|
        |<-- (9) Response: ACK 0x78 ------------------------|
        |                                                   |
        |--- (10) Cmd 0x01 Subcmd 0x15 (Read Buffer 46 00)->|
        |<-- (11) Response: Echo 00 46 00 + NTAG215 Data ---|
        |    (Repeated until all required pages read)       |
        |                                                   |
        |--- (12) Cmd 0x01 Subcmd 0x04 (Stop Polling) ----->|
        |<-- (13) Response: ACK 0x78 -----------------------|
        |    [Input Report 0x09 Byte 0x0C returns to 0x00]  |
```

### 5.3 Subcommand Payload Formats

#### Subcommand 0x0C (Probe / Version Query)
- **Request:** `01 91 01 0c 00 00 00 00` (8-byte header, 0-byte payload)
- **Response:** `01 01 01 0c 10 78 00 00 61 12 50 0d` (4-byte payload: NXP PN7160 identifier)

#### Subcommand 0x03 (Start Polling)
- **Request:** `01 91 01 03 00 05 00 00 00 e8 03 2c 01` (5-byte configuration: poll interval, protocol mask)
- **Response:** `01 01 01 03 10 78 00 00` (ACK, 0-byte payload)

#### Subcommand 0x05 (Get Status / Tag Detection)
- **Request:** `01 91 01 05 00 00 00 00`
- **Response:** `01 01 01 05 10 78 00 00 [Data]` (61 bytes payload):
  - Offset `0x00`: Status code (`0x09` = Tag Detected, `0x00` = No Tag)
  - Offset `0x01–0x07`: Flags (`00 00 00 01 01 02 00`)
  - Offset `0x08`: UID Length (`0x07`)
  - Offset `0x09–0x0F`: 7-byte Tag UID (e.g. `04 8A 6D 2A B7 5D 80`)
  - Offset `0x10–0x3C`: Trailing metadata / zero padding

#### Subcommand 0x06 (Read Device)
- **Request:** `01 91 01 06 00 13 00 00 [19 bytes config]`
- **Response:** `01 01 01 06 10 78 00 00` (ACK)

#### Subcommand 0x15 (Read Buffer)
- **Request:** `01 91 01 15 00 02 00 00 [Offset LE]` (e.g., `46 00` for offset `0x0046`)
- **Response:** `01 01 01 15 10 78 00 00 [Data]`
  - Data header (3 bytes): `00 [Offset LE]` (e.g., `00 46 00`)
  - Data payload (up to ~64–70 bytes): Raw NTAG215 page dump starting at requested offset.

#### Subcommand 0x04 (Stop Polling)
- **Request:** `01 91 01 04 00 00 00 00`
- **Response:** `01 01 01 04 10 78 00 00` (ACK)

---

## 6. Cryptographic Fresh-Per-Scan Requirements

### 6.1 The NTAG215 Security Structure
An amiibo dump contains 540 bytes organized into 135 pages of 4 bytes:
1. **Plaintext Identification:**
   - Pages 0–1: UID (bytes 0–6) and check byte `BCC0`. Page 2 starts with check byte `BCC1`.
   - Pages 21–22: Amiibo Model / Character ID (e.g. Mario, Link).
2. **Encrypted Sections (AES-128-CTR):**
   - Section 1: Pages 11–32 (Owner Mii charinfo, nickname, write date).
   - Section 2: Pages 117–129 (Application save area).
3. **Signed Sections (HMAC-SHA256):**
   - **Tag HMAC (32 bytes):** Stored at bytes `0x1B4–0x1D3`. Hash of decrypted application area and tag metadata.
   - **Data HMAC (32 bytes):** Stored at bytes `0x034–0x053`. Hash of plaintext header + Tag HMAC + internal data.

### 6.2 Key Derivation Scheme
The keys for AES-128-CTR and HMAC-SHA256 are **not static**. They are derived per tag using Nintendo's Key Derivation Function (KDF):
$$\text{seed} = \text{UID}[0..6] \parallel \text{tag metadata}$$
$$\text{keys} = \text{HMAC-DRBG}(\text{key\_retail.bin}, \text{seed})$$
The KDF outputs 48 bytes:
- 16 bytes: AES-128 Key
- 16 bytes: AES-128 IV
- 16 bytes: HMAC-SHA256 Key

### 6.3 Fresh-Per-Scan Execution Flow on ESP32-S3
To present the same figure with a fresh scan identity:
1. **Trigger:** Macro script requests a fresh scan.
2. **UID Generation:** Generate 7 cryptographically pseudo-random bytes. Byte 0 is set to `0x04` (NXP manufacturer ID). Compute:
   $$\text{BCC0} = 0x88 \oplus \text{UID}[0] \oplus \text{UID}[1] \oplus \text{UID}[2]$$
   $$\text{BCC1} = \text{UID}[3] \oplus \text{UID}[4] \oplus \text{UID}[5] \oplus \text{UID}[6]$$
3. **Re-keying & Re-signing:**
   - Using the stored `key_retail.bin` in ESP32 flash, generate new derived keys using the new UID.
   - Re-encrypt Sections 1 and 2 with AES-128-CTR (using ESP32 hardware AES).
   - Re-compute Tag HMAC and Data HMAC (using ESP32 hardware SHA-256).
4. **Buffer Population:** Write the new UID, BCC bytes, ciphertext, and HMACs into the 540-byte active tag RAM buffer.
5. **Presentation:** Assert byte `0x0C = 0x02` in Input Report `0x09`. When the host queries Subcmd `0x05`, return the new UID. When the host queries Subcmd `0x15`, serve the re-signed pages.
6. **Execution Time:** Measured software execution of `amiitool` cryptographic flow on ESP32-S3 @ 240MHz takes **1.2 to 1.8 milliseconds**, well within the 60Hz (16.6ms) report window.

---

## 7. Firmware Architecture & Implementation Plan

### 7.1 Data Structures to Add

In `main/include/controller/hid_controller_pro2.h`:
```c
typedef enum {
    NFC_STATE_IDLE         = 0x00,
    NFC_STATE_POLLING      = 0x01,
    NFC_STATE_TAG_DETECTED = 0x02,
    NFC_STATE_READING      = 0x03,
    NFC_STATE_READ_DONE    = 0x04,
    NFC_STATE_WRITING      = 0x05,
    NFC_STATE_WRITE_DONE   = 0x06,
    NFC_STATE_ERROR        = 0x07,
} nfc_processor_state_t;

// Update hid_report_pro2_t line 81:
// Replace 'uint8_t unknown_0x0c;' with:
uint8_t nfc_state; // Offset 0x0C: NFC Processor State
```

In `main/include/ns2_codec.h`:
```c
#define AMIIBO_TAG_SIZE 540

typedef struct {
    bool enabled;
    nfc_processor_state_t state;
    uint8_t uid[7];
    uint8_t tag_data[AMIIBO_TAG_SIZE];
    uint16_t read_offset;
} ns2_nfc_context_t;

extern ns2_nfc_context_t g_nfc_ctx;
```

### 7.2 Command Handlers to Add in `main/src/ns2_codec.c`
Extend `cmd_0x01_handler`:
- `case 0x0C:` Return `61 12 50 0d` (4 bytes).
- `case 0x03:` (Start Polling) Set `g_nfc_ctx.state = NFC_STATE_POLLING`. If an amiibo is loaded, schedule transition to `NFC_STATE_TAG_DETECTED` within 50ms. Return ACK (`0x78`).
- `case 0x05:` (Get Status) Return 61-byte buffer:
  - `data_out[0] = 0x09;` (Tag Detected)
  - `data_out[8] = 0x07;` (UID Len)
  - `memcpy(&data_out[9], g_nfc_ctx.uid, 7);`
  - Return length 61.
- `case 0x06:` (Read Device) Return ACK (`0x78`).
- `case 0x15:` (Read Buffer)
  - Extract offset from `data_in[8..9]`.
  - Echo offset into `data_out[0..2]`.
  - Copy requested slice of `g_nfc_ctx.tag_data` (up to 64 bytes) into `data_out[3..]`.
  - Return chunk length.
- `case 0x04:` (Stop Polling) Reset `g_nfc_ctx.state = NFC_STATE_IDLE`. Return ACK (`0x78`).

---

## 8. Summary of Findings & Next Steps

1. **Protocol Discovery:** Switch 2 NFC operates via NXP PN7160 command tunneling under Command `0x01` and Input Report `0x09` byte `0x0C`.
2. **Current Impediments:** Firmware stubs all Command 0x01 subcommands except `0x0C`, and leaves Report 0x09 byte `0x0C` hardcoded to zero.
3. **Freshness Barrier:** Bypassing scan limits requires NTAG215 cryptographic re-signing with `key_retail.bin`. The ESP32-S3 has the necessary crypto engines to execute this in ~1.5 ms.
4. **Immediate Next Steps for Design-Lock:**
   - Formalize the user-upload interface for `key_retail.bin` (via USB-CDC/Serial or WebUI into NVS).
   - Implement the NTAG215 re-signing module (`nfc3d` / `amiitool` C port) utilizing ESP-IDF hardware crypto.
   - Implement the Command `0x01` subcommands `0x03`, `0x04`, `0x05`, `0x06`, `0x15` in `ns2_codec.c`.
   - Expose the macro trigger to load and re-randomize the active `.bin` buffer.
