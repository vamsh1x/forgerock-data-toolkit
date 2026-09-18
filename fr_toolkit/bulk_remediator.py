"""Bulk remediator for PingIDM / ForgeRock IDM.

Applies fixes from a CSV action plan. Safety is the whole design:

- **Dry-run is the default.** Nothing is written unless ``--apply`` is given.
- **Every write is journaled** to a JSONL rollback journal recording the
  before/after values of each change. ``--rollback <journal>`` replays the
  journal in reverse to undo an applied plan.
- **Canary support** via ``--limit N`` (apply to the first N rows only) and
  ``--interactive`` (confirm each record).
- **Rate limited** through the shared client throttle.

Action plan CSV columns::

    action,resource,object_id,field,old_value,new_value,reason

- ``action``: ``patch`` (replace ``field`` with ``new_value``),
  ``delete`` (delete the object; ``field``/values ignored),
  ``create`` (create ``resource`` from the JSON object in ``new_value``).
- ``old_value`` is informational (expected current value; a mismatch is
  logged as a warning but does not stop the run -- pass ``--strict`` to
  abort on mismatch).
- ``new_value`` is parsed as JSON when possible, otherwise kept as a string.

Rollback journal entry::

    {"ts": ..., "seq": 0, "action": "patch", "resource": "managed/user",
     "object_id": "jdoe", "field": "mail",
     "before": "old@example.com", "after": "new@example.com",
     "status": "applied", "error": ""}

Usage:
    # preview (default)
    python -m fr_toolkit.bulk_remediator --config config.json --plan plan.csv
    # canary: first 5 rows, for real
    python -m fr_toolkit.bulk_remediator --config config.json --plan plan.csv \\
        --apply --limit 5 --journal run1.journal.jsonl
    # undo it
    python -m fr_toolkit.bulk_remediator --config config.json \\
        --rollback run1.journal.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .config import ToolkitConfig
from .idm_client import IdmClient, IdmError

log = logging.getLogger(__name__)

PLAN_FIELDS = ["action", "resource", "object_id", "field", "old_value", "new_value", "reason"]
VALID_ACTIONS = {"patch", "delete", "create"}


def _coerce(value: str) -> Any:
    """Parse new_value as JSON when possible, else keep the raw string."""
    if value is None or value == "":
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def read_plan(path: str) -> Iterator[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [f for f in PLAN_FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"Action plan is missing columns: {missing}")
        for lineno, row in enumerate(reader, start=2):
            action = (row.get("action") or "").strip().lower()
            if action not in VALID_ACTIONS:
                raise ValueError(f"Line {lineno}: unknown action {action!r}")
            yield {k: (row.get(k) or "").strip() for k in PLAN_FIELDS}


class RollbackJournal:
    """Append-only JSONL journal of applied changes."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.seq = 0
        self._fh = open(path, "a", encoding="utf-8")

    def record(self, entry: Dict[str, Any]) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "seq": self.seq, **entry}
        self.seq += 1
        self._fh.write(json.dumps(entry, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    @staticmethod
    def read(path: str) -> List[Dict[str, Any]]:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def _confirm_interactive(row: Dict[str, str]) -> bool:
    answer = input(
        f"Apply {row['action']} on {row['resource']}/{row['object_id']} "
        f"({row['reason']})? [y/N] "
    ).strip().lower()
    return answer in {"y", "yes"}


def apply_plan(
    client: IdmClient,
    plan_path: str,
    journal: RollbackJournal,
    dry_run: bool = True,
    limit: Optional[int] = None,
    interactive: bool = False,
    strict: bool = False,
) -> Dict[str, int]:
    """Apply an action plan. Returns counters by outcome."""
    stats = {"applied": 0, "dry_run": 0, "skipped": 0, "failed": 0}

    for n, row in enumerate(read_plan(plan_path)):
        if limit is not None and n >= limit:
            break
        action, resource, object_id = row["action"], row["resource"], row["object_id"]
        desc = f"{action} {resource}/{object_id}"
        log.info("[%d] %s -- %s", n + 1, desc, row["reason"] or "no reason given")

        if dry_run:
            journal.record({**row, "before": None, "after": None,
                            "status": "dry-run", "error": ""})
            stats["dry_run"] += 1
            continue
        if interactive and not _confirm_interactive(row):
            journal.record({**row, "before": None, "after": None,
                            "status": "skipped", "error": "declined interactively"})
            stats["skipped"] += 1
            continue

        try:
            if action == "patch":
                current = client.read(resource, object_id, fields=[row["field"]])
                before = current.get(row["field"])
                if row["old_value"] and str(before) != row["old_value"]:
                    msg = (f"expected {row['field']}={row['old_value']!r}, "
                           f"found {before!r}")
                    if strict:
                        raise IdmError(msg)
                    log.warning("OLD_VALUE MISMATCH on %s: %s", desc, msg)
                new_value = _coerce(row["new_value"])
                client.patch(resource, object_id, [
                    {"operation": "replace", "field": f"/{row['field']}",
                     "value": new_value}
                ])
                journal.record({**row, "before": before, "after": new_value,
                                "status": "applied", "error": ""})
            elif action == "delete":
                before = client.read(resource, object_id)  # full snapshot for rollback
                client.delete(resource, object_id)
                journal.record({**row, "before": before, "after": None,
                                "status": "applied", "error": ""})
            elif action == "create":
                created = client.create(resource, _coerce(row["new_value"]))
                journal.record({**row, "before": None,
                                "after": created.get("_id"),
                                "status": "applied", "error": ""})
            stats["applied"] += 1
        except Exception as exc:  # noqa: BLE001 - journal everything, keep going
            log.error("FAILED %s: %s", desc, exc)
            journal.record({**row, "before": None, "after": None,
                            "status": "failed", "error": str(exc)[:500]})
            stats["failed"] += 1

    return stats


def rollback(client: IdmClient, journal_path: str) -> Dict[str, int]:
    """Undo an applied journal by replaying entries in reverse order."""
    entries = RollbackJournal.read(journal_path)
    applied = [e for e in entries if e.get("status") == "applied"]
    stats = {"rolled_back": 0, "failed": 0}
    log.info("Rolling back %d applied entr(ies) ...", len(applied))

    for entry in reversed(applied):
        action, resource = entry["action"], entry["resource"]
        desc = f"rollback {action} {resource}/{entry.get('object_id')}"
        try:
            if action == "patch":
                client.patch(resource, entry["object_id"], [
                    {"operation": "replace",
                     "field": f"/{entry['field']}",
                     "value": entry["before"]}
                ])
            elif action == "delete":
                snapshot = entry.get("before") or {}
                snapshot.pop("_id", None)
                # Re-create under the original id when the backend allows it.
                client.create(f"{resource}/{entry['object_id']}", snapshot)
            elif action == "create":
                client.delete(resource, entry.get("after") or entry.get("object_id"))
            log.info("OK %s", desc)
            stats["rolled_back"] += 1
        except Exception as exc:  # noqa: BLE001
            log.error("FAILED %s: %s", desc, exc)
            stats["failed"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to JSON config file")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", help="CSV action plan to apply")
    group.add_argument("--rollback", metavar="JOURNAL",
                       help="Rollback journal to undo")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes (default is dry-run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N plan rows (canary)")
    parser.add_argument("--interactive", action="store_true",
                        help="Confirm each record before applying")
    parser.add_argument("--strict", action="store_true",
                        help="Abort a patch when old_value doesn't match current")
    parser.add_argument("--journal", default="remediation.journal.jsonl",
                        help="Rollback journal path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = IdmClient(ToolkitConfig.load(args.config))

    if args.rollback:
        stats = rollback(client, args.rollback)
        print(f"Rollback complete: {stats}")
        return

    journal = RollbackJournal(args.journal)
    try:
        stats = apply_plan(
            client, args.plan, journal,
            dry_run=not args.apply,
            limit=args.limit,
            interactive=args.interactive,
            strict=args.strict,
        )
    finally:
        journal.close()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"{mode} complete: {stats} (journal: {args.journal})")


if __name__ == "__main__":
    main()
