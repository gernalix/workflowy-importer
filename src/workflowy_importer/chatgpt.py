from __future__ import annotations

import json
from pathlib import Path


def _message_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        str(part) for part in parts if isinstance(part, (str, int, float))
    )


def conversation_to_markdown(conversation: dict) -> str:
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError("Conversation has no mapping")
    current = conversation.get("current_node")
    chain: list[dict] = []
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(str(current))
        node = mapping.get(current)
        if not isinstance(node, dict):
            break
        chain.append(node)
        current = node.get("parent")
    chain.reverse()
    title = str(conversation.get("title") or "ChatGPT conversation")
    lines = [f"# {title}", ""]
    for node in chain:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        if role not in {"user", "assistant", "system", "developer"}:
            continue
        text = _message_text(message).strip()
        if not text:
            continue
        heading = {
            "user": "User",
            "assistant": "Assistant",
            "system": "System",
            "developer": "Developer",
        }[role]
        lines.extend([f"## {heading}", "", text, ""])
    return "\n".join(lines).rstrip() + "\n"


def load_conversation(
    export_path: Path | str,
    *,
    conversation_id: str | None = None,
    title: str | None = None,
) -> dict:
    path = Path(export_path).expanduser()
    value = json.loads(path.read_text(encoding="utf-8"))
    conversations = value if isinstance(value, list) else value.get("conversations", [])
    if not isinstance(conversations, list):
        raise ValueError("Unsupported ChatGPT conversations export")
    matches: list[dict] = []
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        if conversation_id and str(conv.get("id")) == conversation_id:
            matches.append(conv)
        elif title and str(conv.get("title") or "").casefold() == title.casefold():
            matches.append(conv)
    if not matches:
        raise ValueError("Conversation not found")
    if len(matches) > 1:
        raise ValueError("Conversation selector is ambiguous; use the conversation id")
    return matches[0]
