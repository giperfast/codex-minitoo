"""Read local Codex lifecycle events and update MiniToo (no model calls)."""
from datetime import datetime
from contextlib import closing
import fcntl
import json
from pathlib import Path
import sqlite3
import time
import minitoo


class RetryBackoff:
    """Keep reading events while failed display updates cool down."""
    def __init__(self):
        self.delay = 0
        self.next_attempt = 0

    def ready(self, now):
        return now >= self.next_attempt

    def failed(self, now):
        self.delay = min(self.delay * 2, 300) if self.delay else 30
        self.next_attempt = now + self.delay
        return self.delay

    def succeeded(self):
        self.delay = 0
        self.next_attempt = 0


class Tracker:
    def __init__(self):
        self.sessions = {}

    def consume(self, thread, record):
        if record.get('type') != 'event_msg':
            return
        payload = record.get('payload') or {}
        kind = payload.get('type')
        try:
            stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
        except (ValueError, KeyError):
            return
        turn = payload.get('turn_id')
        previous = self.sessions.get(thread, {})
        if kind == 'task_started':
            self.sessions[thread] = {'state': 'working', 'turn': turn, 'time': stamp}
        elif kind in ('task_complete', 'task_completed', 'turn_aborted', 'task_aborted'):
            if previous.get('turn') and turn and previous['turn'] != turn:
                return
            self.sessions[thread] = {'state': 'done' if kind.startswith('task_complet') else 'idle',
                                     'turn': turn, 'time': stamp}
        elif kind in ('permission_request', 'approval_requested'):
            self.sessions[thread] = {'state': 'waiting', 'turn': turn or previous.get('turn'), 'time': stamp}
        elif kind in ('approval_resolved', 'permission_resolved'):
            self.sessions[thread] = {'state': 'working', 'turn': turn or previous.get('turn'), 'time': stamp}
        elif previous.get('state') in ('working', 'waiting'):
            # Heartbeat from actual activity, without inspecting task contents.
            previous['time'] = stamp

    def selected(self, now):
        states = [v['state'] for v in self.sessions.values() if now - v['time'] < 3600]
        if 'waiting' in states: return 'waiting'
        if 'working' in states: return 'working'
        if any(v['state'] == 'done' and now - v['time'] < 60 for v in self.sessions.values()): return 'done'
        return 'idle'


def recent_threads():
    home = Path.home() / '.codex'
    databases = sorted(home.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not databases:
        return []
    # SQLite's connection context manager commits/rolls back but does not close
    # the connection. Close on every scan to avoid exhausting process FDs.
    with closing(sqlite3.connect(f'file:{databases[0]}?mode=ro', uri=True, timeout=2)) as db:
        return db.execute("SELECT id, rollout_path FROM threads WHERE archived=0 AND source NOT LIKE '%subAgent%' ORDER BY updated_at DESC LIMIT 100").fetchall()


def read_added(path, offset):
    with Path(path).open('rb') as stream:
        size = stream.seek(0, 2)
        initial = offset is None
        if offset is None:
            offset = max(0, size - 2 * 1024 * 1024)
        if offset > size:
            offset = 0
        stream.seek(offset)
        if initial and offset:
            # Initial tail can begin inside a JSON record.
            stream.readline()
            offset = stream.tell()
        data = stream.read()
    end = data.rfind(b'\n')
    if end < 0:
        return [], offset
    records = []
    for line in data[:end].splitlines():
        try: records.append(json.loads(line))
        except ValueError: pass
    return records, offset + end + 1


def main():
    minitoo.RUN.mkdir(exist_ok=True)
    singleton = open(minitoo.RUN / 'watcher.lock', 'a')
    try: fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: return
    tracker = Tracker()
    offsets = {}
    paths = []
    next_scan = 0
    sent = None
    sent_at = 0
    retry = RetryBackoff()
    while True:
        try:
            now = time.time()
            if now >= next_scan:
                paths = recent_threads()
                next_scan = now + 5
            for thread, path in paths:
                try:
                    records, offset = read_added(path, offsets.get(path))
                    offsets[path] = offset
                    for record in records: tracker.consume(thread, record)
                except FileNotFoundError: continue
            state = tracker.selected(now)
            if (state != sent or now - sent_at >= 60) and retry.ready(time.monotonic()):
                try:
                    with open(minitoo.RUN / 'state.lock', 'a') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        minitoo.display(state)
                except Exception as error:
                    delay = retry.failed(time.monotonic())
                    print(time.strftime('%H:%M:%S'), type(error).__name__, str(error),
                          f'повтор через {delay} с', flush=True)
                    continue
                retry.succeeded()
                sent, sent_at = state, time.time()
                diagnostic = {'state': state, 'updated_at': sent_at,
                              'source': 'codex_rollout', 'threads': tracker.sessions}
                (minitoo.RUN / 'watcher-state.json').write_text(json.dumps(diagnostic))
                print(time.strftime('%H:%M:%S'), state, flush=True)
        except Exception as error:
            print(time.strftime('%H:%M:%S'), type(error).__name__, str(error), flush=True)
            time.sleep(5)
        time.sleep(1)


if __name__ == '__main__': main()
