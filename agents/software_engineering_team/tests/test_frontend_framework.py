"""Tests for frontend framework resolution from spec and task metadata."""

import pytest

from shared.frontend_framework import (
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


def test_get_frontend_framework_from_spec_react_before_vue() -> None:
    """When both appear, React is checked first and wins."""
    assert get_frontend_framework_from_spec(
        "Use React for the dashboard. Vue is also mentioned later."
    ) == "react"


def test_get_frontend_framework_from_spec_no_false_positives() -> None:
    """Words like 'reaction' or 'vue' as substring don't match."""
    assert get_frontend_framework_from_spec("User reaction to the event.") is None
    assert get_frontend_framework_from_spec("We need a revue of the process.") is None


def test_get_frontend_framework_from_spec_angular_unchanged() -> None:
    """Angular in spec does not return a value (we only detect React/Vue override)."""
    # We only return react or vue; angular is the default so we don't need to detect it
    result = get_frontend_framework_from_spec("Use Angular for the frontend.")
    assert result is None


def test_resolve_frontend_framework_task_metadata_first() -> None:
    """Task metadata framework_target takes precedence over spec."""
    assert resolve_frontend_framework(
        {"framework_target": "react"},
        "Use Vue for the frontend.",
    ) == "react"
    assert resolve_frontend_framework(
        {"framework_target": "angular"},
        "Use React for the frontend.",
    ) == "angular"
    assert resolve_frontend_framework(
        {"framework_target": "vue"},
        "",
    ) == "vue"


def test_resolve_frontend_framework_spec_fallback() -> None:
    """When metadata has no framework_target, spec is used."""
    assert resolve_frontend_framework({}, "Build a React app.") == "react"
    assert resolve_frontend_framework(None, "Use Vue.js.") == "vue"


def test_resolve_frontend_framework_default_angular() -> None:
    """When neither metadata nor spec specify, default is Angular."""
    assert resolve_frontend_framework({}, "") == "angular"
    assert resolve_frontend_framework(None, "Generic frontend requirements.") == "angular"
    assert resolve_frontend_framework({}, "Use TypeScript and REST APIs.") == "angular"


def test_resolve_frontend_framework_normalizes_value() -> None:
    """Metadata value is normalized (lowercased, valid values only)."""
    assert resolve_frontend_framework({"framework_target": "React"}, "") == "react"
    assert resolve_frontend_framework({"framework_target": "ANGULAR"}, "") == "angular"
