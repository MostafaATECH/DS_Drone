"""Replay the simulated GPS route while the STM32 supplies fresh readings."""

import json
from collections import deque


def load_route(path):
    """Read and validate the supplied route, returning positions, interval, and loop flag."""
    route = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(route, dict):
        raise ValueError("expected a GPS route object")
    interval_ms = route.get("sample_interval_ms")
    if type(interval_ms) is not int or interval_ms <= 0:
        raise ValueError("sample_interval_ms must be a positive integer")
    loop = route.get("loop")
    if type(loop) is not bool:
        raise ValueError("loop must be true or false")
    samples = route.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("samples must be a nonempty list")

    positions = []
    for index, sample in enumerate(samples, start=1):
        position = sample.get("position") if isinstance(sample, dict) else None
        if not isinstance(position, dict) or position.get("source") != "simulated":
            raise ValueError(f"sample {index} must contain a simulated position")
        for key, limit in (("latitude", 90), ("longitude", 180)):
            value = position.get(key)
            if type(value) not in (int, float) or not -limit <= value <= limit:
                raise ValueError(f"sample {index} has an invalid {key}")
        positions.append({key: position[key] for key in ("latitude", "longitude", "source")})
    return positions, interval_ms / 1000, loop


class GpsReplay:
    """Keep the next route index and recent timed fixes for telemetry and incidents."""

    def __init__(self, path, freshness_s):
        """Load the route once and start at its first sample."""
        self.positions, self.interval_s, self.loop = load_route(path)
        self.freshness_s = freshness_s
        self.cursor = 0
        self.next_fix_at = None
        self.fixes = deque(maxlen=128)

    def update(self, now, timestamp, active):
        """Emit a due fix only during active streaming. After a pause, emit the next point."""
        if not active:
            self.next_fix_at = None
            return
        if self.cursor == len(self.positions):
            return
        if self.next_fix_at is not None and now < self.next_fix_at:
            return

        fix = {**self.positions[self.cursor], "fix_timestamp": timestamp}
        self.fixes.append((now, fix))
        self.next_fix_at = now + self.interval_s
        self.cursor += 1
        if self.loop and self.cursor == len(self.positions):
            self.cursor = 0

    def latest_position(self, now):
        """Copy the freshest fix that already existed at the supplied monotonic time."""
        for fix_at, fix in reversed(self.fixes):
            if fix_at <= now:
                return fix.copy() if now - fix_at <= self.freshness_s else None
        return None
