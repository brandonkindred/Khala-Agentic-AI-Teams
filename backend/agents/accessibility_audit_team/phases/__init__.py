"""
Phase implementations for the Digital Accessibility Audit Team.

Phases:
- Intake (Phase 0): Create audit plan and coverage matrix
- Discovery (Phase 1): Wide coverage pass with scans and manual testing
- Verification (Phase 2): Deep AT verification
- Report Packaging (Phase 3): QA and final report
- Retest (Phase 4): Re-verification after fixes
"""

from .discovery import run_discovery_phase
from .intake import run_intake_phase
from .report_packaging import run_report_packaging_phase
from .retest import run_retest_phase
from .verification import run_verification_phase

__all__ = [
    "run_intake_phase",
    "run_discovery_phase",
    "run_verification_phase",
    "run_report_packaging_phase",
    "run_retest_phase",
]
