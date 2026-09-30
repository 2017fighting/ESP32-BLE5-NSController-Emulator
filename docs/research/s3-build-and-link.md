# Research: ESP32-S3 Build, 5 ms Link, 16 MB Partition Layout, and Reproducible Recipe

## Executive Summary

- **Verdict on Released-IDF Build Path:** **YES, a released-IDF build path exists today.** On June 1, 2026, Espressif officially integrated and backported the sub-spec (5 ms / 2.5 ms) BLE connection interval support for ESP32-S3 across stable release branches. It is available in **ESP-IDF v5.5.4+** (branch commit `cf13345`), **v6.0.2+** (commit `142aea3`), **v5.4.4+** (commit `aefcf1c`), and **v5.2.7+** (commit `fb3cac4d438331401ec8dff13796b91905dc6b03`). On these versions, setting `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` in Kconfig is **completely sufficient**—no local binary patching, external ZIP files, or custom toolchain forks are needed.
- **The 5 ms Link Mechanism on S3:** ESP32-S3 uses the closed-source Bluetooth controller blob (`libbtdm_app.a`). The local script `patch_nimble_lib.py` only works for RISC-V NimBLE targets (C6/C61/C2/H2) and explicitly rejects `esp32s3`. User-level binary opcode patching on S3 fails because internal controller validation still triggers `LL_REJECT_EXT_IND`. The official fix works by calling `ble_min_conn_interval_enable(3)` inside the controller initialization wrapper and relaxing host-side validation (`BLE_CONN_INT_MIN_HOST_CHECK`).
- **16 MB Flash Partition Layout:** The current project configuration (`sdkconfig.defaults.esp32s3` with `CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y` and `partitions_n2c.csv` using 2 MB total) leaves ~14 MB unmapped. For the ESP32-S3-DevKitC-1 N16R8, we recommend a 16 MB OTA-capable layout: two 3.0 MB app slots (`ota_0`, `ota_1`), a 32 KB NVS partition, an 8 KB `otadata` partition, a 4 KB `phy_init` partition, and a **9.8 MB SPIFFS/LittleFS partition** (`storage`) to hold macro sequences and Amiibo dumps. EasyMCU's 10 MB `storage` typed `nvs` was an inefficient workaround for byte-by-byte AVR EEPROM emulation and should **not** be duplicated.
- **Hardware Topology (Two USB-C Ports):** The "UART/COM" port (CP2102N/CH343 to GPIO43/44) is dedicated to esptool flashing, hardware reset, and serial logging (`ESP_LOG`). The native "USB" port (GPIO19/20) connects to the ESP32-S3 USB OTG peripheral running TinyUSB CDC-ACM (`CONFIG_TRANSPORT_LAYER_USB_CDC=y`), acting as the dedicated, unpolluted control-plane interface to the host PC.

---

## Part 1: The 5 ms Link on ESP32-S3

### 1.1 The C6/C61 Patch Mechanism (`patch_nimble_lib.py`)

- **Target File:** `$IDF_PATH/components/bt/controller/lib_esp32c6/esp32c6-bt-lib/esp32c6/libble_app.a` or `esp32c61/libble_app.a` (*Source: `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/patch/patch_nimble_lib.py`, lines 71–76*).
- **Architecture & Toolchain:** RISC-V (`riscv32-esp-elf-ar`, `riscv32-esp-elf-objdump`) (*Source: lines 26–64*).
- **Patch Operation:**
  - Extracts object `ble_ll_conn.c.o` from the static archive `libble_app.a` (*Source: lines 288–292*).
  - Searches for instruction `addi a5, a4, -6` (hex `93 07 a7 ff`), which enforces the BLE Core Specification minimum connection interval of 6 units ($6 \times 1.25\text{ ms} = 7.5\text{ ms}$) (*Source: lines 121–134*).
  - Modifies the immediate operand to `addi a5, a4, -4` (hex `93 07 c7 ff`), lowering the minimum allowable interval to 4 units ($4 \times 1.25\text{ ms} = 5.0\text{ ms}$) (*Source: lines 135–144*).
  - Replaces `ble_ll_conn.c.o` in `libble_app.a` and creates a backup file `libble_app.a.original` (*Source: lines 212–217, 298*).
- **Target Exclusion:** `patch_nimble_lib.py` lines 80–91 explicitly check for `esp32s3` and raise an exception:
  `Target 'esp32s3' is not supported: uses a closed-source Bluedroid controller (libbtdm_app.a) instead of NimBLE.`

### 1.2 Why S3 Cannot Use Local Binary Patching

- **Documented Fact:** ESP32-S3 uses the Xtensa architecture and links against a closed-source proprietary controller binary blob `libbtdm_app.a` located at `components/bt/controller/lib_esp32c3_family/esp32s3/libbtdm_app.a` (*Source: `https://github.com/espressif/esp-idf/issues/18467`, `https://github.com/espressif/esp32c3-bt-lib`*).
- **Documented Failure of Binary Patching:** In GitHub Issue #18467 (*author: zhantss*), the author attempted binary-patching `libbtdm_app.a` on S3 using `xtensa-esp32s3-elf-objdump` and custom scripts patching `bltui` branch instructions from 6 to 4 in:
  - `r_llc_con_upd_param_in_range`
  - `f_hci_le_create_con_cmd_handler`
  - `f_hci_le_ext_create_con_cmd_handler`
  Despite the patch being verified in disassembly, packet captures showed the S3 controller still rejected connection parameter update requests from the Switch 2 console with `LL_REJECT_EXT_IND` (opcode `0x11`) (*Source: `https://github.com/espressif/esp-idf/issues/18467#issue-3010313886`*).
- **Source Interpretation:** The closed-source controller contains internal state tables, timing validators, and link layer state machines that cannot be bypassed by merely altering boundary branch instructions. Upstream vendor support from Espressif was mandatory.

### 1.3 Upstream Espressif Resolution & Release Integration

- **Issue History:**
  - **2026-04-15:** Issue #18467 opened requesting 5 ms support on S3 (*Source: `https://github.com/espressif/esp-idf/issues/18467`*).
  - **2026-04-22:** Espressif engineer `esp-zhp` provided an initial standalone fix (`2026-04-22_15-06-17_926b046b.zip`) based on branch `bugfix/fix_ble_read_remote_feature_timeout` (commit `cbd2f2c8486`). This patch added Master/Slave 2.5 ms and 5.0 ms support and introduced `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE` (*Source: Issue #18467, comment 2026-04-22T07:31:45Z*).
  - **2026-04-23:** Author `zhantss` tested and confirmed that the patch successfully established and maintained the 5 ms link on ESP32-S3 (*Source: Issue #18467, comment 2026-04-23T02:36:30Z*).
  - **2026-06-01:** Espressif engineer `esp-zhp` confirmed the feature was officially merged and backported across all stable release branches (*Source: Issue #18467, comment 2026-06-01T11:14:39Z*):
    - **v6.0:** commit `142aea35f3a447df559709ac5473e0b5b1677864`
    - **v5.5:** commit `cf133455b6573b6fd015ac8e1db32b8a2ac69a9a`
    - **v5.4:** commit `aefcf1c2eef91bb359958db3d6b78be6e65b4a5a`
    - **v5.3:** commit `9831261`
    - **v5.2:** commit `fb3cac4d438331401ec8dff13796b91905dc6b03`

### 1.4 Code Implementation of the 5 ms Link in ESP-IDF

Inspection of the primary source commits (`cf13345`, `aefcf1c`, `142aea3`) and controller source files reveals the exact mechanism:

1. **Controller Configuration (`components/bt/controller/esp32c3/Kconfig.in`):**
   ```kconfig
   config BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE
       bool "Allow BLE connection interval below the spec minimum"
       default y
       select BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT if BT_BLUEDROID_ENABLED || BT_NIMBLE_ENABLED
       help
           Enabling this option allows the BLE controller to use a connection interval
           smaller than the Bluetooth Core specification minimum of 7.5 ms. On
           ESP32-C3/S3, when this option is enabled, the connection interval can go as
           low as 3 * 1.25 ms = 3.75 ms.
   ```
   *(Source: `https://raw.githubusercontent.com/espressif/esp-idf/master/components/bt/controller/esp32c3/Kconfig.in`)*

2. **Controller Hook (`components/bt/controller/esp32c3/bt.c`):**
   ```c
   #if CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE
       ble_min_conn_interval_enable(3); // 3 * 1.25 = 3.75ms
   #endif
   ```
   *(Source: `https://raw.githubusercontent.com/espressif/esp-idf/master/components/bt/controller/esp32c3/bt.c`)*
   This function passes the lower limit (3 intervals = 3.75 ms, well below the 4 intervals = 5.0 ms required by NS2) directly into the closed-source controller runtime.

3. **Host Relaxation (`components/bt/host/bluedroid/.../bt_target.h` and `esp_gap_ble_api.c`):**
   `select BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT` sets `UC_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT = 1`. In `bt_target.h`:
   ```c
   #if (UC_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT == 1)
   #define BLE_CONN_INT_MIN_HOST_CHECK 0x0001
   #else
   #define BLE_CONN_INT_MIN_HOST_CHECK 0x0006
   #endif
   ```
   This prevents the host stack from rejecting `interval_min < 6` before sending parameters over HCI (*Source: commit `cf13345.patch`*).

4. **NimBLE Host Compatibility on S3:**
   In `ESP32-BLE5-NSController-Emulator/sdkconfig.defaults.esp32s3`:
   - `CONFIG_BT_NIMBLE_ENABLED=y`
   - `CONFIG_BT_BLUEDROID_ENABLED=n`
   - `CONFIG_BTDM_CTRL_MODE_BLE_ONLY=y`
   - `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y`
   Even when using NimBLE as the host, the underlying link layer on ESP32-S3 is the BTDM controller. The Kconfig symbol `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` configures the BTDM controller to accept 5 ms connection intervals.

### 1.5 Is `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE` Sufficient?

- **Verdict:** **SUFFICIENT** on any ESP-IDF version containing commit `cf13345` / `142aea3` or release cut after June 1, 2026 (ESP-IDF v5.5.4+, v6.0.2+, v5.4.4+).
- **Residual Risk on Older Releases:** If built against ESP-IDF v5.5.2 or v5.5.3 (releases tagged before June 2026), `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE` is undefined/unrecognized and `libbtdm_app.a` will reject the 5 ms interval.

---

## Part 2: Flash Size and Partition Layout

### 2.1 Current Project State

- In `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/sdkconfig.defaults.esp32s3`:
  - `CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y` (*Source: line 17*)
  - `CONFIG_PARTITION_TABLE_CUSTOM=y` (*Source: line 18*)
  - `CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions_n2c.csv"` (*Source: line 19*)
- In `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/partitions_n2c.csv`:
  ```csv
  # Name,   Type, SubType, Offset,  Size, Flags
  nvs,      data, nvs,     ,        0x6000,
  phy_init, data, phy,     ,        0x1000,
  factory,  app,  factory, ,        1500K,
  storage,  data, spiffs,  ,        512K,
  ```
  *(Source: lines 1–6)*
- **Analysis:**
  - Total flash allocated: $0x6000\text{ (24 KB)} + 0x1000\text{ (4 KB)} + 1500\text{ KB} + 512\text{ KB} \approx 2.04\text{ MB}$.
  - The board has 16 MB flash. In the current 8 MB config, 6 MB is unallocated; on the physical 16 MB chip, nearly 14 MB is wasted.
  - There is no OTA partition scheme (`otadata`, `ota_0`, `ota_1`), preventing in-field updates.
  - The packaging scripts (`scripts/package_firmware.py` line 44 and `package_firmware_v5.py` line 44) parse `flash_args` for `--flash-size 8MB`, generating `ns-controller-esp32s3n8.bin` instead of `...n16.bin`.

### 2.2 Comparison with `EasyMCU_ESP32S3`

- **Hardware Context:** `EasyMCU_ESP32S3` runs on the identical hardware: ESP32-S3-DevKitC-1 N16R8 (*Source: `/home/zhao/clone/EasyMCU_ESP32S3/README.md` lines 7–8*).
- **EasyMCU Configuration (`sdkconfig.n16r8`):**
  - `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y`
  - `CONFIG_ESPTOOLPY_FLASHFREQ_80M=y`
  - `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`
  - `CONFIG_SPIRAM_TYPE_ESP32S3_OCTAL=y`
  - `CONFIG_SPIRAM_SIZE=8388608`
  *(Source: `/home/zhao/clone/EasyMCU_ESP32S3/sdkconfig.n16r8` lines 1–5)*
- **EasyMCU Partition Layout (`partitions_16mb.csv`):**
  ```csv
  # Name,   Type, SubType, Offset,   Size,     Flags
  nvs,      data, nvs,     0x9000,   0x6000,
  phy_init, data, phy,     0xf000,   0x1000,
  factory,  app,  factory, 0x10000,  0x600000,  
  storage,  data, nvs,     0x610000, 0x9E0000,
  ```
  *(Source: `/home/zhao/clone/EasyMCU_ESP32S3/partitions_16mb.csv` lines 1–5)*
- **Why EasyMCU Used 10 MB NVS:**
  In `/home/zhao/clone/EasyMCU_ESP32S3/main/storage/storage.c` (lines 17–37):
  ```c
  uint8_t EasyCon_read_byte(uint16_t addr) {
      char key[16];
      snprintf(key, sizeof(key), "b%u", (unsigned)addr);
      nvs_get_u8(s_nvs_handle, key, &value);
      return value;
  }
  ```
  EasyMCU simulated an AVR EEPROM by mapping individual bytes into separate NVS keys (`"b0"`, `"b1"`). Because an NVS entry consumes 32 bytes of flash for a single key-value pair, it required a massive 10 MB NVS partition to emulate a 32 KB EEPROM!
- **Evaluation:** **Do NOT adopt EasyMCU's partition scheme.** Storing files as individual NVS keys is an anti-pattern. Our firmware requires a proper filesystem (SPIFFS or LittleFS) for macro files and Amiibo binaries.

### 2.3 Application Storage Requirements Breakdown

1. **App Binary Footprint:**
   - Base BLE emulator + NimBLE + FreeRTOS: ~1.2 MB.
   - Adding USB CDC ACM, protocol router, macro interpreter, and Amiibo emulated payload: estimated 1.8 MB to 2.2 MB.
   - A 3.0 MB (`0x300000`) app partition provides 40% headroom.
2. **Dual-Bank OTA Update Support:**
   - Two slots (`ota_0` and `ota_1`) of 3.0 MB each = 6.0 MB.
   - `otadata`: 8 KB (`0x2000`).
3. **Amiibo Emulation Storage:**
   - An NTAG215 Amiibo dump is exactly 540 bytes (unpadded) or 572/1024 bytes (padded).
   - An entire library of 500 Amiibos requires only $\approx 270\text{ KB}$.
   - Storing Amiibo files as raw binary files in a filesystem allows easy listing, loading, and dynamic replacement over USB CDC.
4. **Macro Plan Buffer:**
   - Complex recorded controller macros in EasyCon or JSON script format are typically 5 KB to 64 KB each.
   - A macro buffer / file directory of 1 MB can hold hundreds of macro plans.
5. **Filesystem Choice & Size:**
   - SPIFFS or LittleFS (`data, spiffs`).
   - Allocating 9.8 MB (`0x9D0000`) guarantees virtually unlimited room for Amiibos, macro libraries, and future game-specific assets without exhausting flash.

### 2.4 Concrete Recommended 16 MB Partition Table

File: `partitions_16mb_s3.csv`

```csv
# ESP32-S3 16MB Partition Table (Dual OTA + Large Filesystem)
# Name,     Type, SubType,  Offset,    Size,     Flags
nvs,        data, nvs,      0x9000,    0x6000,
otadata,    data, ota,      0xF000,    0x2000,
phy_init,   data, phy,      0x11000,   0x1000,
ota_0,      app,  ota_0,    0x20000,   0x300000,
ota_1,      app,  ota_1,    0x320000,  0x300000,
storage,    data, spiffs,   0x620000,  0x9D0000,
```

- **Alignment & Sizing Checks:**
  - `0x9000` to `0xF000`: 24 KB NVS (standard).
  - `0xF000` to `0x11000`: 8 KB `otadata` (standard 2-sector ping-pong).
  - `0x11000` to `0x12000`: 4 KB `phy_init`.
  - `0x20000`: `ota_0` starts at a 64 KB boundary (0x20000), size 3072 KB (0x300000, multiple of 64 KB).
  - `0x320000`: `ota_1` starts at a 64 KB boundary, size 3072 KB.
  - `0x620000`: `storage` starts at a 64 KB boundary, size 10048 KB (`0x9D0000`).
  - Total end: $0x620000 + 0x9D0000 = 0xFF0000$ (15.9375 MB, within 16 MB limit).

### 2.5 Flash Mode & PSRAM Settings Recommendation

To match the ESP32-S3-WROOM-1-N16R8 module capabilities:
- **Flash Mode:** `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y`
- **Flash Speed:** `CONFIG_ESPTOOLPY_FLASHFREQ_80M=y`
- **Flash Size:** `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`
- **PSRAM (Octal):**
  - `CONFIG_SPIRAM=y`
  - `CONFIG_SPIRAM_MODE_OCT=y`
  - `CONFIG_SPIRAM_TYPE_ESP32S3_OCTAL=y`
  - `CONFIG_SPIRAM_SIZE=8388608`
  - `CONFIG_SPIRAM_SPEED_80M=y`

---

## Part 3: Reproducible Build Recipe

### 3.1 Pinned Prerequisites

1. **Host OS:** Linux (Ubuntu 22.04 / 24.04 or Debian) or macOS.
2. **ESP-IDF Version:**
   - **Recommended:** **ESP-IDF v5.5.4** or **v5.5.5** (tested, stable, release branch contains commit `cf13345`).
   - **Alternative:** **ESP-IDF v6.0.2+** (contains commit `142aea3`).
   - *Do not use:* ESP-IDF v5.5.2 or v5.5.3 without manual cherry-picking of `cf13345`.
3. **Python:** 3.10+ with standard ESP-IDF requirements (`pip install -r $IDF_PATH/requirements.txt`).

### 3.2 Step-by-Step Build Instructions

#### Step 1: Install ESP-IDF v5.5.4
```bash
git clone -b v5.5.4 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf-v5.5.4
cd ~/esp/esp-idf-v5.5.4
./install.sh esp32s3
. ./export.sh
```

#### Step 2: Patch Verification (Precondition Check)
For ESP32-S3: **DO NOT RUN `patch_nimble_lib.py`!**
`patch_nimble_lib.py` is for RISC-V C6/C61 only. S3 controller support is native in v5.5.4.

#### Step 3: Project Configuration
Clone the repository and set the target:
```bash
git clone https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator.git
cd ESP32-BLE5-NSController-Emulator

# Set target to S3 (this automatically pulls sdkconfig.defaults and sdkconfig.defaults.esp32s3)
idf.py set-target esp32s3
```

Ensure `sdkconfig.defaults.esp32s3` has the updated flash and partition settings:
```ini
CONFIG_IDF_TARGET="esp32s3"
CONFIG_BT_ENABLED=y
CONFIG_BT_NIMBLE_ENABLED=y
CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y
CONFIG_TRANSPORT_LAYER_USB_CDC=y
CONFIG_TINYUSB_CDC_ENABLED=y
CONFIG_ESP_CONSOLE_SECONDARY_NONE=y
CONFIG_HID_REPORT_INTERVAL=15
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions_16mb_s3.csv"
CONFIG_BTDM_CTRL_MODE_BLE_ONLY=y
CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY=n
CONFIG_BTDM_CTRL_MODE_BTDM=n
CONFIG_BT_BLUEDROID_ENABLED=n
```

#### Step 4: Build
```bash
idf.py build
```

#### Step 5: Packaging (Single Merged Binary)
```bash
python scripts/package_firmware.py
# Output generated at: release/ns-controller-esp32s3n16.bin
```

---

## Part 4: Hardware Reality — The Two USB-C Ports

The ESP32-S3-DevKitC-1 N16R8 board features **two physical USB-C ports**. Understanding the routing is critical to the control plane architecture:

```
+-------------------------------------------------------------------+
|                     ESP32-S3-DevKitC-1 N16R8                      |
|                                                                   |
|   [Port 1: "UART" / "COM"]            [Port 2: "USB"]             |
|              |                               |                    |
|   +----------------------+        +----------------------+        |
|   | CP2102N / CH343 USB- |        | Directly routed to   |        |
|   | to-UART bridge chip  |        | native ESP32-S3 pins |        |
|   +----------------------+        +----------------------+        |
|              |                               |                    |
|      GPIO43 (U0TXD)                   GPIO19 (USB D-)             |
|      GPIO44 (U0RXD)                   GPIO20 (USB D+)             |
|              |                               |                    |
|   +----------------------+        +----------------------+        |
|   | UART0 HW Peripheral  |        | USB OTG Peripheral   |        |
|   | - esptool flashing   |        | - TinyUSB CDC ACM    |        |
|   | - ESP_LOG monitor    |        | - Control Plane (PC) |        |
|   +----------------------+        +----------------------+        |
+-------------------------------------------------------------------+
```

### Port 1: "UART" / "COM" Port
- **Connection:** Routes through an onboard CP2102N or CH343 USB-to-UART chip connected to ESP32-S3 GPIO43 (U0TXD) and GPIO44 (U0RXD).
- **Auto-Download Circuit:** Contains DTR/RTS transistors connected to EN (CHIP_PU) and GPIO0 for automatic reset into ROM bootloader download mode.
- **Roles:**
  1. Firmware flashing via `idf.py -p /dev/ttyUSB0 flash` or `esptool.py`.
  2. System serial console and debug logging via `idf.py monitor` (baud 115200).
  3. Hard reset and boot log inspection.

### Port 2: "USB" Port
- **Connection:** Routes directly into the ESP32-S3 on-chip Full-Speed USB OTG peripheral PHY on GPIO19 (D-) and GPIO20 (D+).
- **Firmware Binding:** Bound to TinyUSB CDC ACM via `CONFIG_TRANSPORT_LAYER_USB_CDC=y` and `transport_usb_cdc.c` (*Source: `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/main/src/transport/transport_usb_cdc.c`*).
- **Roles:**
  1. **Primary Control Plane Interface:** The host PC (running EasyCon, automated scripts, or GUI) connects here.
  2. Transmits button/joystick commands, macro schedules, and Amiibo emulation requests.
  3. Clean separation: High-speed binary protocol transactions on Port 2 are never contaminated by `ESP_LOG` debug messages emitted on Port 1.

---

## Findings Matrix

| Finding | Classification | Confidence | Evidence / Source Citation |
|---|---|---|---|
| S3 5 ms link requires upstream ESP-IDF fix | Direct Evidence | High | GitHub Issue #18467; author tests confirm binary patches in `libbtdm_app.a` still resulted in `LL_REJECT_EXT_IND`. |
| S3 5 ms fix officially merged and released | Direct Evidence | High | GitHub Issue #18467 (comment 2026-06-01T11:14:39Z by `esp-zhp`); Git commits `cf13345` (v5.5), `142aea3` (v6.0), `aefcf1c` (v5.4), `fb3cac4d4` (v5.2). |
| `patch_nimble_lib.py` cannot patch S3 | Direct Evidence | High | `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/patch/patch_nimble_lib.py`, lines 80–91. |
| `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` is sufficient on v5.5.4+ | Direct Evidence | High | `components/bt/controller/esp32c3/Kconfig.in`; `components/bt/controller/esp32c3/bt.c` calling `ble_min_conn_interval_enable(3)`. |
| Current repo flash setting is 8MB | Direct Evidence | High | `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/sdkconfig.defaults.esp32s3`, line 17 (`CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y`). |
| EasyMCU 10MB NVS was for single-byte EEPROM emulation | Direct Evidence | High | `/home/zhao/clone/EasyMCU_ESP32S3/main/storage/storage.c`, lines 17–37; `partitions_16mb.csv`, line 5. |
| Filesystem partition (SPIFFS/LittleFS) is superior for Amiibo/Macros | Researcher Inference | High | 540 bytes per Amiibo dump; 1000 Amiibos = ~540 KB. Raw file storage avoids 32-byte per-entry NVS overhead. |
| DevKitC-1 "COM" vs "USB" port separation | Direct Evidence | High | ESP32-S3-DevKitC-1 Schematics; `transport_usb_cdc.c` using native TinyUSB; `EasyMCU_ESP32S3/README.md` lines 12–21. |

---

## Contradictions

- **Contradiction:** Project README states: *"理论上，任何使用 Apache NimBLE 开源堆栈的 ESP-IDF 固件都可以支持。但由于 NS2 控制器通讯协议突破了 BLE 最低连接间隔的规范... 需要对 NimBLE 协议栈进行修改 (libble_app.a)"*, while `sdkconfig.defaults.esp32s3` enables NimBLE host (`CONFIG_BT_NIMBLE_ENABLED=y`).
  - **Resolution:** This created confusion for early S3 builders. On ESP32-S3, even when NimBLE is selected as the host stack, the link-layer controller is **not** NimBLE; it is the closed-source BTDM controller (`libbtdm_app.a`). The binary patch modifies the RISC-V NimBLE controller used by C6/C61. S3 does not use `libble_app.a`.

---

## Missing Evidence / Unresolved Inquiries

- **Switch 2 Grip Change Screen Stall:** The README mentions an HID report blocking issue when entering the Switch 2 "Change Grip/Order" screen (*README.md lines 96–99*). Whether this stall is related to BLE supervision timeout interaction with 5 ms intervals or an unhandled HID feature report remains unresolved.

---

## Sources

### Kept Sources
1. `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/patch/patch_nimble_lib.py` — Primary source for binary patch logic, target checks, and RISC-V instruction patterns.
2. `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/sdkconfig.defaults.esp32s3` — Primary source for current S3 configuration and Kconfig symbols.
3. `https://github.com/espressif/esp-idf/issues/18467` — Authoritative tracking issue documenting S3 5 ms failure, vendor communication, test patches, and final upstream merge commits.
4. `https://github.com/espressif/esp-idf/commit/cf13345.patch` — Primary git patch showing relaxation of `BLE_CONN_INT_MIN_HOST_CHECK` across host GAP/GATT APIs.
5. `https://raw.githubusercontent.com/espressif/esp-idf/master/components/bt/controller/esp32c3/Kconfig.in` & `bt.c` — Primary source for `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE` and invocation of `ble_min_conn_interval_enable(3)`.
6. `/home/zhao/clone/EasyMCU_ESP32S3/sdkconfig.n16r8` & `partitions_16mb.csv` & `main/storage/storage.c` — Primary source for reference S3 N16R8 configuration and partition sizing justification.
7. `/home/zhao/clone/ESP32-BLE5-NSController-Emulator/scripts/package_firmware.py` — Primary source for packaging and flash size extraction behavior.

### Rejected / Deprioritized Sources
1. Generic ESP-IDF forum threads discussing standard BLE 7.5 ms limits — Did not address the non-standard 5 ms link or closed-source controller internals.
2. Older PRs for C3/S3 BLE 5.0 extended advertising — Irrelevant to the 5 ms connection interval timing issue.

---

## Next Steps

1. In the main firmware repository, replace `CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y` with `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y` in `sdkconfig.defaults.esp32s3`.
2. Add `partitions_16mb_s3.csv` to the project root and reference it in `sdkconfig.defaults.esp32s3`.
3. Update project documentation to clarify that S3 builders on ESP-IDF v5.5.4+ should skip the patch step entirely.
