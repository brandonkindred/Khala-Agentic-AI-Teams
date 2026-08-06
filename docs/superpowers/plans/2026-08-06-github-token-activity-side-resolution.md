# Activity-Side GitHub Token Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve GitHub tokens inside the three coding-team GitHub-hook Temporal activities from the encrypted job record or `GITHUB_TOKEN`, and reject any plaintext `token` activity argument so secrets never appear in Temporal workflow history by design.

**Architecture:** Add `_require_activity_github_token(request) -> str` in `coding_team_github_activities.py` (lazy imports for Temporal sandbox safety). Each activity calls it first, then uses the returned token with existing git/GitHub helpers. Tests seed encrypted job tokens (or env) instead of passing `token` in the request.

**Tech Stack:** Python 3.10+, pytest, existing `token_crypto.encrypt_token`/`decrypt_token`, coding-team job store via `coding_team_main.get_job`, Temporal activity wrappers already in-tree

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-06-github-token-activity-side-resolution-design.md`
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Design-by-Contract docstrings (Preconditions / Postconditions) on the new helper and updated activities
- Error messages name field names / reasons only — never `repr(request)`, ciphertext, or plaintext tokens
- Ruff line-length 120; coverage ≥ 90% on new/changed code
- Do not wire activities into `CodingTeamWorkflow` (out of scope)
- Do not change `_resolve_github_job_token` resume soft-fail behavior
- Work from worktree `.worktrees/issue-3992-github-token-activity-side` on branch `fix/3992-github-token-activity-side`
- Pytest via: `cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …` (or the worktree’s own `.venv` if present)

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/temporal/coding_team_github_activities.py` | Helper + three activities |
| `backend/agents/software_engineering_team/tests/test_coding_team_github_activity_token.py` | Helper unit tests (new) |
| `backend/agents/software_engineering_team/tests/test_coding_team_github_branch_prep_activity.py` | Branch-prep contract tests |
| `backend/agents/software_engineering_team/tests/test_coding_team_github_publish_activity.py` | Publish contract tests |
| `backend/agents/software_engineering_team/tests/test_coding_team_github_failure_notice_activity.py` | Failure-notice contract tests |

---

### Task 1: `_require_activity_github_token` helper (TDD)

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_coding_team_github_activity_token.py`
- Modify: `backend/agents/software_engineering_team/temporal/coding_team_github_activities.py`

**Interfaces:**
- Consumes: `request: dict[str, Any]` with optional `job_id`; job record may have `github_token_encrypted`; env `GITHUB_TOKEN` / `INTEGRATION_ENCRYPTION_KEY`
- Produces: `_require_activity_github_token(request: dict[str, Any]) -> str`

- [ ] **Step 1: Write the failing helper tests**

Create `test_coding_team_github_activity_token.py`:

```python
"""Unit tests for activity-side GitHub token resolution."""

from __future__ import annotations

from typing import Any, Optional

import pytest
from cryptography.fernet import Fernet

from software_engineering_team import token_crypto
from software_engineering_team.tests.conftest import _ensure_real_modules, _stub_orchestrator_only


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    _ensure_real_modules()
    _stub_orchestrator_only(monkeypatch)
    from software_engineering_team.api import coding_team_main as api_main

    return api_main


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _helper():
    from software_engineering_team.temporal.coding_team_github_activities import (
        _require_activity_github_token,
    )

    return _require_activity_github_token


def test_rejects_plaintext_token_key_without_echoing_secret(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: {"job_id": job_id})
    secret = "ghp_should_not_appear"
    with pytest.raises(ValueError, match="token") as exc_info:
        _helper()({"job_id": "job-1", "token": secret})
    assert secret not in str(exc_info.value)


def test_rejects_missing_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="job_id"):
        _helper()({})


def test_rejects_unknown_job(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: None)
    with pytest.raises(ValueError, match="job_id"):
        _helper()({"job_id": "missing"})


def test_resolves_encrypted_job_token(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    _set_key(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    ct = token_crypto.encrypt_token("persisted-pat")
    assert ct is not None
    monkeypatch.setattr(
        api, "get_job", lambda job_id, cache_dir=None: {"github_token_encrypted": ct}
    )
    assert _helper()({"job_id": "job-1"}) == "persisted-pat"


def test_falls_back_to_github_token_env(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-pat")
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: {})
    assert _helper()({"job_id": "job-1"}) == "env-pat"


def test_encrypted_prefers_over_env(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    _set_key(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "env-pat")
    ct = token_crypto.encrypt_token("persisted-pat")
    assert ct is not None
    monkeypatch.setattr(
        api, "get_job", lambda job_id, cache_dir=None: {"github_token_encrypted": ct}
    )
    assert _helper()({"job_id": "job-1"}) == "persisted-pat"


def test_rejects_when_no_token_available(monkeypatch: pytest.MonkeyPatch, api: Any) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(api, "get_job", lambda job_id, cache_dir=None: {})
    with pytest.raises(ValueError, match="token"):
        _helper()({"job_id": "job-1"})
```

- [ ] **Step 2: Run helper tests to verify they fail**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_activity_token.py -v
```

(If the worktree has no `.venv`, use the absolute path to the main repo’s `backend/.venv/bin/python`.)

Expected: FAIL — `_require_activity_github_token` is not defined (ImportError).

- [ ] **Step 3: Implement the helper**

In `coding_team_github_activities.py`, after `_REQUIRED_FIELDS` (and before the first `@activity.defn`), add:

```python
def _require_activity_github_token(request: dict[str, Any]) -> str:
    """Resolve a GitHub token for a Temporal GitHub-hook activity.

    Preconditions:
        - ``request`` is a dict (the activity request payload).
    Postconditions:
        - Raises ``ValueError`` if ``\"token\"`` is present in ``request`` (plain-text
          tokens must not appear in Temporal activity arguments).
        - Raises ``ValueError`` if ``job_id`` is missing/falsy, the job cannot be
          loaded, or neither ``github_token_encrypted`` nor ``GITHUB_TOKEN`` yields
          a usable token. Messages name field names / reasons only — never the
          request payload, ciphertext, or plaintext secrets.
        - Returns the plaintext token for in-activity use only (never place it in
          the activity return value).
    """
    if "token" in request:
        raise ValueError(
            "github activity request must not include 'token'; "
            "resolve the token activity-side from the job record or GITHUB_TOKEN"
        )
    job_id = request.get("job_id")
    if not job_id:
        raise ValueError("github activity missing required fields: ['job_id']")

    import os

    from software_engineering_team.api import coding_team_main as _main
    from software_engineering_team.token_crypto import decrypt_token

    job = _main.get_job(job_id)
    if job is None:
        raise ValueError("github activity job_id not found")

    token = decrypt_token(job.get("github_token_encrypted")) or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("github activity has no usable GitHub token")
    return token
```

Keep every non-trivial import inside the function body (module docstring sandbox rule).

- [ ] **Step 4: Run helper tests to verify they pass**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_activity_token.py -v
```

Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/temporal/coding_team_github_activities.py \
  backend/agents/software_engineering_team/tests/test_coding_team_github_activity_token.py
git commit -m "$(cat <<'EOF'
Add activity-side GitHub token resolver for Temporal hooks.

EOF
)"
```

---

### Task 2: Wire helper into all three activities + update contracts

**Files:**
- Modify: `backend/agents/software_engineering_team/temporal/coding_team_github_activities.py`

**Interfaces:**
- Consumes: `_require_activity_github_token(request) -> str` from Task 1
- Produces: Updated activity contracts — no `token` in required-field tuples; `job_id` required for branch prep; each activity resolves token before side effects

- [ ] **Step 1: Write failing acceptance assertions in the helper test file**

Append to `test_coding_team_github_activity_token.py`:

```python
def test_activity_required_field_tuples_exclude_token() -> None:
    from software_engineering_team.temporal import coding_team_github_activities as mod

    assert "token" not in mod._REQUIRED_FIELDS
    assert "job_id" in mod._REQUIRED_FIELDS
    assert "token" not in mod._PUBLISH_REQUIRED_FIELDS
    assert "job_id" in mod._PUBLISH_REQUIRED_FIELDS
    assert "token" not in mod._FAILURE_NOTICE_REQUIRED_FIELDS
    assert "job_id" in mod._FAILURE_NOTICE_REQUIRED_FIELDS
```

- [ ] **Step 2: Run that test to verify it fails**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_activity_token.py::test_activity_required_field_tuples_exclude_token -v
```

Expected: FAIL — `"token"` still in publish/failure required tuples; `job_id` missing from `_REQUIRED_FIELDS`.

- [ ] **Step 3: Update activity module constants and bodies**

1. Change:

```python
_REQUIRED_FIELDS = ("job_id", "repo_path", "remote", "default_branch", "integration_branch")
```

```python
_PUBLISH_REQUIRED_FIELDS = ("job_id", "owner", "repo", "repo_path", "issue_number")
```

```python
_FAILURE_NOTICE_REQUIRED_FIELDS = ("job_id", "owner", "repo", "number", "message", "kind")
```

2. At the top of each activity body, **before** the `missing = …` check (so a forbidden `token` fails first even when other fields are missing), resolve:

```python
    token = _require_activity_github_token(request)
```

Note: `_require_activity_github_token` already validates `job_id`. Keeping `job_id` in the required-field tuples is intentional so missing-`job_id` still matches the existing `"missing required fields"` message path when the helper’s check is skipped… Prefer **helper first**, then required-field check for the *other* fields. To avoid double-raising on missing `job_id`, either:
- Call helper first (recommended — matches fail-closed token check first), and leave `job_id` in the tuples (helper raises first with `['job_id']` when missing), or
- Exclude `job_id` from the post-helper missing scan.

Use helper-first; keep `job_id` in the tuples for documentation/acceptance test; when `job_id` is missing the helper raises before the tuple scan.

3. Replace all `request.get("token")` / `request["token"]` uses with the local `token` variable:

- Branch prep: `_prepare_issue_branch(..., token, issue_number=...)`
- Publish: `GitHubClient(token=token)` and `_publish_merged_work(..., token)`
- Failure notice: `GitHubClient(token=token)`

4. Update each activity docstring:
- Remove language about plain-text `token` / deferred activity-side resolution.
- Document: must not include `token`; must include `job_id`; token is resolved via `_require_activity_github_token`.
- Keep “never put secrets in ValueError messages” notes.

Example branch-prep preamble (adapt publish/failure similarly):

```python
    """Prepare development + integration branches for a GitHub-issue-driven run.
    ...
    Preconditions:
        - ``request`` carries non-empty string values for ``job_id``, ``repo_path``,
          ``remote``, ``default_branch``, and ``integration_branch``. Must NOT
          include a ``token`` field — the activity resolves the GitHub token
          from the job's ``github_token_encrypted`` or ``GITHUB_TOKEN``.
        - May also carry ``issue_number`` (Optional[int]).
    ...
    """
    token = _require_activity_github_token(request)
    missing = [f for f in _REQUIRED_FIELDS if not request.get(f)]
    ...
```

- [ ] **Step 4: Run acceptance + helper tests**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_activity_token.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/temporal/coding_team_github_activities.py \
  backend/agents/software_engineering_team/tests/test_coding_team_github_activity_token.py
git commit -m "$(cat <<'EOF'
Resolve GitHub tokens inside Temporal hook activities.

EOF
)"
```

---

### Task 3: Update branch-prep activity tests

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_github_branch_prep_activity.py`

**Interfaces:**
- Consumes: activity requiring `job_id` + resolved token; helper from Task 1
- Produces: Updated tests matching fail-closed token contract

- [ ] **Step 1: Add shared job-seed helper and rewrite failing cases**

Near the top of the test module (after fixtures), add:

```python
def _seed_job_token(
    monkeypatch: pytest.MonkeyPatch, api: Any, job_id: str, plaintext: str = "tok-123"
) -> str:
    """Persist an encrypted token on a fake job and return the plaintext."""
    from cryptography.fernet import Fernet

    from software_engineering_team import token_crypto

    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    ct = token_crypto.encrypt_token(plaintext)
    assert ct is not None
    monkeypatch.setattr(
        api,
        "get_job",
        lambda jid, cache_dir=None: (
            {"job_id": jid, "github_token_encrypted": ct} if jid == job_id else None
        ),
    )
    return plaintext
```

Update every successful / auth-threading call to include `"job_id": "job-1"` and call `_seed_job_token` first. Remove `"token": ...` from requests.

Replace `test_branch_prep_activity_without_token_uses_no_auth_env` with:

```python
def test_branch_prep_activity_rejects_unresolvable_token(api, monkeypatch) -> None:
    """No encrypted job token and no GITHUB_TOKEN must fail closed."""
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(api, "get_job", lambda jid, cache_dir=None: {"job_id": jid})
    with pytest.raises(ValueError, match="token"):
        github_branch_prep_activity(
            {
                "job_id": "job-1",
                "repo_path": "/repo",
                "remote": "origin",
                "default_branch": "main",
                "integration_branch": "khala/issue-1",
            }
        )
```

Add:

```python
def test_branch_prep_activity_rejects_plaintext_token_arg(api, monkeypatch) -> None:
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )

    _seed_job_token(monkeypatch, api, "job-1")
    secret = "ghp_leaked"
    with pytest.raises(ValueError, match="token") as exc_info:
        github_branch_prep_activity(
            {
                "job_id": "job-1",
                "repo_path": "/repo",
                "remote": "origin",
                "default_branch": "main",
                "integration_branch": "khala/issue-1",
                "token": secret,
            }
        )
    assert secret not in str(exc_info.value)
```

Update `test_branch_prep_activity_raises_on_missing_required_field`:
- Add `"job_id": "job-1"` to each request_dict that still tests other missing fields (and seed the job), **or** add a dedicated case `({"repo_path": "/x", ... without job_id}, ["job_id"], None)`.
- Remove the parametrize case that includes `"token": "fake-token-xyz"` (covered by the reject-plaintext test).
- Missing-field cases that include a token key should not — forbidden-token raises first.

Update `test_branch_prep_activity_passes_auth_env_to_fetch` to seed the job and omit `token` from the request; assert auth header still matches `tok-123`.

Update `test_branch_prep_activity_clean_checkout_returns_ok_true` and `test_branch_prep_activity_unsafe_ref_returns_ok_false` to seed job + pass `job_id` (unsafe-ref still needs a resolvable token before `_prepare_issue_branch` runs).

- [ ] **Step 2: Run branch-prep tests**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_branch_prep_activity.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_coding_team_github_branch_prep_activity.py
git commit -m "$(cat <<'EOF'
Update branch-prep activity tests for activity-side tokens.

EOF
)"
```

---

### Task 4: Update publish + failure-notice activity tests

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_github_publish_activity.py`
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_github_failure_notice_activity.py`

**Interfaces:**
- Consumes: activities that resolve token via job/`GITHUB_TOKEN`; `_install` fake stores
- Produces: Tests that omit `token` from requests and seed `github_token_encrypted` (or env)

- [ ] **Step 1: Update publish tests**

In `test_coding_team_github_publish_activity.py`:

1. Change `BASE_REQUEST` to drop `"token"`:

```python
BASE_REQUEST = {
    "job_id": "job-1",
    "owner": "acme",
    "repo": "widgets",
    "repo_path": "/repo",
    "issue_number": 9,
}
```

2. Extend `_install` (or add a wrapper) so every seeded job includes an encrypted token by default:

```python
def _install(
    monkeypatch: pytest.MonkeyPatch, api: Any, job_id: str, **job_fields: Any
) -> tuple[_FakeJobStore, Callable[[], _FakeGitHubClient]]:
    from cryptography.fernet import Fernet

    from software_engineering_team import token_crypto

    if "github_token_encrypted" not in job_fields:
        monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
        ct = token_crypto.encrypt_token("tok-123")
        assert ct is not None
        job_fields = {**job_fields, "github_token_encrypted": ct}
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # ... existing store/client wiring unchanged ...
```

3. In `test_publish_activity_missing_base_field_raises_value_error`, remove the `({"token": None}, ["token"])` parametrize case. Add a separate test:

```python
def test_publish_activity_rejects_plaintext_token_arg(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    _install(monkeypatch, api, "job-1")
    secret = "ghp_leaked"
    request = {**BASE_REQUEST, **MERGED_WORK_FIELDS, "token": secret}
    with pytest.raises(ValueError, match="token") as exc_info:
        _activity()(request)
    assert secret not in str(exc_info.value)
```

4. Add unresolvable-token test: `_install` with `github_token_encrypted` explicitly set to skip default — pass a job with no ciphertext and `delenv GITHUB_TOKEN`, expecting `ValueError` matching `token`. Easiest: monkeypatch `get_job` after `_install` to return `{}` fields without ciphertext, or call `_install(..., github_token_encrypted="")` and adjust `_install` so empty ciphertext is kept and env is cleared.

5. Ensure every happy-path / failure-path test still constructs `GitHubClient` with `"tok-123"` (assert via `get_client().token`).

- [ ] **Step 2: Update failure-notice tests**

Mirror the same pattern in `test_coding_team_github_failure_notice_activity.py`:
- Drop `"token"` from `BASE_REQUEST`
- Seed encrypted token inside `_install`
- Remove `({"token": None}, ["token"])` from missing-field parametrize
- Add reject-plaintext and unresolvable-token tests
- Keep asserting fake client receives `"tok-123"`

- [ ] **Step 3: Run publish + failure-notice + helper + branch-prep suites**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_activity_token.py \
  agents/software_engineering_team/tests/test_coding_team_github_branch_prep_activity.py \
  agents/software_engineering_team/tests/test_coding_team_github_publish_activity.py \
  agents/software_engineering_team/tests/test_coding_team_github_failure_notice_activity.py \
  -v
```

Expected: PASS (all tests in these four files).

- [ ] **Step 4: Commit**

```bash
git add \
  backend/agents/software_engineering_team/tests/test_coding_team_github_publish_activity.py \
  backend/agents/software_engineering_team/tests/test_coding_team_github_failure_notice_activity.py
git commit -m "$(cat <<'EOF'
Update publish and failure-notice tests for activity-side tokens.

EOF
)"
```

---

### Task 5: Final verification

**Files:** (none new — verification only)

- [ ] **Step 1: Re-run the full in-scope suite**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_activity_token.py \
  agents/software_engineering_team/tests/test_coding_team_github_branch_prep_activity.py \
  agents/software_engineering_team/tests/test_coding_team_github_publish_activity.py \
  agents/software_engineering_team/tests/test_coding_team_github_failure_notice_activity.py \
  -v --tb=short
```

Expected: PASS.

- [ ] **Step 2: Lint changed Python files**

```bash
cd backend && ../.venv/bin/ruff check \
  agents/software_engineering_team/temporal/coding_team_github_activities.py \
  agents/software_engineering_team/tests/test_coding_team_github_activity_token.py \
  agents/software_engineering_team/tests/test_coding_team_github_branch_prep_activity.py \
  agents/software_engineering_team/tests/test_coding_team_github_publish_activity.py \
  agents/software_engineering_team/tests/test_coding_team_github_failure_notice_activity.py
```

Expected: no issues. Fix any ruff findings and amend only if the prior commit was yours, unpushed, and the user asked — otherwise make a new fix commit.

- [ ] **Step 3: Confirm acceptance criterion**

Manually confirm (or assert via `test_activity_required_field_tuples_exclude_token` + reject-plaintext tests):
- No activity required-field tuple includes `"token"`.
- Passing `"token"` in a request raises `ValueError` without echoing the secret.
- Happy paths resolve from encrypted job field (prefer) or `GITHUB_TOKEN`.

No extra commit unless Step 2 produced fixes.
