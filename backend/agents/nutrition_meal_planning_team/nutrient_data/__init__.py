"""Nutrient data module — USDA FDC-backed nutrient store.

Provides per-food nutrient profiles (macros + micros), density
conversions, and cooking-method retention factors. Data flows from
FDC CSV snapshots through a normalisation pipeline into Postgres;
runtime readers serve the meal-planning orchestrator with cached
single-row lookups.
"""

from __future__ import annotations

from .version import NUTRIENT_DATA_VERSION

__all__ = ["NUTRIENT_DATA_VERSION"]
