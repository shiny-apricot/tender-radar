"""Fail-closed Polar.sh checkout URL verifier with DNS SSRF guard and redirect inspection."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import re
import socket
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .models import PolarCheckoutStatus

ALLOWED_POLAR_HOSTS: frozenset[str] = frozenset({"buy.polar.sh", "polar.sh", "www.polar.sh"})
LOCAL_TEST_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
POLAR_ROOT_HOSTS: frozenset[str] = frozenset({"polar.sh", "www.polar.sh"})
USER_AGENT = "TenderRadar-PolarGuard/0.1.0"
MAX_HTML_SNIFF_BYTES = 16 * 1024
CHECKOUT_CUES: tuple[str, ...] = ("checkout", "stripe", "subscribe", "polar_c_", "payment")
HOMEPAGE_MARKERS: tuple[str, ...] = (
    "a billing platform for the intelligence era",
    "polar — a billing platform",
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Stop urllib from silently following HTTP 3xx redirects so we inspect Location explicitly."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _is_polar_host(host: str) -> bool:
    return host in ALLOWED_POLAR_HOSTS or host.endswith(".polar.sh")


def _is_global_public_ip(ip_str: str) -> bool:
    cleaned = ip_str.split("%", 1)[0].strip()
    ip_obj = ipaddress.ip_address(cleaned)
    return (
        ip_obj.is_global
        and not ip_obj.is_private
        and not ip_obj.is_loopback
        and not ip_obj.is_link_local
        and not ip_obj.is_reserved
        and not ip_obj.is_multicast
        and not ip_obj.is_unspecified
    )


def _check_dns_public(host: str, port: int, *, require_loopback_only: bool = False) -> str | None:
    """Resolve host via DNS and enforce either public-IP-only or loopback-test-only policy."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"dns_resolution_failed:{exc}"

    if not infos:
        return "dns_resolution_failed:empty"

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return "dns_resolution_failed:empty_sockaddr"
        raw_ip = str(sockaddr[0]).split("%", 1)[0].strip()
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError:
            return f"dns_resolution_invalid_ip:{raw_ip}"

        if require_loopback_only:
            if not ip_obj.is_loopback:
                return f"blocked_non_loopback_test_ip:{raw_ip}"
        elif not _is_global_public_ip(raw_ip):
            return f"blocked_private_ip:{raw_ip}"
    return None


def _validate_hop_target(
    url: str,
    *,
    allow_loopback_for_tests: bool,
    is_redirect: bool,
) -> str | None:
    """Validate URL scheme, Polar domain allowlist, and resolved DNS IP before fetching."""
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    is_local_test = allow_loopback_for_tests and host in LOCAL_TEST_HOSTS

    if is_local_test:
        if scheme not in ("http", "https"):
            return "redirect_off_domain" if is_redirect else "non_https_scheme"
        port = parsed.port or (443 if scheme == "https" else 80)
        return _check_dns_public(host, port, require_loopback_only=True)

    if scheme != "https":
        return "redirect_off_domain" if is_redirect else "non_https_scheme"
    if not _is_polar_host(host):
        return "redirect_off_domain" if is_redirect else "non_polar_domain"
    port = parsed.port or 443
    return _check_dns_public(host, port, require_loopback_only=False)


def _is_root_redirect_target(
    resolved_url: str,
    *,
    allow_loopback_for_tests: bool,
) -> bool:
    parsed = urllib.parse.urlparse(resolved_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if host in POLAR_ROOT_HOSTS and path in ("", "/"):
        return True
    if allow_loopback_for_tests and host in LOCAL_TEST_HOSTS and path in ("", "/"):
        return True
    return False


def _inspect_http_200_body(body_bytes: bytes) -> str | None:
    """Reject Polar homepage, soft-404 pages, and HTTP 200 payloads lacking checkout cues."""
    body_text = body_bytes.decode("utf-8", errors="replace")
    body_lower = body_text.lower()

    if any(marker in body_lower for marker in HOMEPAGE_MARKERS):
        return "polar_homepage_content"

    title_match = re.search(r"<title[^>]*>(.*?)</title>", body_text, re.IGNORECASE | re.DOTALL)
    title_lower = re.sub(r"\s+", " ", title_match.group(1)).strip().lower() if title_match else ""

    no_scripts = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", body_text, flags=re.IGNORECASE | re.DOTALL)
    visible_lower = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", no_scripts)).strip().lower()

    if (
        "page not found" in visible_lower
        or "not found" in title_lower
        or re.search(r"\b404\b", visible_lower)
        or re.search(r"\b404\b", title_lower)
    ):
        return "soft_404_page"

    if not any(cue in body_lower for cue in CHECKOUT_CUES):
        return "missing_checkout_cues"

    return None


def verify_polar_checkout_url(
    url: str,
    timeout: float = 8.0,
    *,
    allow_loopback_for_tests: bool = False,
) -> PolarCheckoutStatus:
    """Verify that a Polar checkout URL resolves to a live public HTTP 200 checkout page."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Checkout URL must be a non-empty string.")
    if timeout <= 0:
        raise ValueError("timeout must be a positive number of seconds.")

    cleaned_url = url.strip()
    parsed = urllib.parse.urlparse(cleaned_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid checkout URL '{cleaned_url}': missing scheme or host.")

    checked_at_utc = datetime.now(timezone.utc).isoformat()
    initial_err = _validate_hop_target(
        cleaned_url,
        allow_loopback_for_tests=allow_loopback_for_tests,
        is_redirect=False,
    )
    if initial_err is not None:
        return PolarCheckoutStatus(
            url=cleaned_url,
            is_live=False,
            status_code=None,
            final_url=cleaned_url,
            redirect_location=None,
            reason=initial_err,
            checked_at_utc=checked_at_utc,
        )

    opener = urllib.request.build_opener(_NoRedirectHandler())
    current_url = cleaned_url
    first_redirect: str | None = None
    max_hops = 3

    for _ in range(max_hops + 1):
        req = urllib.request.Request(
            current_url,
            method="GET",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                status_code = int(getattr(resp, "status", 200))
                final_parsed = urllib.parse.urlparse(current_url)
                final_host = (final_parsed.hostname or "").lower()
                final_path = final_parsed.path or "/"
                if final_host in POLAR_ROOT_HOSTS and final_path in ("", "/"):
                    return PolarCheckoutStatus(
                        url=cleaned_url,
                        is_live=False,
                        status_code=status_code,
                        final_url=current_url,
                        redirect_location=first_redirect,
                        reason="root_landing_page",
                        checked_at_utc=checked_at_utc,
                    )
                if status_code == 200:
                    body_bytes = resp.read(MAX_HTML_SNIFF_BYTES)
                    body_err = _inspect_http_200_body(body_bytes)
                    if body_err is not None:
                        return PolarCheckoutStatus(
                            url=cleaned_url,
                            is_live=False,
                            status_code=status_code,
                            final_url=current_url,
                            redirect_location=first_redirect,
                            reason=body_err,
                            checked_at_utc=checked_at_utc,
                        )
                    return PolarCheckoutStatus(
                        url=cleaned_url,
                        is_live=True,
                        status_code=status_code,
                        final_url=current_url,
                        redirect_location=first_redirect,
                        reason="checkout_live",
                        checked_at_utc=checked_at_utc,
                    )
                return PolarCheckoutStatus(
                    url=cleaned_url,
                    is_live=False,
                    status_code=status_code,
                    final_url=current_url,
                    redirect_location=first_redirect,
                    reason=f"unexpected_status:{status_code}",
                    checked_at_utc=checked_at_utc,
                )
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raw_location = str(exc.headers.get("Location") or "").strip() if exc.headers else ""
                if first_redirect is None and raw_location:
                    first_redirect = raw_location
                if not raw_location:
                    return PolarCheckoutStatus(
                        url=cleaned_url,
                        is_live=False,
                        status_code=exc.code,
                        final_url=current_url,
                        redirect_location=None,
                        reason=f"redirect_missing_location:{exc.code}",
                        checked_at_utc=checked_at_utc,
                    )
                resolved = urllib.parse.urljoin(current_url, raw_location)
                if _is_root_redirect_target(resolved, allow_loopback_for_tests=allow_loopback_for_tests):
                    return PolarCheckoutStatus(
                        url=cleaned_url,
                        is_live=False,
                        status_code=exc.code,
                        final_url=resolved,
                        redirect_location=raw_location,
                        reason="redirect_to_root",
                        checked_at_utc=checked_at_utc,
                    )
                hop_err = _validate_hop_target(
                    resolved,
                    allow_loopback_for_tests=allow_loopback_for_tests,
                    is_redirect=True,
                )
                if hop_err is not None:
                    return PolarCheckoutStatus(
                        url=cleaned_url,
                        is_live=False,
                        status_code=exc.code,
                        final_url=resolved,
                        redirect_location=raw_location,
                        reason=hop_err,
                        checked_at_utc=checked_at_utc,
                    )
                current_url = resolved
                continue

            return PolarCheckoutStatus(
                url=cleaned_url,
                is_live=False,
                status_code=int(exc.code),
                final_url=current_url,
                redirect_location=first_redirect,
                reason=f"http_error:{exc.code}",
                checked_at_utc=checked_at_utc,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return PolarCheckoutStatus(
                url=cleaned_url,
                is_live=False,
                status_code=None,
                final_url=current_url,
                redirect_location=first_redirect,
                reason=f"network_error:{exc}",
                checked_at_utc=checked_at_utc,
            )

    return PolarCheckoutStatus(
        url=cleaned_url,
        is_live=False,
        status_code=None,
        final_url=current_url,
        redirect_location=first_redirect,
        reason="too_many_redirects",
        checked_at_utc=checked_at_utc,
    )
