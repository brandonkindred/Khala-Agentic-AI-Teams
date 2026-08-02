"""Tests for frontend framework resolution from spec and task metadata."""

from pathlib import Path

from software_engineering_team.shared.frontend_framework import (
    detect_framework_from_project,
    get_frontend_framework_from_spec,
    resolve_frontend_framework,
)


def test_get_frontend_framework_from_spec_empty() -> None:
    """Empty or whitespace spec returns None."""
    assert get_frontend_framework_from_spec("") is None
    assert get_frontend_framework_from_spec("   \n  ") is None


def test_get_frontend_framework_from_spec_react() -> None:
    """Spec mentioning React returns 'react'."""
    assert get_frontend_framework_from_spec("The app will use React for the UI.") == "react"
    assert get_frontend_framework_from_spec("Build a React app with hooks.") == "react"
    assert get_frontend_framework_from_spec("Use React for the frontend.") == "react"
    assert get_frontend_framework_from_spec("REACT application") == "react"


def test_get_frontend_framework_from_spec_vue() -> None:
    """Spec mentioning Vue returns 'vue'."""
    assert get_frontend_framework_from_spec("The frontend should use Vue.js.") == "vue"
    assert get_frontend_framework_from_spec("Build with Vue 3 and Composition API.") == "vue"
    assert get_frontend_framework_from_spec("Use Vue for the SPA.") == "vue"


def test_get_frontend_framework_from_spec_priority() -> None:
    """Angular is checked first, then React, then Vue."""
    # Angular takes priority
    assert (
        get_frontend_framework_from_spec(
            "Use Angular for the dashboard. React is also mentioned later."
        )
        == "angular"
    )
    # React takes priority over Vue
    assert (
        get_frontend_framework_from_spec(
            "Use React for the dashboard. Vue is also mentioned later."
        )
        == "react"
    )


def test_get_frontend_framework_from_spec_no_false_positives() -> None:
    """Words like 'reaction' or 'vue' as substring don't match."""
    assert get_frontend_framework_from_spec("User reaction to the event.") is None
    assert get_frontend_framework_from_spec("We need a revue of the process.") is None
    assert get_frontend_framework_from_spec("The vueapp directory holds config.") is None
    assert get_frontend_framework_from_spec("See the vueapplication repo.") is None
    assert get_frontend_framework_from_spec("Check vuefrontend for details.") is None


def test_get_frontend_framework_from_spec_angular() -> None:
    """Spec mentioning Angular returns 'angular'."""
    assert get_frontend_framework_from_spec("Use Angular for the frontend.") == "angular"
    assert get_frontend_framework_from_spec("Build an Angular app.") == "angular"
    assert get_frontend_framework_from_spec("ANGULAR application") == "angular"


def test_resolve_frontend_framework_task_metadata_first() -> None:
    """Task metadata framework_target takes precedence over spec."""
    assert (
        resolve_frontend_framework(
            {"framework_target": "react"},
            "Use Vue for the frontend.",
        )
        == "react"
    )
    assert (
        resolve_frontend_framework(
            {"framework_target": "angular"},
            "Use React for the frontend.",
        )
        == "angular"
    )
    assert (
        resolve_frontend_framework(
            {"framework_target": "vue"},
            "",
        )
        == "vue"
    )


def test_resolve_frontend_framework_spec_fallback() -> None:
    """When metadata has no framework_target, spec is used."""
    assert resolve_frontend_framework({}, "Build a React app.") == "react"
    assert resolve_frontend_framework(None, "Use Vue.js.") == "vue"


def test_resolve_frontend_framework_returns_none_when_no_framework() -> None:
    """When neither metadata, project files, nor spec specify a framework, returns None."""
    assert resolve_frontend_framework({}, "") is None
    assert resolve_frontend_framework(None, "Generic frontend requirements.") is None
    assert resolve_frontend_framework({}, "Use TypeScript and REST APIs.") is None


def test_detect_framework_from_project_angular(tmp_path: Path) -> None:
    """Detect Angular from angular.json or package.json."""
    (tmp_path / "angular.json").write_text("{}")
    assert detect_framework_from_project(tmp_path) == "angular"


def test_detect_framework_from_project_react(tmp_path: Path) -> None:
    """Detect React from package.json."""
    import json

    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))
    assert detect_framework_from_project(tmp_path) == "react"


def test_detect_framework_from_project_vue(tmp_path: Path) -> None:
    """Detect Vue from package.json."""
    import json

    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"dependencies": {"vue": "^3.0.0"}}))
    assert detect_framework_from_project(tmp_path) == "vue"


def test_detect_framework_from_project_none(tmp_path: Path) -> None:
    """No framework detected when no indicators present."""
    assert detect_framework_from_project(tmp_path) is None
    assert detect_framework_from_project(None) is None


def test_detect_framework_from_project_vue_via_vite_config(tmp_path: Path) -> None:
    """Detect Vue from vite.config.ts plus a *.vue file, with no package.json."""
    (tmp_path / "vite.config.ts").write_text("")
    (tmp_path / "App.vue").write_text("<template></template>")
    assert detect_framework_from_project(tmp_path) == "vue"


def test_detect_framework_from_project_react_via_vite_config(tmp_path: Path) -> None:
    """Detect React from vite.config.js plus a *.tsx file when no *.vue file exists."""
    (tmp_path / "vite.config.js").write_text("")
    (tmp_path / "App.tsx").write_text("export default function App() {}")
    assert detect_framework_from_project(tmp_path) == "react"


def test_detect_framework_from_project_vue_config_with_jsx_stays_vue(tmp_path: Path) -> None:
    """vue.config.js is Vue-specific: a Vue CLI project rendering via JSX/TSX (no *.vue
    files) must still resolve to Vue, not fall through to the React marker fallback."""
    (tmp_path / "vue.config.js").write_text("")
    (tmp_path / "App.tsx").write_text("export default {}")
    assert detect_framework_from_project(tmp_path) == "vue"


def test_detect_framework_from_project_vite_config_no_markers(tmp_path: Path) -> None:
    """Vite config with no Vue or React markers returns None (no false positive)."""
    (tmp_path / "vite.config.ts").write_text("")
    assert detect_framework_from_project(tmp_path) is None


def test_detect_framework_from_project_unreadable_directory_returns_none(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An unreadable subdirectory during marker scanning degrades to None, not a crash."""

    (tmp_path / "vite.config.ts").write_text("")

    def _raise_permission_error(self, pattern):
        raise PermissionError("simulated unreadable directory")

    monkeypatch.setattr(Path, "rglob", _raise_permission_error)
    assert detect_framework_from_project(tmp_path) is None


def test_resolve_frontend_framework_normalizes_value() -> None:
    """Metadata value is normalized (lowercased, valid values only)."""
    assert resolve_frontend_framework({"framework_target": "React"}, "") == "react"
    assert resolve_frontend_framework({"framework_target": "ANGULAR"}, "") == "angular"
