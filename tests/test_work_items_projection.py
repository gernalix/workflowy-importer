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
                 executor_policy='rdc', tags=['check'], dependencies=[], unlocks=[],
                 sort_order=None, created_at='2026-01-01T00:00:00Z')
    value.update(kw)
    return value


class ProjectionTests(unittest.TestCase):
    def test_writer_applied_item_is_projected_and_dashboard_acknowledges_it(self):
        client = FakeClient()
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [item('before')], parent='inbox')

            # The second canonical snapshot represents the writer-applied
            # mutation. The path unit schedules this same projector; no
            # Workflowy child command or manual dashboard action is involved.
            sync_items(client, db, [item('before'), item('writer-applied')], parent='inbox')

            visible = next(n for n in client.nodes if 'writer-applied' in n['name'])
            root = next(n for n in client.nodes if n['name'] == 'Codex')
            self.assertEqual('Ready', next(n for n in client.nodes if n['id'] == visible['parent_id'])['name'])
            self.assertIn('C2_ENTITY_ID: writer-applied', visible['note'])
            self.assertIn('✅ Proiezione Workflowy allineata', root['note'])
            self.assertNotIn('⏳ Proiezione Workflowy in corso', root['note'])

    def test_failed_projection_leaves_pending_status_visible(self):
        class FailBeforeGroups(FakeClient):
            def create_node(self, *args, **kwargs):
                if self.nodes:
                    raise RuntimeError('projection_failed')
                return super().create_node(*args, **kwargs)

        client = FailBeforeGroups()
        with closing(connect(':memory:')) as db:
            with self.assertRaisesRegex(RuntimeError, 'projection_failed'):
                sync_items(client, db, [item('writer-applied')], parent='inbox')
        self.assertIn('⏳ Proiezione Workflowy in corso', client.nodes[0]['note'])

    def test_legacy_not_activated_before_cutover(self):
        self.assertIsNone(read_items(roadmap_bytes()))

    def test_reader_uses_canonical_execution_views(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript('''
                CREATE TABLE work_items(work_item_id TEXT,parent_id TEXT,title TEXT,status TEXT,
                    sort_order INTEGER,created_at TEXT,updated_at TEXT,project_name TEXT,repo TEXT);
                INSERT INTO work_items VALUES('one',NULL,'One','running',1,'2026-01-01T00:00:00Z',
                    '2026-01-01T00:00:00Z','C2','repo');
                CREATE VIEW prompts AS SELECT * FROM work_items;
                CREATE VIEW v_work_item_summary AS SELECT * FROM work_items;
                CREATE VIEW v_work_item_runnable AS SELECT * FROM work_items WHERE 0;
                CREATE TABLE work_item_tags(work_item_id TEXT,tag TEXT);
                CREATE TABLE work_item_dependencies(work_item_id TEXT,depends_on_work_item_id TEXT,required INTEGER);
                CREATE TABLE execution(execution_id TEXT,work_item_id TEXT,executor TEXT,worker_ref TEXT,
                    status TEXT,conversation_ref_type TEXT,conversation_ref_uri TEXT,claimed_at REAL);
                INSERT INTO execution VALUES('run:new','one','codex','worker-1','running',
                    'codex_thread','codex://threads/one',10);
                CREATE VIEW v_work_item_execution_current AS SELECT * FROM execution;
                CREATE VIEW v_work_item_execution_history AS SELECT * FROM execution;
            ''')
            rows = read_items(conn.serialize())
        self.assertEqual('codex',rows[0]['current_execution']['executor'])
        self.assertEqual('codex://threads/one',
            rows[0]['execution_history'][0]['conversation_ref_uri'])

    def test_reader_prefers_optional_manual_rank_and_tolerates_rolling_schema(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript('''
                CREATE TABLE work_items(work_item_id TEXT,parent_id TEXT,title TEXT,status TEXT,
                    sort_order INTEGER,created_at TEXT,updated_at TEXT,project_name TEXT,repo TEXT,
                    manual_rank INTEGER,manual_source TEXT,manual_source_modified_at TEXT);
                INSERT INTO work_items VALUES
                    ('one',NULL,'One','pending',1,'2026-01-01T00:00:00Z',
                     '2026-01-01T00:00:00Z','C2','repo',2,'workflowy','wf-2'),
                    ('two',NULL,'Two','pending',2,'2026-01-01T00:00:00Z',
                     '2026-01-01T00:00:00Z','C2','repo',1,'workflowy','wf-1');
                CREATE VIEW prompts AS SELECT * FROM work_items;
                CREATE VIEW v_work_item_summary AS SELECT * FROM work_items;
                CREATE VIEW v_work_item_runnable AS SELECT work_item_id FROM work_items;
                CREATE TABLE work_item_execution_specs(work_item_id TEXT);
                INSERT INTO work_item_execution_specs VALUES('one'),('two');
                CREATE TABLE work_item_tags(work_item_id TEXT,tag TEXT);
                CREATE TABLE work_item_dependencies(work_item_id TEXT,depends_on_work_item_id TEXT,required INTEGER);
            ''')
            rows = read_items(conn.serialize())
        self.assertEqual(['two', 'one'], [row['work_item_id'] for row in rows])
        self.assertEqual(('workflowy', 'wf-1'),
                         (rows[0]['manual_source'], rows[0]['manual_source_modified_at']))

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
            self.assertEqual('Done', next(
                n for n in client.nodes if n['id'] == child_node['parent_id'])['name'])
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

    def test_current_executor_link_and_explicit_no_chat_are_compact(self):
        _, linked = item_text(item('linked', status='running', group='running',
            current_execution={'executor':'codex','worker_ref':'worker-7',
                'conversation_ref_type':'codex_thread',
                'conversation_ref_uri':'codex://threads/thread-7'}), {})
        self.assertIn('Executor corrente: codex · worker-7', linked)
        self.assertIn('<a href="codex://threads/thread-7">Apri chat/thread</a>', linked)
        _, native = item_text(item('native', status='running', group='running',
            current_execution={'executor':'rdc','worker_ref':'native-2',
                'conversation_ref_type':'none','conversation_ref_uri':None}), {})
        self.assertIn('Executor corrente: rdc · native-2 · nessuna chat', native)
        self.assertNotIn('<a href=', native)

    def test_executor_history_is_a_separate_child_node(self):
        client = FakeClient()
        running = item('root', status='running', group='running',
            current_execution={'execution_id':'new','executor':'codex',
                'worker_ref':'worker-new','conversation_ref_type':'codex_thread',
                'conversation_ref_uri':'codex://threads/new'},
            execution_history=[
                {'execution_id':'new','executor':'codex','status':'running',
                 'claimed_at':20,'conversation_ref_uri':'codex://threads/new'},
                {'execution_id':'old','executor':'chatgpt','status':'failed',
                 'claimed_at':10,'conversation_ref_uri':'https://chatgpt.com/c/old'},
            ])
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [running], parent='inbox')
            task = next(n for n in client.nodes if n['name'].startswith('👉 root'))
            history = next(n for n in client.nodes if n['name']=='🕘 Storico executor (1)')
            self.assertEqual(task['id'],history['parent_id'])
            self.assertIn('chatgpt · failed · 10',history['note'])
            self.assertIn('https://chatgpt.com/c/old',history['note'])
    def test_recent_pending_intake_is_visible_once_without_becoming_ready(self):
        client = FakeClient()
        recent = item('new', group='intake', execution_configured=False)
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [recent], parent='inbox')
            intake = next(n for n in client.nodes if n['name'] == 'Inbox execution order')
            projected = [n for n in client.nodes if n['name'].startswith('☐ new')]
            self.assertEqual(1, len(projected))
            self.assertEqual(intake['id'], projected[0]['parent_id'])
            self.assertIn('mancano dati di esecuzione', projected[0]['note'])
            self.assertNotIn(projected[0]['id'], [
                n['id'] for n in client.nodes if n['parent_id'] != intake['id']
            ])

    def test_genuine_reorder_submits_one_bulk_mutation_and_repeated_sync_is_quiet(self):
        client = FakeClient()
        submitted = []
        rows = [item('one', sort_order=1), item('two', sort_order=2)]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc, key)) or {'status':'ok'})
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            second = next(n for n in client.nodes if n['name'].startswith('☐ two'))
            client.move_node(second['id'], ready['id'], position='top')
            result = sync_items(client, db, rows, parent='inbox',
                                submitter=lambda doc, key: submitted.append((doc, key)) or {'status':'ok'})
            self.assertEqual(1, result['mutations_submitted'])
            operation = submitted[0][0]['operations'][0]
            self.assertEqual('set_manual_order', operation['op'])
            self.assertEqual('roadmap', operation['scope'])
            self.assertEqual(['two', 'one'], operation['ordered_ids'])
            self.assertEqual('workflowy', operation['source'])
            again = sync_items(client, db, rows, parent='inbox',
                               submitter=lambda doc, key: submitted.append((doc, key)) or {'status':'ok'})
            self.assertEqual(0, again['mutations_submitted'])
            self.assertEqual(1, len(submitted))

    def test_renderer_refresh_changes_order_without_echo_mutation(self):
        client = FakeClient()
        submitted = []
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [item('one', sort_order=1), item('two', sort_order=2)],
                       parent='inbox', submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            refreshed = [
                item('one', sort_order=1, manual_rank=2, manual_source='workflowy'),
                item('two', sort_order=2, manual_rank=1, manual_source='workflowy'),
            ]
            result = sync_items(client, db, refreshed, parent='inbox',
                                submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            self.assertEqual(0, result['mutations_submitted'])
            self.assertEqual([], submitted)
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            visible = [n['name'].split()[-1] for n in client.nodes
                       if n.get('parent_id') == ready['id'] and n['name'].startswith('☐')]
            self.assertEqual(['two', 'one'], visible)

    def test_reset_submits_clear_for_scope_only(self):
        client = FakeClient()
        submitted = []
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [item('one')], parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            reset = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and next(p for p in client.nodes if p['id'] == n['parent_id'])['name'] == 'Roadmap')
            reset['completed'] = True
            result = sync_items(client, db, [item('one')], parent='inbox',
                                submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            self.assertEqual(1, result['mutations_submitted'])
            self.assertEqual({'op':'clear_manual_order', 'scope':'roadmap'},
                             submitted[0][0]['operations'][0])

    def test_drag_across_status_section_is_reverted_without_mutation(self):
        client = FakeClient()
        submitted = []
        with closing(connect(':memory:')) as db:
            rows = [item('one')]
            sync_items(client, db, rows, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            task = next(n for n in client.nodes if n['name'].startswith('☐ one'))
            blocked = next(n for n in client.nodes if n['name'] == 'Blocked / dependency context')
            client.move_node(task['id'], blocked['id'], position='top')
            result = sync_items(client, db, rows, parent='inbox',
                                submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            self.assertEqual(0, result['mutations_submitted'])
            self.assertEqual(1, result['warnings'])
            self.assertEqual('Ready', next(
                n for n in client.nodes if n['id'] == task['parent_id'])['name'])
            self.assertEqual([], submitted)

    def test_dag_mirror_is_not_sent_in_ordered_ids(self):
        client = FakeClient()
        submitted = []
        dep1, dep2 = item('dep1'), item('dep2')
        target = item('target', dependencies=[
            {'work_item_id':'dep1','title':'dep1','status':'pending'},
            {'work_item_id':'dep2','title':'dep2','status':'pending'},
        ])
        rows = [dep1, dep2, target]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            mirror = next(n for n in client.nodes if n['name'].startswith('↪ Depends on'))
            self.assertIn('C2_REFERENCE_ID: dep2', mirror['note'])
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            task = next(n for n in client.nodes if n['name'].startswith('☐ target'))
            client.move_node(task['id'], ready['id'], position='top')
            sync_items(client, db, rows, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            ordered_ids = submitted[0][0]['operations'][0]['ordered_ids']
            self.assertEqual({'dep1','dep2','target'}, set(ordered_ids))
            self.assertNotIn(mirror['id'], ordered_ids)

    def test_manual_rank_round_trip_survives_refresh(self):
        client = FakeClient()
        submitted = []
        initial = [item('one', sort_order=1), item('two', sort_order=2)]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, initial, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            two = next(n for n in client.nodes if n['name'].startswith('☐ two'))
            client.move_node(two['id'], ready['id'], position='top')
            sync_items(client, db, initial, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            ranked = [
                item('one', sort_order=1, manual_rank=2, manual_source='workflowy',
                     manual_source_modified_at=1_790_000_000),
                item('two', sort_order=2, manual_rank=1, manual_source='workflowy',
                     manual_source_modified_at=1_790_000_000),
            ]
            sync_items(client, db, ranked, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            sync_items(client, db, ranked, parent='inbox',
                       submitter=lambda doc, key: submitted.append((doc,key)) or {'status':'ok'})
            visible = [n['name'].split()[-1] for n in client.nodes
                       if n.get('parent_id') == ready['id'] and n['name'].startswith('☐')]
            self.assertEqual(['two','one'], visible)
            self.assertEqual(1, len(submitted))

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
