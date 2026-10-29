#!/usr/bin/env python3
"""
Compute AI acceptance rate from the corrections dataset.

Acceptance rate = proportion of AI proposals that the doctor confirmed without changes.
This is measured from the corrections table, which logs every AI-proposal vs doctor-confirm
decision with proposal_source and was_changed fields.

Usage:
    ./venv/bin/python scripts/acceptance_rate.py [--clinic default_clinic]

Outputs acceptance rate per proposal_source (local_extractor, gemini, manual).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import get_connection

DEFAULT_CLINIC_ID = "default_clinic"


def acceptance_rate(db_path: str, clinic_id: str = DEFAULT_CLINIC_ID) -> dict:
    """Compute acceptance rate per proposal_source from corrections data."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT proposal_source, was_changed, COUNT(*) as n
        FROM corrections
        WHERE clinic_id = ?
        GROUP BY proposal_source, was_changed
        ORDER BY proposal_source, was_changed
        """,
        (clinic_id,),
    ).fetchall()
    conn.close()

    result = {}
    for source in ("local_extractor", "gemini", "manual"):
        total = 0
        changed = 0
        for r in rows:
            if r["proposal_source"] == source:
                total += r["n"]
                if r["was_changed"]:
                    changed += r["n"]
        accepted = total - changed
        rate = accepted / total if total > 0 else None
        result[source] = {
            "total": total,
            "accepted": accepted,
            "changed": changed,
            "acceptance_rate": rate,
        }

    return result


def main():
    import json
    parser = argparse.ArgumentParser(description="Compute AI acceptance rate from corrections data")
    parser.add_argument("--clinic", default=DEFAULT_CLINIC_ID, help="Clinic ID")
    parser.add_argument("--db", default="db/ayurcore.db", help="Database path")
    parser.add_argument("--out", default=None, help="Output path for JSON report")
    args = parser.parse_args()

    result = acceptance_rate(args.db, args.clinic)

    print(f"Acceptance rate for clinic {args.clinic}:")
    print(f"{'Source':<20} {'Total':>8} {'Accepted':>10} {'Changed':>10} {'Rate':>8}")
    print("-" * 60)
    for source in ("local_extractor", "gemini", "manual"):
        r = result[source]
        rate_str = f"{r['acceptance_rate']:.1%}" if r["acceptance_rate"] is not None else "N/A"
        print(f"{source:<20} {r['total']:>8} {r['accepted']:>10} {r['changed']:>10} {rate_str:>8}")

    overall_total = sum(r["total"] for r in result.values())
    overall_accepted = sum(r["accepted"] for r in result.values())
    overall_rate = overall_accepted / overall_total if overall_total > 0 else None
    overall_rate_str = f"{overall_rate:.1%}" if overall_rate is not None else "N/A"
    print("-" * 60)
    print(f"{'Overall':<20} {overall_total:>8} {overall_accepted:>10} {overall_total - overall_accepted:>10} "
          f"{overall_rate_str:>8}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report_data = {
            "clinic_id": args.clinic,
            "overall": {
                "total": overall_total,
                "accepted": overall_accepted,
                "changed": overall_total - overall_accepted,
                "acceptance_rate": overall_rate,
            },
            "by_source": result,
        }
        out_path.write_text(json.dumps(report_data, indent=2) + "\n")
        print(f"\nWrote acceptance report to {args.out}")


if __name__ == "__main__":
    main()
