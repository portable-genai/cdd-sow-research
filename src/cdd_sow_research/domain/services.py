"""Aggregator re-exporting the CDD orchestrator and its sub-services.

The services live one-per-module (``cdd_service``, ``sow_service``, ``risk_service``,
``adverse_media_service``, ``ownership_service``) so each stays focused. This module is
the single import surface the wiring layers (``api.deps``, ``agent.tools``, ``cli``) use,
so they never need to know which module a service lives in.
"""

from __future__ import annotations

from .adverse_media_service import AdverseMediaService
from .case_policy import CaseTransitionPolicy
from .cdd_service import CddService
from .gap_analysis import GapAnalysisResult, GapAnalysisService
from .ownership_service import OwnershipService
from .periodic_review_service import PeriodicReviewService
from .perpetual_kyc import PerpetualKycEngine
from .perpetual_kyc_service import PerpetualKycService
from .related_party import (
    RelatedPartyInput,
    RelatedPartyPolicy,
    RelatedPartyService,
)
from .rfi_drafting import RfiDraftingService
from .risk_service import RiskRatingService
from .scorecard_service import RiskScorecardService
from .screening import ScreeningPolicy, ScreeningService
from .source_of_funds_service import SourceOfFundsService
from .sow_case_service import SowCaseService
from .sow_service import SourceOfWealthService
from .ubo_graph import UboGraphEngine
from .ubo_graph_service import UboGraphService

__all__ = [
    "CddService",
    "SourceOfWealthService",
    "RiskRatingService",
    "AdverseMediaService",
    "OwnershipService",
    # Long-running, auditable SoW cases (docs/sow-longitudinal-audit-design.md).
    "SowCaseService",
    "GapAnalysisService",
    "GapAnalysisResult",
    "CaseTransitionPolicy",
    "RfiDraftingService",
    # Related parties / key individuals (companies + people in one system).
    "RelatedPartyService",
    "RelatedPartyPolicy",
    "RelatedPartyInput",
    # Sanctions / PEP / watchlist screening.
    "ScreeningService",
    "ScreeningPolicy",
    # Risk-based scorecard + CDD tiering.
    "RiskScorecardService",
    # Source of Funds (distinct from Source of Wealth).
    "SourceOfFundsService",
    # Ongoing monitoring / periodic review.
    "PeriodicReviewService",
    # Perpetual KYC: continuous, signal-driven re-assessment + the review queue.
    "PerpetualKycService",
    "PerpetualKycEngine",
    # Cross-jurisdiction UBO graph: the walked structure behind an entity subject.
    "UboGraphService",
    "UboGraphEngine",
]
