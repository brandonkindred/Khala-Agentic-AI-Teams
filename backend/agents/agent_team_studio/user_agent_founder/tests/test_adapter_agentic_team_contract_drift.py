"""Drift tripwire for the founder ↔ agentic-team contract boundary (ADR-007).

The behavioral suite (``test_adapter_agentic_team.py``) scripts fake HTTP
responses, so it cannot notice when the *real* provisioning side changes
shape. This file imports the real ``agentic_team_provisioning`` models, DTOs,
and app routes and asserts the exact surface ``AgenticTeamAdapter`` depends
on — it must FAIL LOUDLY when either side drifts:

* provisioning → founder: ``TestPipelineRun`` fields, ``PipelineRunStatus``
  members, request DTO shapes, the three test-pipeline routes, and the
  unified-API mount prefix;
* founder-internal: the ``TargetTeamAdapter`` Protocol signatures (the
  Protocol is ``runtime_checkable``, so ``isinstance`` checks attribute
  *presence* only — signature drift is caught here, nowhere else).

When a test here fails, update the adapter (``targets/agentic_team.py``),
this file, and the boundary section of
``system_design/adr/ADR-007-founder-agentic-team-adapter-collapse.md``
together.

Invariants: every test is hermetic — no network, no Postgres (the one test
that imports the provisioning app installs the shared fake first).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from agent_team_studio.agentic_team_provisioning.models import (
    PipelineRunStatus,
    StartPipelineRunRequest,
    SubmitPipelineInputRequest,
)
from agent_team_studio.agentic_team_provisioning.models import (
    TestPipelineRun as PipelineRunModel,  # aliased so pytest does not try to collect it
)
from agent_team_studio.user_agent_founder.targets import agentic_team
from agent_team_studio.user_agent_founder.targets.agentic_team import AgenticTeamAdapter
from agent_team_studio.user_agent_founder.targets.base import TargetTeamAdapter

# ---------------------------------------------------------------------------
# Minimal HTTP stubs — intentionally NOT shared with the behavioral suite so a
# refactor there can never mask a drift failure here.
# ---------------------------------------------------------------------------


class _Resp:
    """Stub httpx response serving one scripted JSON payload.

    Preconditions: ``payload`` is the decoded-JSON value the adapter will
        consume. The tripwire only scripts 2xx responses, so no error-path
        attributes (``.text`` etc.) are modeled.
    Postconditions: ``json()`` returns the payload verbatim.
    """

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        assert self.status_code < 400  # tripwire tests only script 2xx


class _StubClient:
    """Records request URLs/bodies and serves the scripted payload for any URL.

    Postconditions: every ``post`` call is appended to ``self.posts`` as
        ``{"url", "json"}`` and every ``get`` URL to ``self.gets`` before the
        scripted response is returned.
    """

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []

    def get(self, url: str, *, timeout: Any = None) -> _Resp:
        self.gets.append(url)
        return _Resp(200, self._payload)

    def post(self, url: str, *, json: Any = None, timeout: Any = None) -> _Resp:
        self.posts.append({"url": url, "json": json})
        return _Resp(200, self._payload)


def _adapter(spec: str = "# SPEC BODY") -> AgenticTeamAdapter:
    # Seed the spec: start_build sources initial_input from the adapter's own
    # spec (the repo_path argument is the SE-only Protocol handoff, ignored here),
    # so a spec must be present for the create-run body to validate.
    return AgenticTeamAdapter("t1", process_id="p1", spec=spec)


# ---------------------------------------------------------------------------
# Enum coverage + round-trip through the REAL run model
# ---------------------------------------------------------------------------

# Founder-side status expected from poll_build for each REAL pipeline status.
# The values are the literals the orchestrator's shared _run_phase matches on,
# so they are deliberately hardcoded strings, not enum lookups.
EXPECTED_MAPPING: dict[PipelineRunStatus, str] = {
    PipelineRunStatus.RUNNING: "running",
    PipelineRunStatus.WAITING_FOR_INPUT: "waiting_for_input",
    PipelineRunStatus.COMPLETED: "completed",
    PipelineRunStatus.FAILED: "failed",
    PipelineRunStatus.CANCELLED: "cancelled",
}


def test_every_real_status_is_mapped():
    """Every PipelineRunStatus member has an agreed founder-side mapping.

    Only additions need checking here: *removing* a member already fails
    loudly at import time, because EXPECTED_MAPPING references members by
    attribute (an AttributeError errors out the whole module at collection).
    """
    unmapped = set(PipelineRunStatus) - set(EXPECTED_MAPPING)
    assert not unmapped, (
        f"PipelineRunStatus gained member(s) {sorted(m.value for m in unmapped)}. "
        "Update AgenticTeamAdapter.poll_build, EXPECTED_MAPPING here, and ADR-007's "
        "contract-boundary section together."
    )


def _run_for(member: PipelineRunStatus) -> PipelineRunModel:
    """Build a real TestPipelineRun in the given status, with the fields the
    adapter reads populated the way the pipeline runner populates them."""
    waiting = member is PipelineRunStatus.WAITING_FOR_INPUT
    return PipelineRunModel(
        run_id="r1",
        team_id="t1",
        process_id="p1",
        status=member,
        current_step_id="step-1" if waiting else None,
        human_prompt="Q?" if waiting else None,
        error="boom" if member is PipelineRunStatus.FAILED else None,
    )


@pytest.mark.parametrize("member", sorted(EXPECTED_MAPPING, key=lambda m: m.value))
def test_poll_build_round_trips_real_run_model(member: PipelineRunStatus):
    """poll_build maps a real TestPipelineRun payload onto the founder poll
    contract — catches enum-value and field renames on the provisioning side
    organically (the payload is the real model's JSON serialization)."""
    payload = _run_for(member).model_dump(mode="json")
    result = _adapter().poll_build(_StubClient(payload), "r1")

    assert result["status"] == EXPECTED_MAPPING[member], (
        f"poll_build mapped real status {member.value!r} to {result['status']!r}; "
        f"the founder orchestrator expects {EXPECTED_MAPPING[member]!r}."
    )
    if member is PipelineRunStatus.WAITING_FOR_INPUT:
        assert result["waiting_for_answers"] is True
        questions = result["pending_questions"]
        assert len(questions) == 1, "a WAIT step must surface exactly one question"
        question = questions[0]
        assert question["id"] == "r1:step-1"
        assert question["question_text"] == "Q?"
        assert question["options"] == [], "empty options force the persona's free-text answer"
    if member is PipelineRunStatus.FAILED:
        assert result["error"] == "boom"
    if member is PipelineRunStatus.CANCELLED:
        # Pins the adapter's canonical spelling to the real enum value — if the
        # provisioning side ever flips to "canceled", this fails alongside the
        # adapter's normalization instead of drifting silently.
        assert result["status"] == PipelineRunStatus.CANCELLED.value


@pytest.mark.parametrize("prompt", [None, ""])
def test_waiting_without_prompt_polls_as_running(prompt: str | None):
    """A real waiting run with no human_prompt yet is reported as still
    running, so the orchestrator re-polls instead of surfacing an empty question."""
    run = _run_for(PipelineRunStatus.WAITING_FOR_INPUT)
    payload = run.model_dump(mode="json")
    payload["human_prompt"] = prompt
    result = _adapter().poll_build(_StubClient(payload), "r1")
    assert result == {"status": "running"}


@pytest.mark.parametrize("step_id", [None, ""])
def test_waiting_step_id_falls_back_to_wait(step_id: str | None):
    """A missing/empty current_step_id on a real waiting run falls back to the
    'wait' question-id component so distinct WAIT steps cannot collide on ''."""
    run = _run_for(PipelineRunStatus.WAITING_FOR_INPUT)
    payload = run.model_dump(mode="json")
    payload["current_step_id"] = step_id
    result = _adapter().poll_build(_StubClient(payload), "r1")
    assert result["pending_questions"][0]["id"] == "r1:wait"


def test_poll_fields_exist_on_real_model():
    """Cheap explicit tripwire: the fields the adapter reads from poll/create
    responses must exist on the real TestPipelineRun model."""
    needed = {"run_id", "status", "current_step_id", "human_prompt", "error"}
    missing = needed - set(PipelineRunModel.model_fields)
    assert not missing, (
        f"TestPipelineRun lost field(s) {sorted(missing)} that AgenticTeamAdapter reads. "
        "Update targets/agentic_team.py and ADR-007's contract-boundary section together."
    )


# ---------------------------------------------------------------------------
# Request bodies validate against the REAL provisioning DTOs
# ---------------------------------------------------------------------------


def test_start_build_body_validates_against_real_request_model():
    """The create-run body the adapter POSTs is exactly what
    StartPipelineRunRequest accepts — a rename or new constraint fails here."""
    client = _StubClient({"run_id": "r1"})
    # The spec seeded on the adapter (not the ignored repo_path arg) is what the
    # adapter POSTs as initial_input.
    run_id = _adapter().start_build(client, "ignored-repo-path")
    assert run_id == "r1"

    body = client.posts[0]["json"]
    parsed = StartPipelineRunRequest.model_validate(body)
    assert parsed.process_id == "p1"
    assert parsed.initial_input == "# SPEC BODY"
    # Pydantic ignores unknown keys by default, so model_validate alone would
    # let a server-side rename orphan a key the adapter still sends. The
    # subset check makes that drift loud.
    orphaned = set(body) - set(StartPipelineRunRequest.model_fields)
    assert not orphaned, (
        f"AgenticTeamAdapter.start_build sends key(s) {sorted(orphaned)} that "
        "StartPipelineRunRequest no longer declares."
    )


@pytest.mark.parametrize(
    ("answers", "expected_text"),
    [
        ([{"selected_option_id": "other", "other_text": "Ship the MVP"}], "Ship the MVP"),
        # A one-character answer is the boundary case that actually pins the
        # DTO's min_length=1 — longer bodies would pass a silently-raised limit.
        ([{"selected_option_id": "other", "other_text": "y"}], "y"),
        (
            [{"selected_option_id": "other", "other_text": "   "}],
            agentic_team._NO_ANSWER_PLACEHOLDER,
        ),
        ([], agentic_team._NO_ANSWER_PLACEHOLDER),
    ],
)
def test_submit_answers_body_validates_against_real_input_model(
    answers: list[dict[str, Any]], expected_text: str
):
    """Every /input body the adapter can produce — including the blank-answer
    placeholder — satisfies SubmitPipelineInputRequest (pins min_length=1)."""
    client = _StubClient({})
    _adapter().submit_build_answers(client, "r1", answers)

    body = client.posts[0]["json"]
    parsed = SubmitPipelineInputRequest.model_validate(body)
    assert parsed.input == expected_text
    orphaned = set(body) - set(SubmitPipelineInputRequest.model_fields)
    assert not orphaned, (
        f"AgenticTeamAdapter.submit_build_answers sends key(s) {sorted(orphaned)} that "
        "SubmitPipelineInputRequest no longer declares."
    )


# ---------------------------------------------------------------------------
# Routes + mount prefix pinned against the REAL provisioning app
# ---------------------------------------------------------------------------


@pytest.fixture
def provisioning_app(monkeypatch):
    """Import the real provisioning FastAPI app with Postgres faked out.

    Uses the provisioning team's shared fake (the
    ``test_agent_manifests_endpoint.py`` pattern). Route inspection never runs
    the app lifespan, so schema registration is never attempted. The fake only
    matters on the *first* import of ``api.main`` in a session; an earlier
    unpatched import hits a try/except-guarded startup query — harmless.
    """
    from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import (
        install_fake_postgres,
    )

    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return app


def _leaf_route_objects(route):
    """Yield the leaf route-like object(s) ``route`` resolves to.

    FastAPI 0.137+ wraps every ``include_router(...)`` target in a private
    ``fastapi.routing._IncludedRouter`` that has no ``.path``/``.methods`` of
    its own, so reading those attributes directly off an ``app.routes`` entry
    silently yields nothing for a mounted sub-router. Recurses through
    ``effective_route_contexts()`` — FastAPI's own mechanism for resolving a
    route's final absolute path/methods/response_model through arbitrarily
    many levels of nested ``include_router(...)`` calls, used internally for
    OpenAPI generation — to reach the real leaf routes. Falls back to the
    route itself for route types ``effective_route_contexts`` never touches
    (a plain, unwrapped route), which already carries ``.path``/``.methods``
    directly.
    """
    effective_route_contexts = getattr(route, "effective_route_contexts", None)
    if effective_route_contexts is not None:
        yield from effective_route_contexts()
    else:
        yield route


def _pipeline_routes(app):
    """Return the (create, poll, submit) route objects the adapter calls.

    Postconditions: all three routes exist on the app — a moved or renamed
        route fails here with instructions, not as an AttributeError later.
    """
    routes = {
        (method, leaf.path): leaf
        for route in app.routes
        for leaf in _leaf_route_objects(route)
        for method in (getattr(leaf, "methods", None) or ())
    }
    create = routes.get(("POST", "/teams/{team_id}/test-pipeline/runs"))
    poll = routes.get(("GET", "/teams/{team_id}/test-pipeline/runs/{run_id}"))
    submit = routes.get(("POST", "/teams/{team_id}/test-pipeline/runs/{run_id}/input"))
    assert create and poll and submit, (
        "A test-pipeline route AgenticTeamAdapter depends on moved or disappeared "
        "from the provisioning app. Update targets/agentic_team.py._url and ADR-007."
    )
    return create, poll, submit


def test_real_routes_serialize_through_run_model(provisioning_app):
    """The create/poll routes serialize through the real run model — the
    round-trip tests above are only meaningful while this holds."""
    create, poll, _submit = _pipeline_routes(provisioning_app)
    assert create.response_model is PipelineRunModel
    assert poll.response_model is PipelineRunModel


def test_mount_prefix_matches_adapter():
    """The unified-API mount prefix matches the adapter's hardcoded prefix."""
    from unified_api.config import TEAM_CONFIGS

    assert TEAM_CONFIGS["agentic_team_provisioning"].prefix == agentic_team.PROVISIONING_PREFIX, (
        "The unified-API mount prefix for agentic_team_provisioning no longer matches "
        "AgenticTeamAdapter.PROVISIONING_PREFIX — adapter URLs would 404."
    )


def test_adapter_renders_real_route_urls(provisioning_app):
    """The adapter renders *every* URL it calls as exactly base + prefix + the
    real route path — driven through the real adapter methods, so a drifted
    sub-path or a doubled/displaced prefix (which substring or endswith checks
    cannot see) fails here."""
    create, poll, submit = _pipeline_routes(provisioning_app)

    def rendered(route) -> str:
        path = route.path.replace("{team_id}", "t1").replace("{run_id}", "r1")
        return agentic_team.PROVISIONING_PREFIX + path

    client = _StubClient({"run_id": "r1", "status": "completed"})
    adapter = _adapter()
    adapter.start_build(client, "# SPEC")
    adapter.poll_build(client, "r1")
    adapter.submit_build_answers(client, "r1", [{"other_text": "ok"}])
    base = agentic_team.UNIFIED_API_BASE.rstrip("/")
    create_url, submit_url = (p["url"] for p in client.posts)
    assert create_url == base + rendered(create)
    assert client.gets[0] == base + rendered(poll)
    assert submit_url == base + rendered(submit)


# ---------------------------------------------------------------------------
# Founder-side: Protocol signature guard
# ---------------------------------------------------------------------------


def test_adapter_signatures_match_protocol():
    """AgenticTeamAdapter's method signatures match the TargetTeamAdapter
    Protocol exactly. isinstance on a runtime_checkable Protocol (covered by
    the behavioral suite) only checks attribute presence, so parameter or
    signature drift on either side is caught here and nowhere else.

    Method names are derived from the Protocol's full MRO, so a method added
    there — directly or on a base Protocol it inherits — is automatically
    checked against the adapter."""
    protocol_classes = [
        # Walk the MRO so members inherited from a base Protocol are guarded
        # too; skip typing machinery (Protocol, Generic) and object, which
        # contribute no contract members.
        klass
        for klass in TargetTeamAdapter.__mro__
        if klass.__module__ not in ("typing", "builtins")
    ]
    public_members = {
        name: member
        for klass in protocol_classes
        for name, member in vars(klass).items()
        if not name.startswith("_")
    }
    # This guard can only compare plain functions. A member declared another
    # way (property/classmethod/staticmethod) would silently drop out of the
    # signature check below, so its mere existence fails here first.
    unguardable = {
        name for name, member in public_members.items() if not inspect.isfunction(member)
    }
    assert not unguardable, (
        f"Protocol member(s) {sorted(unguardable)} are not plain methods — extend this "
        "guard to compare their shape, or they drift unchecked."
    )
    protocol_methods = sorted(public_members)
    assert protocol_methods, "TargetTeamAdapter declares no public methods — did the Protocol move?"

    for name in protocol_methods:
        impl = getattr(AgenticTeamAdapter, name, None)
        assert impl is not None, f"AgenticTeamAdapter is missing Protocol method {name!r}"

        proto_sig = inspect.signature(getattr(TargetTeamAdapter, name))
        impl_sig = inspect.signature(impl)
        assert [(p.name, p.kind) for p in impl_sig.parameters.values()] == [
            (p.name, p.kind) for p in proto_sig.parameters.values()
        ], f"parameter drift on {name!r}: adapter {impl_sig} vs Protocol {proto_sig}"
        # Both modules use `from __future__ import annotations`, so annotations
        # are strings on both sides and compare textually.
        assert [p.annotation for p in impl_sig.parameters.values()] == [
            p.annotation for p in proto_sig.parameters.values()
        ], f"annotation drift on {name!r}: adapter {impl_sig} vs Protocol {proto_sig}"
        assert impl_sig.return_annotation == proto_sig.return_annotation, (
            f"return-annotation drift on {name!r}: adapter {impl_sig} vs Protocol {proto_sig}"
        )

    # Protocol data attributes: derived from the annotations so a new
    # attribute (or a changed annotation) is automatically checked, mirroring
    # the method loop above. Both current attributes are plain strings.
    assert set(TargetTeamAdapter.__annotations__) == {"team_key", "display_name"}, (
        "TargetTeamAdapter's data attributes changed — extend this guard (and every "
        "adapter) to cover the new shape."
    )
    adapter = _adapter()
    for attr, annotation in TargetTeamAdapter.__annotations__.items():
        # Annotations are strings here (`from __future__ import annotations`).
        assert annotation == "str", (
            f"Protocol attribute {attr!r} is now annotated {annotation!r}; update the "
            "adapter and this guard together."
        )
        assert isinstance(getattr(adapter, attr), str)
