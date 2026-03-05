"""HTML utilities for the software engineering team.

Note: Truncation detection functions have been removed. Truncation is now
detected at the LLM client level via finish_reason checks (LLMTruncatedError),
which triggers task decomposition rather than content-based heuristics.
"""

from __future__ import annotations

HTML_EXTENSIONS = (".html", ".htm", ".component.html")

COMMON_PAIRED_TAGS = [
    "div", "span", "button", "form", "table", "thead", "tbody", "tfoot",
    "tr", "td", "th", "ul", "ol", "li", "nav", "header", "footer", "main",
    "section", "article", "aside", "p", "a", "label", "select", "option",
    "textarea", "fieldset", "legend", "details", "summary", "dialog",
    "template", "slot", "ng-container", "ng-template",
]

SELF_CLOSING_TAGS = {
    "br", "hr", "img", "input", "meta", "link", "area", "base", "col",
    "embed", "param", "source", "track", "wbr",
}


def is_html_file(path: str) -> bool:
    """Check if a file path indicates an HTML file."""
    path_lower = path.lower()
    return any(path_lower.endswith(ext) for ext in HTML_EXTENSIONS)
