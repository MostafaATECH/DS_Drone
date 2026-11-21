"""Check real MQTT delivery using synthetic messages in a unique test namespace."""

import json
import socket
import time
from queue import Empty, Queue
from threading import Event
from uuid import uuid4

import paho.mqtt.client as mqtt

from MQTT_Communication.publish_mqtt import MqttPublisher, OFFLINE_STATUS
from MQTT_Communication.run_gateway import ROOT, load_settings, utc_now


TIMEOUT = 8
TEST_BOARD = {"stm_connected": True, "streaming": True,
              "stream_state": "STREAMING", "imu_status": "READY"}
TEST_UNPLUGGED_BOARD = {"stm_connected": False, "streaming": False,
                        "stream_state": "DISCONNECTED", "imu_status": "UNAVAILABLE"}


def make_client(settings, client_id):
    """Create a test-only MQTT 3.1.1 client that will not reconnect after TCP loss."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                         protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.connect_timeout = TIMEOUT
    if settings["username"]:
        client.username_pw_set(settings["username"], settings["password"])
    if settings["tls"]:
        client.tls_set()
    return client


def start_client(client, settings):
    """Open the broker connection and start processing MQTT traffic."""
    client.connect(settings["host"], settings["port"], keepalive=settings["keepalive_s"])
    client.loop_start()


def subscribe(client, settings, topic):
    """Wait for the broker's subscription acknowledgement and return the subscriber inbox."""
    inbox = Queue()
    ready = Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        """Subscribe only after the broker accepts the MQTT connection."""
        if not reason_code.is_failure:
            client.subscribe(topic, qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        """Signal readiness after the broker grants the subscription."""
        if reason_codes and not reason_codes[0].is_failure:
            ready.set()

    def on_message(client, userdata, message):
        """Pass received messages to the test thread for inspection."""
        inbox.put(message)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    start_client(client, settings)
    if not ready.wait(TIMEOUT):
        raise TimeoutError(f"no successful SUBACK for {topic}")
    return inbox


def receive(inbox, topic):
    """Wait for the matching MQTT message and report a receipt timeout clearly."""
    deadline = time.monotonic() + TIMEOUT
    try:
        while time.monotonic() < deadline:
            message = inbox.get(timeout=max(0, deadline - time.monotonic()))
            if message.topic == topic:
                return message
    except Empty:
        pass
    raise TimeoutError(f"no message received on {topic}")


def require(condition, description):
    """Fail a test explicitly even when Python assertions are disabled."""
    if not condition:
        raise ValueError(description)


def wait_for_connection(publisher, previous=None):
    """Wait for a new successful CONNACK from the gateway's network loop."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        connected_at = publisher.connected_at
        if connected_at is not None and connected_at != previous:
            return connected_at
        time.sleep(0.02)
    raise TimeoutError("gateway did not receive a new CONNACK")


def check_round_trip(settings):
    """Verify received telemetry, late retained status, clean shutdown, and Last Will."""
    token = uuid4().hex[:10]
    test_root = f"{settings['topic_root']}/test-{token}"
    print(f"SYNTHETIC MQTT TEST: {settings['host']}:{settings['port']} on {test_root}/#", flush=True)
    publisher = MqttPublisher({**settings, "topic_root": test_root, "drone_id": "synthetic",
                               "client_id": f"ds-gw-{token}"})
    observer = make_client(settings, f"ds-sub-{token}")
    late_subscriber = make_client(settings, f"ds-late-{token}")
    topic = publisher.status_topic
    try:
        inbox = subscribe(observer, settings, f"{test_root}/#")
        publisher.start()
        initial_connection = wait_for_connection(publisher)

        fixture = {"synthetic": True, "test_id": token, "timestamp": utc_now()}
        now = time.monotonic()
        publisher.publish_update(now, utc_now(), TEST_BOARD, fixture, now)
        # An observer for the telemetry topic verifies QoS and actual receipt.
        telemetry = receive(inbox, publisher.telemetry_topic)
        require(json.loads(telemetry.payload) == fixture and telemetry.qos == 0 and not telemetry.retain,
                "telemetry payload, QoS or retain flag differed")
        print("PASS: synthetic QoS 0 telemetry received")

        late_inbox = subscribe(late_subscriber, settings, topic)
        retained = receive(late_inbox, topic)
        status = json.loads(retained.payload)
        require(retained.retain and retained.qos == 1 and status.get("online") is True
                and status.get("camera_status") == "DISABLED", "retained online status differed")
        print("PASS: late subscriber received retained QoS 1 online status")

        connection = publisher.client.socket()
        require(connection is not None, "the Will test publisher lost its socket early")
        # Close the TCP stream without sending an MQTT DISCONNECT packet.
        connection.shutdown(socket.SHUT_RDWR)
        will = receive(late_inbox, topic)
        require(will.payload == OFFLINE_STATUS.encode() and will.qos == 1,
                "broker did not deliver the expected Last Will")
        late_subscriber.subscribe(topic, qos=1)
        retained_will = receive(late_inbox, topic)
        require(retained_will.retain and retained_will.payload == OFFLINE_STATUS.encode(),
                "the broker did not retain the offline Last Will")
        print("PASS: broker delivered and retained the offline Last Will after abrupt TCP loss")

        wait_for_connection(publisher, initial_connection)
        publisher.publish_update(time.monotonic(), utc_now(), TEST_UNPLUGGED_BOARD)
        status = json.loads(receive(late_inbox, topic).payload)
        require(status.get("online") is True and status.get("stm_connected") is False,
                "the reconnected gateway did not report its current board state")
        print("PASS: gateway reconnected and reported STM32 disconnected while online")

        publisher.close()
        offline = receive(late_inbox, topic)
        require(offline.payload == OFFLINE_STATUS.encode() and offline.qos == 1,
                "clean shutdown did not deliver the offline payload")
        print("PASS: clean gateway shutdown delivered offline status")
    finally:
        try:
            publisher.close()
            if observer.is_connected():
                cleanup = observer.publish(topic, b"", qos=1, retain=True)
                cleanup.wait_for_publish(TIMEOUT)
                require(cleanup.is_published(), "the broker did not acknowledge retained test cleanup")
        finally:
            for client in (late_subscriber, observer):
                client.disconnect()
                client.loop_stop()


def main():
    """Run the round trip and return a nonzero exit code for a failed network check."""
    try:
        check_round_trip(load_settings(ROOT)["mqtt"])
    except (OSError, ValueError, RuntimeError) as error:
        print(f"FAIL: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
