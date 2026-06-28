"""Unit tests for the coding-team human-in-the-loop (HITL) gate helpers and job-store pause ops."""

from __future__ import annotations

from typing import Any, Dict, List

import httpx

from coding_team import hitl, job_store

# --------------------------------------------------------------------------- convert / normalize


def test_convert_strings_get_empty_options_and_unique_ids():
    out = hitl.convert_to_structured_questions(
        ["Allergen strictness default?", "  "], source="plan"
    )
    assert len(out) == 1  # blank dropped
    q = out[0]
    assert q["question_text"] == "Allergen strictness default?"
    assert q["source"] == "plan"
    assert q["required"] is True
    # Plain strings carry no options; UI falls back to free-text "other" field.
    assert q["options"] == []
    assert q["id"].startswith("plan_0_")


def test_convert_dict_preserves_id_and_options():
    out = hitl.convert_to_structured_questions(
        [
            {
                "id": "q1",
                "question_text": "Pick one",
                "context": "why",
                "options": [{"id": "a", "label": "A"}],
            }
        ],
        source="tech_lead",
    )
    assert out[0]["id"] == "q1"
    assert out[0]["context"] == "why"
    assert out[0]["options"] == [{"id": "a", "label": "A", "is_default": False}]


def test_normalize_options_drops_reserved_other_id():
    out = hitl.convert_to_structured_questions(
        [{"question_text": "Q?", "options": [{"id": "other", "label": "Another team"}]}]
    )
    # "other" is reserved for free-text; the option is dropped entirely rather than renamed
    # to a fixed string that could collide with a legitimate "other_opt" option.
    assert out[0]["options"] == []


def test_convert_to_structured_questions_filters_generic_options():
    # Generic yes/no/not-sure/other options are removed from both normalization paths.
    # convert_to_structured_questions applies _filter_generic_options (no minimum count),
    # so a single surviving domain-specific option is kept rather than forced to free-text.
    out = hitl.convert_to_structured_questions(
        [
            {
                "question_text": "Which approach?",
                "options": [
                    {"id": "yes", "label": "Yes"},
                    {"id": "no", "label": "No"},
                    {"id": "rest", "label": "REST API"},
                ],
            }
        ]
    )
    ids = [o["id"] for o in out[0]["options"]]
    assert ids == ["rest"]


def test_normalize_options_deduplicates_within_convert_to_structured_questions():
    # Dedup in _normalize_options applies to BOTH conversion paths, not just
    # normalize_open_questions. Two inputs whose IDs normalize to the same string must collapse.
    out = hitl.convert_to_structured_questions(
        [{"question_text": "Q?", "options": [{"id": "a", "label": "A"}, {"id": " a ", "label": "A-dup"}]}]
    )
    assert len(out[0]["options"]) == 1
    assert out[0]["options"][0]["id"] == "a"


def test_normalize_options_deduplicates_case_insensitive():
    # IDs differing only by case are treated as the same ID (consistent with
    # the case-insensitive comparison in answers_to_resolved).
    out = hitl.convert_to_structured_questions(
        [{"question_text": "Q?", "options": [{"id": "Cloud", "label": "Cloud"}, {"id": "cloud", "label": "Cloud 2"}]}]
    )
    assert len(out[0]["options"]) == 1
    assert out[0]["options"][0]["id"] == "Cloud"  # first occurrence kept


def test_normalize_open_questions_drops_generic_other_label():
    # A non-compliant LLM may emit {id:"choice_other", label:"Other"} — the label filter
    # must catch it even though the id is not the reserved "other" string.
    out = hitl.normalize_open_questions(
        [
            {
                "question_text": "Q?",
                "options": [
                    {"id": "choice_other", "label": "Other"},
                    {"id": "cloud", "label": "Cloud deployment"},
                    {"id": "on_prem", "label": "On-premise deployment"},
                ],
            }
        ]
    )
    ids = [o["id"] for o in out[0]["options"]]
    assert ids == ["cloud", "on_prem"]


def test_normalize_options_strips_whitespace_from_id_and_label():
    # IDs and labels with surrounding whitespace are stripped before storage and comparison,
    # so "other " is treated as the reserved ID and dropped, and " Yes " is matched against
    # the generic-label set and culled.
    out = hitl.normalize_open_questions(
        [
            {
                "question_text": "Q?",
                "options": [
                    {"id": "other ", "label": "Another team"},   # trailing space → reserved, dropped
                    {"id": "opt_a", "label": " Yes "},            # padded generic label → dropped
                    {"id": "opt_b", "label": "Deploy to cloud"},  # clean label → kept
                    {"id": "opt_c", "label": "Deploy on-premise"},  # clean label → kept
                ],
            }
        ]
    )
    ids = [o["id"] for o in out[0]["options"]]
    assert ids == ["opt_b", "opt_c"]


def test_normalize_open_questions_missing_options_key():
    # A dict question that omits the options key entirely is treated as optionless.
    out = hitl.normalize_open_questions([{"question_text": "Q?"}])
    assert out[0]["options"] == []


def test_normalize_open_questions_drops_options_fewer_than_two():
    out = hitl.normalize_open_questions(
        [{"question_text": "Q?", "options": [{"id": "a", "label": "A"}]}]
    )
    # Single option after normalization → discard and fall back to empty options list.
    assert out[0]["options"] == []


def test_normalize_open_questions_drops_duplicate_option_ids():
    # Two entries with the same ID → _normalize_options deduplicates to one → fewer than 2 → free-text.
    out = hitl.normalize_open_questions(
        [{"question_text": "Q?", "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "A2"}]}]
    )
    assert out[0]["options"] == []


def test_normalize_open_questions_deduplicates_partial_duplicates():
    # [a, a, b] deduplicates to [a, b] → 2 unique IDs → accepted, not rejected.
    out = hitl.normalize_open_questions(
        [
            {
                "question_text": "Q?",
                "options": [
                    {"id": "a", "label": "A"},
                    {"id": "a", "label": "A-dup"},
                    {"id": "b", "label": "B"},
                ],
            }
        ]
    )
    assert [o["id"] for o in out[0]["options"]] == ["a", "b"]


def test_normalize_open_questions_drops_generic_yes_no_options():
    # The former yes/no/not_sure fallback IDs are rejected as non-context-specific.
    out = hitl.normalize_open_questions(
        [
            {
                "question_text": "Q?",
                "options": [
                    {"id": "yes", "label": "Yes"},
                    {"id": "no", "label": "No"},
                    {"id": "not_sure", "label": "Not sure"},
                ],
            }
        ]
    )
    assert out[0]["options"] == []


def test_normalize_open_questions_drops_generic_mixed_with_specific():
    # A mix of generic yes/no IDs with one context-specific option is still collapsed: the generic
    # options are culled individually, leaving only the context-specific one — fewer than 2 — so
    # the question falls back to free-text.
    out = hitl.normalize_open_questions(
        [
            {
                "question_text": "Q?",
                "options": [
                    {"id": "yes", "label": "Yes"},
                    {"id": "no", "label": "No"},
                    {"id": "deployment_target", "label": "On-premise"},
                ],
            }
        ]
    )
    assert out[0]["options"] == []


def test_normalize_open_questions_drops_variant_ids_by_label():
    # IDs like opt_yes/opt_no that are not in _GENERIC_OPTION_IDS but have a generic label
    # should be culled by label match, leaving fewer than 2 options → free-text fallback.
    out = hitl.normalize_open_questions(
        [
            {
                "question_text": "Q?",
                "options": [
                    {"id": "opt_yes", "label": "Yes"},
                    {"id": "opt_no", "label": "No"},
                ],
            }
        ]
    )
    assert out[0]["options"] == []


def test_normalize_open_questions_mixed_keeps_specific_when_two_or_more():
    # Generic options are removed individually; if ≥ 2 context-specific options remain, they
    # are accepted. The generic ones should not appear in the final list.
    out = hitl.normalize_open_questions(
        [
            {
                "question_text": "Q?",
                "options": [
                    {"id": "yes", "label": "Yes"},
                    {"id": "cloud", "label": "Cloud deployment"},
                    {"id": "on_prem", "label": "On-premise deployment"},
                ],
            }
        ]
    )
    assert [o["id"] for o in out[0]["options"]] == ["cloud", "on_prem"]


def test_normalize_open_questions_keeps_two_or_more_options():
    out = hitl.normalize_open_questions(
        [{"question_text": "Q?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}]
    )
    assert len(out[0]["options"]) == 2


def test_convert_dict_alt_text_keys_and_empty_dropped():
    out = hitl.convert_to_structured_questions(
        [{"text": "via text"}, {"question": "via question"}, {"foo": "x"}]
    )
    texts = [q["question_text"] for q in out]
    assert texts == ["via text", "via question"]


def test_normalize_open_questions_strings_and_dicts():
    out = hitl.normalize_open_questions(
        [
            "plain?",
            {
                "question_text": "rich?",
                "context": "ctx",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            },
            {"x": 1},
            "",
        ]
    )
    assert out[0] == {"question_text": "plain?", "options": []}
    assert out[1]["question_text"] == "rich?"
    assert out[1]["context"] == "ctx"
    assert out[1]["options"][0]["id"] == "a"


def test_normalize_open_questions_non_list():
    assert hitl.normalize_open_questions(None) == []
    assert hitl.normalize_open_questions("not a list") == []


# --------------------------------------------------------------------------- coverage / unanswered


def test_unanswered_empty_when_no_open_questions():
    assert hitl.unanswered_questions([], [{"question_text": "x"}]) == []


def test_unanswered_all_when_no_resolved():
    assert hitl.unanswered_questions(["a", "b"], None) == ["a", "b"]


def test_unanswered_covered_by_text():
    open_qs = ["Strictness?", "Policy?"]
    resolved = [
        {"question_text": "strictness?", "answer": "strict"},
        {"question_text": "policy?", "answer": "no"},
    ]
    assert hitl.unanswered_questions(open_qs, resolved) == []


def test_unanswered_partial_text_match_fails_closed():
    open_qs = ["Strictness?", "Policy?"]
    resolved = [{"question_text": "strictness?", "answer": "strict"}]
    assert hitl.unanswered_questions(open_qs, resolved) == ["Policy?"]


def test_unanswered_textless_answers_fail_closed():
    # Text-less answers (no question_text) cannot be matched to a question, so they NEVER cover one
    # by raw count — proceeding would risk applying answers to the wrong questions. Fail closed.
    open_qs = ["a", "b"]
    resolved = [{"selected_option_id": "yes"}, {"selected_option_id": "no"}]  # no question_text
    assert hitl.unanswered_questions(open_qs, resolved) == ["a", "b"]


def test_unanswered_textless_answers_insufficient_also_fail_closed():
    open_qs = ["a", "b", "c"]
    resolved = [{"selected_option_id": "yes"}]
    assert hitl.unanswered_questions(open_qs, resolved) == ["a", "b", "c"]


def test_unanswered_handles_dict_open_questions():
    open_qs = [{"question_text": "Strictness?"}]
    resolved = [{"question_text": "strictness?", "answer": "strict"}]
    assert hitl.unanswered_questions(open_qs, resolved) == []


# --------------------------------------------------------------------------- answers_to_resolved


def test_answers_to_resolved_maps_label_and_filters_unknown():
    pending = [{"id": "q1", "question_text": "Pick", "options": [{"id": "a", "label": "Option A"}]}]
    submitted = [
        {"question_id": "q1", "selected_option_id": "a"},
        {
            "question_id": "other_batch",
            "selected_option_id": "z",
        },  # from a different pause; ignored
    ]
    out = hitl.answers_to_resolved(submitted, pending)
    assert len(out) == 1
    assert out[0]["question_id"] == "q1"
    assert out[0]["question_text"] == "Pick"
    assert out[0]["answer"] == "Option A"


def test_answers_to_resolved_other_text_wins():
    pending = [
        {"id": "q1", "question_text": "Pick", "options": [{"id": "other", "label": "Other"}]}
    ]
    submitted = [
        {"question_id": "q1", "selected_option_id": "other", "other_text": "custom answer"}
    ]
    out = hitl.answers_to_resolved(submitted, pending)
    assert out[0]["answer"] == "custom answer"


def test_answers_to_resolved_unknown_option_falls_back_to_id():
    pending = [{"id": "q1", "question_text": "Pick", "options": []}]
    submitted = [{"question_id": "q1", "selected_option_id": "yes"}]
    out = hitl.answers_to_resolved(submitted, pending)
    assert out[0]["answer"] == "yes"


def test_answers_to_resolved_skips_non_dicts():
    pending = [{"id": "q1", "question_text": "Pick"}]
    assert (
        hitl.answers_to_resolved(["not a dict", {"question_id": "q1"}], pending)[0]["question_id"]
        == "q1"
    )


def test_answers_to_resolved_strips_whitespace_from_selected_option_id():
    # selected_option_id with surrounding whitespace must still resolve to the correct label.
    pending = [{"id": "q1", "question_text": "Pick", "options": [{"id": "opt_a", "label": "Option A"}]}]
    submitted = [{"question_id": "q1", "selected_option_id": " opt_a "}]
    out = hitl.answers_to_resolved(submitted, pending)
    assert out[0]["answer"] == "Option A"


def test_answers_to_resolved_other_text_wins_for_capitalized_other():
    # selected_option_id="Other" (capital O) must still trigger the free-text fallback.
    pending = [{"id": "q1", "question_text": "Pick", "options": []}]
    submitted = [{"question_id": "q1", "selected_option_id": "Other", "other_text": "custom answer"}]
    out = hitl.answers_to_resolved(submitted, pending)
    assert out[0]["answer"] == "custom answer"


def test_answers_to_resolved_case_insensitive_option_id_match():
    # The auto-answer LLM may return an option ID with different casing than stored.
    # Case-insensitive comparison ensures the correct label is still resolved.
    pending = [
        {
            "id": "q1",
            "question_text": "Pick deployment",
            "options": [{"id": "cloud_deploy", "label": "Cloud deployment"}],
        }
    ]
    submitted = [{"question_id": "q1", "selected_option_id": "Cloud_Deploy"}]
    out = hitl.answers_to_resolved(submitted, pending)
    assert out[0]["answer"] == "Cloud deployment"


# --------------------------------------------------------------------------- terminal / wait


def test_is_terminal():
    assert hitl.is_terminal({"status": "failed"})
    assert hitl.is_terminal({"status": "cancelled"})
    assert hitl.is_terminal({"cancel_requested": True})
    assert hitl.is_terminal({"status": "completed"})
    assert hitl.is_terminal({"status": "completed_with_failures"})
    # already_complete is a terminal success — a finished already-complete job must not look
    # resumable to the /resume endpoint or the auto-resume guard.
    assert hitl.is_terminal({"status": "already_complete"})
    assert not hitl.is_terminal({"status": "running"})
    assert not hitl.is_terminal({})


def test_wait_for_answers_returns_true_when_flag_clears():
    job = {"waiting_for_answers": False}
    assert hitl.wait_for_answers("j", lambda jid: job, sleep=lambda s: None) is True


def test_wait_for_answers_returns_false_on_terminal():
    job = {"waiting_for_answers": True, "status": "cancelled"}
    assert hitl.wait_for_answers("j", lambda jid: job, sleep=lambda s: None) is False


def test_wait_for_answers_resumes_after_polls():
    state = {"polls": 0}

    def get_job(jid):
        state["polls"] += 1
        return {"waiting_for_answers": state["polls"] < 3}

    assert hitl.wait_for_answers("j", get_job, sleep=lambda s: None) is True
    assert state["polls"] >= 3


def test_wait_for_answers_heartbeats_each_poll():
    state = {"polls": 0}
    beats: list = []

    def get_job(jid):
        state["polls"] += 1
        return {"waiting_for_answers": state["polls"] < 3}

    assert hitl.wait_for_answers("j", get_job, sleep=lambda s: None, heartbeat_fn=beats.append)
    assert len(beats) == 2  # one per waiting poll; none once the flag clears
    for ts in beats:
        assert "T" in ts  # ISO-8601 timestamps


def test_wait_for_answers_heartbeat_failure_does_not_break_wait():
    state = {"polls": 0}

    def get_job(jid):
        state["polls"] += 1
        return {"waiting_for_answers": state["polls"] < 3}

    def boom(ts):
        raise RuntimeError("job service down")

    assert hitl.wait_for_answers("j", get_job, sleep=lambda s: None, heartbeat_fn=boom) is True


def test_wait_for_answers_survives_transient_get_job_failure():
    """A transient job-service read failure (e.g. a connection reset that outlived
    the client's retry budget) must not kill the wait — the loop logs, backs off,
    and re-reads, eventually returning once the flag clears."""
    state = {"polls": 0}

    def get_job(jid):
        state["polls"] += 1
        if state["polls"] in (2, 3):
            raise httpx.ReadError("[Errno 104] Connection reset by peer")
        return {"waiting_for_answers": state["polls"] < 5}

    assert hitl.wait_for_answers("j", get_job, sleep=lambda s: None) is True
    assert state["polls"] >= 5  # the two failing reads did not abort the loop


def test_wait_for_answers_sustained_failure_times_out():
    """If the read keeps failing, the loop must actually enter, hit the except path
    each iteration, and end via the timeout bound (returns False) instead of
    propagating the exception. The clock advances slowly enough (1s/check vs a 5s
    timeout) that the loop body runs several times and ``get_job`` is exercised —
    otherwise the test would be a false positive (loop never entered)."""
    clock = {"t": 0.0}
    calls = {"get_job": 0}

    def now():
        t = clock["t"]
        clock["t"] += 1.0
        return t

    def get_job(jid):
        calls["get_job"] += 1
        raise httpx.ReadError("[Errno 104] Connection reset by peer")

    out = hitl.wait_for_answers("j", get_job, timeout_s=5.0, sleep=lambda s: None, now=now)
    assert out is False
    assert calls["get_job"] >= 3  # the loop entered and exercised the except path repeatedly


def test_wait_for_answers_renews_heartbeat_on_transient_read_failure():
    """While reads keep failing, the loop must keep the liveness heartbeat fresh so a
    second worker doesn't treat this still-alive loop as dead (stale
    ``answer_wait_heartbeat_at``) and auto-resume the job elsewhere, double-driving it."""
    state = {"polls": 0}
    beats: list = []

    def get_job(jid):
        state["polls"] += 1
        if state["polls"] <= 3:
            raise httpx.ReadError("[Errno 104] Connection reset by peer")
        return {"waiting_for_answers": False}  # outage clears -> flag down -> loop returns

    assert hitl.wait_for_answers("j", get_job, sleep=lambda s: None, heartbeat_fn=beats.append) is True
    assert len(beats) == 3  # one heartbeat per failed read; the 4th poll cleared and returned
    for ts in beats:
        assert "T" in ts  # ISO-8601 timestamps


def test_wait_for_answers_times_out():
    clock = {"t": 0.0}

    def now():
        clock["t"] += 10.0
        return clock["t"]

    out = hitl.wait_for_answers(
        "j", lambda jid: {"waiting_for_answers": True}, timeout_s=5.0, sleep=lambda s: None, now=now
    )
    assert out is False


def test_answer_wait_timeout_env(monkeypatch):
    monkeypatch.setenv("CODING_TEAM_ANSWER_WAIT_TIMEOUT_S", "120")
    assert hitl.answer_wait_timeout_s() == 120.0
    monkeypatch.setenv("CODING_TEAM_ANSWER_WAIT_TIMEOUT_S", "0")
    assert hitl.answer_wait_timeout_s() == 3600.0
    monkeypatch.setenv("CODING_TEAM_ANSWER_WAIT_TIMEOUT_S", "garbage")
    assert hitl.answer_wait_timeout_s() == 3600.0
    monkeypatch.delenv("CODING_TEAM_ANSWER_WAIT_TIMEOUT_S", raising=False)
    assert hitl.answer_wait_timeout_s() == 3600.0


# --------------------------------------------------------------------------- job_store pause ops


class _FakeClient:
    """Captures atomic_update calls and serves a mutable job dict."""

    def __init__(self, job: Dict[str, Any]) -> None:
        self._job = job
        self.calls: List[Dict[str, Any]] = []

    def atomic_update(self, job_id, *, merge_fields=None, append_to=None, **_):
        self.calls.append({"merge_fields": merge_fields, "append_to": append_to})
        for k, v in (merge_fields or {}).items():
            self._job[k] = v
        for k, items in (append_to or {}).items():
            self._job.setdefault(k, [])
            self._job[k].extend(items)

    def get_job(self, job_id):
        return self._job


def _patch_client(monkeypatch, job):
    client = _FakeClient(job)
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: client)
    return client


def test_add_pending_questions_sets_waiting(monkeypatch):
    job: Dict[str, Any] = {}
    _patch_client(monkeypatch, job)
    job_store.add_pending_questions("j", [{"id": "q1", "question_text": "x"}])
    assert job["waiting_for_answers"] is True
    assert job["pending_questions"][0]["id"] == "q1"
    assert job_store.is_waiting_for_answers("j") is True


def test_submit_answers_clears_and_appends(monkeypatch):
    job: Dict[str, Any] = {"waiting_for_answers": True, "pending_questions": [{"id": "q1"}]}
    _patch_client(monkeypatch, job)
    job_store.submit_answers("j", [{"question_id": "q1", "selected_option_id": "yes"}])
    assert job["waiting_for_answers"] is False
    assert job["pending_questions"] == []
    assert job_store.get_submitted_answers("j") == [
        {"question_id": "q1", "selected_option_id": "yes"}
    ]


def test_is_waiting_and_get_answers_when_no_job(monkeypatch):
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _FakeClientNone())
    assert job_store.is_waiting_for_answers("missing") is False
    assert job_store.get_submitted_answers("missing") == []


class _FakeClientNone:
    def get_job(self, job_id):
        return None
