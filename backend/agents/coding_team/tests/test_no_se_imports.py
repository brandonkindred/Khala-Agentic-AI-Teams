"""Acyclic guard: the ``coding_team`` package must not import ``software_engineering_team``.

The two teams historically formed a circular dependency. After the shared-infra
extraction (neutral ``shared_*`` packages) and the engine-provider inversion,
coding_team depends only on neutral packages and an injected ``CodeEngineProvider``.
This AST-based test fails if any coding_team module reintroduces a direct
``software_engineering_team`` import — string/docstring mentions are ignored, only
real ``import`` / ``from ... import`` statements count.

The standalone service's SE wiring lives in the separate ``coding_team_service``
composition-root package (the container's ``TEAM_MODULE``), which is deliberately
outside ``coding_team`` and therefore not scanned.
"""

from __future__ import annotations

import ast
from pathlib import Path

CODING_TEAM_ROOT = Path(__file__).resolve().parent.parent


def _offending_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "software_engineering_team" or alias.name.startswith(
                    "software_engineering_team."
                ):
                    offenders.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "software_engineering_team" or module.startswith(
                "software_engineering_team."
            ):
                offenders.append(f"{path}:{node.lineno}: from {module} import ...")
    return offenders


def test_coding_team_has_no_software_engineering_team_imports() -> None:
    offenders: list[str] = []
    for path in sorted(CODING_TEAM_ROOT.rglob("*.py")):
        offenders.extend(_offending_imports(path))
    assert not offenders, "coding_team must not import software_engineering_team:\n" + "\n".join(
        offenders
    )
