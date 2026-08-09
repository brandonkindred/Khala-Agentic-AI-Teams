# SOC2 Compliance Audit Team

A multi-agent team that performs a **SOC2 compliance audit** on a code repository. The team reviews the repository against the five Trust Service Criteria (Security, Availability, Processing Integrity, Confidentiality, Privacy), identifies and documents gaps, and produces either:

- **A SOC2 compliance report** – when issues are found: executive summary, findings by criterion, and remediation recommendations.
- **A next-steps document** – when no material issues are found: guidance on next steps to pursue SOC2 certification (e.g. engaging a CPA firm, scoping the examination, documenting controls).

## Team structure

| Agent | Role |
|-------|------|
| **Security TSC Agent** | Audits against SOC2 Security (Common Criteria): access controls, encryption, change management, monitoring. |
| **Availability TSC Agent** | Audits availability-related controls: backup, recovery, monitoring, capacity. |
| **Processing Integrity TSC Agent** | Audits processing completeness, validity, accuracy, error handling. |
| **Confidentiality TSC Agent** | Audits handling of confidential information: classification, disclosure, disposal. |
| **Privacy TSC Agent** | Audits PII handling: collection, retention, consent, data subject rights. |
| **Report Writer Agent** | Compiles all findings into a compliance report or a next-steps-for-certification document. |

The audit is a fan-out/fan-in pipeline: load the repository (code, config, docs) → run the five TSC agents **in parallel** → invoke the Report Writer to produce the final deliverable. The pipeline stages live in `pipeline.py` as pure functions (`load_context`, `audit_criterion`, `run_all_criteria`, `write_report`, `assemble_result`) — a single source of truth shared by both execution modes.

## Execution modes

The same decomposed pipeline runs two ways:

- **Thread mode** (default): `SOC2AuditOrchestrator` runs the five TSC audits concurrently in a thread pool.
- **Temporal mode** (when `TEMPORAL_ADDRESS` is set): `Soc2AuditWorkflow` orchestrates the pipeline as durable, annotated activities — `soc2_load_repo` → five `soc2_audit_criterion` activities fanned out with `asyncio.gather` → `soc2_write_report` (with `soc2_mark_failed` as the terminal failure marker). Each activity wraps one `pipeline.py` step, so both modes produce the same `SOC2AuditResult`. The worker follows the shared `shared.temporal` pattern (registered in `shared.temporal.teams_registry`; task queue `soc2_compliance-queue`) and boots via the team_service entrypoint or the API `on_startup` backstop.

  `soc2_load_repo` loads the repository **once** and persists the `RepoContext` to a durable snapshot keyed by `job_id` (`context_snapshot.py`, on the `AGENT_CACHE` volume), then passes only the resolved repo path across the workflow. Each audit activity reads that same immutable snapshot, so the large uncapped code corpus never enters Temporal workflow history (payload/size safety) and every criterion audits an identical repo state even if the live checkout changes mid-run. The snapshot is cleaned up when the audit completes or fails. Set `AGENT_CACHE` in Temporal deployments so the snapshot survives a worker restart between activities.

The API's `POST /soc2-audit/run` dispatches to Temporal when it is enabled and falls back to thread mode otherwise; either way you poll `GET /soc2-audit/status/{job_id}` for the result.

## Quick start

### Dependencies

From the repository root:

```bash
pip install -r requirements.txt
```

### Run the audit (CLI / Python)

```python
from pathlib import Path
from soc2_compliance_team.orchestrator import run_soc2_audit

result = run_soc2_audit(Path("/path/to/your/repo"))
print(result.status)  # "completed" | "failed"
if result.compliance_report:
    print(result.compliance_report.raw_markdown)
if result.next_steps_document:
    print(result.next_steps_document.raw_markdown)
```

### Run via API

Start the server:

```bash
uvicorn soc2_compliance_team.api.main:app --host 0.0.0.0 --port 8020
```

Start an audit:

```bash
curl -X POST http://127.0.0.1:8020/soc2-audit/run \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/your/repo"}'
```

Poll for result:

```bash
curl http://127.0.0.1:8020/soc2-audit/status/<job_id>
```

Response includes `status` (`pending` | `running` | `completed` | `failed`) and, when `completed`, a `result` object with either `compliance_report` or `next_steps_document` (and `tsc_results` for per-criterion details).

## LLM configuration

The agents use an LLM to analyze repository content and generate findings and reports. By default the team uses **Ollama** (local). You can override behavior with environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `SOC2_LLM_PROVIDER` | `ollama` or `dummy` | `ollama` |
| `SOC2_LLM_MODEL` | Ollama model name | `deepseek-v4-flash:cloud` |
| `SOC2_LLM_BASE_URL` | Ollama API base URL | `http://127.0.0.1:11434` |
| `SOC2_LLM_TIMEOUT` | Request timeout in seconds | `300` |

Use `SOC2_LLM_PROVIDER=dummy` for testing without an LLM (returns empty findings and a placeholder next-steps document).

Example with Ollama:

```bash
# Ensure Ollama is running and a model is available, e.g.:
# ollama run deepseek-v4-flash:cloud

export SOC2_LLM_PROVIDER=ollama
export SOC2_LLM_MODEL=deepseek-v4-flash:cloud
uvicorn soc2_compliance_team.api.main:app --host 0.0.0.0 --port 8020
```

## Output

- **When the audit finds issues:** `result.compliance_report` contains:
  - `executive_summary`
  - `findings_by_tsc` (findings grouped by Security, Availability, etc.)
  - `recommendations_summary`
  - `raw_markdown` (full report for storage or display)

- **When no material issues are found:** `result.next_steps_document` contains:
  - `title`, `introduction`
  - `steps` (e.g. engage CPA firm, document controls, Type I/II examination)
  - `recommended_timeline`
  - `raw_markdown`

## Project layout

```
soc2_compliance_team/
├── __init__.py
├── models.py          # RepoContext, TSCFinding, TSCAuditResult, SOC2ComplianceReport, NextStepsDocument
├── repo_loader.py     # Load repository into RepoContext for agents
├── (uses llm_service for LLM client)
├── agents.py          # Security, Availability, PI, Confidentiality, Privacy TSC agents + ReportWriter
├── pipeline.py        # Decomposed pipeline steps shared by both execution modes
├── context_snapshot.py # Durable RepoContext snapshot (keyed by job_id) for Temporal fan-out
├── orchestrator.py    # SOC2AuditOrchestrator, run_soc2_audit() (thread-mode driver)
├── temporal/          # Temporal workflow + activities (shared.temporal pattern)
│   ├── activities.py  # soc2_load_repo, soc2_audit_criterion, soc2_write_report, soc2_mark_failed
│   ├── workflows.py   # Soc2AuditWorkflow (load → fan-out audits → report)
│   ├── worker.py      # start_soc2_temporal_worker_thread()
│   └── start_workflow.py
├── api/
│   └── main.py        # FastAPI: POST /soc2-audit/run, GET /soc2-audit/status/{job_id}
└── README.md
```

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
