from __future__ import annotations

import json
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import WorkflowyAPIError, WorkflowyClient
from .automation import classify_with_optional_command, load_rules



DEFAULT_ROADMAP_DIR = Path("~/projects/codex-roadmap").expanduser()


def _roadmap_prompt_text(roadmap_dir: Path, prompt_id: str) -> tuple[str, str] | None:
    if not re.fullmatch(r"\d{6}", prompt_id):
        return None
    roadmap_dir = roadmap_dir.expanduser()
    db_path = roadmap_dir / "roadmap.sqlite"
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        has_materializations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_materializations'"
        ).fetchone()
        if has_materializations:
            row = conn.execute(
                "SELECT body FROM prompt_materializations WHERE prompt_id=?",
                (prompt_id,),
            ).fetchone()
            if row and str(row["body"] or "").strip():
                return str(row["body"]), "roadmap.sqlite"
        row = conn.execute(
            "SELECT current_path FROM prompts WHERE prompt_id=?",
            (prompt_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    rel = str(row["current_path"] or "")
    if not rel:
        return None
    path = (roadmap_dir / rel).resolve()
    try:
        path.relative_to(roadmap_dir.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8"), rel


def _roadmap_fix_packet(roadmap_dir: Path, prompt_id: str) -> dict | None:
    """Read the latest publisher packet from the canonical roadmap DB."""
    if not re.fullmatch(r"\d{6}", prompt_id):
        return None
    db_path = roadmap_dir.expanduser() / "roadmap.sqlite"
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT summary FROM analyses WHERE prompt_id=? AND source_ref LIKE 'codex-usage:%' "
            "ORDER BY analyzed_at DESC,analysis_id DESC LIMIT 1",
            (prompt_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    try:
        packet = json.loads(str(row[0] or ""))
    except json.JSONDecodeError:
        return None
    if not isinstance(packet, dict) or packet.get("prompt_id") != prompt_id:
        return None
    return packet


def _ensure_mirror_parent(client: WorkflowyClient) -> str:
    try:
        for node in client.list_nodes("today"):
            if str(node.get("name") or "").strip() == "Automatic mirrors":
                return str(node["id"])
    except WorkflowyAPIError as exc:
        if exc.status_code != 404:
            raise
    return client.create_node("today", "Automatic mirrors", position="bottom")


def _capture_payload(
    client: WorkflowyClient, payload: dict, rules: dict
) -> tuple[str, str]:
    title = str(payload.get("title") or "Captured item").strip() or "Captured item"
    body = str(payload.get("markdown") or payload.get("text") or "").strip()
    source_url = str(payload.get("source_url") or "").strip()
    decision = classify_with_optional_command(f"{title}\n{body}", rules)
    note = f"Source: {source_url}" if source_url else None
    root_id = client.create_node(
        decision.destination, title, note=note, position="top"
    )
    if body:
        client.create_node(root_id, body, position="bottom")
    if decision.mirror_today:
        client.mirror_node(
            root_id, _ensure_mirror_parent(client), position="top"
        )
    return root_id, decision.rule


def serve(
    client: WorkflowyClient,
    db: sqlite3.Connection,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    rules_path: Path | str,
    roadmap_dir: Path = DEFAULT_ROADMAP_DIR,
) -> int:
    del db
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("The capture bridge may only bind to localhost")
    rules = load_rules(rules_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "workflowy-bridge/0.2"

        def _reply(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(raw)

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or origin.startswith("chrome-extension://")

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
                return
            match = re.fullmatch(r"/roadmap/prompt/(\d{6})", self.path)
            if match:
                found = _roadmap_prompt_text(roadmap_dir, match.group(1))
                if not found:
                    self._reply(404, {"error": "prompt not found"})
                    return
                prompt_text, source = found
                self._reply(
                    200,
                    {
                        "prompt_id": match.group(1),
                        "prompt_text": prompt_text,
                        "source": source,
                    },
                )
                return
            match = re.fullmatch(r"/roadmap/fix-packet/(\d{6})", self.path)
            if match:
                packet = _roadmap_fix_packet(roadmap_dir, match.group(1))
                if not packet:
                    self._reply(404, {"error": "fix packet not found"})
                    return
                self._reply(200, packet)
                return
            self._reply(404, {"error": "not found"})

        def do_OPTIONS(self) -> None:
            if not self._origin_ok():
                self._reply(403, {"error": "origin rejected"})
                return
            self.send_response(204)
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/capture":
                self._reply(404, {"error": "not found"})
                return
            if not self._origin_ok():
                self._reply(403, {"error": "origin rejected"})
                return
            if self.headers.get_content_type() != "application/json":
                self._reply(415, {"error": "application/json required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, {"error": "invalid content length"})
                return
            if length <= 0 or length > 5_000_000:
                self._reply(413, {"error": "payload size rejected"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                node_id, rule = _capture_payload(client, payload, rules)
            except (ValueError, WorkflowyAPIError, OSError) as exc:
                self._reply(400, {"error": str(exc)})
                return
            self._reply(201, {"node_id": node_id, "rule": rule})

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("workflowy-bridge: " + (fmt % args) + "\n")

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"workflowy-bridge listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
