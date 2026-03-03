"""Tests for shared.test_spec_expectations."""

import tempfile
from pathlib import Path

from software_engineering_team.shared.test_spec_expectations import (
    build_test_spec_checklist,
    extract_backend_test_expectations,
    extract_frontend_route_expectations,
)


def test_extract_backend_test_expectations_empty():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        assert extract_backend_test_expectations(path) == ""


def test_extract_backend_test_expectations_finds_imports():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        tests_dir = path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_models.py").write_text(
            "from app.database import Base\nfrom app.models import Tenant, Task\n"
        )
        result = extract_backend_test_expectations(path)
        assert "app.database" in result
        assert "Base" in result
        # May have app.models or app.database depending on parse order
        assert "must export" in result


def test_extract_frontend_route_expectations_empty():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        assert extract_frontend_route_expectations(path) == ""


def test_extract_frontend_route_expectations_finds_paths():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        routes = path / "src" / "app"
        routes.mkdir(parents=True)
        (routes / "app.routes.ts").write_text(
            "loadComponent: () => import('./components/task-form/task-form.component')"
        )
        result = extract_frontend_route_expectations(path)
        assert "task-form" in result


def test_build_test_spec_checklist_backend():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        tests_dir = path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("from app.database import Base\n")
        result = build_test_spec_checklist(path, "backend")
        assert "app.database" in result
        assert "Base" in result


def test_build_test_spec_checklist_frontend_empty():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        assert build_test_spec_checklist(path, "frontend") == ""
