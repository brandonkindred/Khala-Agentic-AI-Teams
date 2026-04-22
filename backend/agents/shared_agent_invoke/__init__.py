"""
shared_agent_invoke — invoke shim mounted inside the agent sandbox runtime.

``mount_invoke_shim(app)`` attaches ``POST /_agents/{agent_id}/invoke`` to the
sandbox's FastAPI app. The shim reads manifests from the in-process
:mod:`agent_registry` singleton and dispatches to the agent's
``source.entrypoint`` (Python class, factory function, or plain callable)
with the request body. The per-agent guard (reject any ``agent_id`` other
than ``SANDBOX_AGENT_ID``) is enforced by middleware in
``agent_sandbox_runtime/entrypoint.py``.
"""

from .shim import InvokeEnvelope, mount_invoke_shim

__all__ = ["InvokeEnvelope", "mount_invoke_shim"]
