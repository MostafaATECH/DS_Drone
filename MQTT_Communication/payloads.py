"""Build the payloads shared with the dashboard."""

import base64
import json


def status_payload(timestamp, board_status, camera_status):
    return {"online": True, "timestamp": timestamp, **board_status, "camera_status": camera_status}


def telemetry_payload(timestamp, tracker_state, position, accel_m_s2, gyro_rad_s):
    return {
        "timestamp": timestamp,
        "tracker_state": tracker_state,
        "position": position,
        "accel_m_s2": dict(zip("xyz", accel_m_s2)),
        "gyro_rad_s": dict(zip("xyz", gyro_rad_s)),
    }


def incident_payload(event_id, timestamp, incident_type, confidence, position):
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "type": incident_type,
        "confidence": confidence,
        "position": position,
    }


def encode_preview_payload(event_id, timestamp, jpeg, max_bytes):
    """Return the JSON preview and its size, or None and the size if it is too large."""
    payload = {"event_id": event_id, "timestamp": timestamp, "image_base64": ""}
    separators = (",", ":")
    # Base64 encodes each group of up to three bytes as four characters.
    encoded_size = 4 * ((len(jpeg) + 2) // 3)
    payload_size = len(json.dumps(payload, separators=separators).encode("utf-8")) + encoded_size
    if payload_size > max_bytes:
        return None, payload_size

    payload["image_base64"] = base64.b64encode(jpeg).decode("ascii")
    return json.dumps(payload, separators=separators), payload_size
