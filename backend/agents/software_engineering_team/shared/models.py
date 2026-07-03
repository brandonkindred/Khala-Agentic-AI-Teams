"""Compatibility shim: shared dev-pipeline models moved to ``shared_dev_models``.

The models now live in the neutral top-level package ``shared_dev_models`` so the
coding team can share them without importing this team. This module aliases the
old import path (``software_engineering_team.shared.models``) onto
``shared_dev_models.models`` via ``sys.modules`` so that:

    - existing ``from software_engineering_team.shared.models import Task`` imports
      keep working, and
    - ``software_engineering_team.shared.models.Task is shared_dev_models.models.Task``
      (identity / ``isinstance`` preserved).

New code should import from ``shared_dev_models`` directly.
"""

from __future__ import annotations

import sys

from shared_dev_models import models as _models

# Replace this module object with the neutral one so every access to
# ``software_engineering_team.shared.models`` resolves to the single definition.
sys.modules[__name__] = _models
