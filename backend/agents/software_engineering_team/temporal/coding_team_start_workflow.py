"""Start the coding team Temporal workflow from synchronous API code.

Thin wrapper over ``shared.temporal.start_workflow_sync`` (the shared sync→async
bridge, which polls for the worker's Temporal client to become ready before
dispatching). We deliberately do NOT use ``shared.temporal.run_team_job`` here:
it creates its own job row and sets ``status=running`` itself, which would
collide with the API's ``create_job`` and the activity-owned status bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set, Tuple

from shared.temporal import execute_workflow_sync, start_workflow_sync
from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow

logger = logging.getLogger(__name__)

# Client-side wait window for ``execute_coding_team_workflow``. An
# address-comments run dispatches the FULL SE pipeline (plan, codegen, lint,
# review, QA, merge) for one review thread, so it is bounded by the same
# worst-case as an ordinary issue run rather than by a comment-sized edit. Four
# hours is that bound with headroom for LLM retries and queueing. It is only a
# WAIT window, never a kill switch: on expiry this caller reattaches to the
# still-running workflow (``reattach_on_timeout=True``), so a value that proves
# too small delays the caller's observation, it does not terminate the run.
_COMMENT_WORKFLOW_TIMEOUT_S = 4 * 60 * 60


# Substrings that mark a dict key as naming a credential. Broader than a bare
# "token" check as defense in depth: the durable Temporal event history is
# permanent, so a payload key spelling a credential any other common way
# (``authorization``, ``api_key``, ``secret``, ``password``, ``credential``)
# must be refused at dispatch too rather than relying on every caller
# remembering to spell it "token". Matched case-insensitively as a substring,
# so ``API-KEY``, ``x_api_key`` and ``github_token`` all hit. ``apikey`` is
# listed separately from ``api_key``/``api-key`` because substring matching is
# literal: an unseparated ``apikey``/``APIKey`` spelling contains none of the
# separated forms. ``private_key`` (a GitHub App signing key or an SSH PEM) is
# listed in all three separator spellings for that SAME literal-matching
# reason -- ``private-key`` and ``privateKey`` (lowered to ``privatekey``)
# contain none of the others -- and ``passphrase`` (which does NOT contain
# ``password``) is likewise a credential no other marker on this list would
# catch. None of the keys the real payloads
# carry (``github``: owner/repo/issue_number/issue_title/remote/base/
# integration_branch/expected_base_sha/expected_head_sha/pr_number/pr_url/
# publish_mode/cleanup_checkout_on_success; ``plan_input``: the fixed
# ``CodingTeamPlanInput`` fields) contains any of these markers, so broadening
# cannot false-positive on a legitimate dispatch.
_TOKEN_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "passphrase",
    "api_key",
    "api-key",
    "apikey",
    "private_key",
    "private-key",
    "privatekey",
    "authorization",
    "credential",
)


def _contains_token_key(value: Any, _seen: Optional[Set[int]] = None) -> bool:
    """True iff ``value`` (recursively) contains a dict with a credential-named key.

    A plain top-level ``"token" in github`` check only catches a token stored
    directly on the ``github`` dict — one nested under a sub-dict (e.g.
    ``github["auth"]["token"]``) would pass that check and get serialized into
    the Temporal workflow's durable event history, exactly the leakage the
    no-token contract exists to prevent.

    Preconditions:
        - ``_seen``, when passed, is a set of ``id()`` values of dicts/lists/
          tuples ALREADY VISITED ANYWHERE IN THE TRAVERSAL (not merely on the
          current recursion path); it is an internal recursion parameter, not
          for callers to populate.
    Postconditions:
        - Returns True iff any dict reachable from ``value`` (through nested
          dicts, lists, or tuples) has a key that case-insensitively contains
          any marker in :data:`_TOKEN_KEY_MARKERS` (e.g. ``"token"``,
          ``"github_token"``, ``"TOKEN"``, ``"authorization"``, ``"api_key"``,
          ``"client_secret"``, ``"password"``, ``"credentials"``).
        - Guards against unbounded recursion on a genuine reference cycle: a
          container already visited (identity-compared via ``id()``) is
          treated as containing no credential key and not traversed again.
          The visited set is accumulated IN PLACE across the whole traversal
          rather than copied per level, so a shared/diamond substructure is
          walked once instead of once per reference (O(total containers), not
          O(2**depth-of-sharing)). This is sound because the predicate is
          monotone: a container that contributed no credential key on its
          first visit cannot contribute one on a later visit, so skipping the
          repeat can never change the result. This does NOT bound recursion
          depth in general -- a pathologically deep but acyclic structure can
          still recurse as deep as the structure goes and, in principle,
          raise ``RecursionError``.
    """
    if isinstance(value, (dict, list, tuple)):
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        if isinstance(value, dict):
            return any(_is_token_key(k) or _contains_token_key(v, seen) for k, v in value.items())
        return any(_contains_token_key(v, seen) for v in value)
    return False


def _is_token_key(key: Any) -> bool:
    """True iff ``key``'s string form case-insensitively contains a credential marker.

    Postconditions:
        - Returns True iff ``str(key).lower()`` contains any substring in
          :data:`_TOKEN_KEY_MARKERS`. Never raises: a non-string key is
          stringified via ``str()`` first and matched on that form. An int key
          therefore can never match (every marker is alphabetic, and
          ``str(123)`` has no letters), but a TUPLE key matches whenever its
          repr-based string form contains a marker — ``("token", 0)``
          stringifies to ``"('token', 0)"``, which contains ``token``, so it IS
          a match. That errs toward detecting more, which is the safe direction
          for a guard whose job is to keep credentials out of a permanent
          Temporal event history.

    Limitations (deliberate, and the reason this is a defence-in-depth screen
    rather than the sole control): the match is on the KEY NAME only, so it
    catches neither a credential stored under an innocuous key (``{"header":
    "Bearer ghp_..."}``) nor a spelling absent from
    :data:`_TOKEN_KEY_MARKERS` (``pwd``, ``auth``, ``bearer``,
    ``access_key``). Those spellings are NOT added because the markers are
    matched as substrings against real dispatch payloads: ``auth`` alone would
    reject any legitimate ``author``/``authored_by`` key, and the list is kept
    to spellings that cannot collide with the fixed ``github``/
    ``CodingTeamPlanInput`` field names enumerated where it is defined. The
    real guarantee is structural -- activities resolve credentials
    activity-side from the job row or ``GITHUB_TOKEN`` and callers never put
    one on these dicts -- and this screen exists to make an accidental
    regression loud, not to sanitize hostile input.
    """
    lowered = str(key).lower()
    return any(marker in lowered for marker in _TOKEN_KEY_MARKERS)


def _reject_credential_named_key(candidate: Any, *, message_prefix: str) -> None:
    """Raise when ``candidate`` carries a CREDENTIAL-named key at any depth.

    The single implementation of the credential screen the two argument
    validators and the serialization-boundary re-check in
    :func:`_build_workflow_payload` all apply. Kept as one helper so the guard
    condition and the canonical marker phrasing (``"a credential-named key
    (any _TOKEN_KEY_MARKERS substring)"``) cannot drift between the three
    sites; only the message PREFIX differs per site, so each keeps the
    distinguishable error text its callers and tests already rely on.

    Preconditions:
        - ``message_prefix`` names the offending argument (and, where the site
          has one, the caller), and reads as a sentence continued by
          "a credential-named key ..." — it is interpolated verbatim.
    Postconditions:
        - Returns None for a falsy ``candidate`` (nothing to leak) or one whose
          keys contain no marker. Never mutates ``candidate``.
    Raises:
        ValueError: ``candidate`` is truthy and :func:`_contains_token_key`
            reports a marker key at any nesting depth.
    """
    if candidate and _contains_token_key(candidate):
        raise ValueError(
            f"{message_prefix} a credential-named key (any _TOKEN_KEY_MARKERS substring)"
        )


def _validate_common_args(job_id: str, repo_path: str, *, caller: str) -> None:
    """Shared job_id/repo_path presence validation for both dispatchers.

    Preconditions:
        - ``caller`` is the calling function's name, used verbatim in the
          raised message so callers keep distinguishable error text.
    Postconditions:
        - Returns None when both ``job_id`` and ``repo_path`` are non-empty.
    Raises:
        ValueError: ``job_id`` or ``repo_path`` is empty.
    """
    if not job_id:
        raise ValueError(f"{caller} requires a non-empty job_id")
    if not repo_path:
        raise ValueError(f"{caller} requires a non-empty repo_path")


def _validate_plan_input_arg(plan_input: Optional[Dict[str, Any]], *, caller: str) -> None:
    """Shared ``plan_input`` token-leak validation for both dispatchers.

    ``plan_input`` is serialized into the same durable Temporal workflow
    payload as ``github`` (see :func:`_build_workflow_payload`), so it needs
    the identical no-embedded-token guarantee that :func:`_validate_github_arg`
    already enforces for ``github`` — there is nothing about ``plan_input``'s
    provenance that makes it contractually token-free by construction.

    Preconditions:
        - ``caller`` is the calling function's name, used verbatim in the
          raised message.
    Postconditions:
        - Returns None when ``plan_input`` is ``None``, or is a dict containing
          no CREDENTIAL-named key (any of :data:`_TOKEN_KEY_MARKERS` — token,
          secret, password, passphrase, api_key/api-key/apikey,
          private_key/private-key/privatekey, authorization, credential) at
          any nesting depth.
    Raises:
        ValueError: ``plan_input`` is not ``None`` and not a ``dict`` (a
            non-dict, e.g. a token string, would otherwise bypass
            :func:`_contains_token_key`'s dict/list/tuple-only traversal and be
            serialized into the durable workflow payload verbatim — the same
            hole :func:`_validate_github_arg` already closes for ``github``);
            or ``plan_input`` contains a credential-named key at any nesting
            depth (see :func:`_contains_token_key`).
    """
    # ``is not None``, not truthiness: a falsy NON-dict (``[]``, ``""``, ``0``,
    # ``False``) is neither "absent" nor a plan, and a truthiness guard would
    # forward it verbatim across the durable workflow boundary — the one input
    # path this validator exists to close. ``None`` remains the single spelling
    # for "no plan".
    if plan_input is not None and not isinstance(plan_input, dict):
        raise ValueError(f"{caller} requires plan_input to be a dict when provided")
    _reject_credential_named_key(
        plan_input, message_prefix=f"{caller} requires plan_input to not include"
    )


def _validate_github_arg(github: Optional[Dict[str, Any]], *, caller: str, required: bool) -> None:
    """Shared ``github`` argument validation for both dispatchers.

    Preconditions:
        - ``caller`` is the calling function's name, used verbatim in raised
          messages.
        - ``required`` is True for callers (``execute_coding_team_workflow``)
          that must always receive a non-empty ``github`` dict; False for
          callers (``start_coding_team_workflow``) for which ``github`` is
          optional.
    Postconditions:
        - Returns None when ``github`` passes validation. When ``required``,
          that means exactly one shape: a non-empty dict carrying no
          credential-named key. When NOT ``required``, it means any FALSY
          ``github`` — ``None``, but also ``{}``, ``""``, ``0``, ``[]`` and
          any other falsy value, since the non-dict guard is only reached for
          a TRUTHY ``github`` — or a truthy dict carrying no credential-named
          key (any of :data:`_TOKEN_KEY_MARKERS` — token, secret, password,
          passphrase, api_key/api-key/apikey,
          private_key/private-key/privatekey, authorization, credential) at
          any nesting depth. Accepting falsy non-dicts here is harmless rather
          than a hole: a falsy value carries no credential to leak, and every
          consumer treats it exactly as it treats an absent ``github``.
    Raises:
        ValueError: ``required`` is True and ``github`` is not a non-empty
            dict; or ``github`` is truthy but not a ``dict`` (a bare truthy
            non-dict, e.g. a token string, would otherwise bypass
            :func:`_contains_token_key`'s dict/list/tuple-only traversal and
            be serialized into the workflow payload verbatim); or ``github``
            contains a credential-named key at any nesting depth.
    """
    if required:
        if not isinstance(github, dict) or not github:
            raise ValueError(f"{caller} requires a non-empty github dict")
    elif github and not isinstance(github, dict):
        raise ValueError(f"{caller} requires github to be a dict when provided")
    _reject_credential_named_key(
        github, message_prefix=f"{caller} github workflow payload must not include"
    )


def _workflow_id(job_id: str) -> str:
    """The Temporal workflow id for ``job_id`` — one shared spelling for both dispatchers."""
    return f"{WORKFLOW_ID_PREFIX}{job_id}"


def _build_workflow_payload(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the ``CodingTeamWorkflow.run`` argument dict shared by both dispatchers.

    Preconditions:
        - ``job_id``/``repo_path`` have already passed
          :func:`_validate_common_args`, and ``github``/``plan_input`` have
          already passed :func:`_validate_github_arg` /
          :func:`_validate_plan_input_arg`. This builder re-checks ONLY the
          credential half of that precondition (shape and presence checks are
          left to the validators) and otherwise copies both payload dicts
          VERBATIM into the durable Temporal workflow argument, so a caller
          that skips the validators would otherwise bypass the credential
          screen entirely — and Temporal event history is permanent. Because
          that failure is IRREVERSIBLE, the re-check is made here rather than
          left to caller convention: it is one cheap dict-key scan at the
          serialization boundary, and it is the last point at which a
          credential-named key can still be stopped. It covers BOTH payload
          dicts, and raises rather than asserts so it survives ``python -O``,
          where an ``assert`` would be stripped and the boundary left
          unguarded in exactly the deployments least likely to be watched.

    Postconditions:
        - Returns ``{"job_id", "repo_path", "plan_input"}`` plus ``"github"``
          when ``github`` is truthy (omitted entirely when falsy/``None``, so a
          caller with no GitHub context doesn't send a spurious empty dict).

    Raises:
        ValueError: ``github`` or ``plan_input`` is a truthy dict-like value
            carrying a credential-named key at any nesting depth (see
            :func:`_contains_token_key`).
    """
    for name, candidate in (("github", github), ("plan_input", plan_input)):
        _reject_credential_named_key(
            candidate, message_prefix=f"{name} workflow payload must not include"
        )
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "repo_path": repo_path,
        "plan_input": plan_input,
    }
    if github:
        payload["github"] = github
    return payload


def _prepare_workflow_args(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Optional[Dict[str, Any]],
    *,
    caller: str,
    github_required: bool,
) -> Tuple[Dict[str, Any], str]:
    """Validate a dispatch's arguments and build its Temporal payload + workflow id.

    Both dispatchers ran the identical validate-then-build prologue --
    :func:`_validate_common_args`, :func:`_validate_github_arg`,
    :func:`_validate_plan_input_arg`, :func:`_build_workflow_payload`,
    :func:`_workflow_id` -- differing only in ``caller`` and whether ``github``
    is required. Keeping one copy means a new validation rule cannot be added
    to the start path and forgotten on the execute path (or vice versa), which
    for the credential screen would be a silent leak on whichever path was
    missed.

    Preconditions:
        - ``caller`` is the calling dispatcher's name, interpolated verbatim
          into every message the validators raise.
        - ``github_required`` is True for a dispatcher that must always receive
          a non-empty ``github`` dict, False for one for which it is optional.

    Postconditions:
        - Returns ``(payload, workflow_id)`` once every validator passes: the
          payload :func:`_build_workflow_payload` builds for these arguments,
          and ``coding_team-<job_id>``.
        - Runs the validators in the same order both dispatchers used, so the
          FIRST violation a caller sees is unchanged by this extraction.
        - Performs no I/O and starts nothing -- a caller that raises here has
          not touched Temporal.

    Raises:
        ValueError: any of the three validators rejects its argument (empty
            ``job_id``/``repo_path``; a ``github`` that is required-but-absent,
            truthy-but-not-a-dict, or carries a credential-named key; a
            ``plan_input`` that is non-``None``-but-not-a-dict or carries a
            credential-named key) -- see those functions for the exact
            contracts.
    """
    _validate_common_args(job_id, repo_path, caller=caller)
    _validate_github_arg(github, caller=caller, required=github_required)
    _validate_plan_input_arg(plan_input, caller=caller)
    return _build_workflow_payload(job_id, repo_path, plan_input, github), _workflow_id(job_id)


def start_coding_team_workflow(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Optional[Dict[str, Any]] = None,
) -> None:
    """Start ``CodingTeamWorkflow`` for a coding-team job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists (the API
          called ``create_job`` before dispatching).
        - ``repo_path`` is a non-empty str; ``plan_input`` is a JSON-serializable
          plan dict (a run with no plan has nothing to execute) that must not
          contain a CREDENTIAL-named key (any of :data:`_TOKEN_KEY_MARKERS` —
          token, secret, password, passphrase, api_key/api-key/apikey,
          private_key/private-key/privatekey, authorization, credential) at
          any nesting depth (see :func:`_contains_token_key`) — it is serialized into the
          same durable Temporal payload as ``github``.
        - ``github``, when provided, is a dict of GitHub-issue run metadata for
          the workflow (owner/repo/issue/base/integration_branch/expected_base_sha/...).
          It must not contain a CREDENTIAL-named key (any of :data:`_TOKEN_KEY_MARKERS`)
          either — activities resolve credentials activity-side.
    Postconditions:
        - A workflow with id ``coding_team-<job_id>`` is started on the coding
          team task queue (fire-and-forget; the caller polls
          ``GET /status/{job_id}``). When ``github`` is a non-empty dict it is
          included on the payload under ``"github"``; otherwise that key is
          omitted.
    Raises:
        ValueError: ``job_id`` or ``repo_path`` is empty; ``github`` is truthy
            but not a ``dict``; ``plan_input`` is not ``None`` and not a
            ``dict``; or ``github`` or
            ``plan_input`` contains a CREDENTIAL-named key (any of :data:`_TOKEN_KEY_MARKERS`) at any
            nesting depth (see :func:`_contains_token_key`).
        RuntimeError: the worker's Temporal client never becomes available
            within the wait window.
    """
    payload, workflow_id = _prepare_workflow_args(
        job_id,
        repo_path,
        plan_input,
        github,
        caller="start_coding_team_workflow",
        github_required=False,
    )
    start_workflow_sync(
        CodingTeamWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started CodingTeamWorkflow id=%s", workflow_id)


def execute_coding_team_workflow(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Dict[str, Any],
) -> Dict[str, Any]:
    """Run one coding-team workflow and wait for its terminal result.

    This is the completion-aware counterpart to :func:`start_coding_team_workflow`.
    It is used by review-comment remediation because a thread must not be replied
    to or resolved merely because Temporal accepted a workflow start.

    Preconditions:
        - ``job_id`` is a non-empty str, unique per review comment, naming an
          existing child job.
        - ``repo_path`` is a non-empty str.
        - ``github`` is a REQUIRED ``dict`` of GitHub PR/comment metadata and
          must not contain a CREDENTIAL-named key (any of
          :data:`_TOKEN_KEY_MARKERS` — token, secret, password, passphrase,
          api_key/api-key/apikey, private_key/private-key/privatekey,
          authorization, credential);
          activities resolve credentials from the child job's encrypted token
          or ``GITHUB_TOKEN`` instead.
        - ``plan_input``, when not ``None``, is a JSON-serializable plan dict
          (it is sent verbatim as a Temporal workflow argument) that must not
          contain a CREDENTIAL-named key (any of :data:`_TOKEN_KEY_MARKERS`) at any nesting
          depth (see :func:`_contains_token_key`).
    Postconditions:
        - Blocks until ``CodingTeamWorkflow`` reaches a terminal result and returns
          that result. A pause remains durable in Temporal and is resumed through
          the normal answer-signal path.
        - Blocking past ``_COMMENT_WORKFLOW_TIMEOUT_S`` (the client-side wait
          window) does NOT surface as failure: this caller reattaches to the
          same still-running workflow (``reattach_on_timeout=True``) and keeps
          waiting rather than reporting a terminal failure while the durable
          workflow may later succeed and push code with nobody watching. The
          caller is a per-comment background-thread worker (see
          ``address_comments._dispatch_implementation``), which can afford to
          keep blocking — there is no request deadline to respect here. The
          residual cost, for operators: a workflow that never reaches a
          terminal state (e.g. it is paused on an answer signal nobody ever
          sends) pins its background thread for the lifetime of the process —
          one leaked thread per stuck workflow, with no abandonment path short
          of terminating the workflow in Temporal.
    Raises:
        ValueError: ``job_id``/``repo_path`` are empty, ``github`` is not a
            non-empty dict, ``plan_input`` is not ``None`` and not a dict, or
            ``github`` or ``plan_input`` contains a CREDENTIAL-named key (any of :data:`_TOKEN_KEY_MARKERS`)
            at any nesting depth (see :func:`_contains_token_key`).
        RuntimeError: ``CodingTeamWorkflow.run`` returned a non-dict result; the
            message names BOTH the workflow id and the observed type, so the run
            is identifiable in Temporal from the error alone. This surfaces on a
            per-comment background worker, far from the dispatch context, where
            the type name by itself would not say WHICH run misbehaved.
        Exception: Any other exception ``execute_workflow_sync`` itself raises
            (a Temporal RPC error, the workflow's own failure exception, a
            cancellation, etc.) propagates unchanged — this function does not
            catch or wrap it. Callers such as the per-comment background
            worker must be prepared for these, not just the two explicit
            raises above.
    """
    payload, workflow_id = _prepare_workflow_args(
        job_id,
        repo_path,
        plan_input,
        github,
        caller="execute_coding_team_workflow",
        github_required=True,
    )
    logger.info(
        "Executing CodingTeamWorkflow id=%s (timeout_s=%s)",
        workflow_id,
        _COMMENT_WORKFLOW_TIMEOUT_S,
    )
    result = execute_workflow_sync(
        CodingTeamWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
        execute_timeout_s=_COMMENT_WORKFLOW_TIMEOUT_S,
        reattach_on_timeout=True,
    )
    logger.info("CodingTeamWorkflow id=%s reached terminal result", workflow_id)
    if not isinstance(result, dict):
        raise RuntimeError(
            f"CodingTeamWorkflow id={workflow_id} returned a non-dict result: "
            f"{type(result).__name__}"
        )
    return result
