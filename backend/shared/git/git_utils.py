"""
Git utilities for the software engineering team branching strategy.

The Tech Lead enforces: all development on a development branch;
create it from main if it does not exist.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

DEVELOPMENT_BRANCH = "development"
MAIN_BRANCH = "main"

DEFAULT_COMMIT_USER_NAME = "Khala"
DEFAULT_COMMIT_USER_EMAIL = "brandon.kindred@gmail.com"


def _configured_commit_identity() -> Tuple[str, str]:
    """Resolve the configured platform commit identity.

    Postconditions:
        - Returns (name, email), both non-empty; blank or unset
          GIT_COMMIT_USER_NAME / GIT_COMMIT_USER_EMAIL fall back to the
          platform defaults.
    """
    name = (os.environ.get("GIT_COMMIT_USER_NAME") or "").strip() or DEFAULT_COMMIT_USER_NAME
    email = (os.environ.get("GIT_COMMIT_USER_EMAIL") or "").strip() or DEFAULT_COMMIT_USER_EMAIL
    return name, email


def git_identity_env() -> Dict[str, str]:
    """Process environment for git subprocesses with a complete commit identity.

    GitHub-cloned checkouts have no repo-local user.name/user.email and the
    agent containers set no global git config, so a bare `git commit` fails
    with "Author identity unknown". Filling git's native identity variables
    here makes identity ambient for every command routed through _run_git
    without persisting anything into the checkout.

    Preconditions: none.
    Postconditions:
        - Returned dict contains all parent environment entries.
        - GIT_AUTHOR_NAME/EMAIL and GIT_COMMITTER_NAME/EMAIL are present and
          non-empty; non-blank values exported by the operator are unchanged,
          while blank exports are replaced (git rejects empty idents with
          "fatal: empty ident name ... not allowed").
    """
    name, email = _configured_commit_identity()
    env = dict(os.environ)
    for key, value in (
        ("GIT_AUTHOR_NAME", name),
        ("GIT_AUTHOR_EMAIL", email),
        ("GIT_COMMITTER_NAME", name),
        ("GIT_COMMITTER_EMAIL", email),
    ):
        if not (env.get(key) or "").strip():
            env[key] = value
    return env


def _run_git(
    repo_path: Path, cmd: list[str], timeout: int = 30, *, merge_stderr: bool = True
) -> Tuple[int, str]:
    """Run git command in repo. Returns (returncode, output).

    Parameters
    ----------
    merge_stderr:
        When True (default) the returned text is ``stdout + stderr`` — convenient
        for surfacing error detail in failure messages. When False, only
        ``stdout`` is returned *on success*, so a command whose stdout is *data*
        (e.g. ``git show <rev>:<path>`` reading a file blob) is not polluted by
        warnings git may emit to stderr while still exiting 0 (CRLF/filter-driver
        advice). On a non-zero exit there is no valid stdout *data* to protect —
        the caller either raises with this text or discards it — so stderr is
        appended regardless of *merge_stderr*, keeping the failure cause
        (``fatal: ...``) out of an otherwise-empty diagnostic.

    Postconditions:
        - The spawned git process observes a complete author/committer
          identity (see git_identity_env).
        - Output is decoded with ``surrogateescape`` so a path containing bytes
          invalid in the locale encoding round-trips instead of raising and
          collapsing the whole command's output.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=timeout,
            env=git_identity_env(),
        )
        out = result.stdout or ""
        # Merge stderr when asked, or unconditionally on failure: a non-zero exit
        # carries its cause on stderr and has no stdout data worth keeping clean.
        if merge_stderr or result.returncode != 0:
            out += result.stderr or ""
        return result.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "Command timed out"
    except Exception as e:
        return -1, str(e)


def create_feature_branch(
    repo_path: str | Path, base_branch: str, feature_name: str
) -> Tuple[bool, str]:
    """
    Create and checkout a feature branch from base_branch.
    feature_name: e.g. "t3-backend-auth" (will become feature/t3-backend-auth).

    If the working tree has uncommitted changes, they are committed on the current
    branch first so checkout can succeed (avoids "would be overwritten by checkout").
    If the branch already exists (e.g. from a previous run), it is deleted
    and recreated from the base branch so the task gets a clean start.

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    branch_name = (
        f"feature/{feature_name}" if not feature_name.startswith("feature/") else feature_name
    )

    # Ensure working tree is clean so checkout does not fail with "would be overwritten"
    status_code, status_out = _run_git(path, ["git", "status", "--porcelain"])
    if status_code == 0 and status_out.strip():
        _run_git(path, ["git", "add", "-A"])
        commit_code, commit_out = _run_git(
            path, ["git", "commit", "-m", "chore: save working tree before feature branch"]
        )
        if commit_code != 0 and "nothing to commit" not in (commit_out or ""):
            logger.warning("Could not commit before feature branch: %s", commit_out)
        else:
            logger.info("Committed uncommitted changes before creating feature branch")

    code, out = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
    if code != 0:
        if "would be overwritten" in out or "Your local changes" in out:
            # First try removing disposable files (e.g. test.db) that block checkout
            if _clear_disposable_files_if_blocking(path, out):
                code, out = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
                if code == 0:
                    logger.info(
                        "Created branch '%s' from '%s' (disposable files cleared)",
                        branch_name,
                        base_branch,
                    )
                    return True, branch_name
            # Working tree still dirty — try stash
            logger.info(
                "Checkout failed due to local changes. Next step -> Trying stash to preserve changes"
            )
            stash_code, stash_out = _run_git(
                path, ["git", "stash", "push", "-u", "-m", "pre-feature-branch"]
            )
            if stash_code == 0:
                code, out = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
                if code == 0:
                    logger.info(
                        "Created branch '%s' from '%s' (changes stashed)", branch_name, base_branch
                    )
                    return True, branch_name
            return False, f"Failed to create branch {branch_name}: {out}"
        if "already exists" in out:
            # If path is already sitting on branch_name, reuse it rather than delete+recreate:
            # this is the common retry-after-a-transient-failure case (the branch was created
            # here on a prior attempt against this same path and never left), and the
            # delete+recreate path below would fail here regardless — it checks out base_branch
            # first, which git refuses when base_branch is attached in another linked worktree
            # (e.g. the swarm's shared checkout, which stays on base_branch for merge/diff), and
            # git also refuses to delete a branch that is currently checked out in THIS path.
            current_code, current_out = _run_git(path, ["git", "branch", "--show-current"])
            if current_code == 0 and current_out.strip() == branch_name:
                logger.info("Branch '%s' already checked out at %s; reusing it", branch_name, path)
                return True, branch_name
            # Stale branch from elsewhere (not checked out here) — delete and recreate. No
            # intermediate `git checkout base_branch` first: deleting a branch only requires
            # that it not be attached in ANY worktree (this one included, already ruled out
            # above) — it does not require this worktree to be on any particular branch first.
            # Attaching base_branch here would itself fail whenever base_branch (development) is
            # already attached in another linked worktree (the swarm's shared checkout).
            logger.warning(
                "Branch '%s' already exists, deleting and recreating from '%s'",
                branch_name,
                base_branch,
            )
            del_code, del_out = _run_git(path, ["git", "branch", "-D", branch_name])
            if del_code != 0:
                # Most commonly: branch_name is attached in a DIFFERENT worktree right now (e.g.
                # another worker still owns it) — genuinely not recoverable from here without
                # detaching it there first, so fail honestly rather than silently succeed.
                return False, f"Failed to delete stale branch {branch_name}: {del_out}"
            code2, out2 = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
            if code2 != 0:
                return False, f"Failed to recreate branch {branch_name}: {out2}"
        else:
            return False, f"Failed to create branch {branch_name}: {out}"
    logger.info("Created branch '%s' from '%s'", branch_name, base_branch)
    return True, branch_name


def checkout_branch(repo_path: str | Path, branch: str) -> Tuple[bool, str]:
    """Checkout the given branch. Returns (success, message)."""
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "checkout", branch])
    if code != 0:
        if _clear_disposable_files_if_blocking(path, out):
            code, out = _run_git(path, ["git", "checkout", branch])
        if code != 0:
            return False, f"Failed to checkout {branch}: {out}"
    return True, f"Checked out {branch}"


class UnsafeRepoPathError(ValueError):
    """Raised when a relative file path would escape (or equal) the repo root.

    Subclasses ``ValueError`` for backward compatibility; the dedicated type lets
    callers catch *only* an unsafe-path rejection (traversal or empty key) and
    convert it into a handled failure, without masking unrelated ``ValueError``s.
    """


def resolve_safe_repo_path(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` under ``root``, rejecting empty/escaping paths.

    The single containment gate shared by :func:`write_files_and_commit` and
    :func:`software_engineering_team.shared.repo_writer.write_repo_text_files`,
    so the traversal guard is applied uniformly on every write path rather than
    on one and missing on the other.

    Preconditions:
        ``root`` is an already-resolved repo-root directory.
    Postconditions:
        Returns the absolute path to write for ``rel_path``. Raises
        ``UnsafeRepoPathError`` when ``rel_path`` is empty, resolves to ``root``
        itself, or lies outside ``root``. Containment is decided lexically
        (``os.path.normpath``), so a symlinked component of ``rel_path`` is not
        followed — only ``..``/leading-``/`` escapes are normalized away.
    """
    safe_rel_path = rel_path.lstrip("/")
    if not safe_rel_path:
        raise UnsafeRepoPathError(f"File path must not be empty: {rel_path!r}")
    full_path = Path(os.path.normpath(root / safe_rel_path))
    # Containment via ``parents`` avoids the ``str.startswith`` sibling-prefix
    # pitfall (e.g. ``/repo`` vs ``/repo-evil``); the ``== root`` check rejects a
    # key that resolves to the repo directory itself (e.g. ``"."`` / ``"a/.."``).
    if full_path == root or root not in full_path.parents:
        raise UnsafeRepoPathError(f"Path traversal detected: {rel_path}")
    return full_path


def write_files_and_commit(
    repo_path: str | Path,
    files_dict: Dict[str, str],
    message: str,
) -> Tuple[bool, str]:
    """
    Write files to repo, git add, and commit on the current branch.
    files_dict: { "path/relative/to/repo": "content" }

    Returns (success, message). An unsafe path (empty, repo-root, or traversal)
    is reported as ``(False, ...)`` rather than raised, so callers that unpack
    ``(success, message)`` run their normal write-failure/cleanup path instead
    of aborting on an exception. Validation happens before any file is written,
    so a rejected batch leaves nothing partially written.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    try:
        resolved = [
            (resolve_safe_repo_path(path, file_path), content)
            for file_path, content in files_dict.items()
        ]
    except UnsafeRepoPathError as exc:
        return False, f"Unsafe file path rejected: {exc}"
    for full_path, content in resolved:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
    code, out = _run_git(path, ["git", "add", "-A"])
    if code != 0:
        return False, f"git add failed: {out}"
    code, out = _run_git(path, ["git", "status", "--porcelain"])
    if code != 0:
        return False, f"git status failed: {out}"
    if not out.strip():
        logger.info("No changes to commit (files unchanged)")
        return True, "No changes to commit"
    code, out = _run_git(path, ["git", "commit", "-m", message])
    if code != 0:
        return False, f"git commit failed: {out}"
    logger.info("Committed: %s", message[:50])
    return True, "Committed"


def commit_working_tree(repo_path: str | Path, message: str) -> Tuple[bool, str]:
    """
    Commit the current working tree (git add -A, git commit -m message).
    Does not write new files; use write_files_and_commit for that.
    Returns (success, message). Treats "nothing to commit" as success.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "add", "-A"])
    if code != 0:
        return False, f"git add failed: {out}"
    code, out = _run_git(path, ["git", "status", "--porcelain"])
    if code != 0:
        return False, f"git status failed: {out}"
    if not out.strip():
        logger.info("No changes to commit (working tree clean)")
        return True, "No changes to commit"
    code, out = _run_git(path, ["git", "commit", "-m", message])
    if code != 0:
        return False, f"git commit failed: {out}"
    logger.info("Committed: %s", message[:50])
    return True, "Committed"


def reset_hard_to(repo_path: str | Path, ref: str) -> Tuple[bool, str]:
    """Hard-reset the CURRENTLY checked-out branch to ``ref``'s commit.

    Unlike checking out ``ref`` directly, this works from inside a linked git
    worktree even when ``ref`` (e.g. ``development``) is already attached in a
    different worktree -- ``git reset --hard <ref>`` only reads ``ref``'s
    commit, it never attaches it here.

    Preconditions:
        - ``repo_path`` is an existing git repository; ``ref`` resolves to a
          commit (a local branch name, tag, or SHA).
    Postconditions:
        - On success, the current branch's tip and working tree exactly match
          ``ref``'s commit (any commits/changes unique to the current branch
          are discarded) and returns ``(True, message)``.
        - On failure (not a repo, or ``ref`` does not resolve) returns
          ``(False, message)`` and leaves the repository state unchanged.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "reset", "--hard", ref])
    if code != 0:
        return False, f"Failed to reset to {ref}: {out}"
    logger.info("Reset current branch to '%s'", ref)
    return True, f"Reset to {ref}"


def get_head_sha(repo_path: str | Path) -> Tuple[bool, str]:
    """Return the full SHA of the current HEAD commit.

    Preconditions:
        - ``repo_path`` refers to a git repository with at least one commit.
    Postconditions:
        - On success returns ``(True, <full commit sha>)`` — the commit the
          repository's HEAD currently resolves to, exactly as emitted by
          ``git rev-parse HEAD`` (40 hex chars for SHA-1 repos, 64 for SHA-256).
        - On failure (not a repo, or ``rev-parse`` errors) returns
          ``(False, message)`` and never a partial/garbage SHA. ``merge_stderr``
          is disabled so a stderr advisory on success cannot pollute the SHA.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "rev-parse", "HEAD"], merge_stderr=False)
    if code != 0:
        return False, f"rev-parse failed: {out}"
    return True, out.strip()


def commit_paths(repo_path: str | Path, paths: List[str], message: str) -> Tuple[bool, str]:
    """
    Stage and commit ONLY the given repo-relative paths.

    Unlike :func:`commit_working_tree` (which stages everything via
    ``git add -A``), this scopes both the stage and the commit to ``paths`` so
    unrelated working-tree changes are never swept into the commit. The commit
    itself is pathspec-limited (``git commit -- <paths>``), so any other staged
    index content is left untouched too. Newly created (untracked) paths are
    picked up because they are staged first.

    Preconditions:
        - ``repo_path`` is a git repository; ``paths`` are repo-relative.

    Postconditions:
        - Only changes under ``paths`` are committed; all other working-tree and
          index changes remain uncommitted. Treats "nothing to commit" (the named
          paths have no pending changes) as success.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    cleaned = [str(p).strip() for p in (paths or []) if str(p).strip()]
    if not cleaned:
        return True, "No paths to commit"
    code, out = _run_git(path, ["git", "add", "--", *cleaned])
    if code != 0:
        return False, f"git add failed: {out}"
    code, out = _run_git(path, ["git", "diff", "--cached", "--name-only", "--", *cleaned])
    if code != 0:
        return False, f"git diff failed: {out}"
    if not out.strip():
        logger.info("No changes to commit for given paths")
        return True, "No changes to commit"
    code, out = _run_git(path, ["git", "commit", "-m", message, "--", *cleaned])
    if code != 0:
        return False, f"git commit failed: {out}"
    logger.info("Committed: %s", message[:50])
    return True, "Committed"


def branch_has_commits_ahead_of(repo_path: str | Path, branch: str, base: str) -> bool:
    """
    Return True if branch has commits not in base.
    Used to check if there is work to merge before attempting emergency merge.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False
    code, out = _run_git(path, ["git", "log", "--oneline", f"{base}..{branch}"])
    return code == 0 and bool((out or "").strip())


def branch_diff(repo_path: str | Path, base: str, branch: str) -> str:
    """
    Return the full ``git diff base...branch`` (changes on branch since it diverged from base).

    Preconditions:
        - base and branch are branch names; the caller wants the feature branch's own changes.
    Postconditions:
        - Returns the complete, untruncated diff text, or "" when the path is not a git repo
          or the diff command fails (so callers can treat "no evidence" uniformly).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return ""
    code, out = _run_git(path, ["git", "diff", f"{base}...{branch}"])
    if code != 0:
        return ""
    return out or ""


# Upper bound on a single deleted blob read inline for review. A removed file
# larger than this is skipped (and surfaced by name in the deletion note) so a
# huge binary deletion can never be loaded whole into memory and OOM the worker.
_MAX_DELETED_BLOB_BYTES = 2_000_000

# Upper bound on how many deleted blobs are fetched for pre-deletion content.
# Each fetch spawns two git subprocesses (cat-file -s + show), so a mass
# deletion/rename (every rename decomposes to delete-old + add-new under
# ``--no-renames``) could otherwise fan out into thousands of process spawns on
# the per-iteration review path. Beyond this cap the removals are still listed by
# name in the deletion note (mirroring find_referencing_paths' scan cap); only
# their inline pre-deletion content is omitted.
_MAX_DELETED_BLOBS_READ = 50


class BaselineDiffUnavailable(RuntimeError):
    """The net base→worktree diff could not be computed.

    Raised when the merge base of *base* and *head* cannot be found (missing base
    ref, shallow clone) or the diff command fails/times out, so callers must fail
    closed — review the whole repository rather than a partial change set that
    silently omits the committed task changes.
    """


def _unambiguous_merge_base(path: Path, base: str, head: str) -> str:
    """Return the single merge base of *base*/*head*, or fail closed.

    Postconditions:
        - Returns the lone merge-base commit SHA.
        - Raises :class:`BaselineDiffUnavailable` when no merge base exists
          (missing ref, shallow clone) or when more than one exists
          (criss-cross/octopus history) — diffing against an arbitrary one of
          several would omit changes versus the others.
    """
    # stdout only — a git stderr advisory (e.g. replace-ref/grafts notes) must not
    # be counted as an extra merge-base SHA, which would spuriously trip the >1
    # "ambiguous" guard and force the degraded whole-repo + manual-review path.
    mb_code, mb_out = _run_git(path, ["git", "merge-base", "--all", base, head], merge_stderr=False)
    mb_lines = [ln.strip() for ln in (mb_out or "").splitlines() if ln.strip()]
    if mb_code != 0 or not mb_lines:
        raise BaselineDiffUnavailable(f"cannot compute merge base of {base}...{head}: {mb_out!r}")
    if len(mb_lines) > 1:
        # ``--all`` lists every merge base; >1 means criss-cross/octopus history.
        raise BaselineDiffUnavailable(
            f"ambiguous merge base of {base}...{head} ({len(mb_lines)} bases)"
        )
    return mb_lines[0]


def read_paths_at_merge_base(
    repo_path: str | Path, base: str, paths: List[str], head: str = "HEAD"
) -> Dict[str, str]:
    """Read the *pre-change* content of *paths* at the ``base``/``head`` merge base.

    Used to show the reviewer what a deletion removed: the deleted content is gone
    from the worktree, so it is fetched from the merge-base blob via
    ``git show <merge-base>:<path>`` (the same baseline ``list_changed_and_deleted``
    diffs against, so the two views are consistent).

    Preconditions:
        - *paths* are repo-relative and already filtered (e.g. sensitive paths
          dropped by the caller) — this reader does no denylisting of its own.
    Postconditions:
        - Returns ``{path: content}`` for each path whose merge-base blob is
          readable text *and* within :data:`_MAX_DELETED_BLOB_BYTES`. A path that
          did not exist at the base, that resolves to a tree (a directory/submodule
          has no blob), that exceeds the size cap, or whose blob is binary (a NUL
          byte) is omitted rather than returned as gibberish. The size is checked
          with ``git cat-file -s`` *before* the blob is read, so an arbitrarily
          large removed binary is never loaded whole (mirroring the worktree
          reader's bounded sniff and avoiding an OOM). At most
          :data:`_MAX_DELETED_BLOBS_READ` paths are fetched (two git spawns each),
          so a mass deletion cannot fan out into thousands of subprocess spawns;
          the caller surfaces the remainder by name in the deletion note.
        - Content is read with ``merge_stderr=False`` so a git stderr warning
          (CRLF/filter advice emitted while still exiting 0) is not spliced into
          the blob; it is then made UTF-8/JSON safe by re-deriving the
          surrogateescaped bytes and decoding with ``errors="replace"``.
        - Returns ``{}`` when not a git repository.
        - Raises :class:`BaselineDiffUnavailable` when the merge base cannot be
          computed, mirroring :func:`list_changed_and_deleted` so the caller
          treats an unavailable baseline uniformly.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return {}
    merge_base = _unambiguous_merge_base(path, base, head)
    result: Dict[str, str] = {}
    # Bound the number of blobs fetched: each costs two git subprocess spawns, so
    # a mass deletion is capped here regardless of caller. The rest are surfaced
    # by name in the caller's deletion note, not as content.
    for rel in list(paths)[:_MAX_DELETED_BLOBS_READ]:
        # The ``<rev>:<path>`` object syntax names the blob directly, so the path
        # is never parsed as a revision/flag (no ``--`` separator needed). Check
        # the blob size first so a huge removed binary is never read into memory.
        # stdout only — a git stderr warning (CRLF/filter advice emitted while
        # still exiting 0) must not contaminate the numeric size and break int().
        sz_code, sz_out = _run_git(
            path, ["git", "cat-file", "-s", f"{merge_base}:{rel}"], merge_stderr=False
        )
        if sz_code != 0:
            continue  # path absent at base, or a tree (submodule/dir): no blob
        try:
            blob_size = int(sz_out.strip())
        except ValueError:
            continue
        if blob_size > _MAX_DELETED_BLOB_BYTES:
            continue  # too large to review inline; surfaced by name in the note
        # stdout only — a git stderr warning must not contaminate the blob.
        code, out = _run_git(path, ["git", "show", f"{merge_base}:{rel}"], merge_stderr=False)
        if code != 0:
            continue
        if "\x00" in out:
            continue  # binary blob: omit rather than review as gibberish
        # _run_git decoded with surrogateescape; re-derive the original bytes and
        # decode with replacement so an invalid-UTF-8 blob is UTF-8/JSON safe.
        result[rel] = out.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return result


def list_changed_and_deleted(
    repo_path: str | Path, base: str, head: str = "HEAD"
) -> Tuple[List[str], List[str]]:
    """Return ``(changed, deleted)`` repo-relative paths for the task's work.

    Computes the *net* state from the ``base``/``head`` merge base to the working
    tree in a single ``git diff --name-status --no-renames -z <merge-base>`` call
    (preceded by ``git merge-base``). Because it is the net base→worktree diff, a
    path added in a feature commit and then removed in the worktree is not
    reported at all (no net change), avoiding a spurious deletion.

    - *changed* — added/modified non-deleted tracked paths a caller can read.
      Untracked files are not included (git diff only reports tracked changes),
      so build/test leftovers are excluded; callers add task-owned new files via
      the writer's normalized output instead.
    - *deleted* — net-removed paths. ``--no-renames`` decomposes a rename into
      delete-old + add-new, so a renamed-away path appears here (its old import
      location) while the new path appears in *changed*.

    ``-z`` yields NUL-delimited, unquoted paths so names with non-ASCII bytes,
    tabs, or newlines round-trip as real filesystem paths.

    Postconditions:
        - Returns ``([], [])`` when not a git repository (caller falls back).
        - Raises :class:`BaselineDiffUnavailable` when the merge base or diff
          cannot be computed, so the caller fails closed instead of reviewing a
          partial change set.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return [], []
    merge_base = _unambiguous_merge_base(path, base, head)
    # stdout only — the NUL-delimited name-status pairs are data; a git stderr
    # warning emitted on success would otherwise be appended and corrupt the
    # status/path pairing (the odd-field guard only catches a subset of that).
    code, out = _run_git(
        path,
        ["git", "diff", "--name-status", "--no-renames", "-z", merge_base],
        merge_stderr=False,
    )
    if code != 0:
        raise BaselineDiffUnavailable(f"net diff vs {merge_base} failed: {out!r}")
    changed: List[str] = []
    deleted: List[str] = []
    seen_changed: set[str] = set()
    seen_deleted: set[str] = set()
    fields = [f for f in (out or "").split("\0") if f]
    if len(fields) % 2 != 0:
        # ``--name-status -z`` emits alternating <status> <path> pairs; an odd
        # count means a trailing status with no path (truncated/unexpected
        # output). Drop the dangling field but log it rather than silently
        # mispairing every subsequent entry.
        logger.warning(
            "list_changed_and_deleted: odd field count (%d) from name-status -z; "
            "dropping trailing status",
            len(fields),
        )
        fields = fields[:-1]
    # ``--name-status -z`` emits alternating <status> and <path> fields.
    for status, entry in zip(fields[::2], fields[1::2]):
        if status.startswith("D"):
            bucket, seen = deleted, seen_deleted
        else:
            bucket, seen = changed, seen_changed
        if entry not in seen:
            seen.add(entry)
            bucket.append(entry)
    return changed, deleted


def merge_branch(repo_path: str | Path, source_branch: str, target_branch: str) -> Tuple[bool, str]:
    """
    Checkout target_branch and merge source_branch into it.
    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "checkout", target_branch])
    if code != 0:
        return False, f"Failed to checkout {target_branch}: {out}"
    code, out = _run_git(
        path, ["git", "merge", source_branch, "-m", f"Merge {source_branch} into {target_branch}"]
    )
    if code != 0:
        return False, f"Merge failed: {out}"
    return True, f"Merged {source_branch} into {target_branch}"


def abort_merge(repo_path: str | Path) -> Tuple[bool, str]:
    """Abort an in-progress merge. Returns (success, message)."""
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "merge", "--abort"])
    if code != 0:
        return False, f"Merge abort failed: {out}"
    return True, "Merge aborted"


def delete_branch(repo_path: str | Path, branch: str) -> Tuple[bool, str]:
    """Delete the branch (must not be checked out). Returns (success, message)."""
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "branch", "-d", branch])
    if code != 0:
        return False, f"Failed to delete branch {branch}: {out}"
    return True, f"Deleted branch {branch}"


def add_worktree(
    repo_path: str | Path, worktree_path: str | Path, ref: str = DEVELOPMENT_BRANCH
) -> Tuple[bool, str]:
    """Create a linked worktree at worktree_path, detached at ref's current commit.

    ``--detach`` checks out ref's commit without attaching HEAD to ref's branch
    name, so this never contends with ref already being attached (checked out)
    in repo_path or any other linked worktree of the same repository — git only
    refuses to *attach* HEAD to a branch name that is attached elsewhere;
    reading its tip commit as a start point (as this, and a caller's subsequent
    ``create_feature_branch``, do) is not restricted.

    Preconditions:
        - repo_path is an existing, non-bare git repository; ref names an
          existing branch or commit-ish reachable in repo_path.
    Postconditions:
        - On success: worktree_path is a linked worktree of repo_path with a
          detached HEAD at ref's current commit; returns (True, str(worktree_path)).
          A stale worktree_path left by a prior abnormal exit (directory and/or
          admin-area registration lingering from a crashed run) is pruned and
          one retry is attempted before failing, so this call is idempotent
          against crash residue.
        - On failure returns (False, message); repo_path's own HEAD and
          working tree are never modified.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    wt_path = Path(worktree_path).resolve()
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    code, out = _run_git(path, ["git", "worktree", "add", "--detach", str(wt_path), ref])
    if code == 0:
        return True, str(wt_path)

    # Stale registration/directory from a crashed prior run — clear any leftover
    # directory first, THEN prune (pruning before removing a directory that still
    # exists is a no-op, since git only prunes registrations whose directory is
    # already gone; the order here matters), then retry once before giving up.
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
    _run_git(path, ["git", "worktree", "prune"])
    code, out = _run_git(path, ["git", "worktree", "add", "--detach", str(wt_path), ref])
    if code != 0:
        return False, f"Failed to add worktree at {wt_path}: {out}"
    return True, str(wt_path)


def remove_worktree(
    repo_path: str | Path, worktree_path: str | Path, *, force: bool = True
) -> Tuple[bool, str]:
    """Remove a linked worktree and prune its administrative-area entry.

    Preconditions:
        - repo_path is any worktree (main or linked) of the repository whose
          ``.git/worktrees/`` area tracks worktree_path.
    Postconditions:
        - worktree_path no longer exists on disk and is no longer listed by
          ``git worktree list``; returns (True, message). A worktree_path that
          is already gone is treated as success (idempotent — safe to call
          from a best-effort cleanup path). ``force=True`` (default) removes
          even with uncommitted changes in the worktree (any real task work is
          already a commit on its feature branch; worktree-local scratch is
          disposable). Never raises: a failed ``git worktree remove`` falls
          back to a filesystem ``rmtree`` plus ``git worktree prune``.
    """
    path = Path(repo_path).resolve()
    wt_path = Path(worktree_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    if not wt_path.exists():
        _run_git(path, ["git", "worktree", "prune"])
        return True, f"{wt_path} already removed"
    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(wt_path))
    code, out = _run_git(path, cmd)
    if code == 0:
        return True, f"Removed worktree {wt_path}"
    # Fall back to a plain filesystem removal + prune so cleanup never blocks on git.
    shutil.rmtree(wt_path, ignore_errors=True)
    _run_git(path, ["git", "worktree", "prune"])
    if wt_path.exists():
        return False, f"Failed to remove worktree {wt_path}: {out}"
    return True, f"Removed worktree {wt_path} via filesystem fallback"


def prune_worktrees(repo_path: str | Path) -> Tuple[bool, str]:
    """Run ``git worktree prune`` to drop admin-area entries for missing worktrees.

    Postconditions:
        - Returns (True, message) on success or when repo_path is not a git
          repository (no-op); (False, message) only on an actual git failure.
          Never raises.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return True, "Not a git repository (no-op)"
    code, out = _run_git(path, ["git", "worktree", "prune"])
    if code != 0:
        return False, f"git worktree prune failed: {out}"
    return True, "Pruned stale worktree entries"


# Cap on referrers listed per deletion before an explicit truncation marker is
# appended (the count is always shown, so nothing is silently hidden).
_MAX_REFERRERS_LISTED = 25

# Above this many deletions, the per-deletion reverse-reference scan (one git grep
# each, over the whole worktree) is skipped to avoid an O(N × repo) stall on a
# mass deletion/rename. The deletions are still listed by name in the note.
_MAX_DELETIONS_SCANNED = 50


def _deleted_module_patterns(rel_path: str) -> List[str]:
    """Importer-search regexes for a deleted path (best-effort; never empty for a
    file with a stem).

    For a ``.py`` module the patterns are precise importer signals — far below a
    bare-stem search's false-positive rate — covering common layouts:

    - every multi-component dotted suffix of the path (``backend.app.services`` and
      ``app.services`` from ``backend/app/services.py``), so a module under *any*
      depth of source root (``src``/``lib``/``backend``/...) is matched without a
      hardcoded root list;
    - a bare ``import <stem>`` / ``from ... import <stem>``;
    - a *relative* import of the module (``from .helper import x`` /
      ``from ..helper import x``);
    - for a deleted package initializer (``pkg/__init__.py``), the package name
      itself (``import pkg`` / ``pkg.``), since removing it breaks ``import pkg``.

    For a non-Python deletion (Java/JS/TS/config/schema/...) there is no portable
    import grammar, so it falls back to *path-delimited* basename references: the
    full filename (``widget.ts``) and the stem when immediately preceded by a path
    separator, quote, or dot (``'widget'``, ``./widget``, ``com.x.Widget``). A bare
    word-boundary stem search is deliberately avoided — for a common basename
    (``index``, ``data``, ``main``, ``utils``) it would match nearly every file
    mentioning the word and flood the deletion note with false dependents.
    """
    p = Path(rel_path)
    stem = p.stem
    if not stem:
        return []

    if p.suffix == ".py":
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]  # the package this file initializes
        if not parts:
            return []
        name = parts[-1]
        # Every multi-component dotted suffix (>=2 parts) is precise enough to
        # search; the single-component case is covered by the import patterns.
        dotted_variants = {".".join(parts[i:]) for i in range(len(parts)) if len(parts) - i >= 2}
        dotted_variants.add(".".join(parts))
        patterns: List[str] = []
        for dotted in sorted(dotted_variants):
            patterns += ["-e", rf"\b{re.escape(dotted)}\b"]
        # Absolute and relative imports of the leaf module name.
        patterns += ["-e", rf"import\s+{re.escape(name)}\b"]
        patterns += ["-e", rf"from\s+\.+{re.escape(name)}\b"]
        return patterns

    # Non-Python: path-/quote-delimited basename references (no bare-word stem,
    # which is far too broad for common basenames). Matches:
    #   - the stem preceded by ``/``, a quote, or ``.`` — used like a path or a
    #     qualified name (``'widget'``, ``./widget``, ``com.x.Widget``);
    #   - the full filename (``widget.ts``);
    #   - a *keyword-anchored* bare stem (``import widget``, ``require widget``,
    #     ``use widget``) — the import keyword keeps this from matching arbitrary
    #     occurrences of a common word, recovering the bare ``import <name>`` form.
    patterns = ["-e", rf"['\"/.]{re.escape(stem)}"]
    if p.name != stem:
        patterns += ["-e", re.escape(p.name)]
    patterns += ["-e", rf"(import|require|include|use|from)\s+['\"]?{re.escape(stem)}\b"]
    return patterns


def find_referencing_paths(
    repo_path: str | Path,
    deleted_paths: List[str],
) -> Dict[str, List[str]]:
    """For each deleted path, the surviving worktree files that still mention it.

    Best-effort reverse-dependency signal so a reviewer can check the deletion
    note's instruction that "nothing still depends on" a removed file. For each
    deletion it ``git grep``s the *worktree* (tracked files, so the just-removed
    file itself is gone) for importer signals (see :func:`_deleted_module_patterns`):
    precise dotted/relative import forms for ``.py`` modules, and a by-name
    basename search for other languages (Java/JS/TS/config/...).

    Each deletion is one ``git grep`` over the tracked worktree, so the scan is
    bounded: a mass deletion/rename of more than :data:`_MAX_DELETIONS_SCANNED`
    paths would rescan the whole repository that many times and could stall code
    review for minutes, so it is skipped entirely (the deletion note still lists
    every removed path by name; only the per-deletion referrer sub-lines are
    omitted). This caps the worst case at a fixed number of greps.

    Preconditions:
        - *deleted_paths* are repo-relative paths reported deleted by
          :func:`list_changed_and_deleted`.
    Postconditions:
        - Returns ``{deleted_path: [referrer, ...]}`` only for deletions with at
          least one surviving referrer; referrers exclude the deleted paths
          themselves and are sorted. Nothing is silently dropped: if more than
          :data:`_MAX_REFERRERS_LISTED` referrers match, the list is truncated but
          a final ``(+N more ...)`` marker carrying the full count is appended, so
          the reviewer always sees that additional dependents exist. Returns
          ``{}`` when not a git repository or git grep is unavailable; never
          raises for a bad pattern.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return {}
    if len(deleted_paths) > _MAX_DELETIONS_SCANNED:
        # A mass deletion/rename would launch one full-repo grep per path; skip the
        # per-deletion referrer scan to bound the cost (paths are still listed by
        # name in the deletion note).
        logger.info(
            "find_referencing_paths: %d deletions exceed the scan cap (%d); "
            "skipping per-deletion reverse-reference search",
            len(deleted_paths),
            _MAX_DELETIONS_SCANNED,
        )
        return {}
    deleted_set = set(deleted_paths)
    result: Dict[str, List[str]] = {}
    for dp in deleted_paths:
        patterns = _deleted_module_patterns(dp)
        if not patterns:
            continue
        # stdout only — a git stderr warning must not be parsed as a referrer path.
        code, out = _run_git(path, ["git", "grep", "-l", "-I", "-E", *patterns], merge_stderr=False)
        # git grep exits 1 for a clean "no matches"; >=2 (or our -1) is an actual
        # error (bad pattern, grep unavailable). Don't conflate them — a silent
        # error would be presented as "nothing depends on it"; log it instead.
        if code == 1:
            continue  # no surviving referrers
        if code != 0:
            logger.warning(
                "find_referencing_paths: git grep failed (code %s) for %s; "
                "reverse-reference signal unavailable for this deletion",
                code,
                dp,
            )
            continue
        refs = sorted(
            {
                ref.strip()
                for ref in (out or "").splitlines()
                if ref.strip() and ref.strip() not in deleted_set
            }
        )
        if not refs:
            continue
        if len(refs) > _MAX_REFERRERS_LISTED:
            shown = refs[:_MAX_REFERRERS_LISTED]
            shown.append(
                f"(+{len(refs) - _MAX_REFERRERS_LISTED} more importers; "
                f"{len(refs)} total — review all before approving)"
            )
            result[dp] = shown
        else:
            result[dp] = refs
    return result


# Default .gitignore for new repos (Python, Node, IDE)
_DEFAULT_GITIGNORE = """# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual environments
.venv
venv/
ENV/
env/

# Node
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
.npm
.eslintcache

# IDE
.idea/
.vscode/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db
"""


def initialize_new_repo(
    repo_path: str | Path,
    *,
    gitignore_content: str | None = None,
) -> Tuple[bool, str]:
    """
    Initialize a directory as a new git repo: init, .gitignore, README.md, CONTRIBUTORS.md,
    docs/ folder, initial commit, rename master to main, create and checkout development branch.

    If the path is already a git repo, ensures development branch exists and checks it out.
    Writes .gitignore, README.md, CONTRIBUTORS.md and creates docs/ only if they do not
    already exist (so callers can pre-create them with desired content).

    Args:
        repo_path: Path to the directory to initialize.
        gitignore_content: Optional content for .gitignore. If provided and .gitignore
            does not exist, this is used; otherwise _DEFAULT_GITIGNORE is used.

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    path.mkdir(parents=True, exist_ok=True)

    if (path / ".git").exists():
        ok, msg = ensure_development_branch(path)
        # Idempotent: already a repo; ensuring development is success
        return True, f"Already a git repo; {msg}"

    # 1. git init
    code, out = _run_git(path, ["git", "init"])
    if code != 0:
        return False, f"git init failed: {out}"
    _run_git(path, ["git", "config", "commit.gpgsign", "false"])
    # Set a default local identity so commits work even when no global git config is set
    # (e.g. in CI environments). Local config is repo-scoped and does not affect global settings.
    name, email = _configured_commit_identity()
    _run_git(path, ["git", "config", "user.email", email])
    _run_git(path, ["git", "config", "user.name", name])

    # 2. .gitignore, README.md, CONTRIBUTORS.md, docs/ (only if missing)
    gitignore_path = path / ".gitignore"
    if not gitignore_path.exists():
        content = gitignore_content if gitignore_content is not None else _DEFAULT_GITIGNORE
        gitignore_path.write_text(content, encoding="utf-8")
    if not (path / "README.md").exists():
        (path / "README.md").write_text("", encoding="utf-8")
    if not (path / "CONTRIBUTORS.md").exists():
        (path / "CONTRIBUTORS.md").write_text("", encoding="utf-8")
    # Create docs folder for documentation
    docs_dir = path / "docs"
    if not docs_dir.exists():
        docs_dir.mkdir(parents=True, exist_ok=True)
        # Add a placeholder file so the directory is tracked by git
        (docs_dir / ".gitkeep").write_text("", encoding="utf-8")

    # 3. Initial commit
    code, out = _run_git(path, ["git", "add", "-A"])
    if code != 0:
        return False, f"git add failed: {out}"
    code, out = _run_git(path, ["git", "commit", "-m", "Initial commit"])
    if code != 0:
        return False, f"Initial commit failed: {out}"

    # 4. Rename master to main (git init may create master or main depending on version)
    code, out = _run_git(path, ["git", "branch", "--show-current"])
    current_branch = (out or "").strip() if code == 0 else "master"
    if current_branch == "master":
        code, out = _run_git(path, ["git", "branch", "-m", "master", "main"])
        if code != 0:
            return False, f"Rename master to main failed: {out}"

    # 5. Create development branch and switch to it
    code, out = _run_git(path, ["git", "checkout", "-b", DEVELOPMENT_BRANCH])
    if code != 0:
        return False, f"Create development branch failed: {out}"
    logger.info("Initialized new repo at %s with development branch", path)
    return True, f"Initialized repo at {path}; on branch {DEVELOPMENT_BRANCH}"


def development_branch_exists(repo_path: str | Path) -> bool:
    """True iff DEVELOPMENT_BRANCH resolves as a local branch in repo_path.

    A pure ref read (``git show-ref --verify --quiet refs/heads/<branch>``);
    never checks out or mutates anything, so it is safe to call from any
    worktree regardless of what branch is attached (checked out) elsewhere.

    Postconditions:
        - Returns True iff the ref resolves; False when absent or repo_path is
          not a git repository. Never raises.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False
    code, _ = _run_git(
        path, ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{DEVELOPMENT_BRANCH}"]
    )
    return code == 0


def _branch_attached_elsewhere(repo_path: Path, branch: str) -> bool:
    """True iff *branch* is currently attached (checked out) in a linked
    worktree of this repository OTHER than repo_path.

    ``git branch -a`` marks a branch checked out in another worktree with a
    ``+`` prefix (not the ``*`` used for the current worktree, and not blank),
    a distinction naive prefix-stripping (``.lstrip("* ")``) collapses — the
    branch then appears simply "not the current branch" and its existence can
    be missed entirely. This reads ``git worktree list --porcelain`` instead,
    which unambiguously pairs each worktree's absolute path with its attached
    branch (or omits one for a detached HEAD), so the comparison is exact
    rather than string-fragile.

    Preconditions:
        - repo_path is an existing git repository (checked by the caller).
    Postconditions:
        - Returns True only when another worktree (not repo_path) has
          ``refs/heads/<branch>`` attached. Returns False when unattached
          anywhere, attached at repo_path itself, or the query fails for any
          reason — failing closed toward "attempt the checkout", so a query
          failure surfaces as the checkout's own error rather than a silent,
          unverified skip.
    """
    code, out = _run_git(repo_path, ["git", "worktree", "list", "--porcelain"], merge_stderr=False)
    if code != 0:
        return False
    target_ref = f"refs/heads/{branch}"
    current_wt: Path | None = None
    for line in (out or "").splitlines():
        if line.startswith("worktree "):
            current_wt = Path(line[len("worktree ") :].strip()).resolve()
        elif line.startswith("branch ") and current_wt is not None:
            if line[len("branch ") :].strip() == target_ref and current_wt != repo_path:
                return True
    return False


def ensure_development_branch(repo_path: str | Path) -> Tuple[bool, str]:
    """
    Ensure the development branch exists. Create it from main if it does not.

    Returns:
        (success, message) - success=True when development exists or was created and checked out.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"

    if development_branch_exists(path):
        if _branch_attached_elsewhere(path, DEVELOPMENT_BRANCH):
            # development is attached in a different linked worktree of this same
            # repository (e.g. a coding-team worker's worktree calling this while
            # the swarm's shared checkout has development attached for merge/diff
            # operations). Attaching it here too is impossible — git allows a
            # branch to be attached in at most one worktree at a time — and
            # unneeded: every caller reaching this branch from a linked worktree
            # has already checked out the branch it actually wants there (e.g. a
            # feature branch via create_feature_branch), so leaving HEAD as the
            # caller set it up is exactly what's expected.
            return True, f"'{DEVELOPMENT_BRANCH}' branch exists (checked out in another worktree)"
        code, out = _run_git(path, ["git", "checkout", DEVELOPMENT_BRANCH])
        if code != 0:
            return False, f"Failed to checkout {DEVELOPMENT_BRANCH}: {out}"
        return True, f"Checked out existing branch '{DEVELOPMENT_BRANCH}'"

    # development does not exist yet: create it from main/master.
    code, out = _run_git(path, ["git", "branch", "-a"])
    if code != 0:
        return False, f"git branch failed: {out}"
    branches = [b.strip().lstrip("* +").split("/")[-1] for b in out.splitlines() if b.strip()]
    if MAIN_BRANCH not in branches and "master" not in branches:
        return False, "Neither 'main' nor 'master' branch found; create an initial commit first"

    base = MAIN_BRANCH if MAIN_BRANCH in branches else "master"
    code, out = _run_git(path, ["git", "checkout", "-b", DEVELOPMENT_BRANCH, base])
    if code != 0:
        return False, f"Failed to create development branch: {out}"
    logger.info("Created branch '%s' from '%s'", DEVELOPMENT_BRANCH, base)
    return True, f"Created branch '{DEVELOPMENT_BRANCH}' from '{base}'"


# Disposable files that can be removed before checkout to avoid "would be overwritten" errors.
# These are typically generated by tests (e.g. SQLite test.db) and should not block branch switches.
_DISPOSABLE_FILES_BEFORE_CHECKOUT = ("test.db", "*.db")


def _clear_disposable_files_if_blocking(path: Path, checkout_out: str) -> bool:
    """
    If checkout failed due to local changes in disposable files (e.g. test.db),
    remove those files so checkout can succeed on retry.
    Returns True if any file was removed.
    """
    removed = False
    if "would be overwritten" not in checkout_out and "Your local changes" not in checkout_out:
        return False
    for name in _DISPOSABLE_FILES_BEFORE_CHECKOUT:
        if "*" in name:
            continue  # Skip glob patterns for now; handle literal test.db
        fp = path / name
        if fp.exists():
            try:
                fp.unlink()
                logger.info("Removed disposable file %s to allow branch checkout", name)
                removed = True
            except OSError as e:
                logger.warning("Could not remove %s before checkout: %s", name, e)
    return removed


def ensure_files_committed_on_main(
    repo_path: str | Path,
    file_paths: List[str],
    *,
    commit_message: str = "Add README, CONTRIBUTORS, .gitignore",
) -> Tuple[bool, str]:
    """
    Ensure the given files are committed on the main branch.
    Checkouts main, adds the files, commits if there are changes, then checkouts development.
    Idempotent: no-op if files are already committed.

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"

    # Check if main branch exists
    code, out = _run_git(path, ["git", "branch", "-a"])
    if code != 0:
        return False, f"git branch failed: {out}"
    branches = [b.strip().lstrip("* ").split("/")[-1] for b in out.splitlines() if b.strip()]
    if MAIN_BRANCH not in branches and "master" not in branches:
        return False, "Neither 'main' nor 'master' branch found"

    base = MAIN_BRANCH if MAIN_BRANCH in branches else "master"
    current_branch_code, current_out = _run_git(path, ["git", "branch", "--show-current"])
    current_branch = (current_out or "").strip() if current_branch_code == 0 else ""

    # Checkout main (clear disposable files like test.db if they block)
    code, out = _run_git(path, ["git", "checkout", base])
    if code != 0:
        if _clear_disposable_files_if_blocking(path, out):
            code, out = _run_git(path, ["git", "checkout", base])
        if code != 0:
            return False, f"Failed to checkout {base}: {out}"

    # Add the files
    for fp in file_paths:
        if (path / fp).exists():
            code, out = _run_git(path, ["git", "add", fp])
            if code != 0:
                _run_git(path, ["git", "checkout", current_branch or DEVELOPMENT_BRANCH])
                return False, f"git add {fp} failed: {out}"

    # Check if there are changes to commit
    code, out = _run_git(path, ["git", "status", "--porcelain"])
    if code != 0:
        _run_git(path, ["git", "checkout", current_branch or DEVELOPMENT_BRANCH])
        return False, f"git status failed: {out}"

    if out.strip():
        code, out = _run_git(path, ["git", "commit", "-m", commit_message])
        if code != 0:
            _run_git(path, ["git", "checkout", current_branch or DEVELOPMENT_BRANCH])
            return False, f"git commit failed: {out}"
        logger.info("Committed %s on %s", file_paths, base)

    # Checkout back to development (or original branch)
    target = current_branch if current_branch and current_branch != base else DEVELOPMENT_BRANCH
    code, out = _run_git(path, ["git", "checkout", target])
    if code != 0:
        if _clear_disposable_files_if_blocking(path, out):
            code, out = _run_git(path, ["git", "checkout", target])
        if code != 0:
            return False, f"Failed to checkout {target}: {out}"

    return True, f"Files committed on {base}"
