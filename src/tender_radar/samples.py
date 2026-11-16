"""Live-grounded TenderRadar CPV vertical sample generator and static HTML renderer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sys
from typing import Any, Sequence
import urllib.request

from eu_uk_procurement_mcp.models import NormalizedTender, TenderSearchReport, TenderSearchRequest
from eu_uk_procurement_mcp.procurement_client import search_unified_tenders

from .digest import (
    COMBINED_LEGAL_ATTRIBUTION,
    _format_budget,
    _format_deadline,
    _safe_http_url,
    strip_sharia_vetoed,
)
from .models import SavedSearchProfile, ScoredTender
from .scorer import score_and_rank_tenders


@dataclass(frozen=True)
class SampleVertical:
    """Definition of a B2B procurement vertical for live sample proof generation."""

    slug: str
    title: str
    cpv_prefix: str
    keyword: str
    description: str
    exclude_keywords: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    min_budget_eur: float | None = None
    sources: tuple[str, ...] = ("uk", "eu")
    limit: int = 5


@dataclass(frozen=True)
class VerticalSampleResult:
    """Generated live sample proof artifact and scoring summary for one vertical."""

    vertical: SampleVertical
    query_keyword: str
    query_cpv_prefix: str
    report: TenderSearchReport
    eligible: tuple[ScoredTender, ...]
    disqualified: tuple[ScoredTender, ...]
    html_path: Path
    byte_size: int


SAMPLE_VERTICALS: tuple[SampleVertical, ...] = (
    SampleVertical(
        slug="cloud-software",
        title="Cloud & Enterprise Software",
        cpv_prefix="72",
        keyword="software",
        description="IT services, software development, consulting, and support tenders under CPV 72.",
    ),
    SampleVertical(
        slug="cybersecurity",
        title="Cybersecurity & Security Software",
        cpv_prefix="7221",
        keyword="security",
        description="Security software programming, identity controls, and audit tenders under CPV 7221.",
    ),
    SampleVertical(
        slug="data-analytics",
        title="Data Processing & Analytics",
        cpv_prefix="7231",
        keyword="data",
        description="Data processing, hosting, database, and analytics services under CPV 7231.",
    ),
    SampleVertical(
        slug="managed-infra",
        title="Managed Cloud & IT Infrastructure",
        cpv_prefix="7251",
        keyword="cloud",
        description="Computer-related management, cloud hosting, and facility operations under CPV 7251.",
    ),
    SampleVertical(
        slug="enterprise-platforms",
        title="Enterprise Software Packages & Platforms",
        cpv_prefix="48",
        keyword="platform",
        description="Software packages, licensing, and enterprise information systems under CPV 48.",
    ),
)

_SAMPLE_CSS = (
    ":root{--bg:#0b1120;--card:#111827;--bd:#1f2937;--tx:#f3f4f6;--mu:#9ca3af;"
    "--ac:#38bdf8;--gn:#34d399;--rd:#f87171}"
    "*{box-sizing:border-box}"
    "body{margin:0;padding:24px;background:var(--bg);color:var(--tx);"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.5}"
    ".wrap{max-width:920px;margin:0 auto}"
    "header{display:flex;justify-content:space-between;align-items:center;"
    "border-bottom:1px solid var(--bd);padding-bottom:14px;margin-bottom:20px;gap:12px;flex-wrap:wrap}"
    ".brand{font-weight:700;font-size:1.15rem;color:#fff;text-decoration:none}"
    ".cta{display:inline-block;padding:8px 14px;border-radius:6px;background:var(--ac);"
    "color:#082f49;font-weight:600;text-decoration:none;font-size:.9rem}"
    ".cta-alt{display:inline-block;padding:8px 14px;border-radius:6px;border:1px solid var(--bd);"
    "color:var(--tx);text-decoration:none;font-size:.9rem;margin-left:8px}"
    "h1{font-size:1.7rem;margin:0 0 8px 0}h2{font-size:1.15rem;margin:22px 0 10px 0;color:#fff}"
    "p{color:var(--mu);margin:0 0 10px 0}"
    ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px;margin:16px 0}"
    ".card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:16px;margin-bottom:14px}"
    ".tag{display:inline-block;font-size:.76rem;color:var(--gn);border:1px solid #065f46;"
    "padding:2px 8px;border-radius:4px;margin-bottom:8px}"
    ".tag-warn{color:var(--rd);border-color:#7f1d1d}"
    "pre{background:#030712;border:1px solid var(--bd);border-radius:6px;padding:12px;"
    "overflow-x:auto;font-size:.8rem;color:#e5e7eb;margin:8px 0 0 0;white-space:pre-wrap}"
    "a{color:var(--ac)}.meta{font-size:.86rem;color:var(--mu)}"
    "footer{margin-top:28px;padding-top:14px;border-top:1px solid var(--bd);font-size:.8rem;color:var(--mu)}"
)


def build_vertical_query_candidates(vertical: SampleVertical) -> tuple[tuple[str, str], ...]:
    """Return ordered live query fallbacks when an exact CPV+keyword query yields zero matches."""
    candidates: list[tuple[str, str]] = [
        (vertical.cpv_prefix, vertical.keyword),
        (vertical.cpv_prefix, "services"),
        (vertical.cpv_prefix, ""),
        ("", vertical.keyword),
        ("", "services"),
    ]
    deduped: list[tuple[str, str]] = []
    for pair in candidates:
        if pair not in deduped:
            deduped.append(pair)
    return tuple(deduped)


def fetch_live_vertical_tenders(
    vertical: SampleVertical,
    *,
    cache_db_path: str | None = None,
    use_cache: bool = True,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[TenderSearchReport, str, str]:
    """Fetch real live notices for a vertical via search_unified_tenders, broadening only on empty HTTP 200."""
    last_report: TenderSearchReport | None = None
    for cpv_prefix, keyword in build_vertical_query_candidates(vertical):
        req = TenderSearchRequest(
            keyword=keyword,
            sources=vertical.sources,
            cpv_prefix=cpv_prefix,
            country="",
            active_only=True,
            limit=vertical.limit,
            use_cache=use_cache,
            cache_db_path=cache_db_path,
        )
        report = search_unified_tenders(req, opener=opener)
        if report.status == "BLOCKED_SHARIA":
            raise RuntimeError(
                f"Vertical '{vertical.slug}' query blocked by ethical sector guard: {report.errors}"
            )
        last_report = report
        if report.tenders:
            return report, keyword, cpv_prefix

    status = last_report.status if last_report else "UNKNOWN"
    raise RuntimeError(
        f"No live notices returned for vertical '{vertical.slug}' after broadening queries (status={status})."
    )


def build_profile_for_vertical(vertical: SampleVertical) -> SavedSearchProfile:
    """Construct the deterministic SavedSearchProfile used to score a sample vertical."""
    return SavedSearchProfile(
        profile_id=f"sample-{vertical.slug}",
        name=vertical.title,
        cpv_prefixes=(vertical.cpv_prefix,) if vertical.cpv_prefix else (),
        include_keywords=(vertical.keyword,) if vertical.keyword else (),
        exclude_keywords=vertical.exclude_keywords,
        countries=vertical.countries,
        min_budget_eur=vertical.min_budget_eur,
        min_days_remaining=0,
        sources=vertical.sources,
        limit=vertical.limit,
    )


def build_raw_portal_json_preview(tender: NormalizedTender) -> dict[str, Any]:
    """Build a compact raw portal JSON field preview strictly from fetched notice fields."""
    short_title = tender.title[:96] + ("..." if len(tender.title) > 96 else "")
    if tender.source == "uk_contracts_finder":
        return {
            "ocid": tender.notice_id,
            "date": tender.published_at,
            "tag": [tender.notice_type],
            "buyer": {"name": tender.buyer_name or "UK Public Authority"},
            "tender": {
                "title": short_title,
                "classification": {"id": tender.cpv_codes[0] if tender.cpv_codes else ""},
                "value": {"amount": tender.estimated_value, "currency": tender.currency},
                "tenderPeriod": {"endDate": tender.deadline_at},
            },
        }
    return {
        "publication-number": tender.notice_id,
        "publication-date": tender.published_at,
        "notice-type": tender.notice_type,
        "notice-title": {"eng": short_title},
        "buyer-name": tender.buyer_name or "Public Contracting Authority",
        "organisation-country-buyer": [tender.country],
        "classification-cpv": list(tender.cpv_codes[:4]),
        "estimated-value-proc": tender.estimated_value,
        "estimated-value-cur-proc": tender.currency,
        "deadline-receipt-tender-date-lot": tender.deadline_at,
    }


def format_five_line_digest_block(scored: ScoredTender) -> str:
    """Format a scored tender as a crisp 5-line TenderRadar Bid-Fit Digest block."""
    t = scored.tender
    safe_url = _safe_http_url(t.url)
    cpvs = ", ".join(t.cpv_codes[:4]) if t.cpv_codes else "None"
    disq = ", ".join(scored.disqualifiers) if scored.disqualifiers else "none"
    halal_gate = f"PASS ({t.sharia_reason})" if t.sharia_allowed else f"BLOCKED ({t.sharia_reason})"
    short_title = t.title[:96] + ("..." if len(t.title) > 96 else "")
    return "\n".join(
        [
            f"[{scored.bid_fit_score}/100] {short_title} ({t.notice_id})",
            f"Buyer: {t.buyer_name or 'Public Authority'} ({t.country} | {t.source})",
            f"CPV: {cpvs} | Est: {_format_budget(scored)} | Deadline: {_format_deadline(scored)}",
            f"Disqualifiers: {disq} | Halal CPV Gate: {halal_gate}",
            f"Link: {safe_url}",
        ]
    )


def _render_notice_card(scored: ScoredTender) -> str:
    t = scored.tender
    safe_url = html.escape(_safe_http_url(t.url), quote=True)
    cpvs = html.escape(", ".join(t.cpv_codes[:4]) if t.cpv_codes else "None")
    signals = html.escape(", ".join(scored.matched_signals) if scored.matched_signals else "baseline")
    disq = html.escape(", ".join(scored.disqualifiers) if scored.disqualifiers else "none")
    halal_status = "PASS" if t.sharia_allowed else "BLOCKED"
    halal_text = html.escape(f"{halal_status} — {t.sharia_reason.rstrip('.')}")
    tag_class = "tag" if scored.is_eligible else "tag tag-warn"
    status_label = f"Score {scored.bid_fit_score}/100 — Eligible" if scored.is_eligible else f"Score {scored.bid_fit_score}/100 — Disqualified"
    deadline_days = f"{scored.days_remaining}d remaining." if scored.days_remaining is not None else "Unspecified days."
    return (
        '<article class="card">'
        f'<span class="{tag_class}">{html.escape(status_label)}</span>'
        f'<p><strong><a href="{safe_url}">{html.escape(t.title)}</a></strong></p>'
        f'<p class="meta">Buyer: {html.escape(t.buyer_name or "Public Authority")}. '
        f'Country: {html.escape(t.country)}. Source: {html.escape(t.source)}. '
        f'Notice ID: {html.escape(t.notice_id)}.</p>'
        f'<p class="meta">CPV: {cpvs}. Value: {html.escape(_format_budget(scored))}. '
        f'Deadline: {html.escape(_format_deadline(scored))}. {html.escape(deadline_days)}</p>'
        f'<p class="meta">Matched signals: {signals}. Disqualifiers: {disq}. '
        f'Halal CPV Gate: {halal_text}.</p>'
        f'<p class="meta">Notice URL: <a href="{safe_url}">{safe_url}</a>.</p>'
        "</article>"
    )


def render_sample_html(
    vertical: SampleVertical,
    *,
    report: TenderSearchReport,
    query_keyword: str,
    query_cpv_prefix: str,
    eligible: Sequence[ScoredTender],
    disqualified: Sequence[ScoredTender],
    generated_at_utc: str,
) -> str:
    """Render a self-contained, zero-media, <15 KB HTML proof page for one CPV vertical."""
    clean_eligible, veto_eligible = strip_sharia_vetoed(eligible)
    clean_disq, veto_disq = strip_sharia_vetoed(disqualified)
    sharia_excluded_count = report.filtered_out_sharia_count + veto_eligible + veto_disq

    ordered = clean_eligible + clean_disq
    if not ordered:
        raise ValueError(f"Cannot render sample HTML for '{vertical.slug}' with zero scored notices.")

    lead = ordered[0]
    raw_preview = json.dumps(build_raw_portal_json_preview(lead.tender), indent=2, ensure_ascii=False)
    digest_preview = format_five_line_digest_block(lead)
    notice_cards = "".join(_render_notice_card(item) for item in ordered[:5])
    sources_str = ", ".join(report.sources_succeeded) or ", ".join(report.sources_requested)
    query_note = f"CPV {query_cpv_prefix or 'all'} | keyword '{query_keyword or 'any'}'"

    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>TenderRadar Sample — {html.escape(vertical.title)}</title>"
        f"<style>{_SAMPLE_CSS}</style>"
        "</head>"
        "<body>"
        '<div class="wrap">'
        "<header>"
        '<a class="brand" href="../index.html">TenderRadar</a>'
        "<div>"
        '<a class="cta" href="mailto:kenzailab@gmail.com?subject=TenderRadar%20Sample%20Request">Request Digest</a>'
        '<a class="cta-alt" href="https://github.com/shiny-apricot/tender-radar">GitHub Repo</a>'
        "</div>"
        "</header>"
        "<main>"
        f"<h1>{html.escape(vertical.title)}</h1>"
        f"<p>{html.escape(vertical.description)}</p>"
        f'<p class="meta">Live query: {html.escape(query_note)}. Sources: {html.escape(sources_str)}. '
        f"Fetched: {len(report.tenders)} notices. Eligible: {len(clean_eligible)}. "
        f"Disqualified: {len(clean_disq)}. Ethical CPV filtered: {sharia_excluded_count}. "
        f"Captured UTC: {html.escape(generated_at_utc)}.</p>"
        '<div class="grid">'
        '<section class="card">'
        '<span class="tag">Before: Raw Portal JSON</span>'
        "<p>Unscored portal payload with nested language maps and unnormalized currency fields.</p>"
        f"<pre><code>{html.escape(raw_preview)}</code></pre>"
        "</section>"
        '<section class="card">'
        '<span class="tag">After: 5-Line Bid-Fit Digest</span>'
        "<p>Deterministic score, EUR budget estimate, deadline days, disqualifiers, and Halal CPV gate.</p>"
        f"<pre><code>{html.escape(digest_preview)}</code></pre>"
        "</section>"
        "</div>"
        "<h2>Live Scored Notices</h2>"
        f"<p>Top {min(5, len(ordered))} real notices retrieved from EU TED v3 and UK Contracts Finder.</p>"
        f"{notice_cards}"
        "</main>"
        "<footer>"
        f"<p>{html.escape(COMBINED_LEGAL_ATTRIBUTION)}</p>"
        '<p><a href="../index.html">Back to TenderRadar Overview</a>.</p>'
        "</footer>"
        "</div>"
        "</body>"
        "</html>"
    )


def generate_vertical_sample_file(
    vertical: SampleVertical,
    output_dir: Path,
    *,
    cache_db_path: str | None = None,
    use_cache: bool = True,
    opener: urllib.request.OpenerDirector | None = None,
    now_utc: datetime | None = None,
) -> VerticalSampleResult:
    """Fetch live tenders, score them deterministically, and write docs/samples/<slug>.html."""
    ref_now = now_utc.astimezone(timezone.utc) if now_utc else datetime.now(timezone.utc)
    report, used_kw, used_cpv = fetch_live_vertical_tenders(
        vertical,
        cache_db_path=cache_db_path,
        use_cache=use_cache,
        opener=opener,
    )
    profile = build_profile_for_vertical(vertical)
    eligible, disqualified, _ = score_and_rank_tenders(
        report.tenders,
        profile,
        now_utc=ref_now,
    )
    clean_eligible, _ = strip_sharia_vetoed(eligible)
    clean_disq, _ = strip_sharia_vetoed(disqualified)
    html_text = render_sample_html(
        vertical,
        report=report,
        query_keyword=used_kw,
        query_cpv_prefix=used_cpv,
        eligible=clean_eligible,
        disqualified=clean_disq,
        generated_at_utc=ref_now.isoformat(),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{vertical.slug}.html"
    out_path.write_text(html_text, encoding="utf-8")
    byte_size = len(html_text.encode("utf-8"))
    if byte_size >= 15 * 1024:
        raise RuntimeError(f"Generated sample {out_path} exceeded 15 KB budget ({byte_size} bytes).")

    return VerticalSampleResult(
        vertical=vertical,
        query_keyword=used_kw,
        query_cpv_prefix=used_cpv,
        report=report,
        eligible=tuple(clean_eligible),
        disqualified=tuple(clean_disq),
        html_path=out_path,
        byte_size=byte_size,
    )


def generate_all_samples(
    output_dir: Path,
    *,
    verticals: Sequence[SampleVertical] = SAMPLE_VERTICALS,
    cache_db_path: str | None = None,
    use_cache: bool = True,
    opener: urllib.request.OpenerDirector | None = None,
    now_utc: datetime | None = None,
) -> tuple[VerticalSampleResult, ...]:
    """Generate static live-grounded HTML sample proof pages for all configured CPV verticals."""
    results: list[VerticalSampleResult] = []
    for vertical in verticals:
        results.append(
            generate_vertical_sample_file(
                vertical,
                output_dir,
                cache_db_path=cache_db_path,
                use_cache=use_cache,
                opener=opener,
                now_utc=now_utc,
            )
        )
    return tuple(results)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint to generate live-grounded CPV vertical sample HTML proof pages."""
    default_out = Path(__file__).resolve().parents[2] / "docs" / "samples"
    parser = argparse.ArgumentParser(description="Generate TenderRadar live CPV sample proof pages")
    parser.add_argument("--out-dir", default=str(default_out), help="Output directory for sample HTML files.")
    parser.add_argument("--cache-db", default=None, help="Optional SQLite TTL cache path.")
    parser.add_argument("--no-cache", action="store_true", help="Bypass local TTL cache on live fetch.")
    args = parser.parse_args(argv)

    results = generate_all_samples(
        Path(args.out_dir),
        cache_db_path=args.cache_db,
        use_cache=not args.no_cache,
    )
    total_net_bytes = sum(r.report.bytes_transferred for r in results)
    for res in results:
        sys.stdout.write(
            f"{res.vertical.slug}: path={res.html_path} html_bytes={res.byte_size} "
            f"net_bytes={res.report.bytes_transferred} fetched={len(res.report.tenders)} "
            f"eligible={len(res.eligible)} disqualified={len(res.disqualified)} "
            f"query=(cpv={res.query_cpv_prefix!r}, kw={res.query_keyword!r})\n"
        )
    sys.stdout.write(f"total_net_bytes={total_net_bytes}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
