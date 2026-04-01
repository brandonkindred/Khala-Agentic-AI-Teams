"""
Accessibility Regression Monitor (ARM)

Continuous monitoring with baseline diffing and alerts.

Tools:
- monitor.create_baseline
- monitor.run_checks
- monitor.diff_against_baseline
- monitor.emit_alerts
"""

import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..models import (
    MonitoringBaseline,
    MonitoringDiff,
    MonitoringRunResult,
)


class AlertConfig(BaseModel):
    """Configuration for monitoring alerts."""

    severity_at_or_above: str = Field(default="High")
    new_only: bool = Field(default=True)
    destinations: List[str] = Field(
        default_factory=lambda: ["slack"],
        description="Alert destinations: slack, email, eventbus, webhook",
    )


class AccessibilityMonitoringAgent:
    """
    Accessibility Regression Monitor (ARM).

    Provides continuous accessibility monitoring:
    - Create baselines from audits or snapshots
    - Run periodic checks against baselines
    - Detect regressions (new issues)
    - Alert on critical changes

    This agent runs on a schedule or CI/CD trigger to catch
    accessibility regressions before they reach production.
    """

    agent_code = "ARM"
    agent_name = "Accessibility Regression Monitor"

    def __init__(self, llm_client: Optional[Any] = None):
        self.llm_client = llm_client
        self._baselines: Dict[str, MonitoringBaseline] = {}
        self._runs: Dict[str, MonitoringRunResult] = {}

    async def create_baseline(
        self,
        audit_id: str,
        env: Literal["stage", "prod"],
        targets: List[Dict[str, str]],
        checks: List[str] = None,
        viewport: Dict[str, int] = None,
    ) -> MonitoringBaseline:
        """
        Create a monitoring baseline.

        The baseline captures the current accessibility state of the targets,
        which is used for future regression detection.

        Args:
            audit_id: Associated audit ID
            env: Environment (stage or prod)
            targets: List of {url, journey} targets
            checks: Checks to run (axe, keyboard_flow, contrast_focus, a11y_tree)
            viewport: Viewport configuration

        Returns:
            MonitoringBaseline with snapshot references
        """
        checks = checks or ["axe", "keyboard_flow", "contrast_focus"]
        baseline_ref = f"baseline_{audit_id}_{uuid.uuid4().hex[:8]}"

        # In production, would run actual checks and capture snapshots
        snapshot_refs = [f"snapshot_{baseline_ref}_{i}" for i in range(len(targets))]

        baseline = MonitoringBaseline(
            baseline_ref=baseline_ref,
            audit_id=audit_id,
            env=env,
            targets=targets,
            checks=checks,
            snapshot_refs=snapshot_refs,
        )

        self._baselines[baseline_ref] = baseline
        return baseline

    async def run_checks(
        self,
        monitor_run_id: str,
        env: Literal["stage", "prod"],
        targets: List[Dict[str, str]],
        checks: List[str] = None,
    ) -> MonitoringRunResult:
        """
        Run monitoring checks against targets.

        Executes accessibility checks and captures the current state
        for comparison against the baseline.

        Returns:
            MonitoringRunResult with findings and results reference
        """
        checks = checks or ["axe", "keyboard_flow"]
        results_ref = f"results_{monitor_run_id}"

        # In production, would run actual checks
        findings = []

        run_result = MonitoringRunResult(
            run_id=monitor_run_id,
            baseline_ref="",  # Will be set when diffing
            env=env,
            results_ref=results_ref,
            findings=findings,
        )

        self._runs[monitor_run_id] = run_result
        return run_result

    async def diff_against_baseline(
        self,
        monitor_run_id: str,
        baseline_ref: str,
        results_ref: str,
        alert_threshold: AlertConfig = None,
    ) -> MonitoringDiff:
        """
        Compare monitoring run against baseline.

        Identifies:
        - New issues (regressions)
        - Resolved issues (improvements)
        - Unchanged issues (persistent)

        Returns:
            MonitoringDiff with categorized issues
        """
        alert_threshold = alert_threshold or AlertConfig()

        if baseline_ref not in self._baselines:
            return MonitoringDiff(
                run_id=monitor_run_id,
                baseline_ref=baseline_ref,
            )

        _baseline = self._baselines[baseline_ref]  # noqa: F841

        # In production, would compare actual results against _baseline
        new_issues = []
        resolved_issues = []
        unchanged_issues = []

        # Count alerts based on threshold
        alerts_triggered = 0
        if alert_threshold.new_only:
            for issue in new_issues:
                severity = issue.get("severity", "Low")
                if self._severity_meets_threshold(severity, alert_threshold.severity_at_or_above):
                    alerts_triggered += 1

        return MonitoringDiff(
            run_id=monitor_run_id,
            baseline_ref=baseline_ref,
            new_issues=new_issues,
            resolved_issues=resolved_issues,
            unchanged_issues=unchanged_issues,
            alerts_triggered=alerts_triggered,
        )

    async def emit_alerts(
        self,
        monitor_run_id: str,
        alerts: List[Dict[str, Any]],
        destination: str = "slack",
    ) -> Dict[str, Any]:
        """
        Emit alerts for monitoring issues.

        Supports multiple destinations:
        - slack: Slack webhook
        - email: Email notification
        - eventbus: Event bus message
        - webhook: Generic webhook

        Returns:
            Status of alert delivery
        """
        # In production, would actually send alerts
        sent = 0
        failed = 0

        for alert in alerts:
            # Simulate sending
            sent += 1

        return {
            "monitor_run_id": monitor_run_id,
            "destination": destination,
            "sent": sent,
            "failed": failed,
        }

    def _severity_meets_threshold(self, severity: str, threshold: str) -> bool:
        """Check if severity meets the alert threshold."""
        severity_order = ["Critical", "High", "Medium", "Low"]
        try:
            severity_idx = severity_order.index(severity)
            threshold_idx = severity_order.index(threshold)
            return severity_idx <= threshold_idx
        except ValueError:
            return False

    async def setup_scheduled_monitoring(
        self,
        baseline_ref: str,
        schedule: str = "daily",
        alert_config: AlertConfig = None,
    ) -> Dict[str, Any]:
        """
        Set up scheduled monitoring for a baseline.

        Args:
            baseline_ref: Baseline to monitor against
            schedule: Schedule frequency (hourly, daily, weekly)
            alert_config: Alert configuration

        Returns:
            Schedule configuration
        """
        alert_config = alert_config or AlertConfig()

        schedule_id = f"schedule_{uuid.uuid4().hex[:8]}"

        return {
            "schedule_id": schedule_id,
            "baseline_ref": baseline_ref,
            "schedule": schedule,
            "alert_config": alert_config.model_dump(),
            "status": "active",
            "next_run": "TBD",  # Would calculate actual next run time
        }

    async def get_monitoring_report(
        self,
        baseline_ref: str,
        time_range_days: int = 30,
    ) -> Dict[str, Any]:
        """
        Get monitoring report for a baseline over time.

        Returns trend data showing accessibility changes over time.
        """
        # Would query historical data in production
        return {
            "baseline_ref": baseline_ref,
            "time_range_days": time_range_days,
            "runs_count": 0,
            "trend": {
                "issues_introduced": 0,
                "issues_resolved": 0,
                "net_change": 0,
            },
            "recurring_issues": [],
            "stability_score": 100,  # Percentage of passing checks
        }
