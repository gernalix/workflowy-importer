from __future__ import annotations

import re

_UUIDISH = re.compile(r"^[0-9a-fA-F-]{12,}$")


def workflowy_url(node_id: str) -> str:
    """Return the canonical Workflowy web/app link for a node id or short id."""
    value = node_id.strip()
    if value.startswith("https://workflowy.com/#/"):
        return value
    if not _UUIDISH.fullmatch(value):
        raise ValueError("Expected a Workflowy node id, short id, or Workflowy URL")
    short_id = value.replace("-", "")[-12:]
    return f"https://workflowy.com/#/{short_id}"
