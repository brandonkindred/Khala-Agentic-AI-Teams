"""Attribution wiring on the Ollama client: telemetry sourcing + the
shared-singleton concurrency guarantee that motivated the contextvar design."""

import threading

import pytest

import llm_service.clients.ollama as ollama_mod
from llm_service.attribution import bind_request_id, llm_attribution
from llm_service.clients.ollama import OllamaLLMClient, _caller_team


def test_empty_objective_is_rejected() -> None:
    """DbC: the generation entrypoints reject an empty/whitespace objective up
    front (before any network call), so attribution is never silently degraded."""
    client = OllamaLLMClient(model="m")
    with pytest.raises(ValueError, match="objective"):
        client.complete_json("p", objective="")
    with pytest.raises(ValueError, match="objective"):
        client.complete("p", objective="   ")
    with pytest.raises(ValueError, match="objective"):
        client.chat([{"role": "user", "content": "x"}], objective="")


def _call_from(path: str):
    """Invoke _caller_team as if from a frame whose source file is ``path``."""

    def probe():
        return _caller_team()

    ns: dict = {}
    exec(compile("def f(probe):\n    return probe()\n", path, "exec"), ns)
    return ns["f"](probe)


def test_caller_team_derives_team_from_source_path() -> None:
    # The team directory under agents/ is returned regardless of import name.
    assert _call_from("/work/backend/agents/blogging/blog_writer_agent/agent.py") == "blogging"
    assert (
        _call_from("/app/agents/software_engineering_team/tech_lead_agent/agent.py")
        == "software_engineering_team"
    )


def test_caller_team_skips_llm_service_and_non_team_frames() -> None:
    # llm_service is not a team; frames outside agents/ yield no team.
    assert _call_from("/x/agents/llm_service/factory.py") == ""
    assert _call_from("/some/site-packages/strands/agent.py") == ""


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
    # Per-call response state lives in contextvars (not instance attributes), so a
    # concurrent call on the shared singleton can't corrupt this record.
    ollama_mod._usage_var.set({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    ollama_mod._latency_var.set(123)
    ollama_mod._caller_var.set("ranker.agent.rank")

    with (
        llm_attribution(team="job_matching", agent_key="ranker", objective="rank candidates"),
        bind_request_id("rid-42"),
    ):
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

    def worker(agent_key: str, objective: str, tokens: int) -> None:
        with (
            llm_attribution(team="team", agent_key=agent_key, objective=objective),
            bind_request_id(f"rid-{agent_key}"),
        ):
            # Per-call response state (usage/latency/caller) must also stay
            # thread-local — the bug the contextvars fix the shared singleton had.
            ollama_mod._usage_var.set({"total_tokens": tokens})
            ollama_mod._caller_var.set(f"{agent_key}.run")
            barrier.wait()  # maximize interleaving while contexts are active
            with lock:
                client._record_telemetry(status="success")

    t1 = threading.Thread(target=worker, args=("agent_a", "obj_a", 11))
    t2 = threading.Thread(target=worker, args=("agent_b", "obj_b", 22))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert records["agent_a"]["objective"] == "obj_a"
    assert records["agent_a"]["request_id"] == "rid-agent_a"
    assert records["agent_a"]["total_tokens"] == 11
    assert records["agent_a"]["caller_tag"] == "agent_a.run"
    assert records["agent_b"]["objective"] == "obj_b"
    assert records["agent_b"]["request_id"] == "rid-agent_b"
    assert records["agent_b"]["total_tokens"] == 22
    assert records["agent_b"]["caller_tag"] == "agent_b.run"
