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

# Ensure software_engineering_team and agents are on sys.path (works when run as module or script).
# software_engineering_team must come BEFORE agents so we load software_engineering_team/api,
# not agents/api (Blog API).
_team_dir = Path(__file__).resolve().parent.parent
_agents_dir = _team_dir.parent
# Remove any existing entries to avoid duplicates, then add in correct order.
# Insert agents first, then team, so team ends up at index 0 (takes precedence for "api").
for _d in (_team_dir, _agents_dir):
    while str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_agents_dir))
sys.path.insert(0, str(_team_dir))

import logging
import uvicorn

from software_engineering_team.shared.llm import get_llm_config_summary

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Run software_engineering_team API (not agents/api which is the Blog API)
if __name__ == "__main__":
    logger.info("LLM config: %s", get_llm_config_summary())
    # Use explicit path so we load software_engineering_team/api, not agents/api
    import api.main as app_module
    uvicorn.run(
        app_module.app,
        host="0.0.0.0",
        port=8000,
        reload=False,
        access_log=False,
    )
