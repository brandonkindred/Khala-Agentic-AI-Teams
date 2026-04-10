# Quality Gates

Cross-cutting agents that review implementation output. None are task assignees; they are invoked **inside** backend and frontend per-task workflows (and optionally by Tech Lead).

| Agent | Role | Used in |
|-------|------|--------|
| **Code Review** | Spec/standards/acceptance criteria | Backend and frontend per-task workflows |
| **QA Expert** | Bugs, tests, README | Backend and frontend per-task workflows |
| **Cybersecurity Expert** | Security review | Backend and frontend per-task; plus full codebase at end |
| **Accessibility Expert** | WCAG 2.2, frontend | Frontend per-task only (lives under `frontend_team/`) |
| **Acceptance Verifier** | Per-criterion evidence | Backend and frontend (optional) |
| **DbC Comments** | Pre/postconditions, invariants | Backend and frontend per-task |

All agents consume implementation output and return review results. For discoverability, use:

```python
from quality_gates import CodeReviewAgent, QAExpertAgent, CybersecurityExpertAgent, AcceptanceVerifierAgent, DbcCommentsAgent
# Accessibility: from frontend_team.accessibility_agent import AccessibilityExpertAgent
```

## Khala platform

This package is part of the [Khala](../../../../README.md) monorepo (Unified API, Angular UI, and full team index).
