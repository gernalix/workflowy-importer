"""Submit Workflowy ordering through the fenced C2 control path.

Reset request identity includes the consumed Workflowy control generation. A
retry of one control is idempotent, while a later recreated control can clear a
newer manual order even when its entity set is unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


C2_TOOLS = Path('/home/daniele/projects/codex-roadmap/tools')


def request_key(
    action: str,
    scope: str,
    ids: list[str],
    *,
    source_modified_at: str | None,
    request_token: str | None,
) -> str:
    payload = {
        'action': action,
        'scope': scope,
        'ids': ids if action == 'set' else sorted(ids),
        'source_modified_at': source_modified_at if action == 'set' else None,
        'request_token': request_token if action == 'clear' else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return 'c2-workflowy-order-v2-' + hashlib.sha256(encoded).hexdigest()[:32]


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
    setter.add_argument('--request-token')
    setter.add_argument('ids', nargs='*')
    clearer = sub.add_parser('clear')
    clearer.add_argument('--scope', choices=('inbox', 'roadmap'), required=True)
    clearer.add_argument('--request-token', required=True)
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
    key = request_key(
        args.action,
        args.scope,
        args.ids,
        source_modified_at=getattr(args, 'source_modified_at', None),
        request_token=args.request_token,
    )
    try:
        result = c2_control.submit_control(
            operation=operation,
            arguments=arguments,
            request_key=key,
            supervisor_id=supervisor_id,
            fencing_token=fencing_token,
            actor='c2-workflowy-order',
        )
    except Exception as exc:
        if 'request_key_conflict:' not in str(exc):
            raise
        result = {'status': 'ok', 'idempotent': True, 'outcome': 'already_applied'}
    print(json.dumps({'status': 'queued', **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
