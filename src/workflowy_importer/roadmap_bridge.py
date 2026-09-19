from __future__ import annotations

import base64
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .api import WorkflowyAPIError, WorkflowyClient
from .links import workflowy_url

ROADMAP_NAMESPACE = "codex-roadmap"
ROADMAP_ROOT_KEY = "__root__"
GROUP_PREFIX = "__group__:"
COMMANDS = {
    "R": "running",
    "running": "running",
    "P": "PASS",
    "PASS": "PASS",
    "B": "BLOCKED",
    "BLOCKED": "BLOCKED",
    "F": "FAIL",
    "FAIL": "FAIL",
}
DASHBOARD_GROUPS = (
    ("pending", "Ready"),
    ("running", "Running"),
    ("blocked", "Needs fix"),
    ("completed", "Done"),
    ("unknown", "Archive"),
)
STATUS_GROUP_KEY = {
    "pending": "pending",
    "running": "running",
    "blocked": "blocked",
    "failed": "blocked",
    "completed": "completed",
    "cancelled": "unknown",
    "superseded": "unknown",
    "unknown": "unknown",
}
LEGACY_GROUP_KEYS = ("failed", "cancelled", "superseded")


@dataclass(slots=True)
class RoadmapPrompt:
    prompt_id: str
    title: str
    status: str
    project_name: str | None
    repo: str | None
    current_path: str
    explanation: str
    model: str | None
    reasoning: str | None
    queue_position: int | None
    dependencies: list[str]
    dependents: list[str]
    relations_out: list[tuple[str, str]]
    relations_in: list[tuple[str, str]]


def _gh_json(*args: str) -> dict:
    try:
        proc = subprocess.run(
            ["gh", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gh_cli_missing") from exc
    if proc.returncode:
        raise RuntimeError(f"gh_failed:{proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid_gh_response") from exc
    if not isinstance(data, dict):
        raise RuntimeError("invalid_gh_response")
    return data


def fetch_remote_roadmap_db(
    repository: str = "gernalix/codex-roadmap",
    branch: str = "main",
) -> bytes:
    payload = _gh_json(
        "api",
        f"repos/{repository}/contents/roadmap.sqlite?ref={branch}",
    )
    try:
        return base64.b64decode(
            str(payload["content"]).replace("\n", ""),
            validate=True,
        )
    except (KeyError, ValueError) as exc:
        raise RuntimeError("remote_roadmap_db_invalid") from exc


def read_roadmap_db(raw: bytes) -> list[RoadmapPrompt]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
        handle.write(raw)
        handle.flush()
        conn = sqlite3.connect(f"file:{handle.name}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = list(
                conn.execute(
                    """SELECT prompt_id,title,status,project_name,repo,current_path,
                              explanation,model,reasoning,queue_position
                       FROM prompts
                       ORDER BY
                         CASE status
                           WHEN 'pending' THEN 0
                           WHEN 'running' THEN 1
                           WHEN 'failed' THEN 2
                           WHEN 'blocked' THEN 3
                           WHEN 'unknown' THEN 4
                           ELSE 5
                         END,
                         COALESCE(queue_position,2147483647),
                         created_at,prompt_id"""
                )
            )
            deps: dict[str, list[str]] = {}
            dependents: dict[str, list[str]] = {}
            for row in conn.execute(
                "SELECT prompt_id,depends_on_prompt_id FROM dependencies"
            ):
                deps.setdefault(row["prompt_id"], []).append(
                    row["depends_on_prompt_id"]
                )
                dependents.setdefault(row["depends_on_prompt_id"], []).append(
                    row["prompt_id"]
                )
            rel_out: dict[str, list[tuple[str, str]]] = {}
            rel_in: dict[str, list[tuple[str, str]]] = {}
            for row in conn.execute(
                "SELECT from_prompt_id,to_prompt_id,relation_type FROM prompt_relations"
            ):
                rel_out.setdefault(row["from_prompt_id"], []).append(
                    (row["relation_type"], row["to_prompt_id"])
                )
                rel_in.setdefault(row["to_prompt_id"], []).append(
                    (row["relation_type"], row["from_prompt_id"])
                )
        finally:
            conn.close()

    return [
        RoadmapPrompt(
            prompt_id=str(row["prompt_id"]),
            title=str(row["title"]),
            status=str(row["status"]),
            project_name=row["project_name"],
            repo=row["repo"],
            current_path=str(row["current_path"]),
            explanation=str(row["explanation"] or ""),
            model=row["model"],
            reasoning=row["reasoning"],
            queue_position=row["queue_position"],
            dependencies=sorted(deps.get(row["prompt_id"], [])),
            dependents=sorted(dependents.get(row["prompt_id"], [])),
            relations_out=sorted(rel_out.get(row["prompt_id"], [])),
            relations_in=sorted(rel_in.get(row["prompt_id"], [])),
        )
        for row in rows
    ]


def _tag(value: str, prefix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return f"#{prefix}_{cleaned}" if cleaned else ""


def prompt_name(prompt: RoadmapPrompt) -> str:
    return f"[{prompt.prompt_id}] {prompt.title}"


def _mapped_link(prompt_id: str, node_ids: dict[str, str]) -> str:
    node_id = node_ids.get(prompt_id)
    if not node_id:
        return prompt_id
    return f"{prompt_id} {workflowy_url(node_id)}"


def prompt_note(
    prompt: RoadmapPrompt,
    node_ids: dict[str, str],
    *,
    repository: str,
    branch: str,
) -> str:
    lines = [
        f"PROMPT_ID: {prompt.prompt_id}",
        f"Stato canonico: {prompt.status}",
    ]
    if prompt.project_name:
        lines.append(f"Progetto: {prompt.project_name}")
    if prompt.model or prompt.reasoning:
        lines.append(
            "Modello: "
            + " / ".join(x for x in (prompt.model, prompt.reasoning) if x)
        )
    if prompt.explanation:
        lines.append(f"Spiegazione: {prompt.explanation}")
    if prompt.current_path:
        lines.append(
            f"Prompt: https://github.com/{repository}/blob/{branch}/{prompt.current_path}"
        )
    if prompt.dependencies:
        lines.append(
            "Dipende da: "
            + " · ".join(_mapped_link(x, node_ids) for x in prompt.dependencies)
        )
    if prompt.dependents:
        lines.append(
            "Sblocca: "
            + " · ".join(_mapped_link(x, node_ids) for x in prompt.dependents)
        )
    if prompt.relations_out:
        lines.append(
            "Relazioni →: "
            + " · ".join(
                f"{kind}:{_mapped_link(pid, node_ids)}"
                for kind, pid in prompt.relations_out
            )
        )
    if prompt.relations_in:
        lines.append(
            "Relazioni ←: "
            + " · ".join(
                f"{kind}:{_mapped_link(pid, node_ids)}"
                for kind, pid in prompt.relations_in
            )
        )
    lines.append(
        "Stato manuale: R=running · P=PASS · B=BLOCKED · F=FAIL."
    )
    tags = " ".join(
        tag
        for tag in (
            "#roadmap",
            _tag(prompt.status, "status"),
            _tag(prompt.project_name or "unknown", "project"),
        )
        if tag
    )
    if tags:
        lines.append(tags)
    return "\n".join(lines)


def command_from_children(children: list[dict]) -> tuple[str, dict] | None:
    matches: list[tuple[str, dict]] = []
    for node in children:
        name = str(node.get("name") or "")
        command = COMMANDS.get(name)
        if command:
            matches.append((command, node))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("ambiguous_roadmap_status_children")
    return matches[0]


def mutation_for_command(
    prompt: RoadmapPrompt,
    command: str,
) -> list[dict]:
    status = prompt.status
    if command == "running":
        if status == "running":
            return []
        if status != "pending":
            raise ValueError(
                f"cannot_mark_running:{prompt.prompt_id}:{status}"
            )
        return [
            {
                "op": "status",
                "prompt_id": prompt.prompt_id,
                "status": "running",
                "actor": "workflowy",
                "note": "workflowy:explicit-running",
            }
        ]

    targets = {
        "PASS": "completed",
        "BLOCKED": "blocked",
        "FAIL": "failed",
    }
    try:
        target = targets[command]
    except KeyError as exc:
        raise ValueError(f"unknown_roadmap_command:{command}") from exc
    if status == target:
        return []
    if status not in {"pending", "running"}:
        raise ValueError(
            f"cannot_apply_{command.lower()}:{prompt.prompt_id}:{status}"
        )

    operations: list[dict] = []
    if status == "pending":
        operations.append(
            {
                "op": "status",
                "prompt_id": prompt.prompt_id,
                "status": "running",
                "actor": "workflowy",
                "note": "workflowy:implicit-running-before-terminal",
            }
        )
    operations.append(
        {
            "op": "terminal_request",
            "prompt_id": prompt.prompt_id,
            "status": target,
            "actor": "workflowy",
            "note": f"workflowy:explicit-{command.lower()}",
        }
    )
    return operations


def _mapping_get(db: sqlite3.Connection, key: str) -> tuple[str, dict] | None:
    row = db.execute(
        "SELECT node_id,metadata_json FROM mappings WHERE namespace=? AND external_key=?",
        (ROADMAP_NAMESPACE, key),
    ).fetchone()
    if not row:
        return None
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return str(row["node_id"]), metadata if isinstance(metadata, dict) else {}


def _mapping_set(
    db: sqlite3.Connection,
    key: str,
    node_id: str,
    metadata: dict | None = None,
) -> None:
    db.execute(
        """INSERT INTO mappings(namespace,external_key,node_id,metadata_json)
           VALUES(?,?,?,?)
           ON CONFLICT(namespace,external_key) DO UPDATE SET
             node_id=excluded.node_id,
             metadata_json=excluded.metadata_json""",
        (
            ROADMAP_NAMESPACE,
            key,
            node_id,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _ensure_mapped_node(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    existing_ids: set[str],
    *,
    key: str,
    parent_id: str,
    name: str,
    note: str | None = None,
) -> tuple[str, dict]:
    mapped = _mapping_get(db, key)
    if mapped and mapped[0] in existing_ids:
        return mapped
    node_id = client.create_node(
        parent_id,
        name,
        note=note,
        position="bottom",
    )
    _mapping_set(db, key, node_id, {})
    existing_ids.add(node_id)
    return node_id, {}


def _submit_with_local_writer(
    roadmap_dir: Path,
    document: dict,
    request_key: str,
) -> dict:
    submitter = roadmap_dir.expanduser() / "tools" / "submit_mutation.py"
    if not submitter.is_file():
        raise RuntimeError(f"roadmap_submitter_missing:{submitter}")
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        encoding="utf-8",
        delete=False,
    ) as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        path = Path(handle.name)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(submitter),
                "--file",
                str(path),
                "--request-key",
                request_key,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode:
        raise RuntimeError(
            f"roadmap_mutation_submit_failed:{proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("roadmap_mutation_submit_invalid_response") from exc
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise RuntimeError(f"roadmap_mutation_submit_rejected:{result}")
    return result


def _fix_prompt_text(prompt_id: str, outcome: str) -> str:
    return f"FIX {outcome[0]} · {prompt_id} #needs_fix"


def sync_roadmap(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    *,
    parent: str = "inbox",
    repository: str = "gernalix/codex-roadmap",
    branch: str = "main",
    roadmap_dir: Path = Path("~/projects/codex-roadmap"),
    raw_roadmap_db: bytes | None = None,
    submitter: Callable[[dict, str], dict] | None = None,
) -> dict[str, int]:
    prompts = read_roadmap_db(
        raw_roadmap_db
        if raw_roadmap_db is not None
        else fetch_remote_roadmap_db(repository, branch)
    )
    prompt_by_id = {p.prompt_id: p for p in prompts}

    exported = client.export_nodes()
    by_id = {
        str(node["id"]): node
        for node in exported
        if isinstance(node, dict) and node.get("id")
    }
    existing_ids = set(by_id)
    children_by_parent: dict[str, list[dict]] = {}
    for node in exported:
        parent_id = node.get("parent_id")
        if parent_id:
            children_by_parent.setdefault(str(parent_id), []).append(node)

    root_id, _ = _ensure_mapped_node(
        client,
        db,
        existing_ids,
        key=ROADMAP_ROOT_KEY,
        parent_id=parent,
        name="Codex",
        note="Dashboard operativa della roadmap Codex. roadmap.sqlite resta la fonte canonica.",
    )
    current_root = by_id.get(root_id)
    if current_root:
        if str(current_root.get("name") or "") != "Codex":
            client.update_node(
                root_id,
                "Codex",
                note="Dashboard operativa della roadmap Codex. roadmap.sqlite resta la fonte canonica.",
            )
        elif str(current_root.get("note") or "") != (
            "Dashboard operativa della roadmap Codex. roadmap.sqlite resta la fonte canonica."
        ):
            client.update_node(
                root_id,
                "Codex",
                note="Dashboard operativa della roadmap Codex. roadmap.sqlite resta la fonte canonica.",
            )

    group_counts = {key: 0 for key, _ in DASHBOARD_GROUPS}
    for prompt in prompts:
        group_counts[STATUS_GROUP_KEY.get(prompt.status, "unknown")] += 1

    group_ids: dict[str, str] = {}
    for key, label in DASHBOARD_GROUPS:
        desired_name = f"{label} ({group_counts[key]})"
        group_id, _ = _ensure_mapped_node(
            client,
            db,
            existing_ids,
            key=GROUP_PREFIX + key,
            parent_id=root_id,
            name=desired_name,
        )
        group_ids[key] = group_id
        current_group = by_id.get(group_id)
        if current_group:
            if str(current_group.get("name") or "") != desired_name:
                client.update_node(group_id, desired_name)
            if str(current_group.get("parent_id") or "") != root_id:
                client.move_node(group_id, root_id, position="bottom")

    archive_id = group_ids["unknown"]
    for legacy_key in LEGACY_GROUP_KEYS:
        mapped = _mapping_get(db, GROUP_PREFIX + legacy_key)
        if not mapped or mapped[0] not in existing_ids:
            continue
        legacy_id = mapped[0]
        if legacy_id in group_ids.values():
            continue
        current_group = by_id.get(legacy_id)
        if current_group:
            desired_name = f"Legacy {legacy_key}"
            if str(current_group.get("name") or "") != desired_name:
                client.update_node(legacy_id, desired_name)
            if str(current_group.get("parent_id") or "") != archive_id:
                client.move_node(legacy_id, archive_id, position="bottom")

    node_ids: dict[str, str] = {}
    created = 0
    for prompt in prompts:
        mapped = _mapping_get(db, prompt.prompt_id)
        if mapped and mapped[0] in existing_ids:
            node_ids[prompt.prompt_id] = mapped[0]
            continue
        node_id = client.create_node(
            group_ids[STATUS_GROUP_KEY.get(prompt.status, "unknown")],
            prompt_name(prompt),
            position="bottom",
        )
        _mapping_set(db, prompt.prompt_id, node_id, {})
        node_ids[prompt.prompt_id] = node_id
        existing_ids.add(node_id)
        created += 1

    submitted = 0
    warnings = 0
    fix_prompts = 0
    submit = submitter or (
        lambda document, key: _submit_with_local_writer(
            roadmap_dir, document, key
        )
    )

    for prompt_id, node_id in node_ids.items():
        prompt = prompt_by_id[prompt_id]
        children = children_by_parent.get(node_id, [])
        try:
            found = command_from_children(children)
        except ValueError:
            warnings += 1
            continue
        if not found:
            continue
        command, command_node = found
        try:
            operations = mutation_for_command(prompt, command)
        except ValueError:
            warnings += 1
            continue
        if operations:
            request_key = (
                f"workflowy-{prompt_id}-{command.lower()}-"
                f"{str(command_node.get('id') or 'node')}"
            )
            submit(
                {
                    "schema": "codex-roadmap.mutation.v1",
                    "actor": "workflowy",
                    "operations": operations,
                },
                request_key,
            )
            submitted += 1
        if command in {"BLOCKED", "FAIL"}:
            wanted = _fix_prompt_text(prompt_id, command)
            if not any(
                str(child.get("name") or "").strip() == wanted
                for child in children
            ):
                client.create_node(node_id, wanted, position="top")
                fix_prompts += 1

    updated = 0
    moved = 0
    for prompt in prompts:
        node_id = node_ids[prompt.prompt_id]
        desired_parent = group_ids[STATUS_GROUP_KEY.get(prompt.status, "unknown")]
        desired_name = prompt_name(prompt)
        desired_note = prompt_note(
            prompt,
            node_ids,
            repository=repository,
            branch=branch,
        )
        current = by_id.get(node_id)
        if current:
            current_name = str(current.get("name") or "")
            current_note = str(current.get("note") or "")
            if current_name != desired_name or current_note != desired_note:
                client.update_node(
                    node_id,
                    desired_name,
                    note=desired_note,
                )
                updated += 1
            if str(current.get("parent_id") or "") != desired_parent:
                client.move_node(node_id, desired_parent, position="bottom")
                moved += 1
        else:
            client.update_node(node_id, desired_name, note=desired_note)
            updated += 1

    db.commit()
    return {
        "prompts": len(prompts),
        "created": created,
        "updated": updated,
        "moved": moved,
        "mutations_submitted": submitted,
        "fix_prompts_created": fix_prompts,
        "warnings": warnings,
    }
