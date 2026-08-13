"""Always-on tests for Agent Studio Postgres schema export."""

from __future__ import annotations

from agent_platform.studio.postgres import SCHEMA


def test_schema_registers_drafts_and_conversation_tables() -> None:
    assert SCHEMA.team == "agent_studio"
    assert "agent_studio_conversations" in SCHEMA.table_names
    assert "agent_studio_drafts" in SCHEMA.table_names
    joined = "\n".join(SCHEMA.statements)
    assert "CREATE TABLE IF NOT EXISTS agent_studio_drafts" in joined
    assert "idx_agent_studio_drafts_user_updated" in joined
