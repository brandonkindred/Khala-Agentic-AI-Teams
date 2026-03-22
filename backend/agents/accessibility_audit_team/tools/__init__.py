"""
Tool schemas for the Digital Accessibility Audit Team.

Organized by domain:
- audit: Core orchestration tools
- web: Web testing tools
- mobile: Mobile testing tools
- at: Assistive technology verification tools
- standards: Standards mapping tools
- evidence: Evidence and repro tools
- remediation: Remediation tools
- qa: QA and consistency tools
- monitoring: Regression monitoring tools (ARM add-on)
- designsystem: Design system tools (ADSE add-on)
- training: Training tools (AET add-on)
"""

from .at import run_script
from .audit import (
    build_coverage_matrix,
    create_plan,
    export_backlog,
)
from .evidence import create_pack, generate_minimal_case
from .mobile import (
    check_font_scaling,
    check_touch_targets,
    record_screen_reader_flow,
)
from .qa import cluster_patterns, validate_finding
from .remediation import generate_regression_checks, suggest_fix
from .standards import map_wcag, tag_section508
from .web import (
    capture_dom_snapshot,
    check_reflow_zoom,
    compute_contrast_and_focus,
    record_keyboard_flow,
    run_automated_scan,
)

__all__ = [
    # Audit
    "create_plan",
    "build_coverage_matrix",
    "export_backlog",
    # Web
    "run_automated_scan",
    "record_keyboard_flow",
    "capture_dom_snapshot",
    "check_reflow_zoom",
    "compute_contrast_and_focus",
    # Mobile
    "record_screen_reader_flow",
    "check_touch_targets",
    "check_font_scaling",
    # AT
    "run_script",
    # Standards
    "map_wcag",
    "tag_section508",
    # Evidence
    "create_pack",
    "generate_minimal_case",
    # Remediation
    "suggest_fix",
    "generate_regression_checks",
    # QA
    "validate_finding",
    "cluster_patterns",
]
