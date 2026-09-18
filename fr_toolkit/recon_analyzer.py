"""Reconciliation audit analyzer for PingIDM / ForgeRock IDM.

Pulls records from ``/openidm/audit/recon`` (optionally bounded by a time
window and/or mapping name) and produces:

- a JSON summary: totals, breakdown by mapping, by status, by reconciliation
  situation, and the top failure reasons
- a CSV of the individual failure records for triage

Recon situations seen in the wild include: ABSENT, ALL_GONE, AMBIGUOUS,
CONFIRMED, FOUND, FOUND_ALREADY_LINKED, LINKED, MISSING, NEW,
SOURCE_MISSING, TARGET_GONE, UNASSIGNED, UNQUALIFIED.

Usage:
    python -m fr_toolkit.recon_analyzer --config config.json \\
        --since 2026-09-01T00:00:00Z --out-json recon-summary.json \\
        --out-csv recon-failures.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import ToolkitConfig
from .idm_client import IdmClient

log = logging.getLogger(__name__)

# Human-friendly notes for the most common recon situations.
SITUATION_NOTES = {
    "FOUND_ALREADY_LINKED": "Correlation attribute matched an object that is already "
    "linked to a different source object (duplicate identity data).",
    "AMBIGUOUS": "Correlation query returned more than one candidate.",
    "ABSENT": "Target object exists but the source object is gone (or vice versa).",
    "MISSING": "Expected object not found on one side of the mapping.",
    "SOURCE_MISSING": "Source object referenced by a link no longer exists.",
    "TARGET_GONE": "Target object referenced by a link no longer exists.",
    "UNQUALIFIED": "Object did not meet the mapping's validSource/validTarget query.",
    "NEW": "Source object has no link yet; will be created on target.",
    "FOUND": "Match found; normal sync path.",
    "CONFIRMED": "Link confirmed; no action needed.",
}

FAILURE_CSV_FIELDS = [
    "timestamp",
    "reconId",
    "mapping",
    "situation",
    "sourceObjectId",
    "targetObjectId",
    "action",
    "messageDetail",
]


def _build_filter(
    since: Optional[str], until: Optional[str], mapping: Optional[str]
) -> str:
    clauses: List[str] = []
    if since:
        clauses.append(f'timestamp gt "{since}"')
    if until:
        clauses.append(f'timestamp lt "{until}"')
    if mapping:
        clauses.append(f'mapping eq "{mapping}"')
    return " and ".join(clauses) if clauses else "true"


def analyze(
    client: IdmClient,
    since: Optional[str] = None,
    until: Optional[str] = None,
    mapping: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch recon audit records and return a summary dict."""
    query_filter = _build_filter(since, until, mapping)
    log.info("Querying audit/recon with filter: %s", query_filter)

    records = list(
        client.query(
            "audit/recon",
            query_filter=query_filter,
            fields=[
                "_id",
                "timestamp",
                "reconId",
                "mapping",
                "situation",
                "sourceObjectId",
                "targetObjectId",
                "action",
                "status",
                "messageDetail",
            ],
            sort_keys="timestamp",
        )
    )

    by_mapping: Counter = Counter()
    by_status: Counter = Counter()
    by_situation: Counter = Counter()
    failure_reasons: Counter = Counter()
    failures: List[Dict[str, Any]] = []

    for rec in records:
        by_mapping[rec.get("mapping", "unknown")] += 1
        status = (rec.get("status") or "UNKNOWN").upper()
        by_status[status] += 1
        situation = rec.get("situation") or "UNKNOWN"
        by_situation[situation] += 1
        if status != "SUCCESS":
            failures.append(rec)
            reason = rec.get("messageDetail") or situation
            failure_reasons[str(reason)[:200]] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filter": query_filter,
        "total_records": len(records),
        "by_mapping": dict(by_mapping.most_common()),
        "by_status": dict(by_status),
        "by_situation": dict(by_situation.most_common()),
        "failure_count": len(failures),
        "top_failure_reasons": [
            {"reason": r, "count": c} for r, c in failure_reasons.most_common(10)
        ],
        "situation_notes": {
            s: SITUATION_NOTES.get(s, "") for s in by_situation
        },
        "failures": failures,  # full records kept for the CSV writer
    }


def write_outputs(
    summary: Dict[str, Any], json_path: Optional[str], csv_path: Optional[str]
) -> None:
    serialisable = {k: v for k, v in summary.items() if k != "failures"}
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(serialisable, fh, indent=2, default=str)
        log.info("Wrote JSON summary to %s", json_path)
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FAILURE_CSV_FIELDS)
            writer.writeheader()
            for rec in summary["failures"]:
                writer.writerow({f: rec.get(f, "") for f in FAILURE_CSV_FIELDS})
        log.info("Wrote %d failures to %s", len(summary["failures"]), csv_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to JSON config file")
    parser.add_argument("--since", help='ISO timestamp lower bound, e.g. "2026-09-01T00:00:00Z"')
    parser.add_argument("--until", help='ISO timestamp upper bound, e.g. "2026-09-10T00:00:00Z"')
    parser.add_argument("--mapping", help="Only analyze one mapping, e.g. systemLdapAccounts_managedUser")
    parser.add_argument("--out-json", default="recon-summary.json")
    parser.add_argument("--out-csv", default="recon-failures.csv")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = IdmClient(ToolkitConfig.load(args.config))
    summary = analyze(client, args.since, args.until, args.mapping)
    write_outputs(summary, args.out_json, args.out_csv)
    print(
        f"Analyzed {summary['total_records']} recon records: "
        f"{summary['failure_count']} failures across "
        f"{len(summary['by_mapping'])} mapping(s)."
    )


if __name__ == "__main__":
    main()
