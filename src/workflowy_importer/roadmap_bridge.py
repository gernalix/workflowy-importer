from __future__ import annotations

import base64
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .api import WorkflowyAPIError, WorkflowyClient
from .fix_packets import load_fix_packet, packet_mutation
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
    ("ready", "Ready"),
    ("waiting", "Waiting"),
    ("running", "Running"),
    ("integration", "Integration"),
    ("blocked", "Needs fix"),
    ("completed", "Done"),
    ("unknown", "Archive"),
)
STATUS_GROUP_KEY = {
    "blocked": "blocked",
    "failed": "blocked",
    "completed": "completed",
    "cancelled": "unknown",
    "superseded": "unknown",
    "unknown": "unknown",
}
DEFAULT_REPO_TASK = Path.home() / "projects" / "github-autosync" / "repo_single_writer.py"
DEFAULT_CCS_URL = "http://127.0.0.1:43817"
LEGACY_GROUP_KEYS = ("pending", "failed", "cancelled", "superseded")


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
    content = str(payload.get("content") or "").replace("\n", "")
    if not content:
        blob_sha = payload.get("sha")
        if not isinstance(blob_sha, str) or not blob_sha:
            raise RuntimeError("remote_roadmap_db_invalid")
        payload = _gh_json("api", f"repos/{repository}/git/blobs/{blob_sha}")
        content = str(payload.get("content") or "").replace("\n", "")
    try:
        return base64.b64decode(content, validate=True)
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



def read_pipeline_status(repo_task: Path = DEFAULT_REPO_TASK) -> dict[str, dict]:
    if not repo_task.expanduser().is_file():
        return {}
    proc = subprocess.run(
        [sys.executable, str(repo_task.expanduser()), "status-all", "--roadmap-only"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    if proc.returncode:
        return {}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    rows = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("task_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("task_id")
    }


def read_ccs_bindings(base_url: str = DEFAULT_CCS_URL) -> dict[str, dict]:
    try:
        with urlopen(base_url.rstrip("/") + "/api/prompts", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    rows = payload.get("bindings") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("prompt_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("prompt_id")
    }


def _external_repo_task(prompt: RoadmapPrompt) -> bool:
    repo = str(prompt.repo or "").strip().lower().removesuffix(".git").rstrip("/")
    if not repo:
        return False
    repo = repo.removeprefix("https://github.com/")
    return repo != "gernalix/codex-roadmap"


def dashboard_group(
    prompt: RoadmapPrompt,
    prompt_by_id: dict[str, RoadmapPrompt],
    pipeline: dict | None,
) -> str:
    if prompt.status in {"blocked", "failed"}:
        return "blocked"
    if prompt.status == "completed":
        pipeline_state = str((pipeline or {}).get("pipeline_state") or "")
        if _external_repo_task(prompt) and pipeline_state not in {"", "done"}:
            return "blocked"
        return "completed"
    if prompt.status in {"cancelled", "superseded", "unknown"}:
        return "unknown"
    if prompt.status == "pending":
        unresolved = [
            dep for dep in prompt.dependencies
            if dep in prompt_by_id and prompt_by_id[dep].status != "completed"
        ]
        return "waiting" if unresolved else "ready"
    if prompt.status == "running":
        pipeline_state = str((pipeline or {}).get("pipeline_state") or "")
        if pipeline_state == "needs-fix":
            return "blocked"
        if pipeline_state in {"integration", "done"}:
            return "integration"
        return "running"
    return "unknown"


def _action_url(prompt_id: str, action: str) -> str:
    return f"http://127.0.0.1:43817/ui/prompt/{prompt_id}/{action}"


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
    pipeline: dict | None = None,
    binding: dict | None = None,
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
    lines.append(f"🚀 Apri: {_action_url(prompt.prompt_id, 'launch')}")
    lines.append(f"📋 Copia: {_action_url(prompt.prompt_id, 'copy')}")
    if binding and binding.get("context_id"):
        lines.append(f"🌐 ChatGPT: {_action_url(prompt.prompt_id, 'chrome')}")
    if binding and binding.get("codex_deep_link"):
        lines.append(f"🧠 Codex deep link: {binding['codex_deep_link']}")
        lines.append(f"↗ Apri Codex: {_action_url(prompt.prompt_id, 'codex')}")
    if prompt.current_path:
        lines.append(
            f"Sorgente audit: https://github.com/{repository}/blob/{branch}/{prompt.current_path}"
        )
    if pipeline:
        state = pipeline.get("integration_state") or pipeline.get("pipeline_state")
        if state:
            lines.append(f"Pipeline: {state}")
        if pipeline.get("pr_url"):
            lines.append(f"PR: {pipeline['pr_url']}")
        if pipeline.get("queue_position") and pipeline.get("queue_size"):
            lines.append(
                f"Coda integrazione: {pipeline['queue_position']}/{pipeline['queue_size']}"
            )
        pipeline_state = str(pipeline.get("pipeline_state") or "")
        if prompt.status == "completed" and pipeline_state not in {"", "done"}:
            lines.append(
                f"⚠ State mismatch: roadmap=completed · pipeline={pipeline_state}"
            )
        elif prompt.status == "running" and pipeline_state == "done":
            lines.append("Finalizzazione roadmap PASS in coda")
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
        "Override manuale d'emergenza: R=running · P=PASS · B=BLOCKED · F=FAIL."
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
    *,
    pipeline_state: str | None = None,
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

    if command == "PASS" and _external_repo_task(prompt) and pipeline_state != "done":
        raise ValueError(f"pass_requires_integrated_repo:{prompt.prompt_id}")

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
    fix_packet_loader: Callable[[str, str], dict | None] | None = None,
    pipeline_status: dict[str, dict] | None = None,
    ccs_bindings: dict[str, dict] | None = None,
) -> dict[str, int]:
    prompts = read_roadmap_db(
        raw_roadmap_db
        if raw_roadmap_db is not None
        else fetch_remote_roadmap_db(repository, branch)
    )
    prompt_by_id = {p.prompt_id: p for p in prompts}
    pipeline_status = pipeline_status if pipeline_status is not None else read_pipeline_status()
    ccs_bindings = ccs_bindings if ccs_bindings is not None else read_ccs_bindings()

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
    prompt_groups: dict[str, str] = {}
    for prompt in prompts:
        group = dashboard_group(
            prompt,
            prompt_by_id,
            pipeline_status.get(prompt.prompt_id),
        )
        prompt_groups[prompt.prompt_id] = group
        group_counts[group] += 1

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
            group_ids[prompt_groups[prompt.prompt_id]],
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
    fix_packets_submitted = 0
    submit = submitter or (
        lambda document, key: _submit_with_local_writer(
            roadmap_dir, document, key
        )
    )
    packet_loader = fix_packet_loader or load_fix_packet

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
            operations = mutation_for_command(
                prompt,
                command,
                pipeline_state=str(
                    (pipeline_status.get(prompt_id) or {}).get("pipeline_state") or ""
                ) or None,
            )
        except ValueError:
            warnings += 1
            # A prior writer pass may already have applied the matching B/F
            # transition. The packet is independent evidence and still needs
            # its one canonical publication on the next timer pass.
            if command not in {"BLOCKED", "FAIL"} or prompt.status != {
                "BLOCKED": "blocked",
                "FAIL": "failed",
            }[command]:
                continue
            operations = []
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
            packet = packet_loader(prompt_id, command)
            if packet:
                operation, request_key = packet_mutation(packet)
                already_sent = db.execute(
                    "SELECT 1 FROM events WHERE source=? AND external_key=?",
                    ("roadmap_fix_packet", request_key),
                ).fetchone()
                if not already_sent:
                    submit(
                        {
                            "schema": "codex-roadmap.mutation.v1",
                            "actor": "workflowy-fix-packet",
                            "operations": [operation],
                        },
                        request_key,
                    )
                    db.execute(
                        "INSERT INTO events(source,external_key,payload_json) VALUES(?,?,?)",
                        ("roadmap_fix_packet", request_key, json.dumps(packet, sort_keys=True)),
                    )
                    fix_packets_submitted += 1

    updated = 0
    moved = 0
    for prompt in prompts:
        node_id = node_ids[prompt.prompt_id]
        desired_parent = group_ids[prompt_groups[prompt.prompt_id]]
        desired_name = prompt_name(prompt)
        desired_note = prompt_note(
            prompt,
            node_ids,
            repository=repository,
            branch=branch,
            pipeline=pipeline_status.get(prompt.prompt_id),
            binding=ccs_bindings.get(prompt.prompt_id),
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
        "fix_packets_submitted": fix_packets_submitted,
        "warnings": warnings,
    }
