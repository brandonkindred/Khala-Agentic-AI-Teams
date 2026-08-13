"""Blogging-service entrypoint registers the LLM usage flusher before Temporal."""

from __future__ import annotations

from pathlib import Path

from blogging_service import entrypoint

_ENTRYPOINT = Path(__file__).resolve().parents[3] / "blogging_service" / "entrypoint.py"


def test_main_registers_usage_flusher_before_temporal_worker() -> None:
    """Activities can run as soon as the Temporal worker starts, which is before
    uvicorn lifespan (and create_team_app's flusher registration) fires."""
    src = _ENTRYPOINT.read_text(encoding="utf-8")
    main = src.split('if __name__ == "__main__":', 1)[1]
    assert main.index("_register_usage_flusher()") < main.index("_start_temporal_worker()")


def test_register_usage_flusher_invokes_register_and_atexit(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "llm_service.usage_flusher.register_usage_flusher",
        lambda: calls.append("register"),
    )
    monkeypatch.setattr(
        "llm_service.usage_flusher.shutdown",
        lambda: calls.append("shutdown"),
    )
    monkeypatch.setattr("atexit.register", lambda fn: calls.append(("atexit", fn)))
    entrypoint._register_usage_flusher()
    assert calls[0] == "register"
    assert calls[1][0] == "atexit"


def test_register_usage_flusher_swallows_failure(monkeypatch, caplog) -> None:
    def boom() -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr("llm_service.usage_flusher.register_usage_flusher", boom)
    with caplog.at_level("WARNING", logger=entrypoint.logger.name):
        entrypoint._register_usage_flusher()
    assert any("llm usage flusher registration failed" in r.getMessage() for r in caplog.records)
