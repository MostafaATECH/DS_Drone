# DS_Drone MQTT handoff for Cristian

## Connection and subscriptions

Use `broker.hivemq.com`, TCP port `1883`, MQTT 3.1.1 and a 15-second keepalive. Leave TLS off and username and password empty for this public test endpoint. The shared root is `aerofleet-dsdrone-4f7c92`. The prefix helps keep demos separate. Anyone connected to the public broker can read or publish these topics.

Use a unique client ID per connection. The gateways default to `dsdrone-4f7c92-drone01` and `dsdrone-4f7c92-drone02`. A dashboard can use `dsdrone-4f7c92-node-red`, with a different ID for each dashboard instance. The independent Python logger generates its own `dsdrone-log-…` ID.

The topic layout is `<root>/<drone_id>/<message_type>`. Use `+` for the drone ID when subscribing to all drones.

| Message | Exact subscription filter | Publish QoS | Suggested subscription QoS | Retained |
| --- | --- | --- | --- | --- |
| Telemetry | `aerofleet-dsdrone-4f7c92/+/telemetry` | 0 | 0 | No |
| Status | `aerofleet-dsdrone-4f7c92/+/status` | 1 | 1 | Yes |
| Incident | `aerofleet-dsdrone-4f7c92/+/incident` | 1 | 1 | No |
| Preview | `aerofleet-dsdrone-4f7c92/+/preview` | 0 | 0 | No |

## Payload examples

Timestamps are ISO 8601 UTC strings ending in `Z`. Read the drone ID from the second topic segment.

### Telemetry

The gateway publishes at up to 1 Hz when it has a fresh IMU sample. The timestamp is the gateway's serial receipt time. Acceleration is in m/s² including gravity, and gyro is in rad/s. `tracker_state` is `IDLE`, `FLYING`, or `PROBLEM`. Label `position.source: "simulated"` as simulated GPS on the map. `position` is `null` when a fresh fix is unavailable.

```json
{
  "timestamp": "2026-11-21T10:00:00.000Z",
  "tracker_state": "IDLE",
  "position": {
    "latitude": 40.3899329,
    "longitude": -3.6293443,
    "source": "simulated",
    "fix_timestamp": "2026-11-21T10:00:00.000Z"
  },
  "accel_m_s2": {"x": 0.12, "y": -0.04, "z": 9.81},
  "gyro_rad_s": {"x": 0.00995, "y": 0.02007, "z": -0.00995}
}
```

### Status

```json
{
  "online": true,
  "timestamp": "2026-11-21T10:00:01.000Z",
  "stm_connected": true,
  "streaming": false,
  "stream_state": "PAUSED",
  "imu_status": "READY",
  "camera_status": "AVAILABLE"
}
```

| Field | Meaning |
| --- | --- |
| `online` | The gateway's MQTT connection. It can stay `true` after the STM32 is unplugged. |
| `timestamp` | Time the gateway assembled this full status. |
| `stm_connected` | The board has recently sent a valid status or measurement frame. |
| `streaming` | The enabled board stream has a fresh measurement. |
| `stream_state` | `CALIBRATING`, `STREAMING`, `PAUSED`, `DISCONNECTED`, or `ERROR`. |
| `imu_status` | `CALIBRATING`, `READY`, `ERROR`, or `UNAVAILABLE`. |
| `camera_status` | `AVAILABLE` means processed frames are arriving. `UNAVAILABLE` means the configured camera could not be used. `DISABLED` means camera processing is off. `ERROR` means the camera backend or model pipeline failed. |

The full status arrives on connect, on changes and about once per second. Check the dashboard's own broker connection and mark a full status older than five seconds stale. During `STREAMING`, mark telemetry stale after five seconds without a fresh reading. During `CALIBRATING` or `PAUSED`, show that state while status is fresh and expect measurements to stop. A retained status is a last-known value and can be stale even when it first arrives.

On unexpected gateway connection loss, HiveMQ can publish the retained QoS 1 Last Will `{"online":false}`. A clean shutdown explicitly publishes the same payload. Show gateway offline and treat the omitted hardware fields as unknown. A missing or disabled camera leaves the serial telemetry path available. A camera outage means the scene is unobserved.

### Incident

The current detector publishes `type: "fire"` after a confirmed episode. Confidence is between 0 and 1. A position, when present, has the same shape as the telemetry position and represents the simulated observing drone at the time of the confirming frame. The ground fire location is unknown. The camera can still confirm an incident during B1 pause or STM32 disconnection, with `position: null` when the observer fix is stale or unavailable.

```json
{
  "event_id": "drone01-fire-example",
  "timestamp": "2026-11-21T10:00:05.000Z",
  "type": "fire",
  "confidence": 0.92,
  "position": null
}
```

### Preview

Previews carry the same event ID and their own frame capture timestamp. The example uses a tiny valid JPEG. Live previews contain the annotated image and are capped at one per second and 120,000 JSON bytes by default.

```json
{
  "event_id": "drone01-fire-example",
  "timestamp": "2026-11-21T10:00:06.000Z",
  "image_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAABf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8APf/Z"
}
```

Match incidents and previews by `(drone_id, event_id)` even if the preview arrives first. Use the same pair to ignore duplicate incident metadata. To show a preview in a browser, set the image URL to `"data:image/jpeg;base64," + payload.image_base64`. An incident can arrive with no preview, for example when the image was oversized or a QoS 0 delivery was lost.

## Delivery and a handoff check

Gateway-to-broker and broker-to-subscriber are separate MQTT exchanges. The subscriber's delivery QoS is limited by both the published QoS and the broker's grant for the subscription. QoS 0 has no acknowledgement and individual updates can be lost. On each QoS 1 exchange the receiver sends `PUBACK`, and retries can produce duplicates. Retention stores the latest status for new subscribers. The public broker provides no durable incident history for this demo. [MQTT 3.1.1 specification](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)

From the DS_Drone root in PowerShell, run the isolated logger check with the base dependencies installed. It publishes labelled synthetic incidents, keeps the same ID separately for two drones, and verifies that a restarted logger skips previously stored IDs. It prints the generated JSONL path. The optional second command checks real incident/preview matching on the configured gateway. Start it before showing the fire photo to the camera.

```powershell
& .\.venv\Scripts\python.exe -m Test.check_incident_logger
& .\.venv\Scripts\python.exe -m Test.check_fire_mqtt --watch
```
