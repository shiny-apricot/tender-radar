"""Deterministic Law 0 ethical sector and CPV screening for public tenders."""

from __future__ import annotations

import re
from typing import Iterable

BLOCKED_CPV_PREFIXES: dict[str, str] = {
    "159": "Beverages and alcoholic drinks (CPV 159*)",
    "353": "Weapons, ammunition and associated parts (CPV 353*)",
    "354": "Military vehicles and associated parts (CPV 354*)",
    "357": "Military electronic systems (CPV 357*)",
    "6611": "Banking services (interest finance) (CPV 6611*)",
}

HARAM_SECTOR_TOKENS: dict[str, tuple[str, ...]] = {
    "weapons_defense": (
        "weapon",
        "weapons",
        "firearm",
        "firearms",
        "ammunition",
        "munition",
        "munitions",
        "missile",
        "missiles",
        "torpedo",
        "artillery",
        "warhead",
        "warheads",
        "warship",
        "warships",
        "armaments",
        "ordnance",
        "rifle",
        "rifles",
        "handgun",
        "handguns",
        "grenade",
        "grenades",
        "combat",
    ),
    "alcohol_tobacco_pork": (
        "alcohol",
        "alcoholic",
        "liquor",
        "brewery",
        "brewing",
        "winery",
        "distillery",
        "beer",
        "wine",
        "whiskey",
        "vodka",
        "tobacco",
        "cigarette",
        "cigarettes",
        "cigar",
        "cigars",
        "vape",
        "vaping",
        "nicotine",
        "pork",
        "bacon",
        "swine",
    ),
    "gambling_lottery": (
        "gambling",
        "gamble",
        "casino",
        "casinos",
        "lottery",
        "lotteries",
        "lotto",
        "betting",
        "sportsbook",
        "bookmaker",
        "poker",
        "roulette",
        "blackjack",
        "jackpot",
        "sweepstakes",
    ),
    "adult_entertainment": (
        "porn",
        "pornography",
        "pornographic",
        "erotic",
        "erotica",
        "escort",
        "nsfw",
        "xxx",
    ),
    "interest_finance": (
        "riba",
        "usury",
        "banking",
        "payday",
        "mortgage",
        "mortgages",
        "loan",
        "loans",
        "lending",
        "bond",
        "bonds",
        "debenture",
        "debentures",
        "forex",
        "derivatives",
    ),
}

COMPOUND_HARAM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "weapons_defense",
        re.compile(r"\b(small[-_\s]?arms|defense[-_\s]?munitions|military[-_\s]?weapons)\b", re.IGNORECASE),
    ),
    (
        "interest_finance",
        re.compile(
            r"\b(interest[-_\s]?bearing|conventional[-_\s]?bond|sovereign[-_\s]?bond|treasury[-_\s]?bond|bank[-_\s]?loan|credit[-_\s]?facility)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "gambling_lottery",
        re.compile(r"(national[-_\s]?lottery|gaming[-_\s]?machine|sports[-_\s]?betting)", re.IGNORECASE),
    ),
    (
        "adult_entertainment",
        re.compile(r"(adult[-_\s]?entertainment|adult[-_\s]?content)", re.IGNORECASE),
    ),
)


class ShariaGuardBlockedError(ValueError):
    """Raised when a query or tender matches a prohibited CPV code or sector."""


def _screen_text_tokens(text: str) -> tuple[bool, str]:
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return True, "Permissible empty text."

    tokens = [tok for tok in re.split(r"[^a-z0-9]+", cleaned) if tok]
    for category, blocked_words in HARAM_SECTOR_TOKENS.items():
        blocked_set = set(blocked_words)
        for tok in tokens:
            if tok in blocked_set:
                return False, f"Blocked under sector policy ({category}): matched token '{tok}'"

    for category, pattern in COMPOUND_HARAM_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return False, f"Blocked under sector policy ({category}): matched '{match.group(0)}'"

    return True, "Permissible sector text."


def _screen_cpv_codes(cpv_codes: Iterable[str]) -> tuple[bool, str]:
    for raw_code in cpv_codes:
        digits = "".join(ch for ch in str(raw_code) if ch.isdigit())
        if not digits:
            continue
        for prefix, label in BLOCKED_CPV_PREFIXES.items():
            if digits.startswith(prefix):
                return False, f"Blocked under CPV exclusion policy: {label} ({digits})"
    return True, "Permissible CPV classification."


def is_query_halal(keyword: str, cpv_prefix: str = "") -> tuple[bool, str]:
    """Screen search query keyword and CPV prefix before issuing upstream requests."""
    if cpv_prefix.strip():
        cpv_ok, cpv_reason = _screen_cpv_codes([cpv_prefix])
        if not cpv_ok:
            return False, cpv_reason

    kw_ok, kw_reason = _screen_text_tokens(keyword)
    if not kw_ok:
        return False, kw_reason

    return True, "Permissible procurement search query."


def is_tender_halal(
    title: str,
    description: str = "",
    cpv_codes: Iterable[str] = (),
    buyer_name: str = "",
) -> tuple[bool, str]:
    """Screen a procurement notice's CPV codes, title, description, and buyer name."""
    cpv_ok, cpv_reason = _screen_cpv_codes(cpv_codes)
    if not cpv_ok:
        return False, cpv_reason

    combined_text = " ".join(part for part in (title, description, buyer_name) if part)
    text_ok, text_reason = _screen_text_tokens(combined_text)
    if not text_ok:
        return False, text_reason

    return True, "Permissible public sector procurement."
