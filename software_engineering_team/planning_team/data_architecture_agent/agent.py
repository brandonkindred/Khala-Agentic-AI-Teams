"""Data Architecture and Engineering agent."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import DataArchitectureInput, DataArchitectureOutput
from .prompts import DATA_ARCHITECTURE_PROMPT

logger = logging.getLogger(__name__)


class DataArchitectureAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: DataArchitectureInput) -> DataArchitectureOutput:
        logger.info("Data Architecture: starting for %s", input_data.requirements_title)
        context = [
            f"**Product:** {input_data.requirements_title}",
            "**Architecture:**",
            (input_data.architecture_overview or "")[:3000],
            "**Spec:**",
            (input_data.spec_content or "")[:5000],
        ]
        data: Dict[str, Any] = self.llm.complete_json(
            DATA_ARCHITECTURE_PROMPT + "\n\n---\n\n" + "\n".join(context),
            temperature=0.2,
        ) or {}
        out = DataArchitectureOutput(
            schema_doc=(data.get("schema_doc") or "").strip(),
            migration_strategy=(data.get("migration_strategy") or "").strip(),
            analytics_taxonomy=(data.get("analytics_taxonomy") or "").strip(),
            data_lifecycle_policy=(data.get("data_lifecycle_policy") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            if out.schema_doc:
                (p / "data_schema.md").write_text("# Data Schema\n\n" + out.schema_doc, encoding="utf-8")
            content = f"# Data Architecture\n\n## Migration Strategy\n\n{out.migration_strategy or 'TBD'}\n\n## Analytics Taxonomy\n\n{out.analytics_taxonomy or 'TBD'}\n\n## Data Lifecycle Policy\n\n{out.data_lifecycle_policy or 'TBD'}"
            (p / "data_architecture.md").write_text(content, encoding="utf-8")
        return out
