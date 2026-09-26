from contextlib import closing
import sqlite3
import unittest
from unittest.mock import Mock

from workflowy_importer.cache import connect
from workflowy_importer.work_items_projection import read_items, sync_items, item_text
from test_roadmap_bridge import FakeClient, roadmap_bytes


def item(key, **kw):
    value = dict(work_item_id=key, parent_id=None, title=key, status='pending',
                 prompt_id=None, group='ready', completed_actionable=0,
                 total_actionable=1, progress_percent=0, current_action=None,
                 next_action='Run check', blocker=None, project_name='Example',
                 executor_policy='rdc', tags=['check'], dependencies=[])
    value.update(kw)
    return value


class ProjectionTests(unittest.TestCase):
    def test_legacy_not_activated_before_cutover(self):
        self.assertIsNone(read_items(roadmap_bytes()))

    def test_tree_reuses_legacy_prompt_and_second_sync_is_noop(self):
        from workflowy_importer.roadmap_bridge import _mapping_set
        client = FakeClient()
        existing = client.create_node('inbox', '[123456] Old')
        child = item('child', parent_id='root', status='completed', group='completed')
        root = item('root', prompt_id='123456', status='running', group='running',
                    completed_actionable=1, progress_percent=100,
                    current_action='Verify result')
        with closing(connect(':memory:')) as db:
            _mapping_set(db, '123456', existing)
            result = sync_items(client, db, [child, root], parent='inbox')
            self.assertEqual(2, result['prompts'])
            self.assertEqual(0, result['mutations_submitted'])
            root_node = next(n for n in client.nodes if n['id'] == existing)
            self.assertIn('100%', root_node['note'])
            child_node = next(n for n in client.nodes if n['name'].startswith('✅ child'))
            self.assertEqual(existing, child_node['parent_id'])
            result = sync_items(client, db, [child, root], parent='inbox')
            self.assertEqual((0,0,0), tuple(result[k] for k in ('created','updated','moved')))

    def test_waiting_explains_dependency_and_escapes_text(self):
        name, note = item_text(item('unsafe', title='<b>literal</b>', dependencies=[
            {'work_item_id':'parent', 'title':'Build', 'status':'running'}]), {'parent':'abc'})
        self.assertIn('&lt;b&gt;', name)
        self.assertIn('In attesa di: Build', note)
        self.assertIn('https://workflowy.com/#/abc', note)

    def test_waiting_missing_execution_data_and_external_ph_owner(self):
        _, waiting = item_text(item('waiting', group='waiting', execution_configured=False), {})
        self.assertIn('mancano dati di esecuzione', waiting)
        _, note = item_text(item('ph', group='waiting', objective='Keep existing data',
            execution_configured=False, external_owner=True), {})
        self.assertIn('Cosa fa: Keep existing data', note)
        self.assertNotIn('mancano dati di esecuzione', note)
        self.assertIn('Gestito da worker esterno PH', note)

    def test_legacy_action_groups_move_under_archive(self):
        from workflowy_importer.roadmap_bridge import _mapping_set, GROUP_PREFIX, ROADMAP_ROOT_KEY
        client = FakeClient()
        root = client.create_node('inbox', 'Codex')
        old = client.create_node(root, 'Integration')
        client.create_node(old, 'User note')
        with closing(connect(':memory:')) as db:
            _mapping_set(db, ROADMAP_ROOT_KEY, root)
            _mapping_set(db, GROUP_PREFIX+'integration', old)
            sync_items(client, db, [item('one')], parent='inbox')
            archived = next(n for n in client.nodes if n['id'] == old)
            self.assertEqual('Legacy integration', archived['name'])
            self.assertNotEqual(root, archived['parent_id'])
            self.assertEqual(old, next(n for n in client.nodes if n['name']=='User note')['parent_id'])

    def test_canonical_reader_rejects_cycle_before_remote_write(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript('''
                CREATE TABLE work_items(work_item_id TEXT,parent_id TEXT,status TEXT,
                    sort_order INTEGER,created_at TEXT);
                INSERT INTO work_items VALUES('a','b','pending',1,'now'),('b','a','pending',2,'now');
                CREATE VIEW prompts AS SELECT * FROM work_items;
                CREATE VIEW v_work_item_summary AS SELECT * FROM work_items;
                CREATE VIEW v_work_item_runnable AS SELECT * FROM work_items;
                CREATE TABLE work_item_tags(work_item_id TEXT,tag TEXT);
                CREATE TABLE work_item_dependencies(work_item_id TEXT,depends_on_work_item_id TEXT,required INTEGER);
            ''')
            # Dependency query also selects title; provide it in the fixture.
            conn.execute('ALTER TABLE work_items ADD COLUMN title TEXT')
            with self.assertRaisesRegex(ValueError, 'invalid_work_item_hierarchy'):
                read_items(conn.serialize())
