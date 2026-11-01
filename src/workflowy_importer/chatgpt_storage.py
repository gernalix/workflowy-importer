"""ChatGPT conversation snapshot persistence and freshness classification.

Uses only a caller-provided SQLite connection. No live browser or Workflowy writes.
"""
from __future__ import annotations
import json
import sqlite3
from typing import Iterable
from .chatgpt_records import ChatRecord


CHATGPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chatgpt_conversations (
  conversation_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  created_at REAL,
  last_interaction_at REAL,
  status TEXT NOT NULL DEFAULT 'IDLE',
  running_evidence_at REAL,
  async_status TEXT,
  gizmo_id TEXT,
  source TEXT NOT NULL,
  last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chatgpt_last_interaction
ON chatgpt_conversations(last_interaction_at DESC);
CREATE INDEX IF NOT EXISTS idx_chatgpt_status
ON chatgpt_conversations(status, last_interaction_at DESC);
CREATE TABLE IF NOT EXISTS chatgpt_sync_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def ensure_chatgpt_schema(db: sqlite3.Connection) -> None:
    db.executescript(CHATGPT_SCHEMA)


def upsert_records(
    db: sqlite3.Connection,
    records: Iterable[ChatRecord],
    *,
    now: float,
) -> int:
    ensure_chatgpt_schema(db)
    count = 0
    with db:
        for record in records:
            db.execute(
                """INSERT INTO chatgpt_conversations(
                     conversation_id,title,url,created_at,last_interaction_at,
                     status,running_evidence_at,async_status,gizmo_id,source,last_seen_at
                   ) VALUES(?,?,?,?,?,'IDLE',?,?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     title=excluded.title,
                     url=excluded.url,
                     created_at=COALESCE(chatgpt_conversations.created_at,excluded.created_at),
                     last_interaction_at=MAX(
                       COALESCE(chatgpt_conversations.last_interaction_at,0),
                       COALESCE(excluded.last_interaction_at,0)
                     ),
                     running_evidence_at=COALESCE(
                       excluded.running_evidence_at,
                       chatgpt_conversations.running_evidence_at
                     ),
                     async_status=excluded.async_status,
                     gizmo_id=COALESCE(excluded.gizmo_id,chatgpt_conversations.gizmo_id),
                     source=excluded.source,
                     last_seen_at=excluded.last_seen_at""",
                (
                    record.conversation_id,
                    record.title,
                    record.url,
                    record.created_at,
                    record.last_interaction_at,
                    now if record.running else None,
                    json.dumps(record.async_status, ensure_ascii=False),
                    record.gizmo_id,
                    record.source,
                    now,
                ),
            )
            count += 1
    return count


def classify_statuses(
    db: sqlite3.Connection,
    *,
    now: float,
    recent_seconds: float = 15 * 60,
    running_ttl_seconds: float = 5 * 60,
) -> dict[str, int]:
    ensure_chatgpt_schema(db)
    counts = {"RUNNING": 0, "RECENT": 0, "IDLE": 0}
    rows = list(
        db.execute(
            "SELECT conversation_id,last_interaction_at,running_evidence_at "
            "FROM chatgpt_conversations"
        )
    )
    with db:
        for row in rows:
            running_at = row["running_evidence_at"]
            updated = row["last_interaction_at"] or 0
            if running_at and now - float(running_at) <= running_ttl_seconds:
                status = "RUNNING"
            elif updated and now - float(updated) <= recent_seconds:
                status = "RECENT"
            else:
                status = "IDLE"
            counts[status] += 1
            db.execute(
                "UPDATE chatgpt_conversations SET status=? WHERE conversation_id=?",
                (status, row["conversation_id"]),
            )
    return counts
