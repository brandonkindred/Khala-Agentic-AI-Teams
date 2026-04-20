# API Mapping

UI actions mapped to HTTP endpoints.

## Blogging API (port 8000)

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Submit research form | POST | `/research-and-review` | `ResearchAndReviewRequest` | `ResearchAndReviewResponse` (research **+ planning**; titles/outline from `ContentPlan`) |
| Submit full pipeline form | POST | `/full-pipeline` | `FullPipelineRequest` | `FullPipelineResponse` (includes optional `content_plan_summary`) |
| Health check | GET | `/health` | - | `BloggingHealthResponse` (`status`, optional `brand_spec_configured`) |

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
| Submit market research job | POST | `/market-research/run` | `RunMarketResearchRequest` | `{job_id, status}` |
| Poll job status | GET | `/market-research/status/{job_id}` | - | `{status, result?: TeamOutput, error?}` |
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

## Personal Assistant API (port 8015)

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Submit assistant request | POST | `/assistant/jobs?user_id={user_id}` | `AssistantRequest` | `{job_id, status}` |
| Poll assistant job | GET | `/assistant/jobs/{job_id}` | - | `AssistantJobStatus` (with `response: AssistantResponse` on completion) |
| Get user profile | GET | `/users/{user_id}/profile` | - | `UserProfile` |
| Update user profile | PUT | `/users/{user_id}/profile` | `UserProfile` | `UserProfile` |
| Get tasks | GET | `/users/{user_id}/tasks` | - | `TaskList` |
| Add tasks from text | POST | `/users/{user_id}/tasks/parse` | `TaskParseRequest` | `TaskList` |
| Toggle task complete | PUT | `/users/{user_id}/tasks/{task_id}/toggle` | - | `TaskItem` |
| Get calendar events | GET | `/users/{user_id}/calendar` | - | `CalendarEvent[]` |
| Parse calendar from text | POST | `/users/{user_id}/calendar/parse` | `CalendarParseRequest` | `CalendarParseResponse` |
| Get wishlist | GET | `/users/{user_id}/wishlist` | - | `WishlistItem[]` |
| Add wishlist item | POST | `/users/{user_id}/wishlist` | `WishlistItem` | `WishlistItem` |
| Search deals | POST | `/users/{user_id}/deals/search` | `DealSearchRequest` | `DealSearchResponse` |
| Get reservations | GET | `/users/{user_id}/reservations` | - | `Reservation[]` |
| Create reservation | POST | `/users/{user_id}/reservations` | `ReservationRequest` | `Reservation` |
| Parse reservation from text | POST | `/users/{user_id}/reservations/parse` | `ReservationParseRequest` | `ReservationParseResponse` |
| Get documents | GET | `/users/{user_id}/documents` | - | `GeneratedDocument[]` |
| Generate document | POST | `/users/{user_id}/documents/generate` | `DocumentGenerateRequest` | `GeneratedDocument` |
| Health check | GET | `/health` | - | `HealthResponse` |

## Unified API Server (port 8080)

The unified API mounts all team APIs under namespaced prefixes:

| Team | Prefix | Example Endpoint |
|------|--------|-----------------|
| Blogging | `/api/blogging` | `/api/blogging/research-and-review` |
| Software Engineering | `/api/software-engineering` | `/api/software-engineering/run-team` |
| Personal Assistant | `/api/personal-assistant` | `/api/personal-assistant/assistant/jobs?user_id={id}` |
| Market Research | `/api/market-research` | `/api/market-research/run` |
| SOC2 Compliance | `/api/soc2-compliance` | `/api/soc2-compliance/soc2-audit/run` |
| Social Marketing | `/api/social-marketing` | `/api/social-marketing/run` |
| Branding | `/api/branding` | `/api/branding/run` |
| Agent Provisioning | `/api/agent-provisioning` | `/api/agent-provisioning/provision` |

| UI Action | Method | Path | Request | Response |
|-----------|--------|------|---------|----------|
| Get API info | GET | `/` | - | `ApiInfoResponse` |
| Health check (all teams) | GET | `/health` | - | `UnifiedHealthResponse` |
| List teams | GET | `/teams` | - | `TeamListResponse` |
