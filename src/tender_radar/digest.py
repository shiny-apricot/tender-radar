"""SQLite seen-state store, SLA continuity checker, digest renderers, and CLI runner."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence
import urllib.parse
import urllib.request

from eu_uk_procurement_mcp.models import NormalizedTender, TenderSearchRequest
from eu_uk_procurement_mcp.procurement_client import search_unified_tenders

from .models import DigestReport, SavedSearchProfile, ScoredTender, SlaCheckResult
from .scorer import score_and_rank_tenders

EU_TED_ATTRIBUTION = (
    "EU tender notices sourced via TED API v3 (European Union, Commission Decision 2011/833/EU)."
)
UK_OGL_ATTRIBUTION = (
    "Contains public sector information licensed under the Open Government Licence v3.0."
)
COMBINED_LEGAL_ATTRIBUTION = f"{EU_TED_ATTRIBUTION} {UK_OGL_ATTRIBUTION}"
DEFAULT_STATE_DB_PATH = ".cache/tender_radar_state.db"
DEFAULT_MAX_SLA_GAP_HOURS = 26.0


class SeenTenderStore:
    """SQLite store tracking delivered tender dedup keys and digest run timestamps for SLA audits."""

    def __init__(self, db_path: str | Path = DEFAULT_STATE_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_tenders (
                    profile_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    notice_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen_utc TEXT NOT NULL,
                    PRIMARY KEY (profile_id, dedup_key)
                );

                CREATE TABLE IF NOT EXISTS digest_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT NOT NULL,
                    ran_at_utc TEXT NOT NULL,
                    new_count INTEGER NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def filter_unseen(
        self,
        profile_id: str,
        scored: Sequence[ScoredTender],
    ) -> tuple[list[ScoredTender], int]:
        """Partition scored tenders into unseen items and count of already-delivered items."""
        if not scored:
            return [], 0
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            rows = conn.execute(
                "SELECT dedup_key FROM seen_tenders WHERE profile_id = ?;",
                (profile_id,),
            ).fetchall()
        seen_keys = {str(r[0]) for r in rows}
        unseen: list[ScoredTender] = []
        already_seen_count = 0
        for item in scored:
            if item.dedup_key in seen_keys:
                already_seen_count += 1
            else:
                unseen.append(item)
        return unseen, already_seen_count

    def mark_seen(
        self,
        profile_id: str,
        scored: Sequence[ScoredTender],
        *,
        seen_at_utc: str | None = None,
    ) -> int:
        """Persist delivered tender dedup keys for a profile and return newly inserted row count."""
        if not scored:
            return 0
        timestamp = seen_at_utc or datetime.now(timezone.utc).isoformat()
        inserted = 0
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            for item in scored:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO seen_tenders
                        (profile_id, dedup_key, notice_id, source, first_seen_utc)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (
                        profile_id,
                        item.dedup_key,
                        item.tender.notice_id,
                        item.tender.source,
                        timestamp,
                    ),
                )
                inserted += max(0, int(cur.rowcount))
            conn.commit()
        return inserted

    def record_digest_run(
        self,
        profile_id: str,
        new_count: int,
        status: str = "SUCCEEDED",
        *,
        ran_at_utc: str | None = None,
    ) -> None:
        """Record a digest execution timestamp in SQLite for SLA gap verification."""
        timestamp = ran_at_utc or datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute(
                """
                INSERT INTO digest_runs (profile_id, ran_at_utc, new_count, status)
                VALUES (?, ?, ?, ?);
                """,
                (profile_id, timestamp, int(new_count), status),
            )
            conn.commit()

    def check_cron_sla(
        self,
        profile_id: str,
        *,
        max_gap_hours: float = DEFAULT_MAX_SLA_GAP_HOURS,
        now_utc: datetime | None = None,
    ) -> SlaCheckResult:
        """Check whether the elapsed gap since the last digest run exceeds max_gap_hours."""
        ref_now = now_utc.astimezone(timezone.utc) if now_utc else datetime.now(timezone.utc)
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            row = conn.execute(
                """
                SELECT ran_at_utc FROM digest_runs
                WHERE profile_id = ?
                ORDER BY run_id DESC LIMIT 1;
                """,
                (profile_id,),
            ).fetchone()
        if row is None or not row[0]:
            return SlaCheckResult(
                is_healthy=True,
                last_run_at_utc=None,
                hours_since_last_run=None,
                max_gap_hours=max_gap_hours,
                pro_rata_credit_eligible=False,
                reason="initial_run_no_prior_gap",
            )

        last_raw = str(row[0])
        normalized_iso = last_raw[:-1] + "+00:00" if last_raw.endswith("Z") else last_raw
        last_dt = datetime.fromisoformat(normalized_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        hours_gap = round((ref_now - last_dt.astimezone(timezone.utc)).total_seconds() / 3600.0, 2)
        if hours_gap > max_gap_hours:
            return SlaCheckResult(
                is_healthy=False,
                last_run_at_utc=last_raw,
                hours_since_last_run=hours_gap,
                max_gap_hours=max_gap_hours,
                pro_rata_credit_eligible=True,
                reason=f"sla_gap_exceeded:{hours_gap:.2f}h>{max_gap_hours:.1f}h",
            )
        return SlaCheckResult(
            is_healthy=True,
            last_run_at_utc=last_raw,
            hours_since_last_run=hours_gap,
            max_gap_hours=max_gap_hours,
            pro_rata_credit_eligible=False,
            reason="within_sla_window",
        )


def _format_budget(scored: ScoredTender) -> str:
    if scored.tender.estimated_value is None:
        return "Undisclosed"
    cur = scored.tender.currency or ""
    base = f"{scored.tender.estimated_value:,.0f} {cur}".strip()
    if scored.budget_eur is not None and (scored.tender.currency or "").upper() != "EUR":
        return f"{base} (~EUR {scored.budget_eur:,.0f})"
    return base


def _format_deadline(scored: ScoredTender) -> str:
    if not scored.tender.deadline_at:
        return "Unspecified"
    if scored.days_remaining is not None:
        return f"{scored.tender.deadline_at} ({scored.days_remaining}d left)"
    return scored.tender.deadline_at


def has_sharia_veto(scored: ScoredTender) -> bool:
    """Return True if a scored notice failed Sharia screening or carries a sharia_veto disqualifier."""
    return (not scored.tender.sharia_allowed) or any(
        d.startswith("sharia_veto:") for d in scored.disqualifiers
    )


def strip_sharia_vetoed(items: Sequence[ScoredTender]) -> tuple[list[ScoredTender], int]:
    """Remove all Sharia-vetoed notices from public output sequences, returning (clean, removed_count)."""
    clean: list[ScoredTender] = []
    removed = 0
    for item in items:
        if has_sharia_veto(item):
            removed += 1
        else:
            clean.append(item)
    return clean, removed


def render_markdown_digest(report: DigestReport) -> str:
    """Render a concise Markdown daily bid-fit digest with legal attribution footer."""
    public_new, removed_new = strip_sharia_vetoed(report.new_tenders)
    public_disq, removed_disq = strip_sharia_vetoed(report.disqualified_tenders)
    sharia_excluded_count = max(report.filtered_sharia_count, removed_new + removed_disq)

    lines: list[str] = [
        f"# TenderRadar Digest: {report.profile_name} (`{report.profile_id}`)",
        "",
        f"- **Generated (UTC)**: `{report.generated_at_utc}`",
        f"- **Sources**: `{', '.join(report.sources_queried)}`",
        f"- **Fetched / Deduplicated / New**: `{report.total_fetched}` / `{report.deduplicated_count}` / `{len(public_new)}`",
        f"- **Already Seen Skipped**: `{report.already_seen_count}`",
        f"- **Ethical Sector Filtered**: `{sharia_excluded_count}`",
        f"- **SLA Continuity**: `{'HEALTHY' if report.sla_status.is_healthy else 'GAP_ALERT'}` (`{report.sla_status.reason}`)",
        "",
    ]

    if not public_new:
        lines.append("No new eligible tenders matched this profile since the previous digest.")
    else:
        lines.append("## Top New Bid-Fit Opportunities")
        lines.append("")
        for idx, item in enumerate(public_new, start=1):
            t = item.tender
            cpvs = ", ".join(t.cpv_codes) if t.cpv_codes else "None"
            signals = ", ".join(item.matched_signals) if item.matched_signals else "baseline"
            lines.extend(
                [
                    f"### {idx}. [{item.bid_fit_score}/100] {t.title}",
                    f"- **Buyer**: {t.buyer_name or 'Public Authority'} (`{t.country}` | `{t.source}`)",
                    f"- **Notice ID**: `{t.notice_id}` | **CPV**: `{cpvs}`",
                    f"- **Estimated Value**: {_format_budget(item)} | **Deadline**: {_format_deadline(item)}",
                    f"- **Fit Signals**: `{signals}`",
                    f"- **Link**: {t.url}",
                    "",
                ]
            )

    if public_disq:
        lines.append("## Disqualified Notices (Filtered Out)")
        lines.append("")
        for item in public_disq:
            lines.append(
                f"- `{item.tender.notice_id}` ({item.tender.title[:80]}): `{', '.join(item.disqualifiers)}`"
            )
        lines.append("")

    lines.extend(["---", f"*{report.attribution}*"])
    return "\n".join(lines)


def _safe_http_url(url: str) -> str:
    """Validate that a tender link uses http/https with a non-empty netloc, else return '#'."""
    cleaned = (url or "").strip()
    if not cleaned:
        return "#"
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return cleaned
    return "#"


def render_html_digest(report: DigestReport) -> str:
    """Render self-contained email-safe HTML digest with zero external media assets."""
    public_new, removed_new = strip_sharia_vetoed(report.new_tenders)
    _, removed_disq = strip_sharia_vetoed(report.disqualified_tenders)
    sharia_excluded_count = max(report.filtered_sharia_count, removed_new + removed_disq)

    rows_html: list[str] = []
    for item in public_new:
        t = item.tender
        safe_url = _safe_http_url(t.url)
        cpvs = html.escape(", ".join(t.cpv_codes) if t.cpv_codes else "None")
        signals = html.escape(", ".join(item.matched_signals) if item.matched_signals else "baseline")
        rows_html.append(
            "<tr>"
            f"<td style=\"padding:10px;border-bottom:1px solid #e2e8f0;font-weight:700;color:#0f172a;\">{item.bid_fit_score}/100</td>"
            f"<td style=\"padding:10px;border-bottom:1px solid #e2e8f0;\">"
            f"<a href=\"{html.escape(safe_url, quote=True)}\" style=\"color:#0369a1;font-weight:600;text-decoration:none;\">{html.escape(t.title)}</a>"
            f"<div style=\"font-size:12px;color:#475569;\">{html.escape(t.buyer_name or 'Public Authority')} ({html.escape(t.country)}) | CPV: {cpvs}</div>"
            f"<div style=\"font-size:12px;color:#64748b;\">Signals: {signals}</div>"
            "</td>"
            f"<td style=\"padding:10px;border-bottom:1px solid #e2e8f0;font-size:13px;\">{html.escape(_format_budget(item))}</td>"
            f"<td style=\"padding:10px;border-bottom:1px solid #e2e8f0;font-size:13px;\">{html.escape(_format_deadline(item))}</td>"
            "</tr>"
        )

    table_block = (
        "<table style=\"width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;\">"
        "<thead><tr style=\"background:#f1f5f9;text-align:left;\">"
        "<th style=\"padding:10px;\">Fit</th>"
        "<th style=\"padding:10px;\">Opportunity</th>"
        "<th style=\"padding:10px;\">Est. Value</th>"
        "<th style=\"padding:10px;\">Deadline</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table>"
        if rows_html
        else "<p style=\"color:#475569;\">No new eligible tenders matched this profile since the previous digest.</p>"
    )

    sla_label = "HEALTHY" if report.sla_status.is_healthy else "GAP_ALERT (Pro-Rata Credit Eligible)"
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
        f"<title>TenderRadar Digest — {html.escape(report.profile_name)}</title></head>"
        "<body style=\"margin:0;padding:24px;background:#f8fafc;color:#0f172a;font-family:Arial,sans-serif;\">"
        "<div style=\"max-width:760px;margin:0 auto;background:#ffffff;border:1px solid #cbd5e1;border-radius:8px;padding:24px;\">"
        f"<h1 style=\"margin:0 0 8px 0;font-size:20px;\">TenderRadar Digest: {html.escape(report.profile_name)}</h1>"
        f"<p style=\"margin:0 0 16px 0;font-size:13px;color:#475569;\">"
        f"Generated: {html.escape(report.generated_at_utc)} | New: {len(public_new)} | "
        f"Skipped Seen: {report.already_seen_count} | Ethical Filtered: {sharia_excluded_count} | "
        f"SLA: {html.escape(sla_label)}</p>"
        f"{table_block}"
        f"<hr style=\"margin:20px 0;border:none;border-top:1px solid #e2e8f0;\">"
        f"<p style=\"font-size:12px;color:#64748b;margin:0;\">{html.escape(report.attribution)}</p>"
        "</div></body></html>"
    )


def build_webhook_payload(report: DigestReport) -> dict[str, Any]:
    """Build compact Slack/Discord/JSON webhook payload for automated daily delivery."""
    public_new, removed_new = strip_sharia_vetoed(report.new_tenders)
    _, removed_disq = strip_sharia_vetoed(report.disqualified_tenders)
    sharia_excluded_count = max(report.filtered_sharia_count, removed_new + removed_disq)

    lines = [
        f"TenderRadar [{report.profile_name}] — {len(public_new)} new bid-fit tenders "
        f"(deduped {report.deduplicated_count} from {report.total_fetched}, SLA: {report.sla_status.reason})"
    ]
    for item in public_new[:5]:
        t = item.tender
        lines.append(
            f"- [{item.bid_fit_score}/100] {t.title} ({t.country}, {_format_budget(item)}, {_format_deadline(item)}) -> {t.url}"
        )
    lines.append(report.attribution)

    return {
        "text": "\n".join(lines),
        "profile_id": report.profile_id,
        "profile_name": report.profile_name,
        "generated_at_utc": report.generated_at_utc,
        "new_count": len(public_new),
        "already_seen_count": report.already_seen_count,
        "filtered_sharia_count": sharia_excluded_count,
        "sharia_excluded_count": sharia_excluded_count,
        "sla_status": report.sla_status.to_dict(),
        "attribution": report.attribution,
        "items": [item.to_dict() for item in public_new],
    }


def _fetch_live_tenders_for_profile(
    profile: SavedSearchProfile,
    *,
    cache_db_path: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> list[NormalizedTender]:
    keywords = profile.include_keywords or ("",)
    cpvs = profile.cpv_prefixes or ("",)
    countries = profile.countries or ("",)
    collected: list[NormalizedTender] = []

    for kw in keywords:
        for cpv in cpvs:
            for ctry in countries:
                req = TenderSearchRequest(
                    keyword=kw,
                    sources=profile.sources,
                    cpv_prefix=cpv,
                    country=ctry,
                    active_only=True,
                    limit=profile.limit,
                    use_cache=True,
                    cache_db_path=cache_db_path,
                )
                search_report = search_unified_tenders(req, opener=opener)
                if search_report.status == "BLOCKED_SHARIA":
                    raise RuntimeError(
                        f"Profile query blocked by ethical sector guard: {search_report.errors}"
                    )
                collected.extend(search_report.tenders)
    return collected


def run_daily_digest(
    profile: SavedSearchProfile,
    *,
    state_db_path: str | Path = DEFAULT_STATE_DB_PATH,
    cache_db_path: str | None = None,
    fixture_tenders: Sequence[NormalizedTender] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    now_utc: datetime | None = None,
    max_sla_gap_hours: float = DEFAULT_MAX_SLA_GAP_HOURS,
) -> DigestReport:
    """Execute one stateless TenderRadar digest cycle with deduplication, scoring, and SLA tracking."""
    ref_now = now_utc.astimezone(timezone.utc) if now_utc else datetime.now(timezone.utc)
    store = SeenTenderStore(state_db_path)
    sla_status = store.check_cron_sla(
        profile.profile_id,
        max_gap_hours=max_sla_gap_hours,
        now_utc=ref_now,
    )

    if fixture_tenders is not None:
        raw_tenders = list(fixture_tenders)
    else:
        raw_tenders = _fetch_live_tenders_for_profile(
            profile,
            cache_db_path=cache_db_path,
            opener=opener,
        )

    eligible, disqualified, filtered_sharia_count = score_and_rank_tenders(
        raw_tenders,
        profile,
        now_utc=ref_now,
    )
    public_eligible, _ = strip_sharia_vetoed(eligible)
    public_disqualified, _ = strip_sharia_vetoed(disqualified)
    unseen_new, already_seen_count = store.filter_unseen(profile.profile_id, public_eligible)
    store.mark_seen(
        profile.profile_id,
        unseen_new,
        seen_at_utc=ref_now.isoformat(),
    )
    store.record_digest_run(
        profile.profile_id,
        new_count=len(unseen_new),
        status="SUCCEEDED",
        ran_at_utc=ref_now.isoformat(),
    )

    return DigestReport(
        profile_id=profile.profile_id,
        profile_name=profile.name,
        generated_at_utc=ref_now.isoformat(),
        sources_queried=profile.sources,
        total_fetched=len(raw_tenders),
        deduplicated_count=len(eligible) + len(disqualified),
        filtered_sharia_count=filtered_sharia_count,
        already_seen_count=already_seen_count,
        new_tenders=tuple(unseen_new),
        disqualified_tenders=tuple(public_disqualified),
        sla_status=sla_status,
        attribution=COMBINED_LEGAL_ATTRIBUTION,
    )


def _load_fixture_tenders(path: Path) -> list[NormalizedTender]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw.get("tenders") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise ValueError(f"Fixture JSON at {path} must be a list or dict with 'tenders' list.")
    tenders: list[NormalizedTender] = []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("Each fixture tender entry must be a JSON object.")
        cpv_raw = item.get("cpv_codes", ())
        tenders.append(
            NormalizedTender(
                source=str(item["source"]),
                notice_id=str(item["notice_id"]),
                title=str(item["title"]),
                buyer_name=str(item.get("buyer_name", "")),
                country=str(item.get("country", "")),
                published_at=str(item.get("published_at", "")),
                deadline_at=item.get("deadline_at"),
                cpv_codes=tuple(str(c) for c in cpv_raw),
                estimated_value=float(item["estimated_value"]) if item.get("estimated_value") is not None else None,
                currency=str(item["currency"]) if item.get("currency") is not None else None,
                notice_type=str(item.get("notice_type", "tender")),
                url=str(item.get("url", "")),
                sharia_allowed=bool(item.get("sharia_allowed", True)),
                sharia_reason=str(item.get("sharia_reason", "Permissible public sector procurement.")),
            )
        )
    return tenders


def _load_profile_from_args(args: argparse.Namespace) -> SavedSearchProfile:
    if args.profile_json:
        data = json.loads(Path(args.profile_json).read_text(encoding="utf-8"))
        return SavedSearchProfile(
            profile_id=str(data["profile_id"]),
            name=str(data["name"]),
            cpv_prefixes=tuple(data.get("cpv_prefixes", ())),
            include_keywords=tuple(data.get("include_keywords", ())),
            exclude_keywords=tuple(data.get("exclude_keywords", ())),
            countries=tuple(data.get("countries", ())),
            min_budget_eur=float(data["min_budget_eur"]) if data.get("min_budget_eur") is not None else None,
            min_days_remaining=int(data.get("min_days_remaining", 0)),
            sources=tuple(data.get("sources", ("uk", "eu"))),
            limit=int(data.get("limit", 20)),
        )

    include_kws = tuple(k.strip() for k in args.keyword.split(",") if k.strip())
    cpvs = tuple(c.strip() for c in args.cpv.split(",") if c.strip())
    countries = tuple(c.strip() for c in args.country.split(",") if c.strip())
    return SavedSearchProfile(
        profile_id=args.profile_id,
        name=args.profile_name,
        cpv_prefixes=cpvs,
        include_keywords=include_kws,
        countries=countries,
        limit=args.limit,
    )


def _render_report_format(report: DigestReport, fmt: str) -> str:
    if fmt == "markdown":
        return render_markdown_digest(report)
    if fmt == "html":
        return render_html_digest(report)
    if fmt == "webhook":
        return json.dumps(build_webhook_payload(report), indent=2, ensure_ascii=False)
    raise ValueError(f"Unsupported digest format: {fmt}")


def _write_text_output(path_str: str, content: str) -> None:
    out_path = Path(path_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for TenderRadar daily bid-fit digest generation."""
    parser = argparse.ArgumentParser(description="TenderRadar EU/UK Automated Bid-Fit Digest")
    parser.add_argument("--profile-json", help="Path to SavedSearchProfile JSON file.")
    parser.add_argument("--profile-id", default="default-profile", help="Profile identifier.")
    parser.add_argument("--profile-name", default="EU/UK Software Bid-Fit", help="Profile display name.")
    parser.add_argument("--keyword", default="software", help="Comma-separated include keywords.")
    parser.add_argument("--cpv", default="48,72", help="Comma-separated CPV prefixes.")
    parser.add_argument("--country", default="", help="Comma-separated ISO2/ISO3 country codes.")
    parser.add_argument("--limit", type=int, default=20, help="Max upstream notices per query.")
    parser.add_argument("--fixture-json", help="Optional deterministic JSON fixture file.")
    parser.add_argument("--db-path", default=DEFAULT_STATE_DB_PATH, help="SQLite seen-state DB path.")
    parser.add_argument("--format", choices=("markdown", "html", "webhook"), default="markdown")
    parser.add_argument("--out", help="Optional output file path for --format.")
    parser.add_argument("--markdown-out", help="Optional output file path for Markdown digest.")
    parser.add_argument("--html-out", help="Optional output file path for HTML digest.")
    parser.add_argument("--webhook-out", help="Optional output file path for webhook JSON payload.")

    args = parser.parse_args(argv)
    profile = _load_profile_from_args(args)
    fixture = _load_fixture_tenders(Path(args.fixture_json)) if args.fixture_json else None

    report = run_daily_digest(
        profile,
        state_db_path=args.db_path,
        fixture_tenders=fixture,
    )

    wrote_any = False
    for fmt, target_path in (
        ("markdown", args.markdown_out),
        ("html", args.html_out),
        ("webhook", args.webhook_out),
    ):
        if target_path:
            _write_text_output(target_path, _render_report_format(report, fmt))
            wrote_any = True

    if args.out:
        _write_text_output(args.out, _render_report_format(report, args.format))
        wrote_any = True

    if not wrote_any:
        sys.stdout.write(_render_report_format(report, args.format) + "\n")
    return 0
