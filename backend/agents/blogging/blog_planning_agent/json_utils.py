"""JSON sanitization for planning LLM output (aligned with ollama client behavior)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict

_JSON_NOISE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]")


def strip_json_noise(s: str) -> str:
    if not s:
        return s
    s = s.replace("\ufeff", "")
    return _JSON_NOISE_RE.sub("", s)


def repair_json_commas(s: str) -> str:
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def parse_json_object(raw: str) -> Dict[str, Any]:
    """Parse a JSON object from model output; raises JSONDecodeError on failure."""
    text = strip_json_noise(raw)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    repaired = repair_json_commas(text)
    try:
        data = json.loads(repaired)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        raw_obj = m.group(0)
        try:
            data = json.loads(raw_obj)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            data = json.loads(repair_json_commas(raw_obj))
            if isinstance(data, dict):
                return data
    raise json.JSONDecodeError("No JSON object found", raw, 0)
