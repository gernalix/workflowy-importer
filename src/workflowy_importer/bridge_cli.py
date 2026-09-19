from __future__ import annotations

import argparse
import configparser
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from .api import WorkflowyAPIError, WorkflowyClient
from .automation import capture_routed, load_rules, record_event
from .cache import (
    connect,
    duplicate_groups,
    node_links,
    old_nodes,
    refresh_cache,
    search,
    weekly_summary,
)
from .chatgpt import conversation_to_markdown, load_conversation
from .credentials import CredentialError, DEFAULT_SECRET_FILE, load_api_key
from .control import run_control_actions
from .links import workflowy_url
from .roadmap_bridge import sync_roadmap

DEFAULT_CACHE = Path("~/.local/share/workflowy-bridge/cache.sqlite3").expanduser()
DEFAULT_RULES = Path("~/.config/workflowy-bridge/rules.json").expanduser()


def _client(args: argparse.Namespace) -> WorkflowyClient:
    key = load_api_key(
        secret_file=args.secret_file,
        env_var=args.api_key_env,
    )
    return WorkflowyClient(api_key=key, base_url=args.base_url)


def _db(path: Path | str) -> sqlite3.Connection:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return connect(p)


def _sync(client: WorkflowyClient, db: sqlite3.Connection) -> int:
    return refresh_cache(db, client.export_nodes())


def _markdown_export(nodes: list[dict]) -> str:
    by_parent: dict[str | None, list[dict]] = {}
    for node in nodes:
        by_parent.setdefault(node.get("parent_id"), []).append(node)
    for values in by_parent.values():
        values.sort(key=lambda x: (x.get("priority") is None, x.get("priority", 0)))

    lines: list[str] = []

    def emit(parent: str | None, depth: int) -> None:
        for node in by_parent.get(parent, []):
            name = str(node.get("name") or "").replace("\n", " ")
            marker = "- [x] " if node.get("completed") else "- "
            lines.append("  " * depth + marker + name)
            note = node.get("note")
            if note:
                for line in str(note).splitlines():
                    lines.append("  " * (depth + 1) + "> " + line)
            emit(str(node.get("id")), depth + 1)

    emit(None, 0)
    return "\n".join(lines) + "\n"


def _git_remote(repo_dir: Path) -> str | None:
    cfg = repo_dir / ".git" / "config"
    if not cfg.exists():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    return parser.get('remote "origin"', "url", fallback=None)


def _validate_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQLite identifier: {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wf", description="Workflowy local automation bridge"
    )
    p.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET_FILE)
    p.add_argument("--api-key-env", default="WORKFLOWY_API_KEY")
    p.add_argument(
        "--base-url",
        default="https://workflowy.com/api/v1",
        help=argparse.SUPPRESS,
    )
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="Refresh the local SQLite cache from Workflowy")
    find = sub.add_parser("find", help="Search the local cache")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=20)

    add = sub.add_parser("add", help="Create a node")
    add.add_argument("text")
    add.add_argument("--parent", default="inbox")
    add.add_argument("--note")
    add.add_argument(
        "--position", choices=["top", "bottom"], default="top"
    )
    for name in ("today", "inbox"):
        quick = sub.add_parser(name, help=f"Quick capture to {name}")
        quick.add_argument("text")
        quick.add_argument("--note")

    show = sub.add_parser("show")
    show.add_argument("node")
    url = sub.add_parser("url", help="Print a Workflowy node link")
    url.add_argument("node_id")
    move = sub.add_parser("move")
    move.add_argument("node_id")
    move.add_argument("parent")
    mirror = sub.add_parser("mirror")
    mirror.add_argument("node_id")
    mirror.add_argument("parent")
    unmirror = sub.add_parser("unmirror")
    unmirror.add_argument("node_id")
    done = sub.add_parser("done")
    done.add_argument("node_id")
    undone = sub.add_parser("undone")
    undone.add_argument("node_id")
    sub.add_parser("targets")

    backup = sub.add_parser("backup")
    backup.add_argument("output", type=Path)
    backup.add_argument("--format", choices=["json", "md"], default="json")

    route = sub.add_parser(
        "route", help="Capture using deterministic routing rules"
    )
    route.add_argument("text")
    route.add_argument("--note")
    route.add_argument("--rules", type=Path, default=DEFAULT_RULES)

    dedupe = sub.add_parser(
        "dedupe", help="Report probable duplicate node titles"
    )
    dedupe.add_argument("--limit", type=int, default=100)

    weekly = sub.add_parser("weekly-review")
    weekly.add_argument("--days", type=int, default=7)
    weekly.add_argument("--parent", default="today")

    resurface = sub.add_parser("resurface")
    resurface.add_argument("--older-than-days", type=int, default=180)
    resurface.add_argument("--limit", type=int, default=3)
    resurface.add_argument("--parent", default="today")

    ingest = sub.add_parser(
        "ingest", help="Ingest a JSON event from another system"
    )
    ingest.add_argument(
        "source",
        choices=[
            "github",
            "activitywatch",
            "personalhub",
            "calendar",
            "gmail",
            "generic",
        ],
    )
    ingest.add_argument("json_file", type=Path)
    ingest.add_argument("--rules", type=Path, default=DEFAULT_RULES)

    chat = sub.add_parser(
        "chatgpt",
        help="Import one conversation from conversations.json",
    )
    chat.add_argument("export", type=Path)
    selector = chat.add_mutually_exclusive_group(required=True)
    selector.add_argument("--conversation-id")
    selector.add_argument("--title")
    chat.add_argument("--parent", default="inbox")

    projects = sub.add_parser(
        "projects", help="Create an index of local Git projects"
    )
    projects.add_argument("--root", type=Path, default=Path("~/projects"))
    projects.add_argument("--parent", default="inbox")

    links = sub.add_parser(
        "export-links", help="Export node IDs and Workflowy URLs"
    )
    links.add_argument("output", type=Path)

    ph = sub.add_parser(
        "personalhub-links",
        help="Fill a PersonalHub text column with unique Workflowy links",
    )
    ph.add_argument("database", type=Path)
    ph.add_argument("--table", required=True)
    ph.add_argument("--name-column", required=True)
    ph.add_argument("--url-column", required=True)
    ph.add_argument("--create-column", action="store_true")
    ph.add_argument("--apply", action="store_true")

    norm = sub.add_parser(
        "normalize",
        help="Apply explicit regex normalizations from a JSON config",
    )
    norm.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    norm.add_argument("--apply", action="store_true")

    arc = sub.add_parser(
        "archive-completed",
        help="Move old completed nodes only when --apply is given",
    )
    arc.add_argument("--older-than-days", type=int, default=90)
    arc.add_argument("--archive-target", required=True)
    arc.add_argument("--apply", action="store_true")

    control = sub.add_parser(
        "control",
        help="Run explicit allowlisted RUN: actions from a Workflowy control node",
    )
    control.add_argument("--parent", required=True)
    control.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    control.add_argument("--timeout", type=float, default=120.0)

    roadmap = sub.add_parser(
        "roadmap-sync",
        help="Mirror the canonical codex roadmap into Workflowy and process running/PASS/FAIL children",
    )
    roadmap.add_argument("--parent", default="inbox")
    roadmap.add_argument("--repository", default="gernalix/codex-roadmap")
    roadmap.add_argument("--branch", default="main")
    roadmap.add_argument(
        "--roadmap-dir",
        type=Path,
        default=Path("~/projects/codex-roadmap"),
    )

    serve = sub.add_parser("serve", help="Run the localhost capture bridge")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    serve.add_argument(
        "--roadmap-dir",
        type=Path,
        default=Path("~/projects/codex-roadmap"),
    )
    return p


def run(args: argparse.Namespace) -> int:
    if args.command == "url":
        print(workflowy_url(args.node_id))
        return 0

    db = _db(args.cache)
    try:
        if args.command == "find":
            for row in search(db, args.query, limit=args.limit):
                print(
                    f"{row['id']}\t{row['name']}\t"
                    f"{workflowy_url(row['id'])}"
                )
            return 0

        if args.command == "dedupe":
            for key, rows in duplicate_groups(db, limit=args.limit):
                print(f"[{len(rows)}] {key}")
                for row in rows:
                    print(f"  {row['id']}  {row['name']}")
            return 0

        if args.command == "export-links":
            out = args.output.expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(node_links(db), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(out)
            return 0

        if args.command == "personalhub-links":
            table = _validate_identifier(args.table)
            name_col = _validate_identifier(args.name_column)
            url_col = _validate_identifier(args.url_column)
            path = args.database.expanduser()
            phdb = sqlite3.connect(path)
            try:
                cols = {
                    row[1]
                    for row in phdb.execute(f"PRAGMA table_info({table})")
                }
                if name_col not in cols:
                    raise ValueError(
                        f"Missing column {name_col} in {table}"
                    )
                if url_col not in cols:
                    if not args.create_column:
                        raise ValueError(
                            f"Missing column {url_col}; pass --create-column"
                        )
                    if args.apply:
                        phdb.execute(
                            f"ALTER TABLE {table} ADD COLUMN {url_col} TEXT"
                        )
                    cols.add(url_col)
                wf: dict[str, list[str]] = {}
                for row in db.execute(
                    "SELECT id,name FROM nodes WHERE TRIM(name)<>''"
                ):
                    wf.setdefault(
                        row["name"].strip().casefold(), []
                    ).append(row["id"])
                updates: list[tuple[str, int]] = []
                for rowid, name in phdb.execute(
                    f"SELECT rowid,{name_col} FROM {table} "
                    f"WHERE {name_col} IS NOT NULL"
                ):
                    ids = wf.get(str(name).strip().casefold(), [])
                    if len(ids) == 1:
                        updates.append((workflowy_url(ids[0]), rowid))
                print(f"unique_matches={len(updates)} apply={args.apply}")
                if args.apply:
                    phdb.executemany(
                        f"UPDATE {table} SET {url_col}=? WHERE rowid=?",
                        updates,
                    )
                    phdb.commit()
                return 0
            finally:
                phdb.close()

        with _client(args) as client:
            if args.command == "roadmap-sync":
                result = sync_roadmap(
                    client,
                    db,
                    parent=args.parent,
                    repository=args.repository,
                    branch=args.branch,
                    roadmap_dir=args.roadmap_dir,
                )
                print(json.dumps(result, sort_keys=True))
            elif args.command == "sync":
                print(f"cached={_sync(client, db)}")
            elif args.command == "add":
                print(
                    client.create_node(
                        args.parent,
                        args.text,
                        note=args.note,
                        position=args.position,
                    )
                )
            elif args.command in {"today", "inbox"}:
                print(
                    client.create_node(
                        args.command,
                        args.text,
                        note=args.note,
                        position="top",
                    )
                )
            elif args.command == "show":
                print(
                    json.dumps(
                        client.get_node(args.node),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            elif args.command == "move":
                client.move_node(args.node_id, args.parent)
                print("ok")
            elif args.command == "mirror":
                parent_id = client.resolve_target_id(args.parent)
                mirror_id, origin_id = client.mirror_node(
                    args.node_id, parent_id
                )
                print(
                    f"mirror_id={mirror_id} origin_id={origin_id}"
                )
            elif args.command == "unmirror":
                client.delete_mirror(args.node_id)
                print("ok")
            elif args.command == "done":
                client.complete_node(args.node_id)
                print("ok")
            elif args.command == "undone":
                client.uncomplete_node(args.node_id)
                print("ok")
            elif args.command == "targets":
                print(
                    json.dumps(
                        client.list_targets(),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            elif args.command == "backup":
                nodes = client.export_nodes()
                out = args.output.expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                if args.format == "json":
                    out.write_text(
                        json.dumps(
                            nodes, indent=2, ensure_ascii=False
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                else:
                    out.write_text(
                        _markdown_export(nodes), encoding="utf-8"
                    )
                refresh_cache(db, nodes)
                print(out)
            elif args.command == "route":
                node_id, decision = capture_routed(
                    client,
                    args.text,
                    load_rules(args.rules),
                    note=args.note,
                )
                print(
                    f"node_id={node_id} "
                    f"destination={decision.destination} "
                    f"rule={decision.rule}"
                )
            elif args.command == "weekly-review":
                _sync(client, db)
                text = weekly_summary(db, days=args.days)
                print(
                    client.create_node(
                        args.parent, text, position="top"
                    )
                )
            elif args.command == "resurface":
                _sync(client, db)
                candidates = old_nodes(
                    db,
                    older_than_days=args.older_than_days,
                    limit=args.limit,
                )
                if not candidates:
                    print("resurfaced=0")
                else:
                    mirror_parent = client.create_node(
                        args.parent, "Resurfaced", position="top"
                    )
                    for row in candidates:
                        client.mirror_node(
                            row["id"], mirror_parent, position="bottom"
                        )
                    print(f"resurfaced={len(candidates)}")
            elif args.command == "ingest":
                payload = json.loads(
                    args.json_file.expanduser().read_text(
                        encoding="utf-8"
                    )
                )
                if not isinstance(payload, dict):
                    raise ValueError(
                        "Event JSON must be an object"
                    )
                title = str(
                    payload.get("title")
                    or payload.get("summary")
                    or f"{args.source} event"
                )
                note = json.dumps(
                    payload, indent=2, ensure_ascii=False
                )
                node_id, decision = capture_routed(
                    client,
                    title,
                    load_rules(args.rules),
                    note=note,
                )
                external = (
                    str(payload.get("id"))
                    if payload.get("id") is not None
                    else None
                )
                inserted = record_event(
                    db,
                    source=args.source,
                    payload=payload,
                    node_id=node_id,
                    external_key=external,
                )
                if not inserted:
                    client.delete_node(node_id)
                    print("duplicate_event=true")
                else:
                    print(
                        f"node_id={node_id} "
                        f"destination={decision.destination}"
                    )
            elif args.command == "chatgpt":
                conv = load_conversation(
                    args.export,
                    conversation_id=args.conversation_id,
                    title=args.title,
                )
                md = conversation_to_markdown(conv)
                node_id = client.create_node(
                    args.parent, md, position="top"
                )
                print(f"node_id={node_id} {workflowy_url(node_id)}")
            elif args.command == "projects":
                root = args.root.expanduser().resolve()
                parent_id = client.create_node(
                    args.parent, "Projects index", position="top"
                )
                count = 0
                for repo in sorted(
                    p
                    for p in root.iterdir()
                    if p.is_dir() and (p / ".git").exists()
                ):
                    remote = _git_remote(repo)
                    note = f"Local path: {repo}"
                    if remote:
                        note += f"\nOrigin: {remote}"
                    client.create_node(
                        parent_id,
                        repo.name,
                        note=note,
                        position="bottom",
                    )
                    count += 1
                print(
                    f"projects={count} parent_id={parent_id}"
                )
            elif args.command == "normalize":
                _sync(client, db)
                cfg = load_rules(args.rules)
                normalizations = [
                    x
                    for x in cfg.get("normalizations", [])
                    if isinstance(x, dict)
                    and x.get("regex") is not None
                ]
                changed = 0
                for row in db.execute(
                    "SELECT id,name FROM nodes"
                ):
                    new = row["name"]
                    for rule in normalizations:
                        flags = (
                            re.IGNORECASE
                            if rule.get("ignore_case", True)
                            else 0
                        )
                        new = re.sub(
                            str(rule["regex"]),
                            str(rule.get("replacement", "")),
                            new,
                            flags=flags,
                        )
                    if new != row["name"]:
                        changed += 1
                        if args.apply:
                            client.update_node(row["id"], new)
                print(
                    f"changes={changed} apply={args.apply}"
                )
            elif args.command == "archive-completed":
                _sync(client, db)
                cutoff = (
                    int(time.time())
                    - args.older_than_days * 86400
                )
                rows = list(
                    db.execute(
                        "SELECT id FROM nodes "
                        "WHERE completed=1 "
                        "AND completed_at IS NOT NULL "
                        "AND completed_at<?",
                        (cutoff,),
                    )
                )
                print(
                    f"eligible={len(rows)} apply={args.apply}"
                )
                if args.apply:
                    for row in rows:
                        client.move_node(
                            row["id"],
                            args.archive_target,
                            position="bottom",
                        )
            elif args.command == "control":
                results = run_control_actions(
                    client,
                    parent=args.parent,
                    config=load_rules(args.rules),
                    timeout=args.timeout,
                )
                for result in results:
                    print(
                        f"{result.node_id}\t{result.action}\t"
                        f"exit={result.returncode}"
                    )
                print(f"processed={len(results)}")
            elif args.command == "serve":
                from .bridge import serve

                return serve(
                    client,
                    db,
                    host=args.host,
                    port=args.port,
                    rules_path=args.rules,
                    roadmap_dir=args.roadmap_dir,
                )
            else:
                raise ValueError(
                    f"Unknown command {args.command}"
                )
        return 0
    finally:
        db.close()


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except (
        CredentialError,
        WorkflowyAPIError,
        RuntimeError,
        ValueError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
