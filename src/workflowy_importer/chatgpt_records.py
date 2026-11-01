"""Pure normalized ChatGPT conversation records, independent of browser and SQLite."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class ChatRecord:
    conversation_id: str
    title: str
    url: str
    created_at: float | None
    last_interaction_at: float | None
    source: str
    gizmo_id: str | None = None
    async_status: object | None = None
    running: bool = False


def _timestamp(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return number / 1000.0 if number > 1e12 else number
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_running(value: object) -> bool:
    if value in (None, False, 0, "0", "", "completed", "complete"):
        return False
    return True


def _conversation_url(
    conversation_id: str,
    gizmo_id: str | None,
    inventory: dict[str, dict],
) -> str:
    existing = inventory.get(conversation_id, {}).get("url")
    if existing:
        return str(existing)
    if gizmo_id and gizmo_id.startswith("g-p-"):
        return f"https://chatgpt.com/g/{gizmo_id}/c/{conversation_id}"
    return f"https://chatgpt.com/c/{conversation_id}"


def record_from_payload(
    payload: dict,
    *,
    source: str,
    inventory: dict[str, dict],
) -> ChatRecord | None:
    conversation_id = str(
        payload.get("id") or payload.get("conversation_id") or ""
    ).strip()
    if not conversation_id:
        return None
    gizmo = payload.get("gizmo_id") or payload.get("conversation_template_id")
    gizmo_id = str(gizmo) if gizmo else None
    title = str(payload.get("title") or f"Chat {conversation_id[:8]}").strip()
    created = _timestamp(payload.get("create_time"))
    updated = _timestamp(payload.get("update_time")) or created
    async_status = payload.get("async_status")
    return ChatRecord(
        conversation_id=conversation_id,
        title=title,
        url=_conversation_url(conversation_id, gizmo_id, inventory),
        created_at=created,
        last_interaction_at=updated,
        source=source,
        gizmo_id=gizmo_id,
        async_status=async_status,
        running=_is_running(async_status),
    )
