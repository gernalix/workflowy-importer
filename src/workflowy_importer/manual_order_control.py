"""Submit one Workflowy ordering mutation through the fenced C2 control path.

Semantic/no-change deduplication belongs to the Workflowy projection cache.
Every invocation that reaches this transport gets a fresh request key so
renewed supervisor authority cannot collide with an older mutation payload.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid


C2_TOOLS = Path('/home/daniele/projects/codex-roadmap/tools')


def transport_request_key() -> str:
    return 'c2-workflowy-order-' + uuid.uuid4().hex


def _canonical_modules():
    tools = str(C2_TOOLS)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import c2_control
    import c2_workflowy_order
    return c2_control, c2_workflowy_order


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    setter = sub.add_parser('set')
    setter.add_argument('--scope', choices=('inbox', 'roadmap'), required=True)
    setter.add_argument('--source-modified-at', required=True)
    setter.add_argument('ids', nargs='*')
    clearer = sub.add_parser('clear')
    clearer.add_argument('--scope', choices=('inbox', 'roadmap'), required=True)
    clearer.add_argument('ids', nargs='*')
    args = parser.parse_args(argv)
    if len(set(args.ids)) != len(args.ids):
        raise ValueError('duplicate_entity_id')

    c2_control, c2_workflowy_order = _canonical_modules()
    supervisor_id, fencing_token = c2_workflowy_order.load_runtime_identity()
    if args.action == 'set':
        operation = 'set_manual_order'
        arguments = {
            'scope': args.scope,
            'ordered_ids': args.ids,
            'source': 'workflowy',
            'source_modified_at': args.source_modified_at,
        }
    else:
        operation = 'clear_manual_order'
        arguments = {'scope': args.scope}
        if args.ids:
            arguments['ids'] = sorted(args.ids)
    result = c2_control.submit_control(
        operation=operation,
        arguments=arguments,
        request_key=transport_request_key(),
        supervisor_id=supervisor_id,
        fencing_token=fencing_token,
        actor='c2-workflowy-order',
    )
    print(json.dumps({'status': 'queued', **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
