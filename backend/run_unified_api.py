#!/usr/bin/env python3
"""
Unified API Server Launcher.

Starts the unified API server that consolidates all Khala team APIs
under a single entry point.

Usage:
    python run_unified_api.py [--host HOST] [--port PORT] [--reload]

Environment Variables:
    UNIFIED_API_HOST - Host to bind (default: 0.0.0.0)
    UNIFIED_API_PORT - Port to bind (default: 8080)

Example:
    # Start with defaults (0.0.0.0:8080)
    python run_unified_api.py

    # Start on specific port
    python run_unified_api.py --port 9000

    # Development mode with auto-reload
    python run_unified_api.py --reload
"""

import argparse
import logging
import sys
from pathlib import Path

# Ensure project root and agents are in path
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent
_agents_dir = _project_root / "agents"

# Load .env so OLLAMA_API_KEY etc. are available when running via make run (e.g. backend/.env or docker/.env)
try:
    from dotenv import load_dotenv

    _backend_env = _project_root / ".env"
    _docker_env = _project_root.parent / "docker" / ".env"
    if _backend_env.exists():
        load_dotenv(_backend_env)
    if _docker_env.exists():
        load_dotenv(_docker_env)
except ImportError:
    pass

if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

import uvicorn  # noqa: E402
from unified_api.config import DEFAULT_HOST, DEFAULT_PORT  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("unified_api_launcher")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Start the Khala Unified API Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available team APIs (all mounted under /api/):
  /api/blogging              - Blog research, review, draft, publication
  /api/software-engineering  - Full dev team simulation
  /api/personal-assistant    - Personal assistant (email, calendar, tasks)
  /api/market-research       - Market research and UX synthesis
  /api/soc2-compliance       - SOC2 compliance audit
  /api/social-marketing      - Social media campaign planning
  /api/branding              - Brand strategy and design
  /api/agent-provisioning    - Agent environment provisioning

Interactive docs available at: http://HOST:PORT/docs
        """,
    )
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"Host to bind (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes (default: 1)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
        help="Log level (default: info)",
    )
    return parser.parse_args()


def main() -> None:
    """Run the unified API server."""
    args = parse_args()

    logger.info("=" * 60)
    logger.info("Khala Unified API Server")
    logger.info("=" * 60)
    logger.info("Host: %s", args.host)
    logger.info("Port: %d", args.port)
    logger.info("Reload: %s", args.reload)
    logger.info("Workers: %d", args.workers)
    logger.info("Log Level: %s", args.log_level)
    logger.info("")
    logger.info("API Documentation: http://%s:%d/docs", "localhost" if args.host == "0.0.0.0" else args.host, args.port)
    logger.info("Health Check: http://%s:%d/health", "localhost" if args.host == "0.0.0.0" else args.host, args.port)
    logger.info("=" * 60)

    uvicorn.run(
        "unified_api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
