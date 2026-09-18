from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .api import WorkflowyAPIError, WorkflowyClient
from .markdown import LinkResolver, build_tree, count_links, preview_tree, render_inline
from .model import ImportNode


def _default_state_path(source: Path) -> Path:
    source = source.expanduser().resolve()
    if source.is_dir():
        return source / ".workflowy-importer-state.json"
    return source.with_name(f".{source.stem}.workflowy-importer-state.json")


def _load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read state file {path}: {exc}") from exc
    return value if isinstance(value, dict) else None


def _save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _create_subtree(
    client: WorkflowyClient,
    node: ImportNode,
    parent_id: str,
    ids: dict[str, str],
    initial_html: dict[str, str],
) -> None:
    rendered = render_inline(node.name)
    node_id = client.create_node(parent_id, rendered, node.layout)
    ids[node.key] = node_id
    initial_html[node.key] = rendered

    if node.completed:
        client.complete_node(node_id)

    for child in node.children:
        _create_subtree(client, child, node_id, ids, initial_html)


def _import(
    client: WorkflowyClient,
    root: ImportNode,
    parent: str,
    resolver: LinkResolver,
    resolve_links: bool,
) -> tuple[str, dict[str, str], int]:
    ids: dict[str, str] = {}
    initial_html: dict[str, str] = {}

    root_html = render_inline(root.name)
    root_id = client.create_node(parent, root_html, "bullets")
    ids[root.key] = root_id
    initial_html[root.key] = root_html

    for child in root.children:
        _create_subtree(client, child, root_id, ids, initial_html)

    updates = 0
    if resolve_links:
        for node in root.walk():
            final_html = render_inline(
                node.name,
                resolve_target=lambda target, n=node: resolver.href(
                    target, n.source_file, ids
                ),
            )
            if final_html != initial_html[node.key]:
                client.update_node(ids[node.key], final_html)
                updates += 1

    return root_id, ids, updates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflowy-import-md",
        description="Import a Markdown file or directory tree into Workflowy.",
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Markdown file or directory to import",
    )
    parser.add_argument(
        "--parent",
        default="None",
        help='Workflowy destination: node ID/URL/shortcut, "inbox", or "None" for top level',
    )
    parser.add_argument(
        "--root-name",
        help="Name of the single container node created for this import",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Override the local idempotency state file",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="If source changed, replace only the previously imported root tracked by the state file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and preview without API access or writes",
    )
    parser.add_argument(
        "--no-resolve-links",
        action="store_true",
        help="Keep [[wikilinks]] and local .md links as-is instead of linking imported Workflowy nodes",
    )
    parser.add_argument(
        "--api-key-env",
        default="WORKFLOWY_API_KEY",
        help="Environment variable containing the Workflowy API key",
    )
    parser.add_argument(
        "--base-url",
        default="https://workflowy.com/api/v1",
        help=argparse.SUPPRESS,
    )
    return parser


def run(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    root_name = args.root_name or (
        f"Imported Markdown — {source.stem if source.is_file() else source.name}"
    )

    parsed = build_tree(source, root_name)
    resolver = LinkResolver(parsed.root)
    resolved, unresolved = count_links(parsed.root, resolver)
    node_count = sum(1 for _ in parsed.root.walk())

    if args.dry_run:
        print(
            f"DRY RUN: files={len(parsed.files)} nodes={node_count} "
            f"links_resolvable={resolved} links_unresolved={unresolved}"
        )
        print(preview_tree(parsed.root))
        return 0

    api_key = os.getenv(args.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key. Set {args.api_key_env}; "
            "the key is never written to the state file."
        )

    state_path = (
        args.state_file or _default_state_path(source)
    ).expanduser().resolve()
    previous = _load_state(state_path)
    source_key = str(source)

    config_matches = bool(
        previous
        and previous.get("source") == source_key
        and previous.get("parent") == args.parent
        and previous.get("root_name") == root_name
    )
    same_fingerprint = bool(
        config_matches and previous.get("fingerprint") == parsed.fingerprint
    )

    with WorkflowyClient(api_key=api_key, base_url=args.base_url) as client:
        if same_fingerprint and previous and previous.get("root_id"):
            if client.node_exists(str(previous["root_id"])):
                print(
                    f"NOOP: existing import is current; files={len(parsed.files)} "
                    f"nodes={previous.get('node_count', node_count)} "
                    f"root_id={previous['root_id']}"
                )
                return 0

        if (
            previous
            and previous.get("root_id")
            and not same_fingerprint
            and not args.replace
        ):
            raise RuntimeError(
                f"Tracked import already exists in {state_path} but source/config changed. "
                "Re-run with --replace to replace that tracked import without creating duplicates."
            )

        new_root_id: str | None = None
        old_root_id = (
            str(previous["root_id"])
            if previous and previous.get("root_id")
            else None
        )

        try:
            new_root_id, ids, updates = _import(
                client=client,
                root=parsed.root,
                parent=args.parent,
                resolver=resolver,
                resolve_links=not args.no_resolve_links,
            )

            if old_root_id and args.replace and old_root_id != new_root_id:
                try:
                    client.delete_node(old_root_id)
                except Exception:
                    client.delete_node(new_root_id)
                    new_root_id = None
                    raise

            _save_state(
                state_path,
                {
                    "format_version": 1,
                    "source": source_key,
                    "parent": args.parent,
                    "root_name": root_name,
                    "fingerprint": parsed.fingerprint,
                    "root_id": new_root_id,
                    "node_count": len(ids),
                },
            )
        except Exception:
            if new_root_id:
                try:
                    client.delete_node(new_root_id)
                except Exception as cleanup_exc:
                    print(
                        f"WARNING: failed to clean partial import {new_root_id}: {cleanup_exc}",
                        file=sys.stderr,
                    )
            raise

    print(
        f"IMPORTED: files={len(parsed.files)} nodes={node_count} "
        f"link_updates={updates} links_unresolved={unresolved} "
        f"root_id={new_root_id}"
    )
    print(f"STATE: {state_path}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (RuntimeError, ValueError, WorkflowyAPIError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
