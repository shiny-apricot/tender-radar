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
from .subscriber_runner import (
    SubscriberConfig,
    SubscriberCycleReport,
    SubscriberDeliveryResult,
    deliver_webhook_payload,
    fetch_active_polar_subscriptions,
    last_subscriber_cycle_utc,
    parse_polar_subscription,
    run_subscriber_digest_cycle,
    validate_webhook_https_ssrf,
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
    "SubscriberConfig",
    "SubscriberCycleReport",
    "SubscriberDeliveryResult",
    "UK_OGL_ATTRIBUTION",
    "build_dedup_key",
    "build_webhook_payload",
    "compute_days_remaining",
    "deduplicate_tenders",
    "deliver_webhook_payload",
    "estimate_budget_eur",
    "fetch_active_polar_subscriptions",
    "last_subscriber_cycle_utc",
    "parse_polar_subscription",
    "render_html_digest",
    "render_markdown_digest",
    "run_daily_digest",
    "run_subscriber_digest_cycle",
    "score_and_rank_tenders",
    "score_tender",
    "validate_webhook_https_ssrf",
    "verify_polar_checkout_url",
]
