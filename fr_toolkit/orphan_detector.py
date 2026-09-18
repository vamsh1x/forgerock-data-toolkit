"""Orphaned-link detector for PingIDM / ForgeRock IDM.

A link in ``repo/link`` is orphaned when the object on either side of it no
longer exists:

- **source orphan** -- ``firstId`` is gone from the source resource
  (typical recon situation: SOURCE_MISSING)
- **target orphan** -- ``secondId`` is gone from the target resource
  (typical recon situation: TARGET_GONE / ABSENT)

Orphans usually mean deletes on one system were never propagated (unlink or
delete-on-target not configured, connector outage during a delete wave, or a
manual cleanup that bypassed IDM).

The detector checks each *unique* id only once per side, so a 100k-link
mapping costs ~2 existence checks per distinct object, not per link.

Usage:
    python -m fr_toolkit.orphan_detector --config config.json \\
        --mapping systemLdapAccounts_managedUser \\
        --source-resource managed/user --target-resource system/ldap/account \\
        --out-csv orphans.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
from typing import Any, Dict, List, Set

from .config import ToolkitConfig
from .idm_client import IdmClient

log = logging.getLogger(__name__)

CSV_FIELDS = [
    "link_id",
    "mapping",
    "orphan_side",   # "source" or "target"
    "missing_resource",
    "missing_id",
    "link_state",
]


def find_orphans(
    client: IdmClient,
    mapping: str,
    source_resource: str,
    target_resource: str,
) -> List[Dict[str, Any]]:
    """Return one row per orphaned link in the mapping."""
    log.info("Loading links for mapping %s ...", mapping)
    links = list(
        client.query(
            "repo/link",
            query_filter=f'linkType eq "{mapping}"',
            fields=["_id", "firstId", "secondId", "linkType", "linkState"],
        )
    )
    log.info("Checking %d link(s) ...", len(links))

    first_ids: Set[str] = {str(l.get("firstId", "")) for l in links if l.get("firstId")}
    second_ids: Set[str] = {str(l.get("secondId", "")) for l in links if l.get("secondId")}

    log.info("Verifying %d source id(s) in %s ...", len(first_ids), source_resource)
    missing_source = {i for i in first_ids if not client.exists(source_resource, i)}
    log.info("Verifying %d target id(s) in %s ...", len(second_ids), target_resource)
    missing_target = {i for i in second_ids if not client.exists(target_resource, i)}

    orphans: List[Dict[str, Any]] = []
    for link in links:
        first_id = str(link.get("firstId", ""))
        second_id = str(link.get("secondId", ""))
        if first_id in missing_source:
            orphans.append(
                {
                    "link_id": link.get("_id", ""),
                    "mapping": mapping,
                    "orphan_side": "source",
                    "missing_resource": source_resource,
                    "missing_id": first_id,
                    "link_state": link.get("linkState", ""),
                }
            )
        if second_id in missing_target:
            orphans.append(
                {
                    "link_id": link.get("_id", ""),
                    "mapping": mapping,
                    "orphan_side": "target",
                    "missing_resource": target_resource,
                    "missing_id": second_id,
                    "link_state": link.get("linkState", ""),
                }
            )
    return orphans


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d orphaned link(s) to %s", len(rows), path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to JSON config file")
    parser.add_argument("--mapping", required=True,
                        help="Mapping name, e.g. systemLdapAccounts_managedUser")
    parser.add_argument("--source-resource", default="managed/user")
    parser.add_argument("--target-resource", default="system/ldap/account")
    parser.add_argument("--out-csv", default="orphans.csv")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = IdmClient(ToolkitConfig.load(args.config))
    rows = find_orphans(client, args.mapping, args.source_resource, args.target_resource)
    write_csv(rows, args.out_csv)
    print(f"Found {len(rows)} orphaned link(s). Report: {args.out_csv}")


if __name__ == "__main__":
    main()
