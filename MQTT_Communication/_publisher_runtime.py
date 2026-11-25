"""Track broker connections and the timing of outgoing updates."""

import logging
import socket
import time
from threading import Lock


LOGGER = logging.getLogger(__name__)
STATUS_INTERVAL_S = 1.0
TELEMETRY_INTERVAL_S = 1.0
PREVIEW_INTERVAL_S = 1.0


class PublisherRuntime:
    def __init__(self, client, settings, client_id):
        self.client = client
        self.settings = settings
        self.client_id = client_id
        self.connection_lock = Lock()
        self.connected_at = None
        self.last_status = None
        self.next_status_at = 0.0
        self.next_telemetry_at = 0.0
        self.next_preview_at = 0.0
        self.last_preview = None

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOGGER.warning("MQTT connection refused: %s", reason_code)
            return
        with self.connection_lock:
            self.connected_at = time.monotonic()
        LOGGER.info("MQTT connected as %s", self.client_id)

    def on_connect_fail(self, client, userdata):
        LOGGER.warning("MQTT could not connect to %s:%s. Retrying.",
                       self.settings["host"], self.settings["port"])

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self.connection_lock:
            self.connected_at = None
        if reason_code.is_failure:
            LOGGER.warning("MQTT connection lost: %s. Retrying.", reason_code)

    def connection_at(self, now):
        """Return the start time of the connection available to this polling cycle."""
        with self.connection_lock:
            started = self.connected_at
            if started is not None and now >= started and self.client.is_connected():
                return started
        return None

    def status_due(self, now, connected_at, board_status, camera_status):
        return ((connected_at, board_status, camera_status) != self.last_status
                or now >= self.next_status_at)

    def record_status(self, now, connected_at, board_status, camera_status):
        self.last_status = (connected_at, board_status.copy(), camera_status)
        self.next_status_at = now + STATUS_INTERVAL_S

    def telemetry_due(self, now, connected_at, measurement_at):
        return (measurement_at is not None and measurement_at >= connected_at
                and now >= self.next_telemetry_at)

    def record_telemetry(self, now):
        self.next_telemetry_at = now + TELEMETRY_INTERVAL_S

    def claim_preview(self, now, frame):
        """Reserve one attempt for a fresh frame and return its MQTT connection start time."""
        if now < self.next_preview_at:
            return None
        connected_at = self.connection_at(now)
        key = (frame.event_id, frame.captured_monotonic)
        if (connected_at is None or not connected_at <= frame.captured_monotonic <= now
                or key == self.last_preview):
            return None

        self.last_preview = key
        self.next_preview_at = now + PREVIEW_INTERVAL_S
        return connected_at

    def wait_for_offline_status(self, info):
        """Give the broker time to acknowledge offline status before disconnecting."""
        try:
            info.wait_for_publish(timeout=2)
            confirmed = info.is_published()
        except (RuntimeError, ValueError):
            confirmed = False
        if confirmed:
            LOGGER.info("MQTT offline status acknowledged")
            return

        LOGGER.warning("MQTT offline status was not acknowledged. Closing TCP for Last Will.")
        connection = self.client.socket()
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def wait_for_keepalive_timeout(self):
        """Leave TCP open with the MQTT loop stopped so the broker can send the Will."""
        connection = self.client.socket()
        if connection is None:
            LOGGER.warning("Last Will demo needs an active broker connection")
            return

        expected = 1.5 * self.settings["keepalive_s"]
        started = time.monotonic()
        deadline = started + expected + 5
        LOGGER.info("Last Will demo: MQTT is silent; expected timeout %.1f s after the last packet", expected)
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                connection.settimeout(remaining)
                if not connection.recv(4096):
                    LOGGER.info("Broker closed the silent connection after %.1f s", time.monotonic() - started)
                    return
        except TimeoutError:
            pass
        except OSError as error:
            LOGGER.info("Silent connection ended after %.1f s: %s", time.monotonic() - started, error)
            return
        finally:
            connection.close()

        LOGGER.warning("The broker did not close the silent connection within %.1f s", expected + 5)
