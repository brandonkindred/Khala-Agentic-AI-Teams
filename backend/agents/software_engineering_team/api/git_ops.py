"""coding_team API — git/branch machinery: auth env, active-issue tracking, ephemeral checkouts, rescue branches, and issue-branch prep.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.
"""

from __future__ import annotations

import base64
import fcntl
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shared.git.git_utils import (
    DEVELOPMENT_BRANCH,
    git_identity_env,
)
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.clone_workspace import (
    clone_lock_path,
    is_per_issue_dir,
    is_within_ephemeral_workspace,
)
from software_engineering_team.github_source import (
    scrub_token_from_text,
)
from software_engineering_team.github_source.client import _is_safe_ref

logger = logging.getLogger(__name__)


def _git_auth_env(token: str) -> Dict[str, str]:
    """Build an env dict that injects Basic credentials via ``GIT_CONFIG_*`` vars.

    Mirrors the unified API's clone-time auth (``_git_auth_env`` in
    ``unified_api/routes/integrations.py``): the credential is passed
    transiently through the environment and never written to ``.git/config``.
    That matters because the checkout lives on the shared ``agents_data``
    volume — a persisted token would outlive the job and leak across runs.

    The scheme must be ``Basic`` with the ``x-access-token`` username:
    GitHub's git smart-HTTP endpoint rejects a ``Bearer`` header (401
    ``invalid credentials``) even for a valid token — Bearer is only accepted
    by the REST API — after which git tries to prompt for a username and
    fails headless ("terminal prompts disabled").

    Preconditions:
        - ``token`` is a non-empty GitHub credential authorizing the operation.
    Postconditions:
        - Returns a copy of ``os.environ`` augmented with a single transient
          ``http.extraHeader`` git-config entry (Authorization: Basic) and
          ``GIT_TERMINAL_PROMPT=0`` so a missing/invalid credential fails fast
          instead of blocking on an interactive prompt until the git timeout.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _scrub_auth_header_values(msg: str, env: Optional[Dict[str, str]]) -> str:
    """Redact the transient auth header from git output.

    ``scrub_token_from_text`` only covers URL-embedded credentials; the
    header value built by ``_git_auth_env`` is a second representation of
    the token (Basic + base64) that verbose/trace git output can echo. Job
    errors and issue comments are built from these messages, so every
    representation must be redacted.

    Postconditions:
        - Neither the full header value, the ``Basic <b64>`` credential, nor
          the bare base64 form appears in the returned text.
    """
    if not env:
        return msg
    header = env.get("GIT_CONFIG_VALUE_0") or ""
    if not header.startswith("Authorization: "):
        return msg
    credential = header[len("Authorization: ") :]  # "Basic <b64>"
    encoded = credential.rsplit(" ", 1)[-1]
    for needle in (header, credential, encoded):
        if needle:
            msg = msg.replace(needle, "***")
    return msg


def _git(
    repo_path: str,
    *args: str,
    timeout: float = 120.0,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str]:
    """Run a git subcommand in ``repo_path``.

    Postconditions:
        - Returns ``(returncode, scrubbed_message)``; the message has any
          URL-embedded token redacted via ``scrub_token_from_text`` and, when
          an auth env was supplied, the transient Authorization header value
          (including its base64 form) redacted as well.
        - ``env=None`` (default) inherits the parent environment, preserving the
          prior behaviour for local-only operations. Pass an auth env (see
          ``_git_auth_env``) for network operations against a private remote.
    """
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        msg = _scrub_auth_header_values((r.stderr or r.stdout).strip(), env)
        return r.returncode, scrub_token_from_text(msg)
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, scrub_token_from_text(_scrub_auth_header_values(str(e), env))


RESCUE_BRANCH_PREFIX = "khala/rescue/"
ACTIVE_ISSUE_CONFIG_KEY = "khala.active-issue"


def _utc_timestamp() -> str:
    """Wall-clock UTC stamp used in rescue branch names (patchable in tests)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _read_active_issue(repo_path: str) -> Optional[int]:
    """Read the repo-local active-issue marker.

    The marker means: a job for that issue was mid-flight on this checkout
    and terminated abnormally (restart, kill, delete). It is the only state
    that survives job deletion, so leftover work is attributed through it.

    Postconditions:
        - Returns the issue number, or None when the marker is absent or
          unparseable (treated as unattributed).
    """
    rc, msg = _main._git(repo_path, "config", "--local", "--get", ACTIVE_ISSUE_CONFIG_KEY)
    if rc != 0:
        return None
    try:
        return int(msg.strip())
    except ValueError:
        return None


def _write_active_issue(repo_path: str, issue_number: int) -> None:
    """Record that a job for issue_number is mid-flight on this checkout."""
    _main._git(repo_path, "config", "--local", ACTIVE_ISSUE_CONFIG_KEY, str(issue_number))


def _clear_active_issue(repo_path: str) -> None:
    """Remove the marker; idempotent (unsetting a missing key is a no-op)."""
    _main._git(repo_path, "config", "--local", "--unset", ACTIVE_ISSUE_CONFIG_KEY)


def _clear_active_issue_if_matches(repo_path: str, issue_number: int) -> None:
    """Remove the marker only when it belongs to this job's issue.

    Two different issues may legitimately run against the same checkout (the
    duplicate guard is per-issue); an older job publishing after a newer job
    prepped must not unset the newer job's marker, or a crash of the newer
    job would lose its development-work attribution.

    Postconditions:
        - The marker is unset iff it equaled ``issue_number``; any other
          value (or no marker) is left untouched.
    """
    if _read_active_issue(repo_path) == issue_number:
        _clear_active_issue(repo_path)


def _is_deletable_per_issue(target: Path) -> bool:
    """True iff *target* is an ephemeral per-issue git checkout safe to delete.

    The three content conditions (2–4) shared by the resolve-time gate in
    ``_ephemeral_checkout_target`` and the under-lock re-validation in
    ``_cleanup_issue_checkout``: strictly under an ephemeral workspace root, an
    ``issue-{N}`` per-issue final component, and carrying a ``.git`` entry. It
    does NOT resolve or re-check the symlink-root condition (1) — callers pass an
    already-resolved, non-symlink ``Path``.

    Preconditions:
        - ``target`` is an already-resolved path (symlink-collapsed).
    Postconditions:
        - Returns True iff all three content conditions hold. Pure apart from
          filesystem reads; never raises.
    """
    return (
        is_within_ephemeral_workspace(target)
        and is_per_issue_dir(target.name)
        and (target / ".git").exists()
    )


def _ephemeral_checkout_target(repo_path: str) -> Optional[Path]:
    """Resolve ``repo_path`` and return it iff it is a platform-owned per-issue
    git checkout safe to delete; otherwise ``None``.

    Resolving here (and handing the resolved ``Path`` back) means the path that is
    *validated* is the exact symlink-collapsed path the caller then deletes,
    closing the check-resolved / delete-raw-string gap a directory→symlink swap
    could otherwise exploit. Four conditions must all hold:

    1. the checkout root itself is NOT a symlink — a legitimate platform-owned
       per-issue checkout is a real directory created by ``git clone``. Resolving
       a symlinked root would follow it to its target, so a job that replaced its
       own ``issue-7`` directory with a symlink to a concurrently-running
       ``issue-8`` checkout would otherwise make cleanup delete the *sibling*;
    2. the path lives strictly under one of this deployment's ephemeral
       workspace roots (``is_within_ephemeral_workspace``) — so an
       operator-pinned or arbitrary path is never eligible (and a filesystem
       root or shallow system dir like ``/`` or ``/data`` is excluded because it
       is not under a workspace root), even if a caller sets the cleanup flag and
       points ``repo_path`` at someone else's repo;
    3. its final component is the auto-derived ``issue-{N}`` per-issue shape
       (``is_per_issue_dir``) — so a repo-level checkout that merely sits under an
       ephemeral root (e.g. the PR-review path ``.../github_workspaces/owner/repo``)
       is never deleted, matching the contract that only per-issue clones are
       reclaimed;
    4. it is actually a git checkout (carries a ``.git`` entry).

    Preconditions:
        - None on caller state; ``repo_path`` may be any string (it is validated
          here precisely because it originates from an untrusted request).
    Postconditions:
        - Returns the resolved ``Path`` when all four conditions hold; ``None`` on
          any resolution error (null byte / unresolvable) or when any condition
          fails. Pure apart from filesystem reads.
    """
    try:
        raw = Path(repo_path)
        resolved = raw.resolve()
        root_is_symlink = raw.is_symlink()
    except (OSError, ValueError):
        return None
    # Refuse a symlinked checkout root: resolving it would follow the link to its
    # target and delete *that* (e.g. a sibling issue-N checkout), not the job's own
    # directory. A real per-issue checkout is never a symlink.
    if root_is_symlink:
        return None
    # ``resolve()`` defaults to ``strict=False`` (Python 3.6+), so a not-yet-created
    # path resolves without raising; passing the already-resolved path to is_within
    # keeps its internal resolve idempotent. Conditions 2–4 are the shared
    # content gate (see ``_is_deletable_per_issue``).
    if not _is_deletable_per_issue(resolved):
        return None
    return resolved


def _is_ephemeral_checkout_path(repo_path: str) -> bool:
    """True only for a platform-owned per-issue git checkout that is safe to delete.

    Thin boolean view over ``_ephemeral_checkout_target`` (see it for the four
    conditions and the threat model). Kept as a predicate for call sites that only
    need the yes/no answer.

    Preconditions:
        - None on caller state; ``repo_path`` may be any string.
    Postconditions:
        - Returns True iff ``_ephemeral_checkout_target`` resolves a deletable
          checkout for ``repo_path``; False otherwise. Pure apart from filesystem
          reads.
    """
    return _ephemeral_checkout_target(repo_path) is not None


def _locked_rmtree(target: Path, repo_path: str) -> None:
    """Delete a resolved per-issue checkout while holding the shared clone flock.

    Holds the SAME sibling ``flock`` that unified_api's ``_ensure_repo_clone``
    takes around clone/fetch, keyed on the RESOLVED checkout path (not the raw
    request string) so a symlinked request can't lock a different name and leave
    the real checkout unguarded. Re-validates under the lock on the fixed
    resolved ``target`` — never by re-resolving ``repo_path`` — so a symlink
    swapped between the first resolve and lock acquisition cannot redirect the
    delete. The lock file is released and closed but never unlinked (unlinking a
    flock'd file lets a waiter keep the orphaned inode while a later run locks a
    fresh one, so two runs would each think they hold "the" lock).

    Preconditions:
        - ``target`` is the resolved, non-symlink per-issue checkout returned by
          ``_ephemeral_checkout_target``; ``repo_path`` is the original request
          string (used only for the failure log line).
    Postconditions:
        - Best-effort: ``target`` is removed only if the lock is acquired and it
          still resolves as a deletable per-issue checkout under the lock. Never
          raises — any lock/rmtree failure is caught and logged so a successful
          job is not turned into a failure. The success line is logged only after
          ``rmtree`` returns.
    """
    # clone_lock_path would only raise ValueError on an empty-name path, which a
    # validated per-issue target never is — but guard it anyway so a future change
    # can't break the "never raises" contract.
    try:
        lock_path = clone_lock_path(target)
    except ValueError as e:
        logger.warning("Skipping checkout cleanup; invalid lock path for %s: %s", target, e)
        return
    try:
        lock_file = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 - closed in finally
    except OSError as e:
        # Can't take the lock (e.g. parent vanished) — skip rather than delete
        # unsynchronised and risk racing a concurrent clone. Best-effort.
        logger.warning("Skipping checkout cleanup; could not open clone lock %s: %s", lock_path, e)
        return
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        except OSError as e:
            # flock can fail (e.g. ENOLCK on some network filesystems). Cleanup must
            # never turn a successful job into a failure, so skip rather than let it
            # propagate — honouring the "never raises" contract.
            logger.warning(
                "Skipping checkout cleanup; could not acquire clone lock %s: %s", lock_path, e
            )
            return
        # Re-validate under the lock on the fixed resolved ``target`` (see the
        # docstring): rmtree does not follow symlinks *inside* the tree, and the
        # resolved root is never a symlink, so a symlink planted in the checkout
        # can't redirect the delete.
        if not _is_deletable_per_issue(target):
            logger.warning("Checkout no longer a deletable per-issue path under lock: %s", target)
            return
        try:
            shutil.rmtree(target)
            logger.info("Removed ephemeral per-issue checkout at %s", target)
        except Exception as e:  # noqa: BLE001 - cleanup must never fail a successful job
            # exc_info so a partial-rmtree failure (the non-atomic case) is
            # diagnosable from the traceback, not just the message.
            logger.warning(
                "Failed to remove ephemeral checkout at %s: %s", repo_path, e, exc_info=True
            )
    finally:
        # Release and close, but do NOT unlink the lock file (see the docstring).
        # Both are wrapped so a degenerate flock/close can't break "never raises".
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lock_file.close()
        except OSError:
            pass


def _cleanup_issue_checkout(repo_path: str) -> None:
    """Remove a platform-owned, ephemeral per-issue checkout after clean success.

    Only called once the job has completed with every task merged and the work
    published to a PR, so the local clone holds nothing the remote does not. The
    folder is recreated by the caller's clone-or-fetch on a later run.

    Concurrency:
        The ``rmtree`` runs while holding the SAME sibling ``flock`` that
        unified_api's ``_ensure_repo_clone`` takes around clone/fetch. Without it,
        a quick ``/api/integrations/github/run-issue`` retry — whose clone happens
        in unified_api *before* the coding-team active-job guard runs — could
        clone/fetch into the directory mid-rmtree. The lock lives in the
        checkout's parent, so it survives the rmtree. The lock file is
        deliberately NOT unlinked: unlinking a flock'd file lets a waiter keep the
        old (now-orphaned) inode while a later run creates a fresh lock file and
        locks the new inode, so two runs would each think they hold "the" lock.
        Leaving it makes a stable per-issue lock both clone and cleanup share; the
        files are tiny and bounded by the number of distinct issues per repo.

    Postconditions:
        - Best-effort: the checkout is removed only when
          ``_ephemeral_checkout_target`` resolves ``repo_path`` to a
          platform-owned, non-shallow per-issue git checkout under an ephemeral
          root, and the resolved (symlink-collapsed) path it returns is the one
          deleted; an unsafe path is refused (logged, left in place). Never
          raises — a cleanup failure (permissions, lock unavailable, race with a
          concurrent reader) must not turn a successful job into a failure; it is
          caught and logged. The success line is logged only after ``rmtree``
          returns.

    Note:
        ``rmtree`` is not atomic. A failure partway through can leave a
        partially-deleted directory at ``repo_path`` (possibly missing
        ``.git``); the retained lock keeps serialising access, but a later retry
        whose ``_ensure_repo_clone`` finds a non-empty, non-git directory will
        fail its ``git clone`` and the leftover must be cleared manually. This is
        rare (``rmtree`` usually fails atomically on a permission error) and the
        published work is already safe on the remote PR.
    """
    target = _ephemeral_checkout_target(repo_path)
    if target is None:
        logger.warning("Refusing to remove unsafe or non-checkout path: %s", repo_path)
        return

    _locked_rmtree(target, repo_path)


def _is_ahead(repo_path: str, ref: str, base_ref: str) -> bool:
    """True if ref resolves to a commit and has commits not reachable from base_ref."""
    rc, _ = _main._git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc != 0:
        return False
    rc, out = _main._git(repo_path, "rev-list", "--count", f"{base_ref}..{ref}")
    if rc != 0:
        return False
    try:
        return int(out.strip()) > 0
    except ValueError:
        return False


def _reachable_from(repo_path: str, tip: str, container: str) -> bool:
    """True if tip is an ancestor of container (resetting container keeps tip reachable)."""
    rc, _ = _main._git(repo_path, "merge-base", "--is-ancestor", tip, container)
    return rc == 0


def _rescue_branch_name(repo_path: str, issue: Optional[int]) -> Optional[str]:
    """Allocate an unused rescue branch name.

    Postconditions:
        - Returns `khala/rescue/issue-<issue>-<ts>` (issue known) or
          `khala/rescue/<ts>`, suffixed `-1`..`-9` on collision; None when
          all ten candidates exist.
    """
    tag = f"issue-{issue}-" if issue is not None else ""
    base = f"{RESCUE_BRANCH_PREFIX}{tag}{_main._utc_timestamp()}"
    for cand in [base] + [f"{base}-{i}" for i in range(1, 10)]:
        rc, _ = _main._git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{cand}")
        if rc != 0:
            return cand
    return None


def _latest_issue_rescue_ref(repo_path: str, issue_number: int) -> Optional[str]:
    """Newest rescue ref for the issue (timestamps sort lexicographically)."""
    rc, out = _main._git(
        repo_path,
        "for-each-ref",
        "--sort=-refname",
        "--count=1",
        "--format=%(refname:short)",
        f"refs/heads/{RESCUE_BRANCH_PREFIX}issue-{issue_number}-*",
    )
    if rc != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0]


def _working_tree_dirty(repo_path: str) -> Tuple[bool, bool, Optional[str]]:
    """Inspect the working tree.

    Postconditions:
        - Returns (status_ok, dirty, listing). status_ok=False means
          `git status` itself failed (state unknowable — callers must fail
          closed, never attempt recovery); listing then carries the error.
        - When status_ok, listing is bounded porcelain output (or None when
          clean) so conflicting paths can be surfaced without dumping file
          contents.
    """
    rc, msg = _main._git(repo_path, "status", "--porcelain")
    if rc != 0:
        return False, True, msg or "git status failed"
    return True, bool(msg.strip()), msg if msg.strip() else None


def _recover_dirty_tree(
    repo_path: str, marker: Optional[int], issue_number: Optional[int], listing: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Commit or preserve a dirty working tree before branch prep.

    Same-issue work (marker == issue_number, HEAD on a real branch) is
    committed in place so it can seed continuation; anything else — foreign
    issue, unknown attribution, detached HEAD — is moved onto a rescue
    branch. Work is never deleted.

    Preconditions:
        - The working tree is dirty and `git status` succeeded (callers
          gate on _working_tree_dirty's status_ok).
    Postconditions:
        - On success (error is None) the working tree is clean and the prior
          dirty state is committed on the returned-or-noted branch; wip_tip
          names the continuation seed candidate when the work belongs to
          issue_number, else None; note is operator-facing.
        - On failure (error set) nothing has been deleted.
    """
    same_issue = marker is not None and issue_number is not None and marker == issue_number
    rc, head = _main._git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    head_branch = head.strip() if rc == 0 else "HEAD"
    on_branch = head_branch not in ("", "HEAD")

    if same_issue and on_branch:
        ok, msg = _main.commit_working_tree(
            repo_path,
            f"wip: recover uncommitted changes from interrupted run (issue {issue_number})",
        )
        if not ok:
            return None, None, msg
        note = f"♻️ Recovered uncommitted changes from an interrupted run (committed on `{head_branch}`)."
        return head_branch, note, None

    rescue = _main._rescue_branch_name(repo_path, marker)
    if rescue is None:
        return None, None, "could not allocate a rescue branch name"
    rc, msg = _main._git(repo_path, "checkout", "-b", rescue, "--")
    if rc != 0:
        return None, None, f"rescue branch creation failed: {msg}"
    was = f" (was on `{head_branch}`)" if on_branch else ""
    ok, msg = _main.commit_working_tree(
        repo_path,
        f"wip: rescue uncommitted changes from interrupted run{was}\n\n{listing}".rstrip(),
    )
    if not ok:
        return None, None, f"rescue commit failed: {msg}"
    wip_tip = rescue if same_issue else None
    note = f"♻️ Recovered uncommitted changes from an interrupted run; preserved on local branch `{rescue}`."
    return wip_tip, note, None


def _preserve_if_would_orphan(
    repo_path: str, branch: str, base_ref: str, seed: str, marker: Optional[int]
) -> Optional[str]:
    """Create a rescue ref for `branch` (any committish, including a
    remote-tracking ref) when adopting `seed` would strand its commits.

    Invariant served: no commits visible to branch prep may become
    unreachable — neither through prep's own `checkout -B` resets of local
    branches, nor through the job's eventual `--force-with-lease` push
    replacing a remote issue tip the chosen seed does not contain.

    Postconditions:
        - Returns None when nothing needed preserving or a rescue ref now
          holds the tip; returns an error string when preservation was
          needed but failed (callers must fail closed).
    """
    if branch == seed:
        return None
    if not _is_ahead(repo_path, branch, base_ref):
        return None
    if _reachable_from(repo_path, branch, seed):
        return None
    name = _main._rescue_branch_name(repo_path, marker)
    if name is None:
        return f"could not allocate a rescue branch to preserve `{branch}`"
    rc, msg = _main._git(repo_path, "branch", name, branch)
    if rc != 0:
        return f"failed to preserve `{branch}` before reset: {msg}"
    logger.warning("Preserved %s on %s before reset (ahead of %s)", branch, name, base_ref)
    return None


def _reconcile_remote_issue_ref(
    repo_path: str,
    remote: str,
    integration_branch: str,
    base_ref: str,
    remote_issue_ref: str,
    auth_env: Optional[Dict[str, str]],
    issue_fetch_msg: str,
) -> Optional[str]:
    """Reconcile a failed issue-branch fetch: confirm absence, then drop the stale tracking ref.

    ``fetch`` exit codes do not distinguish "no such remote ref" from a transient
    transport failure, and only confirmed absence may take the deletion path —
    dropping the tracking ref on a network blip would hide live remote progress
    from candidate selection and let the final force-with-lease push race against
    it. Probe absence explicitly with ``ls-remote --exit-code`` (2 = no matching
    head, 0 = present, anything else = transport failure).

    Preconditions:
        - Called only when the issue-branch fetch returned non-zero; the
          base-branch fetch already succeeded (so the remote is reachable).
    Postconditions:
        - Returns an error string on a transient failure, an unverifiable
          absence, or an undeletable stale ref (callers must fail closed).
        - Returns ``None`` when the remote branch is confirmed absent and its
          stale remote-tracking ref (if it was ahead) has been pinned via a
          rescue ref and then dropped, so it can no longer pose as live state.
    """
    rc_probe, probe_out = _main._git(
        repo_path,
        "ls-remote",
        "--exit-code",
        "--heads",
        "--",
        remote,
        integration_branch,
        env=auth_env,
    )
    if rc_probe == 0:
        return (
            f"could not fetch remote issue branch {integration_branch!r} "
            f"(it exists on the remote — transient failure?): {issue_fetch_msg}"
        )
    if rc_probe != 2:
        return (
            f"cannot verify whether remote issue branch {integration_branch!r} still "
            f"exists (fetch failed: {issue_fetch_msg}; probe failed: {probe_out})"
        )
    # The remote branch is absent (deleted/pruned — the probe confirmed it). A
    # stale remote-tracking ref from an earlier fetch would otherwise pose as
    # live remote state: candidate selection could seed from it and the final
    # force push would republish commits the remote deliberately no longer has.
    # Pin its tip first (never-lose-work invariant), then drop the tracking ref.
    # The rescue is deliberately UNTAGGED: a remote deletion is an explicit
    # signal not to continue this state, so it is preserved for manual recovery
    # without becoming an automatic continuation candidate (unlike preserved
    # local divergence, which the system itself was still carrying).
    if _is_ahead(repo_path, remote_issue_ref, base_ref):
        preserve_err = _main._preserve_if_would_orphan(
            repo_path, remote_issue_ref, base_ref, base_ref, None
        )
        if preserve_err:
            return preserve_err
    _main._git(repo_path, "update-ref", "-d", f"refs/remotes/{remote_issue_ref}")
    # Postcondition, not return-code, check: deletion legitimately fails when the
    # ref never existed (fresh issue), but a ref that SURVIVES (lock, permissions,
    # concurrent git op) would re-enter candidate selection and re-anchor the
    # remote floor to a state the remote deliberately deleted — fail closed.
    rc_gone, _ = _main._git(
        repo_path, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_issue_ref}"
    )
    if rc_gone == 0:
        return (
            f"could not drop stale remote-tracking ref {remote_issue_ref!r}; "
            f"refusing to continue while it can pose as live remote state"
        )
    return None


def _select_seed(
    repo_path: str,
    marker: Optional[int],
    issue_number: Optional[int],
    wip_tip: Optional[str],
    integration_branch: str,
    base_ref: str,
    remote_issue_ref: str,
) -> str:
    """Choose the integration-branch seed from the best eligible prior-progress tip.

    Builds a graph-ordered candidate list (same-issue continuation tips first,
    then local-vs-remote issue tip, then the latest issue rescue ref) and returns
    the first candidate that is ahead of ``base_ref`` and — when the remote issue
    branch is live and ahead (the "remote floor") — already contains it, so the
    eventual ``--force-with-lease`` push cannot silently drop remote-only commits.

    Preconditions:
        - ``base_ref`` and ``remote_issue_ref`` are resolvable refs; the remote
          issue tip has already been reconciled (fetched or its stale ref dropped).
    Postconditions:
        - Returns the chosen seed ref, or ``base_ref`` when no candidate is
          eligible (a fresh issue with no prior progress).
    """
    candidates: List[str] = []
    if marker is not None and issue_number is not None and marker == issue_number:
        # Same-issue continuation: the interrupted run's progress may live on
        # BOTH the wip tip (wherever HEAD was at crash time) and development
        # (merged task work). Order graph-aware — a tip containing the other
        # goes first; diverged tips put development first (the canonical
        # integration line; the wip branch is never reset, and a diverged
        # integration-branch wip is pinned by the orphan-prevention pass).
        if wip_tip and wip_tip != DEVELOPMENT_BRANCH:
            if _reachable_from(repo_path, DEVELOPMENT_BRANCH, wip_tip):
                candidates.extend((wip_tip, DEVELOPMENT_BRANCH))
            else:
                candidates.extend((DEVELOPMENT_BRANCH, wip_tip))
        else:
            candidates.append(DEVELOPMENT_BRANCH)
    # Local-vs-remote issue tip: prefer local only when it already contains
    # the remote tip. The eventual publish is `push --force-with-lease` and
    # the caller's own fetch refreshes the lease, so seeding from a tip
    # that lacks remote-only commits would let the push silently drop them.
    # A diverged local tip is pinned by the orphan-prevention pass below.
    if _reachable_from(repo_path, remote_issue_ref, integration_branch):
        candidates.extend((integration_branch, remote_issue_ref))
    else:
        candidates.extend((remote_issue_ref, integration_branch))
    if issue_number is not None:
        rescue_ref = _latest_issue_rescue_ref(repo_path, issue_number)
        if rescue_ref:
            candidates.append(rescue_ref)
    # Remote floor: when the remote issue branch is live and ahead, no
    # candidate that lacks its commits may seed — the force-with-lease push
    # (lease refreshed by the caller's own fetch) would silently drop the
    # remote-only commits from the published PR. Locally-pinned rescue refs
    # are no substitute for commits the remote is expected to keep.
    remote_floor = _is_ahead(repo_path, remote_issue_ref, base_ref)

    def _eligible(candidate: str) -> bool:
        if not _is_ahead(repo_path, candidate, base_ref):
            return False
        if not remote_floor or candidate == remote_issue_ref:
            return True
        return _reachable_from(repo_path, remote_issue_ref, candidate)

    return next((c for c in candidates if _eligible(c)), base_ref)


def _merge_recovered_wip(
    repo_path: str, wip_tip: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Merge diverged recovered work-in-progress into the continuation line, if needed.

    Recovery may have reported same-issue WIP that lives on a branch which
    diverged from the chosen seed; recovery called it continuation state, so it
    must reach the resumed line rather than being left on a side branch the
    orchestrator never reads. On conflict the merge is aborted and reported
    rather than guessing a resolution — the WIP branch itself is never reset, so
    nothing is lost either way.

    Preconditions:
        - Called after the seed has been checked out onto ``DEVELOPMENT_BRANCH``.
    Postconditions:
        - Returns ``(note, None)`` describing the merge/leave-unmerged outcome
          (``note`` may be ``None`` when no merge was needed), or ``(None, err)``
          when a failed merge could not be cleanly aborted (caller fails closed).
    """
    if not (
        wip_tip
        and _is_safe_ref(wip_tip)
        and not _reachable_from(repo_path, wip_tip, DEVELOPMENT_BRANCH)
    ):
        return None, None
    rc, msg = _main._git(repo_path, "merge", "--no-edit", wip_tip, env=git_identity_env())
    if rc == 0:
        return (
            f"🔀 Merged recovered work-in-progress from `{wip_tip}` into the continuation line.",
            None,
        )
    _main._git(repo_path, "merge", "--abort")
    status_ok, still_dirty, _ = _main._working_tree_dirty(repo_path)
    if not status_ok or still_dirty:
        return (
            None,
            f"merge of recovered work-in-progress `{wip_tip}` failed and could not "
            f"be cleanly aborted: {msg}",
        )
    return (
        f"⚠️ Recovered work-in-progress on `{wip_tip}` conflicts with the continuation "
        f"line; left unmerged on that branch for manual integration.",
        None,
    )


def _prepare_issue_branch(
    repo_path: str,
    remote: str,
    default_branch: str,
    integration_branch: str,
    token: Optional[str] = None,
    issue_number: Optional[int] = None,
) -> Tuple[bool, Optional[str], List[str]]:
    """Prepare development + integration branches, recovering interrupted state.

    Dirty trees are recovered (same-issue work committed in place, foreign
    work preserved on khala/rescue/* branches), the integration branch is
    seeded from the best prior-progress tip so a new job picks up where the
    previous one left off, and no reset may orphan commits.

    Preconditions:
        - repo_path is a git checkout; ref arguments may be untrusted.
    Postconditions (success):
        - integration_branch is checked out with a clean working tree;
          khala.active-issue records issue_number when provided; every commit
          reachable from a local branch on entry is still reachable from some
          local or remote ref; the returned notes describe recovery and
          continuation actions for operator-facing reporting.
    Postconditions (failure):
        - No uncommitted work has been deleted and no commit that was
          reachable on entry has become unreachable.
    """
    notes: List[str] = []

    # Defense-in-depth: reject ref names that could be parsed as git options.
    # This must precede dirty-tree recovery — a request that can never
    # proceed must not commit WIP, create rescue branches, or switch the
    # checkout on its way to being rejected.
    if not _is_safe_ref(default_branch):
        return False, f"unsafe default_branch ref: {default_branch!r}", notes
    if not _is_safe_ref(integration_branch):
        return False, f"unsafe integration_branch ref: {integration_branch!r}", notes

    marker = _read_active_issue(repo_path)

    status_ok, dirty, listing = _main._working_tree_dirty(repo_path)
    if not status_ok:
        return False, f"cannot inspect working tree: {listing}", notes
    wip_tip: Optional[str] = None
    if dirty:
        wip_tip, note, recover_err = _recover_dirty_tree(
            repo_path, marker, issue_number, listing or ""
        )
        if recover_err:
            return (
                False,
                "working tree has uncommitted changes; clean it before retrying:\n"
                f"{listing}\n(automatic recovery failed: {recover_err})",
                notes,
            )
        if note:
            notes.append(note)

    # The marker is NOT cleared here even after recovery: it also drives
    # same-issue continuation (development as a seed candidate), and the
    # development-ahead commits it attributes remain on the checkout until
    # the re-seed below succeeds. The only safe transition is the success
    # path's _write_active_issue overwrite; every failure exit retains it
    # so a retry can still attribute and continue the prior work.

    # `fetch` is the only network op here (the checkouts below are local), so it
    # needs the credential. The clone was authenticated transiently by the
    # unified API; that auth is not persisted, so we re-supply it per fetch.
    auth_env = _git_auth_env(token) if token else None
    rc, msg = _main._git(repo_path, "fetch", "--", remote, default_branch, env=auth_env)
    if rc != 0:
        return False, msg, notes
    # The issue branch may exist remotely from a previous job that pushed
    # before dying; fetch it as a continuation candidate (absence is fine).
    base_ref = f"{remote}/{default_branch}"
    remote_issue_ref = f"{remote}/{integration_branch}"
    rc_issue_fetch, issue_fetch_msg = _main._git(
        repo_path, "fetch", "--", remote, integration_branch, env=auth_env
    )
    if rc_issue_fetch != 0:
        reconcile_err = _reconcile_remote_issue_ref(
            repo_path,
            remote,
            integration_branch,
            base_ref,
            remote_issue_ref,
            auth_env,
            issue_fetch_msg,
        )
        if reconcile_err:
            return False, reconcile_err, notes

    seed = _select_seed(
        repo_path,
        marker,
        issue_number,
        wip_tip,
        integration_branch,
        base_ref,
        remote_issue_ref,
    )

    if seed != base_ref:
        rc, count = _main._git(repo_path, "rev-list", "--count", f"{base_ref}..{seed}")
        ahead = count.strip() if rc == 0 else "?"
        notes.append(
            f"▶️ Continuing issue from previous progress: `{seed}` ({ahead} commits ahead of `{default_branch}`)."
        )

    # Invariant: no commits visible to prep — on local branches about to be
    # reset, or on the just-fetched remote issue tip that the final
    # --force-with-lease push would replace — may become unreachable.
    # Rescue-tag attribution is per ref: work on the issue branch (local or
    # remote tip) belongs to the issue being prepared by construction, so its
    # rescue ref is issue-tagged for _latest_issue_rescue_ref continuation;
    # development work is only attributable through the marker.
    for ref, owner_issue in (
        (DEVELOPMENT_BRANCH, marker),
        (integration_branch, issue_number),
        (remote_issue_ref, issue_number),
    ):
        preserve_err = _main._preserve_if_would_orphan(repo_path, ref, base_ref, seed, owner_issue)
        if preserve_err:
            return False, preserve_err, notes

    rc, msg = _main._git(repo_path, "checkout", "-B", DEVELOPMENT_BRANCH, seed, "--")
    if rc != 0:
        return False, msg, notes

    merge_note, merge_err = _merge_recovered_wip(repo_path, wip_tip)
    if merge_err:
        return False, merge_err, notes
    if merge_note:
        notes.append(merge_note)

    rc, msg = _main._git(repo_path, "checkout", "-B", integration_branch, "--")
    if rc != 0:
        return False, msg, notes
    if issue_number is not None:
        _write_active_issue(repo_path, issue_number)
    return True, None, notes


def _fast_forward(repo_path: str, branch: str, source_ref: str) -> Tuple[bool, Optional[str]]:
    """Force-move ``branch`` to point at ``source_ref`` in ``repo_path``.

    Implemented with ``git branch -f``, which git REFUSES when ``branch`` is
    the attached HEAD of any working tree of the repo. ``_prepare_issue_branch``
    leaves the per-issue checkout with ``branch`` (the integration branch)
    checked out via ``checkout -B``, so a naive ``git branch -f`` here fails
    with "cannot force update the branch '<branch>' used by worktree at ...".

    To avoid that, when ``branch`` is the checkout's current HEAD we first
    detach HEAD onto its own commit (``git checkout --detach``), leaving the
    branch attached nowhere, then force-update it. The final state is identical
    to what the subsequent push expects (``branch`` points at ``source_ref``);
    the working tree is only ever left detached, never on a different branch.
    """
    if not _is_safe_ref(branch) or not _is_safe_ref(source_ref):
        return False, f"unsafe ref: {branch!r} <- {source_ref!r}"

    # If the checkout currently has `branch` attached as HEAD, git will refuse
    # to force-update it. Detach HEAD first so the branch is no longer "used by
    # a worktree", making the force-update legal.
    rc_head, head = _main._git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if rc_head == 0 and head.strip() == branch:
        rc_detach, detach_msg = _main._git(repo_path, "checkout", "--detach")
        if rc_detach != 0:
            return False, f"could not detach HEAD before updating {branch!r}: {detach_msg}"

    rc, msg = _main._git(repo_path, "branch", "-f", "--", branch, source_ref)
    return (rc == 0), (None if rc == 0 else msg)


def _push_branch(
    repo_path: str, remote: str, branch: str, token: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    if not _is_safe_ref(branch):
        return False, f"unsafe branch name: {branch!r}"
    # Push is a network op against the (HTTPS) origin; supply the transient
    # credential so the PR branch actually lands instead of hanging on an auth
    # prompt until the timeout (GIT_TERMINAL_PROMPT=0 turns that into a fast
    # failure for public repos too).
    rc, msg = _main._git(
        repo_path,
        "push",
        "--force-with-lease",
        "-u",
        remote,
        branch,
        timeout=180,
        env=_git_auth_env(token) if token else None,
    )
    return (rc == 0), (None if rc == 0 else msg)
