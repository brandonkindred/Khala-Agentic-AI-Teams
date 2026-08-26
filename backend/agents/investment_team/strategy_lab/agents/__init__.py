"""Strands-powered agents for the Strategy Lab pipeline.

The design phase is split into three narrowly-scoped agents:

* :class:`DesignAgent` — authors a structured ``StrategySpec`` from priors
  + signal brief. No code.
* :class:`DesignReviewAgent` — critiques the candidate spec, emitting a
  :class:`SpecCritique` the designer can act on. No code, no spec mutation.
* :class:`CodeSynthesisAgent` — produces Python from a frozen, review-
  approved spec. Only invoked when the deterministic compiler cannot.

The remaining agents (refinement, alignment, analysis) are unchanged.
"""

from .alignment import (
    AlignmentAuditError,
    AlignmentIssue,
    TradeAlignmentAgent,
    TradeAlignmentReport,
)
from .analysis import AnalysisAgent
from .code_synthesis import CodeSynthesisAgent, CodeSynthesisError
from .design import DesignAgent
from .diff_format import diff_or_full
from .design_review import (
    CritiqueIssue,
    DesignReviewAgent,
    DesignReviewError,
    SpecCritique,
)
from .refinement import RefinementAgent

__all__ = [
    "DesignAgent",
    "DesignReviewAgent",
    "DesignReviewError",
    "CritiqueIssue",
    "SpecCritique",
    "CodeSynthesisAgent",
    "CodeSynthesisError",
    "diff_or_full",
    "RefinementAgent",
    "TradeAlignmentAgent",
    "TradeAlignmentReport",
    "AlignmentAuditError",
    "AlignmentIssue",
    "AnalysisAgent",
]
