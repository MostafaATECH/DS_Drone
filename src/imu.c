#include "imu.h"

#include <errno.h>
#include <math.h>
#include <zephyr/device.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>

#define RAD_TO_DEG 57.2957795f
#define CALIBRATION_SAMPLES 250
#define CALIBRATION_INTERVAL_MS 10
#define SAMPLE_INTERVAL_MS 20
#define MAX_INTEGRATION_GAP_MS 100
#define GRAVITY_MS2 9.80665f
#define MOVEMENT_ACCEL_MS2 1.5f
#define MOVEMENT_GYRO_DPS 5.0f

static const struct device *const imu = DEVICE_DT_GET(DT_NODELABEL(mpu6050));
static struct imu_reading latest_reading;
K_MUTEX_DEFINE(reading_mutex);

/* Copy the latest IMU state and reading for the serial loop. */
void imu_get_reading(struct imu_reading *reading)
{
    k_mutex_lock(&reading_mutex, K_FOREVER);
    *reading = latest_reading;
    k_mutex_unlock(&reading_mutex);
}

/* Publish a complete snapshot. The lock is held only for the copy. */
static void publish_reading(const struct imu_reading *reading)
{
    k_mutex_lock(&reading_mutex, K_FOREVER);
    latest_reading = *reading;
    k_mutex_unlock(&reading_mutex);
}

/* Average 250 stationary gyro samples into a bias in degrees per second. */
static int calibrate_gyro(float bias_dps[3])
{
    for (int sample = 0; sample < CALIBRATION_SAMPLES; sample++) {
        struct sensor_value gyro[3];
        int ret = sensor_sample_fetch(imu);
        if (ret < 0) {
            return ret;
        }
        ret = sensor_channel_get(imu, SENSOR_CHAN_GYRO_XYZ, gyro);
        if (ret < 0) {
            return ret;
        }

        for (int axis = 0; axis < 3; axis++) {
            bias_dps[axis] += sensor_value_to_float(&gyro[axis]) * RAD_TO_DEG;
        }
        k_msleep(CALIBRATION_INTERVAL_MS);
    }
    for (int axis = 0; axis < 3; axis++) {
        bias_dps[axis] /= CALIBRATION_SAMPLES;
    }
    return 0;
}

/* Read all axes from one fetch, using m/s² for acceleration and deg/s for gyro. */
static int read_imu(struct imu_reading *reading, const float bias_dps[3])
{
    struct sensor_value accel[3];
    struct sensor_value gyro[3];
    int ret = sensor_sample_fetch(imu);
    if (ret < 0) {
        return ret;
    }
    ret = sensor_channel_get(imu, SENSOR_CHAN_ACCEL_XYZ, accel);
    if (ret < 0) {
        return ret;
    }
    ret = sensor_channel_get(imu, SENSOR_CHAN_GYRO_XYZ, gyro);
    if (ret < 0) {
        return ret;
    }

    for (int axis = 0; axis < 3; axis++) {
        reading->accel_ms2[axis] = sensor_value_to_float(&accel[axis]);
        reading->gyro_dps[axis] = sensor_value_to_float(&gyro[axis]) * RAD_TO_DEG
                                - bias_dps[axis];
        if (!isfinite(reading->accel_ms2[axis]) || !isfinite(reading->gyro_dps[axis])) {
            return -ERANGE;
        }
    }
    return 0;
}

/* Integrate only across adjacent samples, using degrees per second and seconds. */
static void update_angles(struct imu_reading *reading, int64_t elapsed_ms)
{
    if (elapsed_ms > MAX_INTEGRATION_GAP_MS) {
        return;
    }
    for (int axis = 0; axis < 3; axis++) {
        reading->angle_deg[axis] += reading->gyro_dps[axis] * (elapsed_ms / 1000.0f);
    }
}

struct tracker_motion {
    enum tracker_state pending;
    int64_t pending_since_ms;
};

/* Confirm sustained IMU motion or rest before switching the tracker state. */
static void update_tracker(struct imu_reading *reading, struct tracker_motion *motion,
                           int64_t now_ms)
{
    const float *a = reading->accel_ms2;
    const float *g = reading->gyro_dps;
    float acceleration = sqrtf(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]);
    float rotation_sq = g[0] * g[0] + g[1] * g[1] + g[2] * g[2];
    bool moving = fabsf(acceleration - GRAVITY_MS2) > MOVEMENT_ACCEL_MS2 ||
                  rotation_sq > MOVEMENT_GYRO_DPS * MOVEMENT_GYRO_DPS;
    enum tracker_state wanted = moving ? TRACKER_FLYING : TRACKER_IDLE;

    if (wanted != motion->pending) {
        motion->pending = wanted;
        motion->pending_since_ms = now_ms;
    }
    int confirm_ms = moving ? 1000 : 3000;
    if (moving && reading->tracker == TRACKER_PROBLEM) {
        confirm_ms = 2000;
    }
    if (now_ms - motion->pending_since_ms >= confirm_ms) {
        reading->tracker = wanted;
    }
}

/* Calibrate once, then sample at 50 Hz independently of B1 and serial output. */
static void imu_thread(void *arg1, void *arg2, void *arg3)
{
    ARG_UNUSED(arg1);
    ARG_UNUSED(arg2);
    ARG_UNUSED(arg3);

    struct imu_reading reading = {0};
    float bias_dps[3] = {0};
    struct tracker_motion motion = {0};

    /* Deferred initialization lets the app show green before touching the IMU. */
    if (device_init(imu) != 0 || !device_is_ready(imu)) {
        reading.state = IMU_ERROR;
        publish_reading(&reading);
        return;
    }
    k_msleep(1000);
    if (calibrate_gyro(bias_dps) < 0) {
        reading.state = IMU_ERROR;
        publish_reading(&reading);
        return;
    }

    while (true) {
        int64_t next_sample_ms = k_uptime_get() + SAMPLE_INTERVAL_MS;
        bool previous_valid = reading.valid;
        reading.valid = read_imu(&reading, bias_dps) == 0;
        int64_t now_ms = k_uptime_get();

        if (reading.valid) {
            if (reading.state == IMU_READY && now_ms - reading.sample_ms >= IMU_ERROR_MS) {
                reading.tracker = TRACKER_PROBLEM;
                motion.pending = TRACKER_PROBLEM;
            }
            if (previous_valid) {
                update_angles(&reading, now_ms - reading.sample_ms);
            }
            update_tracker(&reading, &motion, now_ms);
            reading.sample_ms = now_ms;
            reading.state = IMU_READY;
        } else if (reading.state == IMU_CALIBRATING) {
            reading.state = IMU_ERROR;
            publish_reading(&reading);
            return;
        } else {
            motion.pending = reading.tracker;
        }
        publish_reading(&reading);
        k_sleep(K_TIMEOUT_ABS_MS(next_sample_ms));
    }
}

K_THREAD_DEFINE(imu_worker, 2048, imu_thread, NULL, NULL, NULL, 5, 0, 0);
