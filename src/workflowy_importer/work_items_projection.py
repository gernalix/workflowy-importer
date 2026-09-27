"""Read-only C2 projection. Work item identity survives PROMPT_ID materialization."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROADMAP_SECTIONS = (
    ('ready', 'Ready'), ('blocked', 'Blocked / dependency context'),
    ('paused', 'Paused'), ('running', 'Running'), ('waiting', 'Waiting'),
    ('completed', 'Done'), ('archive', 'Archive'),
)
DONE = {'completed', 'waived', 'cancelled', 'superseded'}
TERMINAL_PRESENTATION_GROUPS = frozenset({'completed', 'archive'})
SCOPE_PREFIX = '__manual_scope__:'
RESET_PREFIX = '__manual_reset__:'
MIRROR_PREFIX = '__dag_mirror__:'
ISSUE_DETAIL_PREFIX = 'issue-inbox-detail:'
PROJECTION_VERSION = 3
DEFAULT_ORDER_HELPER: Path | None = None
ROOT_SOURCE = 'Checklist 2.0 · le viste canoniche C2 sono la fonte autoritativa.'


def read_items(raw: bytes) -> list[dict] | None:
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.deserialize(raw)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        marker = conn.execute("SELECT type FROM sqlite_master WHERE name='prompts'").fetchone()
        if not marker or marker[0] != 'view':
            return None  # C2 is activated only by the canonical writer cutover.
        ready = {r[0] for r in conn.execute('SELECT work_item_id FROM v_work_item_runnable')}
        configured = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_item_execution_specs'").fetchone()
        configured_ids = ({r[0] for r in conn.execute('SELECT work_item_id FROM work_item_execution_specs')}
                          if configured else set())
        has_current_execution = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_work_item_execution_current'"
        ).fetchone())
        has_execution_history = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_work_item_execution_history'"
        ).fetchone())
        ready &= configured_ids
        summary_columns = {
            str(r['name']) for r in conn.execute('PRAGMA table_info(v_work_item_summary)')
        }
        manual_rank_column = next(
            (name for name in ('manual_rank', 'manual_order_rank') if name in summary_columns),
            None,
        )
        manual_order = (f'CASE WHEN {manual_rank_column} IS NULL THEN 1 ELSE 0 END, '
                        f'{manual_rank_column}, ' if manual_rank_column else '')
        items = [dict(r) for r in conn.execute(f'''SELECT * FROM v_work_item_summary
            ORDER BY {manual_order}COALESCE(sort_order,2147483647),created_at,work_item_id''')]
        by_id = {r['work_item_id']: r for r in items}
        for item in items:
            key = item['work_item_id']
            item['execution_configured'] = key in configured_ids
            current = (conn.execute(
                'SELECT * FROM v_work_item_execution_current WHERE work_item_id=?', (key,)
            ).fetchone() if has_current_execution else None)
            item['current_execution'] = dict(current) if current else None
            item['execution_history'] = ([dict(row) for row in conn.execute('''
                SELECT * FROM v_work_item_execution_history WHERE work_item_id=?
                ORDER BY claimed_at DESC,execution_id DESC''', (key,))]
                if has_execution_history else [])
            item['external_owner'] = (str(item.get('project_name') or '').casefold() == 'personalhub'
                                      or str(item.get('repo') or '').casefold().rstrip('/').endswith('/personalhub'))
            item['tags'] = [r[0] for r in conn.execute(
                'SELECT tag FROM work_item_tags WHERE work_item_id=? ORDER BY tag', (key,))]
            item['dependencies'] = [dict(r) for r in conn.execute('''
                SELECT w.work_item_id,w.title,w.status FROM work_item_dependencies d
                JOIN work_items w ON w.work_item_id=d.depends_on_work_item_id
                WHERE d.work_item_id=? AND d.required=1''', (key,))]
            status = item['status']
            try:
                recent = datetime.fromisoformat(str(item.get('updated_at')).replace('Z','+00:00')) >= datetime.now(timezone.utc)-timedelta(days=14)
            except (TypeError, ValueError):
                recent = False
            item['group'] = ('running' if status == 'running' else
                ('completed' if recent else 'archive') if status in {'completed','waived'} else
                'archive' if status in {'cancelled','superseded','unknown'} else
                'blocked' if status in {'failed','blocked','needs_fix'} else
                'paused' if status == 'paused' else
                'ready' if key in ready else 'waiting')
            # Reject malformed trees before any remote change.
            seen = {key}
            parent = item['parent_id']
            while parent:
                if parent in seen or parent not in by_id:
                    raise ValueError('invalid_work_item_hierarchy')
                seen.add(parent)
                parent = by_id[parent]['parent_id']
        unlocks: dict[str, list[dict]] = {}
        for item in items:
            for dependency in item['dependencies']:
                unlocks.setdefault(dependency['work_item_id'], []).append({
                    'work_item_id': item['work_item_id'],
                    'title': item['title'],
                    'status': item['status'],
                })
        for item in items:
            item['unlocks'] = unlocks.get(item['work_item_id'], [])
        return items


def read_issue_inbox(raw: bytes) -> list[dict]:
    """Read the backend-owned pending issue order when that rolling view exists."""
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.deserialize(raw)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        marker = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_issue_inbox_pending_ordered'"
        ).fetchone()
        if not marker:
            return []
        return [dict(row) for row in conn.execute(
            'SELECT * FROM v_issue_inbox_pending_ordered')]


def item_text(item: dict, links: dict[str, str]) -> tuple[str, str]:
    esc = lambda value: html.escape(str(value or ''), quote=True)
    icon = '✅' if item['status'] in DONE else '👉' if item['status'] == 'running' else '☐'
    identity = f"[{item['prompt_id']}] " if item['prompt_id'] else ''
    name = f"{icon} {identity}{esc(item['title'])}"
    lines = [f"✅ {item['completed_actionable']}/{item['total_actionable']} · {item['progress_percent']:g}%"]
    lines.append('C2_ENTITY_ID: ' + esc(item['work_item_id']))
    if item.get('objective'):
        lines.append('Cosa fa: ' + esc(item['objective']))
    if item.get('external_owner'):
        lines.append('Gestito da worker esterno PH — non assegnabile da questo supervisor.')
    execution = item.get('current_execution')
    if execution:
        executor = esc(execution.get('executor'))
        worker = execution.get('worker_ref')
        detail = executor + (f' · {esc(worker)}' if worker else '')
        uri = execution.get('conversation_ref_uri')
        if uri:
            label = 'Apri chat/thread' if execution.get('conversation_ref_type') in {
                'chatgpt_web', 'codex_thread'
            } else 'Apri riferimento'
            detail += f' · <a href="{esc(uri)}">{label}</a>'
        else:
            detail += ' · nessuna chat'
        lines.append('Executor corrente: ' + detail)
    if item['current_action']:
        lines.append('👉 ' + esc(item['current_action']))
    if item['next_action']:
        lines.append('Next action: ' + esc(item['next_action']))
    if item['blocker']:
        lines.append('Blocco: ' + esc(item['blocker']))
    waiting = [d for d in item['dependencies'] if d['status'] not in DONE]
    if waiting:
        lines.append('In attesa di: ' + ', '.join(esc(d['title']) for d in waiting))
    elif item['executor_policy'] == 'human' and item['status'] not in DONE:
        lines.append('In attesa di un intervento umano.')
    elif item['group'] in {'intake', 'waiting'} and not item['blocker'] and not item.get('external_owner'):
        lines.append('In attesa: mancano dati di esecuzione.' if item.get('execution_configured') is False
                     else 'In attesa di una dipendenza o risorsa disponibile.')
    project_tag = '#progetto-' + re.sub(r'[^a-z0-9]+', '-', str(item['project_name'] or '').lower()).strip('-')
    lines.append(' · '.join(esc(v) for v in (
        item['project_name'], project_tag, '#executor-' + item['executor_policy'], '#stato-' + item['status'],
        *['#' + t for t in item['tags']]) if v))
    for dep in item['dependencies']:
        if dep['work_item_id'] in links:
            lines.append(f'<a href="https://workflowy.com/#/{links[dep["work_item_id"]]}">{esc(dep["title"])}</a>')
    return name, '\n'.join(lines)


def _manual_rank(item: dict) -> object | None:
    for key in ('manual_rank', 'manual_order_rank'):
        if item.get(key) is not None:
            return item[key]
    return None


def _manual_source(item: dict) -> object | None:
    for key in ('manual_source', 'manual_order_source', 'order_source'):
        if item.get(key) is not None:
            return item[key]
    return None


def _node_sort_key(entry: tuple[int, dict]) -> tuple[bool, float, int]:
    index, node = entry
    try:
        priority = float(node.get('priority'))
    except (TypeError, ValueError):
        priority = float(index)
        missing = True
    else:
        missing = False
    return missing, priority, index


def _vertical_ids(nodes: dict[str, dict], root: str, canonical: dict[str, str]) -> list[str]:
    """Return canonical ids in Workflowy's visible DFS order."""
    children: dict[str, list[tuple[int, dict]]] = {}
    for index, node in enumerate(nodes.values()):
        children.setdefault(str(node.get('parent_id') or ''), []).append((index, node))
    for values in children.values():
        values.sort(key=_node_sort_key)
    by_node = {node_id: entity_id for entity_id, node_id in canonical.items()}
    result: list[str] = []
    seen: set[str] = set()

    def visit(parent_id: str) -> None:
        if parent_id in seen:
            return
        seen.add(parent_id)
        for _index, child in children.get(parent_id, []):
            child_id = str(child['id'])
            entity_id = by_node.get(child_id)
            if entity_id:
                result.append(entity_id)
            visit(child_id)

    visit(root)
    return result


def _children_in_order(nodes: dict[str, dict], parent: str) -> list[str]:
    values = [(index, node) for index, node in enumerate(nodes.values())
              if str(node.get('parent_id') or '') == parent]
    values.sort(key=_node_sort_key)
    return [str(node['id']) for _index, node in values]


def _state_fingerprint(order: list[str], expected_parents: dict[str, str]) -> str:
    payload = json.dumps(
        {'order': order, 'parents': expected_parents, 'version': PROJECTION_VERSION},
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_modified_at(nodes: dict[str, dict], canonical: dict[str, str]) -> object:
    values = [nodes[node_id].get('modifiedAt') for node_id in canonical.values()
              if node_id in nodes and nodes[node_id].get('modifiedAt') is not None]
    if not values:
        return ''
    try:
        return max(values, key=float)
    except (TypeError, ValueError):
        return max(values, key=lambda value: str(value))


def _reparent_stays_in_expected_section(
    nodes: dict[str, dict], node_id: str, expected_parent: str,
    canonical_node_ids: set[str],
) -> bool:
    """Treat temporary nesting under a same-section task as reorder intent."""
    current = str(nodes.get(node_id, {}).get('parent_id') or '')
    seen: set[str] = set()
    while current and current not in seen:
        if current == expected_parent:
            return True
        if current not in canonical_node_ids:
            return False
        seen.add(current)
        current = str(nodes.get(current, {}).get('parent_id') or '')
    return False


def _edge_move_plan(current: list[str], desired: list[str]) -> tuple[list[str], list[str]]:
    """Minimize top/bottom API moves by keeping the longest desired slice in place."""
    if current == desired:
        return [], []
    positions = {node_id: index for index, node_id in enumerate(current)}
    best_start = best_end = 0
    for start in range(len(desired)):
        last = -1
        end = start
        while end < len(desired):
            pos = positions.get(desired[end])
            if pos is None or pos <= last:
                break
            last = pos
            end += 1
        if end - start > best_end - best_start:
            best_start, best_end = start, end
    return desired[:best_start], desired[best_end:]


def _converge_sequence(client, nodes: dict[str, dict], owner: str,
                       sequence: list[str]) -> int:
    current = [node_id for node_id in _children_in_order(nodes, owner)
               if node_id in set(sequence)]
    if current == sequence:
        return 0
    prefix, suffix = _edge_move_plan(current, sequence)
    moved = 0
    for node_id in reversed(prefix):
        client.move_node(node_id, owner, position='top')
        moved += 1
    for node_id in suffix:
        client.move_node(node_id, owner, position='bottom')
        moved += 1
    return moved


def _human_tag(prefix: str, value: object) -> str | None:
    text = str(value or '').strip().rstrip('/').removesuffix('.git')
    if not text:
        return None
    if prefix == 'repo':
        text = text.rsplit('/', 1)[-1]
    slug = re.sub(r'[^a-z0-9]+', '-', text.casefold()).strip('-')
    return f'#{prefix}-{slug}' if slug else None


def _compact_issue_title(description: object, *, limit: int = 52) -> str:
    text = html.unescape(str(description or ''))
    text = re.sub(r'https?://\S+|www\.\S+', ' ', text, flags=re.IGNORECASE)
    text = re.sub(
        r'\b(?:C2_[A-Z0-9_]+|PROMPT_ID|RUN_ID|WORK_ITEM_ID)\s*[:=]\s*\S+',
        ' ', text, flags=re.IGNORECASE,
    )
    text = re.sub(r'\b(?:wi|issue|run|prompt|task):[A-Za-z0-9_.:-]+', ' ', text,
                  flags=re.IGNORECASE)
    text = re.sub(r'(?<!\w)#\d+\b', ' ', text)
    text = re.sub(
        r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
        ' ', text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r'^(?:P0\s*[—-]\s*)?(?:LIVE\s+)?(?:regression of completed Workflowy\s*:|'
        r'(?:Retrospective (?:finding|bottleneck|friction)|Cross-cutting optimization[^:]*)'
        r'(?: from ChatGPT conversation)?[^:]*:|ChatGPT/RDC diagnostic bug:|'
        r'ChatGPT RDC supervisor:|Supervisor watcher bootstrap bug:|'
        r'Amendment to the mass ChatGPT retrospective task\s*\([^)]*\):)\s*',
        '', text, flags=re.IGNORECASE,
    )
    text = re.sub(r'\s+', ' ', text).strip(' \t\r\n-:;,.')
    if not text:
        return 'Issue da triagiare'
    sentence = re.split(r'(?<=[.!?])\s+', text, maxsplit=1)[0]
    candidate = sentence if len(sentence) <= limit else text
    if len(candidate) <= limit:
        return candidate
    clipped = candidate[:limit - 1].rsplit(' ', 1)[0].rstrip(' \t\r\n-:;,.)')
    return (clipped or candidate[:limit - 1]).rstrip() + '…'


def issue_text(issue: dict) -> tuple[str, str]:
    tags = [
        _human_tag('repo', issue.get('repo')),
        _human_tag('executor', issue.get('executor')),
    ]
    note = ' · '.join(tag for tag in tags if tag)
    return '☐ ' + html.escape(_compact_issue_title(issue.get('description')), quote=True), note


def issue_detail_text(issue: dict) -> tuple[str, str]:
    description = html.escape(str(issue.get('description') or ''), quote=True)
    return 'Dettagli', description


def _semantic_event_key(action: str, scope: str, payload: object) -> str:
    encoded = json.dumps(
        {'action': action, 'scope': scope, 'payload': payload},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode()
    return f'workflowy-{action}-manual-order-{scope}-' + hashlib.sha256(encoded).hexdigest()[:24]


def run_manual_order_adapter(
    action: str,
    *,
    scope: str,
    ordered_ids: list[str] | None = None,
    source_modified_at: str | None = None,
    helper: Path | None = DEFAULT_ORDER_HELPER,
) -> dict:
    """Invoke the fenced backend helper; never construct a mutation document here."""
    if action not in {'set', 'clear'}:
        raise ValueError('invalid_manual_order_action')
    argv = ([sys.executable, str(helper)] if helper else
            [sys.executable, '-m', 'workflowy_importer.manual_order_control'])
    argv += [action, '--scope', scope]
    if action == 'set':
        if ordered_ids is None or source_modified_at is None:
            raise ValueError('manual_order_set_arguments_required')
        argv += ['--source-modified-at', str(source_modified_at), *ordered_ids]
    elif ordered_ids:
        argv += ordered_ids
    proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(
            'workflowy_order_helper_failed:' + detail)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('workflowy_order_helper_invalid_response') from exc
    if not isinstance(result, dict) or result.get('status') not in {'ok', 'queued'}:
        raise RuntimeError(f'workflowy_order_helper_rejected:{result}')
    return result


def _sync_items_once(
    client,
    db,
    items: list[dict],
    *,
    parent: str,
    issues: list[dict] | None = None,
    manual_order_adapter: Callable[..., dict] = run_manual_order_adapter,
) -> dict:
    from .roadmap_bridge import (
        GROUP_PREFIX,
        ROADMAP_NAMESPACE,
        ROADMAP_ROOT_KEY,
        _hydrate_mapped_nodes,
        _mapping_get,
        _mapping_set,
        roadmap_projection_note,
    )
    issues = issues or []
    nodes = {str(n['id']): n for n in client.export_nodes() if n.get('id')}
    keys = [ROADMAP_ROOT_KEY]
    keys += [SCOPE_PREFIX + scope for scope in ('inbox', 'roadmap')]
    keys += [RESET_PREFIX + scope for scope in ('inbox', 'roadmap')]
    keys += [GROUP_PREFIX + k for k, _ in ROADMAP_SECTIONS]
    keys += ['wi:' + i['work_item_id'] for i in items]
    keys += ['issue-inbox:' + issue['issue_id'] for issue in issues]
    keys += [ISSUE_DETAIL_PREFIX + issue['issue_id'] for issue in issues]
    keys += ['wi-history:' + i['work_item_id'] for i in items
             if len(i.get('execution_history') or []) > 1]
    for item in items:
        keys += [MIRROR_PREFIX + item['work_item_id'] + ':dep:' + dep['work_item_id']
                 for dep in item.get('dependencies', [])[1:]]
        keys += [MIRROR_PREFIX + item['work_item_id'] + ':unlock:' + dep['work_item_id']
                 for dep in item.get('unlocks', [])[1:]]
    keys += [i['prompt_id'] for i in items if i['prompt_id']]
    mapped_issue_keys = [str(row['external_key']) for row in db.execute(
        """SELECT external_key FROM mappings
           WHERE namespace=? AND (external_key LIKE 'issue-inbox:%'
                                  OR external_key LIKE 'issue-inbox-detail:%')""",
        (ROADMAP_NAMESPACE,),
    )]
    keys += mapped_issue_keys
    _hydrate_mapped_nodes(client, db, nodes, keys)
    counts = dict(created=0, updated=0, moved=0, deleted=0)

    def put(key, parent_id, name, note='', layout='bullets', legacy=None,
            count_update=True, metadata=None, reparent=True):
        mapped = _mapping_get(db, key) or (_mapping_get(db, legacy) if legacy else None)
        previous_metadata = mapped[1] if mapped else {}
        node = nodes.get(mapped[0]) if mapped else None
        if node is None:
            node_id = client.create_node(parent_id, name, note=note, layout_mode=layout)
            node = {'id': node_id, 'parent_id': parent_id, 'name': name, 'note': note,
                    'data': {'layoutMode': layout}}
            nodes[node_id] = node
            counts['created'] += 1
        else:
            node_id = str(node['id'])
            current_layout = (node.get('data') or {}).get('layoutMode')
            if node.get('name') != name or str(node.get('note') or '') != note or current_layout != layout:
                client.update_node(node_id, name, note=note, layout_mode=layout)
                node.update(name=name, note=note)
                node.setdefault('data', {})['layoutMode'] = layout
                if count_update:
                    counts['updated'] += 1
            if reparent and node.get('parent_id') != parent_id:
                client.move_node(node_id, parent_id, position='bottom')
                node['parent_id'] = parent_id
                counts['moved'] += 1
        wanted_metadata = dict(previous_metadata)
        wanted_metadata.update(metadata or {})
        wanted_metadata['authority'] = 'work_items'
        _mapping_set(db, key, node_id, wanted_metadata)
        db.commit()  # Preserve acknowledged remote identities across crashes.
        return node_id

    root = put(ROADMAP_ROOT_KEY, parent, 'Codex', roadmap_projection_note(pending=True, source=ROOT_SOURCE), 'h1', count_update=False)
    group_counts = {
        key: sum(1 for item in items if item['group'] == key)
        for key, _label in ROADMAP_SECTIONS
    }
    scope_ids = {
        'inbox': put(SCOPE_PREFIX+'inbox', root, 'Inbox execution order',
                     f'{len(issues)} issue canoniche pending. Ordine manuale dell’inbox; '
                     'il lifecycle resta sola lettura.', 'h2'),
        'roadmap': put(SCOPE_PREFIX+'roadmap', root, 'Roadmap',
                      f'{len(items)} work item canonici in {len(ROADMAP_SECTIONS)} sezioni. '
                      'Espandi per navigare Ready e il contesto di blocchi/dipendenze; '
                      'il lifecycle resta sola lettura.', 'h2'),
    }
    reset_ids = {
        scope: put(RESET_PREFIX+scope, scope_ids[scope], 'Reset to AI order',
                   'Completa questa azione per rimuovere l’ordine manuale dello scope.', 'todo')
        for scope in ('inbox', 'roadmap')
    }
    groups = {
        key: put(GROUP_PREFIX+key, scope_ids['roadmap'], label,
                 f'{group_counts[key]} work item canonici.', layout='h3')
        for key, label in ROADMAP_SECTIONS
    }

    current_issue_ids = {issue['issue_id'] for issue in issues}
    mapped_issue_ids = {
        key.removeprefix('issue-inbox:')
        if key.startswith('issue-inbox:')
        else key.removeprefix(ISSUE_DETAIL_PREFIX)
        for key in mapped_issue_keys
    }
    for issue_id in sorted(mapped_issue_ids - current_issue_ids):
        key = 'issue-inbox:' + issue_id
        detail_key = ISSUE_DETAIL_PREFIX + issue_id
        detail = _mapping_get(db, detail_key)
        mapped = _mapping_get(db, key)
        for node_id in (
            detail[0] if detail and detail[0] in nodes else None,
            mapped[0] if mapped and mapped[0] in nodes else None,
        ):
            if node_id is None:
                continue
            client.delete_node(node_id)
            nodes.pop(node_id, None)
            counts['deleted'] += 1
        db.execute(
            'DELETE FROM mappings WHERE namespace=? AND external_key IN (?,?)',
            (ROADMAP_NAMESPACE, key, detail_key),
        )
        db.commit()
    links = {}
    for item in items:
        name, note = item_text(item, links)
        initial_parent = groups[item['group']]
        links[item['work_item_id']] = put(
            'wi:'+item['work_item_id'], initial_parent, name, note,
            legacy=item['prompt_id'], reparent=False)
    # All dependency targets now have stable links.
    for item in items:
        name, note = item_text(item, links)
        put('wi:'+item['work_item_id'], nodes[links[item['work_item_id']]]['parent_id'], name, note)

    issue_links = {}
    for issue in issues:
        name, note = issue_text(issue)
        issue_links[issue['issue_id']] = put(
            'issue-inbox:'+issue['issue_id'], scope_ids['inbox'], name, note,
            reparent=False, metadata={
                'entity_kind': 'issue_inbox',
                'entity_id': issue['issue_id'],
            })
        detail_name, detail_note = issue_detail_text(issue)
        put(
            ISSUE_DETAIL_PREFIX+issue['issue_id'], issue_links[issue['issue_id']],
            detail_name, detail_note, metadata={
                'entity_kind': 'issue_inbox_detail',
                'entity_id': issue['issue_id'],
            },
        )

    expected_parents: dict[str, str] = {}
    by_item = {item['work_item_id']: item for item in items}
    for item in items:
        desired_parent = groups[item['group']]
        parent_item = by_item.get(item.get('parent_id'))
        if parent_item and parent_item['group'] == item['group']:
            desired_parent = links[parent_item['work_item_id']]
        expected_parents[item['work_item_id']] = desired_parent
    issue_expected_parents = {
        issue['issue_id']: scope_ids['inbox'] for issue in issues
    }
    by_issue = {issue['issue_id']: issue for issue in issues}
    scope_canonical = {'inbox': issue_links, 'roadmap': links}
    scope_rows = {'inbox': by_issue, 'roadmap': by_item}
    scope_expected = {'inbox': issue_expected_parents, 'roadmap': expected_parents}
    scope_order_canonical = {
        'inbox': issue_links,
        'roadmap': {
            item['work_item_id']: links[item['work_item_id']]
            for item in items
            if item['group'] not in TERMINAL_PRESENTATION_GROUPS
        },
    }
    mutations_submitted = 0
    warnings = 0
    pending_orders: dict[str, list[str] | None] = {}

    # Interpret the export before renderer moves. Only an order change against
    # our last acknowledged projection is human intent. Parent changes across
    # lifecycle sections are reverted and never become lifecycle mutations.
    for scope in ('inbox', 'roadmap'):
        scope_key = SCOPE_PREFIX + scope
        scope_mapping = _mapping_get(db, scope_key)
        state = dict(scope_mapping[1] if scope_mapping else {})
        canonical = scope_canonical[scope]
        order_canonical = scope_order_canonical[scope]
        current_order = _vertical_ids(nodes, scope_ids[scope], order_canonical)
        if isinstance(state.get('pending_order'), list):
            pending = [entity_id for entity_id in state['pending_order']
                       if entity_id in order_canonical]
            if pending:
                state['pending_order'] = pending
            else:
                state.pop('pending_order', None)
        last_order = state.get('last_render_order')
        last_parents = state.get('expected_parents') or {}
        state_is_current = state.get('projection_version') == PROJECTION_VERSION
        canonical_node_ids = set(canonical.values())
        parent_changed = state_is_current and any(
            node_id in nodes
            and str(nodes[node_id].get('parent_id') or '') != str(last_parents.get(item_id) or '')
            and not _reparent_stays_in_expected_section(
                nodes, node_id, str(last_parents.get(item_id) or ''), canonical_node_ids,
            )
            for item_id, node_id in canonical.items() if item_id in last_parents
        )

        reset_node = nodes.get(reset_ids[scope])
        reset_requested = bool(reset_node and reset_node.get('completed'))
        if reset_requested:
            overrides = sorted(
                ({
                    'entity_id': entity_id,
                    'rank': _manual_rank(row),
                    'source_modified_at': row.get('manual_order_source_modified_at'),
                } for entity_id, row in scope_rows[scope].items()
                 if _manual_rank(row) is not None),
                key=lambda value: value['entity_id'],
            )
            request_key = _semantic_event_key('clear', scope, overrides)
            already_submitted = db.execute(
                'SELECT 1 FROM events WHERE source=? AND external_key=?',
                ('workflowy_manual_order', request_key),
            ).fetchone()
            if overrides and not already_submitted:
                manual_order_adapter(
                    'clear', scope=scope,
                    ordered_ids=[entry['entity_id'] for entry in overrides],
                )
                db.execute('INSERT INTO events(source,external_key,payload_json) VALUES(?,?,?)',
                           ('workflowy_manual_order', request_key,
                            json.dumps({'scope': scope, 'action': 'clear',
                                        'overrides': overrides}, sort_keys=True)))
                db.commit()
                mutations_submitted += 1
            client.delete_node(reset_ids[scope])
            nodes.pop(reset_ids[scope], None)
            reset_ids[scope] = put(RESET_PREFIX+scope, scope_ids[scope], 'Reset to AI order',
                                   'Completa questa azione per rimuovere l’ordine manuale dello scope.',
                                   'todo', metadata={'reset_generation': request_key})
            state.pop('pending_order', None)
            state['force_ai'] = True
        elif parent_changed:
            warnings += 1
        elif (state_is_current and isinstance(last_order, list)
              and set(current_order) == set(last_order)
              and current_order != last_order):
            modified = str(_source_modified_at(nodes, order_canonical))
            request_key = _semantic_event_key(
                'set', scope,
                {'ordered_ids': current_order, 'source_modified_at': modified},
            )
            if not db.execute('SELECT 1 FROM events WHERE source=? AND external_key=?',
                              ('workflowy_manual_order', request_key)).fetchone():
                manual_order_adapter(
                    'set', scope=scope, ordered_ids=current_order,
                    source_modified_at=modified)
                db.execute('INSERT INTO events(source,external_key,payload_json) VALUES(?,?,?)',
                           ('workflowy_manual_order', request_key,
                            json.dumps({'scope': scope, 'ordered_ids': current_order}, sort_keys=True)))
                db.commit()
                mutations_submitted += 1
            state['pending_order'] = current_order
            state.pop('force_ai', None)
        pending_orders[scope] = state.get('pending_order') if isinstance(state.get('pending_order'), list) else None
        _mapping_set(db, scope_key, scope_ids[scope], state)

    def order_key(row: dict, entity_id: str, scope: str) -> tuple:
        state = _mapping_get(db, SCOPE_PREFIX + scope)
        metadata = state[1] if state else {}
        pending = pending_orders.get(scope)
        if pending and entity_id in pending:
            return (0, pending.index(entity_id))
        if metadata.get('force_ai'):
            ai_value = row.get('sort_order') if scope == 'roadmap' else row.get('observed_at_ms')
            return (1, ai_value is None, ai_value or 0, entity_id)
        rank = _manual_rank(row)
        ai_value = row.get('sort_order') if scope == 'roadmap' else row.get('observed_at_ms')
        return (0 if rank is not None else 1, rank if rank is not None else 0,
                ai_value is None, ai_value or 0, entity_id)

    ordered_items = sorted(
        items, key=lambda item: order_key(item, item['work_item_id'], 'roadmap'))
    ordered_issues = sorted(
        issues, key=lambda issue: order_key(issue, issue['issue_id'], 'inbox'))
    for item in ordered_items:
        node_id = links[item['work_item_id']]
        desired_parent = expected_parents[item['work_item_id']]
        if nodes[node_id].get('parent_id') != desired_parent:
            client.move_node(node_id, desired_parent, position='bottom')
            nodes[node_id]['parent_id'] = desired_parent
            counts['moved'] += 1
    for issue in ordered_issues:
        node_id = issue_links[issue['issue_id']]
        desired_parent = issue_expected_parents[issue['issue_id']]
        if nodes[node_id].get('parent_id') != desired_parent:
            client.move_node(node_id, desired_parent, position='bottom')
            nodes[node_id]['parent_id'] = desired_parent
            counts['moved'] += 1

    sequences: dict[str, list[str]] = {
        scope_ids['inbox']: [reset_ids['inbox'], *[
            issue_links[issue['issue_id']] for issue in ordered_issues]],
        scope_ids['roadmap']: [reset_ids['roadmap'], *groups.values()],
    }
    for item in ordered_items:
        if item['group'] in TERMINAL_PRESENTATION_GROUPS:
            continue
        sequences.setdefault(expected_parents[item['work_item_id']], []).append(
            links[item['work_item_id']])
    for owner, sequence in sequences.items():
        counts['moved'] += _converge_sequence(client, nodes, owner, sequence)

    history_links = {}
    for item in items:
        current_id = (item.get('current_execution') or {}).get('execution_id')
        history = [row for row in item.get('execution_history') or []
                   if row.get('execution_id') != current_id]
        if not history:
            continue
        history_note = []
        for row in history:
            entry = f"{html.escape(str(row.get('executor') or 'unknown'))} · {html.escape(str(row.get('status') or 'unknown'))} · {html.escape(str(row.get('claimed_at') or ''))}"
            uri = row.get('conversation_ref_uri')
            if uri:
                entry += f' · <a href="{html.escape(str(uri), quote=True)}">Apri</a>'
            else:
                entry += ' · nessuna chat'
            history_note.append(entry)
        history_links[item['work_item_id']] = put('wi-history:'+item['work_item_id'],
            links[item['work_item_id']], f'🕘 Storico executor ({len(history)})',
            '\n'.join(history_note))

    # Secondary DAG edges are presentation-only references. Their ids are
    # deliberately outside the canonical map used by ordering mutations.
    for item in items:
        references = [('dep', row) for row in item.get('dependencies', [])[1:]]
        references += [('unlock', row) for row in item.get('unlocks', [])[1:]]
        for relation, target in references:
            target_id = target['work_item_id']
            label = 'Depends on' if relation == 'dep' else 'Unlocks'
            note = (f'C2_REFERENCE_ID: {target_id}\n'
                    f'<a href="https://workflowy.com/#/{links[target_id]}">Open canonical task</a>')
            put(MIRROR_PREFIX+item['work_item_id']+':'+relation+':'+target_id,
                links[item['work_item_id']], f'↪ {label}: {html.escape(str(target["title"]))}',
                note)

    # Keep the established non-destructive retirement behavior for obsolete
    # dashboard groups; user-owned children are never deleted.
    for legacy in ('integration', 'unknown'):
        mapped = _mapping_get(db, GROUP_PREFIX + legacy)
        if not mapped or mapped[0] not in nodes or mapped[0] in groups.values():
            continue
        node = nodes[mapped[0]]
        if node.get('parent_id') != groups['archive']:
            client.move_node(mapped[0], groups['archive'], position='bottom')
            node['parent_id'] = groups['archive']
            counts['moved'] += 1
        wanted = 'Legacy ' + legacy
        if node.get('name') != wanted:
            client.update_node(mapped[0], wanted)
            node['name'] = wanted
            counts['updated'] += 1

    # Store the exact projection after all renderer moves. On the next export,
    # only a divergence from this state can be interpreted as human ordering.
    projected_nodes = {str(n['id']): n for n in client.export_nodes() if n.get('id')}
    for scope in ('inbox', 'roadmap'):
        canonical = scope_canonical[scope]
        order_canonical = scope_order_canonical[scope]
        order = _vertical_ids(projected_nodes, scope_ids[scope], order_canonical)
        mapping = _mapping_get(db, SCOPE_PREFIX + scope)
        state = dict(mapping[1] if mapping else {})
        state.update({
            'projection_version': PROJECTION_VERSION,
            'last_render_order': order,
            'expected_parents': {entity_id: scope_expected[scope][entity_id]
                                 for entity_id in canonical},
            'last_render_fingerprint': _state_fingerprint(
                order, {entity_id: scope_expected[scope][entity_id]
                        for entity_id in canonical}),
            'last_render_source_modified_at': _source_modified_at(
                projected_nodes, order_canonical),
        })
        # Canonical workflowy ranks acknowledge the pending local intent.
        if (state.get('pending_order') == order and order_canonical
                and all(_manual_rank(scope_rows[scope][entity_id]) is not None
                        and str(_manual_source(scope_rows[scope][entity_id]) or '').casefold() == 'workflowy'
                        for entity_id in order_canonical)):
            state.pop('pending_order', None)
        if state.get('force_ai') and canonical and all(
                _manual_rank(scope_rows[scope][entity_id]) is None
                for entity_id in canonical):
            state.pop('force_ai', None)
        _mapping_set(db, SCOPE_PREFIX + scope, scope_ids[scope], state)
    put(ROADMAP_ROOT_KEY, parent, 'Codex', roadmap_projection_note(pending=False, source=ROOT_SOURCE), 'h1', count_update=False)
    db.commit()
    return dict(prompts=len(items), issues=len(issues), **counts,
                mutations_submitted=mutations_submitted, warnings=warnings)


def _projection_diagnostic(exc: Exception) -> str:
    message = re.sub(r'https?://\S+', '[url]', str(exc), flags=re.IGNORECASE)
    message = re.sub(r'\s+', ' ', message).strip()
    message = re.sub(r'[^A-Za-z0-9_.:\- /\[\]]+', '?', message)[:120]
    return type(exc).__name__ + (': ' + message if message else '')


def sync_items(
    client,
    db,
    items: list[dict],
    *,
    parent: str,
    issues: list[dict] | None = None,
    manual_order_adapter: Callable[..., dict] = run_manual_order_adapter,
) -> dict:
    try:
        return _sync_items_once(
            client, db, items, parent=parent, issues=issues,
            manual_order_adapter=manual_order_adapter,
        )
    except Exception as exc:
        from .roadmap_bridge import ROADMAP_ROOT_KEY, _mapping_get, roadmap_projection_note
        mapped = _mapping_get(db, ROADMAP_ROOT_KEY)
        if mapped:
            try:
                client.update_node(
                    mapped[0], 'Codex',
                    note=roadmap_projection_note(
                        pending=False, source=ROOT_SOURCE,
                        error=_projection_diagnostic(exc),
                    ),
                    layout_mode='h1',
                )
            except Exception:
                pass  # Preserve the primary projection failure.
        raise
