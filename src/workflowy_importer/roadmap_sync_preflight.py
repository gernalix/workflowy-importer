"""Recover expired C2 authority before a Workflowy roadmap sync starts."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time


RESUME = Path('/home/daniele/projects/codex-roadmap/tools/c2_supervisor_resume.py')


def _error_category(stderr: str | None) -> str:
    """Keep a useful exception category without logging GitHub error details."""
    if not isinstance(stderr, str):
        return 'unknown'
    last = stderr.strip().splitlines()[-1:]
    if not last:
        return 'unknown'
    match = re.match(r'(?:[\w.]+\.)?([A-Za-z_]+Error): ([a-z_]+)', last[0])
    return ':'.join(match.groups()) if match else 'unknown'


def local_lease_matches(state: dict) -> bool:
    tools = str(RESUME.parent)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from c2_supervisor_lease import DEFAULT_DB, connect, snapshot

    with connect(DEFAULT_DB) as db:
        row = snapshot(db)
    return bool(row and row['state'] == 'active'
                and row['supervisor_id'] == state.get('supervisor_id')
                and row['fencing_token'] == state.get('fencing_token')
                and row['lease_expires_at'] > time.time())


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(RESUME)], capture_output=True, text=True,
        check=False,
    )
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError:
        print('c2_supervisor_resume_retry:' + _error_category(result.stderr),
              file=sys.stderr)
        return 1
    if not isinstance(state, dict):
        print('c2_supervisor_resume_retry:invalid_shape', file=sys.stderr)
        return 1
    outcome = state.get('outcome')
    if outcome == 'BLOCKED':
        print('c2_supervisor_resume_blocked:' + str(state.get('reason', outcome)),
              file=sys.stderr)
        return 255
    if result.returncode:
        print('c2_supervisor_resume_retry:' + _error_category(result.stderr),
              file=sys.stderr)
        return 1
    if (outcome == 'ALREADY_ACTIVE'
            and state.get('phase') != 'canonical_successor'
            and local_lease_matches(state)):
        return 0
    if (outcome in {'RESUMED', 'STALE_TAKEOVER'}
            and isinstance(state.get('claim'), dict)
            and state['claim'].get('submission') == 'applied'
            and local_lease_matches(state)):
        return 0
    if outcome in {'ALREADY_ACTIVE', 'RESUMED', 'STALE_TAKEOVER'}:
        # A new claim is asynchronous. The writer's DB update triggers the
        # path unit; the timer is a fallback. Keep the Workflowy drag visible
        # until a later sync can submit it with the newly claimed fence.
        print('c2_supervisor_claim_pending', file=sys.stderr)
        return 1
    print('c2_supervisor_resume_unexpected_outcome:' + str(outcome),
          file=sys.stderr)
    return 255


if __name__ == '__main__':
    raise SystemExit(main())
