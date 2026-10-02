#include "transport/transport.h"
#include "buffer/zc_buffer.h"
#include "protocol/protocol.h"
#include "controller/hid_controller.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#ifdef CONFIG_TRANSPORT_LAYER_UART
#include "transport/transport_uart.h"
#include "driver/uart.h"
#endif

#ifdef CONFIG_TRANSPORT_LAYER_USB_SERIAL_JTAG
#include "transport/transport_usb_serial_jtag.h"
#endif

#ifdef CONFIG_TRANSPORT_LAYER_USB_CDC
#include "transport/transport_usb_cdc.h"
#endif

#ifdef CONFIG_PROTOCOL_LAYER_EASYCON
#include "protocol/easycon/easycon_instance.h"
#endif

#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
#include "protocol/control/control_parser.h"
#include "protocol/control/control_protocol.h"
#endif

#include "protocol/control/control_link.h"

/*
 * Ring-buffer size rationale (control-plane build):
 * - The ring is 256 B on purpose. Spec §7.1/§7.5 keep it here and §2.7 depends
 *   on it: at chunk_size = 256 one bulk chunk fills it and the device may ACK
 *   every chunk, which is a permitted outcome rather than a protocol change.
 *   A burst larger than the ring is data loss, not backpressure; whether the
 *   256 B ring absorbs the ACK window is bench ticket #34, not a code guess.
 * - The CONTROL decoder is streaming, so it never needs a whole 512-byte frame
 *   contiguous (see control_parser.c).
 * - For the legacy EasyCon path the original reasoning still applies: host
 *   reports are ~10 bytes, the largest Simple HID frame is 16 bytes, and 256 B
 *   covers >25 frames of jitter while saving RAM against a 1024-byte driver
 *   buffer.
 */
#define TRANSPORT_RX_BUF_SIZE   256
#define TRANSPORT_TX_BUF_SIZE   256

static transport_handle_t g_transport;
static zc_ringbuf_t       g_transport_rx_ringbuf;
static uint8_t            g_transport_rx_buffer[TRANSPORT_RX_BUF_SIZE];
static TaskHandle_t       g_transport_protocol_task = NULL;

static protocol_instance_t *g_protocol_inst = NULL;

static void transport_protocol_task(void *arg)
{
    (void)arg;

    while (1) {
        if (g_protocol_inst == NULL || g_transport.ops == NULL || !g_transport.ops->is_ready(&g_transport)) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
        /* §3.3: an unsolicited EVENT must not wait for the host to send a byte,
         * and `protocol_route` only calls into a parser when the RX ring is
         * non-empty. Drain first, so an event raised while handling the previous
         * request is written after that request's reply (§11 trace A). */
        {
            uint8_t event_wire[CONTROL_WIRE_MAX];
            size_t event_len = control_parser_poll_event(event_wire, sizeof(event_wire));
            if (event_len > 0 && g_transport.ops->submit_tx != NULL) {
                g_transport.ops->submit_tx(&g_transport, event_wire, event_len);
            }
            /* Fall through and service the ring too: the event belongs to an
             * earlier pass, so a reply written below still follows it. */
        }
#endif

        parser_rsp_t rsp = {0};
        parse_result_t result = protocol_route(g_protocol_inst, &g_transport_rx_ringbuf, &rsp);
        ESP_LOGD(LOG_TRANSPORT, "Protocol route result: %d", result);

        if (result == PARSE_OK) {
            if (rsp.len > 0 && g_transport.ops->submit_tx != NULL) {
                g_transport.ops->submit_tx(&g_transport, rsp.data, rsp.len);
            }
        } else if (result == PARSE_NEED_MORE) {
            /* No complete frame yet; yield to let the RX task fill the buffer. */
            vTaskDelay(2);
        } else {
            /* PARSE_INVALID: no parser matched or frame error.
             * Discard one byte to avoid deadlock on stale leading data.
             */
            uint8_t discard;
            zc_read_byte(&g_transport_rx_ringbuf, &discard);
            vTaskDelay(1);
        }
    }
}

int transport_init(void)
{
    memset(&g_transport, 0, sizeof(g_transport));
    memset(g_transport_rx_buffer, 0, sizeof(g_transport_rx_buffer));

    if (zc_init(&g_transport_rx_ringbuf, g_transport_rx_buffer, TRANSPORT_RX_BUF_SIZE, 0) != ESP_OK) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to initialize transport RX ring buffer");
        return -1;
    }

#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
    control_parser_init();
    g_protocol_inst = &control_protocol_instance;
#elif defined(CONFIG_PROTOCOL_LAYER_EASYCON)
    g_protocol_inst = &easycon_protocol_instance;
#else
    g_protocol_inst = NULL;
    ESP_LOGW(LOG_TRANSPORT, "No protocol instance available for current configuration");
#endif

#ifdef CONFIG_TRANSPORT_LAYER_UART
    g_transport.ops = &transport_uart_vtable;

    transport_uart_config_t uart_cfg = {
        .port           = CONFIG_CONTROL_UART_PORT,
        .baud_rate      = CONFIG_CONTROL_UART_BAUD,
        .rx_pin         = CONFIG_CONTROL_UART_RX_PIN,
        .tx_pin         = CONFIG_CONTROL_UART_TX_PIN,
        .rx_buffer_size = TRANSPORT_RX_BUF_SIZE,
        .tx_buffer_size = TRANSPORT_TX_BUF_SIZE,
        .notify_task    = NULL,
    };

    if (g_transport.ops->open(&g_transport, &uart_cfg) != 0) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to open UART transport");
        return -1;
    }

    if (g_transport.ops->activate_rx(&g_transport, &g_transport_rx_ringbuf) != 0) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to activate UART RX");
        g_transport.ops->close(&g_transport);
        return -1;
    }

    /* The shared TX lock and the ESP_LOG hook must be up before the protocol
     * task can write a reply, because the control plane shares UART0 with the
     * log (ADR-0001, §2.2). The driver is installed by now, so the hook has
     * somewhere to write. */
    if (control_link_init((int)uart_cfg.port) != 0) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to initialise the control link");
        g_transport.ops->close(&g_transport);
        return -1;
    }

    ESP_LOGI(LOG_TRANSPORT, "UART transport initialized");
#elif CONFIG_TRANSPORT_LAYER_USB_SERIAL_JTAG
    g_transport.ops = &transport_usb_serial_jtag_vtable;

    transport_usb_serial_jtag_config_t usj_cfg = {
        .rx_buffer_size = TRANSPORT_RX_BUF_SIZE,
        .tx_buffer_size = TRANSPORT_TX_BUF_SIZE,
        .notify_task    = NULL,
    };

    if (g_transport.ops->open(&g_transport, &usj_cfg) != 0) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to open USB Serial/JTAG transport");
        return -1;
    }

    if (g_transport.ops->activate_rx(&g_transport, &g_transport_rx_ringbuf) != 0) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to activate USB Serial/JTAG RX");
        g_transport.ops->close(&g_transport);
        return -1;
    }

    ESP_LOGI(LOG_TRANSPORT, "USB Serial/JTAG transport initialized");
#elif CONFIG_TRANSPORT_LAYER_USB_CDC
    g_transport.ops = &transport_usb_cdc_vtable;

    transport_usb_cdc_config_t usb_cdc_cfg = {
        .rx_buffer_size = TRANSPORT_RX_BUF_SIZE,
        .tx_buffer_size = TRANSPORT_TX_BUF_SIZE,
        .notify_task    = NULL,
    };

    if (g_transport.ops->open(&g_transport, &usb_cdc_cfg) != 0) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to open USB CDC transport");
        return -1;
    }

    if (g_transport.ops->activate_rx(&g_transport, &g_transport_rx_ringbuf) != 0) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to activate USB CDC RX");
        g_transport.ops->close(&g_transport);
        return -1;
    }

    ESP_LOGI(LOG_TRANSPORT, "USB CDC transport initialized");
#else
    ESP_LOGE(LOG_TRANSPORT, "No transport layer selected in configuration");
    return -1;
#endif

#ifdef CONFIG_PROTOCOL_LAYER_CONTROL
    /* §3.3: once the transport can carry it, tell an already-attached container
     * that this boot is ready for HELLO. */
    control_parser_boot_event();
#endif

    return 0;
}

int transport_start(void)
{
    if (g_transport_protocol_task != NULL) {
        ESP_LOGW(LOG_TRANSPORT, "Protocol dispatcher task already started");
        return 0;
    }

    BaseType_t rc;
#if CONFIG_IDF_TARGET_ESP32S3
    rc = xTaskCreatePinnedToCore(transport_protocol_task,
                                 "transport_proto",
                                 4096,
                                 NULL,
                                 4,
                                 &g_transport_protocol_task,
                                 1);
#else
    rc = xTaskCreate(transport_protocol_task,
                     "transport_proto",
                     4096,
                     NULL,
                     4,
                     &g_transport_protocol_task);
#endif
    if (rc != pdPASS) {
        ESP_LOGE(LOG_TRANSPORT, "Failed to create protocol dispatcher task");
        return -1;
    }

    ESP_LOGI(LOG_TRANSPORT, "Protocol dispatcher task started");
    return 0;
}
