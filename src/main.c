#include "board_io.h"
#include "imu.h"

#include <errno.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#define STATUS_INTERVAL_MS 1000
#define BUTTON_POLL_MS 10

enum stream_state {
    STREAM_CALIBRATING,
    STREAM_STREAMING,
    STREAM_PAUSED,
    STREAM_ERROR,
};

static const struct device *const console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
static const char *const status_frames[] = {
    [STREAM_CALIBRATING] = "$DS1,S,CALIBRATING\r\n",
    [STREAM_STREAMING] = "$DS1,S,STREAMING\r\n",
    [STREAM_PAUSED] = "$DS1,S,PAUSED\r\n",
    [STREAM_ERROR] = "$DS1,S,ERROR\r\n",
};
static const enum led_color state_colors[] = {
    [STREAM_CALIBRATING] = LED_GREEN,
    [STREAM_STREAMING] = LED_BLUE,
    [STREAM_PAUSED] = LED_RED,
    [STREAM_ERROR] = LED_RED,
};

/* Send a complete line over the ST-LINK console with its existing CRLF. */
static void send_line(const char *line)
{
    for (; *line != '\0'; line++) {
        uart_poll_out(console, *line);
    }
}

/* Format one valid sample with the six axes and the current tracker state. */
static void send_measurement(const struct imu_reading *reading)
{
    static const char *const tracker_names[] = {
        [TRACKER_IDLE] = "IDLE",
        [TRACKER_FLYING] = "FLYING",
        [TRACKER_PROBLEM] = "PROBLEM",
    };
    char line[130]; /* Up to 128 bytes before LF, then LF and the C terminator. */
    const float *a = reading->accel_ms2;
    const float *g = reading->gyro_dps;
    int length = snprintk(line, sizeof(line), "$DS1,M,%s,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f\r\n",
                         tracker_names[reading->tracker],
                         (double)a[0], (double)a[1], (double)a[2],
                         (double)g[0], (double)g[1], (double)g[2]);
    if (length > 0 && (size_t)length < sizeof(line)) {
        send_line(line);
    }
}

/* Keep the calibration and fault states ahead of the user's pause setting. */
static enum stream_state get_stream_state(const struct imu_reading *reading,
                                          bool board_ready, bool paused, int64_t now_ms)
{
    if (!board_ready || reading->state == IMU_ERROR) {
        return STREAM_ERROR;
    }
    if (reading->state == IMU_CALIBRATING) {
        return STREAM_CALIBRATING;
    }
    if (now_ms - reading->sample_ms >= IMU_ERROR_MS) {
        return STREAM_ERROR;
    }
    return paused ? STREAM_PAUSED : STREAM_STREAMING;
}

/* Poll B1, report state changes and heartbeats, and send valid new measurements. */
int main(void)
{
    if (!device_is_ready(console)) {
        return -ENODEV;
    }

    bool board_ready = board_io_init() == 0;
    bool paused = false;
    enum stream_state state = STREAM_CALIBRATING;
    int64_t next_status_ms = 0;
    int64_t last_sent_ms = -1;

    while (true) {
        struct imu_reading reading;
        imu_get_reading(&reading);
        int64_t now_ms = k_uptime_get();
        enum stream_state next = get_stream_state(&reading, board_ready, paused, now_ms);

        if (board_ready && board_button_pressed(now_ms) && next == state &&
            (state == STREAM_STREAMING || state == STREAM_PAUSED)) {
            paused = !paused;
            next = paused ? STREAM_PAUSED : STREAM_STREAMING;
            /* On resume, wait for a reading acquired after this press. */
            last_sent_ms = now_ms;
        }

        bool changed = next != state || next_status_ms == 0;
        if (changed) {
            state = next;
            if (board_set_led(state_colors[state]) < 0) {
                board_ready = false;
                state = STREAM_ERROR;
                (void)board_set_led(LED_RED);
            }
        }
        if (changed || now_ms >= next_status_ms) {
            send_line(status_frames[state]);
            next_status_ms = now_ms + STATUS_INTERVAL_MS;
        }

        if (state == STREAM_STREAMING && reading.valid && reading.sample_ms > last_sent_ms) {
            send_measurement(&reading);
            last_sent_ms = reading.sample_ms;
        }
        k_sleep(K_TIMEOUT_ABS_MS(now_ms + BUTTON_POLL_MS));
    }
}
