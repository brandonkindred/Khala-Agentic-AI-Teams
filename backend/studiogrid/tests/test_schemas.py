from studiogrid.runtime.errors import SchemaValidationError
from studiogrid.runtime.validators.schema_validator import validate_envelope, validate_payload


def test_validate_envelope_accepts_artifact():
    validate_envelope({"kind": "ARTIFACT", "payload": {}})


def test_validate_payload_requires_artifact_fields():
    try:
        validate_payload("ARTIFACT", {"artifact_type": "x"})
    except SchemaValidationError:
        return
    raise AssertionError("Expected SchemaValidationError")
