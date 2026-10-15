# Workflowy semantic enrichment — task state

## Goal / constraints
Add high-confidence topical tags and reciprocal native-format node hyperlinks to the existing authenticated Workflowy account. Never overwrite original titles or move/delete original nodes. Preserve every preexisting note character. Treat the verified pre-change JSON export as immutable. C2/C3 are not used.

## Verified facts / decisions
- Fedora API access through existing workflowy-importer: live create / update-name / update-note / readback / delete smoke: PASS; temporary nodes confirmed deleted (HTTP 404).
- Immutable pre-change JSON: `/home/daniele/Documents/Workflowy/backups/workflowy-pre-tag-backlink-20261015-173834-CEST.json`; 9,718 nodes, 5,054,566 bytes, SHA-256 `eae61e55ade36bce96ca1160cb274dd9e2da7815043d5ad8cde983e00a6095e0`. Report is beside backup.
- Private semantic plan: `/home/daniele/Documents/Workflowy/semantic-enrichment/2026-10-15/plan-v1.json`; generated from the backup with 343 candidate node edits, 362 tags, and 11 explicitly reciprocal link pairs.
- Tool: `tools/workflowy_semantic_enrich.py`, with `plan`, `apply` and `rollback` commands; targeted unit tests: 5 PASS. No secrets or private plan contents are tracked.
- Each write requires a fresh GET precondition of both name and note, followed by note-only POST and GET readback. Each transition is fsynced to a local JSONL journal; apply can resume. An unexpected change is a conflict, not an overwrite.
- A blank note may be restored as `""` instead of exactly `null` because the public API does not support setting a note to null; the original JSON backup preserves the exact old value.
- Internal hyperlink HTML is accepted and read back by the API; reciprocal links guarantee navigability, but automatic UI-generated backlink badges remain unverified.

## Checklist
- [x] Verify live authentication, edit, deletion, link HTML storage.
- [x] Create and SHA-256 verify pre-change account snapshot.
- [x] Build a conservative high-confidence plan, 11 curated reciprocal pairs.
- [x] Targeted unit tests PASS.
- [x] First 13 linked nodes applied and GET-verified, 0 conflicts.
- [ ] Continue the remaining 330 nodes with per-node precondition, sparse GET readback and final full export verification.
- [ ] Check count, API readback and absence of new duplicates/conflicts.
- [ ] Publish final tool through single-writer integration and verify canonical revision.

## Current step
The first 13 linked nodes have been individually verified. A second throttled run has passed at least 250 additional updates with HTTP success, periodic GET readback and zero conflicts. Continue without restarting or invalidating the running process. `verify` is now implemented and unit-tested for full-export final validation.

## Evidence
Run: `PYTHONPATH=src python3 -m unittest discover -s tests -p test_workflowy_semantic_enrich.py -v`.
Journal will be `/home/daniele/Documents/Workflowy/semantic-enrichment/2026-10-15/journal-v1.jsonl`.

## Acceptance criteria
All planned edits are verified or explicitly recorded as conflicts; source nodes, hierarchy, title strings and preexisting note prefixes stay unchanged. Rollback only touches a node if it still matches exactly what this process wrote.

## Next action
Wait for PID 1899805 to finish; run `tools/workflowy_semantic_enrich.py verify --plan .../plan-v1.json --journal .../journal-v1.jsonl --output .../verification-v1.json`, check report. Record final counts/commit and run the single-writer `finish` integration.
