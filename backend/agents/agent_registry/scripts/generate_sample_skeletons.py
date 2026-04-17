"""
One-shot script: for every manifest with ``inputs.schema_ref``, generate a
minimal ``default.json`` sample file if none exists yet.

Run from ``backend/``::

    python3 -m agent_registry.scripts.generate_sample_skeletons

Never clobbers hand-edited samples (skips when ``default.json`` already exists).
Prints a one-line summary per agent so reruns are reviewable.
"""

from __future__ import annotations

import json
import logging
import sys

from agent_registry import get_registry
from agent_registry.pydantic_examples import example_from_schema
from agent_registry.schema_resolver import SchemaResolutionError, resolve_schema

logger = logging.getLogger("generate_sample_skeletons")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    reg = get_registry()

    generated = 0
    skipped_existing = 0
    skipped_no_ref = 0
    failed = 0

    for manifest in reg.all():
        if not (manifest.inputs and manifest.inputs.schema_ref):
            skipped_no_ref += 1
            continue

        # Resolve to the real on-disk samples dir (handles team_dir naming mismatches
        # like "branding" → "branding_team/").
        samples_dir = reg._samples_dir(manifest.id)  # noqa: SLF001 — intentional use
        if samples_dir is None:
            failed += 1
            continue
        target = samples_dir / "default.json"
        if target.exists():
            logger.info("[skip existing] %s (%s)", manifest.id, target)
            skipped_existing += 1
            continue

        try:
            schema = resolve_schema(manifest.inputs.schema_ref)
        except SchemaResolutionError as exc:
            logger.warning(
                "[fail] %s — could not resolve %s: %s", manifest.id, manifest.inputs.schema_ref, exc
            )
            failed += 1
            continue

        example = example_from_schema(schema)
        samples_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
        logger.info("[wrote] %s", target)
        generated += 1

    logger.info(
        "done — generated=%d, skipped_existing=%d, skipped_no_ref=%d, failed=%d",
        generated,
        skipped_existing,
        skipped_no_ref,
        failed,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
