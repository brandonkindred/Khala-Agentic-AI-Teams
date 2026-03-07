def capture_screenshot(target: str) -> str:
    return f"screenshot://{target}"


def capture_dom_snippet(target: str) -> str:
    return f"<snippet target=\"{target}\">...</snippet>"
