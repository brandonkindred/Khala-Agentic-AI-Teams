from __future__ import annotations

from studiogrid.runtime.errors import SchemaValidationError

VALID_KINDS = {"TASK", "ARTIFACT", "REVIEW", "DECISION", "EVENT", "ERROR"}


def validate_envelope(envelope: dict) -> None:
    if not isinstance(envelope, dict):
        raise SchemaValidationError("Envelope must be an object")
    kind = envelope.get("kind")
    if kind not in VALID_KINDS:
        raise SchemaValidationError(f"Invalid kind: {kind}")
    if "payload" not in envelope:
        raise SchemaValidationError("Envelope must include payload")


def validate_payload(kind: str, payload: dict) -> None:
    if kind == "ARTIFACT":
        required = {"artifact_type", "format", "payload"}
    elif kind == "REVIEW":
        required = {"gate", "passed", "required_fixes"}
    elif kind == "DECISION":
        required = {"title", "options"}
    else:
        required = set()
    missing = [field for field in required if field not in payload]
    if missing:
        raise SchemaValidationError(f"Missing required fields: {', '.join(sorted(missing))}")
