from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from watch_codex import Tracker, read_added


class WatcherTests(unittest.TestCase):
    def test_repeated_database_scans_do_not_exhaust_file_descriptors(self):
        code = '''
import gc, resource, sqlite3, tempfile
from pathlib import Path
from unittest.mock import patch
from watch_codex import recent_threads
gc.disable()
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, soft), hard))
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp)
    (home / '.codex').mkdir()
    db = sqlite3.connect(home / '.codex/state_5.sqlite')
    db.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT, archived INTEGER, source TEXT, updated_at INTEGER)')
    db.execute("INSERT INTO threads VALUES ('test', '/tmp/test.jsonl', 0, 'appServer', 1)")
    db.commit()
    db.close()
    with patch('watch_codex.Path.home', return_value=home):
        for _ in range(300):
            assert recent_threads() == [('test', '/tmp/test.jsonl')]
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def event(self, kind, now, turn='turn1'):
        return {'type': 'event_msg', 'timestamp': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                'payload': {'type': kind, 'turn_id': turn}}

    def test_real_start_complete_and_idle(self):
        tracker = Tracker()
        tracker.consume('thread1', self.event('task_started', 100))
        self.assertEqual(tracker.selected(101), 'working')
        tracker.consume('thread1', self.event('task_complete', 110))
        self.assertEqual(tracker.selected(111), 'done')
        self.assertEqual(tracker.selected(171), 'idle')

    def test_old_turn_completion_cannot_stop_new_turn(self):
        tracker = Tracker()
        tracker.consume('thread1', self.event('task_started', 100, 'new'))
        tracker.consume('thread1', self.event('task_complete', 101, 'old'))
        self.assertEqual(tracker.selected(102), 'working')

    def test_other_thread_completion_preserves_working(self):
        tracker = Tracker()
        tracker.consume('a', self.event('task_started', 100))
        tracker.consume('b', self.event('task_complete', 101))
        self.assertEqual(tracker.selected(102), 'working')

    def test_partial_record_waits_for_newline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'rollout.jsonl'
            record = json.dumps(self.event('task_started', 100)).encode()
            path.write_bytes(record[:20])
            records, offset = read_added(path, None)
            self.assertEqual(records, [])
            path.write_bytes(record + b'\n')
            records, offset = read_added(path, offset)
            self.assertEqual(len(records), 1)
            self.assertEqual(read_added(path, offset)[0], [])
