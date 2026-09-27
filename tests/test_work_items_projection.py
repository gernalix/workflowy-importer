from contextlib import closing
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

from workflowy_importer.cache import connect
from workflowy_importer.work_items_projection import (
    issue_text,
    item_text,
    read_issue_inbox,
    read_items,
    run_manual_order_adapter,
    sync_items,
)
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


def issue(key, **kw):
    value = dict(issue_id=key, description='Issue '+key, observed_at_ms=1,
                 manual_rank=None, manual_order_source=None,
                 manual_order_source_modified_at=None)
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

    def test_failed_projection_leaves_actionable_diagnostic_visible(self):
        class FailBeforeGroups(FakeClient):
            def create_node(self, *args, **kwargs):
                if self.nodes:
                    raise RuntimeError('projection_failed')
                return super().create_node(*args, **kwargs)

        client = FailBeforeGroups()
        with closing(connect(':memory:')) as db:
            with self.assertRaisesRegex(RuntimeError, 'projection_failed'):
                sync_items(client, db, [item('writer-applied')], parent='inbox')
        self.assertIn('⚠ Proiezione Workflowy non completata', client.nodes[0]['note'])
        self.assertIn('Rieseguire roadmap-sync', client.nodes[0]['note'])
        self.assertIn('RuntimeError: projection_failed', client.nodes[0]['note'])
        self.assertNotIn('⏳ Proiezione Workflowy in corso', client.nodes[0]['note'])

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
                    manual_rank INTEGER,manual_order_source TEXT,
                    manual_order_source_modified_at TEXT);
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
                         (rows[0]['manual_order_source'],
                          rows[0]['manual_order_source_modified_at']))

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
    def test_non_runnable_work_item_stays_in_roadmap_not_issue_inbox(self):
        client = FakeClient()
        waiting = item('new', group='waiting', execution_configured=False)
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [waiting], parent='inbox')
            projected = next(n for n in client.nodes if n['name'].startswith('☐ new'))
            parent = next(n for n in client.nodes if n['id'] == projected['parent_id'])
            self.assertEqual('Waiting', parent['name'])
            inbox = next(n for n in client.nodes if n['name'] == 'Inbox execution order')
            self.assertNotEqual(inbox['id'], projected['parent_id'])

    def test_reader_and_projection_use_real_issue_inbox_rows(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript('''
                CREATE TABLE issue_inbox(issue_id TEXT,description TEXT,repo TEXT,
                    observed_at_ms INTEGER,state TEXT,manual_rank INTEGER,
                    manual_order_source TEXT,manual_order_source_modified_at TEXT);
                INSERT INTO issue_inbox VALUES
                    ('issue:one','First issue','repo/a',10,'pending',2,'workflowy','wf-2'),
                    ('issue:two','Second issue','repo/b',20,'pending',1,'workflowy','wf-1');
                CREATE VIEW v_issue_inbox_pending_ordered AS
                    SELECT * FROM issue_inbox WHERE state='pending'
                    ORDER BY manual_rank,observed_at_ms,issue_id;
            ''')
            rows = read_issue_inbox(conn.serialize())
        self.assertEqual(['issue:two', 'issue:one'], [row['issue_id'] for row in rows])
        client = FakeClient()
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [item('roadmap-only')], issues=rows, parent='inbox')
        inbox = next(n for n in client.nodes if n['name'] == 'Inbox execution order')
        visible = [n for n in client.nodes if n.get('parent_id') == inbox['id']
                   and n['name'].startswith('☐')]
        self.assertEqual(['☐ Second issue', '☐ First issue'],
                         [node['name'] for node in visible])
        self.assertNotIn('issue:two', visible[0]['note'])
        self.assertIn('#repo-b', visible[0]['note'])
        detail = next(n for n in client.nodes
                      if n.get('parent_id') == visible[0]['id'] and n['name'] == 'Dettagli')
        self.assertEqual('Second issue', detail['note'])

    def test_inbox_title_is_compact_and_full_description_is_expandable(self):
        client = FakeClient()
        description = (
            'Repair the broken projection for C2_ISSUE_ID:issue:secret '
            'https://internal.example/task/secret while preserving canonical state. '
            + 'More diagnostic context ' * 20
        )
        row = issue(
            'issue:private-id', description=description,
            repo='https://github.com/gernalix/workflowy-importer.git',
            executor='codex',
        )
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [], issues=[row], parent='inbox')
            top = next(n for n in client.nodes if n['name'].startswith('☐ '))
            detail = next(n for n in client.nodes
                          if n.get('parent_id') == top['id'] and n['name'] == 'Dettagli')
            mapping = db.execute(
                "SELECT metadata_json FROM mappings WHERE external_key=?",
                ('issue-inbox:issue:private-id',),
            ).fetchone()
        self.assertLessEqual(len(top['name']), 54)
        for technical in ('C2_ISSUE_ID', 'issue:secret', 'private-id', 'https://'):
            self.assertNotIn(technical, top['name'])
            self.assertNotIn(technical, top['note'])
        self.assertEqual('#repo-workflowy-importer · #executor-codex', top['note'])
        self.assertNotIn('descrizione completa', top['note'])
        self.assertEqual(description, detail['note'])
        self.assertIn('issue:private-id', mapping['metadata_json'])

        technical = issue(
            'issue:hidden',
            description=(
                'GitHub capture issue #2330 / issue:e21c3528a43a4c539c4c23a1c3525e89 '
                'for PROMPT_ID=741905 is P0 and absolute priority.'
            ),
        )
        title, _note = issue_text(technical)
        for hidden in ('#2330', 'issue:e21', 'PROMPT_ID', '741905'):
            self.assertNotIn(hidden, title)
        self.assertIn('P0 and absolute priority', title)

    def test_long_inbox_description_stays_compact_and_retry_is_noop(self):
        client = FakeClient()
        row = issue('issue:long', description='A practical short title. ' + ('detail ' * 1000))
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [], issues=[row], parent='inbox')
            top = next(n for n in client.nodes if n['name'].startswith('☐ '))
            self.assertEqual('☐ A practical short title.', top['name'])
            self.assertLess(len(top['note']), 100)
            result = sync_items(client, db, [], issues=[row], parent='inbox')
        self.assertEqual(
            (0, 0, 0, 0),
            tuple(result[key] for key in ('created', 'updated', 'moved', 'deleted')),
        )

    def test_canonical_inbox_transition_deletes_stale_and_adds_new_item(self):
        client = FakeClient()
        first = issue('issue:first', description='First pending issue', observed_at_ms=1)
        stale = issue('issue:stale', description='Stale pending issue', observed_at_ms=2)
        added = issue('issue:added', description='New pending issue', observed_at_ms=3)
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [], issues=[first, stale], parent='inbox')
            stale_node = next(n for n in client.nodes if n['name'] == '☐ Stale pending issue')
            stale_detail = next(n for n in client.nodes
                                if n.get('parent_id') == stale_node['id'])
            result = sync_items(client, db, [], issues=[first, added], parent='inbox')
            mapped_stale = db.execute(
                "SELECT 1 FROM mappings WHERE external_key IN (?,?)",
                ('issue-inbox:issue:stale', 'issue-inbox-detail:issue:stale'),
            ).fetchall()
        inbox = next(n for n in client.nodes if n['name'] == 'Inbox execution order')
        visible = [n['name'] for n in client.nodes
                   if n.get('parent_id') == inbox['id'] and n['name'].startswith('☐ ')]
        self.assertEqual(['☐ First pending issue', '☐ New pending issue'], visible)
        self.assertNotIn(stale_node['id'], {n['id'] for n in client.nodes})
        self.assertNotIn(stale_detail['id'], {n['id'] for n in client.nodes})
        self.assertEqual([], mapped_stale)
        self.assertEqual(2, result['deleted'])

    def test_existing_roadmap_rows_repair_stale_structure_and_remain_visible(self):
        from workflowy_importer.roadmap_bridge import (
            GROUP_PREFIX, ROADMAP_ROOT_KEY, _mapping_set,
        )
        client = FakeClient()
        root = client.create_node('detached', 'Old Codex')
        roadmap = client.create_node('detached', 'Old Roadmap')
        ready = client.create_node('detached', 'Old Ready')
        mapped_item = client.create_node('detached', 'Old mapped item')
        rows = [
            item('ready', group='ready'), item('waiting', group='waiting'),
            item('running', group='running', status='running'),
            item('blocked', group='blocked', status='blocked'),
            item('done', group='completed', status='completed'),
            item('archived', group='archive', status='cancelled'),
        ]
        with closing(connect(':memory:')) as db:
            _mapping_set(db, ROADMAP_ROOT_KEY, root)
            _mapping_set(db, '__manual_scope__:roadmap', roadmap)
            _mapping_set(db, GROUP_PREFIX+'ready', ready)
            _mapping_set(db, 'wi:ready', mapped_item)
            sync_items(client, db, rows, parent='inbox')
            retry = sync_items(client, db, rows, parent='inbox')
        root_node = next(n for n in client.nodes if n['id'] == root)
        roadmap_node = next(n for n in client.nodes if n['id'] == roadmap)
        self.assertEqual('inbox', root_node['parent_id'])
        self.assertEqual(root, roadmap_node['parent_id'])
        self.assertIn('6 work item canonici', roadmap_node['note'])
        expected = {
            'ready': 'Ready', 'waiting': 'Waiting', 'running': 'Running',
            'blocked': 'Blocked / dependency context', 'done': 'Done',
            'archived': 'Archive',
        }
        for entity_id, group_name in expected.items():
            projected = next(n for n in client.nodes
                             if n['name'].endswith(' ' + entity_id))
            parent_node = next(n for n in client.nodes if n['id'] == projected['parent_id'])
            self.assertEqual(group_name, parent_node['name'])
            self.assertIn('1 work item canonici', parent_node['note'])
        self.assertEqual(
            (0, 0, 0, 0),
            tuple(retry[key] for key in ('created', 'updated', 'moved', 'deleted')),
        )

    def test_large_reversed_terminal_history_is_not_reordered_or_submitted(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        rows = [
            item(
                f'archive-{index:03d}', group='archive', status='completed',
                sort_order=index,
            )
            for index in range(225)
        ]
        rows += [
            item('done-parent', group='completed', status='completed', sort_order=300),
            *[
                item(
                    f'done-child-{index}', parent_id='done-parent',
                    group='completed', status='completed', sort_order=301 + index,
                )
                for index in range(3)
            ],
        ]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            archive = next(n for n in client.nodes if n['name'] == 'Archive')
            archived = [
                n for n in client.nodes
                if n.get('parent_id') == archive['id'] and n['name'].startswith('✅ archive-')
            ]
            done_parent = next(n for n in client.nodes if n['name'].endswith(' done-parent'))
            done_children = [
                n for n in client.nodes
                if n.get('parent_id') == done_parent['id']
                and n['name'].startswith('✅ done-child-')
            ]
            for node in archived:
                client.move_node(node['id'], archive['id'], position='top')
            for node in done_children:
                client.move_node(node['id'], done_parent['id'], position='top')

            result = sync_items(
                client, db, rows, parent='inbox', manual_order_adapter=adapter,
            )

        self.assertEqual(0, result['moved'])
        self.assertEqual(0, result['mutations_submitted'])
        adapter.assert_not_called()

    def test_running_reorder_remains_operational(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        rows = [
            item('running-one', group='running', status='running', sort_order=1),
            item('running-two', group='running', status='running', sort_order=2),
        ]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            running = next(n for n in client.nodes if n['name'] == 'Running')
            second = next(n for n in client.nodes if n['name'].endswith(' running-two'))
            client.move_node(second['id'], running['id'], position='top')
            result = sync_items(
                client, db, rows, parent='inbox', manual_order_adapter=adapter,
            )
            visible = [
                n['name'].split()[-1] for n in client.nodes
                if n.get('parent_id') == running['id'] and n['name'].startswith('👉')
            ]
        self.assertEqual(1, result['mutations_submitted'])
        adapter.assert_called_once_with(
            'set', scope='roadmap', ordered_ids=['running-two', 'running-one'],
            source_modified_at='1790000000',
        )
        self.assertEqual(['running-two', 'running-one'], visible)

    def test_genuine_reorder_submits_one_bulk_mutation_and_repeated_sync_is_quiet(self):
        client = FakeClient()
        adapter = Mock(return_value={'status':'ok'})
        rows = [item('one', sort_order=1), item('two', sort_order=2)]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox',
                       manual_order_adapter=adapter)
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            second = next(n for n in client.nodes if n['name'].startswith('☐ two'))
            client.move_node(second['id'], ready['id'], position='top')
            result = sync_items(client, db, rows, parent='inbox',
                                manual_order_adapter=adapter)
            self.assertEqual(1, result['mutations_submitted'])
            adapter.assert_called_once_with(
                'set', scope='roadmap', ordered_ids=['two', 'one'],
                source_modified_at='1790000000')
            again = sync_items(client, db, rows, parent='inbox',
                               manual_order_adapter=adapter)
            self.assertEqual(0, again['mutations_submitted'])
            self.assertEqual(1, adapter.call_count)

    def test_waiting_reorder_nested_by_drag_is_captured_and_flattened_without_rollback(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        rows = [
            item('one', group='waiting', sort_order=1),
            item('two', group='waiting', sort_order=2),
            item('three', group='waiting', sort_order=3),
        ]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            waiting = next(n for n in client.nodes if n['name'] == 'Waiting')
            one = next(n for n in client.nodes if n['name'].startswith('☐ one'))
            two = next(n for n in client.nodes if n['name'].startswith('☐ two'))
            # Workflowy can transiently express a drag as nesting under the
            # adjacent task rather than as a flat sibling move.
            client.move_node(one['id'], two['id'], position='top')
            result = sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            visible = [
                n['name'].split()[-1] for n in client.nodes
                if n.get('parent_id') == waiting['id'] and n['name'].startswith('☐')
            ]
        self.assertEqual(1, result['mutations_submitted'])
        self.assertEqual(0, result['warnings'])
        self.assertLessEqual(result['moved'], 2)
        adapter.assert_called_once_with(
            'set', scope='roadmap', ordered_ids=['two', 'one', 'three'],
            source_modified_at='1790000000',
        )
        self.assertEqual(['two', 'one', 'three'], visible)

    def test_waiting_nested_drag_is_order_intent_and_is_flattened_stably(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        rows = [
            item('one', group='waiting', sort_order=1),
            item('two', group='waiting', sort_order=2),
            item('three', group='waiting', sort_order=3),
        ]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            waiting = next(n for n in client.nodes if n['name'] == 'Waiting')
            one = next(n for n in client.nodes if n['name'].startswith('☐ one'))
            three = next(n for n in client.nodes if n['name'].startswith('☐ three'))
            # Workflowy can encode a visual drag as temporary nesting under a
            # sibling. The visible DFS order is one,three,two and must be
            # captured rather than rejected as a lifecycle-parent change.
            client.move_node(three['id'], one['id'], position='top')
            result = sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            visible = [n['name'].split()[-1] for n in client.nodes
                       if n.get('parent_id') == waiting['id'] and n['name'].startswith('☐')]
        self.assertEqual(0, result['warnings'])
        self.assertEqual(1, result['mutations_submitted'])
        adapter.assert_called_once_with(
            'set', scope='roadmap', ordered_ids=['one', 'three', 'two'],
            source_modified_at='1790000000')
        self.assertEqual(['one', 'three', 'two'], visible)
        self.assertLessEqual(result['moved'], 3)

    def test_issue_inbox_reorder_sends_only_real_issue_ids(self):
        client = FakeClient()
        adapter = Mock(return_value={'status':'ok'})
        issues = [
            issue('issue:one', description='First issue', observed_at_ms=1),
            issue('issue:two', description='Second issue', observed_at_ms=2),
        ]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [item('work-item')], issues=issues, parent='inbox',
                       manual_order_adapter=adapter)
            inbox = next(n for n in client.nodes if n['name'] == 'Inbox execution order')
            second = next(n for n in client.nodes if n['name'] == '☐ Second issue')
            client.move_node(second['id'], inbox['id'], position='top')
            sync_items(client, db, [item('work-item')], issues=issues, parent='inbox',
                       manual_order_adapter=adapter)
        adapter.assert_called_once_with(
            'set', scope='inbox', ordered_ids=['issue:two', 'issue:one'],
            source_modified_at='1790000000')
        self.assertNotIn('work-item', adapter.call_args.kwargs['ordered_ids'])

    def test_renderer_refresh_changes_order_without_echo_mutation(self):
        client = FakeClient()
        adapter = Mock(return_value={'status':'ok'})
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [item('one', sort_order=1), item('two', sort_order=2)],
                       parent='inbox', manual_order_adapter=adapter)
            refreshed = [
                item('one', sort_order=1, manual_rank=2, manual_order_source='workflowy'),
                item('two', sort_order=2, manual_rank=1, manual_order_source='workflowy'),
            ]
            result = sync_items(client, db, refreshed, parent='inbox',
                                manual_order_adapter=adapter)
            self.assertEqual(0, result['mutations_submitted'])
            adapter.assert_not_called()
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            visible = [n['name'].split()[-1] for n in client.nodes
                       if n.get('parent_id') == ready['id'] and n['name'].startswith('☐')]
            self.assertEqual(['two', 'one'], visible)

    def test_reset_submits_clear_for_scope_only(self):
        client = FakeClient()
        adapter = Mock(return_value={'status':'ok'})
        rows = [item(
            'one', manual_rank=1, manual_order_source='workflowy',
            manual_order_source_modified_at='wf-generation-1',
        )]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox',
                       manual_order_adapter=adapter)
            reset = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and next(p for p in client.nodes if p['id'] == n['parent_id'])['name'] == 'Roadmap')
            old_reset_id = reset['id']
            reset['completed'] = True
            result = sync_items(client, db, rows, parent='inbox',
                                manual_order_adapter=adapter)
            self.assertEqual(1, result['mutations_submitted'])
            adapter.assert_called_once_with(
                'clear', scope='roadmap', ordered_ids=None)
            fresh = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and next(p for p in client.nodes if p['id'] == n['parent_id'])['name'] == 'Roadmap')
            self.assertNotEqual(old_reset_id, fresh['id'])
            self.assertFalse(fresh.get('completed', False))

    def test_reset_waits_for_canonical_clear_readback_before_accepting_new_reorder(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        ranked = [
            item('one', sort_order=1, manual_rank=2, manual_order_source='workflowy',
                 manual_order_source_modified_at='old-generation'),
            item('two', sort_order=2, manual_rank=1, manual_order_source='workflowy',
                 manual_order_source_modified_at='old-generation'),
        ]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, ranked, parent='inbox', manual_order_adapter=adapter)
            roadmap = next(n for n in client.nodes if n['name'] == 'Roadmap')
            reset = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and n.get('parent_id') == roadmap['id'])
            reset['completed'] = True
            first = sync_items(client, db, ranked, parent='inbox', manual_order_adapter=adapter)
            # The writer clear is asynchronous. A second sync can still read
            # the old manual ranks; it must not echo the just-rendered AI order
            # back as a fresh manual override while force_ai is awaiting ack.
            second = sync_items(client, db, ranked, parent='inbox', manual_order_adapter=adapter)
            mapping = db.execute(
                "SELECT metadata_json FROM mappings WHERE external_key=?",
                ('__manual_scope__:roadmap',),
            ).fetchone()
        self.assertEqual((1, 0), (first['mutations_submitted'], second['mutations_submitted']))
        self.assertEqual(1, adapter.call_count)
        self.assertEqual('clear', adapter.call_args.args[0])
        self.assertTrue(__import__('json').loads(mapping['metadata_json']).get('force_ai'))

    def test_reset_waits_for_two_stable_clear_readbacks_before_releasing_force_ai(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        ranked = [
            item('one', sort_order=1, manual_rank=2, manual_order_source='workflowy',
                 manual_order_source_modified_at='old-generation'),
            item('two', sort_order=2, manual_rank=1, manual_order_source='workflowy',
                 manual_order_source_modified_at='old-generation'),
        ]
        cleared = [item('one', sort_order=1), item('two', sort_order=2)]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, ranked, parent='inbox', manual_order_adapter=adapter)
            roadmap = next(n for n in client.nodes if n['name'] == 'Roadmap')
            reset = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and n.get('parent_id') == roadmap['id'])
            reset['completed'] = True
            first = sync_items(client, db, ranked, parent='inbox', manual_order_adapter=adapter)
            second = sync_items(client, db, cleared, parent='inbox', manual_order_adapter=adapter)
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            two = next(n for n in client.nodes if n['name'].startswith('☐ two'))
            client.move_node(two['id'], ready['id'], position='top')
            third = sync_items(client, db, cleared, parent='inbox', manual_order_adapter=adapter)
            fourth = sync_items(client, db, cleared, parent='inbox', manual_order_adapter=adapter)
            fifth = sync_items(client, db, cleared, parent='inbox', manual_order_adapter=adapter)
            mapping = db.execute(
                "SELECT metadata_json FROM mappings WHERE external_key=?",
                ('__manual_scope__:roadmap',),
            ).fetchone()
        self.assertEqual([1, 0, 0, 0, 0], [
            first['mutations_submitted'], second['mutations_submitted'],
            third['mutations_submitted'], fourth['mutations_submitted'],
            fifth['mutations_submitted'],
        ])
        self.assertEqual(1, adapter.call_count)
        self.assertEqual('clear', adapter.call_args.args[0])
        metadata = __import__('json').loads(mapping['metadata_json'])
        self.assertNotIn('force_ai', metadata)
        self.assertNotIn('force_ai_stable_count', metadata)

    def test_repeated_reset_without_visible_override_still_clears_scope(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        with closing(connect(':memory:')) as db:
            sync_items(client, db, [item('one')], parent='inbox',
                       manual_order_adapter=adapter)
            roadmap = next(n for n in client.nodes if n['name'] == 'Roadmap')
            reset = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and n.get('parent_id') == roadmap['id'])
            first_id = reset['id']
            reset['completed'] = True
            first = sync_items(client, db, [item('one')], parent='inbox',
                               manual_order_adapter=adapter)
            fresh = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and n.get('parent_id') == roadmap['id'])
            self.assertNotEqual(first_id, fresh['id'])
            self.assertFalse(fresh.get('completed', False))
            fresh['completed'] = True
            second = sync_items(client, db, [item('one')], parent='inbox',
                                manual_order_adapter=adapter)
        self.assertEqual((1, 1), (first['mutations_submitted'], second['mutations_submitted']))
        self.assertEqual(2, adapter.call_count)
        for call in adapter.call_args_list:
            self.assertEqual(('clear',), call.args)
            self.assertEqual({'scope': 'roadmap', 'ordered_ids': None}, call.kwargs)

    def test_distinct_reset_nodes_resubmit_same_override_with_distinct_local_events(self):
        client = FakeClient()
        adapter = Mock(side_effect=[{'status':'ok'}, {'status':'ok'}])
        rows = [item(
            'one', manual_rank=1, manual_order_source='workflowy',
            manual_order_source_modified_at='wf-generation-1',
        )]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            roadmap = next(n for n in client.nodes if n['name'] == 'Roadmap')
            reset = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and n.get('parent_id') == roadmap['id'])
            first_reset_id = reset['id']
            reset['completed'] = True
            first = sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            fresh = next(n for n in client.nodes if n['name'] == 'Reset to AI order'
                         and n.get('parent_id') == roadmap['id'])
            self.assertNotEqual(first_reset_id, fresh['id'])
            fresh['completed'] = True
            second = sync_items(client, db, rows, parent='inbox', manual_order_adapter=adapter)
            keys = [row[0] for row in db.execute(
                "SELECT external_key FROM events WHERE source='workflowy_manual_order' ORDER BY id"
            )]
        self.assertEqual((1, 1), (first['mutations_submitted'], second['mutations_submitted']))
        self.assertEqual(2, adapter.call_count)
        self.assertEqual(2, len(keys))
        self.assertNotEqual(keys[0], keys[1])

    def test_canonical_status_and_dependency_change_converges_without_echo(self):
        client = FakeClient()
        adapter = Mock(return_value={'status': 'ok'})
        dependency = item('dependency', title='Dependency')
        target = item('target', group='waiting', dependencies=[{
            'work_item_id': 'dependency', 'title': 'Dependency', 'status': 'pending',
        }])
        with closing(connect(':memory:')) as db:
            sync_items(
                client, db, [dependency, target], parent='inbox',
                manual_order_adapter=adapter,
            )
            changed_dependency = item(
                'dependency', title='Dependency', status='completed', group='completed',
            )
            changed_target = item('target', group='ready', dependencies=[{
                'work_item_id': 'dependency', 'title': 'Dependency', 'status': 'completed',
            }])
            result = sync_items(
                client, db, [changed_dependency, changed_target], parent='inbox',
                manual_order_adapter=adapter,
            )
        target_node = next(n for n in client.nodes if n['name'].endswith(' target'))
        target_parent = next(n for n in client.nodes if n['id'] == target_node['parent_id'])
        dependency_node = next(n for n in client.nodes if n['name'].endswith(' Dependency'))
        dependency_parent = next(
            n for n in client.nodes if n['id'] == dependency_node['parent_id'])
        self.assertEqual('Ready', target_parent['name'])
        self.assertEqual('Done', dependency_parent['name'])
        self.assertNotIn('In attesa di:', target_node['note'])
        self.assertEqual(0, result['mutations_submitted'])
        adapter.assert_not_called()

    def test_drag_across_status_section_is_reverted_without_mutation(self):
        client = FakeClient()
        adapter = Mock(return_value={'status':'ok'})
        with closing(connect(':memory:')) as db:
            rows = [item('one')]
            sync_items(client, db, rows, parent='inbox',
                       manual_order_adapter=adapter)
            task = next(n for n in client.nodes if n['name'].startswith('☐ one'))
            blocked = next(n for n in client.nodes if n['name'] == 'Blocked / dependency context')
            client.move_node(task['id'], blocked['id'], position='top')
            result = sync_items(client, db, rows, parent='inbox',
                                manual_order_adapter=adapter)
            self.assertEqual(0, result['mutations_submitted'])
            self.assertEqual(1, result['warnings'])
            self.assertEqual('Ready', next(
                n for n in client.nodes if n['id'] == task['parent_id'])['name'])
            adapter.assert_not_called()

    def test_dag_mirror_is_not_sent_in_ordered_ids(self):
        client = FakeClient()
        adapter = Mock(return_value={'status':'ok'})
        dep1, dep2 = item('dep1'), item('dep2')
        target = item('target', dependencies=[
            {'work_item_id':'dep1','title':'dep1','status':'pending'},
            {'work_item_id':'dep2','title':'dep2','status':'pending'},
        ])
        rows = [dep1, dep2, target]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, rows, parent='inbox',
                       manual_order_adapter=adapter)
            mirror = next(n for n in client.nodes if n['name'].startswith('↪ Depends on'))
            self.assertIn('C2_REFERENCE_ID: dep2', mirror['note'])
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            task = next(n for n in client.nodes if n['name'].startswith('☐ target'))
            client.move_node(task['id'], ready['id'], position='top')
            sync_items(client, db, rows, parent='inbox',
                       manual_order_adapter=adapter)
            ordered_ids = adapter.call_args.kwargs['ordered_ids']
            self.assertEqual({'dep1','dep2','target'}, set(ordered_ids))
            self.assertNotIn(mirror['id'], ordered_ids)

    def test_manual_rank_round_trip_survives_refresh(self):
        client = FakeClient()
        adapter = Mock(return_value={'status':'ok'})
        initial = [item('one', sort_order=1), item('two', sort_order=2)]
        with closing(connect(':memory:')) as db:
            sync_items(client, db, initial, parent='inbox',
                       manual_order_adapter=adapter)
            ready = next(n for n in client.nodes if n['name'] == 'Ready')
            two = next(n for n in client.nodes if n['name'].startswith('☐ two'))
            client.move_node(two['id'], ready['id'], position='top')
            sync_items(client, db, initial, parent='inbox',
                       manual_order_adapter=adapter)
            ranked = [
                item('one', sort_order=1, manual_rank=2, manual_order_source='workflowy',
                     manual_order_source_modified_at='1790000000'),
                item('two', sort_order=2, manual_rank=1, manual_order_source='workflowy',
                     manual_order_source_modified_at='1790000000'),
            ]
            sync_items(client, db, ranked, parent='inbox',
                       manual_order_adapter=adapter)
            sync_items(client, db, ranked, parent='inbox',
                       manual_order_adapter=adapter)
            visible = [n['name'].split()[-1] for n in client.nodes
                       if n.get('parent_id') == ready['id'] and n['name'].startswith('☐')]
            self.assertEqual(['two','one'], visible)
            self.assertEqual(1, adapter.call_count)

    @patch('workflowy_importer.work_items_projection.subprocess.run')
    def test_helper_adapter_builds_argv_and_never_builds_raw_mutation(self, run):
        run.return_value = Mock(returncode=0, stdout='{"status":"queued"}', stderr='')
        result = run_manual_order_adapter(
            'set', scope='inbox', ordered_ids=['issue:two', 'issue:one'],
            source_modified_at=1790000000)
        self.assertEqual({'status':'queued'}, result)
        argv = run.call_args.args[0]
        self.assertEqual(
            [sys.executable, '-m', 'workflowy_importer.manual_order_control'],
            argv[:3],
        )
        self.assertEqual('set', argv[3])
        self.assertEqual(['--scope', 'inbox'], argv[4:6])
        self.assertEqual('1790000000', argv[7])
        self.assertEqual(['issue:two', 'issue:one'], argv[8:])
        self.assertNotIn('codex-roadmap.mutation.v1', ' '.join(argv))

    @patch('workflowy_importer.work_items_projection.subprocess.run')
    def test_helper_adapter_propagates_request_key_conflict(self, run):
        run.return_value = Mock(
            returncode=1,
            stdout='',
            stderr='MutationSubmitError: request_key_conflict:transport-key',
        )
        with self.assertRaisesRegex(RuntimeError, 'request_key_conflict:transport-key'):
            run_manual_order_adapter(
                'clear', scope='roadmap', ordered_ids=['one'])

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
