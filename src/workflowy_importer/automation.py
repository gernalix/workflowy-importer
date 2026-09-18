from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .api import WorkflowyClient


@dataclass(frozen=True, slots=True)
class RouteDecision:
    destination: str
    mirror_today: bool = False
    rule: str = "default"


def _matches(text: str, rule: dict) -> bool:
    haystack = text.casefold()
    contains = [str(x).casefold() for x in rule.get("contains", [])]
    any_contains = [str(x).casefold() for x in rule.get("any_contains", [])]
    pattern = rule.get("regex")
    if contains and not all(token in haystack for token in contains):
        return False
    if any_contains and not any(token in haystack for token in any_contains):
        return False
    if pattern and not re.search(str(pattern), text, flags=re.IGNORECASE):
        return False
    return bool(contains or any_contains or pattern)


def classify(text: str, config: dict) -> RouteDecision:
    for rule in config.get("rules", []):
        if isinstance(rule, dict) and _matches(text, rule):
            return RouteDecision(
                destination=str(
                    rule.get("destination") or config.get("default") or "inbox"
                ),
                mirror_today=bool(rule.get("mirror_today")),
                rule=str(rule.get("name") or "unnamed"),
            )
    return RouteDecision(destination=str(config.get("default") or "inbox"))


def classify_with_optional_command(text: str, config: dict) -> RouteDecision:
    decision = classify(text, config)
    if decision.rule != "default":
        return decision
    command = config.get("fallback_classifier_command")
    if not command:
        return decision
    proc = subprocess.run(
        [str(part) for part in command],
        input=text,
        text=True,
        capture_output=True,
        timeout=float(config.get("classifier_timeout", 15)),
        check=False,
    )
    if proc.returncode != 0:
        return decision
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return decision
    if not isinstance(value, dict) or not value.get("destination"):
        return decision
    confidence = float(value.get("confidence", 0.0))
    threshold = float(config.get("classifier_min_confidence", 0.9))
    if confidence < threshold:
        return decision
    return RouteDecision(
        destination=str(value["destination"]),
        mirror_today=bool(value.get("mirror_today")),
        rule="fallback-classifier",
    )


def load_rules(path: Path | str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {"default": "inbox", "rules": []}
    value = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Routing config must be a JSON object")
    return value


def capture(
    client: WorkflowyClient,
    text: str,
    *,
    destination: str = "inbox",
    note: str | None = None,
    mirror_today: bool = False,
) -> str:
    node_id = client.create_node(destination, text, note=note, position="top")
    if mirror_today:
        mirror_parent = client.create_node(
            "today", "Automatic mirrors", position="bottom"
        )
        client.mirror_node(node_id, mirror_parent, position="top")
    return node_id


def capture_routed(
    client: WorkflowyClient,
    text: str,
    config: dict,
    *,
    note: str | None = None,
) -> tuple[str, RouteDecision]:
    decision = classify_with_optional_command(text, config)
    node_id = capture(
        client,
        text,
        destination=decision.destination,
        note=note,
        mirror_today=decision.mirror_today,
    )
    return node_id, decision


def event_key(source: str, payload: dict) -> str:
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(f"{source}\0{raw}".encode()).hexdigest()


def record_event(
    db: sqlite3.Connection,
    *,
    source: str,
    payload: dict,
    node_id: str | None,
    occurred_at: int | None = None,
    external_key: str | None = None,
) -> bool:
    key = external_key or event_key(source, payload)
    try:
        with db:
            db.execute(
                "INSERT INTO events(source,external_key,occurred_at,node_id,payload_json) VALUES(?,?,?,?,?)",
                (
                    source,
                    key,
                    occurred_at,
                    node_id,
                    json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False
