"""
Run the Software Engineering Team API server.

Usage:
  cd software_engineering_team
  python -m agent_implementations.run_api_server

Or from project root:
  python software_engineering_team/agent_implementations/run_api_server.py

Then POST to http://127.0.0.1:8000/run-team with:
  {"repo_path": "/path/to/your/git/repo"}
"""

import sys
from pathlib import Path

# Ensure software_engineering_team is on sys.path (works when run as module or script)
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

import logging
import uvicorn

from shared.llm import get_llm_config_summary

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Run from software_engineering_team dir so api.main is resolvable
if __name__ == "__main__":
    logger.info("LLM config: %s", get_llm_config_summary())
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
