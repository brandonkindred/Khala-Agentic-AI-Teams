"""Attribution wiring on the Ollama client: telemetry sourcing + the
shared-singleton concurrency guarantee that motivated the contextvar design."""

import threading

import llm_service.clients.ollama as ollama_mod
from llm_service.attribution import bind_request_id, llm_attribution
from llm_service.clients.ollama import OllamaLLMClient


def _capture_records(monkeypatch) -> list:
    records: list = []
    monkeypatch.setattr(
        ollama_mod,
        "record_llm_call",
        lambda **kw: records.append(kw),
    )
    return records


def test_record_telemetry_sources_attribution(monkeypatch) -> None:
    records = _capture_records(monkeypatch)
    client = OllamaLLMClient(model="m")
    client._last_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    client._last_latency_ms = 123

    with llm_attribution(team="job_matching", agent_key="ranker", objective="rank candidates"), \
            bind_request_id("rid-42"):
        client._record_telemetry(status="success")

    assert len(records) == 1
    rec = records[0]
    assert rec["team"] == "job_matching"
    assert rec["agent_key"] == "ranker"
    assert rec["objective"] == "rank candidates"
    assert rec["request_id"] == "rid-42"
    assert rec["total_tokens"] == 15


def test_concurrent_calls_do_not_cross_attribute(monkeypatch) -> None:
    """Two threads sharing one cached client each record their own attribution.

    This is the property the old per-instance ``_current_*`` attributes could
    not guarantee; contextvars are per-thread so there is no cross-talk.
    """
    records: dict = {}
    lock = threading.Lock()
    monkeypatch.setattr(
        ollama_mod,
        "record_llm_call",
        lambda **kw: records.__setitem__(kw["agent_key"], kw),
    )
    # A single shared client instance, as the factory cache would hand out.
    client = OllamaLLMClient(model="m")
    barrier = threading.Barrier(2)

    def worker(agent_key: str, objective: str) -> None:
        with llm_attribution(team="team", agent_key=agent_key, objective=objective), \
                bind_request_id(f"rid-{agent_key}"):
            barrier.wait()  # maximize interleaving while contexts are active
            with lock:
                client._record_telemetry(status="success")

    t1 = threading.Thread(target=worker, args=("agent_a", "obj_a"))
    t2 = threading.Thread(target=worker, args=("agent_b", "obj_b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert records["agent_a"]["objective"] == "obj_a"
    assert records["agent_a"]["request_id"] == "rid-agent_a"
    assert records["agent_b"]["objective"] == "obj_b"
    assert records["agent_b"]["request_id"] == "rid-agent_b"
