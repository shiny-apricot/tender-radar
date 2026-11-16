"""Deterministic bid-fit scoring, disqualifier screening, and cross-source deduplication."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Sequence

from eu_uk_procurement_mcp.models import NormalizedTender
from eu_uk_procurement_mcp.procurement_client import _normalize_country_code
from eu_uk_procurement_mcp.sharia_guard import is_tender_halal

from .models import SavedSearchProfile, ScoredTender

APPROX_EUR_RATES: dict[str, float] = {
    "EUR": 1.0,
    "GBP": 1.17,
    "USD": 0.92,
    "RON": 0.20,
    "PLN": 0.23,
    "SEK": 0.087,
    "DKK": 0.134,
    "CZK": 0.040,
    "HUF": 0.0025,
    "NOK": 0.085,
    "CHF": 1.06,
}


def _normalize_text_for_key(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def build_dedup_key(tender: NormalizedTender) -> str:
    """Build canonical deduplication key combining normalized title/buyer or source:uid."""
    norm_title = _normalize_text_for_key(tender.title)
    norm_buyer = _normalize_text_for_key(tender.buyer_name)
    if norm_title and norm_buyer:
        digest = hashlib.sha256(f"{norm_title}|{norm_buyer}".encode("utf-8")).hexdigest()[:16]
        return f"tb:{digest}"
    source = (tender.source or "unknown").strip().lower()
    uid = (tender.notice_id or norm_title or "untitled").strip().lower()
    return f"su:{source}:{uid}"


def _tender_richness(tender: NormalizedTender) -> tuple[int, int, int, int]:
    return (
        1 if tender.estimated_value is not None else 0,
        1 if tender.deadline_at is not None else 0,
        len(tender.cpv_codes),
        len(tender.title),
    )


def deduplicate_tenders(tenders: Sequence[NormalizedTender]) -> list[NormalizedTender]:
    """Merge and deduplicate EU TED and UK OCDS notices by source:uid and title/buyer fingerprint."""
    seen_source_uids: dict[str, int] = {}
    seen_fingerprints: dict[str, int] = {}
    deduped: list[NormalizedTender] = []

    for item in tenders:
        source_uid = f"{item.source.strip().lower()}:{item.notice_id.strip().lower()}" if item.notice_id.strip() else ""
        dedup_key = build_dedup_key(item)

        existing_idx: int | None = None
        if source_uid and source_uid in seen_source_uids:
            existing_idx = seen_source_uids[source_uid]
        elif dedup_key in seen_fingerprints:
            existing_idx = seen_fingerprints[dedup_key]

        if existing_idx is not None:
            if _tender_richness(item) > _tender_richness(deduped[existing_idx]):
                deduped[existing_idx] = item
            continue

        idx = len(deduped)
        deduped.append(item)
        if source_uid:
            seen_source_uids[source_uid] = idx
        seen_fingerprints[dedup_key] = idx

    return deduped


def estimate_budget_eur(estimated_value: float | None, currency: str | None) -> float | None:
    """Convert estimated contract value to approximate EUR using deterministic currency rates."""
    if estimated_value is None or not currency:
        return None
    rate = APPROX_EUR_RATES.get(currency.strip().upper())
    if rate is None:
        return None
    return round(float(estimated_value) * rate, 2)


def _parse_iso_datetime(raw: str) -> datetime | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        date_prefix = cleaned[:10]
        try:
            dt = datetime.fromisoformat(date_prefix)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_days_remaining(
    deadline_at: str | None,
    *,
    now_utc: datetime | None = None,
) -> int | None:
    """Compute whole days remaining until tender deadline in UTC."""
    if not deadline_at:
        return None
    deadline_dt = _parse_iso_datetime(deadline_at)
    if deadline_dt is None:
        return None
    ref = now_utc.astimezone(timezone.utc) if now_utc else datetime.now(timezone.utc)
    return (deadline_dt.date() - ref.date()).days


def score_tender(
    tender: NormalizedTender,
    profile: SavedSearchProfile,
    *,
    now_utc: datetime | None = None,
) -> ScoredTender:
    """Evaluate a normalized tender against Sharia CPV screening and profile bid-fit rules."""
    disqualifiers: list[str] = []
    matched_signals: list[str] = []

    halal_ok, halal_reason = is_tender_halal(
        title=tender.title,
        cpv_codes=tender.cpv_codes,
        buyer_name=tender.buyer_name,
    )
    if not tender.sharia_allowed or not halal_ok:
        reason = halal_reason if not halal_ok else tender.sharia_reason
        disqualifiers.append(f"sharia_veto:{reason}")

    searchable_text = f"{tender.title} {tender.buyer_name}".lower()
    for excl in profile.exclude_keywords:
        cleaned_excl = excl.strip().lower()
        if cleaned_excl and cleaned_excl in searchable_text:
            disqualifiers.append(f"excluded_keyword:{cleaned_excl}")

    allowed_countries = {
        _normalize_country_code(c) for c in profile.countries if c.strip()
    }
    tender_country = _normalize_country_code(tender.country)
    if allowed_countries and tender_country not in allowed_countries:
        disqualifiers.append(f"country_mismatch:{tender_country or 'UNKNOWN'}")

    days_remaining = compute_days_remaining(tender.deadline_at, now_utc=now_utc)
    if days_remaining is not None and days_remaining < 0:
        disqualifiers.append(f"deadline_expired:{days_remaining}d")
    elif (
        profile.min_days_remaining > 0
        and days_remaining is not None
        and days_remaining < profile.min_days_remaining
    ):
        disqualifiers.append(
            f"deadline_too_soon:{days_remaining}d<{profile.min_days_remaining}d"
        )

    budget_eur = estimate_budget_eur(tender.estimated_value, tender.currency)
    if (
        profile.min_budget_eur is not None
        and budget_eur is not None
        and budget_eur < profile.min_budget_eur
    ):
        disqualifiers.append(
            f"budget_below_min:{budget_eur:.0f}<{profile.min_budget_eur:.0f}"
        )

    score = 0

    # 1. CPV prefix alignment (35 pts max)
    cpv_prefixes = ["".join(ch for ch in p if ch.isdigit()) for p in profile.cpv_prefixes if p.strip()]
    cpv_prefixes = [p for p in cpv_prefixes if p]
    if cpv_prefixes:
        matched_cpvs = [
            code
            for code in tender.cpv_codes
            if any(code.startswith(pref) for pref in cpv_prefixes)
        ]
        if matched_cpvs:
            score += 35
            matched_signals.append(f"cpv:{','.join(matched_cpvs[:3])}")
    else:
        score += 20

    # 2. Include keyword matches (35 pts max)
    inc_keywords = [kw.strip().lower() for kw in profile.include_keywords if kw.strip()]
    if inc_keywords:
        matched_kws = [kw for kw in inc_keywords if kw in searchable_text]
        if matched_kws:
            score += round(35 * (len(matched_kws) / len(inc_keywords)))
            matched_signals.append(f"keywords:{','.join(matched_kws)}")
    else:
        score += 20

    # 3. Country alignment (10 pts max)
    if allowed_countries:
        if tender_country in allowed_countries:
            score += 10
            matched_signals.append(f"country:{tender_country}")
    else:
        score += 5

    # 4. Budget verification (10 pts max)
    if budget_eur is not None:
        if profile.min_budget_eur is None or budget_eur >= profile.min_budget_eur:
            score += 10
            matched_signals.append(f"budget_eur:{budget_eur:.0f}")
    elif profile.min_budget_eur is None:
        score += 3

    # 5. Deadline runway (10 pts max)
    if days_remaining is not None:
        if days_remaining >= max(7, profile.min_days_remaining):
            score += 10
            matched_signals.append(f"runway:{days_remaining}d")
        elif days_remaining >= max(0, profile.min_days_remaining):
            score += 5
            matched_signals.append(f"runway:{days_remaining}d")

    is_eligible = len(disqualifiers) == 0
    final_score = max(0, min(100, score)) if is_eligible else 0

    return ScoredTender(
        tender=tender,
        dedup_key=build_dedup_key(tender),
        bid_fit_score=final_score,
        matched_signals=tuple(matched_signals),
        disqualifiers=tuple(disqualifiers),
        days_remaining=days_remaining,
        budget_eur=budget_eur,
        is_eligible=is_eligible,
    )


def score_and_rank_tenders(
    tenders: Sequence[NormalizedTender],
    profile: SavedSearchProfile,
    *,
    now_utc: datetime | None = None,
) -> tuple[list[ScoredTender], list[ScoredTender], int]:
    """Deduplicate, score, and partition tenders into eligible and disqualified ranked lists."""
    deduped = deduplicate_tenders(tenders)
    eligible: list[ScoredTender] = []
    disqualified: list[ScoredTender] = []
    filtered_sharia_count = 0

    for tender in deduped:
        scored = score_tender(tender, profile, now_utc=now_utc)
        if any(d.startswith("sharia_veto:") for d in scored.disqualifiers):
            filtered_sharia_count += 1
        if scored.is_eligible:
            eligible.append(scored)
        else:
            disqualified.append(scored)

    eligible.sort(
        key=lambda item: (
            -item.bid_fit_score,
            item.days_remaining if item.days_remaining is not None else 9999,
            item.tender.notice_id,
        )
    )
    disqualified.sort(key=lambda item: item.tender.notice_id)
    return eligible, disqualified, filtered_sharia_count
