#!/usr/bin/env python3
"""Run the central job microservice (Postgres-backed)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    _root = Path(__file__).resolve().parent
    _agents = _root / "agents"
    if str(_agents) not in sys.path:
        sys.path.insert(0, str(_agents))

    import uvicorn

    parser = argparse.ArgumentParser(description="Strands job service")
    parser.add_argument("--host", default=os.getenv("JOB_SERVICE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("JOB_SERVICE_PORT", "8091")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "job_service.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
