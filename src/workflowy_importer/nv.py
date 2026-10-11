"""Read-only project release display; stdout remains a machine channel."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]


def banner(args=None):
    args = sys.argv[1:] if args is None else args
    if not sys.stdout.isatty() or not sys.stderr.isatty() or '--json' in args:
        return
    try:
        raw=(ROOT/'NV.txt').read_bytes()
        if not re.fullmatch(rb'[1-9][0-9]*\n',raw) or (ROOT/'version.txt').exists():
            raise ValueError('inconsistent source')
        label='NV '+raw[:-1].decode('ascii')
    except (OSError,ValueError):
        label='NV unavailable'
    print(label,file=sys.stderr)
