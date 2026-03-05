#!/usr/bin/env python3
"""
Start the research-and-review API server.

Usage:
    python3 agent_implementations/run_api_server.py
    # or
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
