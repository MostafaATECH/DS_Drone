"""Assemble confirmed fire incidents and hand camera messages to the MQTT publisher."""

import logging
from collections import deque


LOGGER = logging.getLogger(__name__)


class CameraMessages:
    """Keep a bounded in-memory queue of incident snapshots while MQTT is unavailable."""

    def __init__(self, publisher, replay):
        """Use the gateway's existing MQTT connection and GPS replay."""
        self.publisher = publisher
        self.replay = replay
        self.pending = deque(maxlen=16)
        self.retry_at = 0.0

    def publish_updates(self, now, frame, incidents):
        """Queue each incident with its original fix, retry metadata, and offer the latest preview."""
        for incident in incidents:
            position = None
            if self.replay is not None:
                position = self.replay.latest_position(incident.observed_at, timestamp=incident.timestamp)
            if len(self.pending) == self.pending.maxlen:
                LOGGER.warning("Incident queue full. Dropping %s", self.pending[0]["event_id"])
            self.pending.append({
                "event_id": incident.event_id,
                "timestamp": incident.timestamp,
                "type": incident.type,
                "confidence": incident.confidence,
                "position": position,
            })
            LOGGER.info("Confirmed %s %s (%.2f, %s)", incident.type, incident.event_id,
                        incident.confidence, "simulated observer position" if position else "position unavailable")

        if self.pending and now >= self.retry_at:
            if self.publisher.publish_incident(now, self.pending[0]):
                sent = self.pending.popleft()
                LOGGER.info("Submitted incident %s to the MQTT client", sent["event_id"])
            else:
                self.retry_at = now + 1.0

        self.publisher.publish_preview(now, frame)
