"""Manage this checkout's local macOS background service without storing credentials in launchd."""
import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.floyd.calibrain-harness'
DOMAIN = f'gui/{os.getuid()}'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'start', 'stop', 'status'])
    action = parser.parse_args().action
    if sys.platform != 'darwin':
        parser.error('This service helper uses macOS launchd. Run python -m ctb.server elsewhere.')
    if action == 'install':
        if not (ROOT / '.venv/bin/python').exists() or not (ROOT / 'web-next/out/index.html').exists():
            raise SystemExit('Install Python dependencies and build web-next before installing the service.')
        config = {'Label': LABEL, 'ProgramArguments': [str(ROOT / '.venv/bin/python'), '-u', '-m', 'ctb.server'],
                  'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                  'StandardOutPath': str(ROOT / 'data/service.stdout.log'),
                  'StandardErrorPath': str(ROOT / 'data/service.stderr.log'),
                  'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'}}
        if PLIST.exists():
            previous = plistlib.loads(PLIST.read_bytes())
            if previous.get('WorkingDirectory') != str(ROOT):
                raise SystemExit('A different checkout owns this service label; refusing to overwrite it.')
            run('launchctl', 'bootout', DOMAIN, str(PLIST), check=False)
        (ROOT / 'data').mkdir(exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(config))
        PLIST.chmod(0o600)
        run('launchctl', 'bootstrap', DOMAIN, str(PLIST))
        print('Installed and started: http://127.0.0.1:8800')
    elif action == 'start':
        loaded = run('launchctl', 'print', DOMAIN + '/' + LABEL, check=False)
        if loaded.returncode == 0:
            run('launchctl', 'kickstart', DOMAIN + '/' + LABEL)
        elif PLIST.exists():
            run('launchctl', 'bootstrap', DOMAIN, str(PLIST))
        else:
            raise SystemExit('Install this checkout first: python scripts/service.py install')
        print('Started: http://127.0.0.1:8800')
    elif action == 'stop':
        # Disable KeepAlive before stopping; next explicit start installs it again.
        run('launchctl', 'bootout', DOMAIN, str(PLIST))
        print('Stopped. Use service.py install to start it again.')
    else:
        r = run('launchctl', 'print', DOMAIN + '/' + LABEL, check=False)
        print(r.stdout if r.returncode == 0 else 'Service is not loaded.')


if __name__ == '__main__':
    main()
