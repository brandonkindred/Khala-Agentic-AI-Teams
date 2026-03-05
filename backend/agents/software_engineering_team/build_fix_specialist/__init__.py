"""Build Fix Specialist: minimal, targeted edits for build/test failures.

Standard tool-agent contract
-----------------------------
All fix/patch tool agents follow the same interface:

    agent = ToolAgent(llm_client=llm)      # or ToolAgent() for stateless agents
    output = agent.run(input_model)        # returns output_model

Shared patch semantics use ``CodeEdit`` (file_path, old_text, new_text) for
cross-stack interoperability.  Consumers (backend, frontend, devops) apply
edits via a safe sequential-replace loop that validates ``old_text`` match
before writing.
"""

from .agent import BuildFixSpecialistAgent
from .models import BuildFixInput, BuildFixOutput, CodeEdit

__all__ = [
    "BuildFixSpecialistAgent",
    "BuildFixInput",
    "BuildFixOutput",
    "CodeEdit",
]
