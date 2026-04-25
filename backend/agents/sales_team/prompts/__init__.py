"""Sales-team system prompts and task templates, one module per specialist.

Each leaf module exports:

- ``SYSTEM_PROMPT``: methodology-rich system prompt (with rendered few-shots
  appended at module load when ``FEWSHOT_EXAMPLES`` is non-empty).
- ``TASK_TEMPLATE`` (or named variants): ``.format()``-ready user-prompt
  template with named placeholders.
- ``FEWSHOT_EXAMPLES``: ``list[tuple[dict, dict]]`` of input/output pairs.

This package re-exports each module's constants under a disambiguated name so
``agents.py`` can import them all from a single statement.
"""

from .closer import (
    SYSTEM_PROMPT as CLOSER_SYSTEM_PROMPT,
)
from .closer import (
    TASK_TEMPLATE as CLOSER_TASK_TEMPLATE,
)
from .coach import (
    SYSTEM_PROMPT as COACH_SYSTEM_PROMPT,
)
from .coach import (
    TASK_TEMPLATE as COACH_TASK_TEMPLATE,
)
from .decision_maker_mapper import (
    SYSTEM_PROMPT as DECISION_MAKER_MAPPER_SYSTEM_PROMPT,
)
from .decision_maker_mapper import (
    TASK_TEMPLATE as DECISION_MAKER_MAPPER_TASK_TEMPLATE,
)
from .discovery import (
    SYSTEM_PROMPT as DISCOVERY_SYSTEM_PROMPT,
)
from .discovery import (
    TASK_TEMPLATE as DISCOVERY_TASK_TEMPLATE,
)
from .dossier_builder import (
    SYSTEM_PROMPT as DOSSIER_BUILDER_SYSTEM_PROMPT,
)
from .dossier_builder import (
    TASK_TEMPLATE as DOSSIER_BUILDER_TASK_TEMPLATE,
)
from .nurture import (
    SYSTEM_PROMPT as NURTURE_SYSTEM_PROMPT,
)
from .nurture import (
    TASK_TEMPLATE as NURTURE_TASK_TEMPLATE,
)
from .outreach import (
    SYSTEM_PROMPT as OUTREACH_SYSTEM_PROMPT,
)
from .outreach import (
    TASK_TEMPLATE as OUTREACH_TASK_TEMPLATE,
)
from .proposal import (
    SYSTEM_PROMPT as PROPOSAL_SYSTEM_PROMPT,
)
from .proposal import (
    TASK_TEMPLATE as PROPOSAL_TASK_TEMPLATE,
)
from .prospector import (
    PROSPECT_COMPANIES_TASK_TEMPLATE,
    PROSPECT_TASK_TEMPLATE,
)
from .prospector import (
    SYSTEM_PROMPT as PROSPECTOR_SYSTEM_PROMPT,
)
from .qualifier import (
    SYSTEM_PROMPT as QUALIFIER_SYSTEM_PROMPT,
)
from .qualifier import (
    TASK_TEMPLATE as QUALIFIER_TASK_TEMPLATE,
)

__all__ = [
    "CLOSER_SYSTEM_PROMPT",
    "CLOSER_TASK_TEMPLATE",
    "COACH_SYSTEM_PROMPT",
    "COACH_TASK_TEMPLATE",
    "DECISION_MAKER_MAPPER_SYSTEM_PROMPT",
    "DECISION_MAKER_MAPPER_TASK_TEMPLATE",
    "DISCOVERY_SYSTEM_PROMPT",
    "DISCOVERY_TASK_TEMPLATE",
    "DOSSIER_BUILDER_SYSTEM_PROMPT",
    "DOSSIER_BUILDER_TASK_TEMPLATE",
    "NURTURE_SYSTEM_PROMPT",
    "NURTURE_TASK_TEMPLATE",
    "OUTREACH_SYSTEM_PROMPT",
    "OUTREACH_TASK_TEMPLATE",
    "PROPOSAL_SYSTEM_PROMPT",
    "PROPOSAL_TASK_TEMPLATE",
    "PROSPECTOR_SYSTEM_PROMPT",
    "PROSPECT_TASK_TEMPLATE",
    "PROSPECT_COMPANIES_TASK_TEMPLATE",
    "QUALIFIER_SYSTEM_PROMPT",
    "QUALIFIER_TASK_TEMPLATE",
]
