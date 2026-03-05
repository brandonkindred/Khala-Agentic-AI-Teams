"""
FastAPI endpoints for the Digital Accessibility Audit Team.

Provides REST API for:
- Audit creation and execution
- Findings retrieval
- Report generation
- Monitoring operations (ARM add-on)
- Design system operations (ADSE add-on)
"""

from .main import router

__all__ = ["router"]
