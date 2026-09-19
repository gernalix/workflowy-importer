from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from workflowy_importer.cache import connect
from workflowy_importer.roadmap_bridge import (
    RoadmapPrompt,
    command_from_children,
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
        del layout_mode, position
        self.counter += 1
        node_id = f"00000000-0000-0000-0000-{self.counter:012d}"
        self.nodes.append(
            {
                "id": node_id,
                "parent_id": parent_id,
                "name": name,
                "note": note,
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
        del layout_mode
        for node in self.nodes:
            if node["id"] == node_id:
                if name is not None:
                    node["name"] = name
                if note is not None:
                    node["note"] = note
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
    def test_projection_contains_links_tags_and_dependencies(self):
        prompts = read_roadmap_db(roadmap_bytes())
        self.assertEqual(["123456", "654321"], [p.prompt_id for p in prompts])
        self.assertEqual(["123456"], prompts[1].dependencies)
        self.assertEqual(["654321"], prompts[0].dependents)
        self.assertEqual([("followup", "654321")], prompts[0].relations_out)
        self.assertEqual([("followup", "123456")], prompts[1].relations_in)

    def test_command_is_strict_and_ambiguous_fails_closed(self):
        found = command_from_children([{"name": "PASS", "id": "x"}])
        self.assertEqual("PASS", found[0])
        self.assertIsNone(command_from_children([{"name": " PASS ", "id": "x"}]))
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
        self.assertIn("#project_unknown", prompt_name(prompt))

    def test_terminal_command_can_follow_pending_atomically(self):
        prompt = read_roadmap_db(roadmap_bytes())[0]
        ops = mutation_for_command(prompt, "PASS")
        self.assertEqual(["status", "terminal_request"], [op["op"] for op in ops])
        self.assertEqual("completed", ops[-1]["status"])

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
            self.assertIn("#status_pending", prompt_node["name"])
            self.assertIn("Sblocca:", prompt_node["note"])
            self.assertIn("Relazioni →:", prompt_node["note"])

            client.create_node(prompt_node["id"], "running")
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


if __name__ == "__main__":
    unittest.main()
