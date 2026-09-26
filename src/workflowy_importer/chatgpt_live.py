from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from .api import WorkflowyAPIError, WorkflowyClient

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9333"
DEFAULT_INVENTORY = Path(
    "~/.local/state/chatgpt-rdc-supervisor/chats.json"
).expanduser()

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


@dataclass(slots=True)
class CollectionResult:
    records: list[ChatRecord]
    running_ids: set[str]
    full_complete: bool
    rate_limited: bool = False
    retry_after: float | None = None


class ChatGPTRateLimited(RuntimeError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("ChatGPT conversation endpoint rate limited")
        self.retry_after = retry_after


def ensure_chatgpt_schema(db: sqlite3.Connection) -> None:
    db.executescript(CHATGPT_SCHEMA)


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


def load_supervisor_inventory(path: Path | str = DEFAULT_INVENTORY) -> dict[str, dict]:
    inventory_path = Path(path).expanduser()
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    result: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("conversation_id"):
            continue
        result[str(item["conversation_id"])] = item
    return result


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


def mark_running(
    db: sqlite3.Connection, conversation_ids: Iterable[str], *, now: float
) -> None:
    ids = {str(value) for value in conversation_ids if value}
    if not ids:
        return
    ensure_chatgpt_schema(db)
    with db:
        db.executemany(
            """UPDATE chatgpt_conversations
               SET running_evidence_at=?, status='RUNNING'
               WHERE conversation_id=?""",
            [(now, value) for value in ids],
        )


def get_sync_state(
    db: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    ensure_chatgpt_schema(db)
    row = db.execute(
        "SELECT value FROM chatgpt_sync_state WHERE key=?", (key,)
    ).fetchone()
    return str(row[0]) if row else default


def set_sync_state(db: sqlite3.Connection, key: str, value: object) -> None:
    ensure_chatgpt_schema(db)
    with db:
        db.execute(
            """INSERT INTO chatgpt_sync_state(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )


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


class ChatGPTCloudCollector:
    def __init__(
        self,
        endpoint: str = DEFAULT_CDP_ENDPOINT,
        inventory_path: Path | str = DEFAULT_INVENTORY,
        *,
        request_delay: float = 0.4,
    ) -> None:
        self.endpoint = endpoint
        self.inventory_path = Path(inventory_path).expanduser()
        self.request_delay = request_delay
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self) -> "ChatGPTCloudCollector":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.endpoint)
        contexts = self._browser.contexts
        context = contexts[0] if contexts else self._browser.new_context()
        self._page = next(
            (page for page in context.pages if "chatgpt.com" in page.url), None
        )
        if self._page is None:
            self._page = context.new_page()
            self._page.goto(
                "https://chatgpt.com/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._page = None

    def _fetch_json(self, path: str) -> dict:
        if not path.startswith("/backend-api/"):
            raise ValueError("Only same-origin ChatGPT backend paths are allowed")
        assert self._page is not None
        result = self._page.evaluate(
            """async (path) => {
              const response = await fetch(path, {credentials: 'include'});
              let body = null;
              try { body = await response.json(); } catch {}
              return {
                status: response.status,
                retryAfter: response.headers.get('retry-after'),
                body
              };
            }""",
            path,
        )
        status = int(result.get("status", 0))
        if status == 429:
            try:
                retry_after = float(result.get("retryAfter"))
            except (TypeError, ValueError):
                retry_after = None
            raise ChatGPTRateLimited(retry_after)
        if status < 200 or status >= 300:
            raise RuntimeError(f"ChatGPT backend returned HTTP {status} for {path}")
        body = result.get("body")
        if not isinstance(body, dict):
            raise RuntimeError("ChatGPT backend returned an unexpected payload")
        if self.request_delay:
            time.sleep(self.request_delay)
        return body

    def _conversation_pages(
        self, *, full: bool, inventory: dict[str, dict]
    ) -> list[ChatRecord]:
        records: dict[str, ChatRecord] = {}
        variants = (
            ("chat", {"exclude_conversation_origin": "tpp"}),
            ("tpp", {"conversation_origin": "tpp"}),
        )
        for source, selector in variants:
            offset = 0
            while True:
                params = {
                    **selector,
                    "expand": "false",
                    "hide_snorlax": "false",
                    "is_archived": "false",
                    "is_starred": "false",
                    "limit": "20",
                    "order": "updated",
                    "offset": str(offset),
                }
                body = self._fetch_json(
                    "/backend-api/conversations?" + urlencode(params)
                )
                items = body.get("items", [])
                if not isinstance(items, list):
                    items = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    record = record_from_payload(
                        item, source=source, inventory=inventory
                    )
                    if record:
                        records[record.conversation_id] = record
                if not full:
                    break
                total = int(body.get("total") or 0)
                offset += len(items)
                if not items or offset >= total:
                    break
        return list(records.values())

    @staticmethod
    def _project_ids(value: object) -> set[str]:
        found: set[str] = set()

        def visit(node: object) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in {"id", "gizmo_id"} and isinstance(child, str):
                        if child.startswith("g-p-"):
                            found.add(child)
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(value)
        return found

    @staticmethod
    def _conversation_payloads(value: object) -> list[dict]:
        found: dict[str, dict] = {}

        def visit(node: object) -> None:
            if isinstance(node, dict):
                cid = str(node.get("id") or node.get("conversation_id") or "")
                has_time = node.get("create_time") is not None or node.get("update_time") is not None
                if cid and not cid.startswith("g-p-") and node.get("title") and has_time:
                    found[cid] = node
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(value)
        return list(found.values())

    def _recent_project_conversations(
        self, *, inventory: dict[str, dict]
    ) -> list[ChatRecord]:
        params = {
            "conversations_per_gizmo": "5",
            "limit": "20",
            "owned_only": "false",
        }
        body = self._fetch_json(
            "/backend-api/gizmos/snorlax/sidebar?" + urlencode(params)
        )
        records: list[ChatRecord] = []
        for item in self._conversation_payloads(body):
            record = record_from_payload(
                item, source="project", inventory=inventory
            )
            if record:
                records.append(record)
        return records

    def _project_conversations(
        self, *, inventory: dict[str, dict]
    ) -> list[ChatRecord]:
        project_ids: set[str] = set()
        cursor: str | None = None
        while True:
            params = {
                "conversations_per_gizmo": "0",
                "limit": "20",
                "owned_only": "false",
            }
            if cursor:
                params["cursor"] = cursor
            body = self._fetch_json(
                "/backend-api/gizmos/snorlax/sidebar?" + urlencode(params)
            )
            project_ids.update(self._project_ids(body))
            cursor_value = body.get("cursor") or body.get("next_cursor")
            cursor = str(cursor_value) if cursor_value else None
            if not cursor:
                break

        records: dict[str, ChatRecord] = {}
        for gizmo_id in sorted(project_ids):
            cursor = "0"
            while cursor:
                params = {
                    "cursor": cursor,
                    "limit": "100",
                    "owned_only": "false",
                }
                body = self._fetch_json(
                    f"/backend-api/gizmos/{quote(gizmo_id)}/conversations?"
                    + urlencode(params)
                )
                items = body.get("items", [])
                if not isinstance(items, list):
                    items = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    record = record_from_payload(
                        item, source="project", inventory=inventory
                    )
                    if record:
                        records[record.conversation_id] = record
                cursor_value = body.get("cursor") or body.get("next_cursor")
                cursor = str(cursor_value) if cursor_value else ""
        return list(records.values())

    def _detail_record(
        self, conversation_id: str, inventory: dict[str, dict]
    ) -> ChatRecord | None:
        body = self._fetch_json(
            f"/backend-api/conversation/{quote(conversation_id)}"
        )
        record = record_from_payload(
            body, source="detail", inventory=inventory
        )
        if record is None:
            return None
        mapping = body.get("mapping", {})
        if isinstance(mapping, dict):
            for node in mapping.values():
                message = node.get("message") if isinstance(node, dict) else None
                if not isinstance(message, dict):
                    continue
                if str(message.get("status") or "") == "in_progress":
                    record.running = True
                    break
        return record

    def _open_running_ids(self) -> set[str]:
        running: set[str] = set()
        assert self._browser is not None
        for context in self._browser.contexts:
            for page in context.pages:
                if "/c/" not in page.url:
                    continue
                conversation_id = page.url.split("/c/", 1)[1].split("/", 1)[0]
                try:
                    generating = bool(
                        page.evaluate(
                            """() => !!document.querySelector(
                              '[data-testid="stop-button"]'
                            ) || [...document.querySelectorAll('button')]
                              .some(b => ((b.innerText || b.getAttribute('aria-label') || '')
                              .toLowerCase()).includes('stop generating'))"""
                        )
                    )
                except Exception:
                    generating = False
                if generating:
                    running.add(conversation_id)
        return running

    def collect(
        self,
        *,
        full: bool,
        api_enabled: bool,
        detail_limit: int = 3,
        known_ids: set[str] | None = None,
    ) -> CollectionResult:
        inventory = load_supervisor_inventory(self.inventory_path)
        previously_known = known_ids or set()
        running_ids = self._open_running_ids()
        if not api_enabled:
            return CollectionResult([], running_ids, False)

        records: dict[str, ChatRecord] = {}
        try:
            for record in self._conversation_pages(
                full=full, inventory=inventory
            ):
                records[record.conversation_id] = record
            for record in self._recent_project_conversations(
                inventory=inventory
            ):
                records[record.conversation_id] = record
            if full:
                for record in self._project_conversations(inventory=inventory):
                    records[record.conversation_id] = record

            detail_candidates = sorted(
                records.values(),
                key=lambda row: row.last_interaction_at or 0,
                reverse=True,
            )
            known_ids = {row.conversation_id for row in detail_candidates}
            unseen_inventory = sorted(
                (
                    item
                    for cid, item in inventory.items()
                    if cid not in known_ids and cid not in previously_known
                ),
                key=lambda item: float(item.get("first_seen_epoch") or 0),
                reverse=True,
            )
            candidate_ids = [row.conversation_id for row in detail_candidates]
            candidate_ids.extend(
                str(item["conversation_id"]) for item in unseen_inventory
            )
            for conversation_id in candidate_ids[: max(0, detail_limit)]:
                detail = self._detail_record(conversation_id, inventory)
                if detail:
                    previous = records.get(conversation_id)
                    if previous and not detail.gizmo_id:
                        detail.gizmo_id = previous.gizmo_id
                        detail.url = previous.url
                    records[conversation_id] = detail
                    if detail.running:
                        running_ids.add(conversation_id)
        except ChatGPTRateLimited as exc:
            return CollectionResult(
                list(records.values()),
                running_ids,
                False,
                rate_limited=True,
                retry_after=exc.retry_after,
            )

        running_ids.update(
            row.conversation_id for row in records.values() if row.running
        )
        return CollectionResult(
            list(records.values()), running_ids, full_complete=full
        )


def _mapping_get(
    db: sqlite3.Connection, namespace: str, external_key: str
) -> tuple[str, dict] | None:
    row = db.execute(
        """SELECT node_id,metadata_json FROM mappings
           WHERE namespace=? AND external_key=?""",
        (namespace, external_key),
    ).fetchone()
    if not row:
        return None
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except ValueError:
        metadata = {}
    return str(row["node_id"]), metadata


def _mapping_set(
    db: sqlite3.Connection,
    namespace: str,
    external_key: str,
    node_id: str,
    metadata: dict,
) -> None:
    db.execute(
        """INSERT INTO mappings(namespace,external_key,node_id,metadata_json)
           VALUES(?,?,?,?)
           ON CONFLICT(namespace,external_key) DO UPDATE SET
             node_id=excluded.node_id,
             metadata_json=excluded.metadata_json""",
        (
            namespace,
            external_key,
            node_id,
            json.dumps(metadata, sort_keys=True),
        ),
    )


def _ensure_named_node(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    *,
    namespace: str,
    external_key: str,
    parent_id: str,
    name: str,
) -> str:
    mapped = _mapping_get(db, namespace, external_key)
    if mapped:
        node_id, metadata = mapped
        if metadata.get("name") != name:
            client.update_node(node_id, name=name)
            _mapping_set(
                db, namespace, external_key, node_id, {"name": name}
            )
        return node_id
    node_id = client.create_node(parent_id, name, position="top")
    _mapping_set(db, namespace, external_key, node_id, {"name": name})
    return node_id

def _format_local(epoch: float | None, tz_name: str) -> str:
    if not epoch:
        return "unknown"
    dt = datetime.fromtimestamp(float(epoch), ZoneInfo(tz_name))
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _conversation_projection(row: sqlite3.Row, tz_name: str) -> tuple[str, str]:
    title = html.escape(str(row["title"]), quote=False)
    href = html.escape(str(row["url"]), quote=True)
    name = f'<a href="{href}">{title}</a>'
    note = "\n".join(
        (
            f"Created: {_format_local(row['created_at'], tz_name)}",
            "Last interaction: "
            + _format_local(row["last_interaction_at"], tz_name),
            f"State: {row['status']}",
        )
    )
    return name, note


def sync_chatgpt_dashboard(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    *,
    parent: str = "inbox",
    timezone_name: str = "Europe/Copenhagen",
    max_writes: int = 20,
) -> dict[str, int]:
    ensure_chatgpt_schema(db)
    counts = {
        status: int(
            db.execute(
                "SELECT COUNT(*) FROM chatgpt_conversations WHERE status=?",
                (status,),
            ).fetchone()[0]
        )
        for status in ("RUNNING", "RECENT", "IDLE")
    }
    parent_id = client.resolve_target_id(parent)
    root_name = (
        "🤖 ChatGPT live chats"
        f" · 🟢 {counts['RUNNING']} · 🟡 {counts['RECENT']} · ⚪ {counts['IDLE']}"
    )
    root_id = _ensure_named_node(
        client,
        db,
        namespace="chatgpt-live-root",
        external_key=parent_id,
        parent_id=parent_id,
        name=root_name,
    )
    groups: dict[str, str] = {}
    labels = {
        "RUNNING": "🟢 RUNNING",
        "RECENT": "🟡 RECENT",
        "IDLE": "⚪ IDLE",
    }
    for status in ("RUNNING", "RECENT", "IDLE"):
        groups[status] = _ensure_named_node(
            client,
            db,
            namespace="chatgpt-live-group",
            external_key=f"{root_id}:{status}",
            parent_id=root_id,
            name=f"{labels[status]} ({counts[status]})",
        )

    writes = 0
    rows = list(
        db.execute(
            """SELECT * FROM chatgpt_conversations
               ORDER BY CASE status
                 WHEN 'RUNNING' THEN 0 WHEN 'RECENT' THEN 1 ELSE 2 END,
               COALESCE(last_interaction_at,created_at,0) DESC"""
        )
    )
    for row in rows:
        if writes >= max_writes:
            break
        name, note = _conversation_projection(row, timezone_name)
        projection = {
            "name": name,
            "note": note,
            "status": row["status"],
            "last_interaction_at": row["last_interaction_at"],
        }
        mapped = _mapping_get(
            db, "chatgpt-live-conversation", row["conversation_id"]
        )
        if not mapped:
            node_id = client.create_node(
                groups[row["status"]],
                name,
                position="top",
                note=note,
            )
            _mapping_set(
                db,
                "chatgpt-live-conversation",
                row["conversation_id"],
                node_id,
                projection,
            )
            writes += 1
            continue

        node_id, previous = mapped
        changed = (
            previous.get("name") != name or previous.get("note") != note
        )
        moved = previous.get("status") != row["status"]
        reordered = (
            previous.get("last_interaction_at")
            != row["last_interaction_at"]
        )
        if changed:
            client.update_node(node_id, name=name, note=note)
            writes += 1
        if writes < max_writes and (moved or reordered):
            client.move_node(
                node_id, groups[row["status"]], position="top"
            )
            writes += 1
        if changed or moved or reordered:
            _mapping_set(
                db,
                "chatgpt-live-conversation",
                row["conversation_id"],
                node_id,
                projection,
            )
    db.commit()
    return {**counts, "writes": writes, "total": sum(counts.values())}
