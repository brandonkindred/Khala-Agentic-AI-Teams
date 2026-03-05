"""Document writer tool for ADRs, diagram specs, and markdown output."""

from __future__ import annotations

from pathlib import Path

from strands import tool


@tool
def document_writer_tool(
    output_dir: str = "outputs",
    filename: str = "",
    content: str = "",
    format: str = "markdown",
) -> str:
    """Write content to a file in the outputs directory.

    Use this to create ADRs, Mermaid diagram specs, and other architecture
    deliverables. Creates parent directories as needed. Default output_dir
    is "outputs" for the standard deliverable set.

    Args:
        output_dir: Base directory for output (default "outputs").
        filename: Name of the file (e.g. "architecture-overview.md",
            "adr/ADR-001-microservices.md").
        content: The content to write. For Mermaid diagrams, include the
            diagram source (optionally wrapped in ```mermaid code fences).
        format: Output format - "markdown" or "mermaid". Affects file
            extension if not specified in filename.

    Returns:
        The absolute path of the written file, or an error message.
    """
    try:
        if not filename:
            return "Error: filename is required"
        base = Path(output_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)

        # Ensure filename has extension
        if not any(filename.endswith(ext) for ext in (".md", ".mmd", ".mermaid")):
            ext = ".mmd" if format == "mermaid" else ".md"
            filename = filename + ext

        out_path = base / filename
        out_path.write_text(content, encoding="utf-8")
        return str(out_path)
    except PermissionError as e:
        return f"Error: Permission denied writing to {output_dir}/{filename}: {e}"
    except Exception as e:
        return f"Error writing {filename}: {e}"
