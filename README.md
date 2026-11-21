# DS Drone

Each PC represents one drone. Its STM32 reads an MPU6050 and sends measurements over USB. A Python gateway on the PC adds a simulated GPS position and publishes JSON to the MQTT broker. Cristian can subscribe to the same topics for both drones when the Node-RED dashboard is ready. The optional Logitech camera plugs into the PC and runs local inference.

The STM32 firmware calibrates the IMU, handles B1 and RGB, and sends readings over USB. The PC gateway reads those frames, adds simulated GPS, and sends telemetry and status to HiveMQ. When enabled, the camera sends confirmed fire incidents and matching JPEG previews through the same MQTT connection.

## Project layout

| Location | Responsibility |
| --- | --- |
| `src/`, `Include/` | STM32 application and C headers. `imu.c` calibrates, samples, and processes the IMU. `board_io.c` handles B1 and the RGB LED. `main.c` owns the stream state and serial output. |
| `MQTT_Communication/` | `run_gateway.py` coordinates serial, GPS, and optional camera input. `publish_mqtt.py` publishes on the shared connection. `log_incidents.py` independently subscribes and saves incident records. |
| `AI_Vision/` | `fire_detection.py` runs the shared fire detector and JPEG renderer. `camera_stream.py` owns capture and inference on a worker thread. `fire_episodes.py` confirms a local incident from successive frames. |
| `Test/` | Focused checks, offline image tester, and local outputs. This is the existing casing on disk. |
| `boards/`, `confs/`, `.vscode/`, `CMakeLists.txt`, `build/` | Existing Zephyr board settings, configuration, VS Code tasks, and generated build output. |
| Project root | GPS route `upm_drone_gps_loop.json`, model weights `best.pt`, and shared `config.example.json` with per-PC overrides in `config.local.json`. |

## STM32 to PC serial contract

The old STM32 project sends line-based reports, including a `$SIMU1` frame with comma-separated fields and CRLF. DS_Drone uses the same text framing with a distinct `$DS1` prefix and the fields needed for this demo. The firmware uses Zephyr's `LPUART1` console through the ST-LINK Virtual COM Port. Use **115200 baud, 8 data bits, no parity, 1 stop bit, and no flow control**. Windows currently identifies the port as **COM3**.

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

### Firmware behavior

- Green covers MPU6050 initialization, one second of settling, and 250 successful gyro readings with a 10 ms delay between them. The firmware also needs a successful full IMU read before it streams. A startup failure shows red and reports `ERROR`. Reset the board after checking the wiring.
- After calibration, the IMU worker aims for 50 readings per second. The main loop sends each new sample it sees while streaming and reports status once per second and on state changes. B1 must be stable for 40 ms to register a press. Holding B1 triggers one transition. Sampling and angle integration continue during the pause, so resuming waits only for the next new sample. The angles are kept inside the firmware.
- Any failed read suppresses that measurement. Two seconds without a good read changes the stream state to `ERROR` and turns the LED red. If readings recover, the firmware returns to the current pause or stream setting. The tracker reports `PROBLEM` on recovered samples until it confirms stable motion or rest, then returns to `FLYING` or `IDLE`. These tracker labels describe bench motion inferred from the IMU.

### Build, flash, and monitor on Windows

The Zephyr board definition maps B1 (`sw0`) to PA0. Connect the MPU6050 (`0x68`) to 3.3 V and GND, SDA to D14/PA11, and SCL to D15/PA12. Connect the RGB common anode to 3.3 V and each cathode through its own resistor to D4/PB5 (red), D5/PB8 (green), or D6/PB10 (blue).

```powershell
Set-Location "$env:USERPROFILE\Desktop\DS_Drone"
$zephyrPython = "$env:USERPROFILE\zephyrproject\.venv\Scripts\python.exe"
$env:ZEPHYR_BASE = "$env:USERPROFILE\zephyrproject\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = 'zephyr'
$env:ZEPHYR_SDK_INSTALL_DIR = "$env:USERPROFILE\Desktop\University programing\tools\zephyr-sdk-1.0.1"
$env:PYTHONDONTWRITEBYTECODE = '1'

& $zephyrPython -m west build --board nucleo_wl55jc/stm32wl55xx -d build/step02 . -- `
  '-DCONF_FILE=confs/prj_nucleo_wl55jc.conf' `
  '-DDTC_OVERLAY_FILE=boards/nucleo_wl55jc.overlay' `
  "-DUSER_CACHE_DIR=$($PWD.Path)/.zephyr-cache"
& $zephyrPython -m west flash -d build/step02 --runner openocd
& $zephyrPython -m serial.tools.list_ports -v
& $zephyrPython -m serial.tools.miniterm COM3 115200 --eol CRLF --dtr 0 --rts 0
```

The VS Code West tasks use the same `build/step02` directory. Use this build for the demo because the original `build/zephyr` can still contain the template firmware.

Use the ST-LINK port from the port listing if Windows assigns a different number. Place the IMU on a stable surface, open the monitor, and reset the board. Keep the IMU still until the LED turns blue.

1. Check for green with `CALIBRATING` status lines about one second apart, followed by blue, `STREAMING`, and `$DS1,M,...` measurements. A B1 press while green cannot start streaming early.
2. Press and release B1. Look for red and `PAUSED`. Wait several seconds to confirm that status lines continue while measurement lines stop. Press B1 again and verify blue, `STREAMING`, and new readings.
3. Reset the board and check that it starts green and calibrates again. If testing a disconnected IMU at startup, turn off power before removing or reconnecting its wires. With the sensor disconnected, expect red and regular `ERROR` status lines.
4. Exit the monitor with `Ctrl+]` before opening the same COM port from the Python reader. If testing USB reconnection, reopen the monitor and reset the board to capture the full startup sequence.

## Run the gateway on Windows

Close Miniterm or any other serial monitor first. From PowerShell, create the environment once and install or update the gateway dependencies.

```powershell
Set-Location "$env:USERPROFILE\Desktop\DS_Drone"
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m MQTT_Communication.run_gateway
```

The gateway loads `config.example.json` by default. Create `config.local.json` in the repo root to override only the settings you need. Git ignores this file. For example, `{"serial":{"port":"COM3"}}` fixes the port to COM3. Set `port` to `null` for automatic discovery. Automatic discovery requires a unique ST-LINK match (`0483:374E`) and remembers its USB serial number when available. You can set `serial.usb_serial_number` from `serial.tools.list_ports -v` to identify a particular board when several are plugged in. Relative GPS paths start at the repo root.

The example configuration leaves the camera off. Use `--camera off` to force this mode on a PC with only the base dependencies, or `--camera on` to enable the Brio with the [optional vision dependencies](#test-the-detector-on-a-photograph) installed.

```powershell
# Choose one command per gateway.
& .\.venv\Scripts\python.exe -m MQTT_Communication.run_gateway --camera off
& .\.venv\Scripts\python.exe -m MQTT_Communication.run_gateway --camera on
```

Look for `INFO: MQTT connected as dsdrone-4f7c92-drone01` and `STM32` status lines when the port or board state changes. An open port stays `DISCONNECTED` until a valid frame arrives. `CALIBRATING` and `PAUSED` can have `stm_connected: true` while `streaming` is false. Once measurements arrive, the gateway prints a `Telemetry` JSON line about once per second using the latest fresh IMU sample. Each sample is printed at most once. A `$DS1,M,...` line can establish streaming if the gateway opens a port mid-stream. When the board has explicitly reported calibration, pause, or error, the gateway waits for `STREAMING` before accepting another measurement.

In the illustrative frame above, `read_stm32_serial.py` reads `az = 9.81 m/s²` and `gx = 0.57 deg/s`. `replay_gps.py` supplies the first simulated position at `40.3899329, -3.6293443`. `run_gateway.py` keeps `accel_m_s2.z = 9.81`, converts the gyro once to `gyro_rad_s.x ≈ 0.00995`, and uses the PC's UTC receipt time for the IMU timestamp. The fix gets its own UTC replay timestamp. The position stays `null` if a fresh fix is unavailable, including when the route file is missing or invalid.

B1 pause stops telemetry and holds the GPS cursor. A fresh sample after resume uses the next GPS point. USB removal discards the old sample and closes the port on a serial error or after five seconds of silence. The gateway retries every two seconds by default. Automatic discovery follows the same identified board if its COM number changes, and a new session starts with fresh frames. Press `Ctrl+C` to stop the gateway, send offline status, and release the COM port.

Run the synthetic parser and replay checks from the repository root with `& .\.venv\Scripts\python.exe -m unittest discover -s Test -p test_serial_gps.py -v`.

## MQTT handoff to Node-RED

Cristian's implementation reference contains the four payload examples, filters, status and freshness rules, and bench checks. [MQTT topics and payloads](MQTT_Communication/MQTT_Topics_and_Payloads.md)

`drone_topic()` joins `<topic_root>/<drone_id>/<message_type>`. The shared root is `aerofleet-dsdrone-4f7c92`. Use the defaults for `drone01` and the [second PC configuration](#set-up-the-second-pc) for `drone02`. The gateways use `dsdrone-4f7c92-drone01` and `dsdrone-4f7c92-drone02` as MQTT client IDs. Cristian can set `dsdrone-4f7c92-cristian-node-red` in the Node-RED broker settings.

The classroom broker is `broker.hivemq.com:1883`, MQTT 3.1.1 over TCP with a 15-second keepalive. Leave `username` and `password` as `null` and `tls` as `false` for this endpoint. Anyone on the shared public broker can read or publish under the prefix. It reduces accidental topic collisions and provides no access control.

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

The gateway sends telemetry at up to 1 Hz while a fresh valid measurement is available. It drops measurements received before the current MQTT connection so a reconnect starts with new readings. The GPS file gives a 1,000 ms interval and 120 looping positions in decimal degrees. Advance only while the calibrated stream is active, freeze the replay during calibration, pause, error, or disconnection, and resume from the next point. Use `position: null` if a current point is unavailable. Simulated movement on the map is independent of the tracker classification. MQTT timestamps use UTC ISO 8601 ending in `Z`.

| Stream state | `stm_connected` | `streaming` | `imu_status` |
| --- | --- | --- | --- |
| `CALIBRATING` | `true` | `false` | `CALIBRATING` |
| `STREAMING` | `true` | `true` while a valid measurement is fresh | `READY` |
| `PAUSED` | `true` | `false` | `READY` |
| `ERROR` | `true` | `false` | `ERROR` |
| `DISCONNECTED` | `false` | `false` | `UNAVAILABLE` |

`MqttPublisher.publish_update()` sends full status after each MQTT connection, when the STM32 or camera status changes, and about once per second. B1 pause keeps `online` and `stm_connected` true, sets `stream_state` to `PAUSED`, and stops telemetry. Unplugging the STM32 keeps `online` true and reports `stm_connected: false` and `DISCONNECTED`. Camera capture and Paho's network loop run throughout both cases. `camera_status` reports `AVAILABLE`, `UNAVAILABLE`, `DISABLED`, or `ERROR` independently of the board.

The gateway registers the retained QoS 1 Last Will `{"online":false}` before connecting. HiveMQ sends it when it detects unexpected loss of the gateway connection. On `Ctrl+C`, `MqttPublisher.close()` explicitly publishes the same retained offline payload and waits briefly for its acknowledgement. When the dashboard sees `online: false`, the current board state is unknown. During `STREAMING`, mark measurements stale after five seconds without telemetry. Check status age and the dashboard's own broker connection too.

MQTT delivery happens in two exchanges. First the gateway publishes to HiveMQ. Then HiveMQ delivers to each subscriber at the QoS allowed by that subscription. Telemetry is QoS 0 and individual readings can be lost. Status is QoS 1 and the receiver of each QoS 1 exchange sends a `PUBACK`, so duplicates are possible. Retaining status lets a new subscriber receive the latest stored value for that topic immediately.

### Match a fire incident and its preview

The Brio's worker passes each frame through the same `process_frame()` used by the photo tester. Three consecutive positive frames confirm a fire episode. The gateway copies the latest simulated fix that existed when the confirming frame was captured. That position represents the observing drone. The ground fire location is unknown. An incident still goes out with `position: null` if there was no valid fix or that fix was over five seconds old at capture time.

The incident goes to `aerofleet-dsdrone-4f7c92/drone01/incident` at QoS 1. The gateway holds pending incidents in a bounded in-memory queue and keeps their IDs and position snapshots across retries. The latest annotated JPEG for the active episode goes to `aerofleet-dsdrone-4f7c92/drone01/preview` at QoS 0, at most once a second. `mqtt.preview_max_bytes` caps the complete JSON payload including Base64 at 120,000 bytes by default. A skipped preview produces a diagnostic and leaves incident metadata independent. Both fire topics are non-retained.

For example, an incident without a recent simulated fix looks like this.

```json
{"event_id":"drone01-fire-example","timestamp":"2026-11-21T10:00:05Z","type":"fire","confidence":0.92,"position":null}
```

Cristian can subscribe to the `+/incident` and `+/preview` filters in the table. Read the drone ID from the second topic segment, then store both messages under the pair `(drone_id, event_id)`. Either message can arrive first. Use the same pair to handle duplicate QoS 1 incidents. The preview payload has `event_id`, `timestamp`, and `image_base64`. Create the browser image URL with `"data:image/jpeg;base64," + payload.image_base64`.

### Check HiveMQ from PowerShell

From the repository root, the first command checks TCP reachability. The second checks actual MQTT receipt using labelled synthetic data under a unique `<topic_root>/test-…/synthetic/` namespace.

```powershell
Test-NetConnection broker.hivemq.com -Port 1883
& .\.venv\Scripts\python.exe -m Test.check_mqtt
& .\.venv\Scripts\python.exe -m unittest discover -s Test -v
```

The MQTT check verifies telemetry receipt, retained online status for a late subscriber, the Last Will after abruptly closing TCP, current status after reconnect, and explicit offline status on clean shutdown. The extra topic level keeps these fixtures outside Cristian's dashboard subscriptions. The script removes its retained test status at the end. For a bench check, watch `aerofleet-dsdrone-4f7c92/drone01/status` and `/telemetry` while pressing B1, unplugging only the STM32, and exiting the gateway with `Ctrl+C`. The deliberate TCP failure in the script is the separate Last Will check.

### Try the fire topics before Node-RED is ready

1. In PowerShell from DS_Drone, run the watcher first. Wait for `Watching` so it can receive the non-retained messages.

   ```powershell
   & .\.venv\Scripts\python.exe -m Test.check_fire_mqtt --watch
   ```

2. In a second PowerShell window from DS_Drone, start one camera-enabled gateway. Wait for `Camera AVAILABLE`, then point the Brio at the fire photograph on a screen. Keep the camera aimed at the intended demo image when using the public broker.

   ```powershell
   & .\.venv\Scripts\python.exe -m MQTT_Communication.run_gateway --camera on
   ```

3. The watcher prints when it receives metadata and saves the first matched JPEG as `Test/output/mqtt_fire_preview.jpg`. If the episode started before the watcher subscribed, clear the camera view of the fire image for at least two seconds and show the image again. Stop the gateway with `Ctrl+C`.

For an independent broker check, run `& .\.venv\Scripts\python.exe -m Test.check_fire_mqtt`. It sends labelled synthetic metadata and a generated JPEG on a separate test namespace and verifies receipt without using the camera or STM32.

## Log incidents to JSON Lines

In a separate PowerShell window from DS_Drone, start the independent consumer. Wait for `Listening` before generating a fire episode. `Ctrl+C` stops it. Only the base requirements are needed.

```powershell
& .\.venv\Scripts\python.exe -m MQTT_Communication.log_incidents
```

The logger subscribes to `aerofleet-dsdrone-4f7c92/+/incident` at QoS 1. It writes `Test/output/incidents.jsonl`, adding the drone ID from the topic. It checks the payload and skips malformed data with a warning. It reads saved `(drone_id, event_id)` pairs before connecting, so restarting it against the same file still skips repeated IDs. Use one logger per output file. For a separate run, pass `--output .\Test\output\my_incidents.jsonl`. The output directory is ignored by Git.

Local deduplication prevents repeated rows for IDs already saved in that file. The logger uses a clean MQTT session and the incidents are non-retained, so incidents published while it is disconnected may be missed. The file can also be lost or damaged locally. QoS 1 and local deduplication provide two separate guarantees and do not establish end-to-end exactly-once delivery.

Test the logger without the STM32, camera, or Node-RED by running `& .\.venv\Scripts\python.exe -m Test.check_incident_logger`. It uses labelled synthetic messages in a separate namespace.

## Test the detector on a photograph

Install the optional vision dependencies in the project environment. The CPU wheel versions below are the pair used for the image test. [PyTorch CPU wheel instructions](https://pytorch.org/get-started/previous-versions/#v280)

```powershell
Set-Location "C:\Users\Acer\Desktop\DS_Drone"
python -m venv .venv  # Run once if the project environment does not exist
$projectPython = '.\.venv\Scripts\python.exe'
& $projectPython -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
& $projectPython -m pip install -r requirements-vision.txt
```

Use the project's interpreter in place of `py`, which did not find an installation on this PC. `--image` can point to a photograph outside the repository. Pick a `.jpg` or `.jpeg` output inside DS_Drone. Keep filenames in quotes if the path contains spaces.

```powershell
& $projectPython .\Test\test_model_image.py `
  --image "C:\Users\Acer\Desktop\distributed system\testfire.jpg" `
  --model ".\best.pt" `
  --output ".\Test\output\testfire_preview.jpg"
```

For each new photograph, change `--image` and choose a new filename for `--output`. The tool prints the checkpoint's class mapping, the detected labels and confidences, and the saved path. This `best.pt` reports `0 = smoke` and `1 = fire`. It still saves a preview when it prints `No detections`. Use `--show` if you also want an OpenCV window. A photo provides one inference result. Confirming an incident requires successive live frames.

`process_frame()` validates a BGR frame and runs the detector on CPU with image size 640, confidence threshold 0.25, and IoU threshold 0.45. The shared detector selects only the `fire` label from `model.names`. The renderer scales the source boxes to a preview with a longest edge of 640 pixels, preserving aspect ratio and skipping upscaling. It draws two-pixel orange boxes with the label and confidence to two decimal places. The shared encoder returns JPEG bytes at quality 60, which the still-image tool writes directly. It runs independently of the STM32, camera and MQTT gateway.

Run the shared preview checks with `& $projectPython -m unittest discover -s Test -p test_vision_preview.py -v`.

## Check the Logitech camera locally

Install or update `requirements-vision.txt` using the same project interpreter as the photo test. List devices before choosing an index. The standalone check uses the DirectShow backend by default and displays each camera's device name and USB ID. [Camera enumeration reference](https://pypi.org/project/cv2-enumerate-cameras/1.4.0/)

```powershell
Set-Location "$env:USERPROFILE\Desktop\DS_Drone"
$projectPython = '.\.venv\Scripts\python.exe'
& $projectPython -m pip install -r requirements-vision.txt
& $projectPython -m Test.check_camera --list
& $projectPython -m Test.check_camera --enable --seconds 18 --output '.\Test\output\logitech_preview.jpg'
& $projectPython -m Test.check_camera --enable --show
```

The Brio 100 was `046d:094c` at DirectShow index 1 on this PC. Index 0 was the laptop camera. The default `camera.source` selects the Brio by USB ID, so the worker looks up its current index again when reopening. You can choose an index explicitly using `--source 1` or a numeric `camera.source` in `config.local.json`. Keep `camera.enabled` false on the PC without a camera. `--enable` turns it on for this check. Close the local window with `Q`, `Esc`, or `Ctrl+C`. To check recovery, unplug the Brio, wait for `UNAVAILABLE`, plug it back in, and wait for `AVAILABLE`. The missing-device path can also be exercised with `--enable --source ffff:ffff --seconds 6`.

| State | Meaning |
| --- | --- |
| `DISABLED` | Camera support is off and the detector is unloaded. |
| `UNAVAILABLE` | Waiting for the chosen device, unable to open it, or unable to read a frame. |
| `AVAILABLE` | The worker has processed a frame and produced a preview. |
| `ERROR` | A dependency, checkpoint, inference, or capture backend failed. The check prints the cause. |

`CameraWorker` reads and processes one frame at a time on its own thread. It uses the same `process_frame()` as the photo test, so boxes, labels and JPEG settings match. It replaces the latest preview in one slot and keeps up to 16 pending local incident notifications for the check to drain. It releases the camera after a failure and waits `camera.retry_s` seconds before reopening, initially five. Shutdown signals the worker and waits briefly for the active capture or inference call to finish.

The demo uses the shared confidence threshold of 0.25 when `camera.confidence` is `null`. A number in `config.local.json` overrides it for live capture. One positive frame can show orange boxes in the preview. After three consecutive processed frames with a fire detection, the tracker creates one local incident with a stable `drone01-fire-…` ID. It rearms after two seconds of observed absence. Change `camera.confirmation_frames` or `camera.absence_s` to tune these demo choices. Camera interruptions reset an unconfirmed streak and preserve a confirmed ID while the scene is unobserved. The detector selects the checkpoint's `fire` label by name.

For the local episode check, point the Brio at a screen showing the fire photograph, hold it in view, then remove it from view for at least two seconds. Watch for one `Confirmed fire` line per episode. The camera unit checks use `& $projectPython -m unittest discover -s Test -p test_camera_stream.py -v`.

## Set up the second PC

Open PowerShell in that PC's DS_Drone checkout. Install the base requirements and copy the [drone02 example](config.drone02.example.json). If `config.local.json` already exists, merge the example into it. The local file is ignored by Git.

```powershell
python -m venv .venv
$projectPython = '.\.venv\Scripts\python.exe'
& $projectPython -m pip install -r requirements.txt
if (-not (Test-Path .\config.local.json)) {
    Copy-Item .\config.drone02.example.json .\config.local.json
}
& $projectPython -m serial.tools.list_ports -v
```

Leave `serial.port` as `null` for automatic ST-LINK discovery. If needed, set it to the ST-LINK COM port reported on that PC or set `serial.usb_serial_number` to identify one board among several. Keep the shared broker and topic root from `config.example.json`. With the second board running the DS_Drone firmware, run `& $projectPython -m MQTT_Communication.run_gateway --camera off`. Expect `MQTT connected as dsdrone-4f7c92-drone02` and `camera_status: "DISABLED"`.

## Demo recovery checklist

Close any serial monitor before starting the gateway. Start a subscriber first, using Cristian's filters or the following PowerShell command. It shows status and telemetry from both drone IDs and marks retained deliveries. Stop it with `Ctrl+C`.

```powershell
@'
from uuid import uuid4
from project_config import load_config
from Test.check_mqtt import make_client, subscribe

settings = load_config()["mqtt"]
root = settings["topic_root"]
subscriber = make_client(settings, "ds-watch-" + uuid4().hex[:10])
inbox = subscribe(subscriber, settings, [(f"{root}/+/status", 1), (f"{root}/+/telemetry", 0)])
try:
    while True:
        message = inbox.get()
        print(message.topic, "retained" if message.retain else "live", message.payload.decode())
except KeyboardInterrupt:
    subscriber.disconnect()
    subscriber.loop_stop()
'@ | & .\.venv\Scripts\python.exe -u -
```

1. Start `drone01` with `--camera off`. Reset the STM32 and keep the IMU flat and still. Expect green and `CALIBRATING`, then blue, `STREAMING`, and fresh telemetry every second with advancing simulated GPS points. The real IMU can report `tracker_state: "IDLE"` while `stream_state` is `STREAMING`.
2. Press B1, wait at least five seconds, then press again. Expect red and `PAUSED`, with continued online/connected status, no telemetry, and the GPS replay cursor held in place. On blue, fresh samples resume at the next route point.
3. Unplug only the STM32 USB cable, wait for `DISCONNECTED`, then plug it back in. Status stays online throughout. The board reports `stm_connected: false` and telemetry stops while USB is unplugged. Hold the IMU still for green calibration, then expect blue and newly timestamped readings.
4. Check that the camera-disabled start reports `DISABLED`. Restart with `--camera on`, wait for `AVAILABLE`, then unplug only the Brio. Expect `UNAVAILABLE` while telemetry and status continue. Plug it back in and wait for `AVAILABLE` on the next retry. Use the [fire watcher](#try-the-fire-topics-before-node-red-is-ready), show the demo photo, and check that it saves the preview matching the incident ID.
5. Run `& .\.venv\Scripts\python.exe -m Test.check_incident_logger`. Expect one saved line for each synthetic drone even when the event ID repeats, including after a logger restart.
6. Start `drone02` using the second PC setup. The same wildcard subscriber should show both drone IDs, with `DISABLED` for `drone02` and telemetry from both boards.
7. Open a new subscriber with the gateway running. Expect retained QoS 1 status and only new telemetry. Run `& .\.venv\Scripts\python.exe -m Test.check_mqtt` to check abrupt TCP loss, retained `{"online":false}`, reconnect, fresh-only telemetry, and the clean shutdown message on isolated test topics.
8. Run the [offline photo check](#test-the-detector-on-a-photograph) with a fire photo and a photo with no detections, using a different output name each time. Check the shared label, box, preview and JPEG settings.

### Bench results

- **One STM32 and one PC.** The RGB sequence was verified on the board. A live HiveMQ subscriber saw `tracker_state: IDLE` while `stream_state` was `STREAMING`. During the B1 pause it received 24 online status messages and zero telemetry messages. The GPS replay resumed at the next point. Removing the ST-LINK USB stopped telemetry while the gateway stayed online. Reconnecting produced `CALIBRATING`, followed by fresh telemetry at the next route point.
- **Camera.** With the camera disabled, live IMU telemetry still reached HiveMQ. The Brio reported `UNAVAILABLE` when unplugged and returned to `AVAILABLE` after reconnecting. The subscriber received 31 live IMU messages during the outage. A separate run with a missing camera ID reported `UNAVAILABLE`. A live fire test delivered a matching incident and annotated JPEG with a fresh simulated observer fix. The offline image tester saved JPEG previews for both a fire photo and a photo with no detections.
- **Broker checks with synthetic data.** HiveMQ delivered retained status to a late subscriber, retained the Last Will on a TCP interruption, and delivered only fresh test telemetry after reconnect. Both drone IDs appeared under one wildcard in a one-PC fixture. The incident checks matched the preview and skipped duplicate logger rows across restarts.

Still pending are the simultaneous two-PC, two-board run and Cristian's Node-RED check.
