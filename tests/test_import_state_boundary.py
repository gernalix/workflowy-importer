"""Synthetic importer transactions: source packages never authorize recovery."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from workflowy_importer import cli


class FakeClient:
    def __init__(self):
        self.existing = {'victim', 'existing-new'}
        self.events = []
        self.counter = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def node_exists(self, node):
        return node in self.existing

    def create_node(self, parent, name, *args, **kwargs):
        self.counter += 1
        node = f'created-{self.counter}'
        self.existing.add(node)
        self.events.append(('create', node))
        return node

    def delete_node(self, node):
        self.existing.remove(node)
        self.events.append(('delete', node))


class ImportStateBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        home = patch.object(Path, 'home', return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.source = self.root / 'notes'
        self.source.mkdir()
        (self.source / 'note.md').write_text('# Fixture\n', encoding='utf-8')
        self.client = FakeClient()

    def invoke(self, *extra, source=None):
        args = cli.build_parser().parse_args([str(source or self.source), *extra])
        with patch.object(cli, 'load_api_key', return_value='synthetic-key') as key, patch.object(
            cli, 'WorkflowyClient', return_value=self.client
        ) as factory, contextlib.redirect_stdout(io.StringIO()):
            result = cli.run(args)
        return result, key, factory

    def test_bundled_journals_fail_before_credentials_or_remote_client(self):
        states = [
            {'pending_build': {'root_id': 'victim'}},
            {'root_id': 'existing-new', 'pending_replace': {'old_root_id': 'victim'}},
            {'format_version': 1, 'root_id': 'victim', 'source': str(self.source)},
            ['not a journal'],
        ]
        for source in (self.source, self.source / 'note.md'):
            legacy = cli._legacy_state_path(source)
            for state in states:
                for extra in ([], ['--replace']):
                    with self.subTest(source=source.name, state=state, replace=bool(extra)):
                        raw = json.dumps(state)
                        legacy.write_text(raw)
                        with patch.object(cli, 'load_api_key') as key, patch.object(cli, 'WorkflowyClient') as factory:
                            with self.assertRaisesRegex(RuntimeError, 'Preserve and review'):
                                cli.run(cli.build_parser().parse_args([str(source), *extra]))
                            key.assert_not_called()
                            factory.assert_not_called()
                        self.assertEqual(legacy.read_text(), raw)
                        self.assertFalse(cli._default_state_path(source).parent.exists())
            legacy.unlink()
        self.assertFalse(self.client.events)

    def test_dangling_legacy_symlink_also_blocks_and_is_preserved(self):
        legacy = cli._legacy_state_path(self.source)
        legacy.symlink_to(self.root / 'absent')
        with self.assertRaisesRegex(RuntimeError, 'Legacy import journal'):
            self.invoke()
        self.assertTrue(legacy.is_symlink())
        self.assertFalse(cli._default_state_path(self.source).exists())

    def test_private_first_import_noop_change_guard_and_explicit_replacement(self):
        self.assertEqual(self.invoke()[0], 0)
        private = cli._default_state_path(self.source)
        first = cli._load_state(private)['root_id']
        self.assertFalse(private.is_relative_to(self.source))
        self.assertFalse(cli._legacy_state_path(self.source).exists())
        events = list(self.client.events)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.client.events, events)
        (self.source / 'note.md').write_text('# Changed\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '--replace'):
            self.invoke()
        self.assertEqual(self.client.events, events)
        self.assertEqual(self.invoke('--replace')[0], 0)
        self.assertEqual(self.client.events[-1], ('delete', first))
        self.assertTrue(all(event[0] == 'create' for event in self.client.events[len(events):-1]))
        self.assertNotEqual(cli._load_state(private)['root_id'], first)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(private.parent.parent.stat().st_mode), 0o700)

    def test_private_receipt_is_authoritative_over_bundled_sidecar(self):
        self.invoke()
        legacy = cli._legacy_state_path(self.source)
        raw = json.dumps({'pending_build': {'root_id': 'victim'}})
        legacy.write_text(raw)
        events = list(self.client.events)
        self.invoke()
        self.assertEqual(self.client.events, events)
        self.assertEqual(legacy.read_text(), raw)
        self.assertIn('victim', self.client.existing)

    def test_private_pending_recovery_and_explicit_legacy_selection_survive(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                private = cli._select_state_path(self.source, None)
                state_path = cli._legacy_state_path(self.source) if explicit else private
                cli._save_state(state_path, {'pending_build': {'root_id': 'existing-new'}})
                self.client.existing.add('existing-new')
                extra = ['--state-file', str(state_path)] if explicit else []
                self.invoke(*extra)
                self.assertIn(('delete', 'existing-new'), self.client.events)
                self.assertIn('victim', self.client.existing)
                before = list(self.client.events)
                self.invoke(*extra)
                self.assertEqual(self.client.events, before)

    def test_corrupt_private_state_never_falls_back_to_source(self):
        private = cli._select_state_path(self.source, None)
        legacy = cli._legacy_state_path(self.source)
        raw = json.dumps({'pending_build': {'root_id': 'victim'}})
        legacy.write_text(raw)
        for content in ('{invalid', '[]'):
            private.write_text(content)
            private.chmod(0o600)
            with patch.object(cli, 'load_api_key') as key, patch.object(cli, 'WorkflowyClient') as factory:
                with self.assertRaises(RuntimeError):
                    cli.run(cli.build_parser().parse_args([str(self.source)]))
                key.assert_not_called()
                factory.assert_not_called()
            self.assertEqual(legacy.read_text(), raw)

    def test_dry_run_ignores_legacy_without_state_or_credential_writes(self):
        legacy = cli._legacy_state_path(self.source)
        legacy.write_text('{invalid')
        result, key, factory = self.invoke('--dry-run')
        self.assertEqual(result, 0)
        key.assert_not_called()
        factory.assert_not_called()
        self.assertFalse(cli._default_state_path(self.source).parent.exists())
        self.assertEqual(legacy.read_text(), '{invalid')

    def test_state_identity_uses_canonical_source_path_not_contents(self):
        first = cli._default_state_path(self.source)
        self.assertEqual(first, cli._default_state_path(self.source / '..' / self.source.name))
        (self.source / 'note.md').write_text('changed')
        self.assertEqual(first, cli._default_state_path(self.source))
        other = self.root / 'other'
        other.mkdir()
        self.assertNotEqual(cli._default_state_path(self.source / 'note.md'), cli._default_state_path(other / 'note.md'))

    def test_default_private_symlink_and_exposed_file_are_rejected(self):
        private = cli._select_state_path(self.source, None)
        outside = self.root / 'untrusted.json'
        outside.write_text(json.dumps({'pending_build': {'root_id': 'victim'}}))
        private.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'private regular file'):
            self.invoke()
        self.assertTrue(private.is_symlink())
        private.unlink()
        private.write_text(outside.read_text())
        private.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'private regular file'):
            self.invoke()
        self.assertFalse(self.client.events)

    def test_default_directory_permissions_are_not_silently_changed(self):
        private = cli._select_state_path(self.source, None)
        private.parent.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'directory must be private'):
            self.invoke()
        self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o755)
        self.assertFalse(self.client.events)


if __name__ == '__main__':
    unittest.main()
