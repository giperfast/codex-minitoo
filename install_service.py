"""Install the MiniToo observer as a per-user macOS service."""
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import os
import time

root = Path(__file__).resolve().parent
label = 'local.codex.minitoo.watcher'
target = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
target.parent.mkdir(parents=True, exist_ok=True)
runtime = root / 'runtime'
runtime.mkdir(exist_ok=True)
definition = {'Label': label, 'ProgramArguments': [sys.executable, str(root / 'watch_codex.py')],
              'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True,
              'ThrottleInterval': 10, 'StandardOutPath': str(runtime / 'watcher.log'),
              'StandardErrorPath': str(runtime / 'watcher.log'),
              'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources'}}
target.write_bytes(plistlib.dumps(definition))
# Only remove this integration's old hooks, preserving every other handler.
hooks_path = Path.home() / '.codex/hooks.json'
if hooks_path.exists():
    old = hooks_path.read_bytes()
    document = json.loads(old)
    for event, groups in document.get('hooks', {}).items():
        kept = []
        for group in groups:
            handlers = [h for h in group.get('hooks', []) if str(root / 'minitoo.py') not in h.get('command', '')]
            if handlers:
                copy = dict(group)
                copy['hooks'] = handlers
                kept.append(copy)
        document['hooks'][event] = kept
    hooks_path.with_name('hooks.json.backup-' + str(time.time_ns())).write_bytes(old)
    hooks_path.write_text(json.dumps(document, indent=2) + '\n')
domain = f'gui/{os.getuid()}'
subprocess.run(['launchctl', 'bootout', domain, str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
print('Запущено: ' + label)
print('Лог: ' + str(runtime / 'watcher.log'))
