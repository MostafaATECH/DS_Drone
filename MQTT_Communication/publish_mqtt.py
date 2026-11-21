"""Publish gateway status and live telemetry to the configured MQTT broker."""

import json
import logging
import socket
import time
from threading import Lock

import paho.mqtt.client as mqtt


LOGGER = logging.getLogger(__name__)
OFFLINE_STATUS = '{"online":false}'
STATUS_INTERVAL_S = 1.0


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

    def publish_update(self, now, timestamp, board_status, telemetry=None, measurement_at=None):
        """Publish due status and send only telemetry received during this broker connection."""
        with self.connection_lock:
            connected_at = self.connected_at
        if connected_at is None or now < connected_at or not self.client.is_connected():
            return

        # Paho takes its own queue lock for QoS 1, so publish status without the callback lock.
        if (connected_at, board_status) != self.last_status or now >= self.next_status_at:
            status = {"online": True, "timestamp": timestamp, **board_status,
                      "camera_status": "DISABLED"}
            info = self.client.publish(self.status_topic, json.dumps(status), qos=1, retain=True)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self.last_status = (connected_at, board_status.copy())
                self.next_status_at = now + STATUS_INTERVAL_S

        if (not board_status["streaming"] or telemetry is None or measurement_at is None
                or measurement_at < connected_at or now < self.next_telemetry_at):
            return
        with self.connection_lock:
            if self.connected_at == connected_at and self.client.is_connected():
                info = self.client.publish(self.telemetry_topic, json.dumps(telemetry), qos=0, retain=False)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    self.next_telemetry_at = now + 1.0

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
