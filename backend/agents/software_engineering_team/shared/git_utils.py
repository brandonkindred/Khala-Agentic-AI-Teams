"""
Git utilities for the software engineering team branching strategy.

The Tech Lead enforces: all development on a development branch;
create it from main if it does not exist.
"""

from __future__ import annotations

import logging
import os
import re
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


def _run_git(repo_path: Path, cmd: list[str], timeout: int = 30) -> Tuple[int, str]:
    """Run git command in repo. Returns (returncode, stdout+stderr).

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
        return result.returncode, (result.stdout or "") + (result.stderr or "")
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
            # Stale branch from a previous run — delete and recreate
            logger.warning(
                "Branch '%s' already exists, deleting and recreating from '%s'",
                branch_name,
                base_branch,
            )
            _run_git(path, ["git", "checkout", base_branch])
            del_code, del_out = _run_git(path, ["git", "branch", "-D", branch_name])
            if del_code != 0:
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


def write_files_and_commit(
    repo_path: str | Path,
    files_dict: Dict[str, str],
    message: str,
) -> Tuple[bool, str]:
    """
    Write files to repo, git add, and commit on the current branch.
    files_dict: { "path/relative/to/repo": "content" }

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    for file_path, content in files_dict.items():
        full_path = path / file_path
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
    mb_code, mb_out = _run_git(path, ["git", "merge-base", "--all", base, head])
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
          readable text. A path that did not exist at the base, that resolves to
          a tree (a directory/submodule has no blob), or whose blob is binary (a
          NUL byte) is omitted rather than returned as gibberish. Content is made
          UTF-8/JSON safe: ``_run_git`` decodes with ``surrogateescape``, so a
          blob with invalid-UTF-8 (but NUL-free) bytes would otherwise carry lone
          surrogates that crash a later UTF-8/JSON encode; those bytes are
          re-decoded with ``errors="replace"`` here.
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
    for rel in paths:
        code, out = _run_git(path, ["git", "show", f"{merge_base}:{rel}"])
        if code != 0:
            continue  # path absent at base, or a tree (submodule/dir): no blob
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
    code, out = _run_git(path, ["git", "diff", "--name-status", "--no-renames", "-z", merge_base])
    if code != 0:
        raise BaselineDiffUnavailable(f"net diff vs {merge_base} failed: {out!r}")
    changed: List[str] = []
    deleted: List[str] = []
    seen_changed: set[str] = set()
    seen_deleted: set[str] = set()
    fields = [f for f in (out or "").split("\0") if f]
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


# Conventional Python source roots stripped when deriving a deleted module's
# importable dotted path: code under ``src/pkg/x.py`` is imported as ``pkg.x``.
_PYTHON_SOURCE_ROOTS: frozenset[str] = frozenset({"src", "lib", "app"})

# Cap on referrers listed per deletion before an explicit truncation marker is
# appended (the count is always shown, so nothing is silently hidden).
_MAX_REFERRERS_LISTED = 25


def _deleted_module_patterns(rel_path: str) -> List[str]:
    """Importer-search regexes for a deleted ``.py`` path (``[]`` if not Python).

    Produces precise importer signals — far below a bare-stem search's
    false-positive rate — covering common layouts:

    - the full dotted path (``a.b.c`` from ``a/b/c.py``) and the same with a
      leading source root stripped (``pkg.x`` from ``src/pkg/x.py``), since
      surviving code imports the *package* path, not the on-disk source root;
    - a bare ``import <stem>`` / ``from ... import <stem>``;
    - for a deleted package initializer (``pkg/__init__.py``), the package name
      itself (``import pkg`` / ``pkg.``), since removing it breaks ``import pkg``.
    """
    p = Path(rel_path)
    if p.suffix != ".py":
        return []
    parts = list(p.with_suffix("").parts)
    if not parts:
        return []

    # Candidate dotted module paths: full, and with a leading source root dropped.
    dotted_variants: set[str] = set()
    if parts[-1] == "__init__":
        pkg_parts = parts[:-1]  # the package this file initializes
        if pkg_parts:
            dotted_variants.add(".".join(pkg_parts))
            if pkg_parts[0] in _PYTHON_SOURCE_ROOTS and len(pkg_parts) > 1:
                dotted_variants.add(".".join(pkg_parts[1:]))
            names = [pkg_parts[-1]]
        else:
            return []
    else:
        dotted_variants.add(".".join(parts))
        if parts[0] in _PYTHON_SOURCE_ROOTS and len(parts) > 1:
            dotted_variants.add(".".join(parts[1:]))
        names = [parts[-1]]

    patterns: List[str] = []
    for dotted in dotted_variants:
        patterns += ["-e", rf"\b{re.escape(dotted)}\b"]
    for name in names:
        patterns += ["-e", rf"import\s+{re.escape(name)}\b"]
    return patterns


def find_referencing_paths(
    repo_path: str | Path,
    deleted_paths: List[str],
) -> Dict[str, List[str]]:
    """For each deleted path, the surviving worktree files that still mention it.

    Best-effort reverse-dependency signal so a reviewer can check the deletion
    note's instruction that "nothing still depends on" a removed module. For each
    deleted ``.py`` path it ``git grep``s the *worktree* (tracked files, so the
    just-removed file itself is gone) for the module's importable dotted path(s)
    — with conventional source roots (``src``/``lib``/``app``) stripped — a bare
    ``import <name>``, and, for a deleted ``__init__.py``, the package name. These
    are precise importer signals that keep the false-positive rate far below a
    bare stem search. Non-``.py`` deletions are skipped.

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
    deleted_set = set(deleted_paths)
    result: Dict[str, List[str]] = {}
    for dp in deleted_paths:
        patterns = _deleted_module_patterns(dp)
        if not patterns:
            continue
        code, out = _run_git(path, ["git", "grep", "-l", "-I", "-E", *patterns])
        if code != 0:
            continue  # no matches (grep exits non-zero) or grep unavailable
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


def ensure_development_branch(repo_path: str | Path) -> Tuple[bool, str]:
    """
    Ensure the development branch exists. Create it from main if it does not.

    Returns:
        (created, message) - created=True if branch was created, message describes action.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"

    # Check if development branch exists
    code, out = _run_git(path, ["git", "branch", "-a"])
    if code != 0:
        return False, f"git branch failed: {out}"
    branches = [b.strip().lstrip("* ").split("/")[-1] for b in out.splitlines() if b.strip()]
    if DEVELOPMENT_BRANCH in branches:
        code, out = _run_git(path, ["git", "checkout", DEVELOPMENT_BRANCH])
        if code != 0:
            return False, f"Failed to checkout {DEVELOPMENT_BRANCH}: {out}"
        return False, f"Checked out existing branch '{DEVELOPMENT_BRANCH}'"

    # Ensure we have main or master
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
