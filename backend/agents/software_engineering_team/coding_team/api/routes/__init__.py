"""APIRouter modules for the coding_team API (grouped by concern).

Each module in this package defines a module-local ``APIRouter`` that ``main``
mounts with ``app.include_router`` — absolute route paths are unchanged.
Collaborators are dereferenced through the ``main`` hub at call time so
``monkeypatch.setattr(main, …)`` still takes effect after the api/main.py split.
"""
