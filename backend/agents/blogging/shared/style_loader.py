"""
Load style guide and brand spec file contents for draft and copy editor agents.

Callers load files before instantiating agents; on failure log an error and return empty string.
"""

import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


def load_style_file(path: Union[str, Path], label: str = "file") -> str:
    """
    Load file content as UTF-8 text. On failure (missing file, read error), log and return "".

    Args:
        path: Path to the file.
        label: Human-readable label for log messages (e.g. "writing style guide", "brand spec").

    Returns:
        File content stripped of surrounding whitespace, or "" on any error.
    """
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.error("Could not load %s from %s: %s", label, p, e)
        return ""
