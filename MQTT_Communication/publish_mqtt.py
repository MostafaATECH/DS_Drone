"""Publish gateway status, telemetry, and fire messages on one MQTT connection."""

import json
import logging

import paho.mqtt.client as mqtt

from ._publisher_runtime import PublisherRuntime
from .payloads import encode_preview_payload, status_payload


LOGGER = logging.getLogger(__name__)
OFFLINE_STATUS = '{"online":false}'


def validate_mqtt_settings(settings):
    """Check broker settings and require one topic level each for the root and drone ID."""
    for key in ("host", "topic_root", "drone_id"):
        value = settings[key]
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(f"mqtt.{key} must be a nonempty string")
    for key in ("topic_root", "drone_id"):
        if any(char in settings[key] for char in "/+#\0"):
            raise ValueError(f"mqtt.{key} must be a single topic level without wildcards")
    for key in ("port", "keepalive_s"):
        if type(settings[key]) is not int or not 1 <= settings[key] <= 65535:
            raise ValueError(f"mqtt.{key} must be an integer between 1 and 65535")
    for key in ("client_id", "username", "password"):
        if settings[key] is not None and not isinstance(settings[key], str):
            raise ValueError(f"mqtt.{key} must be a string or null")
    if settings["password"] and not settings["username"]:
        raise ValueError("mqtt.username is required when a password is set")
    if type(settings["tls"]) is not bool:
        raise ValueError("mqtt.tls must be true or false")
    limit = settings["preview_max_bytes"]
    if type(limit) is not int or limit < 1:
        raise ValueError("mqtt.preview_max_bytes must be a positive integer")


def drone_topic(topic_root, drone_id, message_type):
    """Build the three-level MQTT topic used by the gateway and dashboard."""
    return f"{topic_root}/{drone_id}/{message_type}"


class MqttPublisher:
    """Track each broker connection and publish updates from the gateway polling loop."""

    def __init__(self, settings):
        """Set up a Paho v2 client and its offline Will before starting the network loop."""
        self.settings = settings
        self.client_id = settings["client_id"] or f"dsdrone-4f7c92-{settings['drone_id']}"
        self.status_topic = drone_topic(settings["topic_root"], settings["drone_id"], "status")
        self.telemetry_topic = drone_topic(settings["topic_root"], settings["drone_id"], "telemetry")
        self.incident_topic = drone_topic(settings["topic_root"], settings["drone_id"], "incident")
        self.preview_topic = drone_topic(settings["topic_root"], settings["drone_id"], "preview")
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id,
                                  protocol=mqtt.MQTTv311, clean_session=True)
        self.client.will_set(self.status_topic, OFFLINE_STATUS, qos=1, retain=True)
        if settings["username"]:
            self.client.username_pw_set(settings["username"], settings["password"])
        if settings["tls"]:
            self.client.tls_set()
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.max_queued_messages_set(4)
        self._runtime = PublisherRuntime(self.client, settings, self.client_id)
        self.client.on_connect = self._runtime.on_connect
        self.client.on_connect_fail = self._runtime.on_connect_fail
        self.client.on_disconnect = self._runtime.on_disconnect

    @property
    def connected_at(self):
        """Return the start time of the current connection, if connected."""
        return self._runtime.connected_at

    def start(self):
        """Start connecting on Paho's worker thread without blocking serial polling."""
        self.client.connect_async(self.settings["host"], self.settings["port"],
                                  keepalive=self.settings["keepalive_s"])
        self.client.loop_start()

    def publish_update(self, now, timestamp, board_status, telemetry=None, measurement_at=None,
                       *, camera_status="DISABLED"):
        """Publish due status and send only telemetry received during this broker connection."""
        runtime = self._runtime
        connected_at = runtime.connection_at(now)
        if connected_at is None:
            return

        # Paho takes its own queue lock for QoS 1, so publish status without the callback lock.
        if runtime.status_due(now, connected_at, board_status, camera_status):
            status = status_payload(timestamp, board_status, camera_status)
            info = self.client.publish(self.status_topic, json.dumps(status), qos=1, retain=True)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                runtime.record_status(now, connected_at, board_status, camera_status)

        if (not board_status["streaming"] or telemetry is None
                or not runtime.telemetry_due(now, connected_at, measurement_at)):
            return
        with runtime.connection_lock:
            if runtime.connected_at == connected_at and self.client.is_connected():
                info = self.client.publish(self.telemetry_topic, json.dumps(telemetry), qos=0, retain=False)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    runtime.record_telemetry(now)

    def publish_incident(self, now, incident):
        """Hand one incident to Paho for asynchronous QoS 1 delivery and retry."""
        if self._runtime.connection_at(now) is None:
            return False
        info = self.client.publish(self.incident_topic, json.dumps(incident), qos=1, retain=False)
        # Paho retains a QoS 1 packet if the socket disappears during publish.
        return info.rc in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN)

    def publish_preview(self, now, frame):
        """Offer one fresh annotated JPEG per second, with a cap on the full JSON payload."""
        if frame is None or not frame.event_id or not frame.detections:
            return
        runtime = self._runtime
        connected_at = runtime.claim_preview(now, frame)
        if connected_at is None:
            return

        limit = self.settings["preview_max_bytes"]
        payload, payload_size = encode_preview_payload(frame.event_id, frame.captured_at, frame.jpeg, limit)
        if payload is None:
            LOGGER.warning("Skipped preview for %s (%d bytes, limit %d)", frame.event_id, payload_size, limit)
            return

        with runtime.connection_lock:
            if runtime.connected_at == connected_at and self.client.is_connected():
                self.client.publish(self.preview_topic, payload, qos=0, retain=False)

    def close(self, *, demo_will=False):
        """Report a clean stop or demonstrate a silent connection failure."""
        if demo_will:
            self.client.loop_stop()
            self._runtime.wait_for_keepalive_timeout()
            return

        try:
            if self.client.is_connected():
                info = self.client.publish(self.status_topic, OFFLINE_STATUS, qos=1, retain=True)
                self._runtime.wait_for_offline_status(info)
        finally:
            self.client.disconnect()
            self.client.loop_stop()
