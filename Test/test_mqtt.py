"""Check MQTT gateway behavior with synthetic board states and a mocked Paho client."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import paho.mqtt.client as mqtt

from MQTT_Communication.publish_mqtt import MqttPublisher, OFFLINE_STATUS
from project_config import load_config


SETTINGS = {**load_config()["mqtt"], "drone_id": "drone01", "client_id": None,
            "host": "broker.hivemq.com", "port": 1883, "keepalive_s": 15,
            "username": None, "password": None, "tls": False}
NOW = "2026-11-21T10:00:00Z"
STREAMING = {"stm_connected": True, "streaming": True,
             "stream_state": "STREAMING", "imu_status": "READY"}
PAUSED = {**STREAMING, "streaming": False, "stream_state": "PAUSED"}
UNPLUGGED = {"stm_connected": False, "streaming": False,
             "stream_state": "DISCONNECTED", "imu_status": "UNAVAILABLE"}
SYNTHETIC_TELEMETRY = {"synthetic": True, "timestamp": NOW}


class MqttTests(unittest.TestCase):
    """Exercise MQTT configuration, publication gates, and shutdown without a broker."""

    def setUp(self):
        """Create the gateway transport with mocked Paho network calls."""
        self.client_factory = self.enterContext(patch("MQTT_Communication.publish_mqtt.mqtt.Client"))
        self.publisher = MqttPublisher(SETTINGS)
        self.client = self.client_factory.return_value
        self.client.is_connected.return_value = True
        self.client.publish.return_value = Mock(rc=mqtt.MQTT_ERR_SUCCESS)

    def connect(self, now):
        """Simulate a successful CONNACK at a controlled monotonic time."""
        with patch("MQTT_Communication._publisher_runtime.time.monotonic", return_value=now):
            self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)

    def test_transport_settings_and_distinct_ids(self):
        """Set the retained Will before an anonymous, asynchronous MQTT 3.1.1 connection."""
        self.client_factory.assert_called_once_with(mqtt.CallbackAPIVersion.VERSION2,
                                                    client_id="dsdrone-4f7c92-drone01",
                                                    protocol=mqtt.MQTTv311, clean_session=True)
        self.publisher.start()
        root = SETTINGS["topic_root"]
        self.assertEqual(self.publisher.telemetry_topic, f"{root}/drone01/telemetry")
        calls = self.client.method_calls
        self.assertLess(calls.index(call.will_set(self.publisher.status_topic, OFFLINE_STATUS,
                                                 qos=1, retain=True)),
                        calls.index(call.connect_async("broker.hivemq.com", 1883, keepalive=15)))
        self.client.will_set.assert_called_once_with(f"{root}/drone01/status", OFFLINE_STATUS,
                                                    qos=1, retain=True)
        self.client.connect_async.assert_called_once_with("broker.hivemq.com", 1883, keepalive=15)
        self.client.loop_start.assert_called_once()
        self.client.username_pw_set.assert_not_called()
        self.client.tls_set.assert_not_called()
        second = MqttPublisher({**SETTINGS, "drone_id": "drone02"})
        self.assertEqual(second.client_id, "dsdrone-4f7c92-drone02")

    def test_status_changes_and_heartbeat(self):
        """Publish full online status after connection, on B1 and USB changes, and once per second."""
        self.publisher.publish_update(0, NOW, STREAMING)
        self.client.publish.assert_not_called()
        self.connect(1)
        self.publisher.publish_update(1, NOW, STREAMING)
        self.publisher.publish_update(1.1, NOW, STREAMING)
        self.publisher.publish_update(1.2, NOW, PAUSED, SYNTHETIC_TELEMETRY, 1.2)
        self.publisher.publish_update(1.3, NOW, UNPLUGGED)
        self.publisher.publish_update(2.4, NOW, UNPLUGGED)
        self.assertEqual(self.client.publish.call_count, 4)
        for invocation, state in zip(self.client.publish.call_args_list,
                                     [STREAMING, PAUSED, UNPLUGGED, UNPLUGGED]):
            self.assertEqual(invocation.args[0], self.publisher.status_topic)
            self.assertEqual(invocation.kwargs, {"qos": 1, "retain": True})
            self.assertEqual(json.loads(invocation.args[1]),
                             {"online": True, "timestamp": NOW, **state, "camera_status": "DISABLED"})
        self.client.disconnect.assert_not_called()

    def test_reconnect_drops_old_telemetry(self):
        """Drop samples received during outages and keep fresh telemetry at up to 1 Hz."""
        self.connect(1)
        self.publisher.publish_update(1.1, NOW, STREAMING, SYNTHETIC_TELEMETRY, 0.9)
        self.publisher.publish_update(1.2, NOW, STREAMING, SYNTHETIC_TELEMETRY, 1.2)
        self.publisher.publish_update(1.5, NOW, STREAMING, SYNTHETIC_TELEMETRY, 1.5)
        self.client.on_disconnect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.publisher.publish_update(2, NOW, STREAMING, SYNTHETIC_TELEMETRY, 2)
        self.connect(3)
        self.publisher.publish_update(3.1, NOW, STREAMING, SYNTHETIC_TELEMETRY, 2)
        self.publisher.publish_update(3.2, NOW, STREAMING, SYNTHETIC_TELEMETRY, 3.2)
        self.assertEqual([invocation.args[0] for invocation in self.client.publish.call_args_list],
                         [self.publisher.status_topic, self.publisher.telemetry_topic] * 2)
        self.client.publish.assert_called_with(self.publisher.telemetry_topic,
                                               json.dumps(SYNTHETIC_TELEMETRY), qos=0, retain=False)

    def test_offline_ack_before_disconnect(self):
        """Publish retained offline status and wait briefly for PUBACK before leaving."""
        self.client.publish.return_value.is_published.return_value = True
        self.publisher.close()
        self.assertEqual(self.client.mock_calls[-5:], [
            call.publish(self.publisher.status_topic, OFFLINE_STATUS, qos=1, retain=True),
            call.publish().wait_for_publish(timeout=2),
            call.publish().is_published(),
            call.disconnect(),
            call.loop_stop(),
        ])

    def test_demo_will_waits_for_broker_without_publishing_offline(self):
        """Keep TCP open silently while waiting for the broker to close it."""
        connection = self.client.socket.return_value
        connection.recv.side_effect = [b"pending acknowledgement", b""]
        self.publisher.close(demo_will=True)
        self.client.loop_stop.assert_called_once_with()
        self.client.publish.assert_not_called()
        self.client.disconnect.assert_not_called()
        self.assertEqual(connection.recv.call_args_list, [call(4096), call(4096)])
        self.assertEqual(connection.mock_calls[-1], call.close())
