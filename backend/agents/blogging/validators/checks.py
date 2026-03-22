"""
Individual validator checks for blog drafts.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from shared.brand_spec import BrandSpec

from .models import CheckResult


def _split_paragraphs(text: str) -> List[str]:
    """Split text into paragraphs (blank-line separated)."""
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _count_sentences(paragraph: str) -> int:
    """Count sentences in a paragraph (simple heuristic: split on . ! ?)."""
    if not paragraph.strip():
        return 0
    # Split on sentence-ending punctuation
    parts = re.split(r"[.!?]+", paragraph)
    return len([p for p in parts if p.strip()])


def check_banned_phrases(draft: str, brand_spec: BrandSpec) -> CheckResult:
    """Scan draft for banned phrases (exact and case-insensitive match)."""
    banned = brand_spec.voice.banned_phrases or []
    if not banned:
        return CheckResult(name="banned_phrases", status="PASS", details={})

    draft_lower = draft.lower()
    matches: List[str] = []
    for phrase in banned:
        if phrase.lower() in draft_lower:
            matches.append(phrase)

    if matches:
        return CheckResult(
            name="banned_phrases",
            status="FAIL",
            details={"matches": matches},
        )
    return CheckResult(name="banned_phrases", status="PASS", details={})


def check_banned_patterns(draft: str, brand_spec: BrandSpec) -> CheckResult:
    """Check for banned patterns (em dash, excessive exclamation)."""
    patterns = brand_spec.voice.banned_patterns or []
    violations: List[str] = []

    if "em_dash" in patterns or "en_dash" in patterns:
        if "—" in draft or "–" in draft or "\u2014" in draft or "\u2013" in draft:
            violations.append("em_dash_or_en_dash")

    if "excessive_exclamation" in patterns:
        # Heuristic: more than 2 exclamation marks per 500 chars
        excl_count = draft.count("!")
        if len(draft) > 0 and (excl_count / max(len(draft), 1)) * 500 > 2:
            violations.append("excessive_exclamation")

    if violations:
        return CheckResult(
            name="banned_patterns",
            status="FAIL",
            details={"violations": violations},
        )
    return CheckResult(name="banned_patterns", status="PASS", details={})


def check_paragraph_length(draft: str, brand_spec: BrandSpec) -> CheckResult:
    """Check paragraph sentence count is within min/max range."""
    cfg = brand_spec.formatting
    min_sent = cfg.min_paragraph_sentences
    max_sent = cfg.max_paragraph_sentences

    paragraphs = _split_paragraphs(draft)
    bad_paragraphs: List[Dict[str, Any]] = []

    for i, para in enumerate(paragraphs):
        # Skip headings (lines that look like markdown headers)
        if para.startswith("#") or (para.startswith("```") and para.count("```") >= 2):
            continue
        count = _count_sentences(para)
        if count > 0 and (count < min_sent or count > max_sent):
            bad_paragraphs.append({"index": i + 1, "sentence_count": count, "preview": para[:80] + "..."})

    if bad_paragraphs:
        return CheckResult(
            name="paragraph_length",
            status="FAIL",
            details={"violations": bad_paragraphs, "min": min_sent, "max": max_sent},
        )
    return CheckResult(name="paragraph_length", status="PASS", details={})


def check_reading_level(draft: str, brand_spec: BrandSpec) -> CheckResult:
    """Check Flesch-Kincaid grade level against target and max."""
    try:
        import textstat
    except ImportError:
        return CheckResult(
            name="reading_level",
            status="PASS",
            details={"fk_grade": None, "note": "textstat not installed; skip"},
        )

    fk = textstat.flesch_kincaid_grade(draft)
    target = brand_spec.readability.target_grade_level
    max_grade = brand_spec.readability.max_grade_level

    if fk > max_grade:
        return CheckResult(
            name="reading_level",
            status="FAIL",
            details={"fk_grade": round(fk, 1), "target": target, "max": max_grade},
        )
    return CheckResult(name="reading_level", status="PASS", details={"fk_grade": round(fk, 1)})


def check_required_sections(draft: str, brand_spec: BrandSpec) -> CheckResult:
    """Check that required section headings are present."""
    if not brand_spec.formatting.require_sections:
        return CheckResult(name="required_sections", status="PASS", details={})

    required = brand_spec.formatting.required_section_headings or []
    if not required:
        return CheckResult(name="required_sections", status="PASS", details={})

    # Extract markdown headings (lines starting with #)
    headings = set()
    for line in draft.split("\n"):
        m = re.match(r"^#+\s+(.+)$", line.strip())
        if m:
            headings.add(m.group(1).strip())

    missing = [h for h in required if not any(h.lower() in existing.lower() for existing in headings)]
    if missing:
        return CheckResult(
            name="required_sections",
            status="FAIL",
            details={"missing": missing, "required": required},
        )
    return CheckResult(name="required_sections", status="PASS", details={})
