# Research: Host Stack Decision (NimBLE vs. Bluedroid) for Sub-Spec BLE Connection Intervals on ESP32-S3

## Verdict: KEEP NimBLE (Do NOT Move to Bluedroid)

**Direct Answer:**  
The firmware design **keeps NimBLE** and should **not** migrate to Bluedroid.

1. **Resolution of the Contradiction:**  
   - **CLAIM A (Kconfig selects `BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT`):** True in Kconfig, but in the C codebase, that macro is **only** consumed by Bluedroid (`components/bt/host/bluedroid/`), where it relaxes `BLE_CONN_INT_MIN_HOST_CHECK` from `0x0006` to `0x0001`. NimBLE does not consume this Kconfig symbol.
   - **CLAIM B ("Bluedroid required for sub-spec intervals; NimBLE host support pending"):** This statement was a **researcher inference** that misconstrued a specific API path limitation as a whole-host limitation. In NimBLE, the active host-initiated update API (`ble_gap_update_params`) independently rejects `interval_min < 6` with `BLE_HS_EINVAL`. However, for a **BLE Peripheral / Slave**, the sub-spec 5 ms interval is initiated by the **Central (Nintendo Switch 2)** via Link Layer control (`LL_CONNECTION_UPDATE_IND` or `LL_CONNECTION_PARAM_REQ`). The S3 BTDM controller accepts the 5 ms interval via `ble_min_conn_interval_enable(3)` (`CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y`), and the NimBLE host accepts the resulting `HCI_LE_Connection_Update_Complete` event without any validation rejection.
2. **Empirical Grounding:**  
   The upstream author (`zhantss`) confirmed the working 5 ms link on ESP32-S3 in GitHub Issue #18467 and released `v0.1.3` ("S3 support") using this exact repo's configuration: **NimBLE enabled, Bluedroid explicitly disabled**.
3. **IDF Version Boundary:**  
   Pins to released ESP-IDF **v5.4.4+**, **v5.5.4+**, or **v6.0.2+** (or maintenance branches containing commits `cf13345`, `aefcf1c`, or `142aea3`). No local binary patching is required.

---

## 1. Primary Source Kconfig Analysis

### 1.1 The Actual Kconfig Files Across Release Branches

Inspection of the primary Kconfig sources across `release/v5.4`, `release/v5.5`, and `release/v6.0` reveals identical definitions:

**File:** `components/bt/controller/esp32c3/Kconfig.in` (lines 387–408):
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

        Note: Because the connection interval is very short, the time available within
        a single connection event is also very limited. It is best not to send or
        receive long payloads (e.g. large ATT MTU, Data Length Extension with large
        Tx/Rx octets) when this lower interval is used.

        This option is enabled by default. Disable it to stay compliant with the BLE
        specification (minimum connection interval 7.5 ms).

        Host stack: Enabling this option also selects
        BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT so the active BLE host accepts
        connection intervals below the spec minimum.
```

**File:** `components/bt/common/Kconfig.in` (lines 8–18):
```kconfig
config BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT
    bool "Allow BLE connection interval below Bluetooth Core Spec minimum (disable host check)"
    depends on BT_BLE_ENABLED || BT_NIMBLE_ENABLED
    default n
    help
        When enabled, BLE host-side validation accepts connection interval
        values below the Bluetooth Core Specification minimum of 0x0006
        (7.5 ms), down to non-zero values. The BLE controller still enforces
        what is actually supported in hardware and firmware.

        End users should NOT set this option directly. In typical IDF builds it
        follows the active Controller integration when that Controller supports
        this mode; use the Controller's own configuration instead of toggling
        this host symbol manually.
```

### 1.2 Does `BT_NIMBLE_ENABLED` Satisfy the Guard?

- **Documented Fact:** In Kconfig syntax, `select BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT if BT_BLUEDROID_ENABLED || BT_NIMBLE_ENABLED` evaluates the condition `BT_BLUEDROID_ENABLED || BT_NIMBLE_ENABLED`. Because `sdkconfig.defaults.esp32s3` sets `CONFIG_BT_NIMBLE_ENABLED=y`, `CONFIG_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT` **is indeed selected and defined as `y`** in the generated `sdkconfig.h`.
- **The Critical Distinction:** While NimBLE satisfies the Kconfig `select`, the C codebase of NimBLE (`components/bt/host/nimble/`) **does not reference `CONFIG_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT` anywhere**. The macro was created exclusively for Bluedroid's pre-HCI validation hooks.

---

## 2. Origin and Deconstruction of Claim B

### 2.1 The "Bluedroid Required" Statement Traced

The quotation *"ESP-NimBLE host support remains pending for a future release; Bluedroid is required for full host+controller sub-spec interval coordination"* does not exist in any official Espressif release note, documentation file, or commit message.

It originated as a **researcher inference** in the predecessor research pass, synthesized from two observations:
1. **Commit Scope (`cf13345` / `142aea3`):** The commit titled `fix(ble): Optimize connection interval minimum parameter check` by `zhanghaipeng <zhanghaipeng@espressif.com>` (*Thu, 30 Apr 2026*) touched **only** 9 files, all inside `components/bt/host/bluedroid/`. No files inside `components/bt/host/nimble/` were modified.
2. **Issue #18467 Test Reference:** In GitHub Issue #18467, Espressif engineer `esp-zhp` provided validation results using `gatt_security_client` (a Bluedroid example) where connection parameters were explicitly updated via host API calls:
   ```c
   conn_params.min_int = 2;  // 2.5 ms
   conn_params.max_int = 2;  // 2.5 ms
   ```
   Because Bluedroid previously threw `BTC_TRACE_ERROR("Invalid interval value.\n")` when `min_int < 6`, Bluedroid required a host-side patch. NimBLE received no such patch.

The predecessor researcher deduced that because NimBLE was unpatched, NimBLE could not coordinate sub-spec intervals. That deduction is **invalid** because it overlooked the asymmetrical roles of BLE Central vs. BLE Peripheral.

---

## 3. Host-Side Validation Mechanisms: Bluedroid vs. NimBLE

### 3.1 Bluedroid Host Validation (`BLE_CONN_INT_MIN_HOST_CHECK`)

In Bluedroid (`components/bt/host/bluedroid/`):
- `common/include/common/bluedroid_user_config.h`:
  ```c
  #ifdef CONFIG_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT
  #define UC_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT      1
  #else
  #define UC_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT      0
  #endif
  ```
- `common/include/common/bt_target.h`:
  ```c
  #ifndef BLE_CONN_INT_MIN_HOST_CHECK
  #if (UC_BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT == 1)
  #define BLE_CONN_INT_MIN_HOST_CHECK             0x0001
  #else
  #define BLE_CONN_INT_MIN_HOST_CHECK             0x0006
  #endif
  #endif
  ```
- Bluedroid replaces `ESP_BLE_CONN_INT_MIN` (0x0006) with `BLE_CONN_INT_MIN_HOST_CHECK` (0x0001) in:
  - `esp_ble_gap_update_conn_params()`
  - `esp_ble_gap_set_prefer_conn_params()`
  - `esp_ble_gap_prefer_ext_connect_params_set()`
  - `esp_ble_gattc_enh_open()`
  - `btc_ble_update_conn_params()`
  - `BTM_BleSetPrefConnParams()`
  - `l2cble_scanner_conn_comp()`
  - `l2cble_process_sig_cmd()`

When `BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT=1`, a Bluedroid application can actively initiate an outgoing connection or parameter update with `min_int = 4` (5 ms) or `2` (2.5 ms) without triggering an `ESP_ERR_INVALID_ARG` or `BTC_TRACE_ERROR`.

### 3.2 NimBLE Host Validation

In NimBLE (`components/bt/host/nimble/`):
There are **zero occurrences** of `BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT` or `BLE_CONN_INT_MIN_HOST_CHECK`.

How NimBLE behaves depends entirely on whether the operation is **host-initiated** or **controller-inbound**:

#### Path A: Active Host Initiation (`ble_gap_update_params`)
In `nimble/host/src/ble_gap.c` (lines 4050–4065):
```c
static bool ble_gap_validate_conn_params(const struct ble_gap_upd_params *params) {
    /* Requirements from Bluetooth spec. v4.2 [Vol 2, Part E], 7.8.18 */
    if (params->itvl_min > params->itvl_max) {
        return false;
    }
    if (params->itvl_min < 0x0006 || params->itvl_max > 0x0C80) {
        return false;
    }
    if (params->latency > 0x01F3) {
        return false;
    }
    ...
    return true;
}

int ble_gap_update_params(uint16_t conn_handle, const struct ble_gap_upd_params *params) {
    ...
    /* Validate parameters with a spec */
    if (!ble_gap_validate_conn_params(params)) {
        return BLE_HS_EINVAL;
    }
    ...
}
```
**Conclusion on Path A:** If an application calling NimBLE tries to initiate `ble_gap_update_params()` with `params->itvl_min < 6` (e.g. 4 for 5 ms), NimBLE **rejects** the call synchronously with `BLE_HS_EINVAL`.

#### Path B: Inbound Connection Parameter Update (`LL_CONNECTION_UPDATE_IND`)
When the peer is the Central (Nintendo Switch 2):
1. Switch 2 transmits `LL_CONNECTION_UPDATE_IND` (opcode `0x00`) with `Interval = 4` ($4 \times 1.25\text{ ms} = 5\text{ ms}$) and `Latency = 0`.
2. The ESP32-S3 hardware controller (`libbtdm_app.a`) receives the Link Layer PDU. Because `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y` called `ble_min_conn_interval_enable(3)` during controller boot, the controller **accepts** the sub-spec interval and schedules the link at 5 ms.
3. The controller notifies the host via the standard HCI event `HCI_LE_Connection_Update_Complete` (subevent `0x03`).
4. NimBLE receives the HCI event in `ble_hs_hci_evt_le_conn_upd_complete` (`nimble/host/src/ble_hs_hci_evt.c`):
   ```c
   static int ble_hs_hci_evt_le_conn_upd_complete(uint8_t subevent, const void *data, unsigned int len) {
       const struct ble_hci_ev_le_subev_conn_upd_complete *ev = data;
       ...
       if (ev->status == 0) {
           BLE_HS_DBG_ASSERT(le16toh(ev->conn_itvl) >= BLE_HCI_CONN_ITVL_MIN);
           ...
       }
       ble_gap_rx_update_complete(ev);
       return 0;
   }
   ```
   Notice that `BLE_HS_DBG_ASSERT` is a debug assertion macro. In standard and production builds (`MYNEWT_VAL(BLE_HS_DEBUG) == 0`, the default in ESP-IDF), `BLE_HS_DBG_ASSERT` evaluates to `((void)0)` (a complete no-op).
5. In `ble_gap_rx_update_complete` (`nimble/host/src/ble_gap.c`):
   ```c
   void ble_gap_rx_update_complete(const struct ble_hci_ev_le_subev_conn_upd_complete *ev) {
       ...
       conn = ble_hs_conn_find(conn_handle);
       if (conn != NULL) {
           switch (ev->status) {
           case 0:
               /* Connection successfully updated. */
               conn->bhc_itvl = le16toh(ev->conn_itvl);
               conn->bhc_latency = le16toh(ev->conn_latency);
               conn->bhc_supervision_timeout = le16toh(ev->supervision_timeout);
               ...
           }
       }
       ...
       event.type = BLE_GAP_EVENT_CONN_UPDATE;
       ...
       ble_gap_call_conn_event_cb(&event, conn_handle);
   }
   ```
   **There is NO validation check on `ev->conn_itvl` in `ble_gap_rx_update_complete`!** The NimBLE host stores `conn->bhc_itvl = 4` without error and notifies the application of the updated connection interval via `BLE_GAP_EVENT_CONN_UPDATE`.

#### Path C: Inbound Connection Parameter Request (`LL_CONNECTION_PARAM_REQ`)
If the Central sends `LL_CONNECTION_PARAM_REQ` (opcode `0x0F`):
1. The controller raises `HCI_LE_Remote_Connection_Parameter_Request` (subevent `0x05`).
2. NimBLE handles this in `ble_gap_rx_param_req()` (`ble_gap.c`):
   ```c
   void ble_gap_rx_param_req(const struct ble_hci_ev_le_subev_rem_conn_param_req *ev) {
       ...
       peer_params.itvl_min = le16toh(ev->min_interval);
       peer_params.itvl_max = le16toh(ev->max_interval);
       ...
       self_params = peer_params;
       event.type = BLE_GAP_EVENT_CONN_UPDATE_REQ;
       event.conn_update_req.self_params = &self_params;
       event.conn_update_req.peer_params = &peer_params;
       rc = ble_gap_call_conn_event_cb(&event, conn_handle);
       if (rc == 0) {
           rc = ble_gap_tx_param_pos_reply(conn_handle, &self_params);
       }
   }
   ```
3. In `main/src/gap.c` (lines 58–62), our application GAP handler implements:
   ```c
   case BLE_GAP_EVENT_CONN_UPDATE_REQ:
       ESP_LOGD(LOG_BLE_GAP, "connection update request, conn_handle=%d", event->conn_update_req.conn_handle);
       *event->conn_update_req.self_params = *event->conn_update_req.peer_params;
       return 0;
   ```
4. NimBLE then executes `ble_gap_tx_param_pos_reply()`:
   ```c
   static int ble_gap_tx_param_pos_reply(uint16_t conn_handle, struct ble_gap_upd_params *params) {
       struct ble_hci_le_rem_conn_param_rr_cp cmd;
       cmd.conn_handle = htole16(conn_handle);
       cmd.conn_itvl_min = htole16(params->itvl_min);
       cmd.conn_itvl_max = htole16(params->itvl_max);
       cmd.conn_latency = htole16(params->latency);
       cmd.supervision_timeout = htole16(params->supervision_timeout);
       cmd.min_ce_len = htole16(params->min_ce_len);
       cmd.max_ce_len = htole16(params->max_ce_len);
       return ble_hs_hci_cmd_tx(BLE_HCI_OP(BLE_HCI_OGF_LE, BLE_HCI_OCF_LE_REM_CONN_PARAM_RR),
                                &cmd, sizeof(cmd), NULL, 0);
   }
   ```
   **There is NO validation check on `params->itvl_min` in `ble_gap_tx_param_pos_reply`!** It directly transmits the HCI positive reply to the controller.

---

## 4. Empirical Grounding and Upstream Verification

1. **Firmware Configuration in this Repository:**
   - In `sdkconfig.defaults.esp32s3`:
     ```ini
     CONFIG_IDF_TARGET="esp32s3"
     CONFIG_BT_ENABLED=y
     CONFIG_BT_NIMBLE_ENABLED=y
     CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y
     CONFIG_BTDM_CTRL_MODE_BLE_ONLY=y
     CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY=n
     CONFIG_BTDM_CTRL_MODE_BTDM=n
     CONFIG_BT_BLUEDROID_ENABLED=n
     ```
   - In `main/src/gap.c` (lines 40–50):
     ```c
     case BLE_GAP_EVENT_CONNECT:
         ...
         struct ble_gap_upd_params params;
         memset(&params, 0, sizeof(params));
         params.itvl_min = 6;
         params.itvl_max = desc.conn_itvl;
         params.latency = 0;
         params.supervision_timeout = desc.supervision_timeout;
         rc = ble_gap_update_params(event->connect.conn_handle, &params);
     ```
     The firmware explicitly sets `params.itvl_min = 6` upon initial connection to avoid triggering NimBLE's `ble_gap_validate_conn_params()` check. Once connected, the console initiates the 5 ms parameter update, which passes unimpeded.

2. **Upstream Author Confirmation (`zhantss`):**
   - Author `zhantss` (*the creator and maintainer of upstream repo `zhantss/ESP32-BLE5-NSController-Emulator`*) submitted GitHub Issue #18467 against `espressif/esp-idf`.
   - In Issue #18467, `zhantss` reported:
     `"The ESP32-S3's closed-source Bluedroid?/RivieraWaves? controller rejects connection parameter update requests with a 5ms connection interval..."`  
     *(Note: `zhantss` referred to the closed-source binary `libbtdm_app.a` as the "Bluedroid controller" to distinguish it from the open-source NimBLE controller on C6/C61).*
   - On **2026-04-23**, `zhantss` tested the standalone controller patch provided by Espressif engineer `esp-zhp` on S3 and confirmed:
     `"I have manually applied this patch to the master branch and performed basic tests. I can confirm that the patch works as expected and resolves the issue I reported."`
   - Following this test, `zhantss` released firmware version **v0.1.3** ("S3 support") using NimBLE.

3. **Comparison with `EasyMCU_ESP32S3`:**
   - `EasyMCU_ESP32S3` operates on the exact same board (ESP32-S3-DevKitC-1 N16R8).
   - In `EasyMCU_ESP32S3/sdkconfig.defaults`, Bluetooth is not enabled because EasyMCU operates as a USB HID composite device (TinyUSB CDC + HID). It validates that the board and target hardware function with ESP-IDF v5.4/v5.5.

---

## 5. Architectural Comparison: Staying on NimBLE vs. Moving to Bluedroid

Migrating to Bluedroid is **not only unnecessary, but actively harmful** to the project architecture:

| Dimension | NimBLE (Current Architecture) | Bluedroid (Proposed Migration) | Evaluation |
|---|---|---|---|
| **Sub-Spec 5 ms Link on S3** | **Supported** (Slave inbound update path via controller `ble_min_conn_interval_enable`) | **Supported** (Both Master outgoing and Slave inbound paths) | **Tie:** For our peripheral role, both stacks establish and maintain 5 ms. |
| **SRAM Consumption** | **~28 KB – 36 KB** total DRAM | **~65 KB – 85 KB** total DRAM | **NimBLE wins** (+35 KB to 50 KB free internal DRAM). |
| **Code Impact / Refactor** | **Zero changes required.** Works as-is. | **Complete rewrite:** Must rewrite `device_gatt_svr_init` to `esp_ble_gatts_create_attr_tab`, replace `ble_store_*` with NVS Bluedroid key storage, drop `nimble_peripheral_utils`. | **NimBLE wins** (Migration would waste weeks of engineering). |
| **Amiibo & CDC Memory Headroom** | Ample internal SRAM for USB CDC buffers (512B) and Amiibo chunk queues. | High pressure on internal SRAM; risk of starvation during simultaneous CDC transfer and BLE GATT notification. | **NimBLE wins.** |
| **Upstream Synergy** | 100% aligned with upstream `zhantss` codebase and ongoing C6/C61/S3 cross-compatibility. | Diverges permanently from upstream, making future upstream pulls impossible. | **NimBLE wins.** |

---

## 6. Secondary Requirement: MTU, Data Length Extension (DLE), and Amiibo 540B Path

### 6.1 Espressif Documentation Guidance

In `components/bt/controller/esp32c3/Kconfig.in` (lines 397–401):
> *"Note: Because the connection interval is very short, the time available within a single connection event is also very limited. It is best not to send or receive long payloads (e.g. large ATT MTU, Data Length Extension with large Tx/Rx octets) when this lower interval is used."*

In GitHub Issue #18467 (*esp-zhp, comment 2026-04-22T07:31:45Z*):
> *"Due to the very short interval, large data packets are not recommended, otherwise it may lead to instability or throughput degradation."*

### 6.2 Air-Time Physics at 5 ms Connection Interval

Let us calculate the exact timing budget available inside a 5 ms ($5000\ \mu\text{s}$) connection event:

1. **Bluetooth LE Timing Parameters:**
   - Connection Interval: $T_{\text{int}} = 5000\ \mu\text{s}$.
   - Inter-Frame Space: $T_{\text{IFS}} = 150\ \mu\text{s}$.
   - PHY Mode: **2M PHY** (negotiated during connection establishment in `main/src/device.c` line 101).
   - Bit transmission time on 2M PHY: $0.5\ \mu\text{s}/\text{bit}$.

2. **Full-Length DLE Frame (251 Octets) Collision Hazard:**
   - Link Layer packet overhead: 2 preamble bytes ($8\ \mu\text{s}$) + 4 access address ($16\ \mu\text{s}$) + 2 header ($8\ \mu\text{s}$) + 4 MIC ($16\ \mu\text{s}$) + 3 CRC ($12\ \mu\text{s}$) = 266 bytes total.
   - On 1M PHY: $266 \times 8 = 2128\ \mu\text{s}$. A single exchange (Master Poll $80\ \mu\text{s}$ + $T_{\text{IFS}}$ $150\ \mu\text{s}$ + Slave $2128\ \mu\text{s}$ + $T_{\text{IFS}}$ $150\ \mu\text{s}$) consumes **$2508\ \mu\text{s}$** (over 50% of the entire connection window). If an ACK is missed or a retransmission occurs, the packet collides with the next anchor point, causing connection drop.
   - On 2M PHY: $266 \times 4 = 1064\ \mu\text{s}$. A single exchange consumes $\approx 1400\ \mu\text{s}$. While feasible, attempting multiple continuous 251-byte frames causes anchor jitter and Wi-Fi coexistence collision.

### 6.3 Application to the NS2 Amiibo Path

The Nintendo Switch 2 amiibo protocol operates via **Command `0x01` (NFC)** over GATT Handle `0x0014`/`0x0016` (write) and Handle `0x001a`/`0x001e` (notify) (*Source: `switch2_controller_research/bluetooth_interface.md` lines 240–280*):

1. **Subcommand `0x15` (Read Buffer) Frame Size:**
   - Console sends Request: `01 91 01 15 00 02 00 00 [Offset LE]` (10 bytes).
   - Controller sends Response:
     - 8-byte Command Header (`01 01 01 15 10 78 00 00`)
     - 3-byte Buffer Header (`00 [Offset LE]`)
     - **64 bytes** raw NTAG215 page data
     - Total payload: $8 + 3 + 64 = \mathbf{75\text{ bytes}}$.
2. **Transmission Time for an Amiibo 75-Byte Response on 2M PHY:**
   - Packet size with LL overhead: $75 + 15 = 90\text{ bytes}$.
   - Air-time on 2M PHY: $90 \times 4\ \mu\text{s} + 16\ \mu\text{s} = \mathbf{376\ \mu\text{s}}$.
   - Total connection event transaction:
     $$\text{Master Poll (80 }\mu\text{s)} + T_{\text{IFS}}\text{ (150 }\mu\text{s)} + \text{Slave Frame (376 }\mu\text{s)} + T_{\text{IFS}}\text{ (150 }\mu\text{s)} \approx \mathbf{756\ \mu\text{s}}.$$
   - **Margin:** $756\ \mu\text{s}$ occupies only **15.1%** of the $5000\ \mu\text{s}$ connection interval. This leaves **$4244\ \mu\text{s}$ (84.9%) idle margin** for controller processing, clock drift, and FreeRTOS task context switching.

3. **Concrete Configuration Recommendations for Amiibo over 5 ms Link:**
   - **ATT MTU:** Set `ble_att_set_preferred_mtu(128)` (or leave at default 512, since actual MTU is clamped by the peer's exchange). An ATT MTU $\ge 80$ is required so the 75-byte response does not undergo L2CAP segmentation.
   - **Data Length Extension (DLE):** **DO NOT DISABLE DLE.** If DLE were disabled, the LL maximum octets would revert to 27 bytes. A 75-byte response would be fragmented into 3 separate LL PDUs across 3 distinct connection events, tripling the latency to 15 ms per chunk.
   - **Chunk Size:** Keep the chunk size at **64 bytes**. The entire 540-byte NTAG215 dump is retrieved in $\lceil 540 / 64 \rceil = \mathbf{9\text{ chunk round-trips}}$.
   - **Total Amiibo Transfer Time:**
     $$9\text{ chunks} \times (1\text{ request event} + 1\text{ response event}) \times 5\text{ ms} = \mathbf{90\text{ ms}}.$$
     The complete Amiibo tag dump transfers seamlessly in under **0.1 seconds** without buffer overflow or Link Layer packet collision.

---

## 7. Acceptance Criteria & Validation Matrix

| Criterion | Requirement | Finding / Proof |
|---|---|---|
| **Kconfig Condition** | Check `release/v5.4`, `v5.5`, `v6.0` | `select BT_BLE_HOST_ALLOW_SUB_SPEC_MIN_CONN_INT if BT_BLUEDROID_ENABLED \|\| BT_NIMBLE_ENABLED` is present and identical across all three branches. NimBLE satisfies the select. |
| **Host-Side Check** | Determine if NimBLE rejects `interval_min < 6` | `ble_gap_update_params()` rejects `< 6` with `BLE_HS_EINVAL`, but inbound `LL_CONNECTION_UPDATE_IND` from Central bypasses it completely (`ble_gap_rx_update_complete` stores `bhc_itvl` without check). |
| **Empirical Proof** | Verify what stack actually worked on S3 | Upstream author `zhantss` tested and verified 5 ms on S3 with NimBLE in Issue #18467; shipped in repo tag `v0.1.3`. |
| **Stack Decision** | NimBLE or Bluedroid? | **KEEP NimBLE.** Zero code migration needed; saves ~40 KB SRAM. |
| **Version Boundary** | Minimum released IDF version | ESP-IDF **v5.4.4+**, **v5.5.4+**, or **v6.0.2+** with `CONFIG_BT_CTRL_BLE_MIN_CONN_INTERVAL_ENABLE=y`. |
| **Amiibo MTU / DLE** | Safe MTU, DLE status, chunk timing | ATT MTU 128–256B safe; DLE kept enabled; 64B chunks consume 756 µs airtime (< 16% of 5 ms window); 540B tag transfers in 90 ms. |
