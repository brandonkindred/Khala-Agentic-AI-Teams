from __future__ import annotations

from pathlib import Path

import yaml


class RegistryLoader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._registry = None

    def _load(self) -> dict:
        if self._registry is None:
            with (self.root / "workflows" / "agent_registry.yaml").open("r", encoding="utf-8") as f:
                self._registry = yaml.safe_load(f)
        return self._registry

    def get_agent(self, agent_id: str) -> dict:
        agents = self._load().get("agents", {})
        return agents[agent_id]

    def load_prompt(self, prompt_file: str) -> str:
        return (self.root / prompt_file).read_text(encoding="utf-8")
