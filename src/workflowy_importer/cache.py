from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .links import workflowy_url

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  parent_id TEXT,
  name TEXT NOT NULL,
  note TEXT,
  priority REAL,
  completed INTEGER NOT NULL DEFAULT 0,
  layout_mode TEXT,
  created_at INTEGER,
  modified_at INTEGER,
  completed_at INTEGER,
  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id, priority);
CREATE INDEX IF NOT EXISTS idx_nodes_modified ON nodes(modified_at);
CREATE TABLE IF NOT EXISTS snapshots (
  captured_at INTEGER PRIMARY KEY,
  node_count INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings (
  namespace TEXT NOT NULL,
  external_key TEXT NOT NULL,
  node_id TEXT NOT NULL,
  metadata_json TEXT,
  PRIMARY KEY(namespace, external_key)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  external_key TEXT,
  occurred_at INTEGER,
  node_id TEXT,
  payload_json TEXT NOT NULL,
  UNIQUE(source, external_key)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    db = sqlite3.connect(Path(path).expanduser(), timeout=30.0)
    db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    try:
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(id UNINDEXED, name, note)"
        )
    except sqlite3.OperationalError:
        pass
    return db


def refresh_cache(
    db: sqlite3.Connection, nodes: Iterable[dict], *, keep_snapshot: bool = True
) -> int:
    rows = [node for node in nodes if isinstance(node, dict) and node.get("id")]
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    captured = int(time.time())
    with db:
        db.execute("DELETE FROM nodes")
        for node in rows:
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            db.execute(
                """INSERT INTO nodes
                (id,parent_id,name,note,priority,completed,layout_mode,created_at,modified_at,completed_at,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(node["id"]),
                    node.get("parent_id"),
                    str(node.get("name") or ""),
                    node.get("note"),
                    node.get("priority"),
                    int(bool(node.get("completed"))),
                    data.get("layoutMode"),
                    node.get("createdAt"),
                    node.get("modifiedAt"),
                    node.get("completedAt"),
                    json.dumps(node, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        try:
            db.execute("DELETE FROM node_fts")
            db.execute(
                "INSERT INTO node_fts(id,name,note) SELECT id,name,COALESCE(note,'') FROM nodes"
            )
        except sqlite3.OperationalError:
            pass
        if keep_snapshot:
            while db.execute(
                "SELECT 1 FROM snapshots WHERE captured_at=?", (captured,)
            ).fetchone():
                captured += 1
            db.execute(
                "INSERT INTO snapshots(captured_at,node_count,payload_json) VALUES(?,?,?)",
                (captured, len(rows), payload),
            )
    return len(rows)


def search(
    db: sqlite3.Connection, query: str, *, limit: int = 20
) -> list[sqlite3.Row]:
    try:
        return list(
            db.execute(
                """SELECT n.* FROM node_fts f JOIN nodes n ON n.id=f.id
                WHERE node_fts MATCH ? ORDER BY bm25(node_fts) LIMIT ?""",
                (query, limit),
            )
        )
    except sqlite3.OperationalError:
        like = f"%{query}%"
        return list(
            db.execute(
                "SELECT * FROM nodes WHERE name LIKE ? OR note LIKE ? ORDER BY modified_at DESC LIMIT ?",
                (like, like, limit),
            )
        )


def old_nodes(
    db: sqlite3.Connection, *, older_than_days: int, limit: int
) -> list[sqlite3.Row]:
    cutoff = int(time.time()) - older_than_days * 86400
    return list(
        db.execute(
            """SELECT * FROM nodes WHERE completed=0 AND COALESCE(modified_at,created_at,0) < ?
            ORDER BY COALESCE(modified_at,created_at,0) ASC LIMIT ?""",
            (cutoff, limit),
        )
    )


def duplicate_groups(
    db: sqlite3.Connection, *, limit: int = 100
) -> list[tuple[str, list[sqlite3.Row]]]:
    rows = list(db.execute("SELECT * FROM nodes WHERE TRIM(name) <> '' ORDER BY name"))
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = re.sub(r"\s+", " ", row["name"].strip()).casefold()
        groups.setdefault(key, []).append(row)
    dupes = [(key, values) for key, values in groups.items() if len(values) > 1]
    dupes.sort(key=lambda item: (-len(item[1]), item[0]))
    return dupes[:limit]


def snapshot_diff(db: sqlite3.Connection) -> dict[str, int]:
    snaps = list(
        db.execute("SELECT payload_json FROM snapshots ORDER BY captured_at DESC LIMIT 2")
    )
    if len(snaps) < 2:
        return {"added": 0, "removed": 0, "changed": 0}
    current, previous = (json.loads(snaps[0][0]), json.loads(snaps[1][0]))
    a = {str(x["id"]): x for x in current}
    b = {str(x["id"]): x for x in previous}
    common = a.keys() & b.keys()
    return {
        "added": len(a.keys() - b.keys()),
        "removed": len(b.keys() - a.keys()),
        "changed": sum(1 for key in common if a[key] != b[key]),
    }


def weekly_summary(db: sqlite3.Connection, *, days: int = 7) -> str:
    cutoff = int(time.time()) - days * 86400
    created = db.execute(
        "SELECT COUNT(*) FROM nodes WHERE created_at>=?", (cutoff,)
    ).fetchone()[0]
    modified = db.execute(
        "SELECT COUNT(*) FROM nodes WHERE modified_at>=?", (cutoff,)
    ).fetchone()[0]
    completed = db.execute(
        "SELECT COUNT(*) FROM nodes WHERE completed_at>=?", (cutoff,)
    ).fetchone()[0]
    open_todos = db.execute(
        "SELECT COUNT(*) FROM nodes WHERE completed=0 AND layout_mode='todo'"
    ).fetchone()[0]
    diff = snapshot_diff(db)
    return (
        f"Workflowy weekly review ({days} days)\n\n"
        f"- Created nodes: {created}\n"
        f"- Modified nodes: {modified}\n"
        f"- Completed nodes: {completed}\n"
        f"- Open todos now: {open_todos}\n"
        f"- Since previous snapshot: +{diff['added']} / -{diff['removed']} / {diff['changed']} changed"
    )


def node_links(db: sqlite3.Connection) -> list[dict[str, str]]:
    return [
        {"id": row["id"], "name": row["name"], "url": workflowy_url(row["id"])}
        for row in db.execute("SELECT id,name FROM nodes ORDER BY name")
    ]
