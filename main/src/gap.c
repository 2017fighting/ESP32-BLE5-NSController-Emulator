#include "device.h"
#include "controller/hid_controller.h"
#include "utils.h"

#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
#include "protocol/control/control_parser.h"
#include "protocol/control/control_protocol.h"
#endif

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/timers.h"

// §7.6 defect 1: `ble_advertise()` composes `ESP_LOGI` format strings and a
// 30-byte advertising buffer, and running it on the timer service task — whose
// stack is `CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH` (2048 B, shared by every
// timer callback in the firmware) — aborted the device with `rst:0xc` on 8 of 19
// disconnects. The work therefore runs on this task, sized for it; the timer
// keeps only the cancellable 3 s one-shot delay it is good at.
static TimerHandle_t s_restart_adv_timer = NULL;
static TaskHandle_t  s_restart_adv_task  = NULL;

static void restart_adv_task(void* arg) {
  while (1) {
    // Coalesces: two disconnects inside the window are one advert.
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    ble_advertise();
    // The margin the fix relies on, made visible rather than assumed. The
    // high-water mark is in `StackType_t` units, so it is scaled to bytes.
    ESP_LOGD(LOG_BLE_GAP, "advertise-restart task free stack, min ever: %u B",
      (unsigned)(uxTaskGetStackHighWaterMark(NULL) * sizeof(StackType_t)));
  }
}

static void restart_adv_timer_cb(TimerHandle_t xTimer) {
  // Hand off; the timer task must stay as light as every other callback's.
  if (s_restart_adv_task != NULL) {
    xTaskNotifyGive(s_restart_adv_task);
  }
}

static void print_conn_desc(struct ble_gap_conn_desc* desc) {
  ESP_LOGD(LOG_BLE_GAP, "handle=%d our_ota_addr_type=%d our_ota_addr=", 
    desc->conn_handle, desc->our_ota_addr.type);
  log_print_addr(desc->our_ota_addr.val);
  ESP_LOGD(LOG_BLE_GAP, " our_id_addr_type=%d our_id_addr=",
    desc->our_id_addr.type);
  log_print_addr(desc->our_id_addr.val);
  ESP_LOGD(LOG_BLE_GAP, " peer_ota_addr_type=%d peer_ota_addr=",
    desc->peer_ota_addr.type);
  log_print_addr(desc->peer_ota_addr.val);
  ESP_LOGD(LOG_BLE_GAP, " peer_id_addr_type=%d peer_id_addr=",
    desc->peer_id_addr.type);
  log_print_addr(desc->peer_id_addr.val);
  ESP_LOGD(LOG_BLE_GAP, " conn_itvl=%d conn_latency=%d supervision_timeout=%d "
    "encrypted=%d authenticated=%d bonded=%d\n",
    desc->conn_itvl, desc->conn_latency,
    desc->supervision_timeout,
    desc->sec_state.encrypted,
    desc->sec_state.authenticated,
    desc->sec_state.bonded);
}

int handle_gap_event(struct ble_gap_event* event, void* arg) {
  struct ble_gap_conn_desc desc;
  int rc;

  switch(event->type) {
    case BLE_GAP_EVENT_CONNECT:
      if (event->connect.status == 0) {
        rc = ble_gap_conn_find(event->connect.conn_handle, &desc);
        assert(rc == 0);
        print_conn_desc(&desc);
        if (g_device_status == DEV_ADV_IND) {
          g_console_ns2.ble_addr.type = desc.peer_ota_addr.type;
          memcpy(g_console_ns2.ble_addr.val, desc.peer_ota_addr.val, 6);
          ESP_LOGI(LOG_BLE_GAP, "connected, set nintendo switch addr, addr=");
          log_print_addr(g_console_ns2.ble_addr.val);
          struct ble_gap_upd_params params;
          memset(&params, 0, sizeof(params));
          // ESP-IDF 5.5.3, maybe esp-idf support min connection interval
          params.itvl_min = 6;
          params.itvl_max = desc.conn_itvl;
          params.latency = 0;
          params.supervision_timeout = desc.supervision_timeout;
          rc = ble_gap_update_params(event->connect.conn_handle, &params);
          if (rc != 0) {
            ESP_LOGE(LOG_BLE_GAP, "failed to update connection parameters, rc=%d", rc);
          }
        } else {
          ESP_LOGE(LOG_BLE_GAP, "device not ready, reset device");
          device_status_set(DEV_BOOT);
        }
        // cancel pending restart advertising timer
        if (s_restart_adv_timer != NULL) {
          xTimerStop(s_restart_adv_timer, 0);
        }
#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
        /* §12.2 validation 3 (#35): the console's own connection interval is what
         * the macro's input-to-input latency is compared against, so it has to be
         * in the same INFO capture as the measurement. `main.c` raises only the
         * `control` tag above WARN, so the CONTROL layer logs it. */
        control_notify_console_interval(desc.conn_itvl);
        // §4.1/§3.3: the console link is one of the five axes, and its edge is
        // an EVENT; the container never infers it from the control link.
        control_notify_console_link(CONTROL_CONSOLE_EVENT_CONNECTED, 0);
#endif
      } else {
        // failed, restart advertising
        ESP_LOGE(LOG_BLE_GAP, "connection failed, status=%d, restart advertising",
          event->connect.status);
        ble_advertise();
      }
      return 0;
    case BLE_GAP_EVENT_DISCONNECT:
      ESP_LOGI(LOG_BLE_GAP, "disconnected, reason=%d, restart advertising after 3s", event->disconnect.reason);
#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
      // §3.3: `reason` is a u16 because the one measured value that matters is
      // 531 = 0x0213, which does not fit a byte. The drop changes no mode (§4.7).
      control_notify_console_link(CONTROL_CONSOLE_EVENT_DISCONNECTED,
                                  (uint16_t)event->disconnect.reason);
#endif
      if (s_restart_adv_task == NULL) {
        if (xTaskCreate(restart_adv_task, "restart_adv", 4096, NULL, 4, &s_restart_adv_task) != pdPASS) {
          ESP_LOGE(LOG_BLE_GAP, "failed to create advertise-restart task");
          s_restart_adv_task = NULL;
        }
      }
      if (s_restart_adv_timer == NULL) {
        s_restart_adv_timer = xTimerCreate("restart_adv", pdMS_TO_TICKS(3000), pdFALSE, NULL, restart_adv_timer_cb);
      }
      if (s_restart_adv_timer != NULL) {
        xTimerReset(s_restart_adv_timer, 0);
      }
      // stop hid task
      g_hid_controller.ops->stop_task(&g_hid_controller);
      return 0;
    case BLE_GAP_EVENT_CONN_UPDATE:
      ESP_LOGD(LOG_BLE_GAP, "connection updated, conn_handle=%d, status=%d", 
        event->conn_update.conn_handle, event->conn_update.status);
      return 0;
    case BLE_GAP_EVENT_CONN_UPDATE_REQ:
      ESP_LOGD(LOG_BLE_GAP, "connection update request, conn_handle=%d", event->conn_update_req.conn_handle);
      // set conn params
      *event->conn_update_req.self_params = *event->conn_update_req.peer_params;
      return 0;
    case BLE_GAP_EVENT_ADV_COMPLETE:
      ESP_LOGI(LOG_BLE_GAP, "adv complete");
      return 0;
    case BLE_GAP_EVENT_ENC_CHANGE:
      ESP_LOGI(LOG_BLE_GAP, "encryption change event; status=%d ", event->enc_change.status);
      rc = ble_gap_conn_find(event->enc_change.conn_handle, &desc);
      assert(rc == 0);
      print_conn_desc(&desc);
      return 0;
    case BLE_GAP_EVENT_PASSKEY_ACTION:
      ESP_LOGD(LOG_BLE_GAP, "passkey action event; action=%d", event->passkey.params.action);
      return 0;
    case BLE_GAP_EVENT_NOTIFY_TX:
      ESP_LOGD(LOG_BLE_GAP, "notify_tx event; conn_handle=%d attr_handle=%d "
        "status=%d is_indication=%d",
        event->notify_tx.conn_handle,
        event->notify_tx.attr_handle,
        event->notify_tx.status,
        event->notify_tx.indication);
      return 0;
    case BLE_GAP_EVENT_SUBSCRIBE:
      ESP_LOGI(LOG_BLE_GAP, "subscribe event; conn_handle=0x00%02x attr_handle=0x00%02x "
        "reason=%d prevn=%d curn=%d previ=%d curi=%d\n",
        event->subscribe.conn_handle,
        event->subscribe.attr_handle,
        event->subscribe.reason,
        event->subscribe.prev_notify,
        event->subscribe.cur_notify,
        event->subscribe.prev_indicate,
        event->subscribe.cur_indicate);
      // cccd subscribe
      subscribe_entry_set(event->subscribe.attr_handle, 
        event->subscribe.conn_handle,
        event->subscribe.cur_notify == 1,
        event->subscribe.cur_indicate == 1);
      
      // 0x000e init hid report
      if (event->subscribe.attr_handle == 0x000e && event->subscribe.cur_notify == 1) {
        // reset hid report buffer
        g_hid_controller.ops->hid_reset(&g_hid_controller);
        // start hid task
        g_hid_controller.ops->start_task(&g_hid_controller);
#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
        // §3.3/§4.7: a re-subscribe re-arms neutral and the pass continues at
        // its current frame; it is a console-link edge, not a mode change.
        control_notify_console_link(CONTROL_CONSOLE_EVENT_RESUBSCRIBED, 0);
#endif
      }
      break;
    case BLE_GAP_EVENT_MTU:
      ESP_LOGD(LOG_BLE_GAP, "mtu changed, conn_handle=%d, channel_id=%d, mtu=%d", 
        event->mtu.conn_handle, event->mtu.channel_id, event->mtu.value);
      return 0;
    case BLE_GAP_EVENT_REPEAT_PAIRING:
      rc = ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc);
      assert(rc == 0);
      ble_store_util_delete_peer(&desc.peer_id_addr);
      return BLE_GAP_REPEAT_PAIRING_RETRY;
    case BLE_GAP_EVENT_PARING_COMPLETE:
      ESP_LOGD(LOG_BLE_GAP, "paring complete event; status=%d",
        event->pairing_complete.status);
      return 0;
    case BLE_GAP_EVENT_AUTHORIZE:
      ESP_LOGD(LOG_BLE_GAP, "authorize event; conn_handle=%d", event->authorize.conn_handle);
      return 0;
    default:
      break;
  }
  return 0;
}