"""
Composable health check system for team services.

Teams register health checks (db, llm, temporal, custom) and the standard
``/health`` endpoint runs them all, reporting per-check status.

Usage::

    from team_contract.health import HealthCheckRegistry, HealthCheck

    registry = HealthCheckRegistry()
    registry.add(HealthCheck("llm", check_llm_connectivity))
    registry.add(HealthCheck("postgres", check_postgres))

    @app.get("/health")
    async def health():
        return await registry.run_all()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Type for health check functions: sync or async callables returning a dict.
CheckFn = Union[
    Callable[[], Dict[str, Any]],
    Callable[[], Coroutine[Any, Any, Dict[str, Any]]],
]


@dataclass
class HealthCheck:
    """A named health check with a callable probe.

    The check function should return a dict with at least ``{"status": "ok"|"degraded"|"error"}``.
    If it raises an exception, the check is reported as ``"error"``.
    """

    name: str
    check_fn: CheckFn
    critical: bool = True  # If True, failure makes overall status "degraded"
    timeout_seconds: float = 5.0


@dataclass
class HealthCheckResult:
    name: str
    status: str  # "ok", "degraded", "error"
    latency_ms: float
    detail: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
        }
        if self.detail:
            d["detail"] = self.detail
        if self.extra:
            d.update(self.extra)
        return d


class HealthCheckRegistry:
    """Registry of health checks with aggregate execution."""

    def __init__(self) -> None:
        self._checks: List[HealthCheck] = []

    def add(self, check: HealthCheck) -> "HealthCheckRegistry":
        """Add a health check. Returns self for chaining."""
        self._checks.append(check)
        return self

    async def _run_one(self, check: HealthCheck) -> HealthCheckResult:
        """Run a single check with timeout protection."""
        t0 = time.monotonic()
        try:
            result = check.check_fn()
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=check.timeout_seconds)
            latency = (time.monotonic() - t0) * 1000
            status = result.get("status", "ok") if isinstance(result, dict) else "ok"
            return HealthCheckResult(
                name=check.name,
                status=status,
                latency_ms=latency,
                extra=result if isinstance(result, dict) else {},
            )
        except asyncio.TimeoutError:
            latency = (time.monotonic() - t0) * 1000
            return HealthCheckResult(
                name=check.name,
                status="error",
                latency_ms=latency,
                detail=f"Timed out after {check.timeout_seconds}s",
            )
        except Exception as e:
            latency = (time.monotonic() - t0) * 1000
            return HealthCheckResult(
                name=check.name,
                status="error",
                latency_ms=latency,
                detail=str(e),
            )

    async def run_all(self) -> Dict[str, Any]:
        """Execute all checks concurrently and return aggregate result."""
        if not self._checks:
            return {"status": "ok", "checks": {}}

        results = await asyncio.gather(
            *(self._run_one(check) for check in self._checks),
            return_exceptions=False,
        )

        checks_dict = {r.name: r.to_dict() for r in results}
        has_critical_failure = any(
            r.status != "ok" and check.critical
            for r, check in zip(results, self._checks)
        )
        overall = "degraded" if has_critical_failure else "ok"

        return {
            "status": overall,
            "checks": checks_dict,
        }
