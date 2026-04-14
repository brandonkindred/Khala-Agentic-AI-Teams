# System Design

This directory contains all architecture decision records (ADRs), feature specifications, and other design documents for the Khala platform.

## Directory Structure

```
system_design/
  specs/          # Feature specifications (SPEC-NNN)
  adr/            # Architecture Decision Records (ADR-NNN)
```

## Naming Conventions

| Document Type     | Pattern                              | Example                                          |
|-------------------|--------------------------------------|--------------------------------------------------|
| Feature Spec      | `specs/SPEC-NNN-short-title.md`      | `specs/SPEC-001-platform-hardening.md`           |
| ADR               | `adr/ADR-NNN-short-title.md`         | `adr/ADR-001-postgres-over-sqlite.md`            |

Numbers are zero-padded to 3 digits and assigned sequentially within each category.

## Feature Spec Template

Feature specs follow this structure:

1. **Metadata** -- title, status, author, date, priority
2. **Problem Statement** -- what prompted this work
3. **Current State** -- diagrams and analysis of the existing system
4. **Goals / Non-Goals** -- explicit scope boundary
5. **Detailed Design** -- work items grouped by priority, with diagrams
6. **Rollout Plan** -- phased timeline with checklists
7. **Verification** -- how to confirm the changes work end-to-end

## ADR Template

ADRs follow a lightweight format:

1. **Status** -- Proposed | Accepted | Deprecated | Superseded
2. **Context** -- what forces are at play
3. **Decision** -- what we decided
4. **Consequences** -- what follows from the decision
