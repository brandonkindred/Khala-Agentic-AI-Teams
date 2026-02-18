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

The **orchestrator** loads the repository (code, config, docs), runs each TSC agent in sequence, then invokes the Report Writer to produce the final deliverable.

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
| `SOC2_LLM_MODEL` | Ollama model name | `llama3.1` |
| `SOC2_LLM_BASE_URL` | Ollama API base URL | `http://127.0.0.1:11434` |
| `SOC2_LLM_TIMEOUT` | Request timeout in seconds | `300` |

Use `SOC2_LLM_PROVIDER=dummy` for testing without an LLM (returns empty findings and a placeholder next-steps document).

Example with Ollama:

```bash
# Ensure Ollama is running and a model is available, e.g.:
# ollama run llama3.1

export SOC2_LLM_PROVIDER=ollama
export SOC2_LLM_MODEL=llama3.1
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
├── llm_client.py      # Ollama / Dummy LLM client
├── agents.py          # Security, Availability, PI, Confidentiality, Privacy TSC agents + ReportWriter
├── orchestrator.py    # SOC2AuditOrchestrator, run_soc2_audit()
├── api/
│   └── main.py        # FastAPI: POST /soc2-audit/run, GET /soc2-audit/status/{job_id}
└── README.md
```
