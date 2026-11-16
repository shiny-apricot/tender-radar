# TenderRadar

Stateless EU TED v3 and UK Contracts Finder OCDS bid-fit digest engine.
Built on Python 3.10+ stdlib with SQLite seen-state deduplication and SLA gap tracking.

## Why TenderRadar vs. Raw Portal Alerts

- **Deterministic Bid-Fit Scoring (0-100)**: Scores CPV prefix matches, keyword hits, target countries, budget thresholds, and deadline runway.
- **Explicit Disqualifiers**: Surfaces exact blockers (`excluded_keyword`, `country_mismatch`, `deadline_too_soon`, `budget_below_min`, `sharia_veto`).
- **Cross-Source Deduplication**: Deduplicates EU TED v3 and UK OCDS notices by `source:notice_id` and normalized title/buyer fingerprint.
- **SQLite Seen-State & SLA Gap Guard**: Sends only unseen notices per profile and detects `>26h` gaps via `check_cron_sla()`.

## Quick Start

```bash
PYTHONPATH=src \
  python3 -m tender_radar \
  --keyword "software" \
  --cpv "48,72" \
  --country "GBR,DEU" \
  --format markdown
```

Run deterministic offline verification against a JSON fixture:

```bash
PYTHONPATH=src \
  python3 -m tender_radar \
  --fixture-json notices.json \
  --format html
```

## Ethical CPV & Sector Exclusions

- **Blocked CPV Prefixes**: `159` (beverages and alcoholic drinks), `353` (weapons and ammunition), `354` (military vehicles), `357` (military electronic systems), and `6611` (banking services / interest finance).
- **Blocked Sector Terms**: Weapons, defense munitions, alcohol, winery, brewery, tobacco, pork, gambling, casino, adult content, banking, bank loan, credit facility, and interest-bearing debt instruments.

## Early Access & Checkout Status

- **Repository**: [github.com/shiny-apricot/tender-radar](https://github.com/shiny-apricot/tender-radar)
- **Status**: Early-access cohorts & free 7-day sample digests onboarding via kenzailab@gmail.com — self-serve Polar checkout re-opens once cloud scheduler billing clears.
- **7-Day Sample Digest**: Email `kenzailab@gmail.com` with subject `TenderRadar 7-Day Sample` and your target CPV codes and countries.

## Legal Attribution

- EU tender notices are reused in accordance with European Commission **Decision 2011/833/EU**.
- Contains public sector information licensed under the Open Government Licence v3.0.
