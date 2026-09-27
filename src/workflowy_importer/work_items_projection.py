"""Read-only C2 projection. Work item identity survives PROMPT_ID materialization."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Callable

ROADMAP_SECTIONS = (
    ('ready', 'Ready'), ('blocked', 'Blocked / dependency context'),
    ('paused', 'Paused'), ('running', 'Running'), ('waiting', 'Waiting'),
    ('completed', 'Done'), ('archive', 'Archive'),
)
DONE = {'completed', 'waived', 'cancelled', 'superseded'}
INTAKE_WINDOW = timedelta(days=14)
SCOPE_PREFIX = '__manual_scope__:'
RESET_PREFIX = '__manual_reset__:'
MIRROR_PREFIX = '__dag_mirror__:'
PROJECTION_VERSION = 1


def is_recent_intake(item: dict) -> bool:
    """New pending work stays visible without changing its waiting state."""
    try:
        created = datetime.fromisoformat(str(item.get('created_at')).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created >= datetime.now(timezone.utc) - INTAKE_WINDOW


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
                'ready' if key in ready else
                'intake' if is_recent_intake(item) else 'waiting')
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


def _scope_for(item: dict) -> str:
    return 'inbox' if item.get('group') == 'intake' else 'roadmap'


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


def _mutation_document(operation: dict) -> dict:
    return {
        'schema': 'codex-roadmap.mutation.v1',
        'actor': 'workflowy',
        'operations': [operation],
    }


def sync_items(
    client,
    db,
    items: list[dict],
    *,
    parent: str,
    submitter: Callable[[dict, str], dict] | None = None,
    roadmap_dir: Path = Path('~/projects/codex-roadmap'),
) -> dict:
    from .roadmap_bridge import (
        GROUP_PREFIX,
        ROADMAP_ROOT_KEY,
        _hydrate_mapped_nodes,
        _mapping_get,
        _mapping_set,
        _submit_with_local_writer,
        roadmap_projection_note,
    )
    nodes = {str(n['id']): n for n in client.export_nodes() if n.get('id')}
    keys = [ROADMAP_ROOT_KEY]
    keys += [SCOPE_PREFIX + scope for scope in ('inbox', 'roadmap')]
    keys += [RESET_PREFIX + scope for scope in ('inbox', 'roadmap')]
    keys += [GROUP_PREFIX + k for k, _ in ROADMAP_SECTIONS]
    keys += ['wi:' + i['work_item_id'] for i in items]
    keys += ['wi-history:' + i['work_item_id'] for i in items
             if len(i.get('execution_history') or []) > 1]
    for item in items:
        keys += [MIRROR_PREFIX + item['work_item_id'] + ':dep:' + dep['work_item_id']
                 for dep in item.get('dependencies', [])[1:]]
        keys += [MIRROR_PREFIX + item['work_item_id'] + ':unlock:' + dep['work_item_id']
                 for dep in item.get('unlocks', [])[1:]]
    keys += [i['prompt_id'] for i in items if i['prompt_id']]
    _hydrate_mapped_nodes(client, db, nodes, keys)
    counts = dict(created=0, updated=0, moved=0)

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

    root_source = 'Checklist 2.0 · work_items è la fonte canonica.'
    root = put(ROADMAP_ROOT_KEY, parent, 'Codex', roadmap_projection_note(pending=True, source=root_source), 'h1', count_update=False)
    scope_ids = {
        'inbox': put(SCOPE_PREFIX+'inbox', root, 'Inbox execution order',
                     'Ordine manuale dell’inbox. Il lifecycle resta sola lettura.', 'h2'),
        'roadmap': put(SCOPE_PREFIX+'roadmap', root, 'Roadmap',
                      'Ready e contesto di blocchi/dipendenze. Il lifecycle resta sola lettura.', 'h2'),
    }
    reset_ids = {
        scope: put(RESET_PREFIX+scope, scope_ids[scope], 'Reset to AI order',
                   'Completa questa azione per rimuovere l’ordine manuale dello scope.', 'todo')
        for scope in ('inbox', 'roadmap')
    }
    groups = {
        key: put(GROUP_PREFIX+key, scope_ids['roadmap'], label, layout='h3')
        for key, label in ROADMAP_SECTIONS
    }
    links = {}
    for item in items:
        name, note = item_text(item, links)
        initial_parent = scope_ids['inbox'] if _scope_for(item) == 'inbox' else groups[item['group']]
        links[item['work_item_id']] = put(
            'wi:'+item['work_item_id'], initial_parent, name, note,
            legacy=item['prompt_id'], reparent=False)
    # All dependency targets now have stable links.
    for item in items:
        name, note = item_text(item, links)
        put('wi:'+item['work_item_id'], nodes[links[item['work_item_id']]]['parent_id'], name, note)

    expected_parents: dict[str, str] = {}
    by_item = {item['work_item_id']: item for item in items}
    for item in items:
        scope = _scope_for(item)
        desired_parent = scope_ids['inbox'] if scope == 'inbox' else groups[item['group']]
        parent_item = by_item.get(item.get('parent_id'))
        if parent_item and _scope_for(parent_item) == scope and parent_item['group'] == item['group']:
            desired_parent = links[parent_item['work_item_id']]
        expected_parents[item['work_item_id']] = desired_parent

    submit = submitter or (
        lambda document, key: _submit_with_local_writer(roadmap_dir, document, key)
    )
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
        canonical = {
            item['work_item_id']: links[item['work_item_id']]
            for item in items if _scope_for(item) == scope
        }
        current_order = _vertical_ids(nodes, scope_ids[scope], canonical)
        last_order = state.get('last_render_order')
        last_parents = state.get('expected_parents') or {}
        state_is_current = state.get('projection_version') == PROJECTION_VERSION
        parent_changed = state_is_current and any(
            node_id in nodes
            and str(nodes[node_id].get('parent_id') or '') != str(last_parents.get(item_id) or '')
            for item_id, node_id in canonical.items() if item_id in last_parents
        )

        reset_node = nodes.get(reset_ids[scope])
        reset_requested = bool(reset_node and reset_node.get('completed'))
        if reset_requested:
            modified = reset_node.get('modifiedAt') or ''
            request_key = f'workflowy-clear-manual-order-{scope}-{reset_ids[scope]}-{modified}'
            if not db.execute('SELECT 1 FROM events WHERE source=? AND external_key=?',
                              ('workflowy_manual_order', request_key)).fetchone():
                submit(_mutation_document({'op': 'clear_manual_order', 'scope': scope}), request_key)
                db.execute('INSERT INTO events(source,external_key,payload_json) VALUES(?,?,?)',
                           ('workflowy_manual_order', request_key,
                            json.dumps({'scope': scope, 'action': 'clear'}, sort_keys=True)))
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
            modified = _source_modified_at(nodes, canonical)
            digest = hashlib.sha256(json.dumps(current_order).encode()).hexdigest()[:16]
            request_key = f'workflowy-set-manual-order-{scope}-{digest}-{modified}'
            if not db.execute('SELECT 1 FROM events WHERE source=? AND external_key=?',
                              ('workflowy_manual_order', request_key)).fetchone():
                submit(_mutation_document({
                    'op': 'set_manual_order', 'scope': scope,
                    'ordered_ids': current_order, 'source': 'workflowy',
                    'source_modified_at': modified,
                }), request_key)
                db.execute('INSERT INTO events(source,external_key,payload_json) VALUES(?,?,?)',
                           ('workflowy_manual_order', request_key,
                            json.dumps({'scope': scope, 'ordered_ids': current_order}, sort_keys=True)))
                mutations_submitted += 1
            state['pending_order'] = current_order
            state.pop('force_ai', None)
        pending_orders[scope] = state.get('pending_order') if isinstance(state.get('pending_order'), list) else None
        _mapping_set(db, scope_key, scope_ids[scope], state)

    def order_key(item: dict, scope: str) -> tuple:
        state = _mapping_get(db, SCOPE_PREFIX + scope)
        metadata = state[1] if state else {}
        pending = pending_orders.get(scope)
        if pending and item['work_item_id'] in pending:
            return (0, pending.index(item['work_item_id']))
        if metadata.get('force_ai'):
            return (1, item.get('sort_order') is None, item.get('sort_order') or 0,
                    str(item.get('created_at') or ''), item['work_item_id'])
        rank = _manual_rank(item)
        return (0 if rank is not None else 1, rank if rank is not None else 0,
                item.get('sort_order') is None, item.get('sort_order') or 0,
                str(item.get('created_at') or ''), item['work_item_id'])

    ordered_items = sorted(items, key=lambda item: order_key(item, _scope_for(item)))
    for item in ordered_items:
        node_id = links[item['work_item_id']]
        desired_parent = expected_parents[item['work_item_id']]
        if nodes[node_id].get('parent_id') != desired_parent:
            client.move_node(node_id, desired_parent, position='bottom')
            nodes[node_id]['parent_id'] = desired_parent
            counts['moved'] += 1

    sequences: dict[str, list[str]] = {
        scope_ids['inbox']: [reset_ids['inbox']],
        scope_ids['roadmap']: [reset_ids['roadmap'], *groups.values()],
    }
    for item in ordered_items:
        sequences.setdefault(expected_parents[item['work_item_id']], []).append(
            links[item['work_item_id']])
    for owner, sequence in sequences.items():
        current = [node_id for node_id in _children_in_order(nodes, owner) if node_id in set(sequence)]
        if current != sequence:
            for node_id in reversed(sequence):
                client.move_node(node_id, owner, position='top')
                counts['moved'] += 1

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
        canonical = {item['work_item_id']: links[item['work_item_id']]
                     for item in items if _scope_for(item) == scope}
        order = _vertical_ids(projected_nodes, scope_ids[scope], canonical)
        mapping = _mapping_get(db, SCOPE_PREFIX + scope)
        state = dict(mapping[1] if mapping else {})
        state.update({
            'projection_version': PROJECTION_VERSION,
            'last_render_order': order,
            'expected_parents': {item_id: expected_parents[item_id] for item_id in canonical},
            'last_render_fingerprint': _state_fingerprint(
                order, {item_id: expected_parents[item_id] for item_id in canonical}),
            'last_render_source_modified_at': _source_modified_at(projected_nodes, canonical),
        })
        # Canonical workflowy ranks acknowledge the pending local intent.
        if (state.get('pending_order') == order and canonical
                and all(_manual_rank(by_item[item_id]) is not None
                        and str(_manual_source(by_item[item_id]) or '').casefold() == 'workflowy'
                        for item_id in canonical)):
            state.pop('pending_order', None)
        if state.get('force_ai') and canonical and all(_manual_rank(by_item[item_id]) is None
                                                       for item_id in canonical):
            state.pop('force_ai', None)
        _mapping_set(db, SCOPE_PREFIX + scope, scope_ids[scope], state)
    put(ROADMAP_ROOT_KEY, parent, 'Codex', roadmap_projection_note(pending=False, source=root_source), 'h1', count_update=False)
    db.commit()
    return dict(prompts=len(items), **counts,
                mutations_submitted=mutations_submitted, warnings=warnings)
