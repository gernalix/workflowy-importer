"""Workflowy bridge command-line grammar, independent from command execution.

All defaults and argument validation match the original CLI contract; no API,
cache or workflow side effects occur while constructing the parser.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .chatgpt_live import DEFAULT_CDP_ENDPOINT, DEFAULT_INVENTORY
from .credentials import DEFAULT_SECRET_FILE


DEFAULT_CACHE = Path("~/.local/share/workflowy-bridge/cache.sqlite3").expanduser()


DEFAULT_RULES = Path("~/.config/workflowy-bridge/rules.json").expanduser()


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
    ensure_mirror = sub.add_parser(
        "ensure-mirror",
        help="Ensure exactly one locally tracked mirror under a parent",
    )
    ensure_mirror.add_argument("node_id")
    ensure_mirror.add_argument("parent")
    daily_mirror = sub.add_parser(
        "ensure-daily-mirror",
        help="Backfill and maintain a mirror in Workflowy daily notes",
    )
    daily_mirror.add_argument("node_id")
    daily_mirror.add_argument("--since", required=True, type=date.fromisoformat)
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

    live = sub.add_parser(
        "chatgpt-live-sync",
        help="Track account-wide ChatGPT chats and project them into Workflowy",
    )
    live.add_argument("--parent", default="inbox")
    live.add_argument("--cdp-endpoint", default=DEFAULT_CDP_ENDPOINT)
    live.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    live.add_argument("--recent-minutes", type=float, default=15.0)
    live.add_argument("--running-ttl-minutes", type=float, default=5.0)
    live.add_argument("--full-sweep-hours", type=float, default=24.0)
    live.add_argument("--detail-limit", type=int, default=2)
    live.add_argument("--max-writes", type=int, default=20)
    live.add_argument("--timezone", default="Europe/Copenhagen")

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

    serve = sub.add_parser("serve", help="Run the localhost capture bridge")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    return p
