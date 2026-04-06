"""GradingScale value object — builds once from template YAML, used everywhere."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GradingScale:
    """Immutable grading scale built from a template's ``scoring.grading_scale``."""

    thresholds: tuple[tuple[int, str], ...]

    @classmethod
    def from_template(cls, template: dict) -> GradingScale:
        """Build a GradingScale from the template's scoring configuration.

        Falls back to a sensible default if ``scoring.grading_scale`` is
        missing or empty.
        """
        raw = template.get("scoring", {}).get("grading_scale", [])
        if not raw:
            raw = [
                {"min_pct": 90, "grade": "Excellent"},
                {"min_pct": 75, "grade": "Good"},
                {"min_pct": 50, "grade": "Needs Improvement"},
                {"min_pct": 0, "grade": "Poor"},
            ]
        pairs = tuple(
            sorted(
                ((entry["min_pct"], entry["grade"]) for entry in raw),
                key=lambda t: t[0],
                reverse=True,
            )
        )
        return cls(thresholds=pairs)

    def grade(self, pct: float) -> str:
        """Map a percentage to a grade label."""
        for threshold, label in self.thresholds:
            if pct >= threshold:
                return label
        return self.thresholds[-1][1] if self.thresholds else "Poor"
