"""
Add-on agents for the Digital Accessibility Audit Team.

Add-on agents provide extended functionality beyond the core audit workflow:

- AET (Training Agent): Mines patterns and builds training modules
- ARM (Monitoring Agent): Continuous regression monitoring with baseline diffing
- ADSE (Design System Agent): Hardens design system components with a11y contracts
"""

from .training_agent import AccessibilityTrainingAgent
from .monitoring_agent import AccessibilityMonitoringAgent
from .design_system_agent import AccessibleDesignSystemAgent

__all__ = [
    "AccessibilityTrainingAgent",
    "AccessibilityMonitoringAgent",
    "AccessibleDesignSystemAgent",
]
