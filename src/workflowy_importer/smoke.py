from __future__ import annotations

import argparse
import contextlib
import io
import os
import tempfile
import uuid
from pathlib import Path

from .api import WorkflowyAPIError, WorkflowyClient
from .cli import _load_state, run


def _fixture(root: Path) -> None:
    (root / "A.md").write_text(
        "# Alpha\n"
        "## Work\n"
        "- Parent\n"
        "  - Child\n"
        "- [ ] open task\n"
        "- [x] done task\n"
        "> A quoted note.\n\n"
        "```python\n"
        "print(\"hello\")\n"
        "```\n\n"
        "See [[B#Beta|alias]] and [Alpha](A.md#Alpha).\n",
        encoding="utf-8",
    )
    (root / "B.md").write_text(
        "# Beta\n"
        "## Details\n"
        "Linked content.\n",
        encoding="utf-8",
    )


def _import_args(
    source: Path,
    state_file: Path,
    root_name: str,
    api_key_env: str,
    base_url: str,
    *,
    replace: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        source=source,
        parent="inbox",
        root_name=root_name,
        state_file=state_file,
        replace=replace,
        dry_run=False,
        no_resolve_links=False,
        api_key_env=api_key_env,
        base_url=base_url,
    )


def _quiet_run(args: argparse.Namespace) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return run(args)


def _collect_tree(client: WorkflowyClient, root_id: str) -> list[dict]:
    nodes = [client.get_node(root_id)]
    stack = [root_id]
    while stack:
        parent_id = stack.pop()
        children = client.list_nodes(parent_id)
        nodes.extend(children)
        stack.extend(
            str(node["id"])
            for node in children
            if node.get("id")
        )
    return nodes


def _known_roots(state: dict | None) -> set[str]:
    roots: set[str] = set()
    if not isinstance(state, dict):
        return roots

    root_id = state.get("root_id")
    if root_id:
        roots.add(str(root_id))

    for key in ("pending_build", "pending_replace"):
        pending = state.get(key)
        if not isinstance(pending, dict):
            continue
        for candidate in ("root_id", "old_root_id"):
            value = pending.get(candidate)
            if value:
                roots.add(str(value))
        rollback = pending.get("rollback_state")
        if isinstance(rollback, dict) and rollback.get("root_id"):
            roots.add(str(rollback["root_id"]))

    return roots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflowy-import-smoke",
        description=(
            "Run one destructive-but-self-cleaning smoke test under the Workflowy Inbox."
        ),
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


def run_smoke(api_key_env: str, base_url: str) -> int:
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing API key in {api_key_env}")

    cleanup_roots: set[str] = set()
    cleanup_error: Exception | None = None

    with tempfile.TemporaryDirectory(prefix="workflowy-importer-smoke-") as tmp:
        source = Path(tmp)
        state_file = source / "state.json"
        _fixture(source)
        root_name = f"workflowy-importer smoke {uuid.uuid4().hex[:10]}"
        base_args = _import_args(
            source,
            state_file,
            root_name,
            api_key_env,
            base_url,
        )

        try:
            _quiet_run(base_args)
            first_state = _load_state(state_file)
            if not first_state or not first_state.get("root_id"):
                raise RuntimeError("First import did not persist a root_id")
            first_root = str(first_state["root_id"])
            cleanup_roots.add(first_root)

            with WorkflowyClient(api_key=api_key, base_url=base_url) as client:
                tree = _collect_tree(client, first_root)

            completed_todo = any(
                node.get("completed") is True
                and isinstance(node.get("data"), dict)
                and node["data"].get("layoutMode") == "todo"
                for node in tree
            )
            internal_link = any(
                '<a href="https://workflowy.com/#/' in str(node.get("name", ""))
                for node in tree
            )
            if not completed_todo:
                raise RuntimeError("Completed todo was not preserved")
            if not internal_link:
                raise RuntimeError("Internal Markdown/wiki link was not converted")

            _quiet_run(base_args)
            second_state = _load_state(state_file)
            if not second_state or str(second_state.get("root_id")) != first_root:
                raise RuntimeError("Identical rerun was not a NOOP")

            with (source / "A.md").open("a", encoding="utf-8") as handle:
                handle.write("\nChanged for replace smoke.\n")

            try:
                _quiet_run(base_args)
            except RuntimeError as exc:
                if "Re-run with --replace" not in str(exc):
                    raise
            else:
                raise RuntimeError("Changed source was accepted without --replace")

            replace_args = _import_args(
                source,
                state_file,
                root_name,
                api_key_env,
                base_url,
                replace=True,
            )
            _quiet_run(replace_args)

            final_state = _load_state(state_file)
            if not final_state or not final_state.get("root_id"):
                raise RuntimeError("Replacement did not persist a root_id")
            final_root = str(final_state["root_id"])
            cleanup_roots.add(final_root)
            if final_root == first_root:
                raise RuntimeError("Replacement reused the old root unexpectedly")

            with WorkflowyClient(api_key=api_key, base_url=base_url) as client:
                if client.node_exists(first_root):
                    raise RuntimeError("Old root still exists after --replace")
                final_tree = _collect_tree(client, final_root)
                if not any(
                    "Changed for replace smoke." in str(node.get("name", ""))
                    for node in final_tree
                ):
                    raise RuntimeError("Replacement content was not imported")

        finally:
            state = _load_state(state_file)
            cleanup_roots.update(_known_roots(state))
            try:
                with WorkflowyClient(api_key=api_key, base_url=base_url) as client:
                    for root_id in cleanup_roots:
                        if client.node_exists(root_id):
                            client.delete_node(root_id)
                        if client.node_exists(root_id):
                            raise RuntimeError(
                                f"Smoke cleanup could not delete root {root_id}"
                            )
            except Exception as exc:
                cleanup_error = exc

        if cleanup_error:
            raise RuntimeError(f"Smoke cleanup failed: {cleanup_error}")

    print(
        "SMOKE=PASS "
        "checks=import,links,todo,noop,replace-guard,replace,cleanup"
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run_smoke(args.api_key_env, args.base_url)
    except (RuntimeError, WorkflowyAPIError, OSError) as exc:
        print(f"SMOKE=FAIL {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
