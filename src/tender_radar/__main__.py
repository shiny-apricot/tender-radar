"""Module entrypoint for `python3 -m tender_radar`."""

from __future__ import annotations

from pathlib import Path
import sys

_REPO_PRODUCTS_PROCUREMENT = (
    Path(__file__).resolve().parents[3]
    / "eu_uk_procurement_tenders_mcp_actor"
    / "src"
)
if _REPO_PRODUCTS_PROCUREMENT.is_dir() and str(_REPO_PRODUCTS_PROCUREMENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PRODUCTS_PROCUREMENT))

from .digest import main

if __name__ == "__main__":
    raise SystemExit(main())
