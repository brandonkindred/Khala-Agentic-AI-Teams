"""
shared_agent_invoke — single-line-mount invoke shim for team FastAPI services.

Every team that wants Agent Console Runner support calls
``mount_invoke_shim(app, team_key="...")`` once in its ``api/main.py``. That
mounts ``POST /_agents/{agent_id}/invoke`` — an internal route used only by
the sandboxed unified_api proxy when it routes Runner invocations to this
service.

The shim reads manifests from the in-process
:mod:`agent_registry` singleton and dispatches to the agent's ``source.entrypoint``
(Python class, factory function, or plain callable) with the request body.
"""

from .shim import InvokeEnvelope, mount_invoke_shim

__all__ = ["InvokeEnvelope", "mount_invoke_shim"]
