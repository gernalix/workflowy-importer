"""Read-only C2 projection. Work item identity survives PROMPT_ID materialization."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import html
import re
import sqlite3

GROUPS = (
    ('running', 'IN CORSO'), ('ready', 'PRONTI'),
    ('waiting', 'IN ATTESA'), ('blocked', 'BLOCCATI / NEEDS FIX'),
    ('completed', 'COMPLETATI RECENTEMENTE'), ('archive', 'Archivio'),
)
DONE = {'completed', 'waived', 'cancelled', 'superseded'}


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
        ready &= configured_ids
        items = [dict(r) for r in conn.execute('''SELECT * FROM v_work_item_summary
            ORDER BY COALESCE(sort_order,2147483647),created_at,work_item_id''')]
        by_id = {r['work_item_id']: r for r in items}
        for item in items:
            key = item['work_item_id']
            item['execution_configured'] = key in configured_ids
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
                'ready' if key in ready else 'waiting')
            # Reject malformed trees before any remote change.
            seen = {key}
            parent = item['parent_id']
            while parent:
                if parent in seen or parent not in by_id:
                    raise ValueError('invalid_work_item_hierarchy')
                seen.add(parent)
                parent = by_id[parent]['parent_id']
        return items


def item_text(item: dict, links: dict[str, str]) -> tuple[str, str]:
    esc = lambda value: html.escape(str(value or ''), quote=True)
    icon = '✅' if item['status'] in DONE else '👉' if item['status'] == 'running' else '☐'
    identity = f"[{item['prompt_id']}] " if item['prompt_id'] else ''
    name = f"{icon} {identity}{esc(item['title'])}"
    lines = [f"✅ {item['completed_actionable']}/{item['total_actionable']} · {item['progress_percent']:g}%"]
    if item.get('objective'):
        lines.append('Cosa fa: ' + esc(item['objective']))
    if item.get('external_owner'):
        lines.append('Gestito da worker esterno PH — non assegnabile da questo supervisor.')
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
    elif item['group'] == 'waiting' and not item['blocker'] and not item.get('external_owner'):
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


def sync_items(client, db, items: list[dict], *, parent: str) -> dict:
    from .roadmap_bridge import _mapping_get, _mapping_set, _hydrate_mapped_nodes, GROUP_PREFIX, ROADMAP_ROOT_KEY
    nodes = {str(n['id']): n for n in client.export_nodes() if n.get('id')}
    keys = [ROADMAP_ROOT_KEY] + [GROUP_PREFIX + k for k, _ in GROUPS]
    keys += ['wi:' + i['work_item_id'] for i in items]
    keys += [i['prompt_id'] for i in items if i['prompt_id']]
    _hydrate_mapped_nodes(client, db, nodes, keys)
    counts = dict(created=0, updated=0, moved=0)

    def put(key, parent_id, name, note='', layout='bullets', legacy=None):
        mapped = _mapping_get(db, key) or (_mapping_get(db, legacy) if legacy else None)
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
                counts['updated'] += 1
            if node.get('parent_id') != parent_id:
                client.move_node(node_id, parent_id, position='bottom')
                node['parent_id'] = parent_id
                counts['moved'] += 1
        _mapping_set(db, key, node_id, {'authority': 'work_items'})
        db.commit()  # Preserve acknowledged remote identities across crashes.
        return node_id

    root = put(ROADMAP_ROOT_KEY, parent, 'Codex', 'Checklist 2.0 · work_items è la fonte canonica.', 'h1')
    groups = {k: put(GROUP_PREFIX+k, root, label, layout='h2') for k, label in GROUPS}
    links = {}
    pending = list(items)
    while pending:
        for item in pending[:]:
            if item['parent_id'] and item['parent_id'] not in links:
                continue
            name, note = item_text(item, links)
            links[item['work_item_id']] = put('wi:'+item['work_item_id'],
                links[item['parent_id']] if item['parent_id'] else groups[item['group']],
                name, note, legacy=item['prompt_id'])
            pending.remove(item)
    # All dependency targets now have stable links.
    for item in items:
        name, note = item_text(item, links)
        put('wi:'+item['work_item_id'], nodes[links[item['work_item_id']]]['parent_id'], name, note)
    # Retire the old top-level Integration/Unknown groups without deleting
    # user-owned children; every canonical prompt node above has already moved.
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
    # Apply order only if the remote sequence differs; retain manual nodes.
    desired = {root: list(groups.values())}
    for item in items:
        owner = links[item['parent_id']] if item['parent_id'] else groups[item['group']]
        desired.setdefault(owner, []).append(links[item['work_item_id']])
    for owner, sequence in desired.items():
        known = set(sequence)
        current = [str(n['id']) for n in nodes.values() if n.get('parent_id') == owner and str(n['id']) in known]
        if current != sequence:
            for node_id in reversed(sequence):
                client.move_node(node_id, owner, position='top')
                counts['moved'] += 1
    return dict(prompts=len(items), **counts, mutations_submitted=0, warnings=0)
