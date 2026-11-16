"""Deterministic tests for TenderRadar Polar subscriber runner & daemon Stage 4.75."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import sys
import threading
from typing import Any

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PRODUCT_ROOT.parent.parent
PROCUREMENT_SRC = REPO_ROOT / "products" / "eu_uk_procurement_tenders_mcp_actor" / "src"

for _p in (str(PRODUCT_ROOT / "src"), str(PROCUREMENT_SRC), str(REPO_ROOT)):
    if Path(_p).is_dir() and _p not in sys.path:
        sys.path.insert(0, _p)

from eu_uk_procurement_mcp.models import NormalizedTender
from tender_radar.subscriber_runner import (
    DEFAULT_AGENCY_PRODUCT_ID,
    DEFAULT_PRO_PRODUCT_ID,
    SubscriberConfig,
    SubscriberCycleReport,
    deliver_webhook_payload,
    fetch_active_polar_subscriptions,
    last_subscriber_cycle_utc,
    parse_polar_subscription,
    run_subscriber_digest_cycle,
    validate_webhook_https_ssrf,
)


def _notice(
    notice_id: str,
    title: str,
    *,
    cpv: tuple[str, ...] = ("72200000",),
    country: str = "DEU",
    sharia_allowed: bool = True,
) -> NormalizedTender:
    return NormalizedTender(
        source="eu_ted_v3",
        notice_id=notice_id,
        title=title,
        buyer_name="Public Authority",
        country=country,
        published_at="2026-11-10T10:00:00Z",
        deadline_at="2026-12-20T12:00:00Z",
        cpv_codes=cpv,
        estimated_value=120000.0,
        currency="EUR",
        notice_type="cn-standard",
        url=f"https://ted.europa.eu/en/notice/-/detail/{notice_id}",
        sharia_allowed=sharia_allowed,
        sharia_reason="Permissible" if sharia_allowed else "Prohibited sector",
    )


def test_parse_polar_subscription_pro_and_agency_limits() -> None:
    pro_raw = {
        "id": "sub_pro_1",
        "product_id": "prod_pro",
        "customer_id": "cust_1",
        "status": "active",
        "custom_field_data": {
            "cpv_and_keywords": "72:software; 48:cloud",
            "target_countries": "DEU, GBR",
            "delivery_webhook_url": "https://hooks.example.org/services/123",
        },
    }
    pro_cfg = parse_polar_subscription(
        pro_raw,
        pro_product_id="prod_pro",
        agency_product_id="prod_agency",
    )
    assert isinstance(pro_cfg, SubscriberConfig)
    assert pro_cfg.tier == "pro"
    assert len(pro_cfg.profiles) == 1
    assert pro_cfg.profiles[0].cpv_prefixes == ("72",)
    assert pro_cfg.profiles[0].include_keywords == ("software",)
    assert pro_cfg.profiles[0].countries == ("DEU", "GBR")

    agency_raw = {
        "id": "sub_ag_1",
        "product_id": "prod_agency",
        "customer_id": "cust_2",
        "status": "active",
        "custom_field_data": {
            "cpv_and_keywords": "72:software; 48:platform; 7221:security; 7231:data; 7251:cloud; 30:hardware",
            "target_countries": "GBR",
            "delivery_webhook_url": "https://hooks.example.org/services/agency",
        },
    }
    agency_cfg = parse_polar_subscription(
        agency_raw,
        pro_product_id="prod_pro",
        agency_product_id="prod_agency",
    )
    assert agency_cfg.tier == "agency"
    assert len(agency_cfg.profiles) == 5

    with pytest.raises(ValueError, match="unrecognized product_id"):
        parse_polar_subscription(
            {**pro_raw, "product_id": "unknown"},
            pro_product_id="prod_pro",
            agency_product_id="prod_agency",
        )

    for inactive_status in ("canceled", "past_due", "incomplete"):
        with pytest.raises(ValueError, match="not active"):
            parse_polar_subscription(
                {**pro_raw, "status": inactive_status},
                pro_product_id="prod_pro",
                agency_product_id="prod_agency",
            )


def test_parse_polar_subscription_live_polar_uuids_and_aliases() -> None:
    assert DEFAULT_PRO_PRODUCT_ID == "0bf896dd-e186-4875-bd78-8628bd469422"
    assert DEFAULT_AGENCY_PRODUCT_ID == "d2532f72-e5f9-4613-a51d-be052b9d77fe"

    pro_live = parse_polar_subscription(
        {
            "id": "sub_live_pro_uuid",
            "product_id": "0bf896dd-e186-4875-bd78-8628bd469422",
            "customer_id": "cust_live_pro",
            "status": "active",
            "custom_field_data": {
                "cpv_and_keywords": "72:software; 48:cloud",
                "target_countries": "DEU",
                "delivery_webhook_url": "https://hooks.example.org/services/live-pro",
            },
        }
    )
    assert pro_live.tier == "pro"
    assert len(pro_live.profiles) == 1

    agency_live = parse_polar_subscription(
        {
            "id": "sub_live_agency_uuid",
            "product_id": "d2532f72-e5f9-4613-a51d-be052b9d77fe",
            "customer_id": "cust_live_agency",
            "status": "active",
            "custom_field_data": {
                "cpv_and_keywords": "72:software; 48:platform; 7221:security; 7231:data; 7251:cloud; 30:hardware",
                "target_countries": "GBR",
                "delivery_webhook_url": "https://hooks.example.org/services/live-agency",
            },
        }
    )
    assert agency_live.tier == "agency"
    assert len(agency_live.profiles) == 5

    pro_alias = parse_polar_subscription(
        {
            "id": "sub_alias_pro",
            "product_id": "tenderradar-pro",
            "customer_id": "cust_alias_pro",
            "status": "active",
            "custom_field_data": {
                "cpv_and_keywords": "72:software; 48:cloud",
                "target_countries": "DEU",
                "delivery_webhook_url": "https://hooks.example.org/services/alias-pro",
            },
        }
    )
    assert pro_alias.tier == "pro"
    assert len(pro_alias.profiles) == 1

    agency_alias = parse_polar_subscription(
        {
            "id": "sub_alias_agency",
            "product_id": "tenderradar-agency",
            "customer_id": "cust_alias_agency",
            "status": "active",
            "custom_field_data": {
                "cpv_and_keywords": "72:software; 48:platform; 7221:security; 7231:data; 7251:cloud; 30:hardware",
                "target_countries": "GBR",
                "delivery_webhook_url": "https://hooks.example.org/services/alias-agency",
            },
        }
    )
    assert agency_alias.tier == "agency"
    assert len(agency_alias.profiles) == 5


def test_validate_webhook_https_ssrf_blocks_private_and_allows_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tender_radar.polar_guard as pg

    with pytest.raises(ValueError, match="https://"):
        validate_webhook_https_ssrf("http://hooks.slack.com/services/a/b/c")

    for bad_url in (
        "https://127.0.0.1/hook",
        "https://10.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
    ):
        with pytest.raises(ValueError, match="SSRF"):
            validate_webhook_https_ssrf(bad_url)

    assert (
        validate_webhook_https_ssrf("http://127.0.0.1:8080/hook", allow_loopback_for_tests=True)
        == "http://127.0.0.1:8080/hook"
    )

    monkeypatch.setattr(
        pg.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    assert validate_webhook_https_ssrf("https://hooks.slack.com/services/T00/B00/XX") == (
        "https://hooks.slack.com/services/T00/B00/XX"
    )


def test_fetch_polar_subscriptions_fail_closed_and_cycle_delivery(tmp_path: Path) -> None:
    received_webhooks: list[dict[str, Any]] = []
    auth_headers: list[str] = []

    class _MockPolarAndWebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            auth_headers.append(str(self.headers.get("Authorization") or ""))
            if "fail_auth" in auth_headers[-1]:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"detail":"Unauthorized"}')
                return
            if self.path.startswith("/v1/subscriptions/"):
                payload = {
                    "items": [
                        {
                            "id": "sub_live_1",
                            "product_id": "prod_pro",
                            "customer_id": "cust_live_1",
                            "status": "active",
                            "custom_field_data": {
                                "cpv_and_keywords": "72:software",
                                "target_countries": "DEU",
                                "delivery_webhook_url": f"http://127.0.0.1:{server_port}/webhook",
                            },
                        }
                    ],
                    "pagination": {"max_page": 1},
                }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/webhook":
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                received_webhooks.append(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            self.send_response(500)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), _MockPolarAndWebhookHandler)
    server_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        api_base = f"http://127.0.0.1:{server_port}/v1"

        # Fail-closed on HTTP 401
        with pytest.raises(RuntimeError, match="HTTP 401"):
            fetch_active_polar_subscriptions("fail_auth_token", api_base_url=api_base)

        # Fail-closed on direct webhook HTTP 500
        with pytest.raises(RuntimeError, match="HTTP 500"):
            deliver_webhook_payload(
                f"http://127.0.0.1:{server_port}/bad",
                {"text": "hi", "notices": []},
                allow_loopback_for_tests=True,
            )

        notices = [
            _notice("100-2026", "Cloud analytics software platform", cpv=("72200000",), country="DEU"),
            _notice("666-2026", "Municipal banking loan software", cpv=("66110000",), country="DEU"),
        ]
        db_path = tmp_path / "subscriber_state.db"
        now = datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc)

        # Dry run dispatches 0 HTTP POSTs
        dry_report = run_subscriber_digest_cycle(
            "polar_org_secret_token",
            db_path=db_path,
            now_utc=now,
            dry_run=True,
            fixture_notices=notices,
            pro_product_id="prod_pro",
            agency_product_id="prod_agency",
            api_base_url=api_base,
            allow_loopback_for_tests=True,
        )
        assert isinstance(dry_report, SubscriberCycleReport)
        assert dry_report.deliveries_attempted == 0
        assert dry_report.results[0].status == "DRY_RUN"
        assert len(received_webhooks) == 0

        # Live delivery strips sharia-vetoed notice and sends compatible text + notices payload
        live_report = run_subscriber_digest_cycle(
            "polar_org_secret_token",
            db_path=db_path,
            now_utc=now + timedelta(minutes=5),
            dry_run=False,
            fixture_notices=notices,
            pro_product_id="prod_pro",
            agency_product_id="prod_agency",
            api_base_url=api_base,
            allow_loopback_for_tests=True,
        )
        assert live_report.deliveries_succeeded == 1
        assert live_report.results[0].status == "DELIVERED"
        assert live_report.results[0].new_notices_count == 1
        assert len(received_webhooks) == 1
        posted = received_webhooks[0]
        assert "text" in posted
        assert "notices" in posted
        assert len(posted["notices"]) == 1
        assert posted["notices"][0]["tender"]["notice_id"] == "100-2026"
        assert "666-2026" not in json.dumps(posted)

        # Re-running with same notices skips duplicate delivery cleanly
        repeat_report = run_subscriber_digest_cycle(
            "polar_org_secret_token",
            db_path=db_path,
            now_utc=now + timedelta(hours=24),
            dry_run=False,
            fixture_notices=notices,
            pro_product_id="prod_pro",
            agency_product_id="prod_agency",
            api_base_url=api_base,
            allow_loopback_for_tests=True,
        )
        assert repeat_report.results[0].status == "SKIPPED_NO_NEW"
        assert len(received_webhooks) == 1
        assert last_subscriber_cycle_utc(db_path) is not None

        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM subscriber_delivery_runs;").fetchone()[0]
        assert count == 3
    finally:
        server.shutdown()
        server.server_close()


def test_deliver_webhook_payload_blocks_http_307_ssrf_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted_hosts: list[tuple[str, int]] = []
    orig_create_connection = socket.create_connection

    def _spy_create_connection(address: tuple[str, int], *args: Any, **kwargs: Any) -> Any:
        attempted_hosts.append((str(address[0]), int(address[1])))
        if str(address[0]) == "169.254.169.254":
            raise AssertionError("Must NEVER attempt connection to metadata IP 169.254.169.254")
        return orig_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", _spy_create_connection)

    class _RedirectWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(307)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), _RedirectWebhookHandler)
    server_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with pytest.raises(RuntimeError, match=r"SSRF redirect blocked: HTTP 307"):
            deliver_webhook_payload(
                f"http://127.0.0.1:{server_port}/redirect-hook",
                {"text": "hi", "notices": []},
                allow_loopback_for_tests=True,
            )
        assert ("127.0.0.1", server_port) in attempted_hosts
        assert all(host != "169.254.169.254" for host, _ in attempted_hosts)
    finally:
        server.shutdown()
        server.server_close()
