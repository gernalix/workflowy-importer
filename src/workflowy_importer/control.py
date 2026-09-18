from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .api import WorkflowyClient


@dataclass(frozen=True, slots=True)
class ControlResult:
    node_id: str
    action: str
    returncode: int


def run_control_actions(
    client: WorkflowyClient,
    *,
    parent: str,
    config: dict,
    timeout: float = 120.0,
) -> list[ControlResult]:
    """Run explicit RUN: actions from a Workflowy control node.

    Commands are allowlisted in config["control_actions"] and executed without
    a shell. Unknown actions are left untouched. Successful task nodes are
    marked complete; output is written as a child and capped.
    """
    actions = config.get("control_actions", {})
    if not isinstance(actions, dict):
        raise ValueError("control_actions must be a JSON object")
    results: list[ControlResult] = []
    for node in client.list_nodes(parent):
        if node.get("completed"):
            continue
        name = str(node.get("name") or "").strip()
        if not name.startswith("RUN: "):
            continue
        action = name[5:].strip()
        argv = actions.get(action)
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(x, str) for x in argv)
        ):
            continue
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = (
            proc.stdout
            + ("\n" + proc.stderr if proc.stderr else "")
        ).strip()
        output = output[-12000:] if output else "(no output)"
        client.create_node(
            str(node["id"]),
            f"Exit {proc.returncode}\n\n```\n{output}\n```",
            position="top",
        )
        if proc.returncode == 0:
            client.complete_node(str(node["id"]))
        results.append(
            ControlResult(
                str(node["id"]),
                action,
                proc.returncode,
            )
        )
    return results
