"""
Run the Personal Assistant Team API server.

Usage:
  cd agents/
  python -m personal_assistant_team.agent_implementations.run_api_server

Or from project root:
  python -m agents.personal_assistant_team.agent_implementations.run_api_server

Then access the UI at http://127.0.0.1:8015/
Or POST to http://127.0.0.1:8015/users/{user_id}/assistant with:
  {"message": "your request here"}
"""

import sys
from pathlib import Path

# Set up imports to work from various locations
_this_file = Path(__file__).resolve()
_team_dir = _this_file.parent.parent  # personal_assistant_team/
_agents_dir = _team_dir.parent  # agents/
_project_root = _agents_dir.parent  # khala/

# Add agents dir to path so imports like personal_assistant_team.api.main work
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

import logging  # noqa: E402
import os  # noqa: E402

import uvicorn  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    """Run the Personal Assistant API server."""
    host = os.getenv("PA_HOST", "0.0.0.0")
    port = int(os.getenv("PA_PORT", "8015"))

    logger.info("Starting Personal Assistant API server on %s:%d", host, port)
    logger.info("LLM Provider: %s", os.getenv("LLM_PROVIDER", "ollama"))
    logger.info("LLM Model: %s", os.getenv("LLM_MODEL", "default"))
    logger.info("UI available at http://%s:%d/", host if host != "0.0.0.0" else "localhost", port)

    from personal_assistant_team.api.main import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
