"""Compatibility shim: the Strands-model resolver moved into ``llm_service``.

``resolve_strands_model`` / ``resolve_text_mode_strands_model`` now live in
``llm_service.strands_model`` (they only ever wrapped ``llm_service``), so the
coding team can use them without importing this team. This aliases the old path
onto ``llm_service.strands_model`` via ``sys.modules`` so existing SE imports and
patches keep working. New code imports ``llm_service.strands_model``.
"""

from __future__ import annotations

import sys

from llm_service import strands_model as _strands_model

sys.modules[__name__] = _strands_model
