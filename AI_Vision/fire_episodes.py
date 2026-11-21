"""Confirm a fire episode across live frames and keep its ID until observed absence."""

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class FireIncident:
    """The local incident created by the frame that confirmed an episode."""

    event_id: str
    timestamp: str
    confidence: float
    observed_at: float
    type: str = "fire"


class FireEpisodeTracker:
    """Track one incident for a continuous period of accepted fire detections."""

    def __init__(self, drone_id, confirmation_frames, absence_s):
        """Set the positive-frame count and the observed absence needed to rearm."""
        self.drone_id = drone_id
        self.confirmation_frames = confirmation_frames
        self.absence_s = absence_s
        self.positive_frames = 0
        self.absent_since = None
        self.active_incident = None

    def interrupt(self):
        """Break frame continuity while preserving any confirmed incident during an outage."""
        self.positive_frames = 0
        self.absent_since = None

    def observe(self, now, timestamp, confidence):
        """Count one processed frame and return an incident only when one is first confirmed."""
        if self.absent_since is not None and now - self.absent_since >= self.absence_s:
            self.active_incident = None
            self.absent_since = None

        if confidence is None:
            self.positive_frames = 0
            if self.active_incident is not None and self.absent_since is None:
                self.absent_since = now
            return None

        self.absent_since = None
        if self.active_incident is not None:
            return None
        self.positive_frames += 1
        if self.positive_frames < self.confirmation_frames:
            return None

        event_id = f"{self.drone_id}-fire-{uuid4().hex}"
        self.active_incident = FireIncident(event_id, timestamp, confidence, now)
        return self.active_incident
