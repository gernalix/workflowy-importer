"""Narrow unit tests; no live Workflowy API calls."""
import importlib.util
import hashlib
import json
import tempfile
from types import SimpleNamespace
from unittest import mock
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


    def test_verify_confirmed_reciprocal_links(self):
        source = [node(A, "Workflowy"), node(B, "Logseq")]
        edits = module.make_edits(source, [[A, B]])
        live = [{**n, "note": next(e["new_note"] for e in edits if e["id"] == n["id"])} for n in source]

        class FakeAPI:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def export_nodes(self):
                return live

        with tempfile.TemporaryDirectory() as temp:
            t = Path(temp)
            raw = json.dumps(source).encode()
            backup = t / "original.json"; backup.write_bytes(raw)
            plan = t / "plan.json"
            plan.write_text(json.dumps({"schema": "workflowy-semantic/v1",
                "backup_file": str(backup), "backup_sha256": hashlib.sha256(raw).hexdigest(),
                "edits": edits, "pairs": [[A, B]]}))
            journal = t / "journal.jsonl"
            journal.write_text("".join(json.dumps({"id": id, "status": "applied"}) + "\n" for id in (A, B)))
            output = t / "result.json"
            args = SimpleNamespace(plan=plan, journal=journal, output=output)
            with mock.patch.object(module, "WorkflowyClient", FakeAPI), mock.patch.object(module, "load_api_key", return_value="dummy"):
                module.verify(args)
            result = json.loads(output.read_text())
            self.assertEqual(result["confirmed_notes"], 2)
            self.assertEqual(result["reciprocal_pairs"], 1)
            self.assertEqual(result["errors"], [])



if __name__ == "__main__":
    unittest.main()
