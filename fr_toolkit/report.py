"""Root-cause report generator for PingIDM / ForgeRock IDM data incidents.

Takes the machine-readable outputs of the other toolkit modules and turns
them into an incident-style markdown report a client (or an on-call SRE)
can act on:

- Symptoms (what was observed, with numbers)
- Data evidence (tables built from the analyzer CSVs/JSON)
- Likely root cause (ranked hypotheses, each tied to evidence -- these are
  hypotheses with confidence levels, not certainties)
- Recommended fixes (ordered, referencing the toolkit modules that perform
  each step)

Usage:
    python -m fr_toolkit.recon_analyzer --config config.json \\
        --out-json recon-summary.json --out-csv recon-failures.csv
    python -m fr_toolkit.link_conflict_finder --config config.json \\
        --mapping systemLdapAccounts_managedUser --out-csv link-conflicts.csv
    python -m fr_toolkit.orphan_detector --config config.json \\
        --mapping systemLdapAccounts_managedUser --out-csv orphans.csv
    python -m fr_toolkit.report --recon recon-summary.json \\
        --conflicts link-conflicts.csv --orphans orphans.csv \\
        --title "IDM data incident 2026-09-17" --out incident-report.md
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _load_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_csv(path: Optional[str]) -> List[Dict[str, str]]:
    if not path:
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _md_table(headers: List[str], rows: List[List[Any]], limit: int = 20) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    if len(rows) > limit:
        lines.append(f"\n*... and {len(rows) - limit} more rows*")
    return "\n".join(lines)


def _hypotheses(
    recon: Optional[Dict[str, Any]],
    conflicts: List[Dict[str, str]],
    orphans: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Rank likely root causes from the evidence. Confidence is heuristic."""
    hyps: List[Dict[str, str]] = []
    situations = (recon or {}).get("by_situation", {})
    total = (recon or {}).get("total_records", 0) or 1

    def pct(n: int) -> str:
        return f"{100.0 * n / total:.1f}%"

    linked = situations.get("FOUND_ALREADY_LINKED", 0)
    if linked:
        n_conf = len(conflicts)
        hyps.append({
            "hypothesis": "Duplicate identity data: the same person exists more than "
                          "once under one correlation key (e.g. two managed users "
                          "sharing a mail/employeeID).",
            "confidence": "High" if n_conf else "Medium",
            "evidence": f"{linked} FOUND_ALREADY_LINKED recon failures "
                        f"({pct(linked)} of records)"
                        + (f"; link_conflict_finder confirmed {n_conf} live "
                           f"collision(s)" if n_conf else
                           "; no live collisions found -- data may since have been "
                           "cleaned, or collisions are on the source side"),
            "fix": "Deduplicate/merge the colliding identities, then re-run recon. "
                   "Use link_conflict_finder.py to enumerate collisions and "
                   "bulk_remediator.py (dry-run first) to apply the merge plan.",
        })

    orph_src = sum(1 for o in orphans if o.get("orphan_side") == "source")
    orph_tgt = sum(1 for o in orphans if o.get("orphan_side") == "target")
    if orphans:
        hyps.append({
            "hypothesis": "Deletes on one system are not propagated: accounts removed "
                          "upstream/downstream left stale links behind.",
            "confidence": "High" if len(orphans) > 10 else "Medium",
            "evidence": f"{len(orphans)} orphaned link(s): {orph_src} source-side, "
                        f"{orph_tgt} target-side",
            "fix": "Decide per mapping whether a missing object should unlink or "
                   "delete the counterpart; enable the matching situation action "
                   "(e.g. UNLINK on TARGET_GONE); clean existing orphans with "
                   "bulk_remediator.py delete_link rows, then re-run recon.",
        })

    unqual = situations.get("UNQUALIFIED", 0)
    if unqual and unqual / total > 0.2:
        hyps.append({
            "hypothesis": "Mapping scope/filter drift: a large share of objects no "
                          "longer match validSource/validTarget, so recon ignores "
                          "them (often after an HR feed or OU change).",
            "confidence": "Medium",
            "evidence": f"{unqual} UNQUALIFIED records ({pct(unqual)} of total)",
            "fix": "Diff the current validSource/validTarget queries against the "
                   "upstream feed; fix the filter or the feed, then re-run recon.",
        })

    mappings = (recon or {}).get("by_mapping", {})
    if len(mappings) > 1:
        worst = max(mappings, key=mappings.get)
        hyps.append({
            "hypothesis": f"Mapping-specific configuration issue in {worst}: failures "
                          "concentrate in one mapping while others are healthy.",
            "confidence": "Medium",
            "evidence": f"Failure distribution by mapping: "
                        + ", ".join(f"{m}={c}" for m, c in
                                    list(mappings.items())[:5]),
            "fix": f"Review {worst}: correlation query, situation action scripts, "
                   "and connector configuration. Test changes in a lower "
                   "environment before production.",
        })

    if not hyps:
        hyps.append({
            "hypothesis": "No dominant pattern in the collected evidence; the issue "
                          "may be transient (connector outage, throttling) or outside "
                          "the analyzed window.",
            "confidence": "Low",
            "evidence": "Analyzer outputs show no concentrated failure mode.",
            "fix": "Widen the recon analysis window, check connector and IDM server "
                   "logs around the incident time, and re-run the toolkit.",
        })
    return hyps


def build_report(
    title: str,
    recon: Optional[Dict[str, Any]],
    conflicts: List[Dict[str, str]],
    orphans: List[Dict[str, str]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = (recon or {}).get("total_records", 0)
    failed = (recon or {}).get("failure_count", 0)

    parts = [
        f"# {title}",
        "",
        f"_Generated {now} by forgerock-data-toolkit_",
        "",
        "## Symptoms",
        "",
    ]
    if recon:
        parts += [
            f"- **{failed}** of **{total}** reconciliation records failed "
            f"({100.0 * failed / max(total, 1):.1f}%).",
            f"- Mappings affected: {len(recon.get('by_mapping', {}))}.",
            f"- Live correlation collisions found: **{len(conflicts)}**.",
            f"- Orphaned links found: **{len(orphans)}**.",
        ]
    else:
        parts += ["- Recon summary not provided; findings below are link/orphan scans only."]
    parts += ["", "## Data evidence", ""]

    if recon and recon.get("by_situation"):
        parts += ["### Failures by recon situation", "",
                  _md_table(["Situation", "Count"],
                            [[s, c] for s, c in recon["by_situation"].items()]), ""]
    if recon and recon.get("top_failure_reasons"):
        parts += ["### Top failure reasons", "",
                  _md_table(["Reason", "Count"],
                            [[r["reason"], r["count"]]
                             for r in recon["top_failure_reasons"]]), ""]
    if conflicts:
        parts += ["### Correlation collisions / duplicate links", "",
                  _md_table(["Type", "Resource", "Attribute", "Value", "IDs"],
                            [[c["check_type"], c["mapping_or_resource"],
                              c["attribute"], c["value"][:40], c["object_ids"][:60]]
                             for c in conflicts]), ""]
    if orphans:
        parts += ["### Orphaned links", "",
                  _md_table(["Link", "Side", "Missing resource", "Missing ID"],
                            [[o["link_id"], o["orphan_side"],
                              o["missing_resource"], o["missing_id"]]
                             for o in orphans]), ""]

    parts += ["## Likely root cause", "",
              "_Ranked hypotheses. Treat as leads to verify, not conclusions._", ""]
    for i, h in enumerate(_hypotheses(recon, conflicts, orphans), 1):
        parts += [f"### {i}. {h['hypothesis']}",
                  f"- **Confidence:** {h['confidence']}",
                  f"- **Evidence:** {h['evidence']}",
                  f"- **Recommended fix:** {h['fix']}", ""]

    parts += [
        "## Recommended remediation order",
        "",
        "1. Confirm each hypothesis against a lower environment or a sampled "
        "object set -- never bulk-fix production on report data alone.",
        "2. Build the fix list as a CSV action plan "
        "(see `examples/action-plan.example.csv`).",
        "3. Run `bulk_remediator.py` in dry-run mode, review the journal, then "
        "apply to a canary batch (`--limit 5`).",
        "4. Re-run the analyzers; failures should trend to zero. Roll back via "
        "the journal if anything regresses.",
        "",
        "## Collection notes",
        "",
        "- Recon window/filter: "
        f"`{(recon or {}).get('filter', 'n/a')}`",
        f"- Report inputs: recon summary, {len(conflicts)} conflict row(s), "
        f"{len(orphans)} orphan row(s).",
    ]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recon", help="recon-summary.json from recon_analyzer")
    parser.add_argument("--conflicts", help="link-conflicts.csv from link_conflict_finder")
    parser.add_argument("--orphans", help="orphans.csv from orphan_detector")
    parser.add_argument("--title", default="IDM data incident report")
    parser.add_argument("--out", default="incident-report.md")
    args = parser.parse_args()

    report = build_report(
        args.title,
        _load_json(args.recon),
        _load_csv(args.conflicts),
        _load_csv(args.orphans),
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"Report written to {args.out}")


if __name__ == "__main__":
    main()
