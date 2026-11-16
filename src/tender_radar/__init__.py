"""TenderRadar: Stateless EU TED v3 & UK Contracts Finder OCDS automated bid-fit digest."""

from __future__ import annotations

from .digest import (
    COMBINED_LEGAL_ATTRIBUTION,
    EU_TED_ATTRIBUTION,
    SeenTenderStore,
    UK_OGL_ATTRIBUTION,
    build_webhook_payload,
    render_html_digest,
    render_markdown_digest,
    run_daily_digest,
)
from .models import (
    DigestReport,
    PolarCheckoutStatus,
    SavedSearchProfile,
    ScoredTender,
    SlaCheckResult,
)
from .polar_guard import verify_polar_checkout_url
from .scorer import (
    build_dedup_key,
    compute_days_remaining,
    deduplicate_tenders,
    estimate_budget_eur,
    score_and_rank_tenders,
    score_tender,
)

__all__ = [
    "COMBINED_LEGAL_ATTRIBUTION",
    "DigestReport",
    "EU_TED_ATTRIBUTION",
    "PolarCheckoutStatus",
    "SavedSearchProfile",
    "ScoredTender",
    "SeenTenderStore",
    "SlaCheckResult",
    "UK_OGL_ATTRIBUTION",
    "build_dedup_key",
    "build_webhook_payload",
    "compute_days_remaining",
    "deduplicate_tenders",
    "estimate_budget_eur",
    "render_html_digest",
    "render_markdown_digest",
    "run_daily_digest",
    "score_and_rank_tenders",
    "score_tender",
    "verify_polar_checkout_url",
]
