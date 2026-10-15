"""Narrow unit tests; no live Workflowy API calls."""
import importlib.util
from pathlib import Path
import unittest

FILE = Path(__file__).resolve().parents[1] / "tools" / "workflowy_semantic_enrich.py"
spec = importlib.util.spec_from_file_location("workflowy_semantic_enrich", FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def node(i, name, parent=None, note=None):
    return {"id": i, "name": name, "parent_id": parent, "note": note,
            "data": {"layoutMode": "bullets"}}


A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class WorkflowyEnrichTests(unittest.TestCase):
    def test_known_entity_and_existing_tag(self):
        self.assertEqual(module.tags_for(node(A, "Analisi Workflowy e Logseq")),
                         ["workflowy", "logseq"])
        self.assertEqual(module.tags_for(node(A, "Workflowy", note="#workflowy")), [])

    def test_ignore_url_code_and_long_import(self):
        self.assertEqual(module.tags_for(node(A, "https://github.com/gernalix/MegaVault")), [])
        self.assertEqual(module.tags_for(node(A, "PersonalHub " * 30)), [])
        x = node(A, "Fedora"); x["data"]["layoutMode"] = "code-block"
        self.assertEqual(module.tags_for(x), [])

    def test_reciprocal_link_and_preservation(self):
        records = [node(A, "Workflowy", note="Note originale"),
                   node(B, "Filtro Workflowy")]
        edits = module.make_edits(records, [[A, B]])
        self.assertEqual(len(edits), 2)
        changes = {x["id"]: x for x in edits}
        self.assertEqual(changes[A]["old_note"], "Note originale")
        self.assertTrue(changes[A]["new_note"].startswith("Note originale\n\n"))
        self.assertIn(module.workflowy_url(B), changes[A]["new_note"])
        self.assertIn(module.workflowy_url(A), changes[B]["new_note"])
        self.assertEqual(changes[B]["name"], "Filtro Workflowy")

    def test_reject_ancestor_and_unknown_target(self):
        records = [node(A, "Workflowy"), node(B, "Fedora", parent=A), node(C, "Logseq")]
        with self.assertRaises(ValueError):
            module.make_edits(records, [[A, B]])
        with self.assertRaises(ValueError):
            module.make_edits(records, [[A, "missing"]])

    def test_no_duplicate_links(self):
        records = [node(A, "Workflowy", note=module.workflowy_url(B)),
                   node(B, "Fedora")]
        edits = module.make_edits(records, [[A, B]])
        self.assertFalse(next(x for x in edits if x["id"] == A)["links"])


if __name__ == "__main__":
    unittest.main()
