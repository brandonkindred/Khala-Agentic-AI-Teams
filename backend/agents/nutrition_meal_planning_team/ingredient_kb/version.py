"""Version constant for the ingredient knowledge base.

SPEC-005 §4.10. Downstream consumers pin on this:

- SPEC-007 guardrail: ``guardrail_version`` stamp on stored
  recommendations.
- SPEC-009 recipe rollup: ``recipe_cache_key`` hashes include it.
- SPEC-008 FDC ingestion: row-level ``data_version`` cross-check.

Bump rules:

- **MAJOR**: taxonomy change (enum removal or rename), canonical id
  rename. Downstream caches must invalidate.
- **MINOR**: enum addition, ingredient addition, alias addition,
  density addition.
- **PATCH**: citation edits, typos, non-behavioral fixes.
"""

from __future__ import annotations

KB_VERSION = "1.0.0"
