"""APIRouter modules for the SE team API (grouped by concern).

Each module in this package defines a module-local ``APIRouter`` that ``main``
mounts with ``app.include_router`` — absolute route paths are unchanged. The
monkeypatched collaborators (the ``background`` runners, ``SUPERVISOR_LOG_DIR``)
are dereferenced through the ``main`` module object at call time so
``monkeypatch.setattr(main, …)`` still takes effect after the api/main.py split.
"""
