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
        self.questions = {}

    def consume(self, thread, record):
        payload = record.get('payload') or {}
        kind = payload.get('type')
        try:
            stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
        except (ValueError, KeyError):
            return
        turn = payload.get('turn_id')
        previous = self.sessions.get(thread, {})
        if record.get('type') == 'response_item':
            pending = self.questions.setdefault(thread, {})
            call = payload.get('call_id')
            name = payload.get('name', '').split('.')[-1]
            if kind == 'function_call' and name in ('request_user_input', 'request_user_input_async') and call:
                pending[call] = name
                self.sessions[thread] = {'state': 'waiting', 'turn': previous.get('turn'), 'time': stamp}
            elif kind in ('function_call_output', 'verified_answer') and call in pending:
                if pending[call] == 'request_user_input' or kind == 'verified_answer':
                    pending.pop(call)
                    previous.update(state='waiting' if pending else 'working', time=stamp)
            elif kind == 'message' and payload.get('role') == 'user' and pending:
                # Async replies are user messages; ordinary follow-ups also
                # supersede the previous question. Never retain question text.
                pending.clear()
                previous.update(state='working', time=stamp)
            return
        if record.get('type') != 'event_msg':
            return
        if kind == 'task_started':
            self.questions.pop(thread, None)
            self.sessions[thread] = {'state': 'working', 'turn': turn, 'time': stamp}
        elif kind in ('task_complete', 'task_completed', 'turn_aborted', 'task_aborted'):
            if previous.get('turn') and turn and previous['turn'] != turn:
                return
            completed = kind.startswith('task_complet')
            pending = self.questions.get(thread, {})
            if not completed:
                self.questions.pop(thread, None)
            state = 'waiting' if completed and pending else 'done' if completed else 'idle'
            self.sessions[thread] = {'state': state,
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
    next_upload = 0
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
            # Keep consuming events during the firmware cooldown. Once ready,
            # send the current aggregate state, not a queue of old transitions.
            if (state != sent or now - sent_at >= 60) and retry.ready(time.monotonic()) and time.monotonic() >= next_upload:
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
                next_upload = time.monotonic() + minitoo.IMAGE_UPLOAD_GAP
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
