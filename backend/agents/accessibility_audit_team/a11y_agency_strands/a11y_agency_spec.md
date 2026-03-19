# Strands-Native Accessibility Agency

This module implements a deterministic Strands-aligned workflow coordinator with specialist agents exposed as tools.

## Workflow Steps
1. `run_discovery()`
2. `run_inventory_setup()`
3. `run_component_audit()`
4. `run_journey_assessment()`
5. `run_page_audit()`
6. `run_architecture_audit()`
7. `run_infrastructure_audit()`
8. `run_wcag_coverage()`
9. `run_508_mapping()`
10. `run_scoring_and_prioritization()`
11. `run_reporting()`
12. `request_human_approval()`
13. `run_delivery()`
14. `run_retest_cycle()`

## Required Tools
All required local tools from the specification are implemented under `app/tools/`.

## Quality Gates
- Reporting is blocked unless component, journey, page, and WCAG coverage phases are complete.
- Delivery is blocked unless reporting, remediation, Section 508 mapping, and approval phases are complete.
