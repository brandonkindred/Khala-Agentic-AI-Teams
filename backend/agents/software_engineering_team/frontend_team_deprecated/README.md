# Frontend Team

Frontend sub-orchestration for the software engineering team. Runs a contract-first pipeline from clarification through implementation, quality gates, and handoff for frontend tasks.

## Objective

Operate like an expert front-end squad that can take implementation-ready tasks from planning/design agents and deliver merge-ready React or Angular code with strong UX, accessibility, performance, API/state integration, and testing discipline.

## Design Principles

1. **Contract-first execution**: every task should include user goal, scoped UI behavior, interaction states, ACs, framework target, styling constraints, and accessibility requirements.
2. **Design/code collaboration**: micro UI decisions are allowed only within design-system guardrails and must be documented as assumptions.
3. **Framework-native implementation**: React and Angular paths share concepts but must follow native framework idioms.
4. **State/API are first-class**: a feature is incomplete without loading/error/empty states, robust data flow, and safe API handling.
5. **Hard quality gates**: AC coverage, tests, accessibility review, code review, and design-system compliance are required before merge.

## Agent Roster

| Agent | Role |
|-------|------|
| **Frontend Team Lead (Orchestrator)** | Task intake, framework path selection, sequencing, and gate enforcement |
| **Frontend Task Clarifier** | Validates task completeness and kicks back unclear requests |
| **Micro UI Design Agent** | Small, bounded UI layout/composition decisions within design system |
| **UX Interaction Agent** | Defines interaction flows, transitions, and keyboard/focus behavior |
| **UX Designer** | User flows, interaction patterns, and experience design from task and spec |
| **UI Designer** | Visual design and component specs; consumes UX output |
| **Design System** | Component library alignment and consistency |
| **Frontend Architect** | Technical architecture decisions (structure, patterns, state management) |
| **React Implementation Agent** | Production-grade React implementation (hooks/state/forms/tests) |
| **Angular Implementation Agent** | Production-grade Angular implementation (RxJS/reactive forms/tests) |
| **Feature Agent** | Primary coding agent (FrontendExpertAgent) using framework target from task input |
| **State Management Agent** | Local/shared/server state design and invariants |
| **API Integration Agent** | Typed DTO mapping, API client integration, error mapping |
| **Front-End Test Engineer** | AC-to-test trace, unit/component/integration coverage |
| **Front-End Code Review Agent** | Senior framework-fit and maintainability review |
| **UX Engineer** | Polish pass: micro-interactions, consistency, UX refinement |
| **Performance Engineer** | Bundle size, rendering optimization, performance review |
| **Build/Release** | CI/CD and release planning; writes `plan/frontend_build_release.md` |
| **Accessibility Agent** | WCAG 2.2 compliance; reviews frontend per task |
| **Documentation & Handoff Agent** | Completion package, QA notes, and git provenance metadata |

## Pipeline Order

```
Team Lead → Task Clarifier → Micro UI Design → UX Interaction → Design System/State/API planning → Framework Implementation (React or Angular) → UX Engineer → Performance → Quality Gates → Build/Release → Handoff
```

Design phase (UX → UI → Design System) is **skipped for lightweight tasks** (fix, patch, refactor, etc.) to speed up implementation-only work.

## Quality Gates Integration

The frontend workflow invokes cross-cutting agents from [quality_gates/](../quality_gates/README.md):

- **Code Review** – Spec/standards/acceptance criteria
- **QA Expert** – Bugs, unit/integration tests, README
- **Cybersecurity Expert** – Frontend security checklist (CSP, token storage, PKCE, sanitization)
- **Accessibility Expert** – WCAG 2.2, keyboard nav, screen reader behavior
- **Acceptance Verifier** – Per-criterion evidence
- **DbC Comments** – Pre/postconditions on public APIs

## Lightweight Task Path

Tasks with keywords like `fix`, `resolve`, `update`, `patch`, `refactor` and short descriptions skip the design phase. The orchestrator goes directly to Frontend Architect → Feature Implementation → quality gates.

## Required Task Input

Frontend tasks are expected to carry the normalized contract fields below:

- `task_id`, `title`, `priority`, `framework_target` (`react` | `angular` | `either`)
- `repo_context` (app + module paths)
- `goal.summary`
- `scope.included` and `scope.excluded`
- `constraints` (framework versions, styling system, form handling, API contract ref)
- `acceptance_criteria`
- `non_functional_requirements` (performance, accessibility, analytics)
- `dependencies`
- `test_requirements`
- `risk_flags`

If these are missing or vague, the Task Clarifier should return specific clarification requests before coding begins.

## Completion Package Output

Each completed task should include:

- `task_id`, `status`, `framework_used`
- `files_changed`
- `acceptance_criteria_trace` (criterion -> implementation refs + tests)
- `quality_gates` status (lint/types/tests/a11y/ux/review)
- `notes`, `risks_remaining`
- `git_operations` (branch, commits, merge metadata)

## See Also

- [Software Engineering Team README](../README.md) – Full SDLC, orchestrator, and run instructions
- [Quality Gates](../quality_gates/README.md) – Cross-cutting review agents

## Khala platform

This package is part of the [Khala](../../../../README.md) monorepo (Unified API, Angular UI, and full team index).
