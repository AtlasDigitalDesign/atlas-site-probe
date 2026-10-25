"""Command line: python -m atlas_site_probe sites.txt --csv out.csv [--hubspot]"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

from .probe import probe
from . import hubspot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas_site_probe", description="Read-only checks on public web pages.")
    ap.add_argument("sites", help="text file with one URL or domain per line, or a single URL")
    ap.add_argument("--csv", help="write one row per site to this CSV (import it into a Clay table)")
    ap.add_argument("--json", help="write full results to this JSON file")
    ap.add_argument("--hubspot", action="store_true", help="push sites with findings to HubSpot (needs HUBSPOT_TOKEN)")
    ap.add_argument("--min-findings", type=int, default=2, help="findings a site needs before it goes to HubSpot")
    ap.add_argument("--dry-run", action="store_true", help="show what would go to HubSpot without sending")
    ap.add_argument("--no-cart", action="store_true", help="skip the /cart check on Shopify stores")
    a = ap.parse_args(argv)

    if os.path.isfile(a.sites):
        with open(a.sites) as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    else:
        urls = [a.sites]

    results = []
    for u in urls:
        r = probe(u, check_cart=not a.no_cart)
        results.append(r)
        print(f"{r.status or 'ERR'}  {len(r.findings)} findings  {r.url}", file=sys.stderr)

    rows = [r.to_row() for r in results]
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    if a.json:
        with open(a.json, "w") as f:
            json.dump([r.to_dict() for r in results], f, indent=2)
    if not a.csv and not a.json:
        json.dump([r.to_dict() for r in results], sys.stdout, indent=2)
        print()

    if a.hubspot or a.dry_run:
        try:
            log = hubspot.push_rows(rows, min_findings=a.min_findings, dry_run=a.dry_run)
        except hubspot.HubSpotError as e:
            print(f"hubspot: {e}", file=sys.stderr)
            return 1
        for entry in log:
            print(f"hubspot: {entry['url']}: {entry['action']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
