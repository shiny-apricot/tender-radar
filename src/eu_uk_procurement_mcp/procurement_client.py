"""EU TED v3 and UK Contracts Finder OCDS procurement client, normalizers, and SQLite TTL cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .models import NormalizedTender, TenderSearchReport, TenderSearchRequest
from .sharia_guard import is_query_halal, is_tender_halal

UK_OCDS_SEARCH_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
EU_TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

MAX_RESPONSE_BYTES = 512 * 1024
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_CACHE_DB_PATH = ".cache/procurement_cache.db"
HONEST_USER_AGENT = "AutonomyCorp-EU-UK-Procurement-MCP/0.1.0 (+https://apify.com/autonomycorp)"

TED_REQUEST_FIELDS: list[str] = [
    "publication-number",
    "notice-title",
    "title-proc",
    "publication-date",
    "buyer-name",
    "organisation-name-buyer",
    "organisation-country-buyer",
    "classification-cpv",
    "estimated-value-proc",
    "estimated-value-cur-proc",
    "estimated-value-lot",
    "estimated-value-cur-lot",
    "deadline-receipt-tender-date-lot",
    "notice-type",
]

ISO2_TO_ISO3_COUNTRY: dict[str, str] = {
    "UK": "GBR",
    "GB": "GBR",
    "DE": "DEU",
    "FR": "FRA",
    "ES": "ESP",
    "IT": "ITA",
    "NL": "NLD",
    "BE": "BEL",
    "PL": "POL",
    "RO": "ROU",
    "SE": "SWE",
    "AT": "AUT",
    "PT": "PRT",
    "IE": "IRL",
    "DK": "DNK",
    "FI": "FIN",
    "CZ": "CZE",
    "HU": "HUN",
    "GR": "GRC",
    "BG": "BGR",
    "HR": "HRV",
    "SK": "SVK",
    "LT": "LTU",
    "LV": "LVA",
    "EE": "EST",
    "SI": "SVN",
    "LU": "LUX",
    "CY": "CYP",
    "MT": "MLT",
    "NO": "NOR",
}


class SqliteTtlCache:
    """Lightweight SQLite key-value TTL cache for normalized procurement reports."""

    def __init__(self, db_path: str | Path = DEFAULT_CACHE_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS procurement_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def get(self, cache_key: str) -> dict[str, Any] | None:
        """Return cached dictionary if present and unexpired."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM procurement_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            payload_json, expires_at = row
            if float(expires_at) <= now:
                conn.execute("DELETE FROM procurement_cache WHERE cache_key = ?", (cache_key,))
                conn.commit()
                return None
            data = json.loads(payload_json)
            return data if isinstance(data, dict) else None

    def set(self, cache_key: str, payload: dict[str, Any], ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        """Store payload dictionary with an expiration timestamp."""
        expires_at = time.time() + max(1, int(ttl_seconds))
        encoded = json.dumps(payload, ensure_ascii=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO procurement_cache (cache_key, payload_json, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    expires_at = excluded.expires_at
                """,
                (cache_key, encoded, expires_at),
            )
            conn.commit()


def _normalize_country_code(country: str) -> str:
    cleaned = (country or "").strip().upper()
    if not cleaned:
        return ""
    return ISO2_TO_ISO3_COUNTRY.get(cleaned, cleaned)


def _http_read_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[dict[str, Any], int]:
    """Perform a bounded stdlib HTTP request and parse JSON fail-closed."""
    headers = {
        "User-Agent": HONEST_USER_AGENT,
        "Accept": "application/json",
    }
    data_bytes: bytes | None = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    active_opener = opener if opener is not None else urllib.request.build_opener()

    try:
        with active_opener.open(req, timeout=timeout_seconds) as resp:
            status_code = getattr(resp, "status", 200)
            if status_code >= 400:
                raise RuntimeError(f"Upstream HTTP {status_code} from {url}")
            raw = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as err:
        err_body = err.read(1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"Upstream HTTP {err.code} from {url}: {err_body}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise RuntimeError(f"Upstream network error from {url}: {err}") from err

    if len(raw) > max_bytes:
        raise RuntimeError(
            f"Upstream payload from {url} exceeded {max_bytes} byte cap ({len(raw)} bytes read)."
        )

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RuntimeError(f"Malformed JSON payload from {url}: {err}") from err

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected JSON object from {url}, got {type(parsed).__name__}")

    return parsed, len(raw)


def _pick_ted_text(value: Any) -> str:
    """Extract a clean display string from TED v3 multilingual dicts or lists."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            picked = _pick_ted_text(item)
            if picked:
                return picked
        return ""
    if isinstance(value, dict):
        if "eng" in value:
            picked = _pick_ted_text(value["eng"])
            if picked:
                return picked
        for lang_val in value.values():
            picked = _pick_ted_text(lang_val)
            if picked:
                return picked
    return str(value).strip()


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list) and value:
        return _coerce_float(value[0])
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            if item:
                return str(item).strip()
        return None
    text = str(value).strip()
    return text if text else None


def normalize_uk_ocds_release(release: dict[str, Any]) -> NormalizedTender:
    """Normalize a single UK Contracts Finder OCDS release into NormalizedTender."""
    tender = release.get("tender") or {}
    buyer = release.get("buyer") or {}
    ocid = str(release.get("ocid") or release.get("id") or "").strip()
    tender_id = str(tender.get("id") or "").strip()
    title = str(tender.get("title") or "").strip()
    buyer_name = str(buyer.get("name") or "").strip()

    if not buyer_name:
        for party in release.get("parties") or []:
            if isinstance(party, dict) and "buyer" in (party.get("roles") or []):
                buyer_name = str(party.get("name") or "").strip()
                if buyer_name:
                    break

    cpv_list: list[str] = []
    main_cls = tender.get("classification") or {}
    if isinstance(main_cls, dict) and main_cls.get("id"):
        cpv_list.append(str(main_cls["id"]).strip())
    for item in tender.get("items") or []:
        if isinstance(item, dict):
            cls_obj = item.get("classification") or {}
            if isinstance(cls_obj, dict) and cls_obj.get("id"):
                code = str(cls_obj["id"]).strip()
                if code not in cpv_list:
                    cpv_list.append(code)

    val_obj = tender.get("value") or {}
    est_val = _coerce_float(val_obj.get("amount") if isinstance(val_obj, dict) else None)
    currency = _coerce_str(val_obj.get("currency") if isinstance(val_obj, dict) else None)

    period = tender.get("tenderPeriod") or {}
    deadline_at = _coerce_str(period.get("endDate") if isinstance(period, dict) else None)

    tags = release.get("tag") or []
    notice_type = str(tags[0]) if isinstance(tags, list) and tags else str(tender.get("status") or "tender")

    if tender_id:
        notice_url = f"https://www.contractsfinder.service.gov.uk/Notice/{urllib.parse.quote(tender_id)}"
    else:
        notice_url = f"https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/{urllib.parse.quote(ocid)}"

    description = str(tender.get("description") or "").strip()
    sharia_ok, sharia_reason = is_tender_halal(
        title=title,
        description=description,
        cpv_codes=cpv_list,
        buyer_name=buyer_name,
    )

    return NormalizedTender(
        source="uk_contracts_finder",
        notice_id=ocid or tender_id,
        title=title,
        buyer_name=buyer_name,
        country="GBR",
        published_at=str(release.get("date") or "").strip(),
        deadline_at=deadline_at,
        cpv_codes=tuple(cpv_list),
        estimated_value=est_val,
        currency=currency,
        notice_type=notice_type,
        url=notice_url,
        sharia_allowed=sharia_ok,
        sharia_reason=sharia_reason,
    )


def normalize_ted_v3_notice(notice: dict[str, Any]) -> NormalizedTender:
    """Normalize a single EU TED API v3 notice object into NormalizedTender."""
    pub_num = str(notice.get("publication-number") or "").strip()
    title = _pick_ted_text(notice.get("notice-title")) or _pick_ted_text(notice.get("title-proc"))
    buyer_name = _pick_ted_text(notice.get("buyer-name")) or _pick_ted_text(notice.get("organisation-name-buyer"))

    raw_countries = notice.get("organisation-country-buyer") or []
    if isinstance(raw_countries, list) and raw_countries:
        country = str(raw_countries[0]).strip().upper()
    elif isinstance(raw_countries, str) and raw_countries.strip():
        country = raw_countries.strip().upper()
    else:
        country = "EUR"

    raw_cpv = notice.get("classification-cpv") or []
    cpv_list: list[str] = []
    if isinstance(raw_cpv, list):
        for code in raw_cpv:
            cleaned_code = str(code).strip()
            if cleaned_code and cleaned_code not in cpv_list:
                cpv_list.append(cleaned_code)
    elif isinstance(raw_cpv, str) and raw_cpv.strip():
        cpv_list.append(raw_cpv.strip())

    est_val = _coerce_float(notice.get("estimated-value-proc"))
    if est_val is None:
        est_val = _coerce_float(notice.get("estimated-value-lot"))

    currency = _coerce_str(notice.get("estimated-value-cur-proc"))
    if currency is None:
        currency = _coerce_str(notice.get("estimated-value-cur-lot"))

    deadline_at = _coerce_str(notice.get("deadline-receipt-tender-date-lot"))
    published_at = str(notice.get("publication-date") or "").strip()
    notice_type = str(notice.get("notice-type") or "ted-notice").strip()

    notice_url = f"https://ted.europa.eu/en/notice/-/detail/{urllib.parse.quote(pub_num)}" if pub_num else "https://ted.europa.eu/"

    sharia_ok, sharia_reason = is_tender_halal(
        title=title,
        description="",
        cpv_codes=cpv_list,
        buyer_name=buyer_name,
    )

    return NormalizedTender(
        source="eu_ted_v3",
        notice_id=pub_num,
        title=title,
        buyer_name=buyer_name,
        country=country,
        published_at=published_at,
        deadline_at=deadline_at,
        cpv_codes=tuple(cpv_list),
        estimated_value=est_val,
        currency=currency,
        notice_type=notice_type,
        url=notice_url,
        sharia_allowed=sharia_ok,
        sharia_reason=sharia_reason,
    )


def _matches_uk_filters(
    release: dict[str, Any],
    normalized: NormalizedTender,
    keyword: str,
    cpv_prefix: str,
) -> bool:
    if cpv_prefix:
        prefix_digits = "".join(ch for ch in cpv_prefix if ch.isdigit())
        if prefix_digits and not any(code.startswith(prefix_digits) for code in normalized.cpv_codes):
            return False

    kw = keyword.strip().lower()
    if kw:
        tender = release.get("tender") or {}
        cls_desc = ""
        if isinstance(tender.get("classification"), dict):
            cls_desc = str(tender["classification"].get("description") or "")
        haystack = " ".join(
            [
                normalized.title,
                normalized.buyer_name,
                str(tender.get("description") or ""),
                cls_desc,
            ]
        ).lower()
        terms = [t for t in kw.split() if t]
        if not all(term in haystack for term in terms):
            return False

    return True


def fetch_uk_contracts_finder(
    *,
    keyword: str = "",
    cpv_prefix: str = "",
    country: str = "",
    active_only: bool = True,
    limit: int = 10,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[list[NormalizedTender], int]:
    """Fetch and normalize live UK Contracts Finder OCDS releases."""
    norm_country = _normalize_country_code(country)
    if norm_country and norm_country != "GBR":
        return [], 0

    clamped_limit = max(1, min(50, int(limit)))
    page_size = min(35, max(10, clamped_limit * 4)) if (keyword.strip() or cpv_prefix.strip()) else clamped_limit
    params: dict[str, str] = {"limit": str(page_size)}
    if active_only:
        params["stages"] = "tender"

    url = f"{UK_OCDS_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    matched: list[NormalizedTender] = []
    total_bytes = 0
    max_pages = 2 if (keyword.strip() or cpv_prefix.strip()) else 1

    next_url: str | None = url
    for _ in range(max_pages):
        if not next_url:
            break
        data, bytes_read = _http_read_json(
            next_url,
            method="GET",
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
        total_bytes += bytes_read
        releases = data.get("releases")
        if not isinstance(releases, list):
            raise RuntimeError("Malformed UK Contracts Finder OCDS response: missing 'releases' list.")

        for release in releases:
            if not isinstance(release, dict):
                continue
            normalized = normalize_uk_ocds_release(release)
            if _matches_uk_filters(release, normalized, keyword, cpv_prefix):
                matched.append(normalized)
                if len(matched) >= clamped_limit:
                    return matched, total_bytes

        links = data.get("links") or {}
        next_candidate = links.get("next") if isinstance(links, dict) else None
        if isinstance(next_candidate, str) and next_candidate.strip():
            candidate_url = next_candidate.strip()
            parsed_next = urllib.parse.urlparse(candidate_url)
            if (
                parsed_next.scheme.lower() != "https"
                or (parsed_next.hostname or "").lower() != "www.contractsfinder.service.gov.uk"
            ):
                raise RuntimeError("Unsafe off-host pagination URL rejected")
            next_url = candidate_url
        else:
            next_url = None

    return matched, total_bytes


def build_ted_expert_query(
    *,
    keyword: str = "",
    cpv_prefix: str = "",
    country: str = "",
    active_only: bool = True,
) -> str:
    """Construct a valid EU TED v3 expert search query string."""
    clauses: list[str] = []
    cleaned_kw = keyword.replace('"', " ").strip()
    if cleaned_kw:
        clauses.append(f'FT ~ "{cleaned_kw}"')

    cpv_digits = "".join(ch for ch in cpv_prefix if ch.isdigit())
    if cpv_digits:
        clauses.append(f"classification-cpv = {cpv_digits}*")

    norm_country = _normalize_country_code(country)
    if norm_country:
        clauses.append(f"organisation-country-buyer = {norm_country}")

    if active_only:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y%m%d")
        clauses.append("notice-type = cn-standard")
        clauses.append(f"PD >= {cutoff}")

    if not clauses:
        clauses.append("notice-type = cn-standard")

    return " AND ".join(clauses)


def fetch_ted_europa_v3(
    *,
    keyword: str = "",
    cpv_prefix: str = "",
    country: str = "",
    active_only: bool = True,
    limit: int = 10,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[list[NormalizedTender], int]:
    """Fetch and normalize live EU TED API v3 procurement notices."""
    clamped_limit = max(1, min(50, int(limit)))
    expert_query = build_ted_expert_query(
        keyword=keyword,
        cpv_prefix=cpv_prefix,
        country=country,
        active_only=active_only,
    )
    payload: dict[str, Any] = {
        "query": expert_query,
        "scope": "ACTIVE" if active_only else "ALL",
        "fields": TED_REQUEST_FIELDS,
        "limit": clamped_limit,
        "page": 1,
    }
    data, bytes_read = _http_read_json(
        EU_TED_SEARCH_URL,
        method="POST",
        payload=payload,
        timeout_seconds=timeout_seconds,
        opener=opener,
    )
    notices = data.get("notices")
    if not isinstance(notices, list):
        raise RuntimeError("Malformed EU TED v3 response: missing 'notices' list.")

    results = [normalize_ted_v3_notice(item) for item in notices if isinstance(item, dict)]
    return results[:clamped_limit], bytes_read


def _normalize_sources(sources: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in sources:
        item = raw.strip().lower()
        if item in ("uk", "uk_contracts_finder"):
            canonical = "uk"
        elif item in ("eu", "ted", "eu_ted_v3"):
            canonical = "eu"
        else:
            raise ValueError(f"Unsupported procurement source '{raw}'. Expected 'uk' or 'eu'.")
        if canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        raise ValueError("At least one procurement source ('uk' or 'eu') must be requested.")
    return tuple(normalized)


def _build_cache_key(request: TenderSearchRequest, sources: tuple[str, ...]) -> str:
    canonical = json.dumps(
        {
            "keyword": request.keyword.strip().lower(),
            "sources": list(sources),
            "cpv_prefix": request.cpv_prefix.strip(),
            "country": _normalize_country_code(request.country),
            "active_only": request.active_only,
            "limit": max(1, min(50, int(request.limit))),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _report_from_cache_dict(data: dict[str, Any]) -> TenderSearchReport:
    tenders = tuple(NormalizedTender(**item) for item in data.get("tenders", []))
    return TenderSearchReport(
        status=str(data["status"]),
        checked_at_utc=str(data["checked_at_utc"]),
        keyword=str(data["keyword"]),
        sources_requested=tuple(data["sources_requested"]),
        sources_succeeded=tuple(data["sources_succeeded"]),
        tenders=tenders,
        total_returned=len(tenders),
        filtered_out_sharia_count=int(data.get("filtered_out_sharia_count", 0)),
        from_cache=True,
        bytes_transferred=0,
        errors=dict(data.get("errors", {})),
    )


def search_unified_tenders(
    request: TenderSearchRequest,
    *,
    opener: urllib.request.OpenerDirector | None = None,
) -> TenderSearchReport:
    """Execute unified, fail-closed, Sharia-filtered search across UK OCDS and EU TED v3."""
    sources = _normalize_sources(request.sources)
    clamped_limit = max(1, min(50, int(request.limit)))
    timeout_seconds = min(30.0, max(1.0, float(request.timeout_seconds)))
    checked_at_utc = datetime.now(timezone.utc).isoformat()

    query_ok, query_reason = is_query_halal(request.keyword, request.cpv_prefix)
    if not query_ok:
        return TenderSearchReport(
            status="BLOCKED_SHARIA",
            checked_at_utc=checked_at_utc,
            keyword=request.keyword,
            sources_requested=sources,
            sources_succeeded=(),
            tenders=(),
            total_returned=0,
            filtered_out_sharia_count=0,
            from_cache=False,
            bytes_transferred=0,
            errors={"sharia_guard": query_reason},
        )

    cache: SqliteTtlCache | None = None
    cache_key = _build_cache_key(request, sources)
    if request.use_cache:
        cache = SqliteTtlCache(request.cache_db_path or DEFAULT_CACHE_DB_PATH)
        cached = cache.get(cache_key)
        if cached is not None:
            return _report_from_cache_dict(cached)

    per_source_limit = clamped_limit if len(sources) == 1 else max(1, (clamped_limit + 1) // 2)
    collected_by_source: dict[str, list[NormalizedTender]] = {}
    sources_succeeded: list[str] = []
    errors: dict[str, str] = {}
    total_bytes = 0

    for src in sources:
        try:
            if src == "uk":
                items, b_read = fetch_uk_contracts_finder(
                    keyword=request.keyword,
                    cpv_prefix=request.cpv_prefix,
                    country=request.country,
                    active_only=request.active_only,
                    limit=per_source_limit,
                    timeout_seconds=timeout_seconds,
                    opener=opener,
                )
            else:
                items, b_read = fetch_ted_europa_v3(
                    keyword=request.keyword,
                    cpv_prefix=request.cpv_prefix,
                    country=request.country,
                    active_only=request.active_only,
                    limit=per_source_limit,
                    timeout_seconds=timeout_seconds,
                    opener=opener,
                )
            collected_by_source[src] = items
            sources_succeeded.append(src)
            total_bytes += b_read
        except RuntimeError as exc:
            errors[src] = str(exc)

    if not sources_succeeded:
        raise RuntimeError(f"All requested procurement upstreams failed: {json.dumps(errors)}")

    # Interleave sources cleanly so unified results include both EU and UK when both succeed.
    merged: list[NormalizedTender] = []
    max_len = max((len(v) for v in collected_by_source.values()), default=0)
    for idx in range(max_len):
        for src in sources_succeeded:
            src_list = collected_by_source[src]
            if idx < len(src_list):
                merged.append(src_list[idx])

    halal_tenders: list[NormalizedTender] = []
    filtered_out = 0
    for tender in merged:
        if not tender.sharia_allowed:
            filtered_out += 1
            continue
        halal_tenders.append(tender)
        if len(halal_tenders) >= clamped_limit:
            break

    status = "SUCCEEDED" if not errors else "PARTIAL_SUCCEEDED"
    report = TenderSearchReport(
        status=status,
        checked_at_utc=checked_at_utc,
        keyword=request.keyword,
        sources_requested=sources,
        sources_succeeded=tuple(sources_succeeded),
        tenders=tuple(halal_tenders),
        total_returned=len(halal_tenders),
        filtered_out_sharia_count=filtered_out,
        from_cache=False,
        bytes_transferred=total_bytes,
        errors=errors,
    )

    if cache is not None:
        cache.set(cache_key, report.to_dict(), ttl_seconds=DEFAULT_CACHE_TTL_SECONDS)

    return report
