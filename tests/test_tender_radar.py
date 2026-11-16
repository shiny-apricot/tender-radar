"""Deterministic unit and contract tests for TenderRadar."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import io
import json
from pathlib import Path
import re
import socket
import sys
import threading
import urllib.error
import urllib.request

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PRODUCT_ROOT.parent.parent
PROCUREMENT_SRC = REPO_ROOT / "products" / "eu_uk_procurement_tenders_mcp_actor" / "src"

for _p in (str(PRODUCT_ROOT / "src"), str(PROCUREMENT_SRC), str(REPO_ROOT / "src")):
    if Path(_p).is_dir() and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from autonomy_corp.context_firewall import detect_hollow_or_mock_functions
    from autonomy_corp.marketing_linter import lint_marketing_file, lint_marketing_text
    from autonomy_corp.outbound_truth_gate import (
        EvidenceItem,
        Offering,
        OutboundPackage,
        check_outbound_package,
        find_leaks,
    )

    HAS_MONOREPO_GATES = True
except ImportError:
    HAS_MONOREPO_GATES = False


def _standalone_find_hollow_functions(source_code: str) -> list[str]:
    tree = ast.parse(source_code)
    hollow: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            stmts = (
                body[1:]
                if body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)
                else body
            )
            if not stmts:
                hollow.append(node.name)
            elif len(stmts) == 1 and isinstance(stmts[0], ast.Pass):
                hollow.append(node.name)
            elif (
                len(stmts) == 1
                and isinstance(stmts[0], ast.Expr)
                and isinstance(stmts[0].value, ast.Constant)
                and stmts[0].value.value is Ellipsis
            ):
                hollow.append(node.name)
    return sorted(set(hollow))


def _standalone_find_leaks(text: str) -> list[str]:
    patterns = (r"file://", r"/home/\w+", r"localhost", r"127\.0\.0\.1", r"\bMe[şs]veret\b")
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]
from eu_uk_procurement_mcp.models import NormalizedTender
from tender_radar.digest import (
    COMBINED_LEGAL_ATTRIBUTION,
    SeenTenderStore,
    _safe_http_url,
    build_webhook_payload,
    main as cli_main,
    render_html_digest,
    render_markdown_digest,
    run_daily_digest,
)
from tender_radar.models import SavedSearchProfile
from tender_radar.polar_guard import verify_polar_checkout_url
from tender_radar.samples import (
    SAMPLE_VERTICALS,
    build_vertical_query_candidates,
    fetch_live_vertical_tenders,
    generate_all_samples,
    render_sample_html,
)
from tender_radar.scorer import (
    build_dedup_key,
    deduplicate_tenders,
    score_and_rank_tenders,
    score_tender,
)


def _sample_tender(
    *,
    source: str = "eu_ted_v3",
    notice_id: str = "756436-2026",
    title: str = "License management software package",
    buyer_name: str = "Ministerul Justitiei",
    country: str = "ROU",
    cpv_codes: tuple[str, ...] = ("48218000",),
    estimated_value: float | None = 1881155.9,
    currency: str | None = "RON",
    deadline_at: str | None = "2026-12-09+02:00",
    sharia_allowed: bool = True,
    sharia_reason: str = "Permissible public sector procurement.",
) -> NormalizedTender:
    return NormalizedTender(
        source=source,
        notice_id=notice_id,
        title=title,
        buyer_name=buyer_name,
        country=country,
        published_at="2026-11-02+01:00",
        deadline_at=deadline_at,
        cpv_codes=cpv_codes,
        estimated_value=estimated_value,
        currency=currency,
        notice_type="cn-standard",
        url=f"https://ted.europa.eu/en/notice/-/detail/{notice_id}",
        sharia_allowed=sharia_allowed,
        sharia_reason=sharia_reason,
    )


def test_bid_fit_scoring_and_ranking_deterministic() -> None:
    profile = SavedSearchProfile(
        profile_id="soft-eu-uk",
        name="EU/UK Software",
        cpv_prefixes=("48", "72"),
        include_keywords=("software", "cloud"),
        exclude_keywords=("hardware",),
        countries=("ROU", "GBR"),
        min_budget_eur=50000.0,
        min_days_remaining=5,
    )
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    t_partial = _sample_tender(
        notice_id="100-2026",
        title="License management software package",
        cpv_codes=("48218000",),
        estimated_value=500000.0,
        currency="RON",
        deadline_at="2026-12-01T12:00:00Z",
    )
    t_strong = _sample_tender(
        notice_id="200-2026",
        title="Cloud software platform development",
        buyer_name="Surrey County Council",
        country="GBR",
        cpv_codes=("72200000",),
        estimated_value=120000.0,
        currency="GBP",
        deadline_at="2026-12-10T12:00:00Z",
    )

    eligible, disqualified, sharia_filtered = score_and_rank_tenders(
        [t_partial, t_strong], profile, now_utc=now
    )
    assert sharia_filtered == 0
    assert disqualified == []
    assert [s.tender.notice_id for s in eligible] == ["200-2026", "100-2026"]
    assert eligible[0].bid_fit_score == 100
    assert eligible[1].bid_fit_score == 83


def test_disqualifiers_and_sharia_cpv_veto() -> None:
    profile = SavedSearchProfile(
        profile_id="strict-filter",
        name="Strict Profile",
        cpv_prefixes=("48",),
        include_keywords=("software",),
        exclude_keywords=("legacy",),
        countries=("GBR",),
        min_budget_eur=100000.0,
        min_days_remaining=10,
    )
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    haram_cpv = _sample_tender(
        notice_id="301-2026",
        title="Military radar software",
        country="GBR",
        cpv_codes=("35710000",),
        estimated_value=500000.0,
        currency="GBP",
        deadline_at="2026-12-15T12:00:00Z",
    )
    haram_sector = _sample_tender(
        notice_id="302-2026",
        title="National lottery software platform",
        country="GBR",
        cpv_codes=("48000000",),
        estimated_value=500000.0,
        currency="GBP",
        deadline_at="2026-12-15T12:00:00Z",
    )
    multi_disq = _sample_tender(
        notice_id="303-2026",
        title="Legacy software maintenance",
        country="FRA",
        cpv_codes=("48000000",),
        estimated_value=10000.0,
        currency="EUR",
        deadline_at="2026-11-18T12:00:00Z",
    )

    s_cpv = score_tender(haram_cpv, profile, now_utc=now)
    assert s_cpv.is_eligible is False
    assert s_cpv.bid_fit_score == 0
    assert any(d.startswith("sharia_veto:") for d in s_cpv.disqualifiers)

    s_sec = score_tender(haram_sector, profile, now_utc=now)
    assert s_sec.is_eligible is False
    assert any(d.startswith("sharia_veto:") for d in s_sec.disqualifiers)

    s_multi = score_tender(multi_disq, profile, now_utc=now)
    assert s_multi.is_eligible is False
    assert s_multi.bid_fit_score == 0
    assert "excluded_keyword:legacy" in s_multi.disqualifiers
    assert "country_mismatch:FRA" in s_multi.disqualifiers
    assert any(d.startswith("deadline_too_soon:") for d in s_multi.disqualifiers)
    assert any(d.startswith("budget_below_min:") for d in s_multi.disqualifiers)


def test_cross_source_deduplication_keeps_richer_notice() -> None:
    uk_sparse = _sample_tender(
        source="uk_contracts_finder",
        notice_id="ocds-b5fd17-123",
        title="Cloud Security Audit Platform!",
        buyer_name="Surrey County Council",
        country="GBR",
        cpv_codes=("72200000",),
        estimated_value=None,
        currency=None,
        deadline_at=None,
    )
    eu_rich = _sample_tender(
        source="eu_ted_v3",
        notice_id="999888-2026",
        title="Cloud Security Audit Platform",
        buyer_name="Surrey County Council",
        country="GBR",
        cpv_codes=("72200000", "72222300"),
        estimated_value=150000.0,
        currency="GBP",
        deadline_at="2026-12-20T12:00:00Z",
    )

    assert build_dedup_key(uk_sparse) == build_dedup_key(eu_rich)
    deduped = deduplicate_tenders([uk_sparse, eu_rich])
    assert len(deduped) == 1
    assert deduped[0].notice_id == "999888-2026"
    assert deduped[0].estimated_value == 150000.0


def test_sqlite_seen_state_and_sla_gap_tracking(tmp_path: Path) -> None:
    db_path = tmp_path / "radar.db"
    profile = SavedSearchProfile(
        profile_id="daily-soft",
        name="Daily Software",
        cpv_prefixes=("48",),
        include_keywords=("software",),
        countries=("ROU",),
    )
    t1 = _sample_tender(notice_id="101-2026")
    t2 = _sample_tender(notice_id="102-2026", title="Enterprise ERP software suite")

    day1 = datetime(2026, 11, 15, 7, 0, tzinfo=timezone.utc)
    r1 = run_daily_digest(
        profile,
        state_db_path=db_path,
        fixture_tenders=[t1],
        now_utc=day1,
    )
    assert r1.sla_status.is_healthy is True
    assert r1.sla_status.pro_rata_credit_eligible is False
    assert len(r1.new_tenders) == 1
    assert r1.already_seen_count == 0

    # Day 2 within 24h: t1 skipped as already seen, t2 delivered as new
    day2 = day1 + timedelta(hours=24)
    r2 = run_daily_digest(
        profile,
        state_db_path=db_path,
        fixture_tenders=[t1, t2],
        now_utc=day2,
    )
    assert r2.sla_status.is_healthy is True
    assert r2.sla_status.pro_rata_credit_eligible is False
    assert r2.already_seen_count == 1
    assert [x.tender.notice_id for x in r2.new_tenders] == ["102-2026"]

    # Day 3 after 30h (>26h SLA threshold): gap alert triggers pro-rata credit eligibility
    day3 = day2 + timedelta(hours=30)
    store = SeenTenderStore(db_path)
    sla = store.check_cron_sla(profile.profile_id, now_utc=day3)
    assert sla.is_healthy is False
    assert sla.pro_rata_credit_eligible is True
    assert sla.hours_since_last_run == 30.0
    assert sla.reason.startswith("sla_gap_exceeded:")


def test_digest_renderers_and_cli_fixture_execution(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_state.db"
    fixture_path = tmp_path / "fixture.json"
    out_md = tmp_path / "digest.md"

    tender = _sample_tender(notice_id="756436-2026")
    fixture_path.write_text(json.dumps({"tenders": [tender.to_dict()]}), encoding="utf-8")

    exit_code = cli_main(
        [
            "--profile-id",
            "cli-test",
            "--profile-name",
            "CLI Test Profile",
            "--keyword",
            "software",
            "--cpv",
            "48",
            "--country",
            "ROU",
            "--fixture-json",
            str(fixture_path),
            "--db-path",
            str(db_path),
            "--format",
            "markdown",
            "--out",
            str(out_md),
        ]
    )
    assert exit_code == 0
    md_text = out_md.read_text(encoding="utf-8")
    assert "Decision 2011/833/EU" in md_text
    assert "Contains public sector information licensed under the Open Government Licence v3.0" in md_text
    assert "756436-2026" in md_text

    # Re-run to verify HTML and webhook renderers cleanly
    profile = SavedSearchProfile(
        profile_id="render-test",
        name="Render Profile",
        cpv_prefixes=("48",),
        include_keywords=("software",),
        countries=("ROU",),
    )
    report = run_daily_digest(
        profile,
        state_db_path=tmp_path / "render.db",
        fixture_tenders=[tender],
        now_utc=datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc),
    )
    html_out = render_html_digest(report)
    assert "<img" not in html_out.lower()
    assert "<video" not in html_out.lower()
    assert COMBINED_LEGAL_ATTRIBUTION in html_out

    webhook = build_webhook_payload(report)
    assert webhook["new_count"] == 1
    assert webhook["attribution"] == COMBINED_LEGAL_ATTRIBUTION
    assert "756436-2026" in webhook["items"][0]["tender"]["notice_id"]
    assert "Decision 2011/833/EU" in webhook["text"]


def test_live_digest_fails_closed_on_upstream_error(tmp_path: Path) -> None:
    class FailingHandler(urllib.request.BaseHandler):
        def https_open(self, req: urllib.request.Request) -> io.BytesIO:
            raise urllib.error.HTTPError(
                req.full_url,
                503,
                "Service Unavailable",
                {},
                io.BytesIO(b"upstream unavailable"),
            )

    failing_opener = urllib.request.OpenerDirector()
    failing_opener.add_handler(FailingHandler())
    profile = SavedSearchProfile(
        profile_id="live-fail",
        name="Live Fail Closed",
        cpv_prefixes=("48",),
        include_keywords=("software",),
        countries=("GBR",),
    )
    with pytest.raises(RuntimeError, match="All requested procurement upstreams failed"):
        run_daily_digest(
            profile,
            state_db_path=tmp_path / "fail.db",
            cache_db_path=str(tmp_path / "cache.db"),
            opener=failing_opener,
        )


def test_polar_guard_local_server_and_validation() -> None:
    class MockPolarHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/checkout-redirect-root":
                self.send_response(302)
                self.send_header("Location", "https://polar.sh")
                self.end_headers()
            elif self.path == "/checkout-local-root":
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            elif self.path == "/checkout-live":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>Polar Checkout</body></html>")
            elif self.path == "/checkout-soft-404":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><head><title>404 Not Found</title></head><body><h1>404</h1><p>Page not found</p></body></html>")
            elif self.path == "/checkout-homepage-200":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>Polar \xe2\x80\x94 A billing platform for the intelligence era</body></html>")
            elif self.path == "/checkout-no-cues":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><p>Hello world</p></body></html>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), MockPolarHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        res_default_blocked = verify_polar_checkout_url(f"{base}/checkout-live")
        assert res_default_blocked.is_live is False
        assert res_default_blocked.reason == "non_https_scheme"

        res_root = verify_polar_checkout_url(f"{base}/checkout-redirect-root", allow_loopback_for_tests=True)
        assert res_root.is_live is False
        assert res_root.status_code == 302
        assert res_root.reason == "redirect_to_root"

        res_local_root = verify_polar_checkout_url(f"{base}/checkout-local-root", allow_loopback_for_tests=True)
        assert res_local_root.is_live is False
        assert res_local_root.reason == "redirect_to_root"

        res_live = verify_polar_checkout_url(f"{base}/checkout-live", allow_loopback_for_tests=True)
        assert res_live.is_live is True
        assert res_live.status_code == 200
        assert res_live.reason == "checkout_live"

        res_soft_404 = verify_polar_checkout_url(f"{base}/checkout-soft-404", allow_loopback_for_tests=True)
        assert res_soft_404.is_live is False
        assert res_soft_404.status_code == 200
        assert res_soft_404.reason == "soft_404_page"

        res_homepage_200 = verify_polar_checkout_url(f"{base}/checkout-homepage-200", allow_loopback_for_tests=True)
        assert res_homepage_200.is_live is False
        assert res_homepage_200.status_code == 200
        assert res_homepage_200.reason == "polar_homepage_content"

        res_no_cues = verify_polar_checkout_url(f"{base}/checkout-no-cues", allow_loopback_for_tests=True)
        assert res_no_cues.is_live is False
        assert res_no_cues.status_code == 200
        assert res_no_cues.reason == "missing_checkout_cues"

        res_404 = verify_polar_checkout_url(f"{base}/missing", allow_loopback_for_tests=True)
        assert res_404.is_live is False
        assert res_404.status_code == 404
        assert res_404.reason == "http_error:404"
    finally:
        server.shutdown()
        server.server_close()

    res_http = verify_polar_checkout_url("http://buy.polar.sh/polar_cl_test")
    assert res_http.is_live is False
    assert res_http.reason == "non_https_scheme"

    res_bad_host = verify_polar_checkout_url("https://example.org/checkout")
    assert res_bad_host.is_live is False
    assert res_bad_host.reason == "non_polar_domain"

    with pytest.raises(ValueError):
        verify_polar_checkout_url("")


def test_landing_page_and_readme_pass_truth_gate_and_linter() -> None:
    paused_notice = (
        "Early-access cohorts & free 7-day sample digests onboarding via kenzailab@gmail.com "
        "— self-serve Polar checkout re-opens once cloud scheduler billing clears."
    )

    readme_path = PRODUCT_ROOT / "README.md"
    readme_text = readme_path.read_text(encoding="utf-8")
    assert "buy.polar.sh" not in readme_text
    assert "pro-rata" not in readme_text.lower()
    assert paused_notice in readme_text
    assert "kenzailab@gmail.com" in readme_text
    assert "https://github.com/shiny-apricot/tender-radar" in readme_text

    landing_path = PRODUCT_ROOT / "docs" / "index.html"
    assert landing_path.is_file()
    raw_html = landing_path.read_text(encoding="utf-8")
    assert len(raw_html.encode("utf-8")) < 15 * 1024
    assert "buy.polar.sh" not in raw_html
    assert "pro-rata" not in raw_html.lower()
    assert paused_notice in html.unescape(raw_html)
    assert "https://github.com/shiny-apricot/tender-radar" in raw_html
    assert '<meta property="og:image" content="https://shiny-apricot.github.io/tender-radar/assets/og-card.png">' in raw_html
    assert '<meta name="twitter:card" content="summary_large_image">' in raw_html
    assert "mailto:kenzailab@gmail.com?subject=TenderRadar%207-Day%20Sample" in raw_html
    assert "<img" not in raw_html.lower()
    assert "<video" not in raw_html.lower()
    assert "<script" not in raw_html.lower()
    assert "javascript:" not in raw_html.lower()
    assert "Decision 2011/833/EU" in raw_html
    assert "Contains public sector information licensed under the Open Government Licence v3.0" in raw_html

    # Strip HTML tags while preserving code blocks for marketing_linter text validation
    no_style = re.sub(r"<style[\s\S]*?</style>", " ", raw_html, flags=re.IGNORECASE)
    with_code = re.sub(r"<pre><code>([\s\S]*?)</code></pre>", r"\n```\n\1\n```\n", no_style)
    visible_text = html.unescape(re.sub(r"<[^>]+>", " ", with_code))

    if HAS_MONOREPO_GATES:
        readme_lint = lint_marketing_file(readme_path, is_external=True)
        assert readme_lint.is_clean is True, f"README linter issues: {readme_lint.issues}"
        assert find_leaks(readme_text) == []

        file_lint = lint_marketing_file(landing_path, is_external=True)
        assert file_lint.is_clean is True, f"Landing file linter issues: {file_lint.issues}"

        landing_lint = lint_marketing_text(visible_text, is_external=True)
        assert landing_lint.is_clean is True, f"Landing linter issues: {landing_lint.issues}"
        assert find_leaks(raw_html) == []
        assert find_leaks(visible_text) == []

        from autonomy_corp.marketing_linter import lint_funnel_parity

        frozen_offering = Offering(
            id="tenderradar-pro",
            name="TenderRadar Pro",
            price=49.0,
            status="paused_pending_token",
            fulfillment_verified=False,
            attached_custom_fields=("cpv_and_keywords", "target_countries", "delivery_webhook_url"),
            active_cron_runner=False,
        )
        parity = lint_funnel_parity(readme_text, raw_html, frozen_offering)
        assert parity.is_clean is True, f"Funnel parity issues on frozen docs: {parity.issues}"

        sample_pkg = OutboundPackage(
            channel="landing_cta",
            recipient="kenzailab@gmail.com",
            subject="TenderRadar Free 7-Day Sample Digest",
            body=(
                "Early-access cohorts & free 7-day sample digests onboarding via kenzailab@gmail.com "
                "— self-serve Polar checkout re-opens once cloud scheduler billing clears."
            ),
            evidence=(
                EvidenceItem("TenderRadar source", "https://github.com/shiny-apricot/tender-radar"),
            ),
            source_mode="live",
            authored_by="worker-solution-coder",
            reviewed_by="worker-anti-slop-qa-gate",
            founder_approved=True,
            offering_id="iktisad-oss",
        )
        sample_offerings = {
            "iktisad-oss": Offering(
                id="iktisad-oss",
                name="Open-source MIT CLI",
                price=0.0,
                status="live",
                fulfillment_verified=True,
            ),
        }
        sample_check = check_outbound_package(sample_pkg, offerings=sample_offerings)
        assert sample_check.ok is True
        assert sample_check.violations == ()
    else:
        assert _standalone_find_leaks(readme_text) == []
        assert _standalone_find_leaks(raw_html) == []
        assert _standalone_find_leaks(visible_text) == []


def test_no_hollow_or_mock_functions_in_tender_radar_source() -> None:
    py_files = sorted((PRODUCT_ROOT / "src" / "tender_radar").glob("*.py"))
    assert len(py_files) >= 5
    for py_file in py_files:
        src = py_file.read_text(encoding="utf-8")
        hollow = detect_hollow_or_mock_functions(src) if HAS_MONOREPO_GATES else _standalone_find_hollow_functions(src)
        assert hollow == [], f"Hollow/stub functions detected in {py_file.name}: {hollow}"


def test_html_digest_neutralizes_javascript_url_xss(tmp_path: Path) -> None:
    base_tender = _sample_tender(notice_id="888-2026")
    xss_tender = NormalizedTender(
        source=base_tender.source,
        notice_id=base_tender.notice_id,
        title=base_tender.title,
        buyer_name=base_tender.buyer_name,
        country=base_tender.country,
        published_at=base_tender.published_at,
        deadline_at=base_tender.deadline_at,
        cpv_codes=base_tender.cpv_codes,
        estimated_value=base_tender.estimated_value,
        currency=base_tender.currency,
        notice_type=base_tender.notice_type,
        url="javascript:alert(1)",
        sharia_allowed=base_tender.sharia_allowed,
        sharia_reason=base_tender.sharia_reason,
    )
    profile = SavedSearchProfile(
        profile_id="xss-check",
        name="XSS Check",
        cpv_prefixes=("48",),
        include_keywords=("software",),
        countries=("ROU",),
    )
    report = run_daily_digest(
        profile,
        state_db_path=tmp_path / "xss.db",
        fixture_tenders=[xss_tender],
        now_utc=datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc),
    )
    html_out = render_html_digest(report)
    assert "javascript:" not in html_out.lower()
    assert 'href="#"' in html_out


def test_cli_single_pass_emits_markdown_html_and_webhook_simultaneously(tmp_path: Path) -> None:
    db_path = tmp_path / "single_pass.db"
    fixture_path = tmp_path / "fixture.json"
    out_md = tmp_path / "out" / "digest.md"
    out_html = tmp_path / "out" / "digest.html"
    out_webhook = tmp_path / "out" / "webhook.json"

    tender = _sample_tender(notice_id="909090-2026")
    fixture_path.write_text(json.dumps({"tenders": [tender.to_dict()]}), encoding="utf-8")

    exit_code = cli_main(
        [
            "--profile-id",
            "atomic-pass",
            "--profile-name",
            "Atomic Pass Profile",
            "--keyword",
            "software",
            "--cpv",
            "48",
            "--country",
            "ROU",
            "--fixture-json",
            str(fixture_path),
            "--db-path",
            str(db_path),
            "--markdown-out",
            str(out_md),
            "--html-out",
            str(out_html),
            "--webhook-out",
            str(out_webhook),
        ]
    )
    assert exit_code == 0
    assert out_md.is_file()
    assert out_html.is_file()
    assert out_webhook.is_file()

    assert "909090-2026" in out_md.read_text(encoding="utf-8")
    assert "License management software package" in out_html.read_text(encoding="utf-8")
    webhook_payload = json.loads(out_webhook.read_text(encoding="utf-8"))
    assert webhook_payload["new_count"] == 1
    assert webhook_payload["items"][0]["tender"]["notice_id"] == "909090-2026"

    # Subsequent invocation sees it was already delivered
    out_webhook_2 = tmp_path / "out" / "webhook_2.json"
    exit_code_2 = cli_main(
        [
            "--profile-id",
            "atomic-pass",
            "--profile-name",
            "Atomic Pass Profile",
            "--keyword",
            "software",
            "--cpv",
            "48",
            "--country",
            "ROU",
            "--fixture-json",
            str(fixture_path),
            "--db-path",
            str(db_path),
            "--webhook-out",
            str(out_webhook_2),
        ]
    )
    assert exit_code_2 == 0
    webhook_payload_2 = json.loads(out_webhook_2.read_text(encoding="utf-8"))
    assert webhook_payload_2["new_count"] == 0
    assert webhook_payload_2["already_seen_count"] == 1


def test_polar_guard_blocks_ssrf_private_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    for blocked_ip in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in blocked_ip else socket.AF_INET
        sockaddr = (blocked_ip, 443, 0, 0) if family == socket.AF_INET6 else (blocked_ip, 443)
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, type=0, _fam=family, _sa=sockaddr: [(_fam, socket.SOCK_STREAM, 6, "", _sa)],
        )
        res = verify_polar_checkout_url("https://buy.polar.sh/polar_c_test")
        assert res.is_live is False
        assert res.reason == f"blocked_private_ip:{blocked_ip}"

    res_https_loopback = verify_polar_checkout_url("https://127.0.0.1/checkout")
    assert res_https_loopback.is_live is False
    assert res_https_loopback.reason == "non_polar_domain"


def test_sample_proof_pages_exist_and_pass_linter_and_truth_gate() -> None:
    samples_dir = PRODUCT_ROOT / "docs" / "samples"
    expected_slugs = [
        "cloud-software",
        "cybersecurity",
        "data-analytics",
        "managed-infra",
        "enterprise-platforms",
    ]
    assert [v.slug for v in SAMPLE_VERTICALS] == expected_slugs

    landing_html = (PRODUCT_ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert "kenzailab@gmail.com" in landing_html
    assert "https://github.com/shiny-apricot/tender-radar" in landing_html

    for slug in expected_slugs:
        sample_path = samples_dir / f"{slug}.html"
        assert sample_path.is_file(), f"Missing sample proof page: {sample_path}"
        assert f"samples/{slug}.html" in landing_html

        raw_html = sample_path.read_text(encoding="utf-8")
        assert len(raw_html.encode("utf-8")) < 15 * 1024
        assert "https://github.com/shiny-apricot/tender-radar" in raw_html
        assert "sharia_veto:" not in raw_html
        assert "<img" not in raw_html.lower()
        assert "<video" not in raw_html.lower()
        assert "<script" not in raw_html.lower()
        assert "javascript:" not in raw_html.lower()
        assert "Decision 2011/833/EU" in raw_html
        assert "Contains public sector information licensed under the Open Government Licence v3.0" in raw_html
        assert "https://" in raw_html
        assert "Halal CPV Gate:" in raw_html
        assert "Disqualifiers:" in raw_html

        if HAS_MONOREPO_GATES:
            lint_report = lint_marketing_file(sample_path, is_external=True)
            assert lint_report.is_clean is True, f"Linter issues in {slug}.html: {lint_report.issues}"
            assert find_leaks(raw_html) == []
        else:
            assert _standalone_find_leaks(raw_html) == []


def test_samples_generator_broadening_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from eu_uk_procurement_mcp.models import TenderSearchReport, TenderSearchRequest
    import tender_radar.samples as samples_mod

    vertical = SAMPLE_VERTICALS[0]
    candidates = build_vertical_query_candidates(vertical)
    assert candidates[0] == ("72", "software")
    assert ("72", "services") in candidates
    assert ("", "software") in candidates

    calls: list[tuple[str, str]] = []
    live_notice = _sample_tender(
        notice_id="445566-2026",
        title="Cloud software support services",
        cpv_codes=("72267000",),
        deadline_at="2026-12-20T12:00:00Z",
    )

    def _local_search_with_broadening(
        req: TenderSearchRequest,
        *,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> TenderSearchReport:
        del opener
        calls.append((req.cpv_prefix, req.keyword))
        if len(calls) == 1:
            return TenderSearchReport(
                status="SUCCEEDED",
                checked_at_utc="2026-11-15T12:00:00Z",
                keyword=req.keyword,
                sources_requested=req.sources,
                sources_succeeded=("eu",),
                tenders=(),
                total_returned=0,
            )
        return TenderSearchReport(
            status="SUCCEEDED",
            checked_at_utc="2026-11-15T12:00:00Z",
            keyword=req.keyword,
            sources_requested=req.sources,
            sources_succeeded=("eu",),
            tenders=(live_notice,),
            total_returned=1,
        )

    monkeypatch.setattr(samples_mod, "search_unified_tenders", _local_search_with_broadening)
    results = generate_all_samples(
        tmp_path / "samples_out",
        verticals=(vertical,),
        use_cache=False,
        now_utc=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert calls == [("72", "software"), ("72", "services")]
    assert results[0].query_keyword == "services"
    assert results[0].html_path.is_file()
    assert results[0].byte_size < 15 * 1024

    def _failing_search(
        req: TenderSearchRequest,
        *,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> TenderSearchReport:
        del req, opener
        raise RuntimeError("All requested procurement upstreams failed")

    monkeypatch.setattr(samples_mod, "search_unified_tenders", _failing_search)
    with pytest.raises(RuntimeError, match="All requested procurement upstreams failed"):
        fetch_live_vertical_tenders(vertical, use_cache=False)


def test_sharia_vetoed_notices_stripped_from_all_public_outputs(tmp_path: Path) -> None:
    from eu_uk_procurement_mcp.models import TenderSearchReport

    profile = SavedSearchProfile(
        profile_id="sharia-redact",
        name="Halal Redaction Test",
        cpv_prefixes=("48", "72"),
        include_keywords=("software",),
        countries=("ROU", "GBR"),
    )
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    halal_ok = _sample_tender(
        notice_id="111-2026",
        title="Cloud analytics software platform",
        cpv_codes=("72200000",),
        deadline_at="2026-12-20T12:00:00Z",
    )
    haram_bank = _sample_tender(
        notice_id="666-2026",
        title="Municipal banking core credit facility",
        cpv_codes=("66110000",),
        deadline_at="2026-12-20T12:00:00Z",
    )
    haram_alc = _sample_tender(
        notice_id="667-2026",
        title="State winery and brewery ERP",
        cpv_codes=("15910000",),
        deadline_at="2026-12-20T12:00:00Z",
    )

    report = run_daily_digest(
        profile,
        state_db_path=tmp_path / "redact.db",
        fixture_tenders=[halal_ok, haram_bank, haram_alc],
        now_utc=now,
    )
    assert report.filtered_sharia_count == 2
    assert len(report.new_tenders) == 1
    assert report.disqualified_tenders == ()

    md_text = render_markdown_digest(report)
    html_text = render_html_digest(report)
    webhook_json = json.dumps(build_webhook_payload(report))

    for rendered in (md_text, html_text, webhook_json):
        assert "sharia_veto:" not in rendered
        assert "666-2026" not in rendered
        assert "667-2026" not in rendered
        assert "Municipal banking" not in rendered
        assert "winery" not in rendered
        assert "111-2026" in rendered

    eligible, disqualified, _ = score_and_rank_tenders(
        [halal_ok, haram_bank, haram_alc],
        profile,
        now_utc=now,
    )
    sample_report = TenderSearchReport(
        status="SUCCEEDED",
        checked_at_utc=now.isoformat(),
        keyword="software",
        sources_requested=("eu",),
        sources_succeeded=("eu",),
        tenders=(halal_ok, haram_bank, haram_alc),
        total_returned=3,
    )
    sample_html = render_sample_html(
        SAMPLE_VERTICALS[0],
        report=sample_report,
        query_keyword="software",
        query_cpv_prefix="72",
        eligible=eligible,
        disqualified=disqualified,
        generated_at_utc=now.isoformat(),
    )
    assert "sharia_veto:" not in sample_html
    assert "666-2026" not in sample_html
    assert "667-2026" not in sample_html
    assert "Ethical CPV filtered: 2." in sample_html
    assert "111-2026" in sample_html
