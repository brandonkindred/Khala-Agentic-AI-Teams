"""Domain service modules for agentic team provisioning HTTP handlers.

Routers in ``api.routes`` stay thin; business logic for extracted endpoint
groups lives here. Services import ``api.main`` only inside functions to avoid
import cycles and to honor the hub monkeypatch surface.
"""
