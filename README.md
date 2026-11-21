# DS Drone

Each PC represents one drone. Its STM32 reads an MPU6050 and sends measurements over USB. A Python gateway on the PC adds a simulated GPS position and publishes JSON to the MQTT broker. Cristian's Node-RED dashboard subscribes to the same topics for both drones. The optional Logitech camera plugs into the PC and runs local inference.

DS_Drone starts from a Zephyr template whose `main()` returns immediately. The STM32 behavior and Python components below describe the planned interfaces.

## Project layout

| Location | Responsibility |
| --- | --- |
| `src/`, `Include/` | STM32 application and C headers. Read and calibrate the IMU, maintain the stream state, handle B1 and RGB, and send serial frames. |
| `MQTT_Communication/` | One Python gateway entry point, with separate serial reading and GPS replay. The gateway owns the MQTT connection and publishes combined status. An independent incident logger subscribes to the broker. |
| `AI_Vision/` | Optional camera capture, local detection, annotation, and JPEG encoding. The offline image test uses the same vision functions. |
| `Test/` | Focused checks, offline image tester, and local outputs. This is the existing casing on disk. |
| `boards/`, `confs/`, `.vscode/`, `CMakeLists.txt`, `build/` | Existing Zephyr board settings, configuration, VS Code tasks, and generated build output. |
| Project root | GPS route `upm_drone_gps_loop.json`, local weights `best.pt`, and future shared `config.example.json` with per-PC overrides in `config.local.json`. |

## STM32 to PC serial contract

The old STM32 project sends line-based reports, including a `$SIMU1` frame with comma-separated fields and CRLF. DS_Drone uses the same text framing with a distinct `$DS1` prefix and the fields needed for this demo. The Zephyr board devicetree selects `LPUART1` at **115200 baud**. Windows currently identifies the ST-LINK Virtual COM Port as **COM3**. Use 115200, 8 data bits, no parity, 1 stop bit, and no flow control as the proposed terminal settings. The current DS_Drone template disables the console, so receiving frames with these settings still needs an on-board check.

```text
$DS1,S,CALIBRATING
$DS1,S,STREAMING
$DS1,M,IDLE,0.12,-0.04,9.81,0.57,1.15,-0.57
$DS1,S,PAUSED
$DS1,S,ERROR
```

| Frame | Fields after `$DS1` |
| --- | --- |
| Status | `S,stream_state` with one of `CALIBRATING`, `STREAMING`, `PAUSED`, `ERROR`. Send on state changes and once per second, including during calibration, pause, and error. |
| Measurement | `M,tracker_state,ax,ay,az,gx,gy,gz` for one fresh, valid sample while streaming. Acceleration is in m/s² and includes gravity. Gyro is in deg/s after bias correction, matching the older IMU code. Tracker values are `IDLE`, `FLYING`, and `PROBLEM`. They describe the IMU-based demo classification. |

- Send ASCII, one comma-separated frame per line, with decimal points and a CRLF ending. The PC also accepts LF. Limit a line to 128 bytes before LF, including any CR. Send complete frames without debug text interleaved. Debug output uses other prefixes.
- The receiver buffers split reads until LF. It ignores other prefixes and rejects overlong lines, invalid fields, wrong field counts, and non-finite numbers. Discard an overlong line through its next LF. Only a valid `S` or `M` refreshes board liveness.
- The board sends `CALIBRATING` after boot and switches to `STREAMING` after successful calibration. Clear cached measurements when entering calibration, pause, error, or disconnection. After resume or reopen, wait for a fresh measurement. A valid `M` also establishes `STREAMING` when the PC starts listening mid-stream. Device sequence numbers and timestamps are omitted. The PC records its receipt time and uses monotonic time for freshness.
- A serial read or device-removal error, or five seconds without any valid frame, produces `DISCONNECTED` on the PC. Clear partial input and stale samples, and retry the identified port every two seconds initially. An open COM port or debug text alone cannot establish a responsive board.

### Board wiring to check

The Zephyr board definition maps `sw0` to B1 on PA0 and I2C2 to PA11 (SDA) and PA12 (SCL). The older overlay adds the MPU6050 at `0x68` and the active-low RGB channels on PB5, PB8, and PB10. The new firmware will need those declarations in its own overlay.

Confirm the MPU6050 uses 3.3 V and GND, SDA is on D14/PA11, and SCL is on D15/PA12. Confirm the RGB common anode is on 3.3 V and each cathode has its own resistor to D4/PB5 (red), D5/PB8 (green), or D6/PB10 (blue). Keep the board still at startup. Once Step 02 is ready, use VS Code's **West: Build** and **West: Flash** tasks, open the ST-LINK port with the settings above, and reset the board. Capture the startup lines and LED sequence, then press B1 twice and record the lines for each press. Expect green during calibration, blue during streaming, and red during pause or IMU error. Unplug USB to check that serial output stops, then reconnect and check the COM number and startup sequence.

To identify the COM port from the project root in PowerShell:

```powershell
& "$env:USERPROFILE\zephyrproject\.venv\Scripts\python.exe" -m serial.tools.list_ports -v
```

## MQTT handoff to Node-RED

Each topic is `<topic_root>/<drone_id>/<message_type>`. The default root for Cristian is `aerofleet-dsdrone-4f7c92`. Use `drone01` and `drone02` on the two PCs, with distinct client IDs. The planned classroom broker is `broker.hivemq.com:1883`, MQTT 3.1.1 over TCP, with a 15-second keepalive and no TLS or credentials.

| Message | Node-RED subscription | QoS | Retained | Payload |
| --- | --- | --- | --- | --- |
| Telemetry | `aerofleet-dsdrone-4f7c92/+/telemetry` | 0 | No | `timestamp`, `tracker_state`, `position`, `accel_m_s2`, `gyro_rad_s` |
| Status | `aerofleet-dsdrone-4f7c92/+/status` | 1 | Yes | `online`, `timestamp`, `stm_connected`, `streaming`, `stream_state`, `imu_status`, `camera_status` |
| Incident | `aerofleet-dsdrone-4f7c92/+/incident` | 1 | No | `event_id`, `timestamp`, `type`, `confidence`, `position` |
| Preview | `aerofleet-dsdrone-4f7c92/+/preview` | 0 | No | `event_id`, `timestamp`, `image_base64` |

### Follow one measurement

The MPU6050 gives the STM32 six numbers. After calibration, the STM32 sends them over USB in the example `$DS1,M,...` line. The PC reads the six numbers, converts the gyro values from deg/s to rad/s once (`degrees × π / 180`), and uses the time it received the line as the measurement timestamp. It adds the next position from the GPS file, marks it `simulated`, and adds the position's replay time as `fix_timestamp`. It then publishes the JSON below to `aerofleet-dsdrone-4f7c92/drone01/telemetry`. Node-RED reads `drone01` from the topic and uses the payload to update the readings and map.

```json
{
  "timestamp": "2026-11-21T10:00:00Z",
  "tracker_state": "IDLE",
  "position": {
    "latitude": 40.3899329,
    "longitude": -3.6293443,
    "source": "simulated",
    "fix_timestamp": "2026-11-21T10:00:00Z"
  },
  "accel_m_s2": {"x": 0.12, "y": -0.04, "z": 9.81},
  "gyro_rad_s": {"x": 0.00995, "y": 0.02007, "z": -0.00995}
}
```

Send telemetry once per second when a fresh valid measurement is available. The GPS file gives a 1,000 ms interval and 120 looping positions in decimal degrees. Advance only while the calibrated stream is active, freeze the replay during calibration, pause, error, or disconnection, and resume from the next point. Use `position: null` if a current point is unavailable. Simulated movement on the map is independent of the tracker classification. MQTT timestamps use UTC ISO 8601 ending in `Z`.

| Stream state | `stm_connected` | `streaming` | `imu_status` |
| --- | --- | --- | --- |
| `CALIBRATING` | `true` | `false` | `CALIBRATING` |
| `STREAMING` | `true` | `true` while a valid measurement is fresh | `READY` |
| `PAUSED` | `true` | `false` | `READY` |
| `ERROR` | `true` | `false` | `ERROR` |
| `DISCONNECTED` | `false` | `false` | `UNAVAILABLE` |

`online` describes the PC gateway's broker connection and can stay `true` when the board is unplugged. Report status on each MQTT connection, on changes, and once per second. The retained QoS 1 Last Will is `{"online":false}`. When the gateway goes offline, the dashboard should show the board's condition as unknown. The dashboard should check status freshness and its own broker connection. It should show calibration or pause while those states have fresh status, and mark measurements stale after five seconds without telemetry in `STREAMING`.

Camera status is `AVAILABLE`, `UNAVAILABLE`, `DISABLED`, or `ERROR`. Incidents and previews come from the camera and can continue while STM32 measurements are paused. Use `position: null` on an incident when the last simulated fix is over five seconds old. Match incident metadata and JPEG previews by drone ID and `event_id`. The logger can use the same pair to discard QoS 1 duplicates.
