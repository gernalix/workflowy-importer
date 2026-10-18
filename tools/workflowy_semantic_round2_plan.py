"""Build a conservative Workflowy v2 note-edit plan; NEVER writes to Workflowy.

Uses a precise diff to remove ONLY tags created by our first pass and adds
human-curated reciprocal hyperlinks. Output runs through the existing journaled
apply/rollback/verify tool (workflowy_semantic_enrich.py).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import re
from pathlib import Path

from workflowy_semantic_enrich import ancestor, private_write, title
from workflowy_importer.links import workflowy_url


def drop_our_old_tags(note: str, first_edit: dict) -> str:
    if not first_edit.get("tags"):
        return note
    original = first_edit.get("old_note") or ""
    expected = first_edit["new_note"]
    if note != expected or not expected.startswith(original):
        raise ValueError("First-pass note differs from verified baseline")
    addition = expected[len(original):]
    separator = "\n\n" if original else ""
    if not addition.startswith(separator):
        raise ValueError("First-pass appended-note separator mismatch")
    lines = addition[len(separator):].split("\n")
    tagline = " ".join("#" + v for v in first_edit["tags"])
    if not lines or lines[0] != tagline:
        raise ValueError("First-pass tag line does not match previous plan")
    remainder = "\n".join(lines[1:])
    if remainder and not remainder.startswith("Correlati: "):
        raise ValueError("Unexpected first-pass suffix: refusing edit")
    return original + (separator + remainder if remainder else "")


def build_plan(snapshot: list[dict], prior: dict, pairs: list[dict]) -> dict:
    by_id = {n["id"]: n for n in snapshot}
    if len(by_id) != len(snapshot):
        raise ValueError("Duplicate nodes in source snapshot")
    desired = {}
    old_notes = {}
    removed_tags = 0
    for edit in prior["edits"]:
        n = by_id.get(edit["id"])
        if n is None:
            raise ValueError("A first-pass-modified node is missing")
        if n.get("name") != edit["name"] or n.get("note") != edit["new_note"]:
            raise ValueError("First-pass note diverged; manual conflict review required")
        before = n.get("note") or ""
        after = drop_our_old_tags(before, edit)
        if before != after:
            desired[n["id"]] = after
            old_notes[n["id"]] = n.get("note")
            removed_tags += len(edit["tags"])

    target_links = collections.defaultdict(list)
    clean_pairs = []
    rationales = []
    for entry in pairs:
        a, b = entry["a"], entry["b"]
        reason = entry.get("reason", "").strip()
        if a == b or a not in by_id or b not in by_id:
            raise ValueError("Invalid pair or missing node")
        if len(reason) < 14:
            raise ValueError("Every link requires a meaningful editorial rationale")
        if ancestor(a, b, by_id) or ancestor(b, a, by_id):
            raise ValueError("An ancestor/descendant link is redundant")
        if tuple(sorted((a, b))) in {tuple(sorted(x)) for x in clean_pairs}:
            raise ValueError("Duplicate proposed pair")
        clean_pairs.append([a, b])
        rationales.append({"a": a, "b": b, "reason": reason})
        target_links[a].append(b)
        target_links[b].append(a)
    if any(len(v) > 6 for v in target_links.values()):
        raise ValueError("Too many new links on a single node")

    added_links = 0
    for source, targets in target_links.items():
        n = by_id[source]
        before = desired.get(source, n.get("note") or "")
        available = []
        for target in targets:
            url = workflowy_url(target)
            if url in (n.get("name") or "") + " " + before:
                continue
            label = title(by_id[target].get("name") or "")[:80] or "Nota correlata"
            available.append(f'<a href="{url}">{html.escape(label, quote=False)}</a>')
        if not available:
            continue
        new = before + ("\n\n" if before else "") + "Vedi anche: " + " · ".join(available)
        desired[source] = new
        old_notes.setdefault(source, n.get("note"))
        added_links += len(available)

    edits = []
    for node_id, note in desired.items():
        original = by_id[node_id]
        if note == (original.get("note") or ""):
            continue
        edits.append({
            "id": node_id, "name": original.get("name") or "",
            "old_note": old_notes[node_id], "new_note": note,
            "tags": [], "links": target_links.get(node_id, []),
        })
    edits.sort(key=lambda n: (not bool(n["links"]), n["id"]))
    return {
        "schema": "workflowy-semantic/v1",
        "edits": edits, "pairs": clean_pairs,
        "rationales": rationales,
        "removed_generated_tags": removed_tags,
        "added_link_instances": added_links,
        "tagging_policy": "No generated topic tags. Remove first-pass-only lexical tags; preserve original/user tags.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", type=Path, required=True)
    ap.add_argument("--first-plan", type=Path, required=True)
    ap.add_argument("--curated-pairs", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    raw = args.backup.read_bytes()
    old = json.loads(args.first_plan.read_text())
    earlier = Path(old["backup_file"])
    if hashlib.sha256(earlier.read_bytes()).hexdigest() != old["backup_sha256"]:
        raise ValueError("First-pass source archive SHA mismatch")
    pairs = json.loads(args.curated_pairs.read_text())
    result = build_plan(json.loads(raw), old, pairs)
    result.update({
        "backup_file": str(args.backup.resolve()),
        "backup_sha256": hashlib.sha256(raw).hexdigest(),
        "node_count": len(json.loads(raw)),
    })
    private_write(args.output, result)
    print(json.dumps({
        "plan": str(args.output), "notes_to_update": len(result["edits"]),
        "auto_tags_to_remove": result["removed_generated_tags"],
        "new_reciprocal_pairs": len(result["pairs"]),
        "link_instances_added": result["added_link_instances"],
        "new_tags": 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
