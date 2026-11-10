from __future__ import annotations

import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .api import WorkflowyAPIError, WorkflowyClient
from .automation import classify_with_optional_command, load_rules
from .capture_auth import AUTH_HEADERS, CaptureAuthError, CaptureAuthority

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
    capture_token: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    rules_path: Path | str,
) -> int:
    del db
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("The capture bridge may only bind to localhost")
    rules = load_rules(rules_path)
    if capture_token == getattr(client, "api_key", None):
        raise CaptureAuthError()
    # Validate before opening a listener. The actual bound endpoint is set below.
    authority = CaptureAuthority(capture_token, "unbound")

    class Handler(BaseHTTPRequestHandler):
        server_version = "workflowy-bridge/0.2"

        def setup(self) -> None:
            self.request.settimeout(10)
            super().setup()

        def _reply(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(raw)

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or origin.startswith("chrome-extension://")

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/challenge":
                if not self._origin_ok():
                    self._reply(403, {"error": "origin rejected"})
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    if set(query) != {"client_nonce"} or len(query["client_nonce"]) != 1:
                        raise CaptureAuthError()
                    payload = authority.issue(query["client_nonce"][0])
                except CaptureAuthError:
                    self._reply(401, {"error": "capture authorization failed"})
                    return
                self._reply(200, payload)
                return
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
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
            self.send_header("Access-Control-Allow-Headers", "content-type, " + ", ".join(AUTH_HEADERS))
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/capture":
                self._reply(404, {"error": "not found"})
                return
            if not self._origin_ok():
                self._reply(403, {"error": "origin rejected"})
                return
            try:
                receipt = authority.authorize(self.headers)
            except CaptureAuthError:
                self._reply(401, {"error": "capture authorization failed"})
                return
            if self.headers.get_content_type() != "application/octet-stream":
                self._reply(415, {"error": "encrypted capture required"})
                return
            if len(self.headers.get_all("Content-Length", [])) != 1 or self.headers.get("Transfer-Encoding"):
                self._reply(400, {"error": "invalid content length"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, {"error": "invalid content length"})
                return
            if length < 16 or length > 5_000_016:
                self._reply(413, {"error": "payload size rejected"})
                return
            try:
                body = self.rfile.read(length)
                payload = json.loads(authority.decrypt(receipt, body))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                node_id, rule = _capture_payload(client, payload, rules)
            except (ValueError, WorkflowyAPIError, OSError):
                self._reply(400, {"error": "capture failed; remote outcome may be uncertain"})
                return
            self._reply(201, {"node_id": node_id, "rule": rule})

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("workflowy-bridge: " + (fmt % args) + "\n")

    server = ThreadingHTTPServer((host, port), Handler)
    address, bound_port = server.server_address[:2]
    authority.endpoint = f"http://[{address}]:{bound_port}" if ":" in address else f"http://{address}:{bound_port}"
    print(f"workflowy-bridge listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
