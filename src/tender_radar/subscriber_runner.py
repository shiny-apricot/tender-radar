"""Multi-tenant Polar.sh subscriber digest runner with SSRF-hardened webhook delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from eu_uk_procurement_mcp.models import NormalizedTender

from .digest import (
    COMBINED_LEGAL_ATTRIBUTION,
    SeenTenderStore,
    _fetch_live_tenders_for_profile,
    build_webhook_payload,
    strip_sharia_vetoed,
)
from .models import DigestReport, SavedSearchProfile
from .polar_guard import LOCAL_TEST_HOSTS, _check_dns_public
from .scorer import score_and_rank_tenders

POLAR_API_BASE_URL = "https://api.polar.sh/v1"
DEFAULT_PRO_PRODUCT_ID = os.environ.get("TENDERRADAR_PRO_PRODUCT_ID", "tenderradar-pro")
DEFAULT_AGENCY_PRODUCT_ID = os.environ.get("TENDERRADAR_AGENCY_PRODUCT_ID", "tenderradar-agency")
MAX_AGENCY_PROFILES = 5
MAX_PRO_PROFILES = 1
USER_AGENT = "TenderRadar-SubscriberRunner/0.1.0"
BLOCKED_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata",
        "100.100.100.200",
    }
)


@dataclass(frozen=True)
class SubscriberConfig:
    """Validated Polar subscriber configuration and saved search profiles."""

    subscription_id: str
    customer_id: str
    product_id: str
    tier: str
    webhook_url: str
    profiles: tuple[SavedSearchProfile, ...]
    status: str = "active"
    customer_email: str = ""


@dataclass(frozen=True)
class SubscriberDeliveryResult:
    """Outcome of processing and delivering one subscriber search profile."""

    subscription_id: str
    tier: str
    profile_id: str
    new_notices_count: int
    http_status: int | None
    status: str
    response_excerpt: str = ""
    error: str = ""


@dataclass(frozen=True)
class SubscriberCycleReport:
    """Summary telemetry for a complete Polar subscriber digest cycle."""

    ran_at_utc: str
    subscriptions_fetched: int
    profiles_processed: int
    deliveries_attempted: int
    deliveries_succeeded: int
    deliveries_failed: int
    dry_run: bool
    results: tuple[SubscriberDeliveryResult, ...]


def _parse_single_profile_spec(
    raw_spec: str,
    *,
    profile_id: str,
    profile_name: str,
    countries: tuple[str, ...],
) -> SavedSearchProfile:
    cleaned = raw_spec.strip()
    cpvs: list[str] = []
    keywords: list[str] = []

    if ":" in cleaned or "|" in cleaned:
        sep = ":" if ":" in cleaned else "|"
        left, right = cleaned.split(sep, 1)
        for tok in re.split(r"[\s,]+", left.strip()):
            t = tok.strip()
            if not t:
                continue
            if t.isdigit():
                cpvs.append(t)
            else:
                keywords.append(t)
        for tok in re.split(r"[\s,]+", right.strip()):
            t = tok.strip()
            if not t:
                continue
            if t.isdigit() and not cpvs:
                cpvs.append(t)
            else:
                keywords.append(t)
    else:
        for tok in re.split(r"[\s,]+", cleaned):
            t = tok.strip()
            if not t:
                continue
            if t.isdigit():
                cpvs.append(t)
            else:
                keywords.append(t)

    if not cpvs and not keywords:
        raise ValueError(f"Empty profile spec '{raw_spec}' in cpv_and_keywords.")

    return SavedSearchProfile(
        profile_id=profile_id,
        name=profile_name,
        cpv_prefixes=tuple(dict.fromkeys(cpvs)),
        include_keywords=tuple(dict.fromkeys(keywords)),
        countries=countries,
    )


def parse_polar_subscription(
    raw: Mapping[str, Any],
    *,
    pro_product_id: str,
    agency_product_id: str,
) -> SubscriberConfig:
    """Parse a Polar subscription payload into a validated SubscriberConfig."""
    if not isinstance(raw, Mapping):
        raise ValueError("Polar subscription payload must be a JSON object.")

    sub_id = str(raw.get("id") or "").strip()
    if not sub_id:
        raise ValueError("Polar subscription missing 'id'.")

    product_obj = raw.get("product") if isinstance(raw.get("product"), Mapping) else {}
    product_id = str(raw.get("product_id") or product_obj.get("id") or "").strip()
    if product_id == agency_product_id:
        tier = "agency"
        max_profiles = MAX_AGENCY_PROFILES
    elif product_id == pro_product_id:
        tier = "pro"
        max_profiles = MAX_PRO_PROFILES
    else:
        raise ValueError(
            f"Subscription '{sub_id}' has unrecognized product_id '{product_id}'."
        )

    customer_obj = raw.get("customer") if isinstance(raw.get("customer"), Mapping) else {}
    customer_id = str(raw.get("customer_id") or customer_obj.get("id") or "").strip()
    customer_email = str(customer_obj.get("email") or raw.get("customer_email") or "").strip()
    status = str(raw.get("status") or "active").strip().lower()

    custom_raw = raw.get("custom_field_data")
    if not isinstance(custom_raw, Mapping):
        custom_raw = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}

    cpv_and_keywords = str(custom_raw.get("cpv_and_keywords") or "").strip()
    if not cpv_and_keywords:
        raise ValueError(f"Subscription '{sub_id}' missing custom field 'cpv_and_keywords'.")

    target_countries_raw = str(custom_raw.get("target_countries") or "").strip()
    countries = tuple(
        c.strip().upper()
        for c in re.split(r"[\s,;]+", target_countries_raw)
        if c.strip()
    )

    webhook_url = str(custom_raw.get("delivery_webhook_url") or "").strip()
    if not webhook_url:
        raise ValueError(f"Subscription '{sub_id}' missing custom field 'delivery_webhook_url'.")

    raw_specs = [part.strip() for part in cpv_and_keywords.split(";") if part.strip()]
    if not raw_specs:
        raise ValueError(f"Subscription '{sub_id}' has no usable profiles in 'cpv_and_keywords'.")

    selected_specs = raw_specs[:max_profiles]
    profiles = tuple(
        _parse_single_profile_spec(
            spec,
            profile_id=f"sub-{sub_id}-p{idx}",
            profile_name=f"TenderRadar {tier.title()} ({sub_id} #{idx})",
            countries=countries,
        )
        for idx, spec in enumerate(selected_specs, start=1)
    )

    return SubscriberConfig(
        subscription_id=sub_id,
        customer_id=customer_id,
        product_id=product_id,
        tier=tier,
        webhook_url=webhook_url,
        profiles=profiles,
        status=status,
        customer_email=customer_email,
    )


def validate_webhook_https_ssrf(
    url: str,
    *,
    allow_loopback_for_tests: bool = False,
) -> str:
    """Enforce HTTPS and block private/loopback/metadata DNS targets via polar_guard._check_dns_public."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Webhook URL must be a non-empty string.")
    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().strip(".")
    if not scheme or not host:
        raise ValueError(f"Invalid webhook URL '{cleaned}': missing scheme or host.")

    if host in BLOCKED_METADATA_HOSTS:
        raise ValueError(f"Webhook SSRF check failed: blocked metadata host '{host}'.")

    is_local_test = allow_loopback_for_tests and host in LOCAL_TEST_HOSTS
    if is_local_test:
        if scheme not in ("http", "https"):
            raise ValueError(f"Webhook URL '{cleaned}' must use http or https for local test.")
        port = parsed.port or (443 if scheme == "https" else 80)
        dns_err = _check_dns_public(host, port, require_loopback_only=True)
        if dns_err is not None:
            raise ValueError(f"Webhook SSRF check failed: {dns_err}")
        return cleaned

    if scheme != "https":
        raise ValueError(f"Webhook URL '{cleaned}' must use https:// in production.")

    try:
        ip_obj = ipaddress.ip_address(host)
        if not (
            ip_obj.is_global
            and not ip_obj.is_private
            and not ip_obj.is_loopback
            and not ip_obj.is_link_local
            and not ip_obj.is_reserved
            and not ip_obj.is_multicast
            and not ip_obj.is_unspecified
        ):
            raise ValueError(f"Webhook SSRF check failed: blocked_non_public_ip:{host}")
    except ValueError as exc:
        if "Webhook SSRF check failed" in str(exc):
            raise

    port = parsed.port or 443
    dns_err = _check_dns_public(host, port, require_loopback_only=False)
    if dns_err is not None:
        raise ValueError(f"Webhook SSRF check failed: {dns_err}")
    return cleaned


def fetch_active_polar_subscriptions(
    token: str,
    *,
    timeout: float = 10.0,
    api_base_url: str = POLAR_API_BASE_URL,
    opener: urllib.request.OpenerDirector | None = None,
) -> list[dict[str, Any]]:
    """Fetch active Polar subscriptions fail-closed (never catches errors to return [])."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("POLAR_ORG_ACCESS_TOKEN must be a non-empty string.")
    if timeout <= 0:
        raise ValueError("timeout must be positive.")

    clean_token = token.strip()
    base = api_base_url.rstrip("/")
    http_opener = opener or urllib.request.build_opener()
    collected: list[dict[str, Any]] = []
    page = 1

    while True:
        url = f"{base}/subscriptions/?active=true&limit=100&page={page}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {clean_token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with http_opener.open(req, timeout=timeout) as resp:
                status_code = int(getattr(resp, "status", 200))
                if status_code != 200:
                    raise RuntimeError(
                        f"Polar subscriptions API returned unexpected HTTP {status_code}."
                    )
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_preview = ""
            try:
                body_preview = exc.read(256).decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"Polar subscriptions API HTTP {exc.code}: {body_preview or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Polar subscriptions API request failed: {exc}") from exc

        if not isinstance(payload, Mapping):
            raise RuntimeError("Polar subscriptions API returned non-object JSON payload.")

        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeError("Polar subscriptions API response missing 'items' list.")

        for item in items:
            if isinstance(item, dict):
                collected.append(item)

        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), Mapping) else {}
        max_page = int(pagination.get("max_page") or 1)
        if not items or page >= max_page:
            break
        page += 1

    return collected


def deliver_webhook_payload(
    webhook_url: str,
    payload: Mapping[str, Any],
    *,
    timeout: float = 10.0,
    allow_loopback_for_tests: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, str]:
    """Validate SSRF policy and POST Slack/Discord/JSON compatible payload to subscriber webhook."""
    cleaned_url = validate_webhook_https_ssrf(
        webhook_url,
        allow_loopback_for_tests=allow_loopback_for_tests,
    )
    body_obj = dict(payload)
    if "notices" not in body_obj:
        body_obj["notices"] = list(body_obj.get("items", ()))
    if "text" not in body_obj:
        raise ValueError("Webhook payload must contain 'text' field.")

    raw_body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cleaned_url,
        data=raw_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    http_opener = opener or urllib.request.build_opener()
    try:
        with http_opener.open(req, timeout=timeout) as resp:
            status_code = int(getattr(resp, "status", 200))
            excerpt = resp.read(500).decode("utf-8", errors="replace")
            if status_code >= 400:
                raise RuntimeError(f"Webhook returned HTTP {status_code}: {excerpt}")
            return status_code, excerpt
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read(256).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"Webhook delivery HTTP {exc.code}: {err_body or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Webhook delivery network failure: {exc}") from exc


def _init_subscriber_runs_table(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriber_delivery_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                tier TEXT NOT NULL,
                ran_at_utc TEXT NOT NULL,
                new_notices_count INTEGER NOT NULL,
                http_status INTEGER,
                status TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.commit()


def _record_subscriber_delivery(
    db_path: Path,
    result: SubscriberDeliveryResult,
    ran_at_utc: str,
) -> None:
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.execute(
            """
            INSERT INTO subscriber_delivery_runs (
                subscription_id, profile_id, tier, ran_at_utc,
                new_notices_count, http_status, status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                result.subscription_id,
                result.profile_id,
                result.tier,
                ran_at_utc,
                int(result.new_notices_count),
                result.http_status,
                result.status,
                result.error,
            ),
        )
        conn.commit()


def last_subscriber_cycle_utc(db_path: str | Path) -> datetime | None:
    """Return the latest recorded subscriber delivery timestamp from SQLite, if any."""
    path_obj = Path(db_path)
    if not path_obj.is_file():
        return None
    with sqlite3.connect(path_obj, timeout=10.0) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='subscriber_delivery_runs';"
        ).fetchone()
        if not row:
            return None
        ts_row = conn.execute(
            "SELECT ran_at_utc FROM subscriber_delivery_runs ORDER BY run_id DESC LIMIT 1;"
        ).fetchone()
    if not ts_row or not ts_row[0]:
        return None
    raw_ts = str(ts_row[0]).strip()
    norm_ts = raw_ts[:-1] + "+00:00" if raw_ts.endswith("Z") else raw_ts
    dt = datetime.fromisoformat(norm_ts)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_subscriber_digest_cycle(
    token: str,
    *,
    db_path: str | Path,
    now_utc: datetime | None = None,
    dry_run: bool = False,
    fixture_notices: Sequence[NormalizedTender] | None = None,
    pro_product_id: str = DEFAULT_PRO_PRODUCT_ID,
    agency_product_id: str = DEFAULT_AGENCY_PRODUCT_ID,
    api_base_url: str = POLAR_API_BASE_URL,
    allow_loopback_for_tests: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
    deliver_when_empty: bool = False,
) -> SubscriberCycleReport:
    """Fetch active Polar subscriptions, score & redact notices, and dispatch webhooks."""
    ref_now = now_utc.astimezone(timezone.utc) if now_utc else datetime.now(timezone.utc)
    ran_at_utc = ref_now.isoformat()
    db_file = Path(db_path)
    store = SeenTenderStore(db_file)
    _init_subscriber_runs_table(db_file)

    raw_subscriptions = fetch_active_polar_subscriptions(
        token,
        api_base_url=api_base_url,
        opener=opener,
    )

    results: list[SubscriberDeliveryResult] = []
    profiles_processed = 0
    deliveries_attempted = 0
    deliveries_succeeded = 0
    deliveries_failed = 0

    for raw_sub in raw_subscriptions:
        try:
            sub_cfg = parse_polar_subscription(
                raw_sub,
                pro_product_id=pro_product_id,
                agency_product_id=agency_product_id,
            )
        except ValueError as exc:
            sub_id = str(raw_sub.get("id") or "unknown") if isinstance(raw_sub, Mapping) else "unknown"
            res = SubscriberDeliveryResult(
                subscription_id=sub_id,
                tier="unknown",
                profile_id="",
                new_notices_count=0,
                http_status=None,
                status="FAILED",
                error=str(exc),
            )
            _record_subscriber_delivery(db_file, res, ran_at_utc)
            results.append(res)
            deliveries_failed += 1
            continue

        for profile in sub_cfg.profiles:
            profiles_processed += 1
            try:
                sla_status = store.check_cron_sla(profile.profile_id, now_utc=ref_now)
                if fixture_notices is not None:
                    raw_tenders = list(fixture_notices)
                else:
                    raw_tenders = _fetch_live_tenders_for_profile(profile, opener=opener)

                eligible, disqualified, filtered_sharia = score_and_rank_tenders(
                    raw_tenders,
                    profile,
                    now_utc=ref_now,
                )
                public_eligible, _ = strip_sharia_vetoed(eligible)
                public_disqualified, _ = strip_sharia_vetoed(disqualified)
                unseen_new, already_seen_count = store.filter_unseen(
                    profile.profile_id,
                    public_eligible,
                )

                report = DigestReport(
                    profile_id=profile.profile_id,
                    profile_name=profile.name,
                    generated_at_utc=ran_at_utc,
                    sources_queried=profile.sources,
                    total_fetched=len(raw_tenders),
                    deduplicated_count=len(eligible) + len(disqualified),
                    filtered_sharia_count=filtered_sharia,
                    already_seen_count=already_seen_count,
                    new_tenders=tuple(unseen_new),
                    disqualified_tenders=tuple(public_disqualified),
                    sla_status=sla_status,
                    attribution=COMBINED_LEGAL_ATTRIBUTION,
                )
                payload = build_webhook_payload(report)
                payload["notices"] = payload["items"]

                if dry_run:
                    validate_webhook_https_ssrf(
                        sub_cfg.webhook_url,
                        allow_loopback_for_tests=allow_loopback_for_tests,
                    )
                    res = SubscriberDeliveryResult(
                        subscription_id=sub_cfg.subscription_id,
                        tier=sub_cfg.tier,
                        profile_id=profile.profile_id,
                        new_notices_count=len(unseen_new),
                        http_status=None,
                        status="DRY_RUN",
                    )
                    _record_subscriber_delivery(db_file, res, ran_at_utc)
                    results.append(res)
                    continue

                if not unseen_new and not deliver_when_empty:
                    store.record_digest_run(
                        profile.profile_id,
                        new_count=0,
                        status="SUCCEEDED",
                        ran_at_utc=ran_at_utc,
                    )
                    res = SubscriberDeliveryResult(
                        subscription_id=sub_cfg.subscription_id,
                        tier=sub_cfg.tier,
                        profile_id=profile.profile_id,
                        new_notices_count=0,
                        http_status=None,
                        status="SKIPPED_NO_NEW",
                    )
                    _record_subscriber_delivery(db_file, res, ran_at_utc)
                    results.append(res)
                    continue

                deliveries_attempted += 1
                http_status, excerpt = deliver_webhook_payload(
                    sub_cfg.webhook_url,
                    payload,
                    allow_loopback_for_tests=allow_loopback_for_tests,
                    opener=opener,
                )
                store.mark_seen(profile.profile_id, unseen_new, seen_at_utc=ran_at_utc)
                store.record_digest_run(
                    profile.profile_id,
                    new_count=len(unseen_new),
                    status="SUCCEEDED",
                    ran_at_utc=ran_at_utc,
                )
                deliveries_succeeded += 1
                res = SubscriberDeliveryResult(
                    subscription_id=sub_cfg.subscription_id,
                    tier=sub_cfg.tier,
                    profile_id=profile.profile_id,
                    new_notices_count=len(unseen_new),
                    http_status=http_status,
                    status="DELIVERED",
                    response_excerpt=excerpt,
                )
                _record_subscriber_delivery(db_file, res, ran_at_utc)
                results.append(res)
            except Exception as exc:
                deliveries_failed += 1
                status_label = "BLOCKED_SSRF" if "SSRF" in str(exc) else "FAILED"
                res = SubscriberDeliveryResult(
                    subscription_id=sub_cfg.subscription_id,
                    tier=sub_cfg.tier,
                    profile_id=profile.profile_id,
                    new_notices_count=0,
                    http_status=None,
                    status=status_label,
                    error=str(exc),
                )
                _record_subscriber_delivery(db_file, res, ran_at_utc)
                results.append(res)

    return SubscriberCycleReport(
        ran_at_utc=ran_at_utc,
        subscriptions_fetched=len(raw_subscriptions),
        profiles_processed=profiles_processed,
        deliveries_attempted=deliveries_attempted,
        deliveries_succeeded=deliveries_succeeded,
        deliveries_failed=deliveries_failed,
        dry_run=dry_run,
        results=tuple(results),
    )
