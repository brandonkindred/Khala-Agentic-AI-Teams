"""Temporal task queue and workflow IDs for the personal assistant team.

``TASK_QUEUE`` is a literal (``{team}-queue``) matching the shared registry
convention (``shared_temporal.teams_registry.start_all_team_workers`` uses
``f"{team}-queue"``). It is deliberately not read from the environment: the
package ``__init__`` imports this module, and the temporalio workflow sandbox
replays that package during workflow registration — a top-level ``os.getenv``
here would trip the sandbox.
"""

TASK_QUEUE = "personal_assistant-queue"
WORKFLOW_ID_PREFIX_ASSISTANT = "pa-assistant-"
