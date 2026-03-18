from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml


class RegistryLoader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._registry: dict | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self._registry is None:
            with self._lock:
                if self._registry is None:
                    with (self.root / "workflows" / "agent_registry.yaml").open("r", encoding="utf-8") as f:
                        self._registry = yaml.safe_load(f)
        return self._registry

    def get_agent(self, agent_id: str) -> dict:
        agents = self._load().get("agents", {})
        return agents[agent_id]

    def list_agents(self) -> list[dict[str, Any]]:
        agents = self._load().get("agents", {})
        return [{"agent_id": agent_id, **cfg} for agent_id, cfg in agents.items()]

    def find_assisting_agents(
        self, *, problem_description: str, required_skills: list[str], limit: int | None = None
    ) -> list[dict[str, Any]]:
        description_tokens = self._tokenize(problem_description)
        needed_skills = {skill.strip().lower() for skill in required_skills if skill.strip()}
        scored: list[dict[str, Any]] = []
        for entry in self.list_agents():
            agent_skills = {skill.lower() for skill in entry.get("skills", [])}
            keyword_tokens = self._tokenize(" ".join(entry.get("keywords", [])))
            skill_matches = sorted(needed_skills.intersection(agent_skills))
            keyword_matches = sorted(description_tokens.intersection(keyword_tokens))
            score = (3 * len(skill_matches)) + len(keyword_matches)
            if needed_skills and not skill_matches:
                continue
            if score == 0 and description_tokens:
                continue
            scored.append(
                {
                    "agent_id": entry["agent_id"],
                    "score": score,
                    "description": entry.get("description", ""),
                    "skills": entry.get("skills", []),
                    "actions": entry.get("actions", []),
                    "resources": entry.get("resources", []),
                    "schemas": entry.get("schemas", []),
                    "match": {
                        "skills": skill_matches,
                        "keywords": keyword_matches,
                    },
                }
            )

        scored.sort(key=lambda item: (-item["score"], item["agent_id"]))
        if limit is not None:
            return scored[:limit]
        return scored

    @staticmethod
    def _tokenize(value: str) -> set[str]:
        cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
        return {part for part in cleaned.split() if part}

    def load_prompt(self, prompt_file: str) -> str:
        return (self.root / prompt_file).read_text(encoding="utf-8")
