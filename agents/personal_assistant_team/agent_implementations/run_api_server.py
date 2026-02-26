"""
Run the Personal Assistant Team API server.

Usage:
  cd personal_assistant_team
  python -m agent_implementations.run_api_server

Or from project root:
  python agents/personal_assistant_team/agent_implementations/run_api_server.py

Then POST to http://127.0.0.1:8015/users/{user_id}/assistant with:
  {"message": "your request here"}
"""

import sys
from pathlib import Path

_team_dir = Path(__file__).resolve().parent.parent
_agents_dir = _team_dir.parent

for _d in (_team_dir, _agents_dir):
    while str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_agents_dir))
sys.path.insert(0, str(_team_dir))

import logging
import os

import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    """Run the Personal Assistant API server."""
    host = os.getenv("PA_HOST", "0.0.0.0")
    port = int(os.getenv("PA_PORT", "8015"))
    
    logger.info("Starting Personal Assistant API server on %s:%d", host, port)
    logger.info("LLM Provider: %s", os.getenv("SW_LLM_PROVIDER", "ollama"))
    logger.info("LLM Model: %s", os.getenv("SW_LLM_MODEL", "default"))
    
    from api.main import app
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
