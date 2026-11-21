#ifndef DS_DRONE_IMU_H
#define DS_DRONE_IMU_H

#include <stdbool.h>
#include <stdint.h>

#define IMU_ERROR_MS 2000

enum imu_state {
    IMU_CALIBRATING,
    IMU_READY,
    IMU_ERROR,
};

enum tracker_state {
    TRACKER_IDLE,
    TRACKER_FLYING,
    TRACKER_PROBLEM,
};

struct imu_reading {
    enum imu_state state;
    enum tracker_state tracker;
    bool valid;
    int64_t sample_ms;
    float accel_ms2[3];
    float gyro_dps[3];
    float angle_deg[3];
};

void imu_get_reading(struct imu_reading *reading);

#endif
