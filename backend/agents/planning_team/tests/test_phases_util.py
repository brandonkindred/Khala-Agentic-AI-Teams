"""Unit tests for planning_team.phases._util (shared phase-context helpers)."""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_team.models import ClientContext  # noqa: E402
from planning_team.phases._util import as_client_context, assemble_material  # noqa: E402

# --- as_client_context ------------------------------------------------------


def test_as_client_context_none_passthrough():
    assert as_client_context(None) is None


def test_as_client_context_instance_passthrough():
    cc = ClientContext(problem_summary="X")
    assert as_client_context(cc) is cc


def test_as_client_context_coerces_dict():
    result = as_client_context({"problem_summary": "Need X"})
    assert isinstance(result, ClientContext)
    assert result.problem_summary == "Need X"


# --- assemble_material -------------------------------------------------------


def test_assemble_material_brief_and_spec_concatenated():
    material = assemble_material({"initial_brief": "Brief text", "spec_content": "Spec text"})
    assert material == "Brief:\nBrief text\n\nSpec:\nSpec text"


def test_assemble_material_brief_only():
    assert assemble_material({"initial_brief": "Brief text"}) == "Brief text"


def test_assemble_material_spec_only():
    assert assemble_material({"spec_content": "Spec text"}) == "Spec text"


def test_assemble_material_default_fallback():
    assert assemble_material({}) == "No brief or spec provided."


def test_assemble_material_extra_fallback_used_before_default():
    assert assemble_material({}, extra_fallback="Problem summary") == "Problem summary"


def test_assemble_material_brief_or_spec_takes_priority_over_extra_fallback():
    assert (
        assemble_material({"initial_brief": "Brief text"}, extra_fallback="Problem summary")
        == "Brief text"
    )


def test_assemble_material_custom_default():
    assert assemble_material({}, default="Custom default") == "Custom default"
