"""Deploy the built terminal to Application Support and manage its macOS user service."""
import argparse
import hashlib
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/Calibrain Harness'
LABEL = 'com.floyd.calibrain-harness'
DOMAIN = f'gui/{os.getuid()}'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def prepare_runtime():
    if not (ROOT / 'web-next/out/index.html').exists():
        raise SystemExit('Build web-next first.')
    if run('git', '-C', str(ROOT), 'status', '--porcelain').stdout.strip():
        raise SystemExit('Commit the scoped source changes before deploying a release.')
    sha = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD').stdout.strip()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    RUNTIME.chmod(0o700)
    release = RUNTIME / 'releases' / sha
    if not release.exists():
        stage = RUNTIME / 'releases' / (sha + '.staging')
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT / 'ctb', stage / 'ctb', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'web-next/out', stage / 'web-next/out', dirs_exist_ok=True)
        shutil.copy2(ROOT / 'LICENSE', stage / 'LICENSE')
        manifest = {'source_commit': sha, 'source_checkout': str(ROOT), 'built_at': time.time(),
                    'files': {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in stage.rglob('*') if p.is_file()}}
        (stage / 'release.json').write_text(json.dumps(manifest, indent=2))
        stage.rename(release)
    venv = RUNTIME / 'venv'
    if not (venv / 'bin/python').exists():
        run(sys._base_executable, '-m', 'venv', str(venv))
        versions = run(str(ROOT / '.venv/bin/python'), '-m', 'pip', 'freeze').stdout
        lock = RUNTIME / 'requirements.lock'
        lock.write_text(versions)
        result = run(str(venv / 'bin/python'), '-m', 'pip', 'install', '-r', str(lock), check=False)
        if result.returncode:
            raise SystemExit(result.stderr)
    if not (RUNTIME / '.env').exists():
        shutil.copy2(ROOT / '.env', RUNTIME / '.env')
        (RUNTIME / '.env').chmod(0o600)
    (RUNTIME / 'data').mkdir(exist_ok=True)
    return release


def stop_checkout_listener():
    result = run('lsof', '-t', '-nP', '-iTCP:8800', '-sTCP:LISTEN', check=False)
    for raw in result.stdout.split():
        pid = int(raw)
        cmd = run('ps', '-p', str(pid), '-o', 'command=').stdout
        cwd = run('lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn').stdout
        if '-m ctb.server' not in cmd or not any('n' + str(p) in cwd for p in (ROOT, RUNTIME)):
            raise SystemExit('Another application owns port 8800. It was not stopped.')
        os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if run('lsof', '-t', '-nP', '-iTCP:8800', '-sTCP:LISTEN', check=False).returncode:
            return
        time.sleep(.1)
    raise SystemExit('The previous server has not released port 8800.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'start', 'stop', 'status'])
    action = parser.parse_args().action
    if sys.platform != 'darwin':
        parser.error('Run python -m ctb.server on other systems.')
    if action == 'install':
        release = prepare_runtime()
        if PLIST.exists():
            previous = plistlib.loads(PLIST.read_bytes())
            if previous.get('WorkingDirectory') != str(ROOT) and not str(previous.get('WorkingDirectory', '')).startswith(str(RUNTIME)):
                raise SystemExit('Another checkout owns this service label.')
            run('launchctl', 'bootout', DOMAIN, str(PLIST), check=False)
        stop_checkout_listener()
        database = RUNTIME / 'data/alpaca.db'
        if database.exists():
            backups=RUNTIME/'data/backups'
            backups.mkdir(mode=0o700,exist_ok=True)
            backup=backups/('before-'+release.name+'-'+str(int(time.time()))+'.db')
            with sqlite3.connect(database) as src,sqlite3.connect(backup) as dst:
                src.backup(dst)
            backup.chmod(0o600)
        if not database.exists():
            source = ROOT / 'data/alpaca.db'
            if source.exists():
                with sqlite3.connect(source) as src, sqlite3.connect(database) as dst:
                    src.backup(dst)
        current = RUNTIME / 'current'
        next_link = RUNTIME / 'current.next'
        next_link.unlink(missing_ok=True)
        next_link.symlink_to(release, target_is_directory=True)
        next_link.replace(current)
        config = {'Label': LABEL, 'ProgramArguments': [str(RUNTIME / 'venv/bin/python'), '-u', '-m', 'ctb.server'],
                  'WorkingDirectory': str(current), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                  'StandardOutPath': str(RUNTIME / 'data/service.stdout.log'),
                  'StandardErrorPath': str(RUNTIME / 'data/service.stderr.log'),
                  'EnvironmentVariables': {'PYTHONUNBUFFERED': '1', 'CTB_ENV_PATH': str(RUNTIME / '.env'),
                    'CTB_DB_PATH': str(database), 'CTB_KILL_FILE': str(RUNTIME / 'KILL')}}
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(config))
        PLIST.chmod(0o600)
        run('launchctl', 'bootstrap', DOMAIN, str(PLIST))
        print('Deployed ' + release.name + ': http://127.0.0.1:8800')
    elif action == 'start':
        if run('launchctl', 'print', DOMAIN + '/' + LABEL, check=False).returncode == 0:
            run('launchctl', 'kickstart', DOMAIN + '/' + LABEL)
        elif PLIST.exists():
            run('launchctl', 'bootstrap', DOMAIN, str(PLIST))
        else:
            raise SystemExit('Install the service first.')
        print('Started: http://127.0.0.1:8800')
    elif action == 'stop':
        run('launchctl', 'bootout', DOMAIN, str(PLIST))
        print('Stopped.')
    else:
        result = run('launchctl', 'print', DOMAIN + '/' + LABEL, check=False)
        print(result.stdout if result.returncode == 0 else 'Service is not loaded.')


if __name__ == '__main__':
    main()
