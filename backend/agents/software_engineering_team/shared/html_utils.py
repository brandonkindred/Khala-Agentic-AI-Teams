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


def is_html_truncated(content: str) -> bool:
    """Return True if the HTML content appears to be truncated.

    Checks for:
    - Mid-tag truncation (unclosed angle bracket)
    - Unclosed attribute quotes
    - Unbalanced paired tags
    """
    if not content or not content.strip():
        return False

    # Check for unclosed angle bracket (mid-tag)
    if content.count("<") > content.count(">"):
        return True

    # Check for unclosed attribute quotes
    import re
    last_tag_match = list(re.finditer(r"<[^>]*$", content))
    if last_tag_match:
        fragment = last_tag_match[-1].group()
        single_quotes = fragment.count("'")
        double_quotes = fragment.count('"')
        if single_quotes % 2 != 0 or double_quotes % 2 != 0:
            return True
        return True  # unclosed tag itself

    # Check for unclosed attribute quotes in the last token
    stripped = content.rstrip()
    single_count = 0
    double_count = 0
    in_tag = False
    for ch in stripped:
        if ch == "<":
            in_tag = True
            single_count = 0
            double_count = 0
        elif ch == ">":
            in_tag = False
        elif in_tag:
            if ch == "'":
                single_count += 1
            elif ch == '"':
                double_count += 1
    if in_tag or single_count % 2 != 0 or double_count % 2 != 0:
        return True

    # Check for unbalanced paired tags
    import re
    open_tags: list[str] = []
    for match in re.finditer(r"<(/?)(\w[\w-]*)[^>]*?>", content):
        closing = match.group(1) == "/"
        tag = match.group(2).lower()
        if tag in SELF_CLOSING_TAGS:
            continue
        if tag not in COMMON_PAIRED_TAGS:
            continue
        if closing:
            if open_tags and open_tags[-1] == tag:
                open_tags.pop()
        else:
            # Check if self-closing (ends with /)
            full_match = match.group(0)
            if not full_match.endswith("/>"):
                open_tags.append(tag)
    return len(open_tags) > 0


def validate_html_completeness(content: str) -> tuple[bool, str]:
    """Validate HTML content for completeness.

    Returns:
        (is_valid, error_message) where error_message is "" when valid.
    """
    if not content or not content.strip():
        return True, ""

    import re

    # Check for unclosed angle bracket / mid-attribute
    if content.count("<") > content.count(">"):
        # Find the unclosed tag context
        last_open = content.rfind("<")
        fragment = content[last_open:]
        tag_match = re.match(r"</?(\w[\w-]*)", fragment)
        tag_name = tag_match.group(1) if tag_match else "tag"
        return False, f"Truncated mid-tag: <{tag_name} not closed"

    # Check for unclosed attribute quotes in last open tag
    last_tag = list(re.finditer(r"<[^>]*$", content))
    if last_tag:
        fragment = last_tag[-1].group()
        if fragment.count('"') % 2 != 0:
            return False, "Truncated HTML: unclosed double quote in attribute"
        if fragment.count("'") % 2 != 0:
            return False, "Truncated HTML: unclosed single quote in attribute"
        tag_match = re.match(r"</?(\w[\w-]*)", fragment)
        tag_name = tag_match.group(1) if tag_match else "tag"
        return False, f"Truncated mid-tag: <{tag_name} not closed"

    # Check for unbalanced paired tags
    open_tags: list[str] = []
    for match in re.finditer(r"<(/?)(\w[\w-]*)[^>]*?>", content):
        closing = match.group(1) == "/"
        tag = match.group(2).lower()
        if tag in SELF_CLOSING_TAGS:
            continue
        if tag not in COMMON_PAIRED_TAGS:
            continue
        if closing:
            if open_tags and open_tags[-1] == tag:
                open_tags.pop()
        else:
            full_match = match.group(0)
            if not full_match.endswith("/>"):
                open_tags.append(tag)
    if open_tags:
        unclosed = open_tags[-1]
        return False, f"Unbalanced HTML tags: <{unclosed}> not closed"

    return True, ""


def get_truncated_html_files(files: dict[str, str]) -> list[str]:
    """Return list of filenames (from a {filename: content} dict) with truncated HTML."""
    return [path for path, content in files.items() if is_html_file(path) and is_html_truncated(content)]


def get_truncated_files_summary(files: dict[str, str]) -> str:
    """Return a human-readable summary of truncated HTML files, or '' if none."""
    truncated = get_truncated_html_files(files)
    if not truncated:
        return ""
    lines = ["The following HTML files appear truncated:"]
    for path in truncated:
        _, error = validate_html_completeness(files[path])
        lines.append(f"  {path}: {error}")
    return "\n".join(lines)


def merge_html_continuation(original: str, continuation: str) -> str:
    """Merge an HTML original with its continuation, stripping joining whitespace.

    If the original ends with an incomplete tag fragment (e.g. ``</span`` without
    the closing ``>``), that fragment is removed so the continuation can supply
    the complete tag.
    """
    if not continuation:
        return original
    import re
    stripped = original.rstrip()
    # Remove any trailing incomplete tag (< ... without closing >)
    stripped = re.sub(r"<[^>]*$", "", stripped)
    return stripped + continuation.lstrip()
