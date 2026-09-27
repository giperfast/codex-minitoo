#!/usr/bin/env python3
"""Local Codex hooks -> MiniToo Bluetooth. No account, API key or network."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import struct
import tempfile
from usage_limits import read_limits, normalize

ROOT = Path(__file__).resolve().parent
RUN = ROOT / 'runtime'
CONFIG = ROOT / 'config.json'
EVENTS = {'UserPromptSubmit': 'working', 'PreToolUse': 'working',
          'PermissionRequest': 'waiting', 'Stop': 'done',
          'Interrupt': 'idle', 'SessionEnd': 'end'}
MESSAGES = {'working': (0x14, 'CODEX WORKING'), 'waiting': (0x12, 'CODEX NEEDS YOU'),
            'done': (0x12, 'CODEX DONE'), 'idle': (0x14, 'CODEX IDLE')}

def notification(text, icon=0x14):
    # Cut on a UTF-8 boundary, firmware limit is bytes rather than characters.
    data = text.encode('utf-8')[:128].decode('utf-8', errors='ignore').encode('utf-8')
    return 'raw ' + bytes([0x50, icon, len(data)]).__add__(data).hex(' ')

def config():
    if not CONFIG.exists():
        raise RuntimeError('MiniToo не настроен. Выполните: python3 minitoo.py setup')
    return json.loads(CONFIG.read_text())

def fifo_write(command, timeout=2):
    fifo = RUN / 'commands.fifo'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not stat.S_ISFIFO(fifo.stat().st_mode):
                raise RuntimeError('Ожидался Bluetooth FIFO')
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, (command + '\n').encode())
            finally:
                os.close(fd)
            return
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENXIO, errno.EAGAIN):
                raise
            time.sleep(.05)
    raise RuntimeError('Bluetooth helper недоступен; запустите python3 minitoo.py start')

def start():
    cfg = config()
    app = RUN / 'MiniToo.app'
    if not app.exists():
        raise RuntimeError('Сначала выполните bash build.sh')
    if (RUN / 'commands.fifo').exists():
        # Do not create a second daemon which would unlink the first FIFO.
        try:
            fifo_write('raw bd 27')
            return
        except RuntimeError:
            (RUN / 'commands.fifo').unlink(missing_ok=True)
    env = os.environ.copy()
    env.update(DIVOOM_FIFO=str(RUN / 'commands.fifo'), DIVOOM_LOG=str(RUN / 'bluetooth.log'))
    if cfg.get('rfcomm_port') is not None:
        env['DIVOOM_PORT'] = str(int(cfg['rfcomm_port']))
    # Launch as a macOS app, retaining the bundle's Bluetooth identity after
    # relocation. Explicit --env values carry the project's new FIFO/log paths.
    arguments = ['open', '-g', '-n']
    for key, value in env.items():
        if key.startswith('DIVOOM_'):
            arguments.extend(['--env', key + '=' + value])
    arguments.extend([str(app), '--args', cfg['mac'], 'daemon'])
    subprocess.run(arguments, check=True, timeout=10, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (RUN / 'commands.fifo').exists():
            return
        time.sleep(.1)
    raise RuntimeError('Не удалось подключиться. См. runtime/bluetooth.log и разрешение Bluetooth в macOS')

def display(state):
    cfg = config()
    if not (RUN / 'commands.fifo').exists():
        start()
    # Optional persistent faces: IDs must belong to this device, no guessed IDs.
    clock = cfg.get('clock_ids', {}).get(state)
    if clock is not None:
        device = cfg.get('device_id')
        if not device:
            raise RuntimeError('Для clock_ids требуется device_id')
        selector = dict(Command='Channel/SetClockSelectId', ClockId=int(clock),
                        DeviceId=int(device), ParentClockId=0, ParentItemId='',
                        PageIndex=0, LcdIndependence=0, LcdIndex=0, Language='en')
        fifo_write('json ' + json.dumps(selector, separators=(',', ':')))
    elif cfg.get('display_mode') == 'notification':
        icon, message = MESSAGES[state]
        fifo_write(notification(message, icon))
    else:
        refresh_limits()
        send_screen(state)

def refresh_limits(force=False):
    path = RUN / 'limits.json'
    cached = json.loads(path.read_text()) if path.exists() else None
    if force or cached is None or time.time() - cached.get('fetched_at', 0) >= 60:
        try:
            cached = normalize(read_limits(timeout=8))
            path.write_text(json.dumps(cached))
        except Exception:
            # Retain the previous snapshot with its original timestamp. Renderer
            # marks it OLD after five minutes; missing values remain unknown.
            if cached is None:
                cached = normalize({})
                cached['fetched_at'] = 0
                path.write_text(json.dumps(cached))
    renderer = RUN / 'render-status'
    subprocess.run([str(renderer), str(RUN / 'screens'), str(path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    return cached

def image_blob(jpeg):
    return bytes([0x23, 1, 0x03, 0xe8, 8, 10, 1]) + struct.pack('>I', len(jpeg)) + jpeg

def send_screen(state):
    # Missing image acknowledgements do not prove the RFCOMM channel closed.
    # Let the watcher retry with backoff without interrupting this connection.
    # The helper exits and removes its FIFO when the channel actually closes;
    # display() will then start a new helper on the next attempt.
    upload_screen(state)


def upload_screen(state):
    image = RUN / 'screens' / (state + '.jpg')
    if not image.exists():
        raise RuntimeError('Нет изображения состояния: выполните bash build.sh')
    blob = image_blob(image.read_bytes())
    size = struct.pack('<I', len(blob))
    # The Swift helper tokenizes on spaces. Keep queued rawfiles in a private
    # directory without spaces, and retain them until consumed by the daemon.
    cache = RUN / 'screen-cache.json'
    folder = Path(json.loads(cache.read_text())['folder']) if cache.exists() else None
    if folder is None or not folder.is_dir():
        folder = Path(tempfile.mkdtemp(prefix='codex-minitoo-', dir='/private/tmp'))
        cache.write_text(json.dumps({'folder': str(folder)}))
    rawfile = folder / (state + '.raw')
    lines = []
    for index, offset in enumerate(range(0, len(blob), 256)):
        command = bytes([0x8b, 1]) + size + struct.pack('<H', index) + blob[offset:offset + 256]
        lines.append(command.hex(' '))
    rawfile.write_text('\n'.join(lines) + '\n')
    # Wait for the announce acknowledgement in the helper log, not a blind
    # streaming delay. Device replies: 01 .. 04 8b 55 00 ... 02.
    log = RUN / 'bluetooth.log'
    offset = 0
    if log.exists():
        offset = log.stat().st_size
    fifo_write('raw ' + (bytes([0x8b, 0]) + size).hex(' '))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log.exists():
            with log.open() as stream:
                stream.seek(offset)
                if '04 8b 55 00' in stream.read():
                    fifo_write(f'rawfile {rawfile} 40')
                    # Hold session lock until upload completes to avoid
                    # interleaving another state's announce and chunks.
                    time.sleep(len(lines) * .04 + 1)
                    return
        time.sleep(.05)
    raise TimeoutError('MiniToo не подтвердил начало загрузки изображения (0x8B)')

def update(event, now=None):
    state = EVENTS.get(event.get('hook_event_name'))
    if state is None:
        return
    now = time.time() if now is None else now
    key = event.get('session_id') or event.get('thread_id') or 'manual'
    RUN.mkdir(parents=True, exist_ok=True)
    with open(RUN / 'state.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = RUN / 'state.json'
        saved = json.loads(path.read_text()) if path.exists() else {}
        sessions = {k: v for k, v in saved.get('sessions', {}).items() if now - v['time'] < 3600}
        if state == 'end':
            sessions.pop(key, None)
        else:
            sessions[key] = {'state': state, 'time': now}
        states = [v['state'] for v in sessions.values()]
        selected = ('waiting' if 'waiting' in states else 'working' if 'working' in states
                    else 'done' if 'done' in states else 'idle')
        sent = saved.get('sent')
        sent_at = saved.get('sent_at', now)
        if selected != sent or now - sent_at >= 60:
            try:
                display(selected)
                sent = selected
                sent_at = now
            except Exception as error:
                with open(RUN / 'errors.log', 'a') as log:
                    log.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {error}\n')
        path.write_text(json.dumps({'sessions': sessions, 'sent': sent, 'sent_at': sent_at}))

def setup(mac=None):
    if mac is None:
        raw = subprocess.check_output(['system_profiler', 'SPBluetoothDataType', '-json'], text=True)
        found = []
        def walk(value):
            if isinstance(value, dict):
                for name, item in value.items():
                    if re.search(r'mini[\s_-]*too', name, re.I) and isinstance(item, dict):
                        address = item.get('device_address')
                        if address:
                            found.append(address)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(json.loads(raw))
        found = list(set(found))
        if len(found) != 1:
            raise RuntimeError('Сопрягите MiniToo в Настройки системы → Bluetooth, затем повторите setup; либо setup --mac AA:BB:CC:DD:EE:FF')
        mac = found[0]
    mac = mac.replace('-', ':').upper()
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', mac):
        raise RuntimeError('Неверный Bluetooth MAC')
    cfg = config() if CONFIG.exists() else {}
    cfg['mac'] = mac
    CONFIG.write_text(json.dumps(cfg, indent=2) + '\n')
    print('Сохранён MiniToo: ' + mac)

def hooks_document():
    command = shlex.join([sys.executable, str(ROOT / 'minitoo.py'), 'hook'])
    return {'hooks': {event: [{'hooks': [{'type': 'command', 'command': command,
                                          'async': True,
                                          'timeout': 3 if event in ('Interrupt', 'SessionEnd') else 20}]}]
                      for event in EVENTS}}

def install_hooks(target):
    target = Path(target).expanduser()
    existing = json.loads(target.read_text()) if target.exists() else {}
    hooks = existing.setdefault('hooks', {})
    for event, groups in hooks_document()['hooks'].items():
        current = hooks.setdefault(event, [])
        for group in groups:
            if group not in current:
                current.append(group)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + '.backup-' + str(time.time_ns()))
        backup.write_bytes(target.read_bytes())
    target.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + '\n')
    print(f'Добавлены hooks: {target}. В Codex требуется review/trust через /hooks.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('setup').add_argument('--mac')
    for name in ('start', 'stop', 'hook', 'hooks', 'limits'):
        sub.add_parser(name)
    sub.add_parser('state').add_argument('state', choices=MESSAGES)
    sub.add_parser('install-hooks').add_argument('--target', default='~/.codex/hooks.json')
    args = parser.parse_args()
    RUN.mkdir(parents=True, exist_ok=True)
    try:
        if args.cmd == 'setup': setup(args.mac)
        elif args.cmd == 'start': start()
        elif args.cmd == 'stop': fifo_write('quit')
        elif args.cmd in ('state', 'limits'):
            # Manual uploads share the observer's lock for the entire transfer.
            # Otherwise another announce can interrupt the pending image chunks.
            with open(RUN / 'state.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if args.cmd == 'state':
                    display(args.state)
                else:
                    print(json.dumps(refresh_limits(force=True), indent=2))
                    saved = json.loads((RUN / 'state.json').read_text()) if (RUN / 'state.json').exists() else {}
                    display(saved.get('sent') or 'idle')
        elif args.cmd == 'hooks': print(json.dumps(hooks_document(), indent=2))
        elif args.cmd == 'install-hooks': install_hooks(args.target)
        elif args.cmd == 'hook': update(json.load(sys.stdin))
    except Exception as error:
        if args.cmd == 'hook':
            # Never emit a blocking hook decision or leak task text into logs.
            with open(RUN / 'errors.log', 'a') as log: log.write(str(error) + '\n')
            return 0
        print(str(error), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
