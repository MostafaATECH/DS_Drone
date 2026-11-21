"""Read the settings shared by the gateway and local checks."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_config(root=ROOT):
    """Load the example settings and apply the per-PC overrides by section."""
    settings = json.loads((root / "config.example.json").read_text(encoding="utf-8-sig"))
    override_path = root / "config.local.json"
    if not override_path.exists():
        return settings

    overrides = json.loads(override_path.read_text(encoding="utf-8-sig"))
    if not isinstance(overrides, dict):
        raise ValueError("config.local.json must contain an object")
    for section, values in overrides.items():
        if section not in settings or not isinstance(values, dict):
            raise ValueError(f"invalid configuration section: {section}")
        unknown = values.keys() - settings[section].keys()
        if unknown:
            raise ValueError(f"unknown {section} setting: {', '.join(sorted(unknown))}")
        settings[section].update(values)
    return settings
