"""Regression checks for Deepthought prompt contracts.

Preconditions:
    - ``prompts`` module is importable under the agents package path.

Postconditions:
    - Assertions document call-site expectations for prompt shape (prose vs JSON).
"""

from deepthought.prompts import DELIBERATION_SYSTEM_PROMPT


def test_deliberation_system_prompt_asks_for_structured_prose_not_json():
    """Deliberation notes are returned via ``complete()`` as prose for synthesis.

    Preconditions:
        - ``DELIBERATION_SYSTEM_PROMPT`` is the system prompt for ``_deliberate``.

    Postconditions:
        - Prompt requests structured prose and does not instruct JSON object output.
    """
    assert "structured prose" in DELIBERATION_SYSTEM_PROMPT
    assert "not JSON" in DELIBERATION_SYSTEM_PROMPT
    assert "produce a JSON object" not in DELIBERATION_SYSTEM_PROMPT
    for topic in (
        "Contradictions",
        "Gaps",
        "Agreements",
        "Quality flags",
        "Synthesis guidance",
    ):
        assert topic in DELIBERATION_SYSTEM_PROMPT
