"""Version constant for the nutrient data store.

Downstream consumers pin on this for cache invalidation (reader LRU
keys, plan cache vectors). Bump rules:

- MAJOR: schema reshape, nutrient enum removal or rename. Downstream
  caches must invalidate.
- MINOR: new nutrients added, FDC snapshot refresh, retention factor
  updates that do not remove existing entries.
- PATCH: override corrections, citation edits, non-behavioral fixes.
"""

from __future__ import annotations

NUTRIENT_DATA_VERSION = "1.0.0"
