# DS Drone demo

Allow **9 minutes 30 seconds** for the live demo and leave the remaining 30 seconds for transitions. Questions follow the demo.

## Before the timer

- Build and verify the firmware, choose the ST-LINK port, close the serial monitor, and keep the IMU on a stable surface. Set up the project Python environment. If using the Brio, install the vision dependencies and have a fire photo ready on screen. Keep the Brio aimed away from the photo until the fire demonstration. [Setup and firmware commands](README.md#windows-setup)
- Open PowerShell windows from the checkout for the gateway, a status and telemetry subscriber, the incident logger and the fire watcher. Use the [README subscriber command](README.md#demo-recovery-checklist) while Node-RED is pending.
- Mostafa covers STM32 integration, the Python gateway and incident logger, MQTT, simulated GPS replay, and local inference. Cristian owns the Node-RED dashboard. Both PCs share the same HiveMQ broker and topic root. Use distinct gateway IDs `drone01` and `drone02`, a separate client ID for each connection, and leave the second PC's camera disabled. [Cristian's MQTT handoff](MQTT_Communication/MQTT_Topics_and_Payloads.md)

## Live order

| Time | Action and what to show |
| --- | --- |
| 0:00–1:00 | Trace MPU6050 → STM32 → ST-LINK USB → Python gateway → HiveMQ → subscribers. The GPS file supplies simulated decimal-degree positions. The camera runs on the PC, independently of the STM32. |
| 1:00–2:00 | Start the subscriber and logger. If presenting the camera, start the fire watcher before bringing the photo into view. Start one gateway using the commands below. |
| 2:00–3:30 | Reset the STM32. Keep the IMU still through green and `CALIBRATING`. When blue, show a fresh IMU reading and advancing simulated GPS points. A stationary board can report tracker `IDLE` while the stream reports `STREAMING`. |
| 3:30–4:30 | Press B1. Show red and `PAUSED`. Status keeps arriving online, telemetry stops, and the GPS cursor holds. Press B1 again and show blue and the next simulated fix. |
| 4:30–6:00 | Unplug only the STM32 USB cable. Show `online: true`, `stm_connected: false`, and `DISCONNECTED`. Reconnect, keep the IMU still, and show green calibration followed by blue and fresh telemetry. |
| 6:00–8:00 | If using the Brio, show the fire photo for several frames. Three consecutive processed frames with a fire detection confirm an episode. Compare the logger's incident ID and the watcher's saved JPEG. The model has a smoke label, and this project selects fire only. If the camera is unavailable, show the offline preview and identify it as still-image inference. |
| 8:00–9:30 | Run the isolated broker check below. It exercises Last Will by cutting the test gateway's TCP connection. HiveMQ retains `{"online":false}` after it detects the loss. Compare it with the earlier USB removal, when the real gateway stayed online. Stop the real gateway with `Ctrl+C` to send explicit offline status. |

Run these commands in separate windows. Choose one gateway command. The watcher waits for a matching live incident and preview and saves it as `Test/output/mqtt_fire_preview.jpg`.

```powershell
& .\.venv\Scripts\python.exe -m MQTT_Communication.log_incidents
& .\.venv\Scripts\python.exe -m Test.check_fire_mqtt --watch
& .\.venv\Scripts\python.exe -m MQTT_Communication.run_gateway --camera on
# Use this gateway command when the Brio is unavailable.
& .\.venv\Scripts\python.exe -m MQTT_Communication.run_gateway --camera off
& .\.venv\Scripts\python.exe -m Test.check_mqtt
```

Telemetry and previews use QoS 0. Incidents and status use QoS 1, so repeated delivery is possible. The logger keeps one row per `(drone_id, event_id)` already saved. The subscriber matches the preview using the same pair. Retained status is the broker's last-known value. The public broker shares the topics with other users.

## Evidence and remaining work

The one-PC physical bench verified RGB, B1, USB reconnection, camera recovery, live IMU with simulated GPS, and a real Brio incident with a matching MQTT JPEG. The Last Will and restart deduplication checks used labelled synthetic messages on isolated topics. The full two-PC, two-board run, Cristian's Node-RED integration, physical IMU error behavior, and the interactive camera window remain pending. [Bench results](README.md#bench-results)

The final report of up to four pages, presentation slides, and Node-RED flow documentation are separate submission work. The project specification calls for submission at least two days before the presentation.
