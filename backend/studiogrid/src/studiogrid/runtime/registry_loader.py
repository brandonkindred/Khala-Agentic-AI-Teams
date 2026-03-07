from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class RegistryLoader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._registry = None

    def _load(self) -> dict[str, Any]:
        if self._registry is None:
            with (self.root / "workflows" / "agent_registry.yaml").open("r", encoding="utf-8") as f:
                self._registry = yaml.safe_load(f)
        return self._registry

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        agents = self._load().get("agents", {})
        return agents[agent_id]

    def list_agents(self) -> list[dict[str, Any]]:
        agents = self._load().get("agents", {})
        return [{"agent_id": agent_id, **cfg} for agent_id, cfg in agents.items()]

    def list_teams(self, *, available_only: bool = False) -> list[dict[str, Any]]:
        teams = self._load().get("teams", {})
        team_rows = [{"team_id": team_id, **cfg} for team_id, cfg in teams.items()]
        if available_only:
            team_rows = [team for team in team_rows if team.get("is_available", False)]
        return sorted(team_rows, key=lambda team: team["team_id"])

    def find_assisting_agents(
        self,
        *,
        problem_description: str,
        required_skills: list[str],
        requesting_agent_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        description_tokens = self._tokenize(problem_description)
        needed_skills = {skill.strip().lower() for skill in required_skills if skill.strip()}
        requesting_agent_teams = self._team_ids_for_agent(requesting_agent_id) if requesting_agent_id else set()

        scored: list[dict[str, Any]] = []
        for entry in self.list_agents():
            agent_skills = {skill.lower() for skill in entry.get("skills", [])}
            keyword_tokens = self._tokenize(" ".join(entry.get("keywords", [])))
            skill_matches = sorted(needed_skills.intersection(agent_skills))
            keyword_matches = sorted(description_tokens.intersection(keyword_tokens))

            if needed_skills and not skill_matches:
                continue

            base_score = (3 * len(skill_matches)) + len(keyword_matches)
            if base_score == 0 and description_tokens:
                continue

            candidate_teams = self._team_ids_for_agent(entry["agent_id"])
            shared_teams = sorted(requesting_agent_teams.intersection(candidate_teams))
            same_team_bonus = 100 if shared_teams else 0
            score = base_score + same_team_bonus

            scored.append(
                {
                    "agent_id": entry["agent_id"],
                    "score": score,
                    "description": entry.get("description", ""),
                    "skills": entry.get("skills", []),
                    "actions": entry.get("actions", []),
                    "resources": entry.get("resources", []),
                    "schemas": entry.get("schemas", []),
                    "teams": sorted(candidate_teams),
                    "match": {
                        "skills": skill_matches,
                        "keywords": keyword_matches,
                        "shared_teams": shared_teams,
                    },
                }
            )

        scored.sort(key=lambda item: (-item["score"], item["agent_id"]))
        if limit is not None:
            return scored[:limit]
        return scored

    def _team_ids_for_agent(self, agent_id: str | None) -> set[str]:
        if not agent_id:
            return set()
        teams = self._load().get("teams", {})
        return {
            team_id
            for team_id, team_cfg in teams.items()
            if agent_id in team_cfg.get("agent_ids", [])
        }

    @staticmethod
    def _tokenize(value: str) -> set[str]:
        cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
        return {part for part in cleaned.split() if part}

    def load_prompt(self, prompt_file: str) -> str:
        return (self.root / prompt_file).read_text(encoding="utf-8")
