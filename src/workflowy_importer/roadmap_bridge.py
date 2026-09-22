from __future__ import annotations

import base64
import html
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .api import WorkflowyAPIError, WorkflowyClient
from .fix_packets import load_fix_packet, load_latest_terminal_outcome, packet_mutation
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
PROMPT_NODE_RE = re.compile(r"^\[(\d{6})\]\s")


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
    manual_prerequisites: list[str] = field(default_factory=list)
    last_outcome: str | None = None
    fix_packet: dict | None = None


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
            manual_prerequisites: dict[str, list[str]] = {}
            try:
                for row in conn.execute(
                    """SELECT prompt_id,tag
                       FROM prompt_tags
                       WHERE tag LIKE 'manual-prerequisite:%'
                       ORDER BY prompt_id,tag"""
                ):
                    tag = str(row["tag"])
                    manual_prerequisites.setdefault(str(row["prompt_id"]), []).append(
                        tag.split(":", 1)[1]
                    )
            except sqlite3.OperationalError:
                pass

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

            last_outcomes: dict[str, str] = {}
            try:
                for row in conn.execute(
                    """SELECT prompt_id,outcome
                       FROM executions
                       WHERE outcome IS NOT NULL
                       ORDER BY COALESCE(ended_at,started_at,recorded_at) DESC,
                                execution_id DESC"""
                ):
                    prompt_id = str(row["prompt_id"])
                    last_outcomes.setdefault(prompt_id, str(row["outcome"]))
            except sqlite3.OperationalError:
                pass

            fix_packets: dict[str, dict] = {}
            try:
                for row in conn.execute(
                    """SELECT prompt_id,summary
                       FROM analyses
                       WHERE source_ref LIKE 'codex-usage:%'
                       ORDER BY analyzed_at DESC,analysis_id DESC"""
                ):
                    prompt_id = str(row["prompt_id"])
                    if prompt_id in fix_packets:
                        continue
                    try:
                        packet = json.loads(str(row["summary"] or ""))
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(packet, dict)
                        and str(packet.get("prompt_id") or "") == prompt_id
                    ):
                        fix_packets[prompt_id] = packet
            except sqlite3.OperationalError:
                pass
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
            manual_prerequisites=sorted(
                manual_prerequisites.get(str(row["prompt_id"]), [])
            ),
            last_outcome=last_outcomes.get(str(row["prompt_id"])),
            fix_packet=fix_packets.get(str(row["prompt_id"])),
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
        return "waiting" if unresolved or prompt.manual_prerequisites else "ready"
    if prompt.status == "running":
        outcome = str(prompt.last_outcome or "").upper()
        if outcome in {"BLOCKED", "FAIL"}:
            return "blocked"
        pipeline_state = str((pipeline or {}).get("pipeline_state") or "")
        if pipeline_state == "needs-fix":
            return "blocked"
        if pipeline_state in {"integration", "done"}:
            return "integration"
        return "running"
    return "unknown"


def _action_url(prompt_id: str, action: str) -> str:
    return f"http://127.0.0.1:43817/ui/prompt/{prompt_id}/{action}"


def _text(value: object) -> str:
    """Escape HTML markup in visible text without encoding quotes/apostrophes."""
    return html.escape(str(value), quote=False)


def _link(label: str, url: str) -> str:
    return (
        f'<a href="{html.escape(str(url), quote=True)}">'
        f"{_text(label)}</a>"
    )


def _tag(value: str, prefix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return f"#{prefix}_{cleaned}" if cleaned else ""


DASHBOARD_MARKERS = {
    "ready": "🟢",
    "waiting": "🟡",
    "running": "🔵",
    "integration": "🟣",
    "blocked": "🔴",
    "completed": "✅",
    "unknown": "⚪",
}


def prompt_name(prompt: RoadmapPrompt, group: str | None = None) -> str:
    marker = DASHBOARD_MARKERS.get(group or "", "")
    marker_text = f"{marker} " if marker else ""
    return (
        f"[{prompt.prompt_id}] {marker_text}"
        f"<b>{_text(prompt.title)}</b>"
    )


def _human_text(value: object, *, limit: int = 280) -> str:
    text = " ".join(str(value or "").replace("\\_", "_").split()).strip()
    text = re.sub(
        r"^(?:BLOCKER|ERROR|NEXT_ACTION|RESULT)\s*[:=]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if re.fullmatch(r"[A-Za-z0-9_.:/-]+", text or ""):
        text = text.replace("_", " ")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _talking_lines(
    prompt: RoadmapPrompt,
    *,
    group: str,
    pipeline: dict | None,
    binding: dict | None,
    fix_packet: dict | None,
) -> list[str]:
    pipeline = pipeline or {}
    packet = fix_packet or prompt.fix_packet
    pipeline_state = str(pipeline.get("pipeline_state") or "")
    outcome = str(prompt.last_outcome or "").upper()

    lines: list[str] = []
    problem = (
        prompt.status in {"blocked", "failed"}
        or (
            prompt.status == "running"
            and outcome in {"BLOCKED", "FAIL", "CANCELLED", "UNKNOWN"}
        )
        or pipeline_state == "needs-fix"
    )

    if (
        prompt.status == "completed"
        and _external_repo_task(prompt)
        and pipeline_state not in {"", "done"}
    ):
        lines.append("🟠 PASS Codex, integrazione repository ancora aperta.")
    elif problem:
        if prompt.status == "failed" or outcome == "FAIL":
            lines.append("🔴 FAIL · Codex non ha completato il lavoro.")
        elif outcome == "CANCELLED":
            lines.append("🟠 Interrotto prima del PASS.")
        else:
            lines.append("🔴 BLOCKED · Codex si è fermato prima del PASS.")

        if isinstance(packet, dict):
            blocker = _human_text(packet.get("blocker"))
            next_action = _human_text(packet.get("next_action"))
            if blocker:
                lines.append(f"<b>Blocco</b>: {_text(blocker)}")
            if next_action:
                lines.append(f"<b>Prossimo passo</b>: {_text(next_action)}")
        else:
            lines.append("<b>Blocco</b>: causa non ancora disponibile.")
    elif group == "integration":
        lines.append("🟣 Codex completato · integrazione/CI in corso.")
    elif group == "running":
        lines.append("🔵 In esecuzione.")
    elif group == "waiting":
        reasons = [str(dep) for dep in prompt.dependencies]
        reasons.extend(
            f"prerequisito: {value.replace('-', ' ')}"
            for value in prompt.manual_prerequisites
        )
        suffix = " · ".join(reasons[:3])
        if len(reasons) > 3:
            suffix += " · …"
        lines.append(
            "🟡 In attesa"
            + (f" di {_text(suffix)}." if suffix else ".")
        )
    elif group == "ready":
        lines.append("🟢 Pronto all'avvio.")
    elif group == "completed":
        lines.append("✅ PASS · chiuso.")
    else:
        lines.append("⚪ Stato non operativo: verifica la fonte canonica.")

    return lines


def _mapped_link(prompt_id: str, node_ids: dict[str, str]) -> str:
    node_id = node_ids.get(prompt_id)
    if not node_id:
        return _text(prompt_id)
    return _link(prompt_id, workflowy_url(node_id))


def prompt_note(
    prompt: RoadmapPrompt,
    node_ids: dict[str, str],
    *,
    repository: str,
    branch: str,
    pipeline: dict | None = None,
    binding: dict | None = None,
    group: str | None = None,
    fix_packet: dict | None = None,
) -> str:
    effective_group = group or dashboard_group(
        prompt, {prompt.prompt_id: prompt}, pipeline
    )
    lines = _talking_lines(
        prompt,
        group=effective_group,
        pipeline=pipeline,
        binding=binding,
        fix_packet=fix_packet,
    )

    lines.append("")
    if prompt.explanation:
        lines.append(
            f"💡 <b>In parole semplici</b>: {_text(prompt.explanation)}"
        )
    else:
        lines.append("💡 <b>In parole semplici</b>: spiegazione non ancora disponibile.")

    action_links = " · ".join(
        (
            _link("🚀 Avvia", _action_url(prompt.prompt_id, "launch")),
            _link("📋 Copia prompt", _action_url(prompt.prompt_id, "copy")),
            _link("🔎 Verifica", _action_url(prompt.prompt_id, "verify")),
        )
    )
    lines.extend(["", f"<b>Azioni</b>: {action_links}"])

    binding = binding or {}
    has_chrome = bool(binding.get("context_id") and binding.get("url"))
    has_codex = bool(
        binding.get("codex_thread") and binding.get("codex_deep_link")
    )
    chrome_link = (
        _link("Apri Chrome", _action_url(prompt.prompt_id, "chrome"))
        if has_chrome
        else _link("Associa Chrome", _action_url(prompt.prompt_id, "bind-chrome"))
    )
    codex_link = (
        _link("Apri Codex", _action_url(prompt.prompt_id, "codex"))
        if has_codex
        else _link("Associa Codex", _action_url(prompt.prompt_id, "bind-codex"))
    )
    lines.append(
        "<b>Collegamenti</b>: "
        + ("🌐 Chrome ✅ " if has_chrome else "🌐 Chrome ❌ ")
        + chrome_link
        + " · "
        + ("🧠 Codex ✅ " if has_codex else "🧠 Codex ❌ ")
        + codex_link
    )

    details = [
        f"ID {_text(prompt.prompt_id)}",
        f"stato {_text(prompt.status)}",
    ]
    if prompt.project_name:
        details.append(f"progetto {_text(prompt.project_name)}")
    if prompt.model or prompt.reasoning:
        model = " / ".join(
            _text(x)
            for x in (prompt.model, prompt.reasoning)
            if x
        )
        details.append(f"modello {model}")
    lines.append("<b>Dettagli</b>: " + " · ".join(details))

    outcome = str(prompt.last_outcome or "").upper()
    if prompt.status == "running" and outcome in {"BLOCKED", "FAIL"}:
        lines.append(
            f"<b>Esito operativo</b>: {_text(outcome)} · "
            "finalizzazione canonica in attesa"
        )
    if prompt.current_path:
        source_url = (
            f"https://github.com/{repository}/blob/{branch}/{prompt.current_path}"
        )
        lines.append(f"<b>Sorgente</b>: {_link('GitHub', source_url)}")

    if pipeline:
        pipeline_bits: list[str] = []
        state = pipeline.get("integration_state") or pipeline.get("pipeline_state")
        if state:
            pipeline_bits.append(_text(state))
        if pipeline.get("pr_url"):
            pipeline_bits.append(_link("PR", str(pipeline["pr_url"])))
        if pipeline.get("queue_position") and pipeline.get("queue_size"):
            pipeline_bits.append(
                "coda "
                f"{_text(pipeline['queue_position'])}/"
                f"{_text(pipeline['queue_size'])}"
            )
        if pipeline_bits:
            lines.append("<b>Pipeline</b>: " + " · ".join(pipeline_bits))

        pipeline_state = str(pipeline.get("pipeline_state") or "")
        if prompt.status == "completed" and pipeline_state not in {"", "done"}:
            lines.append(
                "⚠ <b>State mismatch</b>: roadmap=completed · "
                f"pipeline={_text(pipeline_state)}"
            )
        elif prompt.status == "running" and pipeline_state == "done":
            lines.append("Finalizzazione roadmap PASS in coda.")

    if prompt.dependencies:
        lines.append(
            "<b>Dipende da</b>: "
            + " · ".join(_mapped_link(x, node_ids) for x in prompt.dependencies)
        )
    if prompt.dependents:
        lines.append(
            "<b>Sblocca</b>: "
            + " · ".join(_mapped_link(x, node_ids) for x in prompt.dependents)
        )
    if prompt.relations_out:
        lines.append(
            "<b>Relazioni →</b>: "
            + " · ".join(
                f"{_text(kind)}:{_mapped_link(pid, node_ids)}"
                for kind, pid in prompt.relations_out
            )
        )
    if prompt.relations_in:
        lines.append(
            "<b>Relazioni ←</b>: "
            + " · ".join(
                f"{_text(kind)}:{_mapped_link(pid, node_ids)}"
                for kind, pid in prompt.relations_in
            )
        )

    lines.append(
        "<b>Esito manuale</b>: R=running · P=PASS · B=BLOCKED · F=FAIL"
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


def _prompt_id_from_name(name: object) -> str | None:
    match = PROMPT_NODE_RE.match(str(name or ""))
    return match.group(1) if match else None


def _group_name_matches(name: object, label: str) -> bool:
    return bool(
        re.fullmatch(
            rf"{re.escape(label)}(?: \(\d+\))?",
            str(name or "").strip(),
        )
    )


def _hydrate_mapped_nodes(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    by_id: dict[str, dict],
    keys: list[str],
) -> None:
    """Verify mapped nodes directly before treating an export miss as deletion."""
    for key in keys:
        mapped = _mapping_get(db, key)
        if not mapped:
            continue
        node_id = mapped[0]
        if node_id in by_id:
            continue
        try:
            node = client.get_node(node_id)
        except WorkflowyAPIError as exc:
            if exc.status_code == 404:
                continue
            raise
        if isinstance(node, dict) and node.get("id"):
            by_id[node_id] = node


def _reconcile_prompt_duplicates(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    by_id: dict[str, dict],
    existing_ids: set[str],
    children_by_parent: dict[str, list[dict]],
    prompt_by_id: dict[str, RoadmapPrompt],
    group_ids: dict[str, str],
) -> int:
    """Adopt existing projection nodes and collapse duplicate prompt bullets."""
    group_node_ids = set(group_ids.values())
    candidates: dict[str, list[dict]] = {}
    for node in list(by_id.values()):
        if str(node.get("parent_id") or "") not in group_node_ids:
            continue
        prompt_id = _prompt_id_from_name(node.get("name"))
        if prompt_id in prompt_by_id:
            candidates.setdefault(prompt_id, []).append(node)

    deleted = 0
    for prompt_id in prompt_by_id:
        mapped = _mapping_get(db, prompt_id)
        canonical_id = (
            mapped[0]
            if mapped and mapped[0] in existing_ids
            else None
        )
        prompt_candidates = candidates.get(prompt_id, [])
        if canonical_id is None and prompt_candidates:
            # Prefer the node carrying child state/commands; otherwise keep a
            # stable existing node instead of creating another projection.
            canonical = max(
                prompt_candidates,
                key=lambda node: (
                    len(children_by_parent.get(str(node["id"]), [])),
                    str(node["id"]),
                ),
            )
            canonical_id = str(canonical["id"])
            _mapping_set(db, prompt_id, canonical_id, {})

        if canonical_id is None:
            continue

        for duplicate in prompt_candidates:
            duplicate_id = str(duplicate["id"])
            if duplicate_id == canonical_id:
                continue

            # Preserve every child before removing a generated duplicate.
            moved_children = list(children_by_parent.get(duplicate_id, []))
            for child in moved_children:
                child_id = str(child["id"])
                client.move_node(child_id, canonical_id, position="bottom")
                child["parent_id"] = canonical_id
                children_by_parent.setdefault(canonical_id, []).append(child)
            children_by_parent.pop(duplicate_id, None)

            client.delete_node(duplicate_id)
            by_id.pop(duplicate_id, None)
            existing_ids.discard(duplicate_id)
            deleted += 1

    return deleted


def _ensure_mapped_node(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    existing_ids: set[str],
    *,
    key: str,
    parent_id: str,
    name: str,
    note: str | None = None,
    layout_mode: str = "bullets",
) -> tuple[str, dict]:
    mapped = _mapping_get(db, key)
    if mapped and mapped[0] in existing_ids:
        return mapped
    node_id = client.create_node(
        parent_id,
        name,
        note=note,
        layout_mode=layout_mode,
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
    observed_outcome_loader: Callable[[str], str | None] | None = None,
    pipeline_status: dict[str, dict] | None = None,
    ccs_bindings: dict[str, dict] | None = None,
) -> dict[str, int]:
    prompts = read_roadmap_db(
        raw_roadmap_db
        if raw_roadmap_db is not None
        else fetch_remote_roadmap_db(repository, branch)
    )
    prompt_by_id = {p.prompt_id: p for p in prompts}
    outcome_loader = observed_outcome_loader or load_latest_terminal_outcome
    for prompt in prompts:
        if prompt.status != "running":
            continue
        observed_outcome = str(outcome_loader(prompt.prompt_id) or "").upper()
        if observed_outcome in {"PASS", "BLOCKED", "FAIL", "CANCELLED", "UNKNOWN"}:
            prompt.last_outcome = observed_outcome

    pipeline_status = pipeline_status if pipeline_status is not None else read_pipeline_status()
    ccs_bindings = ccs_bindings if ccs_bindings is not None else read_ccs_bindings()

    exported = client.export_nodes()
    by_id = {
        str(node["id"]): node
        for node in exported
        if isinstance(node, dict) and node.get("id")
    }

    # Workflowy's export can lag behind successful mutations. A missing mapped
    # node is therefore verified through the single-node endpoint before the
    # projector is allowed to recreate it.
    mapping_keys = (
        [ROADMAP_ROOT_KEY]
        + [GROUP_PREFIX + key for key, _ in DASHBOARD_GROUPS]
        + [GROUP_PREFIX + key for key in LEGACY_GROUP_KEYS]
        + [prompt.prompt_id for prompt in prompts]
    )
    _hydrate_mapped_nodes(client, db, by_id, mapping_keys)

    existing_ids = set(by_id)
    children_by_parent: dict[str, list[dict]] = {}
    for node in by_id.values():
        parent_id = node.get("parent_id")
        if parent_id:
            children_by_parent.setdefault(str(parent_id), []).append(node)

    root_mapped = _mapping_get(db, ROADMAP_ROOT_KEY)
    if not root_mapped or root_mapped[0] not in existing_ids:
        root_candidates = [
            node
            for node in by_id.values()
            if str(node.get("parent_id") or "") == parent
            and str(node.get("name") or "").strip() == "Codex"
            and (
                "roadmap Codex" in str(node.get("note") or "")
                or any(
                    _group_name_matches(child.get("name"), label)
                    for child in children_by_parent.get(str(node["id"]), [])
                    for _key, label in DASHBOARD_GROUPS
                )
            )
        ]
        if root_candidates:
            root_candidate = max(
                root_candidates,
                key=lambda node: len(
                    children_by_parent.get(str(node["id"]), [])
                ),
            )
            _mapping_set(
                db,
                ROADMAP_ROOT_KEY,
                str(root_candidate["id"]),
                {},
            )

    root_id, _ = _ensure_mapped_node(
        client,
        db,
        existing_ids,
        key=ROADMAP_ROOT_KEY,
        parent_id=parent,
        name="Codex",
        note="Dashboard operativa della roadmap Codex. roadmap.sqlite resta la fonte canonica.",
        layout_mode="h1",
    )
    current_root = by_id.get(root_id)
    if current_root:
        root_note = "Dashboard operativa della roadmap Codex. roadmap.sqlite resta la fonte canonica."
        root_data = current_root.get("data") if isinstance(current_root.get("data"), dict) else {}
        root_layout = str(root_data.get("layoutMode") or "bullets")
        if (
            str(current_root.get("name") or "") != "Codex"
            or str(current_root.get("note") or "") != root_note
            or root_layout != "h1"
        ):
            client.update_node(
                root_id,
                "Codex",
                note=root_note,
                layout_mode="h1",
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
        mapped_group = _mapping_get(db, GROUP_PREFIX + key)
        if not mapped_group or mapped_group[0] not in existing_ids:
            group_candidates = [
                node
                for node in children_by_parent.get(root_id, [])
                if _group_name_matches(node.get("name"), label)
            ]
            if group_candidates:
                candidate = max(
                    group_candidates,
                    key=lambda node: len(
                        children_by_parent.get(str(node["id"]), [])
                    ),
                )
                _mapping_set(
                    db,
                    GROUP_PREFIX + key,
                    str(candidate["id"]),
                    {},
                )
        group_id, _ = _ensure_mapped_node(
            client,
            db,
            existing_ids,
            key=GROUP_PREFIX + key,
            parent_id=root_id,
            name=desired_name,
            layout_mode="h2",
        )
        group_ids[key] = group_id
        current_group = by_id.get(group_id)
        if current_group:
            group_data = current_group.get("data") if isinstance(current_group.get("data"), dict) else {}
            group_layout = str(group_data.get("layoutMode") or "bullets")
            if (
                str(current_group.get("name") or "") != desired_name
                or group_layout != "h2"
            ):
                client.update_node(group_id, desired_name, layout_mode="h2")
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

    duplicates_deleted = _reconcile_prompt_duplicates(
        client,
        db,
        by_id,
        existing_ids,
        children_by_parent,
        prompt_by_id,
        group_ids,
    )

    node_ids: dict[str, str] = {}
    created = 0
    for prompt in prompts:
        mapped = _mapping_get(db, prompt.prompt_id)
        if mapped and mapped[0] in existing_ids:
            node_ids[prompt.prompt_id] = mapped[0]
            continue
        group = prompt_groups[prompt.prompt_id]
        node_id = client.create_node(
            group_ids[group],
            prompt_name(prompt, group),
            layout_mode="h3" if group == "blocked" else "bullets",
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
    display_packets: dict[str, dict] = {
        prompt.prompt_id: prompt.fix_packet
        for prompt in prompts
        if isinstance(prompt.fix_packet, dict)
    }

    # Publish and display terminal B/F context even when the status came from
    # Codex directly rather than a manual Workflowy child command.
    for prompt in prompts:
        outcome = str(prompt.last_outcome or "").upper()
        if prompt.status == "blocked":
            outcome = "BLOCKED"
        elif prompt.status == "failed":
            outcome = "FAIL"
        if outcome not in {"BLOCKED", "FAIL"}:
            continue
        packet = packet_loader(prompt.prompt_id, outcome)
        if not packet:
            continue
        display_packets[prompt.prompt_id] = packet
        operation, request_key = packet_mutation(packet)
        already_sent = db.execute(
            "SELECT 1 FROM events WHERE source=? AND external_key=?",
            ("roadmap_fix_packet", request_key),
        ).fetchone()
        if already_sent:
            continue
        try:
            submit(
                {
                    "schema": "codex-roadmap.mutation.v1",
                    "actor": "workflowy-fix-packet",
                    "operations": [operation],
                },
                request_key,
            )
        except RuntimeError:
            warnings += 1
            continue
        db.execute(
            "INSERT INTO events(source,external_key,payload_json) VALUES(?,?,?)",
            ("roadmap_fix_packet", request_key, json.dumps(packet, sort_keys=True)),
        )
        fix_packets_submitted += 1

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

    desired_group_nodes: dict[str, list[str]] = {
        key: [
            node_ids[prompt.prompt_id]
            for prompt in prompts
            if prompt_groups[prompt.prompt_id] == key
        ]
        for key, _label in DASHBOARD_GROUPS
    }
    known_prompt_nodes = set(node_ids.values())
    current_group_nodes: dict[str, list[str]] = {
        key: [
            str(node["id"])
            for node in children_by_parent.get(group_ids[key], [])
            if str(node.get("id") or "") in known_prompt_nodes
        ]
        for key, _label in DASHBOARD_GROUPS
    }

    updated = 0
    moved = 0
    for prompt in prompts:
        node_id = node_ids[prompt.prompt_id]
        group = prompt_groups[prompt.prompt_id]
        desired_parent = group_ids[group]
        desired_name = prompt_name(prompt, group)
        desired_layout = "h3" if group == "blocked" else "bullets"
        desired_note = prompt_note(
            prompt,
            node_ids,
            repository=repository,
            branch=branch,
            pipeline=pipeline_status.get(prompt.prompt_id),
            binding=ccs_bindings.get(prompt.prompt_id),
            group=group,
            fix_packet=display_packets.get(prompt.prompt_id),
        )
        current = by_id.get(node_id)
        if current:
            current_name = str(current.get("name") or "")
            current_note = str(current.get("note") or "")
            current_data = current.get("data") if isinstance(current.get("data"), dict) else {}
            current_layout = str(current_data.get("layoutMode") or "bullets")
            if (
                current_name != desired_name
                or current_note != desired_note
                or current_layout != desired_layout
            ):
                client.update_node(
                    node_id,
                    desired_name,
                    note=desired_note,
                    layout_mode=desired_layout,
                )
                updated += 1
            if str(current.get("parent_id") or "") != desired_parent:
                client.move_node(node_id, desired_parent, position="bottom")
                moved += 1
        else:
            client.update_node(
                node_id,
                desired_name,
                note=desired_note,
                layout_mode=desired_layout,
            )
            updated += 1

    reordered = 0
    for key, _label in DASHBOARD_GROUPS:
        desired = desired_group_nodes[key]
        if current_group_nodes[key] == desired:
            continue
        # Workflowy only exposes top/bottom sibling placement. Moving the
        # canonical sequence in reverse to the top yields exactly the DB
        # queue order while preserving the relative order of non-prompt nodes.
        for node_id in reversed(desired):
            client.move_node(node_id, group_ids[key], position="top")
            reordered += 1

    db.commit()
    return {
        "prompts": len(prompts),
        "created": created,
        "updated": updated,
        "moved": moved,
        "reordered": reordered,
        "duplicates_deleted": duplicates_deleted,
        "mutations_submitted": submitted,
        "fix_prompts_created": fix_prompts,
        "fix_packets_submitted": fix_packets_submitted,
        "warnings": warnings,
    }
