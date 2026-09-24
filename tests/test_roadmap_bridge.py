from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from workflowy_importer import bridge_cli
from workflowy_importer.bridge import _roadmap_fix_packet, _roadmap_prompt_text
from workflowy_importer.api import WorkflowyAPIError
from workflowy_importer.cache import connect
from workflowy_importer.fix_packets import load_fix_packet, load_latest_terminal_outcome
from workflowy_importer.roadmap_bridge import (
    RoadmapPrompt,
    _integration_dashboard_note,
    _integrator_progress_line,
    _notify_ready_entries,
    _send_ready_telegram,
    command_from_children,
    dashboard_group,
    mutation_for_command,
    prompt_name,
    prompt_note,
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


def roadmap_order_bytes() -> bytes:
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
            CREATE TABLE dependencies(
              prompt_id TEXT NOT NULL,
              depends_on_prompt_id TEXT NOT NULL
            );
            CREATE TABLE prompt_relations(
              from_prompt_id TEXT NOT NULL,
              to_prompt_id TEXT NOT NULL,
              relation_type TEXT NOT NULL
            );
            CREATE TABLE prompt_tags(
              prompt_id TEXT NOT NULL,
              tag TEXT NOT NULL
            );
            """
        )
        rows = [
            ("111111", "Ready first", "pending", 1),
            ("222222", "Waiting dependency", "pending", 2),
            ("333333", "Ready second", "pending", 3),
            ("444444", "Waiting manual", "pending", 4),
            ("999999", "Running prerequisite", "running", 5),
        ]
        for prompt_id, title, status, queue_position in rows:
            conn.execute(
                """INSERT INTO prompts VALUES(
                   ?,?,?, 'Example','gernalix/example',
                   ?,?,'GPT-5.6 Luna','low',?,'2026-09-19T00:00:00Z'
                )""",
                (
                    prompt_id,
                    title,
                    status,
                    f"prompts/{prompt_id}.md",
                    title,
                    queue_position,
                ),
            )
        conn.execute("INSERT INTO dependencies VALUES('222222','999999')")
        conn.execute(
            "INSERT INTO prompt_tags VALUES('444444','manual-prerequisite:revoke-pat')"
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
        index = next(
            (i for i, node in enumerate(self.nodes) if node["id"] == node_id),
            None,
        )
        if index is None:
            raise AssertionError(f"missing node {node_id}")
        node = self.nodes.pop(index)
        node["parent_id"] = parent_id
        sibling_indexes = [
            i
            for i, sibling in enumerate(self.nodes)
            if sibling.get("parent_id") == parent_id
        ]
        if position == "top":
            insert_at = sibling_indexes[0] if sibling_indexes else len(self.nodes)
        elif position == "bottom":
            insert_at = sibling_indexes[-1] + 1 if sibling_indexes else len(self.nodes)
        else:
            raise AssertionError(f"unsupported position {position}")
        self.nodes.insert(insert_at, node)


    def get_node(self, node_id: str) -> dict:
        for node in self.nodes:
            if node["id"] == node_id:
                return dict(node)
        raise WorkflowyAPIError("missing", status_code=404)

    def delete_node(self, node_id: str) -> None:
        before = len(self.nodes)
        self.nodes = [node for node in self.nodes if node["id"] != node_id]
        if len(self.nodes) == before:
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

    def test_ready_and_waiting_follow_canonical_queue_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            raw = roadmap_order_bytes()
            sync_roadmap(
                client,
                db,
                raw_roadmap_db=raw,
                submitter=lambda doc, key: {"status": "ok"},
            )

            ready = next(
                node for node in client.nodes
                if str(node["name"]).startswith("Ready (")
            )
            waiting = next(
                node for node in client.nodes
                if str(node["name"]).startswith("Waiting (")
            )

            # Simulate a user/API order drift without changing canonical DB order.
            ready_children = [
                node
                for node in client.nodes
                if node.get("parent_id") == ready["id"]
                and str(node.get("name") or "").startswith("[")
            ]
            first_index = client.nodes.index(ready_children[0])
            second_index = client.nodes.index(ready_children[1])
            client.nodes[first_index], client.nodes[second_index] = (
                client.nodes[second_index],
                client.nodes[first_index],
            )

            result = sync_roadmap(
                client,
                db,
                raw_roadmap_db=raw,
                submitter=lambda doc, key: {"status": "ok"},
            )

            ready_ids = [
                str(node["name"])[1:7]
                for node in client.nodes
                if node.get("parent_id") == ready["id"]
                and str(node.get("name") or "").startswith("[")
            ]
            waiting_ids = [
                str(node["name"])[1:7]
                for node in client.nodes
                if node.get("parent_id") == waiting["id"]
                and str(node.get("name") or "").startswith("[")
            ]
            self.assertEqual(["111111", "333333"], ready_ids)
            self.assertEqual(["222222", "444444"], waiting_ids)
            self.assertGreater(result["reordered"], 0)
            db.close()

    def test_ready_notifications_fire_once_per_entry_and_retry_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            prompts = read_roadmap_db(roadmap_order_bytes())
            prompt_by_id = {prompt.prompt_id: prompt for prompt in prompts}
            groups = {
                prompt.prompt_id: dashboard_group(prompt, prompt_by_id, pipeline=None)
                for prompt in prompts
            }
            delivered: list[str] = []

            # First sync establishes a quiet baseline for already-Ready tasks.
            self.assertEqual(
                (0, 0),
                _notify_ready_entries(
                    db,
                    prompts,
                    groups,
                    lambda prompt: delivered.append(prompt.prompt_id) or True,
                ),
            )
            self.assertEqual([], delivered)

            # A task entering Ready is notified once.
            groups["444444"] = "ready"
            self.assertEqual(
                (1, 0),
                _notify_ready_entries(
                    db,
                    prompts,
                    groups,
                    lambda prompt: delivered.append(prompt.prompt_id) or True,
                ),
            )
            self.assertEqual(["444444"], delivered)
            self.assertEqual(
                (0, 0),
                _notify_ready_entries(
                    db,
                    prompts,
                    groups,
                    lambda prompt: delivered.append(prompt.prompt_id) or True,
                ),
            )
            self.assertEqual(["444444"], delivered)

            # Leaving and re-entering Ready is a new transition. A failed
            # delivery is not acknowledged and is retried on the next sync.
            groups["444444"] = "waiting"
            self.assertEqual(
                (0, 0),
                _notify_ready_entries(db, prompts, groups, lambda prompt: True),
            )
            groups["444444"] = "ready"
            self.assertEqual(
                (0, 1),
                _notify_ready_entries(db, prompts, groups, lambda prompt: False),
            )
            self.assertEqual(
                (1, 0),
                _notify_ready_entries(
                    db,
                    prompts,
                    groups,
                    lambda prompt: delivered.append(prompt.prompt_id) or True,
                ),
            )
            self.assertEqual(["444444", "444444"], delivered)
            db.close()

    @patch("workflowy_importer.roadmap_bridge.subprocess.run")
    def test_ready_telegram_uses_shared_notifier_and_project_id(self, mocked_run):
        mocked_run.return_value = MagicMock(returncode=0)
        prompt = read_roadmap_db(roadmap_bytes())[0]
        with tempfile.TemporaryDirectory() as tmp:
            credentials = Path(tmp) / "telegram.env"
            credentials.write_text("placeholder=not-read-by-test\n", encoding="utf-8")
            with patch(
                "workflowy_importer.roadmap_bridge._telegram_credentials_path",
                return_value=credentials,
            ):
                self.assertTrue(_send_ready_telegram(prompt))

        command = mocked_run.call_args.args[0]
        env = mocked_run.call_args.kwargs["env"]
        self.assertEqual([sys.executable, "-m", "telegram_notify"], command[:3])
        self.assertIn("123456", command[3])
        self.assertIn("Parent", command[4])
        self.assertEqual("96", env["TELEGRAM_PROJECT_ID"])
        self.assertEqual(str(credentials), env["TELEGRAM_NOTIFY_CONFIG"])

    def test_manual_prerequisite_is_waiting_not_ready(self):
        prompts = read_roadmap_db(roadmap_order_bytes())
        prompt_by_id = {prompt.prompt_id: prompt for prompt in prompts}
        prompt = prompt_by_id["444444"]
        self.assertEqual(["revoke-pat"], prompt.manual_prerequisites)
        self.assertEqual(
            "waiting",
            dashboard_group(prompt, prompt_by_id, pipeline=None),
        )

    def test_running_last_blocked_outcome_is_needs_fix_before_canonical_finish(self):
        prompt = next(
            prompt
            for prompt in read_roadmap_db(roadmap_bytes("running"))
            if prompt.prompt_id == "123456"
        )
        prompt.last_outcome = "BLOCKED"
        self.assertEqual(
            "blocked",
            dashboard_group(prompt, {prompt.prompt_id: prompt}, pipeline=None),
        )

    def test_blocked_prompt_with_active_followup_is_archived(self):
        prompts = read_roadmap_db(roadmap_bytes("blocked"))
        prompt_by_id = {prompt.prompt_id: prompt for prompt in prompts}
        self.assertEqual(
            "unknown",
            dashboard_group(prompt_by_id["123456"], prompt_by_id, pipeline=None),
        )

    def test_blocked_prompt_with_resolved_by_successor_is_archived(self):
        source = RoadmapPrompt(
            prompt_id="111111",
            title="Source",
            status="blocked",
            project_name="Example",
            repo="gernalix/example",
            current_path="falliti/source.md",
            explanation="",
            model=None,
            reasoning=None,
            queue_position=None,
            dependencies=[],
            dependents=[],
            relations_out=[("resolved_by", "222222")],
            relations_in=[],
        )
        successor = RoadmapPrompt(
            prompt_id="222222",
            title="Successor",
            status="completed",
            project_name="Example",
            repo="gernalix/example",
            current_path="completed/successor.md",
            explanation="",
            model=None,
            reasoning=None,
            queue_position=None,
            dependencies=[],
            dependents=[],
            relations_out=[],
            relations_in=[("resolved_by", "111111")],
        )
        prompt_by_id = {item.prompt_id: item for item in (source, successor)}
        self.assertEqual(
            "unknown",
            dashboard_group(source, prompt_by_id, pipeline=None),
        )

    def test_blocked_leaf_remains_needs_fix(self):
        prompt = next(
            prompt
            for prompt in read_roadmap_db(roadmap_bytes("blocked"))
            if prompt.prompt_id == "123456"
        )
        prompt.relations_out = []
        self.assertEqual(
            "blocked",
            dashboard_group(prompt, {prompt.prompt_id: prompt}, pipeline=None),
        )

    def test_anonymous_historical_blocked_prompt_is_archived_even_with_materialization(self):
        prompt = RoadmapPrompt(
            prompt_id="123456",
            title="Historical",
            status="blocked",
            project_name=None,
            repo=None,
            current_path="falliti/historical.md",
            explanation="",
            model=None,
            reasoning=None,
            queue_position=None,
            dependencies=[],
            dependents=[],
            relations_out=[],
            relations_in=[],
        )
        self.assertEqual(
            "unknown",
            dashboard_group(prompt, {prompt.prompt_id: prompt}, pipeline=None),
        )

    def test_blocked_prompt_without_materialization_is_archived(self):
        prompt = RoadmapPrompt(
            prompt_id="123456",
            title="Historical",
            status="blocked",
            project_name="Example",
            repo="gernalix/example",
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
        self.assertEqual(
            "unknown",
            dashboard_group(prompt, {prompt.prompt_id: prompt}, pipeline=None),
        )

    def test_blocked_chain_with_running_descendant_is_archived(self):
        source = RoadmapPrompt(
            prompt_id="111111",
            title="Source",
            status="blocked",
            project_name="Example",
            repo="gernalix/example",
            current_path="falliti/source.md",
            explanation="",
            model=None,
            reasoning=None,
            queue_position=None,
            dependencies=[],
            dependents=[],
            relations_out=[("fix", "222222")],
            relations_in=[],
        )
        middle = RoadmapPrompt(
            prompt_id="222222",
            title="Middle",
            status="blocked",
            project_name="Example",
            repo="gernalix/example",
            current_path="falliti/middle.md",
            explanation="",
            model=None,
            reasoning=None,
            queue_position=None,
            dependencies=[],
            dependents=[],
            relations_out=[("followup", "333333")],
            relations_in=[("fix", "111111")],
        )
        live = RoadmapPrompt(
            prompt_id="333333",
            title="Live",
            status="running",
            project_name="Example",
            repo="gernalix/example",
            current_path="prompts/live.md",
            explanation="",
            model=None,
            reasoning=None,
            queue_position=None,
            dependencies=[],
            dependents=[],
            relations_out=[],
            relations_in=[("followup", "222222")],
        )
        prompt_by_id = {item.prompt_id: item for item in (source, middle, live)}
        self.assertEqual(
            "unknown",
            dashboard_group(source, prompt_by_id, pipeline=None),
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

    def test_local_telemetry_does_not_override_canonical_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            result = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes("running"),
                observed_outcome_loader=lambda prompt_id: (
                    "BLOCKED" if prompt_id == "123456" else None
                ),
                submitter=lambda _doc, _key: (_ for _ in ()).throw(
                    RuntimeError("should_not_submit")
                ),
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            running = next(
                node for node in client.nodes if str(node["name"]).startswith("Running (")
            )
            self.assertEqual(running["id"], prompt_node["parent_id"])
            self.assertIn("<b>Dettagli</b>: ID 123456 · stato running", prompt_node["note"])
            self.assertNotIn("<b>Esito operativo</b>", prompt_node["note"])
            self.assertEqual(0, result["warnings"])
            self.assertEqual(0, result["mutations_submitted"])
            db.close()


    def test_manual_lifecycle_commands_are_disabled(self):
        for name in ("R", "P", "B", "F", "running", "PASS", "BLOCKED", "FAIL"):
            self.assertIsNone(command_from_children([{"name": name, "id": "x"}]))


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

    def test_plain_explanation_is_prominent_and_apostrophes_stay_readable(self):
        prompt = RoadmapPrompt(
            prompt_id="123456",
            title="Distribuire l'hardening",
            status="pending",
            project_name="Example",
            repo="gernalix/example",
            current_path="prompts/example.md",
            explanation="Controlla che l'aggiornamento funzioni davvero sull'installazione reale.",
            model="GPT-5.6 Luna",
            reasoning="low",
            queue_position=1,
            dependencies=[],
            dependents=[],
            relations_out=[],
            relations_in=[],
            chat_guidance="Stessa chat di 357862",
        )
        name = prompt_name(prompt, "ready")
        note = prompt_note(
            prompt,
            {"123456": "node-1"},
            repository="gernalix/codex-roadmap",
            branch="main",
            group="ready",
        )
        self.assertEqual("[123456] 🟢 <b>Distribuire l'hardening</b>", name)
        self.assertIn(
            "💡 <b>In parole semplici</b>: Controlla che l'aggiornamento funzioni davvero sull'installazione reale.",
            note,
        )
        self.assertIn(
            "💬 <b>Chat Codex</b>: ↩️ Chat preesistente · Stessa chat di 357862",
            note,
        )
        self.assertLess(
            note.index("<b>In parole semplici</b>"),
            note.index("<b>Chat Codex</b>"),
        )
        self.assertLess(
            note.index("<b>Chat Codex</b>"),
            note.index("<b>Azioni</b>"),
        )

        prompt.chat_guidance = "Nuova chat Codex; continua da zero"
        new_chat_note = prompt_note(
            prompt,
            {"123456": "node-1"},
            repository="gernalix/codex-roadmap",
            branch="main",
            group="ready",
        )
        self.assertIn(
            "💬 <b>Chat Codex</b>: 🆕 Nuova chat · Nuova chat Codex; continua da zero",
            new_chat_note,
        )
        self.assertNotIn("&#x27;", name)
        self.assertNotIn("&#x27;", note)

    def test_direct_manual_lifecycle_mutation_is_rejected(self):
        prompt = read_roadmap_db(roadmap_bytes())[0]
        for command in ("running", "PASS", "BLOCKED", "FAIL"):
            with self.assertRaisesRegex(ValueError, "workflowy_lifecycle_commands_disabled"):
                mutation_for_command(prompt, command, pipeline_state="done")


    def test_sync_creates_projection_but_child_status_commands_are_inert(self):
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
            self.assertIn("🟢 Pronto all'avvio.", prompt_node["note"])
            self.assertIn("<b>Lifecycle</b>: stato sola lettura", prompt_node["note"])
            self.assertNotIn("Esito manuale", prompt_node["note"])
            client.create_node(prompt_node["id"], "R")
            second = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=lambda doc, key: submitted.append((doc, key)) or {"status": "ok"},
            )
            self.assertEqual(0, second["mutations_submitted"])
            self.assertEqual([], submitted)
            db.close()


    def test_sync_recovers_lost_mappings_and_deduplicates_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            submit = lambda doc, key: {"status": "ok"}

            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=submit,
            )
            ready = next(
                node for node in client.nodes
                if str(node["name"]).startswith("Ready (")
            )
            parent = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            duplicate_id = client.create_node(
                ready["id"],
                parent["name"],
                note=parent["note"],
            )
            client.create_node(duplicate_id, "R")

            # Simulate a lost/recreated local mapping cache: the projector must
            # rediscover the existing dashboard rather than append another copy.
            db.execute(
                "DELETE FROM mappings WHERE namespace=?",
                ("codex-roadmap",),
            )
            db.commit()

            result = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=submit,
            )

            prompt_nodes = [
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            ]
            self.assertEqual(1, len(prompt_nodes))
            self.assertEqual(1, result["duplicates_deleted"])
            self.assertTrue(
                any(
                    child["parent_id"] == prompt_nodes[0]["id"]
                    and child["name"] == "R"
                    for child in client.nodes
                )
            )
            self.assertEqual(
                1,
                len([node for node in client.nodes if node["name"] == "Codex"]),
            )
            self.assertEqual(
                1,
                len(
                    [
                        node for node in client.nodes
                        if str(node["name"]).startswith("Ready (")
                    ]
                ),
            )
            db.close()

    def test_mapped_node_missing_from_export_is_verified_before_recreate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            client = FakeClient()
            submit = lambda doc, key: {"status": "ok"}

            sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=submit,
            )
            prompt_node = next(
                node for node in client.nodes
                if str(node["name"]).startswith("[123456]")
            )
            prompt_node_id = prompt_node["id"]
            real_export = client.export_nodes
            client.export_nodes = lambda: [
                node
                for node in real_export()
                if node["id"] != prompt_node_id
            ]

            result = sync_roadmap(
                client,
                db,
                raw_roadmap_db=roadmap_bytes(),
                submitter=submit,
            )

            self.assertEqual(0, result["created"])
            self.assertEqual(
                1,
                len(
                    [
                        node for node in client.nodes
                        if str(node["name"]).startswith("[123456]")
                    ]
                ),
            )
            db.close()

    def test_integrator_progress_uses_real_pipeline_phases(self):
        self.assertIn(
            "████░░░░░░ 40% · Controlli CI",
            _integrator_progress_line(
                {
                    "pipeline_state": "integration",
                    "integration_state": "checks-pending",
                    "queue_position": 2,
                    "queue_size": 4,
                }
            ),
        )
        self.assertIn(
            "██████████ 100% · Completato",
            _integrator_progress_line(
                {"pipeline_state": "done", "integration_state": "merged"}
            ),
        )
        note = _integration_dashboard_note(
            {
                "123456": {
                    "pipeline_state": "integration",
                    "integration_state": "merge-wait",
                    "pr_number": 27,
                    "queue_position": 1,
                    "queue_size": 2,
                    "integration_reason": "mergeable-conflicting",
                },
                "654321": {
                    "pipeline_state": "needs-fix",
                    "integration_state": "checks-failed",
                    "pr_number": 28,
                },
                "999999": {
                    "pipeline_state": "done",
                    "integration_state": "merged",
                },
            }
        )
        self.assertIn("<b>Integrator</b>: 2 task · 1 in corso · 1 da correggere", note)
        self.assertIn("123456 · ████████░░ 80% · Merge · PR #27 · coda 1/2", note)
        self.assertIn("mergeable-conflicting", note)
        self.assertIn("654321 · ████░░░░░░ 40% · Controlli CI · PR #28", note)
        self.assertNotIn("999999", note)

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
            self.assertIn("<b>Azioni</b>:", prompt_node["note"])
            self.assertIn(
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/launch">🚀 Avvia</a>',
                prompt_node["note"],
            )
            self.assertIn(
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/copy">📋 Copia prompt</a>',
                prompt_node["note"],
            )
            self.assertIn(
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/verify">🔎 Verifica</a>',
                prompt_node["note"],
            )
            self.assertIn(
                '<b>Collegamenti</b>: 🌐 Chrome ✅ '
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/chrome">Apri Chrome</a> · '
                '🧠 Codex ✅ '
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/codex">Apri Codex</a>',
                prompt_node["note"],
            )
            self.assertIn(
                '<b>Pipeline</b>: checks-pending · '
                '<a href="https://github.com/gernalix/example/pull/27">PR</a> · coda 1/2',
                prompt_node["note"],
            )
            self.assertIn(
                "<b>Integrator</b>: ████░░░░░░ 40% · Controlli CI · coda 1/2",
                prompt_node["note"],
            )
            self.assertIn(
                "<b>Integrator</b>: 1 task · 1 in corso · 0 da correggere",
                integration["note"],
            )
            self.assertIn(
                "123456 · ████░░░░░░ 40% · Controlli CI · PR #27 · coda 1/2",
                integration["note"],
            )
            self.assertNotIn("Chrome URL:", prompt_node["note"])
            self.assertNotIn("Codex URL:", prompt_node["note"])
            self.assertNotIn("/bind-chrome", prompt_node["note"])
            self.assertNotIn("/bind-codex", prompt_node["note"])
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
                '<b>Collegamenti</b>: 🌐 Chrome ✅ '
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/chrome">Apri Chrome</a> · '
                '🧠 Codex ❌ '
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/bind-codex">Associa Codex</a>',
                prompt_node["note"],
            )
            self.assertNotIn("Link mancanti:", prompt_node["note"])
            self.assertNotIn("Chrome URL:", prompt_node["note"])
            self.assertNotIn("Codex URL:", prompt_node["note"])
            self.assertNotIn("/ui/prompt/123456/bind\"", prompt_node["note"])
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
            self.assertIn(
                '<b>Collegamenti</b>: 🌐 Chrome ❌ '
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/bind-chrome">Associa Chrome</a> · '
                '🧠 Codex ✅ '
                '<a href="http://127.0.0.1:43817/ui/prompt/123456/codex">Apri Codex</a>',
                prompt_node["note"],
            )
            self.assertNotIn("Link mancanti:", prompt_node["note"])
            self.assertNotIn("Chrome URL:", prompt_node["note"])
            self.assertNotIn("Codex URL:", prompt_node["note"])
            db.close()

    def test_archived_blocked_prompt_preserves_plain_language_fix_packet(self):
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
            self.assertIn("⚪", prompt_node["name"])
            self.assertIn("<b>Parent</b>", prompt_node["name"])
            self.assertEqual("bullets", prompt_node["data"]["layoutMode"])
            self.assertIn(
                "🔴 BLOCKED · Codex si è fermato prima del PASS.",
                prompt_node["note"],
            )
            self.assertIn(
                "<b>Blocco</b>: Runtime heartbeat is stale; GNOME companion did not answer.",
                prompt_node["note"],
            )
            self.assertIn(
                "<b>Prossimo passo</b>: Restart the local companion and run Verify again.",
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
                "<b>Blocco</b>: causa non ancora disponibile.",
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

    def test_repo_pass_override_is_not_available_in_workflowy(self):
        prompt = read_roadmap_db(roadmap_bytes("running"))[0]
        with self.assertRaisesRegex(ValueError, "workflowy_lifecycle_commands_disabled"):
            mutation_for_command(prompt, "PASS", pipeline_state="done")


    def test_short_blocked_child_is_ignored(self):
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
            self.assertEqual(0, result["mutations_submitted"])
            self.assertFalse(
                any(
                    node["parent_id"] == prompt_node["id"]
                    and str(node["name"]).startswith("FIX B")
                    for node in client.nodes
                )
            )
            db.close()


    def test_manual_blocked_failed_children_do_not_publish_fix_packets(self):
        for command in ("B", "F"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
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
                client.create_node(prompt_node["id"], command)
                result = sync_roadmap(
                    client,
                    db,
                    raw_roadmap_db=roadmap_bytes(),
                    submitter=lambda doc, key: submitted.append((doc, key)) or {"status": "ok"},
                )
                self.assertEqual(0, result["mutations_submitted"])
                self.assertEqual(0, result["fix_packets_submitted"])
                self.assertEqual([], submitted)
                db.close()



if __name__ == "__main__":
    unittest.main()
