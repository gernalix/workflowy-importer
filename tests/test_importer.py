from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workflowy_importer.cli import _load_state, _reconcile_pending_replace, _save_state, build_parser
from workflowy_importer.markdown import LinkResolver, build_tree, count_links, render_inline


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

            final = _reconcile_pending_replace(client, state_path, state)

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

            final = _reconcile_pending_replace(client, state_path, state)

            self.assertEqual(final, rollback)
            self.assertEqual(_load_state(state_path), rollback)
            self.assertEqual(client.deleted, [])


if __name__ == "__main__":
    unittest.main()
