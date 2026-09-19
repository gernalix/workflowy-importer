from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from workflowy_importer import bridge_cli
from workflowy_importer.bridge import _roadmap_fix_packet, _roadmap_prompt_text
from workflowy_importer.cache import connect
from workflowy_importer.fix_packets import load_fix_packet, load_latest_terminal_outcome
from workflowy_importer.roadmap_bridge import (
    RoadmapPrompt,
    command_from_children,
    dashboard_group,
    mutation_for_command,
    prompt_name,
    read_roadmap_db,
    sync_roadmap,
)


def roadmap_bytes(status: str = "pending") -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
        conn = sqlite3.connect(handle.name)
        conn.executescript(
            """
            CREATE TABLE prompts(
              prompt_id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              status TEXT NOT NULL,
              project_name TEXT,
              repo TEXT,
              current_path TEXT NOT NULL,
              explanation TEXT,
              model TEXT,
              reasoning TEXT,
              queue_position INTEGER,
              created_at TEXT NOT NULL
            );
            CREATE TABLE prompt_materializations(
              prompt_id TEXT PRIMARY KEY,
              body TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL,
              actor TEXT NOT NULL
            );
            CREATE TABLE dependencies(
              prompt_id TEXT NOT NULL,
              depends_on_prompt_id TEXT NOT NULL
            );
            CREATE TABLE prompt_relations(
              from_prompt_id TEXT NOT NULL,
              to_prompt_id TEXT NOT NULL,
              relation_type TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO prompts VALUES(
               '123456','Parent',?,'Example','gernalix/example',
               'prompts/parent.md','Do parent','GPT-5.6 Luna','low',1,'2026-09-19T00:00:00Z'
            )""",
            (status,),
        )
        conn.execute(
            """INSERT INTO prompts VALUES(
               '654321','Child','pending','Example','gernalix/example',
               'prompts/child.md','Do child','GPT-5.6 Terra','medium',2,'2026-09-19T00:01:00Z'
            )"""
        )
        conn.execute(
            "INSERT INTO prompt_materializations VALUES(?,?,?,?,?)",
            ("123456", "PROMPT_ID=123456\nDo parent\n", "sha-parent", "2026-09-19T00:00:00Z", "test"),
        )
        conn.execute(
            "INSERT INTO prompt_materializations VALUES(?,?,?,?,?)",
            ("654321", "PROMPT_ID=654321\nDo child\n", "sha-child", "2026-09-19T00:01:00Z", "test"),
        )
        conn.execute("INSERT INTO dependencies VALUES('654321','123456')")
        conn.execute(
            "INSERT INTO prompt_relations VALUES('123456','654321','followup')"
        )
        conn.commit()
        conn.close()
        return Path(handle.name).read_bytes()


class FakeClient:
    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.counter = 0

    def export_nodes(self) -> list[dict]:
        return [dict(node) for node in self.nodes]

    def create_node(
        self,
        parent_id: str,
        name: str,
        layout_mode: str = "bullets",
        position: str = "bottom",
        note: str | None = None,
    ) -> str:
        del position
        self.counter += 1
        node_id = f"00000000-0000-0000-0000-{self.counter:012d}"
        self.nodes.append(
            {
                "id": node_id,
                "parent_id": parent_id,
                "name": name,
                "note": note,
                "data": {"layoutMode": layout_mode},
                "modifiedAt": 1_790_000_000,
            }
        )
        return node_id

    def update_node(
        self,
        node_id: str,
        name: str | None = None,
        *,
        note: str | None = None,
        layout_mode: str | None = None,
    ) -> None:
        for node in self.nodes:
            if node["id"] == node_id:
                if name is not None:
                    node["name"] = name
                if note is not None:
                    node["note"] = note
                if layout_mode is not None:
                    node.setdefault("data", {})["layoutMode"] = layout_mode
                return
        raise AssertionError(f"missing node {node_id}")

    def move_node(
        self,
        node_id: str,
        parent_id: str,
        position: str = "top",
    ) -> None:
        del position
        for node in self.nodes:
            if node["id"] == node_id:
                node["parent_id"] = parent_id
                return
        raise AssertionError(f"missing node {node_id}")


class RoadmapBridgeTests(unittest.TestCase):
    def test_local_bridge_reads_canonical_prompt_body_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "roadmap.sqlite").write_bytes(roadmap_bytes())
            found = _roadmap_prompt_text(root, "123456")
            self.assertIsNotNone(found)
            self.assertEqual("PROMPT_ID=123456\nDo parent\n", found[0])
            self.assertEqual("roadmap.sqlite", found[1])

    def test_local_bridge_reads_canonical_fix_packet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "roadmap.sqlite"
            db_path.write_bytes(roadmap_bytes("blocked"))
            conn = sqlite3.connect(db_path)
            conn.executescript(
                "CREATE TABLE analyses(analysis_id INTEGER PRIMARY KEY,prompt_id TEXT,analyzed_at TEXT,summary TEXT,source_ref TEXT);"
            )
            conn.execute(
                "INSERT INTO analyses VALUES(1,'123456','2026-09-19T01:00:00Z',?,?)",
                ('{"prompt_id":"123456","outcome":"BLOCKED"}', "codex-usage:cycle:hash"),
            )
            conn.commit()
            conn.close()
            self.assertEqual("BLOCKED", _roadmap_fix_packet(root, "123456")["outcome"])

    def test_cli_dispatches_roadmap_sync(self):
        args = bridge_cli.build_parser().parse_args(["roadmap-sync"])
        db = MagicMock()
        client = MagicMock()
        client_context = MagicMock()
        client_context.__enter__.return_value = client
        with patch.object(bridge_cli, "_db", return_value=db), patch.object(
            bridge_cli, "_client", return_value=client_context
        ), patch.object(bridge_cli, "sync_roadmap", return_value={"prompts": 2}) as sync:
            self.assertEqual(0, bridge_cli.run(args))
        sync.assert_called_once_with(
            client,
            db,
            parent="inbox",
            repository="gernalix/codex-roadmap",
            branch="main",
            roadmap_dir=Path("~/projects/codex-roadmap"),
        )

    def test_projection_contains_links_tags_and_dependencies(self):
        prompts = read_roadmap_db(roadmap_bytes())
        self.assertEqual(["123456", "654321"], [p.prompt_id for p in prompts])
        self.assertEqual(["123456"], prompts[1].dependencies)
        self.assertEqual(["654321"], prompts[0].dependents)
        self.assertEqual([("followup", "654321")], prompts[0].relations_out)
        self.assertEqual([("followup", "123456")], prompts[1].relations_in)

    def test_running_last_blocked_outcome_is_needs_fix_before_canonical_finish(self):
        prompt = read_roadmap_db(roadmap_bytes("running"))[0]
        prompt.last_outcome = "BLOCKED"
        self.assertEqual(
            "blocked",
            dashboard_group(prompt, {prompt.prompt_id: prompt}, pipeline=None),
        )

    def test_latest_local_terminal_outcome_uses_newest_finished_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            published = Path(tmp)
            for cycle, status, ended in (
                ("older", "BLOCKED", "2026-09-19T22:00:00Z"),
                ("newer", "PASS", "2026-09-19T22:05:00Z"),
            ):
                path = published / f"prompts/123456/cycles/{cycle}/metrics.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "prompt_id": "123456",
                            "status": status,
                            "cycle_key": cycle,
                            "timestamp_end_utc": ended,
                        }
                    ),
                    encoding="utf-8",
                )
            self.assertEqual(
                "PASS",
                load_latest_terminal_outcome("123456", published_root=published),
            )

    def test_local_blocked_result_updates_dashboard_even_when_writer_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            packet = {
                "schema": "codex-roadmap.fix-packet.v1",
                "prompt_id": "123456",
                "outcome": "BLOCKED",
                "blocker": "GitHub API rate limit blocks roadmap finalization.",
                "work_state": {},
                "next_action": "Retry the canonical mutation after the rate limit clears.",
                "report_ref": "codex-usage:cycle-rate-limit:hash-rate-limit",
            }

            result = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes("running"),
                observed_outcome_loader=lambda prompt_id: (
                    "BLOCKED" if prompt_id == "123456" else None
                ),
                fix_packet_loader=lambda prompt_id, outcome: (
                    packet
                    if prompt_id == "123456" and outcome == "BLOCKED"
                    else None
                ),
                submitter=lambda _doc, _key: (_ for _ in ()).throw(
                    RuntimeError("github_rate_limit")
                ),
            )

            prompt_node = next(
                node
                for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            needs_fix = next(
                node for node in client.nodes if str(node["name"]).startswith("Needs fix (")
            )
            group_names = {str(node["name"]) for node in client.nodes}
            self.assertEqual(needs_fix["id"], prompt_node["parent_id"])
            self.assertIn("Needs fix (1)", group_names)
            self.assertIn("Running (0)", group_names)
            self.assertIn("Stato canonico: running", prompt_node["note"])
            self.assertIn("Esito operativo Codex: BLOCKED", prompt_node["note"])
            self.assertIn("GitHub API rate limit", prompt_node["note"])
            self.assertEqual(1, result["warnings"])
            db.close()

    def test_command_is_strict_and_ambiguous_fails_closed(self):
        aliases = {
            "R": "running",
            "P": "PASS",
            "B": "BLOCKED",
            "F": "FAIL",
            "running": "running",
            "PASS": "PASS",
            "BLOCKED": "BLOCKED",
            "FAIL": "FAIL",
        }
        for name, expected in aliases.items():
            found = command_from_children([{"name": name, "id": "x"}])
            self.assertEqual(expected, found[0])
        self.assertIsNone(command_from_children([{"name": " P ", "id": "x"}]))
        self.assertIsNone(command_from_children([{"name": "pass", "id": "x"}]))
        with self.assertRaises(ValueError):
            command_from_children(
                [
                    {"name": "running", "id": "a"},
                    {"name": "PASS", "id": "b"},
                ]
            )

    def test_prompt_without_project_keeps_required_project_tag(self):
        prompt = RoadmapPrompt(
            prompt_id="123456",
            title="No project",
            status="unknown",
            project_name=None,
            repo=None,
            current_path="",
            explanation="",
            model=None,
            reasoning=None,
            queue_position=None,
            dependencies=[],
            dependents=[],
            relations_out=[],
            relations_in=[],
        )
        self.assertEqual("[123456] <b>No project</b>", prompt_name(prompt))

    def test_terminal_command_can_follow_pending_atomically(self):
        prompt = read_roadmap_db(roadmap_bytes())[0]
        ops = mutation_for_command(prompt, "PASS", pipeline_state="done")
        self.assertEqual(["status", "terminal_request"], [op["op"] for op in ops])
        self.assertEqual("completed", ops[-1]["status"])
        blocked = mutation_for_command(prompt, "BLOCKED")
        self.assertEqual("blocked", blocked[-1]["status"])
        failed = mutation_for_command(prompt, "FAIL")
        self.assertEqual("failed", failed[-1]["status"])

    def test_sync_creates_whole_projection_and_submits_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            submitted: list[tuple[dict, str]] = []

            first = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=lambda doc, key: submitted.append((doc, key)) or {"status": "ok"},
            )
            self.assertEqual(2, first["prompts"])
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            self.assertEqual("[123456] 🟢 <b>Parent</b>", prompt_node["name"])
            self.assertIn("🟢 Questo prompt è pronto.", prompt_node["note"])
            self.assertIn("🟠 Link Chrome mancante. Vuoi aggiungerlo?", prompt_node["note"])
            self.assertIn("🟠 Link Codex mancante. Vuoi aggiungerlo?", prompt_node["note"])
            self.assertIn("#status_pending", prompt_node["note"])
            self.assertIn("Sblocca:", prompt_node["note"])
            self.assertIn("Relazioni →:", prompt_node["note"])
            group_names = {str(node["name"]) for node in client.nodes}
            self.assertIn("Ready (1)", group_names)
            self.assertIn("Waiting (1)", group_names)
            self.assertIn("Running (0)", group_names)
            self.assertIn("Integration (0)", group_names)
            self.assertIn("Needs fix (0)", group_names)
            self.assertIn("Done (0)", group_names)
            self.assertIn("Archive (0)", group_names)
            root = next(node for node in client.nodes if node["name"] == "Codex")
            ready = next(node for node in client.nodes if node["name"] == "Ready (1)")
            self.assertEqual("h1", root["data"]["layoutMode"])
            self.assertEqual("h2", ready["data"]["layoutMode"])
            self.assertEqual("bullets", prompt_node["data"]["layoutMode"])

            client.create_node(prompt_node["id"], "R")
            second = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=lambda doc, key: submitted.append((doc, key)) or {"status": "ok"},
            )
            self.assertEqual(1, second["mutations_submitted"])
            self.assertEqual("status", submitted[-1][0]["operations"][0]["op"])
            self.assertEqual("running", submitted[-1][0]["operations"][0]["status"])
            db.close()


    def test_running_repo_task_moves_to_integration_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes("running"),
                submitter=lambda doc, key: {"status": "ok"},
                pipeline_status={
                    "123456": {
                        "pipeline_state": "integration",
                        "integration_state": "checks-pending",
                        "pr_number": 27,
                        "pr_url": "https://github.com/gernalix/example/pull/27",
                        "queue_position": 1,
                        "queue_size": 2,
                    }
                },
                ccs_bindings={
                    "123456": {
                        "context_id": "ctx-1",
                        "url": "https://chatgpt.com/c/example",
                        "codex_thread": "thread-1",
                        "codex_deep_link": "codex://threads/thread-1",
                    }
                },
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            integration = next(
                node for node in client.nodes
                if str(node["name"]).startswith("Integration (")
            )
            self.assertEqual(integration["id"], prompt_node["parent_id"])
            self.assertIn("📋 Copia:", prompt_node["note"])
            self.assertIn("🌐 Chrome URL: https://chatgpt.com/c/example", prompt_node["note"])
            self.assertIn("↗ Apri Chrome:", prompt_node["note"])
            self.assertIn("🧠 Codex URL: codex://threads/thread-1", prompt_node["note"])
            self.assertIn("Link: 🌐 Chrome ✅ · 🧠 Codex ✅", prompt_node["note"])
            self.assertNotIn("🔗 Completa link:", prompt_node["note"])
            self.assertIn(
                "🌐 Ricollega Chrome: http://127.0.0.1:43817/ui/prompt/123456/bind-chrome",
                prompt_node["note"],
            )
            self.assertIn(
                "🧠 Ricollega Codex: http://127.0.0.1:43817/ui/prompt/123456/bind-codex",
                prompt_node["note"],
            )
            self.assertIn("🔎 Verify: http://127.0.0.1:43817/ui/prompt/123456/verify", prompt_node["note"])
            self.assertIn("Coda integrazione: 1/2", prompt_node["note"])
            db.close()

    def test_incomplete_binding_exposes_late_link_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=lambda doc, key: {"status": "ok"},
                ccs_bindings={
                    "123456": {
                        "context_id": "ctx-1",
                        "url": "https://chatgpt.com/c/example",
                        "codex_thread": None,
                        "codex_deep_link": None,
                    }
                },
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            self.assertIn(
                "🔗 Completa link: http://127.0.0.1:43817/ui/prompt/123456/bind",
                prompt_node["note"],
            )
            self.assertIn("Link: 🌐 Chrome ✅ · 🧠 Codex ❌", prompt_node["note"])
            self.assertIn("Link mancanti: Codex", prompt_node["note"])
            self.assertIn(
                "🌐 Ricollega Chrome: http://127.0.0.1:43817/ui/prompt/123456/bind-chrome",
                prompt_node["note"],
            )
            self.assertIn(
                "🧠 Associa Codex: http://127.0.0.1:43817/ui/prompt/123456/bind-codex",
                prompt_node["note"],
            )
            self.assertIn("🌐 Chrome URL: https://chatgpt.com/c/example", prompt_node["note"])
            self.assertNotIn("🧠 Codex URL:", prompt_node["note"])
            db.close()

    def test_codex_first_binding_is_visible_and_chrome_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=lambda doc, key: {"status": "ok"},
                ccs_bindings={
                    "123456": {
                        "context_id": None,
                        "url": None,
                        "codex_thread": "thread-1",
                        "codex_deep_link": "codex://threads/thread-1",
                    }
                },
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            self.assertIn("Link: 🌐 Chrome ❌ · 🧠 Codex ✅", prompt_node["note"])
            self.assertIn("Link mancanti: Chrome", prompt_node["note"])
            self.assertIn(
                "🌐 Associa Chrome: http://127.0.0.1:43817/ui/prompt/123456/bind-chrome",
                prompt_node["note"],
            )
            self.assertIn(
                "🧠 Ricollega Codex: http://127.0.0.1:43817/ui/prompt/123456/bind-codex",
                prompt_node["note"],
            )
            self.assertIn("🧠 Codex URL: codex://threads/thread-1", prompt_node["note"])
            db.close()

    def test_blocked_prompt_talks_in_plain_language_from_fix_packet(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            packet = {
                "prompt_id": "123456",
                "outcome": "BLOCKED",
                "blocker": "Runtime heartbeat is stale; GNOME companion did not answer.",
                "next_action": "Restart the local companion and run Verify again.",
                "report_ref": "codex-usage:test:abcd",
            }
            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes("blocked"),
                submitter=lambda doc, key: {"status": "ok"},
                fix_packet_loader=lambda prompt_id, outcome: (
                    packet if prompt_id == "123456" and outcome == "BLOCKED" else None
                ),
                ccs_bindings={
                    "123456": {
                        "context_id": "ctx-1",
                        "url": "https://chatgpt.com/c/example",
                        "codex_thread": "thread-1",
                        "codex_deep_link": "codex://threads/thread-1",
                    }
                },
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            self.assertIn("🔴", prompt_node["name"])
            self.assertIn("<b>Parent</b>", prompt_node["name"])
            self.assertEqual("h3", prompt_node["data"]["layoutMode"])
            self.assertIn(
                "🔴 Codex si è fermato prima di completare il lavoro con PASS.",
                prompt_node["note"],
            )
            self.assertIn(
                "💬 In breve: Runtime heartbeat is stale; GNOME companion did not answer.",
                prompt_node["note"],
            )
            self.assertIn(
                "👉 Prossimo passo consigliato: Restart the local companion and run Verify again.",
                prompt_node["note"],
            )
            db.close()

    def test_blocked_without_packet_refuses_to_invent_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes("blocked"),
                submitter=lambda doc, key: {"status": "ok"},
                fix_packet_loader=lambda _prompt_id, _outcome: None,
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            self.assertIn(
                "Non ho ancora un riassunto affidabile del motivo",
                prompt_node["note"],
            )
            db.close()

    def test_terminal_blocked_packet_publishes_without_manual_b_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            submitted: list[tuple[dict, str]] = []
            packet = {
                "schema": "codex-roadmap.fix-packet.v1",
                "prompt_id": "123456",
                "outcome": "BLOCKED",
                "blocker": "One concrete blocker.",
                "work_state": {},
                "next_action": "Do one concrete repair.",
                "report_ref": "codex-usage:cycle-x:hash-x",
            }
            result = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes("blocked"),
                submitter=lambda doc, key: submitted.append((doc, key)) or {"status": "ok"},
                fix_packet_loader=lambda prompt_id, outcome: (
                    packet if prompt_id == "123456" and outcome == "BLOCKED" else None
                ),
            )
            self.assertEqual(1, result["fix_packets_submitted"])
            self.assertTrue(
                any(
                    doc["actor"] == "workflowy-fix-packet"
                    for doc, _key in submitted
                )
            )
            db.close()

    def test_repo_pass_override_requires_integrated_pipeline(self):
        prompt = read_roadmap_db(roadmap_bytes("running"))[0]
        with self.assertRaises(ValueError):
            mutation_for_command(prompt, "PASS", pipeline_state="integration")
        ops = mutation_for_command(prompt, "PASS", pipeline_state="done")
        self.assertEqual("completed", ops[-1]["status"])

    def test_short_blocked_creates_fix_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            submitted: list[tuple[dict, str]] = []
            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=lambda doc, key: submitted.append((doc, key)) or {"status": "ok"},
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            client.create_node(prompt_node["id"], "B")
            result = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=lambda doc, key: submitted.append((doc, key)) or {"status": "ok"},
            )
            self.assertEqual(1, result["mutations_submitted"])
            self.assertEqual("blocked", submitted[-1][0]["operations"][-1]["status"])
            self.assertTrue(
                any(
                    node["parent_id"] == prompt_node["id"]
                    and node["name"] == "FIX B · 123456 #needs_fix"
                    for node in client.nodes
                )
            )
            db.close()

    def test_blocked_and_failed_reports_publish_one_sanitized_packet(self):
        for command, outcome in (("B", "BLOCKED"), ("F", "FAIL")):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                published = Path(tmp) / "published"
                metrics_path = published / "prompts/123456/cycles/report-1/metrics.json"
                metrics_path.parent.mkdir(parents=True)
                metrics_path.write_text(
                    json.dumps(
                        {
                            "prompt_id": "123456",
                            "status": outcome,
                            "cycle_key": "report-1",
                            "timestamp_end_utc": "2026-09-19T01:00:00Z",
                            "final_response_redacted": (
                                f"RESULT={outcome}\n"
                                "Blocker: token=super-secret failed at /private/repo/file.py\n"
                                "Branch: task/123456\nPR #42\ncommit abcdef1\n"
                                "Next action: repair the parser fixture."
                            ),
                        }
                    ),
                    encoding="utf-8",
                )
                packet = load_fix_packet("123456", outcome, published_root=published)
                self.assertEqual(outcome, packet["outcome"])
                self.assertIn("<redacted>", packet["blocker"])
                self.assertNotIn("/private/repo", packet["blocker"])
                self.assertEqual("task/123456", packet["work_state"]["branch"])
                self.assertEqual("#42", packet["work_state"]["pr"])

                db = connect(Path(tmp) / "cache.sqlite")
                client = FakeClient()
                submitted: list[tuple[dict, str]] = []
                submit = lambda doc, key: submitted.append((doc, key)) or {"status": "ok"}
                sync_roadmap(client, db, raw_roadmap_db=roadmap_bytes(), submitter=submit)
                prompt_node = next(node for node in client.nodes if str(node["name"]).startswith("[123456]"))
                client.create_node(prompt_node["id"], command)
                first = sync_roadmap(
                    client,
                    db,
                    raw_roadmap_db=roadmap_bytes(),
                    submitter=submit,
                    fix_packet_loader=lambda _id, _outcome: packet,
                )
                self.assertEqual(1, first["fix_packets_submitted"])
                self.assertEqual(2, len(submitted))
                self.assertEqual("analysis", submitted[-1][0]["operations"][0]["op"])
                second = sync_roadmap(
                    client,
                    db,
                    raw_roadmap_db=roadmap_bytes("blocked" if outcome == "BLOCKED" else "failed"),
                    submitter=submit,
                    fix_packet_loader=lambda _id, _outcome: packet,
                )
                self.assertEqual(0, second["fix_packets_submitted"])
                self.assertEqual(2, len(submitted))
                db.close()


if __name__ == "__main__":
    unittest.main()
