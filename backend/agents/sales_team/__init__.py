"""AI Sales Team — full B2B sales pod. Every agent calls the shared
``llm_service`` layer via ``complete_validated`` for typed, self-correcting
structured output with per-role ``sales.<role>`` telemetry tagging."""

from .models import (
    CareerRole,
    ClosingStrategy,
    DealOutcome,
    DealResult,
    DecisionMakerSignal,
    DeepResearchRequest,
    DeepResearchResult,
    DiscoveryPlan,
    EmailTouch,
    EvidenceCitation,
    IdealCustomerProfile,
    LearningInsights,
    NurtureSequence,
    OutreachAngle,
    OutreachSequence,
    OutreachVariant,
    PersonalizationGrade,
    PipelineStage,
    Prospect,
    ProspectDossier,
    ProspectListEntry,
    PublicWorkItem,
    QualificationScore,
    SalesPipelineRequest,
    SalesPipelineResult,
    SalesProposal,
    StageOutcome,
)
from .orchestrator import SalesPodOrchestrator

__all__ = [
    "SalesPodOrchestrator",
    "SalesPipelineRequest",
    "SalesPipelineResult",
    "PipelineStage",
    "IdealCustomerProfile",
    "Prospect",
    # Outreach
    "OutreachSequence",
    "OutreachVariant",
    "OutreachAngle",
    "EmailTouch",
    "EvidenceCitation",
    "PersonalizationGrade",
    # Other stages
    "QualificationScore",
    "NurtureSequence",
    "DiscoveryPlan",
    "SalesProposal",
    "ClosingStrategy",
    "StageOutcome",
    "DealOutcome",
    "DealResult",
    "LearningInsights",
    # Deep-research prospecting
    "DeepResearchRequest",
    "DeepResearchResult",
    "ProspectListEntry",
    "ProspectDossier",
    "CareerRole",
    "PublicWorkItem",
    "DecisionMakerSignal",
]
