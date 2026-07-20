# shared_git

Neutral, team-agnostic **git operations**.

`git_utils` (branch/commit/merge/diff over a single `_run_git` subprocess choke
point) and `branch_utils` (branch-name derivation) are used by both the
software-engineering team and the coding team. Git sharing already worked when it
lived under `software_engineering_team.shared`; this package removes the last bit
of "one team importing the other's internals" by giving it a neutral home.

## Layout

| Module | Was | Responsibility |
|---|---|---|
| `git_utils` | `software_engineering_team/shared/git_utils.py` | `checkout_branch`, `create_feature_branch`, `merge_branch`, `branch_diff`, `commit_paths`, `initialize_new_repo`, `DEVELOPMENT_BRANCH`, … |
| `branch_utils` | `software_engineering_team/shared/branch_utils.py` | Deterministic branch-name helpers. |

## Usage

```python
from shared_git.git_utils import branch_diff, checkout_branch, DEVELOPMENT_BRANCH
from shared_git import branch_utils
```

## Compatibility

`software_engineering_team/shared/git_utils.py` and `.../branch_utils.py` remain
as thin `sys.modules` alias shims onto this package, so the many existing SE
importers — and the tests that `@patch("software_engineering_team.shared.git_utils.…")`
— keep working unchanged (the shim IS the same module object). coding_team and
`shared_command_runner` import `shared_git` directly.
