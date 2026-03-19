from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def persist_artifact(path: str, payload: Any) -> str:
    artifact = Path(path)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(artifact)


def load_artifact(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
