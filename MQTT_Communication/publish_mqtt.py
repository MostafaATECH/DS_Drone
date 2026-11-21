"""Publish gateway status, telemetry, and fire messages on one MQTT connection."""

import base64
import json
import logging
import socket
import time
from threading import Lock

import paho.mqtt.client as mqtt


LOGGER = logging.getLogger(__name__)
OFFLINE_STATUS = '{"online":false}'
STATUS_INTERVAL_S = 1.0
PREVIEW_INTERVAL_S = 1.0


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
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect

        self.connection_lock = Lock()
        self.connected_at = None
        self.last_status = None
        self.next_status_at = 0.0
        self.next_telemetry_at = 0.0
        self.next_preview_at = 0.0
        self.last_preview = None

    def start(self):
        """Start connecting on Paho's worker thread without blocking serial polling."""
        self.client.connect_async(self.settings["host"], self.settings["port"],
                                  keepalive=self.settings["keepalive_s"])
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """Record the successful connection time so the next poll sends fresh status."""
        if reason_code.is_failure:
            LOGGER.warning("MQTT connection refused: %s", reason_code)
            return
        with self.connection_lock:
            self.connected_at = time.monotonic()
        LOGGER.info("MQTT connected as %s", self.client_id)

    def _on_connect_fail(self, client, userdata):
        """Report unsuccessful TCP connection attempts while Paho keeps retrying."""
        LOGGER.warning("MQTT could not connect to %s:%s. Retrying.",
                       self.settings["host"], self.settings["port"])

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Stop publishing until Paho receives another successful CONNACK."""
        with self.connection_lock:
            self.connected_at = None
        if reason_code.is_failure:
            LOGGER.warning("MQTT connection lost: %s. Retrying.", reason_code)

    def _connection_at(self, now):
        """Return the current broker connection time when this poll can publish."""
        with self.connection_lock:
            started = self.connected_at
            if started is not None and now >= started and self.client.is_connected():
                return started
        return None

    def publish_update(self, now, timestamp, board_status, telemetry=None, measurement_at=None,
                       *, camera_status="DISABLED"):
        """Publish due status and send only telemetry received during this broker connection."""
        connected_at = self._connection_at(now)
        if connected_at is None:
            return

        # Paho takes its own queue lock for QoS 1, so publish status without the callback lock.
        states = (connected_at, board_status, camera_status)
        if states != self.last_status or now >= self.next_status_at:
            status = {"online": True, "timestamp": timestamp, **board_status,
                      "camera_status": camera_status}
            info = self.client.publish(self.status_topic, json.dumps(status), qos=1, retain=True)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self.last_status = (connected_at, board_status.copy(), camera_status)
                self.next_status_at = now + STATUS_INTERVAL_S

        if (not board_status["streaming"] or telemetry is None or measurement_at is None
                or measurement_at < connected_at or now < self.next_telemetry_at):
            return
        with self.connection_lock:
            if self.connected_at == connected_at and self.client.is_connected():
                info = self.client.publish(self.telemetry_topic, json.dumps(telemetry), qos=0, retain=False)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    self.next_telemetry_at = now + 1.0

    def publish_incident(self, now, incident):
        """Hand one incident to Paho for asynchronous QoS 1 delivery and retry."""
        if self._connection_at(now) is None:
            return False
        info = self.client.publish(self.incident_topic, json.dumps(incident), qos=1, retain=False)
        # Paho retains a QoS 1 packet if the socket disappears during publish.
        return info.rc in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN)

    def publish_preview(self, now, frame):
        """Offer one fresh annotated JPEG per second, with a cap on the full JSON payload."""
        if frame is None or not frame.event_id or not frame.detections or now < self.next_preview_at:
            return
        connected_at = self._connection_at(now)
        key = (frame.event_id, frame.captured_monotonic)
        if connected_at is None or not connected_at <= frame.captured_monotonic <= now or key == self.last_preview:
            return

        self.last_preview = key
        self.next_preview_at = now + PREVIEW_INTERVAL_S
        payload = {"event_id": frame.event_id, "timestamp": frame.captured_at, "image_base64": ""}
        encoded_size = 4 * ((len(frame.jpeg) + 2) // 3)
        payload_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) + encoded_size
        limit = self.settings["preview_max_bytes"]
        if payload_size > limit:
            LOGGER.warning("Skipped preview for %s (%d bytes, limit %d)", frame.event_id, payload_size, limit)
            return

        payload["image_base64"] = base64.b64encode(frame.jpeg).decode("ascii")
        with self.connection_lock:
            if self.connected_at == connected_at and self.client.is_connected():
                self.client.publish(self.preview_topic, json.dumps(payload, separators=(",", ":")),
                                    qos=0, retain=False)

    def close(self):
        """Publish offline, wait briefly for PUBACK, then stop the MQTT network loop."""
        try:
            if self.client.is_connected():
                info = self.client.publish(self.status_topic, OFFLINE_STATUS, qos=1, retain=True)
                try:
                    info.wait_for_publish(timeout=2)
                    confirmed = info.is_published()
                except (RuntimeError, ValueError):
                    confirmed = False
                if confirmed:
                    LOGGER.info("MQTT offline status acknowledged")
                else:
                    LOGGER.warning("MQTT offline status was not acknowledged. Closing TCP for Last Will.")
                    connection = self.client.socket()
                    if connection is not None:
                        try:
                            connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
        finally:
            self.client.disconnect()
            self.client.loop_stop()
