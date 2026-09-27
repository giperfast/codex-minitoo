"""Read-only Codex app-server client; credentials stay inside Codex."""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import time


def read_limits(timeout=12):
    codex = shutil.which('codex')
    if not codex:
        for app in ('ChatGPT', 'Codex'):
            resources = Path('/Applications') / (app + '.app/Contents/Resources')
            for relative in ('codex-cli/CodexCLI.app/Contents/MacOS/codex', 'codex'):
                candidate = resources / relative
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    codex = str(candidate)
                    break
            if codex:
                break
    if not codex:
        raise RuntimeError('Codex executable not found')
    process = subprocess.Popen([codex, '-c', 'features.hooks=false', 'app-server', '--stdio'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    pending = b''
    deadline = time.monotonic() + timeout
    def send(message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()
    def response(identifier):
        nonlocal pending
        while time.monotonic() < deadline:
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('id') == identifier:
                    if 'error' in message:
                        raise RuntimeError('Codex limits request failed')
                    return message['result']
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
        raise RuntimeError('Codex limits request timed out')
    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'minitoo_limits', 'version': '1.0'},
            'capabilities': {'experimentalApi': True}}})
        response(1)
        send({'method': 'initialized'})
        send({'id': 2, 'method': 'account/rateLimits/read'})
        return response(2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def normalize(result, now=None):
    now = time.time() if now is None else now
    buckets = result.get('rateLimitsByLimitId') or {}
    bucket = buckets.get('codex') or result.get('rateLimits') or {}
    windows = []
    for key, fallback in [('primary', '5H'), ('secondary', 'WEEK')]:
        window = bucket.get(key) or {}
        used = window.get('usedPercent')
        minutes = window.get('windowDurationMins')
        label = 'WEEK' if minutes == 10080 else '5H' if minutes == 300 else (str(minutes) + 'M' if minutes else fallback)
        windows.append({'label': label, 'remaining': None if used is None else max(0, min(100, 100 - used)),
                        'resets_at': window.get('resetsAt')})
    return {'fetched_at': now, 'windows': windows}


if __name__ == '__main__':
    print(json.dumps(normalize(read_limits()), indent=2))
