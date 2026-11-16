"""Immutable dataclass schemas for TenderRadar EU/UK automated bid-fit digests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from eu_uk_procurement_mcp.models import NormalizedTender


@dataclass(frozen=True)
class SavedSearchProfile:
    """Saved procurement monitoring profile used for deterministic bid-fit scoring."""

    profile_id: str
    name: str
    cpv_prefixes: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    min_budget_eur: float | None = None
    min_days_remaining: int = 0
    sources: tuple[str, ...] = ("uk", "eu")
    limit: int = 20

    def to_dict(self) -> dict[str, Any]:
        """Serialize saved search profile to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class ScoredTender:
    """Procurement notice enriched with deterministic Bid-Fit Score and disqualifiers."""

    tender: NormalizedTender
    dedup_key: str
    bid_fit_score: int
    matched_signals: tuple[str, ...]
    disqualifiers: tuple[str, ...]
    days_remaining: int | None
    budget_eur: float | None
    is_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize scored tender to a JSON-compatible dictionary."""
        payload = asdict(self)
        payload["tender"] = self.tender.to_dict()
        return payload


@dataclass(frozen=True)
class SlaCheckResult:
    """Result of checking cron digest continuity against the 26-hour SLA threshold."""

    is_healthy: bool
    last_run_at_utc: str | None
    hours_since_last_run: float | None
    max_gap_hours: float
    pro_rata_credit_eligible: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize SLA check result to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class DigestReport:
    """Complete daily digest report with deduplicated, scored, unseen tenders and SLA telemetry."""

    profile_id: str
    profile_name: str
    generated_at_utc: str
    sources_queried: tuple[str, ...]
    total_fetched: int
    deduplicated_count: int
    filtered_sharia_count: int
    already_seen_count: int
    new_tenders: tuple[ScoredTender, ...]
    disqualified_tenders: tuple[ScoredTender, ...]
    sla_status: SlaCheckResult
    attribution: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize digest report to a JSON-compatible dictionary."""
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "generated_at_utc": self.generated_at_utc,
            "sources_queried": list(self.sources_queried),
            "total_fetched": self.total_fetched,
            "deduplicated_count": self.deduplicated_count,
            "filtered_sharia_count": self.filtered_sharia_count,
            "already_seen_count": self.already_seen_count,
            "new_tenders": [item.to_dict() for item in self.new_tenders],
            "disqualified_tenders": [item.to_dict() for item in self.disqualified_tenders],
            "sla_status": self.sla_status.to_dict(),
            "attribution": self.attribution,
        }


@dataclass(frozen=True)
class PolarCheckoutStatus:
    """Live HTTP verification result for a Polar.sh checkout URL."""

    url: str
    is_live: bool
    status_code: int | None
    final_url: str
    redirect_location: str | None
    reason: str
    checked_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize Polar checkout status to a JSON-compatible dictionary."""
        return asdict(self)
