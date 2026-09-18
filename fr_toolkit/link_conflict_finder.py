"""Link-conflict finder for PingIDM / ForgeRock IDM.

Detects the data conditions that produce FOUND_ALREADY_LINKED (and
AMBIGUOUS) reconciliation failures, *before* the next recon run:

1. **Correlation collisions** -- two or more managed objects sharing the
   same value for a correlation attribute (e.g. two users with the same
   ``mail``). When recon correlates a source account against these, IDM
   cannot pick a unique target and the link fails.
2. **Duplicate links** -- more than one ``repo/link`` entry for the same
   mapping pointing at the same source object (``firstId``).

Writes a CSV report listing every collision with the object IDs involved,
which is the starting point for the cleanup action plan.

Usage:
    python -m fr_toolkit.link_conflict_finder --config config.json \\
        --managed-resource managed/user --mapping systemLdapAccounts_managedUser \\
        --out-csv link-conflicts.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
from collections import defaultdict
from typing import Any, Dict, List

from .config import ToolkitConfig
from .idm_client import IdmClient

log = logging.getLogger(__name__)

CSV_FIELDS = [
    "check_type",
    "mapping_or_resource",
    "attribute",
    "value",
    "count",
    "object_ids",
]


def find_correlation_collisions(
    client: IdmClient,
    managed_resource: str,
    attributes: List[str],
) -> List[Dict[str, Any]]:
    """Find managed objects sharing a correlation attribute value."""
    fields = ["_id"] + attributes
    log.info("Scanning %s for duplicate %s ...", managed_resource, attributes)
    seen: Dict[str, Dict[str, List[str]]] = {a: defaultdict(list) for a in attributes}

    for obj in client.query(managed_resource, fields=fields):
        obj_id = obj.get("_id", "")
        for attr in attributes:
            value = obj.get(attr)
            if value in (None, ""):
                continue
            # Correlation is usually on scalar values; stringify defensively.
            seen[attr][str(value)].append(obj_id)

    collisions: List[Dict[str, Any]] = []
    for attr, values in seen.items():
        for value, ids in values.items():
            if len(ids) > 1:
                collisions.append(
                    {
                        "check_type": "correlation_collision",
                        "mapping_or_resource": managed_resource,
                        "attribute": attr,
                        "value": value,
                        "count": len(ids),
                        "object_ids": ";".join(sorted(ids)),
                    }
                )
    return collisions


def find_duplicate_links(
    client: IdmClient, mapping: str
) -> List[Dict[str, Any]]:
    """Find multiple repo links for the same source object in one mapping.

    In ``repo/link``, ``linkType`` is the mapping name, ``firstId`` the
    source object id and ``secondId`` the target object id.
    """
    log.info("Scanning repo/link for duplicate links in mapping %s ...", mapping)
    by_first: Dict[str, List[str]] = defaultdict(list)
    for link in client.query(
        "repo/link",
        query_filter=f'linkType eq "{mapping}"',
        fields=["_id", "firstId", "secondId", "linkType", "linkState"],
    ):
        by_first[str(link.get("firstId", ""))].append(str(link.get("_id", "")))

    return [
        {
            "check_type": "duplicate_link",
            "mapping_or_resource": mapping,
            "attribute": "firstId",
            "value": first_id,
            "count": len(link_ids),
            "object_ids": ";".join(sorted(link_ids)),
        }
        for first_id, link_ids in by_first.items()
        if len(link_ids) > 1
    ]


def find_conflicts(
    client: IdmClient,
    managed_resource: str,
    mapping: str,
    attributes: List[str],
) -> List[Dict[str, Any]]:
    collisions = find_correlation_collisions(client, managed_resource, attributes)
    log.info("Found %d correlation collision(s)", len(collisions))
    duplicates = find_duplicate_links(client, mapping)
    log.info("Found %d duplicate link group(s)", len(duplicates))
    return collisions + duplicates


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d conflict(s) to %s", len(rows), path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to JSON config file")
    parser.add_argument("--managed-resource", default="managed/user",
                        help="Managed object set to scan (default managed/user)")
    parser.add_argument("--mapping", required=True,
                        help="Mapping name, e.g. systemLdapAccounts_managedUser")
    parser.add_argument("--attributes", nargs="*", default=None,
                        help="Correlation attributes to check (default: from config)")
    parser.add_argument("--out-csv", default="link-conflicts.csv")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = ToolkitConfig.load(args.config)
    client = IdmClient(config)
    rows = find_conflicts(
        client, args.managed_resource, args.mapping,
        args.attributes or config.correlation_attributes,
    )
    write_csv(rows, args.out_csv)
    print(f"Found {len(rows)} conflict(s). Report: {args.out_csv}")


if __name__ == "__main__":
    main()
