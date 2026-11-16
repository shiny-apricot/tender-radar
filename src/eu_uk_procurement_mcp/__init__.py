"""EU TED v3 & UK Contracts Finder OCDS Unified Procurement MCP Server & Apify Actor."""

from __future__ import annotations

from .models import NormalizedTender, TenderSearchReport, TenderSearchRequest
from .procurement_client import (
    EU_TED_SEARCH_URL,
    MAX_RESPONSE_BYTES,
    UK_OCDS_SEARCH_URL,
    SqliteTtlCache,
    fetch_ted_europa_v3,
    fetch_uk_contracts_finder,
    normalize_ted_v3_notice,
    normalize_uk_ocds_release,
    search_unified_tenders,
)
from .sharia_guard import (
    BLOCKED_CPV_PREFIXES,
    HARAM_SECTOR_TOKENS,
    ShariaGuardBlockedError,
    is_query_halal,
    is_tender_halal,
)

__all__ = [
    "BLOCKED_CPV_PREFIXES",
    "EU_TED_SEARCH_URL",
    "HARAM_SECTOR_TOKENS",
    "MAX_RESPONSE_BYTES",
    "NormalizedTender",
    "ShariaGuardBlockedError",
    "SqliteTtlCache",
    "TenderSearchReport",
    "TenderSearchRequest",
    "UK_OCDS_SEARCH_URL",
    "fetch_ted_europa_v3",
    "fetch_uk_contracts_finder",
    "is_query_halal",
    "is_tender_halal",
    "normalize_ted_v3_notice",
    "normalize_uk_ocds_release",
    "search_unified_tenders",
]
