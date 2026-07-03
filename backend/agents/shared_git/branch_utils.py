"""Shared feature-branch slug/suffix helpers for the v2 deliver phases.

The backend and frontend code-v2 teams both build feature-branch names from a
task id and title. Keeping the slug rules in one place guarantees the two teams
produce identical, git-safe branch names and that a fix (e.g. collision-resistant
suffixes) applies to both at once.
"""

from __future__ import annotations

import hashlib
import re

_MAX_SCOPE_SLUG_LENGTH = 40
_MAX_TASK_ID_SLUG_LENGTH = 20
_BRANCH_HASH_LENGTH = 8


def make_slug(task_id: str, task_title: str) -> str:
    """Return the stable branch/commit scope slug for a delivery task.

    Preconditions:
        - ``task_id`` / ``task_title`` are strings (either may be empty).
    Postconditions:
        - Returns a non-empty, lowercase, ``[a-z0-9-]`` slug of at most
          ``_MAX_SCOPE_SLUG_LENGTH`` characters, falling back to ``"task"``.
    """
    return (
        re.sub(r"[^a-z0-9-]+", "-", (task_title or task_id).lower()).strip("-")[
            :_MAX_SCOPE_SLUG_LENGTH
        ]
        or "task"
    )


def make_task_id_slug(task_id: str) -> str:
    """Return a branch-safe task-id slug for feature branch names.

    Postconditions:
        - Returns a non-empty ``[a-z0-9-]`` slug of at most
          ``_MAX_TASK_ID_SLUG_LENGTH`` characters, falling back to ``"task"``.
    """
    return (
        re.sub(r"[^a-z0-9-]+", "-", (task_id or "task").lower()).strip("-")[
            :_MAX_TASK_ID_SLUG_LENGTH
        ]
        or "task"
    )


def make_branch_suffix(task_id: str, task_title: str) -> str:
    """Return the branch suffix used by ``create_feature_branch``.

    Appends a stable short hash of the raw task id so two distinct tasks whose
    slugs collide (e.g. both reduce to ``task-task``) never resolve to the same
    branch — ``create_feature_branch`` deletes-and-recreates an existing branch,
    so a collision would silently destroy another task's unmerged handoff branch.
    The hash is deterministic per task id, so retries of the same task reuse it.
    """
    base = f"{make_task_id_slug(task_id)}-{make_slug(task_id, task_title)}"
    # sha256 (not sha1) purely to avoid security-scanner false positives — this digest is a
    # branch-name disambiguator, never a security primitive.
    digest = hashlib.sha256((task_id or "task").encode("utf-8")).hexdigest()[:_BRANCH_HASH_LENGTH]
    return f"{base}-{digest}"
