"""Build the small, safe recovery context stored in the canonical roadmap."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_PUBLISHED_ROOT = Path("~/projects/codex-usage").expanduser()
_PATH_RE = re.compile(r"(?<![\w.-])/(?:[^\s`'\")]+)")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(token|secret|api[_ -]?key|password)\b\s*([:=])\s*[^\s,;]+"
)
_LABEL_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:blocker|failure|error|cause|first failure)\s*[:=-]\s*(.+)$"
)
_NEXT_RE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:next action|action|next step)\s*[:=-]\s*(.+)$")
_BRANCH_RE = re.compile(r"(?im)\bbranch\s*[:=]\s*[`*_~]*([A-Za-z0-9._/-]+)")
_PR_RE = re.compile(r"(?i)\b(?:PR|pull request)\s*[#:]?\s*(\d+)")
_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)

_TERMINAL_OUTCOMES = {"PASS", "BLOCKED", "FAIL", "CANCELLED", "UNKNOWN"}


def _safe_text(value: object, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").replace("\\_", "_").split())
    text = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    text = _PATH_RE.sub("<path>", text)
    return text[:limit].rstrip()


def _labelled(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text)
    return _safe_text(match.group(1)) if match else None


def _concrete_blocker(text: str, outcome: str) -> str:
    labelled = _labelled(text, _LABEL_RE)
    if labelled:
        return labelled
    for line in text.splitlines():
        candidate = _safe_text(line)
        if candidate and candidate.upper() != f"RESULT={outcome}" and re.search(
            r"(?i)\b(block|fail|error|missing|denied|conflict|timeout)\b", candidate
        ):
            return candidate
    return f"Codex reported {outcome}; inspect the linked execution report."


def _work_state(text: str) -> dict[str, str]:
    state: dict[str, str] = {}
    if branch := _BRANCH_RE.search(text):
        state["branch"] = branch.group(1)
    if pr := _PR_RE.search(text):
        state["pr"] = f"#{pr.group(1)}"
    if commit := _COMMIT_RE.search(text):
        state["commit"] = commit.group(0)
    return state


def _candidate_metrics(published_root: Path, prompt_id: str, outcome: str) -> list[dict[str, Any]]:
    root = published_root.expanduser()
    cycles = root / "prompts" / prompt_id / "cycles"
    if not cycles.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    for path in cycles.glob("*/metrics.json"):
        try:
            metrics = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metrics, dict):
            continue
        if str(metrics.get("prompt_id") or "") != prompt_id:
            continue
        if str(metrics.get("status") or "").upper() != outcome:
            continue
        if not str(metrics.get("final_response_redacted") or "").strip():
            continue
        candidates.append(metrics)
    return candidates


def load_latest_terminal_outcome(
    prompt_id: str,
    *,
    published_root: Path = DEFAULT_PUBLISHED_ROOT,
) -> str | None:
    """Return the newest finished Codex outcome published locally for a prompt."""
    if not re.fullmatch(r"\d{6}", prompt_id):
        return None
    cycles = published_root.expanduser() / "prompts" / prompt_id / "cycles"
    if not cycles.is_dir():
        return None

    latest_key: tuple[str, str] | None = None
    latest_outcome: str | None = None
    for path in cycles.glob("*/metrics.json"):
        try:
            metrics = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metrics, dict):
            continue
        if str(metrics.get("prompt_id") or "") != prompt_id:
            continue
        outcome = str(metrics.get("status") or "").upper()
        ended_at = str(metrics.get("timestamp_end_utc") or "")
        if outcome not in _TERMINAL_OUTCOMES or not ended_at:
            continue
        key = (ended_at, str(metrics.get("cycle_key") or ""))
        if latest_key is None or key > latest_key:
            latest_key = key
            latest_outcome = outcome
    return latest_outcome


def load_fix_packet(
    prompt_id: str,
    outcome: str,
    *,
    published_root: Path = DEFAULT_PUBLISHED_ROOT,
) -> dict[str, Any] | None:
    """Load one redacted terminal report already published by codex-usage."""
    if outcome not in {"BLOCKED", "FAIL"} or not re.fullmatch(r"\d{6}", prompt_id):
        return None
    candidates = _candidate_metrics(published_root, prompt_id, outcome)
    if not candidates:
        return None
    metrics = max(
        candidates,
        key=lambda row: (str(row.get("timestamp_end_utc") or ""), str(row.get("cycle_key") or "")),
    )
    final = str(metrics["final_response_redacted"])
    cycle_key = str(metrics.get("cycle_key") or "")
    report_digest = hashlib.sha256(final.encode("utf-8")).hexdigest()[:16]
    next_action = _labelled(final, _NEXT_RE) or "Use the concrete blocker above for the smallest corrective action."
    return {
        "schema": "codex-roadmap.fix-packet.v1",
        "prompt_id": prompt_id,
        "outcome": outcome,
        "blocker": _concrete_blocker(final, outcome),
        "work_state": _work_state(final),
        "next_action": _safe_text(next_action),
        "report_ref": f"codex-usage:{cycle_key}:{report_digest}",
    }


def packet_mutation(packet: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return the existing roadmap analysis operation and a stable writer key."""
    prompt_id = str(packet["prompt_id"])
    report_ref = str(packet["report_ref"])
    digest = report_ref.rsplit(":", 1)[-1]
    return (
        {
            "op": "analysis",
            "prompt_id": prompt_id,
            "actor": "workflowy-fix-packet",
            "bottlenecks_found": True,
            "summary": json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "source_ref": report_ref,
        },
        f"workflowy-fix-packet-{prompt_id}-{digest}",
    )
