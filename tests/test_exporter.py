from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.export_typecho import COLUMNS, main
from migrator.source import load_jsonl
from migrator.validate import check_existing_ids
from migrator.common import MigrationError
from tests.helpers import comment, content


class FakeClient:
    def __init__(self, row, code=0):
        self.stdout = io.StringIO(json.dumps(row) + '\n')
        self.returncode = code
    def terminate(self):
        self.returncode = -15
    def wait(self):
        return self.returncode


class ExportTests(unittest.TestCase):
    def test_local_client_export_roundtrip_without_printing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'source.jsonl'
            rows = [content(), comment()]
            clients = [FakeClient({c: r.get(c) for c in COLUMNS[t]})
                       for t, r in zip(COLUMNS, rows)]
            argv = ['export_typecho.py', '--defaults-file', str(Path(temp) / 'local.cnf'),
                    '--database', 'migration_sandbox', '--output', str(path)]
            captured = io.StringIO()
            with patch('sys.argv', argv), patch('subprocess.Popen', side_effect=clients) as spawn, contextlib.redirect_stdout(captured):
                self.assertEqual(main(), 0)
            self.assertEqual(len(load_jsonl(path).comments), 1)
            self.assertNotIn('synthetic@example', captured.getvalue().lower())
            command = spawn.call_args_list[0].args[0]
            self.assertIn('--host=127.0.0.1', command)
            self.assertEqual(command[1].split('=')[0], '--defaults-extra-file')
            self.assertFalse(any(arg.startswith('--password') for arg in command))

    def test_failed_client_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'source.jsonl'
            path.write_text('previous')
            argv = ['export_typecho.py', '--defaults-file', str(Path(temp) / 'local.cnf'),
                    '--database', 'sandbox', '--output', str(path)]
            row = {c: content().get(c) for c in COLUMNS['contents']}
            with patch('sys.argv', argv), patch('subprocess.Popen', return_value=FakeClient(row, 1)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 2)
            self.assertEqual(path.read_text(), 'previous')
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_existing_ids_collision_and_objectid_type_distinction(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ids.jsonl'
            path.write_text('{"_id":"' + 'a'*32 + '"}\n')
            with self.assertRaises(MigrationError): check_existing_ids([{'_id': 'a'*32}], path)
            path.write_text('{"_id":{"$oid":"' + 'a'*24 + '"}}\n')
            self.assertEqual(check_existing_ids([{'_id': 'a'*32}], path), 1)
