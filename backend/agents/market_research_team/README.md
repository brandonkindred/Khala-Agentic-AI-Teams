# Market Research & Concept Viability Team

This team is designed as a **human-AI collaborative workflow** for discovering what users need and whether a product concept is viable.

## Recommended structure

Use a **single orchestrator with selectable topology**:

- `unified` (default): one cohesive team pass when speed is the priority.
- `split`: explicit discovery and viability phases when rigor and handoff checkpoints matter.

This gives you both options without duplicating implementation.

## What this team does

1. Ingests interview transcripts (inline or via `transcript_folder_path`).
2. Extracts UX insights (jobs, pains, desired outcomes).
3. Synthesizes user psychology/market signals.
4. Produces a viability recommendation with confidence and rationale.
5. Generates practical research scripts for the next sprint.
6. Waits for **human approval** before marking work ready for execution.

## API

Start:

```bash
uvicorn market_research_team.api.main:app --reload --host 0.0.0.0 --port 8010
```

Run:

```http
POST /market-research/run
```

Example payload:

```json
{
  "product_concept": "AI-powered interview synthesis workspace",
  "target_users": "product managers at B2B SaaS companies",
  "business_goal": "reduce time to validated roadmap decisions",
  "topology": "split",
  "transcript_folder_path": "./sample_transcripts",
  "human_approved": false,
  "human_feedback": "Need evidence about willingness to pay before greenlighting MVP"
}
```
