"""Read the settings shared by the gateway and local checks."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
# Match quoted strings first so a URL or other value containing // stays intact.
STRINGS_OR_LINE_COMMENTS = re.compile(r'("(?:\\[^\r\n]|[^"\\\r\n])*")|//[^\r\n]*')


def load_config(root=ROOT):
    """Read config.json, allowing single-line comments outside strings."""
    contents = (root / "config.json").read_text(encoding="utf-8-sig")
    settings = json.loads(STRINGS_OR_LINE_COMMENTS.sub(lambda match: match[1] or "", contents))
    if not isinstance(settings, dict):
        raise ValueError("config.json must contain a JSON object")
    for section in ("serial", "gps", "mqtt", "camera"):
        if not isinstance(settings.get(section), dict):
            raise ValueError(f"config.json must contain a '{section}' object")
    return settings
