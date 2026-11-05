"""Deployment regressions: external secret file and offline preflight."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from monitor.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]


def test_external_secret_file(monkeypatch, tmp_path):
    env = tmp_path / 'radar.env'
    env.write_text('GUI_PASSWORD=synthetic-password-for-test\n', encoding='utf-8')
    monkeypatch.delenv('GUI_PASSWORD', raising=False)
    monkeypatch.setenv('RADAR_ENV_FILE', str(env))
    assert load_settings().gui_password == 'synthetic-password-for-test'


def test_process_environment_wins(monkeypatch, tmp_path):
    env = tmp_path / 'radar.env'
    env.write_text('GUI_PASSWORD=file-value\n', encoding='utf-8')
    monkeypatch.setenv('RADAR_ENV_FILE', str(env))
    monkeypatch.setenv('GUI_PASSWORD', 'process-value')
    assert load_settings().gui_password == 'process-value'


def run_check(tmp_path, text, overrides=None, create=True):
    secrets = tmp_path / 'radar.env'
    if create:
        secrets.write_text(text)
        secrets.chmod(0o600)
    data = tmp_path / 'data'
    data.mkdir()
    if overrides is not None:
        with sqlite3.connect(data / 'radar.sqlite3') as db:
            db.execute('CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('INSERT INTO kv VALUES (?, ?)', ('settings_overrides', json.dumps(overrides)))
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'SYSTEMROOT')}
    env.update(RADAR_ENV_FILE=str(secrets), RADAR_DATA_DIR=str(data))
    return subprocess.run([sys.executable, str(ROOT / 'tools/preflight.py')],
                          env=env, capture_output=True, text=True)


def test_preflight_valid_offline(tmp_path):
    r = run_check(tmp_path, 'GUI_PASSWORD=synthetic-test-password-32-chars\nRELAY_VIA_COLAB=0\n')
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'synthetic-test-password' not in r.stdout + r.stderr


def test_preflight_rejects_missing_secret_file(tmp_path):
    r = run_check(tmp_path, '', create=False)
    assert r.returncode == 1 and 'отсутствует' in r.stdout


def test_preflight_rejects_weak_password_without_printing_it(tmp_path):
    r = run_check(tmp_path, 'GUI_PASSWORD=weakxyz\n')
    assert r.returncode == 1 and 'GUI_PASSWORD' in r.stdout
    assert 'weakxyz' not in r.stdout + r.stderr


def test_preflight_checks_database_overrides(tmp_path):
    r = run_check(tmp_path, 'GUI_PASSWORD=synthetic-test-password-32-chars\n',
                  {'network': {'gui_public': True}})
    assert r.returncode == 1 and 'gui_public' in r.stdout
