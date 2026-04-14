"""Strands SDK multi-agent graphs for the branding team pipeline."""

__all__ = ["build_branding_graph"]


def build_branding_graph(**kwargs):
    """Lazy re-export to avoid circular imports with ``agents.py``."""
    from .top_level import build_branding_graph as _build

    return _build(**kwargs)
