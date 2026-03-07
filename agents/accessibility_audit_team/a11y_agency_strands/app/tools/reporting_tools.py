def write_docx_from_template(template: str, context: dict) -> str:
    return f"docx://{template}?keys={','.join(sorted(context.keys()))}"


def render_pdf(docx_path: str) -> str:
    return f"pdf://{docx_path}"


def export_backlog_csv(findings: list[dict]) -> str:
    return f"csv://backlog?rows={len(findings)}"


def create_jira_issues(findings: list[dict]) -> list[str]:
    return [f"A11Y-{idx+1}" for idx, _ in enumerate(findings)]
