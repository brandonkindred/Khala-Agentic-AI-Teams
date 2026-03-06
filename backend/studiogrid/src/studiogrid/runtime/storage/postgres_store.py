from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any


class PostgresStore:
    """In-memory fallback store with the same contract as a Postgres-backed store."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.artifact_latest: dict[tuple[str, str], str] = {}
        self.idempotency: dict[str, Any] = {}
        self._artifact_versions: defaultdict[tuple[str, str], int] = defaultdict(int)

    def put_idempotency(self, key: str, value: Any) -> None:
        self.idempotency[key] = deepcopy(value)

    def get_idempotency(self, key: str) -> Any | None:
        value = self.idempotency.get(key)
        return deepcopy(value)

    def next_artifact_version(self, run_id: str, artifact_type: str) -> int:
        row_key = (run_id, artifact_type)
        self._artifact_versions[row_key] += 1
        return self._artifact_versions[row_key]
