import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from workflowy_importer import nv

class NVTests(unittest.TestCase):
    def test_interactive_banner_preserves_stdout_and_redirects(self):
        with tempfile.TemporaryDirectory() as td,patch.object(nv,'ROOT',Path(td)):
            (Path(td)/'NV.txt').write_bytes(b'1\n')
            out=io.StringIO();err=io.StringIO()
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):nv.banner([])
            self.assertEqual((out.getvalue(),err.getvalue()),('',''))
            out.isatty=lambda:True;err.isatty=lambda:True
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                nv.banner(['--json']);self.assertEqual(err.getvalue(),'')
                nv.banner([])
            self.assertEqual(out.getvalue(),'');self.assertEqual(err.getvalue(),'NV 1\n')
            for raw in (b'0\n',b'01\n',b'1',b'1\r\n'):
                (Path(td)/'NV.txt').write_bytes(raw)
                err.seek(0);err.truncate()
                with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):nv.banner([])
                self.assertEqual(err.getvalue(),'NV unavailable\n')
