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
- [x] Verify live edit/create/delete and tag/link note serialization.
- [x] Pre-change full backup: 9,718 nodes, checksum verified.
- [x] Private semantic plan: 343 node edits, 362 tags, 11 curated reciprocal pairs.
- [x] Apply all 343 updates, journal all; 0 conflicts and 0 failed writes.
- [x] Export entire live account after the run and verify 343/343 note exact matches, 362/362 tags, 11/11 reciprocal link pairs, unchanged original names/structure on edited nodes, 0 missing original IDs, 0 errors. Three new nodes appeared since baseline, outside the 343 edits.
- [x] Targeted tests: 6 PASS.
- [ ] Finish single-writer integration and verify canonical master revision.

## Current step
Completed live account enrichment and full verification. Only code integration/merge verification remains. All private data and its journal stay on Fedora under `~/Documents/Workflowy/semantic-enrichment/2026-10-15`.

## Evidence
- Baseline: 9,718 nodes, SHA-256 `eae61e55ade36bce96ca1160cb274dd9e2da7815043d5ad8cde983e00a6095e0`.
- Final read-only report: `/home/daniele/Documents/Workflowy/semantic-enrichment/2026-10-15/verification-v1.json` with `confirmed_notes=343`, `tags_confirmed=362`, `reciprocal_pairs=11`, `current_nodes=9721`, `current_unique_ids=true`, `missing_original_ids=0`, `new_ids=3`, `errors=[]`.
- Immutable original backup and private plan are retained. Rollback uses `tools/workflowy_semantic_enrich.py rollback` and compare-before-write; a former null note becomes an empty note if cleared through the public API.
- Unit tests: `PYTHONPATH=src python3 -m unittest discover -s tests -p test_workflowy_semantic_enrich.py -q`, 6 PASS.
- HTML links are reciprocal and confirmed as stored API data; appearance in Workflowy's native UI backlink panel was not proven. Public API-created links have historically not triggered backlink indexing automatically.

## Acceptance criteria
All planned edits are verified or explicitly recorded as conflicts; source nodes, hierarchy, title strings and preexisting note prefixes stay unchanged. Rollback only touches a node if it still matches exactly what this process wrote.

## Next action
Run `python3 /home/daniele/projects/github-autosync/repo_single_writer.py finish --repo /home/daniele/projects/workflowy-importer --task-id workflowy-semantic-20261015`. Check merge/integration status on `refs/heads/master`, and report any pending queue without falsely claiming merged.
