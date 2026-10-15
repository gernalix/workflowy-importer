"""Safety invariants for private semantic discovery and second-pass plan."""
import importlib.util
from pathlib import Path
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from workflowy_semantic_discover import eligible, exclude_note_generated
from workflowy_semantic_round2_plan import build_plan, drop_our_old_tags


A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def node(identifier, name, note=None, parent=None):
    return {"id": identifier, "name": name, "note": note, "parent_id": parent}


def first_edit(identifier, name, old, new, tags, links=None):
    return {"id": identifier, "name": name, "old_note": old,
            "new_note": new, "tags": tags, "links": links or []}


class Round2Tests(unittest.TestCase):
    def test_drop_only_our_topic_tags_preserve_original(self):
        old = "Contenuto #deciso\nConservare esattamente."
        added = old + "\n\n#workflowy #logseq\nCorrelati: <a href=\"https://workflowy.com/#/aaaaaaaaaaaa\">nota</a>"
        f = first_edit(A, "Confronto Workflowy Logseq", old, added, ["workflowy", "logseq"], [B])
        self.assertEqual(
            drop_our_old_tags(added, f),
            old + "\n\nCorrelati: <a href=\"https://workflowy.com/#/aaaaaaaaaaaa\">nota</a>",
        )

    def test_drop_tag_only_note(self):
        self.assertEqual(
            drop_our_old_tags("#codex", first_edit(A, "Codex", None, "#codex", ["codex"])), ""
        )

    def test_refuse_unexpected_suffix(self):
        entry = first_edit(A, "Workflowy", None, "#workflowy\nunrecognized", ["workflowy"])
        with self.assertRaises(ValueError):
            drop_our_old_tags(entry["new_note"], entry)

    def test_curated_reciprocal_and_original_user_tag(self):
        old = "User text #idea"
        source = [node(A, "Workflowy", old + "\n\n#workflowy"),
                  node(B, "Logseq", None)]
        first = {"edits": [first_edit(A, "Workflowy", old, old + "\n\n#workflowy", ["workflowy"])]}
        pairs = [{"a": A, "b": B, "reason": "Both outline tools in a documented comparison"}]
        plan = build_plan(source, first, pairs)
        edits = {e["id"]: e for e in plan["edits"]}
        self.assertEqual(plan["removed_generated_tags"], 1)
        self.assertEqual(plan["added_link_instances"], 2)
        self.assertEqual(edits[A]["new_note"].count("#idea"), 1)
        self.assertNotIn("#workflowy", edits[A]["new_note"])
        self.assertIn("bbbbbbbbbbbb", edits[A]["new_note"])
        self.assertIn("aaaaaaaaaaaa", edits[B]["new_note"])
        self.assertTrue(all(not e["tags"] for e in plan["edits"]))

    def test_conflicting_user_edit_fails_closed(self):
        source = [node(A, "Workflowy", "#workflowy changed")]
        prior = {"edits": [first_edit(A, "Workflowy", None, "#workflowy", ["workflowy"])]}
        with self.assertRaises(ValueError):
            build_plan(source, prior, [])

    def test_reject_ancestor_relations(self):
        first = {"edits": []}
        with self.assertRaises(ValueError):
            build_plan([node(A, "Root"), node(B, "Child", parent=A)],
                       first, [{"a": A, "b": B,
                                "reason": "Do not link ancestors even when words overlap"}])

    def test_chat_messages_not_embedding_candidates(self):
        self.assertFalse(eligible(node(A, "[1/21, 16:37] Contact: Hi"), 0))
        self.assertFalse(eligible(node(A, "[10:37, 8/6/2026] Contact: Hello"), 0))
        self.assertTrue(eligible(node(A, "Confronto fra Workflowy e Logseq"), 0))

    def test_generated_metadata_excluded_from_embedding(self):
        note = "Nota originale\n\n#codex #workflowy\nCorrelati: <a href=\"x\">link</a>"
        self.assertEqual(exclude_note_generated(note), "Nota originale")


if __name__ == "__main__":
    unittest.main()
