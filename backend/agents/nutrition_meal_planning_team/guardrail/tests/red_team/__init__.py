"""Red-team fixtures — known-hostile LLM-style outputs SPEC-007 §6.2.

Every fixture must be hard-rejected by ``check_recommendation``. CI
fails if any fixture passes; that is the canary for catalog
regressions, parser regressions, or check-pipeline regressions.
"""
