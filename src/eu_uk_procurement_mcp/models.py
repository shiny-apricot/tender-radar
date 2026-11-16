"""Immutable dataclass schemas for EU & UK public procurement search."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TenderSearchRequest:
    """Validated query parameters for unified EU TED v3 and UK OCDS search."""

    keyword: str = ""
    sources: tuple[str, ...] = ("uk", "eu")
    cpv_prefix: str = ""
    country: str = ""
    active_only: bool = True
    limit: int = 10
    use_cache: bool = True
    timeout_seconds: float = 12.0
    cache_db_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize request parameters to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class NormalizedTender:
    """Normalized public procurement notice from UK Contracts Finder or EU TED v3."""

    source: str
    notice_id: str
    title: str
    buyer_name: str
    country: str
    published_at: str
    deadline_at: str | None
    cpv_codes: tuple[str, ...]
    estimated_value: float | None
    currency: str | None
    notice_type: str
    url: str
    sharia_allowed: bool = True
    sharia_reason: str = "Permissible public sector procurement."

    def to_dict(self) -> dict[str, Any]:
        """Serialize normalized tender record to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class TenderSearchReport:
    """Top-level search result bundling verified tenders, counts, and upstream errors."""

    status: str
    checked_at_utc: str
    keyword: str
    sources_requested: tuple[str, ...]
    sources_succeeded: tuple[str, ...]
    tenders: tuple[NormalizedTender, ...]
    total_returned: int
    filtered_out_sharia_count: int = 0
    from_cache: bool = False
    bytes_transferred: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize full tender search report to a JSON-compatible dictionary."""
        return asdict(self)
