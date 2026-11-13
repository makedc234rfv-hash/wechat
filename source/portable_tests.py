"""Offline tests; synthetic data only. Never call Weixin or enumerate real accounts."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import wechat_tool as tool
from export_archive import display_text, page, safe_name, text_decode
from period_report import build, period
from snapshot import wal_frames
from compression import zstd


class PortableTests(unittest.TestCase):
    def test_period(self):
        self.assertEqual(period('week', '2026-10-27'), ('2026-10-26', '2026-11-01'))
        self.assertEqual(period('month', '2026-02-10'), ('2026-02-01', '2026-02-28'))
        with self.assertRaises(ValueError):
            period('custom', '2026-10-27', '2026-10-28', '2026-10-27')

    def test_compressed_text(self):
        data = '合成聊天，不含真实数据'.encode()
        self.assertEqual(text_decode(zstd.compress(data)), data.decode())

    def test_html_escape(self):
        self.assertIn('&lt;script&gt;', page('<script>', 'synthetic'))
        self.assertIn("default-src 'none'", page('test', 'synthetic'))
        self.assertNotIn('/', safe_name('../synthetic'))

    def test_report(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'manifest.json').write_text('{}')
            messages = root / 'synthetic' / '2026-10-27'
            messages.mkdir(parents=True)
            record = {'timestamp': '2026-10-27T10:00:00+08:00', 'conversation_name': '测试会话',
                      'message_type': 'text', 'direction': 'sent'}
            (messages / 'messages.jsonl').write_text(json.dumps(record), encoding='utf-8')
            result, summary = build(root, 'day', '2026-10-27', '2026-10-27')
            self.assertTrue(result.is_file())
            self.assertEqual(summary['message_count'], 1)

    def test_wal(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'synthetic.db'
            connection = sqlite3.connect(source)
            try:
                connection.execute('PRAGMA page_size=4096')
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA wal_autocheckpoint=0')
                connection.execute('CREATE TABLE records(body TEXT)')
                connection.commit()
                connection.execute('INSERT INTO records VALUES (?)', ('synthetic',))
                connection.commit()
                raw = Path(str(source) + '-wal').read_bytes()
                frames, info = wal_frames(raw)
                self.assertGreater(info['committed_frames'], 0)
                replay = bytearray(source.read_bytes())
                for number, data in frames:
                    if len(replay) < number * 4096:
                        replay.extend(bytes(number * 4096 - len(replay)))
                    replay[(number - 1) * 4096:number * 4096] = data
                target = Path(folder) / 'replayed.db'
                target.write_bytes(replay[:info['commit_pages'] * 4096])
                with contextlib.closing(sqlite3.connect(target.resolve().as_uri() + '?immutable=1', uri=True)) as checked:
                    self.assertEqual(checked.execute('SELECT body FROM records').fetchone()[0], 'synthetic')
                self.assertLess(len(wal_frames(raw[:-1])[0]), len(frames))
                bad = bytearray(raw)
                bad[-5] ^= 1
                self.assertEqual(wal_frames(bad)[1]['tail'], 'invalid_checksum_tail')
            finally:
                connection.close()

    def test_multiple_accounts_requires_choice(self):
        with patch.object(tool, 'accounts', return_value=[Path('one'), Path('two')]):
            with self.assertRaises(ValueError):
                tool.choose_account()

    def test_output_protection(self):
        with TemporaryDirectory() as folder, patch.object(tool, 'discover_weixin') as discovery:
            account = Path(folder) / 'account'
            account.mkdir()
            for out in (account, account / 'export', account.parent):
                with self.assertRaises(ValueError):
                    tool.export_local(account, out, 'day', '2026-10-27', '2026-10-27')
            existing = Path(folder) / 'existing'
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                tool.export_local(account, existing, 'day', '2026-10-27', '2026-10-27')
            discovery.assert_not_called()

    def test_no_implicit_export(self):
        with patch.object(tool, 'choose_account') as choose, contextlib.redirect_stderr(io.StringIO()):
            for argv in ([], ['--output', 'unused'], ['--start', '2026-10-27', '--output', 'unused']):
                with self.assertRaises(SystemExit) as caught:
                    tool.main(argv)
                self.assertEqual(caught.exception.code, 2)
            choose.assert_not_called()

    def _mock_run(self, failure=False, unknown=False):
        with TemporaryDirectory() as folder:
            account = Path(folder) / 'fake-account'
            account.mkdir()
            out = Path(folder) / 'new-output'
            def fake_probe(args):
                scratch = Path(args.snapshot_dir)
                scratch.mkdir(parents=True)
                (scratch / 'synthetic.txt').write_text('temporary synthetic data')
                return 0
            def fake_export(*args):
                if failure:
                    raise RuntimeError('synthetic export failure')
                Path(args[2]).mkdir()
                return {'messages': 1, 'conversations': 1}
            with patch.object(tool, 'discover_weixin', return_value=([{'Id': 123}], Path('Weixin.exe'), Path('Weixin.dll'))), \
                    patch.object(tool, 'probe_run', side_effect=fake_probe), \
                    patch.object(tool, 'export', side_effect=fake_export) as exporter, \
                    patch.object(tool, 'list_conversations', return_value=[('synthetic', '联系人', 'synthetic-id')]), \
                    patch.object(tool, 'build', return_value=(out / 'synthetic.html', {})):
                if failure or unknown:
                    with self.assertRaises((RuntimeError, ValueError)):
                        tool.export_local(account, out, 'day', '2026-10-27', '2026-10-27',
                                          ['unknown'] if unknown else None)
                    if unknown:
                        exporter.assert_not_called()
                else:
                    result = tool.export_local(account, out, 'day', '2026-10-27', '2026-10-27')
                    self.assertEqual(result['messages'], 1)
                self.assertFalse((out / '_temporary_snapshot').exists())

    def test_success_cleanup(self):
        self._mock_run()

    def test_failure_cleanup(self):
        self._mock_run(failure=True)

    def test_unknown_conversation_fails_closed(self):
        self._mock_run(unknown=True)


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PortableTests)
    return unittest.TextTestRunner(verbosity=2).run(suite)


if __name__ == '__main__':
    raise SystemExit(0 if run().wasSuccessful() else 1)
