from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from workflowy_importer.automation import classify
from workflowy_importer.cache import connect, duplicate_groups, refresh_cache, search
from workflowy_importer.chatgpt import conversation_to_markdown
from workflowy_importer.credentials import CredentialError, load_api_key
from workflowy_importer.links import workflowy_url
from workflowy_importer.bridge import _roadmap_prompt_metadata


class CredentialTests(unittest.TestCase):
    def test_secret_file_0600_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "key"
            p.write_text("secret\n", encoding="utf-8")
            p.chmod(0o600)
            os.environ["WF_TEST_KEY"] = "env-secret"
            self.assertEqual(
                load_api_key(
                    secret_file=p,
                    env_var="WF_TEST_KEY",
                ),
                "secret",
            )

    def test_bad_permissions_do_not_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "key"
            p.write_text("secret", encoding="utf-8")
            p.chmod(0o644)
            os.environ["WF_TEST_KEY"] = "env-secret"
            with self.assertRaises(CredentialError):
                load_api_key(
                    secret_file=p,
                    env_var="WF_TEST_KEY",
                )


class CacheTests(unittest.TestCase):
    def test_connection_waits_for_transient_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            db.close()

    def test_refresh_search_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "cache.sqlite")
            refresh_cache(
                db,
                [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "parent_id": None,
                        "name": "Alpha",
                        "note": "needle",
                        "priority": 1,
                        "completed": False,
                        "data": {"layoutMode": "bullets"},
                    },
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "parent_id": None,
                        "name": " alpha ",
                        "note": None,
                        "priority": 2,
                        "completed": False,
                        "data": {"layoutMode": "bullets"},
                    },
                ],
            )
            self.assertEqual(search(db, "needle")[0]["name"], "Alpha")
            self.assertEqual(len(duplicate_groups(db)[0][1]), 2)
            db.close()


class RoutingTests(unittest.TestCase):
    def test_explicit_rule_then_safe_default(self):
        cfg = {
            "default": "inbox",
            "rules": [
                {
                    "name": "bug",
                    "contains": ["personalhub", "bug"],
                    "destination": "ph",
                    "mirror_today": True,
                }
            ],
        }
        decision = classify("Bug in PersonalHub", cfg)
        self.assertEqual(decision.destination, "ph")
        self.assertTrue(decision.mirror_today)
        self.assertEqual(
            classify("ambiguous thought", cfg).destination,
            "inbox",
        )


class LinkTests(unittest.TestCase):
    def test_full_uuid_to_short_workflowy_url(self):
        self.assertEqual(
            workflowy_url(
                "6ed4b9ca-256c-bf2e-bd70-d8754237b505"
            ),
            "https://workflowy.com/#/d8754237b505",
        )


class RoadmapLaunchMetadataTests(unittest.TestCase):
    def test_reads_canonical_codex_launch_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = sqlite3.connect(root / "roadmap.sqlite")
            db.execute(
                """CREATE TABLE prompts (
                     prompt_id TEXT PRIMARY KEY,
                     project_id TEXT,
                     project_name TEXT,
                     repo TEXT,
                     chat_guidance TEXT,
                     prompt_type TEXT,
                     model TEXT,
                     reasoning TEXT
                   )"""
            )
            db.execute(
                """INSERT INTO prompts VALUES (?,?,?,?,?,?,?,?)""",
                (
                    "604812", "23", "chrome-codex-switcher",
                    "gernalix/chrome-codex-switcher", "new chat", "Prompt",
                    "gpt-5.6-terra", "medium",
                ),
            )
            db.commit()
            db.close()

            metadata = _roadmap_prompt_metadata(root, "604812")
            self.assertEqual(metadata["project_id"], "23")
            self.assertEqual(metadata["repo"], "gernalix/chrome-codex-switcher")
            self.assertEqual(metadata["model"], "gpt-5.6-terra")
            self.assertEqual(metadata["reasoning"], "medium")



class ChatGPTTests(unittest.TestCase):
    def test_export_path_to_markdown(self):
        conv = {
            "title": "Example",
            "current_node": "a",
            "mapping": {
                "a": {
                    "parent": "b",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["answer"]},
                    },
                },
                "b": {
                    "parent": None,
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["question"]},
                    },
                },
            },
        }
        md = conversation_to_markdown(conv)
        self.assertIn("# Example", md)
        self.assertLess(
            md.index("## User"),
            md.index("## Assistant"),
        )


if __name__ == "__main__":
    unittest.main()
