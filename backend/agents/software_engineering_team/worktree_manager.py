"""Per-worker git worktree lifecycle for the coding-team implementation swarm.

Each implementation worker (``frontend_v2``/``backend_v2``) needs its own
isolated git working directory so concurrent workers don't corrupt each
other's checkout (concurrent branch checkouts, build/lint reading another
worker's in-flight files). :class:`WorktreeManager` creates and owns one
linked git worktree per worker, sibling to the swarm's shared repo checkout
(``self.path``), sharing that repo's object store and branch refs.

Placement mirrors the existing dot-prefixed sibling convention used for the
per-issue clone lock (``coding_team.clone_workspace.clone_lock_path``):
``<repo_path.parent>/.<repo_path.name>.worktrees/<agent_id>`` — never nested
inside ``repo_path`` itself, since a nested worktree's files would be visible
to ``repo_path``'s own recursive scans (the repo-context cache, build
verification's directory walks), reintroducing the cross-worker leakage this
class exists to prevent.

Scope note: a linked worktree only checks out *tracked* files. Nothing on the
frontend build path installs dependencies on demand, so a fresh worktree
without ``node_modules`` would fail its first build — :meth:`prepare` symlinks
an existing ``node_modules`` (repo root or ``frontend/``, mirroring
``software_engineering_team``'s own frontend-directory resolution) into each
worktree at the same relative path. This is deliberately a *shared, read-only*
resource: every worktree gets its own independent symlink to the one
repo-level ``node_modules`` directory, which is safe for any number of
worktrees — including 2+ concurrent same-stack (e.g. ``frontend_v2-1``,
``frontend_v2-2``) workers — because nothing on the swarm's own build/lint
path installs into it (no ``npm install``/``npm ci``). The one genuine
global-environment mutation is the backend worker's dependency install
(``pip install``), which is not per-worktree; guarding that is tracked
separately and out of scope here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Sequence

from shared.git.git_utils import (
    DEVELOPMENT_BRANCH,
    UnsafeRepoPathError,
    add_worktree,
    development_branch_exists,
    ensure_development_branch,
    initialize_new_repo,
    prune_worktrees,
    remove_worktree,
    resolve_safe_repo_path,
)

logger = logging.getLogger(__name__)


class WorktreePrepareError(RuntimeError):
    """Raised when a worker's worktree cannot be created, or is used before/after
    it should be.

    Fails closed rather than letting a worker silently fall back onto the
    shared repo path, which would reintroduce the cross-worker corruption
    (concurrent checkouts, build/lint reading another worker's files) this
    module exists to prevent.
    """


class WorktreeManager:
    """One linked git worktree per implementation worker for a single swarm run.

    All worktrees are created eagerly and serially in :meth:`prepare` — never
    lazily from inside a worker thread — so concurrent ``git worktree add``
    calls never race on the shared ``.git/worktrees/`` administrative area;
    :meth:`path_for` is then a pure, lock-free lookup.

    Invariants:
        - Once :meth:`prepare` returns successfully, ``path_for(agent_id)``
          returns the same :class:`~pathlib.Path` for the manager's lifetime,
          for every ``agent_id`` passed to ``__init__``.
    """

    def __init__(self, repo_path: Path, agent_ids: Sequence[str]) -> None:
        """Store identity only; no filesystem or git access happens here.

        Preconditions:
            - ``repo_path`` is the swarm's shared repo checkout (``self.path``).
            - ``agent_ids`` are the swarm's worker agent ids.
        Postconditions:
            - ``path_for`` raises :class:`WorktreePrepareError` until
              :meth:`prepare` completes successfully.
        """
        self._repo_path = Path(repo_path).resolve()
        # De-dup, order-preserving: a duplicate agent_id must not create two
        # worktree registrations for the same directory.
        self._agent_ids = list(dict.fromkeys(agent_ids))
        self._root = self._repo_path.parent / f".{self._repo_path.name}.worktrees"
        self._paths: Dict[str, Path] = {}
        self._prepared = False

    def prepare(self) -> None:
        """Create (or reuse) a detached worktree per agent id, sibling to repo_path.

        Preconditions:
            - ``repo_path`` is an existing directory; it will be initialized as
              a git repository with a ``development`` branch here if it is not
              one already (mirrors ``v2_team_worker._ensure_development_ready``
              — the very first coding-team task against a brand-new checkout
              used to trigger this lazily from inside the worker; preparing
              worktrees up front means it must happen here instead, once, for
              the shared checkout itself before any linked worktree is added
              off it).
        Postconditions:
            - For every agent id, ``path_for(agent_id)`` resolves to an
              existing linked worktree of repo_path, detached at
              development's current tip.
            - A worktree directory/registration left by a previous abnormal
              exit is pruned and recreated (self-healing) — see
              ``shared.git.git_utils.add_worktree``.
            - Idempotent: a second call after a successful ``prepare()`` is a
              no-op.
        Raises:
            - :class:`WorktreePrepareError` if the shared repo cannot be
              initialized, an agent id is unsafe to use as a worktree path
              component, or any individual worktree fails to create — fails
              closed rather than silently falling a worker back onto the
              shared repo_path, which would reintroduce the corruption this
              class exists to prevent.
        """
        if self._prepared:
            return
        self._ensure_root_repo_ready()
        # Clear any admin-area residue from a crashed prior run before creating
        # anything new, so a leftover registration for an unrelated worker is
        # never discovered mid-round by whichever worker happens to touch it.
        prune_worktrees(self._repo_path)
        node_modules_src = self._node_modules_source()
        for agent_id in self._agent_ids:
            wt_path = self._worktree_path_for(agent_id)
            ok, msg = add_worktree(self._repo_path, wt_path, ref=DEVELOPMENT_BRANCH)
            if not ok:
                raise WorktreePrepareError(
                    f"Failed to create worktree for agent {agent_id!r} at {wt_path}: {msg}"
                )
            # Recorded immediately (not only after the whole loop completes): if a LATER
            # agent's worktree fails to create, this one must still be found by cleanup()
            # rather than left behind as an orphaned worktree + admin-area entry.
            self._paths[agent_id] = wt_path
            if node_modules_src is not None:
                self._symlink_node_modules(wt_path, node_modules_src)
        self._prepared = True

    def _worktree_path_for(self, agent_id: str) -> Path:
        """Resolve agent_id's worktree path, failing closed if it would escape self._root.

        Preconditions:
            - None.
        Postconditions:
            - Returns the resolved worktree path when agent_id is a safe single path
              component under self._root.
        Raises:
            - :class:`WorktreePrepareError` when agent_id is empty or would place the
              resulting path outside self._root (path separators, ``..`` traversal, or an
              absolute path) — agent ids ultimately trace back to Tech-Lead-generated or
              persisted stack names, not a fully trusted source, so this is validated the
              same way ``shared.git.resolve_safe_repo_path`` guards repo-relative file
              writes elsewhere: without it, a malformed agent id could route
              ``add_worktree``/``remove_worktree`` to create or delete an arbitrary
              filesystem path outside the intended worktree root.
        """
        try:
            return resolve_safe_repo_path(self._root, agent_id)
        except UnsafeRepoPathError as exc:
            raise WorktreePrepareError(
                f"Unsafe agent id {agent_id!r} for worktree path: {exc}"
            ) from exc

    def _ensure_root_repo_ready(self) -> None:
        """Ensure repo_path itself is an initialized git repo with development.

        The shared checkout may not exist as a git repository yet on a brand
        new coding-team job (previously initialized lazily by the first
        worker's ``run_implement`` call) — worktrees can only be added off an
        already-initialized repository, so this must happen before any is.
        """
        if not (self._repo_path / ".git").exists():
            ok, msg = initialize_new_repo(self._repo_path)
            if not ok:
                raise WorktreePrepareError(f"Failed to initialize repo at {self._repo_path}: {msg}")
            return
        if not development_branch_exists(self._repo_path):
            ok, msg = ensure_development_branch(self._repo_path)
            if not ok:
                raise WorktreePrepareError(
                    f"Failed to ensure '{DEVELOPMENT_BRANCH}' at {self._repo_path}: {msg}"
                )

    def _node_modules_source(self) -> Optional[Path]:
        """Resolve an existing frontend ``node_modules`` directory to share.

        Mirrors ``software_engineering_team``'s own frontend-directory
        resolution (repo root if it has ``package.json``, else ``frontend/``)
        so the symlink lands exactly where the build path will look for it.

        Postconditions:
            - Returns the ``node_modules`` directory when present, else None.
              Never raises.
        """
        frontend_dir = (
            self._repo_path
            if (self._repo_path / "package.json").exists()
            else (self._repo_path / "frontend")
        )
        node_modules = frontend_dir / "node_modules"
        return node_modules if node_modules.is_dir() else None

    def _symlink_node_modules(self, wt_path: Path, node_modules_src: Path) -> None:
        """Symlink node_modules_src into wt_path at the same relative location.

        Symlink, not copy: cheap, and worktree removal (``rmtree``/``git
        worktree remove``) never follows a symlink into the shared directory
        it points at, so cleanup can't corrupt repo_path's real
        ``node_modules``. Build-output/cache directories (``.angular/``,
        ``dist/``) are deliberately left worktree-local — never symlinked.

        Postconditions:
            - A failure to symlink is logged and does not raise: the worktree
              is still usable (a backend task, or a frontend task that
              tolerates a slow reinstall) without it.
        """
        rel_frontend_dir = node_modules_src.parent.relative_to(self._repo_path)
        target_dir = wt_path / rel_frontend_dir
        link_path = target_dir / "node_modules"
        if link_path.exists() or link_path.is_symlink():
            return
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            link_path.symlink_to(node_modules_src, target_is_directory=True)
        except OSError as e:
            logger.warning("Could not symlink node_modules into worktree %s: %s", wt_path, e)

    def path_for(self, agent_id: str) -> Path:
        """Return the prepared worktree path for agent_id.

        Preconditions:
            - :meth:`prepare` completed successfully; ``agent_id`` was passed
              to ``__init__``.
        Postconditions:
            - Returns the same Path for the same agent_id for the manager's
              lifetime. A pure lookup; never touches disk.
        Raises:
            - :class:`WorktreePrepareError` when called before a successful
              ``prepare()`` or with an agent_id this manager was not
              constructed with — fails closed rather than returning None,
              which a caller could mistake for "use the shared path instead."
        """
        if not self._prepared:
            raise WorktreePrepareError("WorktreeManager.prepare() has not completed")
        try:
            return self._paths[agent_id]
        except KeyError:
            raise WorktreePrepareError(f"No worktree prepared for agent {agent_id!r}") from None

    def cleanup(self) -> None:
        """Best-effort remove every managed worktree and prune leftover metadata.

        Postconditions:
            - Each worktree is removed and its admin-area entry pruned when
              possible; a per-worktree failure is logged and never raises —
              this always runs from a ``finally``, so it must never mask the
              job's real outcome. Safe to call multiple times, and safe to
              call even if :meth:`prepare` never completed (no-op).
        """
        for agent_id, wt_path in list(self._paths.items()):
            ok, msg = remove_worktree(self._repo_path, wt_path, force=True)
            if not ok:
                logger.warning(
                    "Failed to remove worktree for agent %s at %s: %s", agent_id, wt_path, msg
                )
        if self._paths:
            prune_worktrees(self._repo_path)
        self._paths = {}
        self._prepared = False
