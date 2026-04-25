"""Markdown rendering for the ProspectDossier block embedded in the outreach prompt.

Lives in the prompts package because it shapes prompt-bound text — kept
deterministic and side-effect-free so prompt diffs stay reviewable.
"""

from __future__ import annotations

from ..models import ProspectDossier

# How many items we carry into the prompt from each dossier list. Keeps the
# rendered block bounded regardless of how rich the dossier is.
_DOSSIER_LIST_TOP_K = 5


def _truncate(items: list, k: int = _DOSSIER_LIST_TOP_K) -> list:
    return items[:k] if len(items) > k else items


def render_dossier_for_prompt(dossier: ProspectDossier) -> str:
    """Render a ProspectDossier as a compact Markdown block for the outreach prompt.

    Deterministic. Truncates long lists to top-K so the rendered block stays
    within a bounded token budget regardless of dossier thickness. Empty
    sections are omitted so the model never sees an empty heading.
    """
    lines: list[str] = [f"## Prospect Dossier (confidence: {dossier.confidence:.2f})"]

    identity_bits: list[str] = []
    name_title = dossier.full_name
    if dossier.current_title or dossier.current_company:
        name_title = f"{name_title} — {dossier.current_title} at {dossier.current_company}".strip()
    identity_bits.append(f"- Name: {name_title}")
    if dossier.location:
        identity_bits.append(f"- Location: {dossier.location}")
    if dossier.linkedin_url:
        identity_bits.append(f"- LinkedIn: {dossier.linkedin_url}")
    if dossier.personal_site:
        identity_bits.append(f"- Personal site: {dossier.personal_site}")
    lines.append("### Identity")
    lines.extend(identity_bits)

    if dossier.executive_summary:
        lines.append("### Executive Summary")
        lines.append(dossier.executive_summary)

    if dossier.trigger_events:
        lines.append("### Trigger Events")
        for ev in _truncate(dossier.trigger_events):
            lines.append(f"- {ev}")

    if dossier.publications:
        lines.append("### Publications")
        for p in _truncate(dossier.publications):
            bits = [f"[{p.kind}] {p.title}"]
            if p.venue:
                bits.append(f"— {p.venue}")
            if p.date:
                bits.append(f"({p.date})")
            url_suffix = f"\n  {p.url}" if p.url else ""
            lines.append(f"- {' '.join(bits)}{url_suffix}")

    if dossier.recent_activity:
        lines.append("### Recent Activity")
        for a in _truncate(dossier.recent_activity):
            lines.append(f"- {a}")

    if dossier.conversation_hooks:
        lines.append("### Conversation Hooks")
        for h in _truncate(dossier.conversation_hooks):
            lines.append(f"- {h}")

    if dossier.mutual_connection_angles:
        lines.append("### Mutual Connection Angles")
        for m in _truncate(dossier.mutual_connection_angles):
            lines.append(f"- {m}")

    if dossier.stated_beliefs:
        lines.append("### Stated Beliefs")
        for b in _truncate(dossier.stated_beliefs):
            lines.append(f"- {b}")

    if dossier.topics_of_interest:
        lines.append("### Topics of Interest")
        lines.append(", ".join(_truncate(dossier.topics_of_interest, 10)))

    if dossier.sources:
        lines.append("### Sources (only these URLs may be cited)")
        for s in dossier.sources:
            lines.append(f"- {s}")

    return "\n".join(lines)
