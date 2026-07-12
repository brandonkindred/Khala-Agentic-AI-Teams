"""Unit tests for the pure reasoning helpers (``deepthought.reasoning``).

These functions are the single source of truth shared by the thread-mode runtime
and the deterministic Temporal workflow, so they are tested directly here.
"""

from __future__ import annotations

import itertools

from deepthought.models import AgentResult, AgentSpec, KnowledgeEntry, SkillRequirement
from deepthought.reasoning import (
    MAX_CHILDREN_PER_AGENT,
    build_child_specs,
    build_finding_entry,
    collect_specialists,
    compute_structural_confidence,
    find_similar_entries,
    format_answer,
    jaccard_similarity,
    normalise_words,
    parse_analysis,
    results_to_dicts,
)

# --------------------------------------------------------------------------- #
# Similarity
# --------------------------------------------------------------------------- #


def test_normalise_words_drops_short_words_and_punctuation():
    assert normalise_words("What is the Meaning?") == {"what", "the", "meaning"}


def test_jaccard_identical_is_one_and_disjoint_is_zero():
    assert jaccard_similarity("alpha beta gamma", "alpha beta gamma") == 1.0
    assert jaccard_similarity("alpha beta", "delta epsilon") == 0.0


def test_jaccard_empty_side_is_zero():
    assert jaccard_similarity("", "anything here") == 0.0
    assert jaccard_similarity("a b", "") == 0.0  # single-char words normalise away


def _entry(question: str, confidence: float = 0.5) -> KnowledgeEntry:
    return KnowledgeEntry(
        agent_id="a",
        agent_name="prior",
        focus_question=question,
        finding="finding text",
        confidence=confidence,
    )


def test_find_similar_entries_filters_by_threshold():
    entries = [
        _entry("how do neural networks learn representations"),
        _entry("what is the capital of france"),
    ]
    hits = find_similar_entries(entries, "how do neural networks learn features", threshold=0.4)
    assert len(hits) == 1
    assert hits[0].focus_question.startswith("how do neural networks")


def test_find_similar_entries_empty_when_below_threshold():
    entries = [_entry("completely unrelated subject matter")]
    assert find_similar_entries(entries, "quantum chromodynamics basics", threshold=0.7) == []


# --------------------------------------------------------------------------- #
# parse_analysis
# --------------------------------------------------------------------------- #


def test_parse_analysis_direct_answer():
    a = parse_analysis(
        {"summary": "s", "can_answer_directly": True, "direct_answer": "A", "confidence": 0.9},
        focus_question="q",
    )
    assert a.can_answer_directly is True
    assert a.direct_answer == "A"
    assert a.confidence == 0.9
    assert a.skill_requirements == []


def test_parse_analysis_decomposition_keeps_skills():
    data = {
        "can_answer_directly": False,
        "skill_requirements": [
            {"name": "n", "description": "d", "focus_question": "fq", "reasoning": "r"}
        ],
    }
    a = parse_analysis(data, focus_question="q")
    assert a.can_answer_directly is False
    assert a.confidence == 0.0
    assert len(a.skill_requirements) == 1


def test_parse_analysis_no_skills_forces_direct():
    a = parse_analysis({"can_answer_directly": False, "skill_requirements": []}, focus_question="q")
    assert a.can_answer_directly is True
    assert a.summary == "q"  # default falls back to the focus question


def test_parse_analysis_skips_malformed_skill_and_caps_at_five():
    skills = [
        {"name": f"n{i}", "description": "d", "focus_question": "fq", "reasoning": "r"}
        for i in range(7)
    ]
    skills.insert(0, {"name": "bad"})  # missing required fields -> skipped
    a = parse_analysis(
        {"can_answer_directly": False, "skill_requirements": skills}, focus_question="q"
    )
    # Only the first MAX_CHILDREN_PER_AGENT raw entries are considered; the bad
    # one is among them and dropped, so at most 5 (minus the malformed) survive.
    assert len(a.skill_requirements) <= MAX_CHILDREN_PER_AGENT
    assert all(s.name != "bad" for s in a.skill_requirements)


# --------------------------------------------------------------------------- #
# build_child_specs / results_to_dicts
# --------------------------------------------------------------------------- #


def _parent() -> AgentSpec:
    return AgentSpec(
        agent_id="parent-1", name="root", role_description="r", focus_question="pq", depth=2
    )


def test_build_child_specs_uses_injected_id_factory_and_increments_depth():
    counter = itertools.count()
    skills = [
        SkillRequirement(name="a", description="da", focus_question="qa", reasoning="ra"),
        SkillRequirement(name="b", description="db", focus_question="qb", reasoning="rb"),
    ]
    specs = build_child_specs(skills, _parent(), lambda: f"cid-{next(counter)}")
    assert [s.agent_id for s in specs] == ["cid-0", "cid-1"]
    assert all(s.depth == 3 for s in specs)
    assert all(s.parent_id == "parent-1" for s in specs)
    assert [s.name for s in specs] == ["a", "b"]


def test_build_child_specs_caps_at_max_children():
    skills = [
        SkillRequirement(name=f"n{i}", description="d", focus_question="q", reasoning="r")
        for i in range(MAX_CHILDREN_PER_AGENT + 3)
    ]
    specs = build_child_specs(skills, _parent(), lambda: "x")
    assert len(specs) == MAX_CHILDREN_PER_AGENT


def test_results_to_dicts_projects_compact_fields():
    r = AgentResult(
        agent_id="i", agent_name="n", depth=1, focus_question="q", answer="ans", confidence=0.6
    )
    assert results_to_dicts([r]) == [
        {"agent_name": "n", "focus_question": "q", "confidence": 0.6, "answer": "ans"}
    ]


# --------------------------------------------------------------------------- #
# compute_structural_confidence
# --------------------------------------------------------------------------- #


def test_confidence_direct_answer_blend():
    assert compute_structural_confidence(
        was_decomposed=False, self_assessed=1.0, child_results=[]
    ) == round(0.4 + 0.6 * 0.95, 3)


def test_confidence_decomposed_without_children_is_floor():
    assert (
        compute_structural_confidence(was_decomposed=True, self_assessed=0.0, child_results=[])
        == 0.3
    )


def _child(conf: float, reused: bool = False) -> AgentResult:
    return AgentResult(
        agent_id="i",
        agent_name="n",
        depth=1,
        focus_question="q",
        answer="a",
        confidence=conf,
        reused_from_cache=reused,
    )


def test_confidence_decomposed_with_children_and_penalties():
    base = compute_structural_confidence(
        was_decomposed=True, self_assessed=0.0, child_results=[_child(0.8), _child(0.8)]
    )
    contradicted = compute_structural_confidence(
        was_decomposed=True,
        self_assessed=0.0,
        child_results=[_child(0.8), _child(0.8)],
        deliberation_notes="they contradict each other; also contradict again",
    )
    reused = compute_structural_confidence(
        was_decomposed=True,
        self_assessed=0.0,
        child_results=[_child(0.8, reused=True), _child(0.8, reused=True)],
    )
    assert contradicted < base
    assert reused < base
    assert 0.1 <= base <= 0.95


# --------------------------------------------------------------------------- #
# findings + answer formatting
# --------------------------------------------------------------------------- #


def test_build_finding_entry_truncates_and_tags():
    spec = AgentSpec(
        agent_id="i",
        name="quantum_physics_expert",
        role_description="r",
        focus_question="q",
        depth=1,
    )
    entry = build_finding_entry(spec, "x" * 900, 0.7)
    assert len(entry.finding) == 500
    assert entry.tags == ["quantum", "physics", "expert"]
    assert entry.confidence == 0.7


def _tree() -> AgentResult:
    grandchild = AgentResult(
        agent_id="g", agent_name="deep", depth=2, focus_question="gq", answer="ga", confidence=0.5
    )
    child = AgentResult(
        agent_id="c",
        agent_name="mid",
        depth=1,
        focus_question="cq",
        answer="ca",
        confidence=0.6,
        child_results=[grandchild],
        was_decomposed=True,
    )
    return AgentResult(
        agent_id="r",
        agent_name="root",
        depth=0,
        focus_question="rq",
        answer="root answer",
        confidence=0.7,
        child_results=[child],
        was_decomposed=True,
    )


def test_collect_specialists_is_recursive():
    assert collect_specialists(_tree()) == [("mid", "cq"), ("deep", "gq")]


def test_format_answer_not_decomposed_returns_answer():
    r = AgentResult(
        agent_id="r", agent_name="root", depth=0, focus_question="q", answer="plain", confidence=0.5
    )
    assert format_answer(r) == "plain"


def test_format_answer_decomposed_appends_footer():
    out = format_answer(_tree())
    assert out.startswith("root answer")
    assert "Specialists consulted:" in out
    assert "- **mid**: cq" in out
    assert "- **deep**: gq" in out


def test_format_answer_decomposed_no_specialists_returns_answer():
    r = AgentResult(
        agent_id="r",
        agent_name="root",
        depth=0,
        focus_question="q",
        answer="a",
        confidence=0.5,
        was_decomposed=True,
        child_results=[],
    )
    assert format_answer(r) == "a"
