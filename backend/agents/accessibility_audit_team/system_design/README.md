# Accessibility Audit Team — System Design

This folder documents the architectural and design decisions behind the
**Digital Accessibility Audit Team** (`backend/agents/accessibility_audit_team/`).
It is a multi-agent system that delivers repeatable, evidence-backed
**WCAG 2.2** and **Section 508** audits for web and mobile applications.

The team is mounted at **`/api/accessibility-audit`** via the `accessibility_audit`
entry in `backend/unified_api/config.py`. Its main entry point is the
`AccessibilityAuditOrchestrator` class in `orchestrator.py`, which coordinates
**8 core specialist agents** plus **3 optional add-ons** through a sequential
**5-phase workflow** with state persistence after every phase.

## Read Order

Pick a starting point based on what you need to understand:

| You are... | Start with | Then read |
|---|---|---|
| A new contributor onboarding to the team | [`01-architecture.md`](./01-architecture.md) | `02` → `04` |
| An SRE/ops engineer debugging a stuck run | [`02-system-design.md`](./02-system-design.md) | `04` |
| A PM or compliance stakeholder scoping a capability | [`03-use-cases.md`](./03-use-cases.md) | `01` |
| Debugging a specific audit request end-to-end | [`04-flow.md`](./04-flow.md) | `02` |

## Documents

1. **[`01-architecture.md`](./01-architecture.md)** — Component architecture.
   Who are the agents, what they own, how they are grouped into Lane A / Lane B /
   Quality, and what shared services they depend on.

2. **[`02-system-design.md`](./02-system-design.md)** — Runtime behavior.
   The 5 phases, the state machine, the artifact-store persistence model, and
   how the `MessageBus` decouples inter-agent coordination.

3. **[`03-use-cases.md`](./03-use-cases.md)** — Actors and capabilities.
   Who calls the team (humans and systems), which API endpoints realize each
   use case, and how add-ons expose opt-in capabilities.

4. **[`04-flow.md`](./04-flow.md)** — End-to-end request flow.
   A sequence diagram that traces a single audit from `POST /audit/create`
   through every phase, every artifact write, and back to the client.

## Acronym Glossary

### Core specialist agents (always on)

| Code | Name | Responsibility |
|---|---|---|
| **APL** | Accessibility Program Lead | Scope, audit plan, coverage matrix, final executive summary and roadmap |
| **WAS** | Web Audit Specialist | Web testing: scans, keyboard/focus/contrast, reflow |
| **MAS** | Mobile Accessibility Specialist | iOS/Android screen-reader, touch targets, font scaling |
| **ATS** | Assistive Technology Specialist | "Truth layer": NVDA/JAWS/VoiceOver/TalkBack verification |
| **SLMS** | Standards & Legal Mapping Specialist | WCAG 2.2 SC mapping + Section 508 crosswalk with confidence |
| **REE** | Reproduction & Evidence Engineer | Evidence packs, screenshots, videos, DOM/a11y-tree snapshots, minimal reproducers |
| **RA** | Remediation Advisor | Fix recipes, acceptance criteria, test plans |
| **QCR** | QA & Consistency Reviewer | Deduplication, pattern clustering, quality gate |

### Optional add-on agents (enabled via `enable_addons=True`)

| Code | Name | Responsibility |
|---|---|---|
| **ARM** | Accessibility Regression Monitor | Continuous monitoring: baselines, diffs, alerts |
| **AET** | Accessibility Education & Training | Mines findings patterns into training modules |
| **ADSE** | Accessible Design System Engineer | Hardens design system components with a11y contracts |

### Other terms

- **Lane A / Lane B** — the two-lane execution model. Lane A (WAS, MAS) is
  fast and broad ("coverage"); Lane B (ATS, SLMS, REE, RA) is deep and rigorous
  ("credibility"). QCR is the gatekeeper between them.
- **Finding** — a single accessibility issue. Not "reportable" until it has
  repro steps, expected vs actual, user impact, at least one evidence artifact,
  a WCAG mapping with confidence, and remediation guidance. See `models.py`
  `Finding`.
- **Pattern** — a cluster of findings that share a root cause. Assigned by
  QCR. See `models.py` `PatternCluster`.
- **AT** — Assistive Technology (screen readers, switch devices, etc.).
- **SC** — WCAG Success Criterion, e.g. `2.4.7 Focus Visible`.

## Keep This Updated When...

- [ ] A new specialist or add-on agent is added → update `01-architecture.md`
      component inventory and the glossary above.
- [ ] The phase sequence in `orchestrator.py` `_run_audit_phases` changes →
      update `02-system-design.md` state diagram and `04-flow.md` sequence.
- [ ] A new `ArtifactType` or `RetentionPolicy` is added in `artifact_store.py`
      → update `02-system-design.md` persistence section.
- [ ] A new API endpoint is added in `api/main.py` → update `03-use-cases.md`
      use-case-to-endpoint mapping table.
- [ ] An existing agent's responsibilities change → update its row in the
      glossary above and in `01-architecture.md`.

## Source of Truth

When these docs and the code disagree, **the code wins.** The existing
[`../README.md`](../README.md) has the runnable quick-start, API examples,
and full taxonomy. This folder captures the *why* those docs imply but do
not explain in depth.
