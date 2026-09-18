# forgerock-data-toolkit

Production data-remediation toolkit for **PingIDM / ForgeRock Identity Management**.
It finds the data problems that break reconciliations — duplicate correlation
values, orphaned links, sync drift — explains the likely root cause, and applies
fixes safely with dry-run-by-default execution and a rollback journal.

Built by **Vamshi Angothu** — SRE specializing in IAM (ForgeRock / Ping Identity).

## What it does

```
 ┌──────────────────┐      ┌──────────────────────┐      ┌────────────────┐
 │  recon_analyzer  │      │ link_conflict_finder │      │ orphan_detector│
 │  audit/recon ->  │      │ duplicate correlation│      │ stale repo/link│
 │  failure report  │      │ values, dup links    │      │ entries        │
 └────────┬─────────┘      └──────────┬───────────┘      └───────┬────────┘
          └──────────────┬────────────┴──────────────┬───────────┘
                         ▼                           ▼
                  ┌─────────────┐            ┌──────────────────┐
                  │  report.py  │            │ bulk_remediator  │
                  │  incident-  │            │ CSV action plan, │
                  │  style root │            │ dry-run default, │
                  │  cause doc  │            │ rollback journal │
                  └─────────────┘            └──────────────────┘
```

| Module | One-liner |
|---|---|
| `idm_client.py` | Retry-with-backoff REST client (header auth, paged queries, rate limiting) |
| `recon_analyzer.py` | Summarizes `audit/recon`: by status/situation/mapping, top failure reasons, CSV triage list |
| `link_conflict_finder.py` | Detects correlation collisions (`FOUND_ALREADY_LINKED` precursors) and duplicate links |
| `orphan_detector.py` | Finds `repo/link` entries whose source or target object no longer exists |
| `bulk_remediator.py` | Applies a CSV action plan: **dry-run default**, canary `--limit`, `--interactive`, rollback journal + `--rollback` |
| `report.py` | Turns analyzer outputs into an incident-style markdown report with ranked root-cause hypotheses |

## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (only dependency: `requests`)

## Configuration

Copy `examples/config.example.json` to `config.json` (gitignored) and fill in your
environment, or export variables — **env always wins**:

| Variable | Purpose |
|---|---|
| `FR_IDM_BASE_URL` | e.g. `https://idm.example.com:8443/openidm` (required) |
| `FR_IDM_USERNAME` / `FR_IDM_PASSWORD` | Service account (read-only for analysis; write for remediation) |
| `FR_IDM_VERIFY_SSL` | `true`/`false` (default `true` — keep it true in prod) |
| `FR_IDM_PAGE_SIZE` / `FR_IDM_MAX_RETRIES` / `FR_IDM_BACKOFF` | Paging & retry tuning |
| `FR_IDM_RATE_LIMIT` | Max requests/sec (default 10) |
| `FR_IDM_CORRELATION_ATTRS` | Comma-separated, e.g. `userName,mail,employeeNumber` |

## Typical workflow

```bash
# 1. What broke in the last recon window?
python -m fr_toolkit.recon_analyzer --config config.json \
  --since 2026-09-01T00:00:00Z --out-json recon-summary.json --out-csv recon-failures.csv

# 2. Are there live correlation collisions behind the FOUND_ALREADY_LINKEDs?
python -m fr_toolkit.link_conflict_finder --config config.json \
  --mapping systemLdapAccounts_managedUser --out-csv link-conflicts.csv

# 3. Any stale links from deletes that never propagated?
python -m fr_toolkit.orphan_detector --config config.json \
  --mapping systemLdapAccounts_managedUser \
  --source-resource managed/user --target-resource system/ldap/account \
  --out-csv orphans.csv

# 4. Write it up for the client / on-call
python -m fr_toolkit.report --recon recon-summary.json \
  --conflicts link-conflicts.csv --orphans orphans.csv \
  --title "IDM data incident 2026-09-17" --out incident-report.md

# 5. Remediate: build a CSV plan (see examples/action-plan.example.csv),
#    preview it, canary 5 rows, then the full run
python -m fr_toolkit.bulk_remediator --config config.json --plan plan.csv
python -m fr_toolkit.bulk_remediator --config config.json --plan plan.csv \
  --apply --limit 5 --journal run1.journal.jsonl
python -m fr_toolkit.bulk_remediator --config config.json --plan plan.csv \
  --apply --journal run1.journal.jsonl

# 6. Something regressed? Undo the journal, newest change first
python -m fr_toolkit.bulk_remediator --config config.json \
  --rollback run1.journal.jsonl
```

## ⚠️ Safety rules

1. **Dry-run first, always.** The remediator previews without `--apply`.
2. **Test in a lower environment** before touching production IDM.
3. **Back up** the affected objects/mappings before a bulk run.
4. **Canary**: use `--limit 5` on the first real run, verify, then go wide.
5. **Use a least-privilege service account** — read-only for the analyzers.
6. Every applied change lands in the **rollback journal**; keep the journal
   until the next clean recon confirms the fix.
7. Never commit `config.json`, `.env`, journals, or real CSV extracts —
   `.gitignore` already excludes them.

## Auth notes

The client authenticates with the IDM REST convention
(`X-OpenIDM-Username` / `X-OpenIDM-Password` headers). For PingOne Advanced
Identity Cloud tenants, swap in a bearer-token header in `idm_client.py`
(the session header block is the single place to change).

## License

MIT — see [LICENSE](LICENSE).
