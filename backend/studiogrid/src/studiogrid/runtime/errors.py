class StudioGridError(Exception):
    """Base error."""


class TransientError(StudioGridError):
    """Retryable: network, timeouts, temporary service failures."""


class SchemaValidationError(StudioGridError):
    """Non-retryable: agent produced invalid schema output."""


class PermissionError(StudioGridError):
    """Non-retryable: forbidden tool/action requested."""


class PolicyViolationError(StudioGridError):
    """Non-retryable: violates project guardrails."""


class GateFailedError(StudioGridError):
    """Expected gate outcome that triggers a revision loop."""
