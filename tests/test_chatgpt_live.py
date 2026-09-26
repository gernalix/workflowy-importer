from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from workflowy_importer.cache import connect
from workflowy_importer.chatgpt_live import (
    ChatGPTCloudCollector,
    ChatRecord,
    classify_statuses,
    mark_running,
    record_from_payload,
    sync_chatgpt_dashboard,
    upsert_records,
)


class FakeClient:
    def __init__(self) -> None:
        self.created: list[tuple] = []
        self.updated: list[tuple] = []
        self.moved: list[tuple] = []
        self.next_id = 1

    def resolve_target_id(self, target: str) -> str:
        return f"target:{target}"
    def create_node(
        self, parent_id, name, layout_mode="bullets", position="bottom", note=None
    ) -> str:
        node_id = f"node-{self.next_id}"
        self.next_id += 1
        self.created.append((node_id, parent_id, name, position, note))
        return node_id

    def update_node(self, node_id, name=None, *, note=None, layout_mode=None):
        self.updated.append((node_id, name, note, layout_mode))

    def move_node(self, node_id, parent_id, position="top"):
        self.moved.append((node_id, parent_id, position))


class ChatGPTLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "cache.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_payload_uses_exact_inventory_url_and_timestamps(self) -> None:
        cid = "abc-123"
        inventory = {
            cid: {
                "conversation_id": cid,
                "url": "https://chatgpt.com/g/g-p-project-fedora/c/abc-123",
            }
        }
        record = record_from_payload(
            {
                "id": cid,
                "title": "Cross-device chat",
                "create_time": "2026-09-26T16:00:00Z",
                "update_time": "2026-09-26T16:03:00Z",
                "gizmo_id": "g-p-project",
            },
            source="project",
            inventory=inventory,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(inventory[cid]["url"], record.url)
        self.assertEqual("Cross-device chat", record.title)
        self.assertEqual(1790438400.0, record.created_at)
        self.assertEqual(1790438580.0, record.last_interaction_at)

    def test_running_recent_idle_transitions(self) -> None:
        now = time.time()
        records = [
            ChatRecord("run", "Run", "https://chatgpt.com/c/run", now - 50, now - 10, "chat"),
            ChatRecord("recent", "Recent", "https://chatgpt.com/c/recent", now - 500, now - 120, "chat"),
            ChatRecord("idle", "Idle", "https://chatgpt.com/c/idle", now - 5000, now - 4000, "chat"),
        ]
        upsert_records(self.db, records, now=now)
        mark_running(self.db, {"run"}, now=now)
        counts = classify_statuses(
            self.db,
            now=now,
            recent_seconds=900,
            running_ttl_seconds=300,
        )
        self.assertEqual({"RUNNING": 1, "RECENT": 1, "IDLE": 1}, counts)

        counts = classify_statuses(
            self.db,
            now=now + 1000,
            recent_seconds=900,
            running_ttl_seconds=300,
        )
        self.assertEqual({"RUNNING": 0, "RECENT": 0, "IDLE": 3}, counts)

    def test_projection_is_linked_and_idempotent(self) -> None:
        now = time.time()
        upsert_records(
            self.db,
            [
                ChatRecord(
                    "cid",
                    "Mobile & Web",
                    "https://chatgpt.com/c/cid",
                    now - 60,
                    now - 5,
                    "chat",
                )
            ],
            now=now,
        )
        classify_statuses(self.db, now=now, recent_seconds=900)
        client = FakeClient()
        first = sync_chatgpt_dashboard(client, self.db, max_writes=20)
        self.assertEqual(1, first["RECENT"])
        self.assertEqual(1, first["writes"])
        conversation = [
            call for call in client.created if "chatgpt.com/c/cid" in call[2]
        ]
        self.assertEqual(1, len(conversation))
        self.assertIn("<a href=", conversation[0][2])
        self.assertIn("Created:", conversation[0][4])
        self.assertIn("Last interaction:", conversation[0][4])

        created = len(client.created)
        updated = len(client.updated)
        moved = len(client.moved)
        second = sync_chatgpt_dashboard(client, self.db, max_writes=20)
        self.assertEqual(0, second["writes"])
        self.assertEqual(created, len(client.created))
        self.assertEqual(updated, len(client.updated))
        self.assertEqual(moved, len(client.moved))

    def test_project_id_extraction_is_recursive(self) -> None:
        value = {
            "items": [
                {"gizmo": {"id": "g-p-one"}},
                {"id": "not-a-project"},
                {"children": [{"gizmo_id": "g-p-two"}]},
            ]
        }
        self.assertEqual(
            {"g-p-one", "g-p-two"},
            ChatGPTCloudCollector._project_ids(value),
        )

    def test_ui_refresh_drives_collection_without_direct_fetch(self) -> None:
        class Collector(ChatGPTCloudCollector):
            def _open_running_ids(self):
                return set()

            def _ui_refresh_records(self, *, inventory, wait_ms=7000):
                return (
                    [
                        ChatRecord(
                            "cid",
                            "Primary chat",
                            "https://chatgpt.com/c/cid",
                            10.0,
                            20.0,
                            "chat",
                        )
                    ],
                    False,
                    None,
                )

        collector = Collector(
            inventory_path=Path(self.tmp.name) / "missing.json",
            request_delay=0,
        )
        result = collector.collect(
            full=False,
            api_enabled=True,
            detail_limit=0,
            known_ids=set(),
        )
        self.assertFalse(result.rate_limited)
        self.assertTrue(result.full_complete)
        self.assertEqual(["cid"], [row.conversation_id for row in result.records])


if __name__ == "__main__":
    unittest.main()
