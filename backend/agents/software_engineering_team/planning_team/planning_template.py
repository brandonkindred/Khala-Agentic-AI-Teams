"""
Template-based output format for planning agents.

Avoids JSON so the LLM can stream or truncate without breaking parse.
Sections are delimited; partial output can still be parsed.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Section markers (must be at start of line)
MARKER_NODES = "## NODES ##"
MARKER_END_NODES = "## END NODES ##"
MARKER_EDGES = "## EDGES ##"
MARKER_END_EDGES = "## END EDGES ##"
MARKER_SUMMARY = "## SUMMARY ##"
MARKER_END_SUMMARY = "## END SUMMARY ##"
BLOCK_SEP = "---"


def _section(text: str, start_marker: str, end_marker: str) -> str:
    """Extract section between start_marker and end_marker (or end of text)."""
    start = text.find(start_marker)
    if start == -1:
        return ""
    start += len(start_marker)
    end = text.find(end_marker, start)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _parse_block(block: str) -> Dict[str, Any]:
    """Parse a single block of key: value lines. Pipe-separated values become lists."""
    out: Dict[str, Any] = {}
    list_keys = {"acceptance_criteria", "inputs", "outputs"}
    for line in block.splitlines():
        line = line.strip()
        if not line or line == BLOCK_SEP:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if key in list_keys and value:
            out[key] = [v.strip() for v in value.split("|") if v.strip()]
        elif value:
            out[key] = value
        else:
            out[key] = ""
    return out


def _blocks_in_section(section_text: str) -> List[str]:
    """Split section by --- into blocks (each block is one node or one edge)."""
    if not section_text.strip():
        return []
    blocks: List[str] = []
    for part in section_text.split(BLOCK_SEP):
        part = part.strip()
        if part:
            blocks.append(part)
    return blocks


def parse_planning_template(text: str) -> Dict[str, Any]:
    """
    Parse template output into nodes, edges, and summary.

    Tolerant to truncation: if an end marker is missing, the rest of the
    text is treated as that section. Returns dict with keys:
    - nodes: list of dicts (id, domain, kind, summary, details, user_story,
      acceptance_criteria, inputs, outputs, parent_id, metadata)
    - edges: list of dicts (from_id, to_id, type)
    - summary: str
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    summary = ""

    nodes_section = _section(text, MARKER_NODES, MARKER_END_NODES)
    if not nodes_section and MARKER_NODES in text:
        # Truncated: take from NODES to end of text
        idx = text.find(MARKER_NODES) + len(MARKER_NODES)
        nodes_section = text[idx:].strip()
    for block in _blocks_in_section(nodes_section):
        obj = _parse_block(block)
        if obj.get("id"):
            # Ensure list fields
            for k in ("acceptance_criteria", "inputs", "outputs"):
                if k not in obj:
                    obj[k] = []
                elif isinstance(obj[k], str):
                    obj[k] = [obj[k]] if obj[k] else []
            nodes.append(obj)

    edges_section = _section(text, MARKER_EDGES, MARKER_END_EDGES)
    if not edges_section and MARKER_EDGES in text:
        idx = text.find(MARKER_EDGES) + len(MARKER_EDGES)
        edges_section = text[idx:].strip()
        # Stop at next section if present
        if MARKER_SUMMARY in edges_section:
            edges_section = edges_section.split(MARKER_SUMMARY)[0].strip()
    for block in _blocks_in_section(edges_section):
        obj = _parse_block(block)
        if obj.get("from_id") and obj.get("to_id"):
            edges.append(obj)

    summary_section = _section(text, MARKER_SUMMARY, MARKER_END_SUMMARY)
    if summary_section:
        summary = summary_section.strip().split("\n")[0].strip()
    elif MARKER_SUMMARY in text and MARKER_END_SUMMARY not in text[text.find(MARKER_SUMMARY) :]:
        idx = text.find(MARKER_SUMMARY) + len(MARKER_SUMMARY)
        summary = text[idx:].strip().split("\n")[0].strip()[:500]

    return {"nodes": nodes, "edges": edges, "summary": summary}
