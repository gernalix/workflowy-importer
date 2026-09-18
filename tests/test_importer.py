from __future__ import annotations

import tempfile
import unittest

import httpx
from pathlib import Path

from workflowy_importer.api import WorkflowyAPIError, WorkflowyClient
from workflowy_importer.cli import _load_state, _reconcile_pending_state, _save_state, build_parser
from workflowy_importer.markdown import LinkResolver, build_tree, count_links, render_inline
from workflowy_importer.smoke import _known_roots


class ImporterTests(unittest.TestCase):
    def test_structure_tasks_code_and_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text(
                "# H1\n"
                "Intro **bold**.\n\n"
                "## H2\n"
                "- parent\n"
                "  - [x] done\n"
                "  - [ ] open\n\n"
                "```python\n"
                "print('x')\n"
                "```\n",
                encoding="utf-8",
            )
            parsed = build_tree(root, "Import")
            nodes = list(parsed.root.walk())
            by_name = {node.name: node for node in nodes}

            self.assertEqual(by_name["H1"].layout, "h1")
            self.assertEqual(by_name["H2"].layout, "h2")
            self.assertEqual(by_name["done"].layout, "todo")
            self.assertTrue(by_name["done"].completed)
            self.assertEqual(by_name["open"].layout, "todo")
            self.assertFalse(by_name["open"].completed)
            self.assertEqual(by_name["print('x')"].layout, "code-block")
            self.assertIn("<b>bold</b>", render_inline("Intro **bold**."))

    def test_wikilinks_and_local_markdown_links_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "A.md").write_text(
                "# Alpha\nSee [[B#Beta|the beta]].\n",
                encoding="utf-8",
            )
            (root / "B.md").write_text(
                "# Beta\nBack to [Alpha](A.md#Alpha).\n",
                encoding="utf-8",
            )
            parsed = build_tree(root, "Import")
            resolver = LinkResolver(parsed.root)
            ids = {
                node.key: f"00000000-0000-0000-0000-{i:012d}"
                for i, node in enumerate(parsed.root.walk(), 1)
            }

            a_file = next(
                node for node in parsed.root.walk() if node.key == "file:A.md"
            )
            body = next(
                node for node in a_file.walk() if "[[B#Beta" in node.name
            )
            rendered = render_inline(
                body.name,
                resolve_target=lambda target: resolver.href(
                    target, body.source_file, ids
                ),
            )
            self.assertIn('<a href="https://workflowy.com/#/', rendered)
            self.assertIn(">the beta</a>", rendered)

            resolved, unresolved = count_links(parsed.root, resolver)
            self.assertEqual(resolved, 2)
            self.assertEqual(unresolved, 0)

    def test_ambiguous_basenames_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one").mkdir()
            (root / "two").mkdir()
            (root / "one" / "Same.md").write_text("# One\n", encoding="utf-8")
            (root / "two" / "Same.md").write_text("# Two\n", encoding="utf-8")
            (root / "ref.md").write_text("[[Same]]\n", encoding="utf-8")
            parsed = build_tree(root, "Import")
            resolver = LinkResolver(parsed.root)

            self.assertIsNone(resolver.resolve_key("Same", "ref.md"))
            self.assertIsNotNone(resolver.resolve_key("one/Same", "ref.md"))

    def test_frontmatter_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meta.md").write_text(
                "---\ntags: [a, b]\n---\n# Title\n",
                encoding="utf-8",
            )
            parsed = build_tree(root, "Import")
            names = [node.name for node in parsed.root.walk()]
            self.assertIn("Front matter", names)
            self.assertIn("tags: [a, b]", names)


class FakeClient:
    def __init__(self, existing: set[str]):
        self.existing = set(existing)
        self.deleted: list[str] = []

    def node_exists(self, node_id: str) -> bool:
        return node_id in self.existing

    def delete_node(self, node_id: str) -> None:
        if node_id not in self.existing:
            raise AssertionError(f"missing node {node_id}")
        self.existing.remove(node_id)
        self.deleted.append(node_id)


class StateRecoveryTests(unittest.TestCase):
    def test_cli_module_and_defaults_load(self) -> None:
        args = build_parser().parse_args(["notes"])
        self.assertEqual(args.parent, "None")
        self.assertFalse(args.replace)
        self.assertFalse(args.no_resolve_links)

    def test_pending_replace_finishes_by_deleting_old_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            rollback = {"root_id": "old", "fingerprint": "old-fingerprint"}
            state = {
                "root_id": "new",
                "fingerprint": "new-fingerprint",
                "pending_replace": {
                    "old_root_id": "old",
                    "rollback_state": rollback,
                },
            }
            _save_state(state_path, state)
            client = FakeClient({"new", "old"})

            final = _reconcile_pending_state(client, state_path, state)

            self.assertEqual(client.deleted, ["old"])
            self.assertNotIn("pending_replace", final)
            self.assertNotIn("pending_replace", _load_state(state_path))

    def test_pending_replace_rolls_state_back_if_new_root_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            rollback = {"root_id": "old", "fingerprint": "old-fingerprint"}
            state = {
                "root_id": "new",
                "fingerprint": "new-fingerprint",
                "pending_replace": {
                    "old_root_id": "old",
                    "rollback_state": rollback,
                },
            }
            _save_state(state_path, state)
            client = FakeClient({"old"})

            final = _reconcile_pending_state(client, state_path, state)

            self.assertEqual(final, rollback)
            self.assertEqual(_load_state(state_path), rollback)
            self.assertEqual(client.deleted, [])


    def test_pending_build_deletes_partial_root_and_restores_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            rollback = {"root_id": "old", "fingerprint": "old-fingerprint"}
            state = {
                "format_version": 1,
                "pending_build": {
                    "root_id": "partial",
                    "rollback_state": rollback,
                },
            }
            _save_state(state_path, state)
            client = FakeClient({"old", "partial"})

            final = _reconcile_pending_state(client, state_path, state)

            self.assertEqual(client.deleted, ["partial"])
            self.assertEqual(final, rollback)
            self.assertEqual(_load_state(state_path), rollback)

    def test_pending_initial_build_removes_state_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {
                "format_version": 1,
                "pending_build": {
                    "root_id": "partial",
                    "rollback_state": None,
                },
            }
            _save_state(state_path, state)
            client = FakeClient({"partial"})

            final = _reconcile_pending_state(client, state_path, state)

            self.assertIsNone(final)
            self.assertFalse(state_path.exists())
            self.assertEqual(client.deleted, ["partial"])


class ApiRetrySafetyTests(unittest.TestCase):
    def _client_with_transport(self, handler) -> WorkflowyClient:
        client = WorkflowyClient(api_key="test", max_retries=2)
        client._client.close()
        client._client = httpx.Client(
            base_url="https://workflowy.invalid/api/v1",
            transport=httpx.MockTransport(handler),
        )
        return client

    def test_create_node_does_not_retry_503(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, text="temporary")

        with self._client_with_transport(handler) as client:
            with self.assertRaises(WorkflowyAPIError):
                client.create_node("inbox", "test")

        self.assertEqual(calls, 1)

    def test_get_node_retries_safe_transient_failure(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, text="temporary")
            return httpx.Response(
                200,
                json={"node": {"id": "abc", "name": "ok"}},
            )

        with self._client_with_transport(handler) as client:
            node = client.get_node("abc")

        self.assertEqual(node["id"], "abc")
        self.assertEqual(calls, 2)

    def test_node_exists_uses_status_code_not_error_string(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="missing")

        with self._client_with_transport(handler) as client:
            self.assertFalse(client.node_exists("missing"))


class SmokeHelperTests(unittest.TestCase):
    def test_known_roots_covers_final_and_pending_states(self) -> None:
        state = {
            "root_id": "new",
            "pending_replace": {
                "old_root_id": "old",
                "rollback_state": {"root_id": "rollback"},
            },
        }
        self.assertEqual(_known_roots(state), {"new", "old", "rollback"})


if __name__ == "__main__":
    unittest.main()
