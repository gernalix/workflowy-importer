"""Reversible Workflowy topic tagging and explicitly reciprocal links."""
from __future__ import annotations
import argparse
import collections
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import time
from workflowy_importer.api import WorkflowyClient
from workflowy_importer.credentials import load_api_key
from workflowy_importer.links import workflowy_url

TOPICS = {
    "personalhub": r"\bpersonal\s*hub\b|\bpersonalhub\b|\bwordpulse\b",
    "megavault": r"\bmega\s*vault\b",
    "workflowy": r"\bwork\s*flowy\b",
    "codex": r"\bcodex\b",
    "grindr": r"\bgrindr\b",
    "telegram": r"\btelegram\b",
    "fedora": r"\bfedora\b",
    "whatsapp": r"\bwhatsapp\b",
    "logseq": r"\blogseq\b",
    "obsidian": r"\bobsidian\b",
    "readwise": r"\breadwise\b",
    "docker": r"\bdocker\b",
    "python": r"\bpython\b",
    "github": r"\bgithub\b",
    "chatgpt": r"\bchat\s*gpt\b",
}
TOPICS = {k: re.compile(v, re.I) for k, v in TOPICS.items()}


def title(name: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", name)).strip()


def tags_for(node: dict) -> list[str]:
    name, note = node.get("name") or "", node.get("note") or ""
    t = title(name)
    if (not 3 <= len(t) <= 155 or len(name) > 340 or name.count("\n") > 1
            or t.startswith(("http://", "https://", "/", "file:"))
            or (node.get("data") or {}).get("layoutMode") == "code-block"):
        return []
    return [
        tag for tag, regex in TOPICS.items()
        if regex.search(t) and not re.search(r"(?<!\w)#" + tag + r"\b", name + " " + note, re.I)
    ][:3]


def ancestor(a: str, b: str, by_id: dict) -> bool:
    cur = by_id[a].get("parent_id")
    for _ in range(len(by_id)):
        if not cur:
            return False
        if cur == b:
            return True
        cur = (by_id.get(cur) or {}).get("parent_id")
    return True


def make_edits(nodes: list[dict], pairs: list[list[str]]) -> list[dict]:
    by_id = {str(n["id"]): n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("Duplicate node IDs")
    links = collections.defaultdict(list)
    for pair in pairs:
        if len(pair) != 2 or pair[0] == pair[1] or any(i not in by_id for i in pair):
            raise ValueError("Invalid pair: " + repr(pair))
        a, b = pair
        if ancestor(a, b, by_id) or ancestor(b, a, by_id):
            raise ValueError("Ancestor/descendant pair: " + repr(pair))
        links[a].append(b)
        links[b].append(a)
    edits = []
    for node in nodes:
        name, old = node.get("name") or "", node.get("note")
        note = old or ""
        tags = tags_for(node)
        targets = []
        for other in dict.fromkeys(links.get(node["id"], [])):
            if other[-12:] not in (name + " " + note):
                targets.append(other)
        targets = targets[:8]
        if not tags and not targets:
            continue
        additions = []
        if tags:
            additions.append(" ".join("#" + tag for tag in tags))
        if targets:
            values = []
            for other in targets:
                caption = title(by_id[other].get("name") or "")[:68] or "Nodo collegato"
                values.append('<a href="' + workflowy_url(other) + '">' + html.escape(caption) + "</a>")
            additions.append("Correlati: " + " · ".join(values))
        new = note + ("\n\n" if note else "") + "\n".join(additions)
        edits.append({"id": node["id"], "name": name, "old_note": old,
                      "new_note": new, "tags": tags, "links": targets})
    return sorted(edits, key=lambda e: (not bool(e["links"]), -len(e["tags"]), e["id"]))


def private_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def journal_write(path: Path, node_id: str, status: str, detail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                            "id": node_id, "status": status, "detail": detail}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def journal_status(path: Path) -> dict[str, str]:
    latest = {}
    if path.exists():
        for line in path.read_text().splitlines():
            item = json.loads(line)
            latest[item["id"]] = item["status"]
    return latest


def plan(args) -> None:
    raw = args.backup.read_bytes()
    nodes = json.loads(raw)
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("Expected a nonempty Workflowy export")
    pairs = json.loads(args.pairs.read_text()) if args.pairs else []
    edits = make_edits(nodes, pairs)
    private_write(args.output, {
        "schema": "workflowy-semantic/v1",
        "created": datetime.now(timezone.utc).isoformat(),
        "backup_file": str(args.backup.resolve()),
        "backup_sha256": hashlib.sha256(raw).hexdigest(),
        "node_count": len(nodes), "pairs": pairs, "edits": edits,
    })
    print(json.dumps({"nodes": len(nodes), "candidate_edits": len(edits),
                      "new_tags": sum(len(e["tags"]) for e in edits),
                      "reciprocal_links": len(pairs), "plan": str(args.output)}))


def mutate(args) -> None:
    record = json.loads(args.plan.read_text())
    if record.get("schema") != "workflowy-semantic/v1":
        raise ValueError("Invalid plan schema")
    if hashlib.sha256(Path(record["backup_file"]).read_bytes()).hexdigest() != record["backup_sha256"]:
        raise ValueError("Baseline backup checksum mismatch")
    entries = record["edits"] if args.mode == "apply" else list(reversed(record["edits"]))
    latest = journal_status(args.journal)
    done = skipped = conflict = readbacks = 0
    with WorkflowyClient(load_api_key()) as api:
        for entry in entries:
            id = entry["id"]
            if args.mode == "apply" and latest.get(id) == "applied":
                skipped += 1
                continue
            if args.mode == "rollback" and latest.get(id) != "applied":
                continue
            if done >= args.limit:
                break
            try:
                live = api.get_node(id)
                time.sleep(args.delay)
                note = live.get("note") or ""
                before = entry["new_note"] if args.mode == "rollback" else (entry["old_note"] or "")
                after = (entry["old_note"] or "") if args.mode == "rollback" else entry["new_note"]
                if args.mode == "apply" and note == after:
                    journal_write(args.journal, id, "applied", "resumed")
                    latest[id] = "applied"
                    skipped += 1
                    continue
                if live.get("name") != entry["name"] or note != before:
                    journal_write(args.journal, id, "conflict", "live fields differ")
                    conflict += 1
                    continue
                journal_write(args.journal, id, "pending", args.mode)
                api.update_node(id, note=after)
                time.sleep(args.delay)
                check_now = args.mode == "rollback" or (done + 1) % args.readback_every == 0
                if check_now:
                    checked = api.get_node(id)
                    time.sleep(args.delay)
                    if (checked.get("note") or "") != after:
                        raise RuntimeError("API readback mismatch")
                    readbacks += 1
                state = "applied" if args.mode == "apply" else "rolled_back"
                journal_write(args.journal, id, state, "GET readback" if check_now else "HTTP 200; batch export verification pending")
                latest[id] = state
                done += 1
                if done % 10 == 0:
                    print(json.dumps({"mode": args.mode, "updated": done, "readbacks": readbacks, "conflicts": conflict}), flush=True)
            except Exception as exc:
                journal_write(args.journal, id, "error", type(exc).__name__)
                raise
    print(json.dumps({"mode": args.mode, "updated": done, "readbacks": readbacks,
                      "already_processed": skipped, "conflicts": conflict,
                      "journal": str(args.journal)}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_subparsers(dest="mode", required=True)
    p = modes.add_parser("plan")
    p.add_argument("--backup", required=True, type=Path)
    p.add_argument("--pairs", type=Path)
    p.add_argument("--output", required=True, type=Path)
    for name in ("apply", "rollback"):
        p = modes.add_parser(name)
        p.add_argument("--plan", required=True, type=Path)
        p.add_argument("--journal", required=True, type=Path)
        p.add_argument("--limit", type=int, default=25)
        p.add_argument("--delay", type=float, default=1.15)
        p.add_argument("--readback-every", type=int, default=1)
    args = parser.parse_args()
    if args.mode == "plan":
        plan(args)
    else:
        if args.limit < 1 or args.delay < 1 or args.readback_every < 1:
            raise ValueError("limit and readback-every must be positive; delay >=1s")
        mutate(args)


if __name__ == "__main__":
    main()
