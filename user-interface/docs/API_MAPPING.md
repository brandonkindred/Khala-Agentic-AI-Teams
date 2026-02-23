# API Mapping

UI actions mapped to HTTP endpoints.

## Blogging API (port 8000)

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Submit research form | POST | `/research-and-review` | `ResearchAndReviewRequest` | `ResearchAndReviewResponse` |
| Submit full pipeline form | POST | `/full-pipeline` | `FullPipelineRequest` | `FullPipelineResponse` |
| Health check | GET | `/health` | - | `{ status: "ok" }` |

## Software Engineering Team API (port 8000)

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Start team | POST | `/run-team` | `RunTeamRequest` | `RunTeamResponse` |
| List running/pending jobs | GET | `/run-team/jobs` | - | `RunningJobsResponse` |
| Poll job status | GET | `/run-team/{job_id}` | - | `JobStatusResponse` |
| Retry failed tasks | POST | `/run-team/{job_id}/retry-failed` | - | `RetryResponse` |
| Re-plan with clarifications | POST | `/run-team/{job_id}/re-plan-with-clarifications` | `RePlanWithClarificationsRequest` | `RunTeamResponse` |
| Create clarification session | POST | `/clarification/sessions` | `ClarificationCreateRequest` | `ClarificationResponse` |
| Send clarification message | POST | `/clarification/sessions/{id}/messages` | `ClarificationMessageRequest` | `ClarificationResponse` |
| Get clarification session | GET | `/clarification/sessions/{id}` | - | `ClarificationSessionResponse` |
| Get execution tasks | GET | `/execution/tasks` | - | `Record<string, unknown>` |
| Execution stream (SSE) | GET | `/execution/stream` | - | Event stream |
| **Backend-Code-V2:** Run backend-code-v2 team | POST | `/backend-code-v2/run` | `BackendCodeV2RunRequest` | `BackendCodeV2RunResponse` |
| **Backend-Code-V2:** Poll job status | GET | `/backend-code-v2/status/{job_id}` | - | `BackendCodeV2StatusResponse` |
| **Planning-V2:** Run planning-v2 workflow | POST | `/planning-v2/run` | `PlanningV2RunRequest` | `PlanningV2RunResponse` |
| **Planning-V2:** Poll job status | GET | `/planning-v2/status/{job_id}` | - | `PlanningV2StatusResponse` |
| **Planning-V2:** List planning-v2 jobs | GET | `/planning-v2/jobs` | - | `RunningJobsResponse` |
| **Planning-V2:** Get job result (phase results) | GET | `/planning-v2/result/{job_id}` | - | `PlanningV2ResultResponse` |
| Health check | GET | `/health` | - | `{ status: "ok" }` |

## Market Research API (port 8011)

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Run market research | POST | `/market-research/run` | `RunMarketResearchRequest` | `TeamOutput` |
| Health check | GET | `/health` | - | `{ status: "ok" }` |

## SOC2 Compliance API (port 8020)

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Start audit | POST | `/soc2-audit/run` | `RunAuditRequest` | `RunAuditResponse` |
| Poll audit status | GET | `/soc2-audit/status/{job_id}` | - | `AuditStatusResponse` |
| Health check | GET | `/health` | - | `{ status: "ok" }` |

## Social Media Marketing API (port 8010)

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Run marketing team | POST | `/social-marketing/run` | `RunMarketingTeamRequest` | `RunMarketingTeamResponse` |
| Poll job status | GET | `/social-marketing/status/{job_id}` | - | `MarketingJobStatusResponse` |
| Ingest performance | POST | `/social-marketing/performance/{job_id}` | `PerformanceIngestRequest` | `PerformanceIngestResponse` |
| Revise | POST | `/social-marketing/revise/{job_id}` | `ReviseMarketingTeamRequest` | `RunMarketingTeamResponse` |
| Health check | GET | `/health` | - | `{ status: "ok" }` |
