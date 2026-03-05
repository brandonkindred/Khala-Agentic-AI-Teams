"""
Pytest configuration and fixtures for blogging tests.

Adds the blogging directory to sys.path so imports (blog_research_agent, etc.)
resolve when running tests from repo root or from agents/blogging.
"""

import sys
from pathlib import Path

_blogging_dir = Path(__file__).resolve().parent
if str(_blogging_dir) not in sys.path:
    sys.path.insert(0, str(_blogging_dir))
