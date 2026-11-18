"""The shipped .env.example must work as a systemd EnvironmentFile (2026-11-17).

systemd keeps everything after "=" as the value, so "QUICK_SEARCH_TIMEOUT=12   # seconds" became the value
"12   # seconds" and float() failed at import: a fresh install made with scripts/deploy.sh crash-looped.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / '.env.example'


def systemd_values(text):
    """What systemd's EnvironmentFile parser hands the service for simple unquoted lines."""
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(('#', ';')):
            continue
        key, sep, value = line.partition('=')
        assert sep, f'not an assignment: {line!r}'
        values[key.strip()] = value.strip()
    return values


def test_no_value_carries_a_comment():
    bad = [line for line in EXAMPLE.read_text().splitlines()
           if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=.*\s#', line)]
    assert bad == []


def test_the_backend_config_imports_with_the_example_loaded_as_systemd_would(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.isupper() or k in ('PATH', 'HOME', 'LANG', 'TMPDIR')}
    env.update(systemd_values(EXAMPLE.read_text()))
    # Point the server paths somewhere harmless: importing config must not need /opt.
    for key in ('DATABASE_PATH', 'UPLOAD_DIR', 'TOOLS_DIR', 'KB_DIR', 'SANDBOX_DIR', 'SETTINGS_PATH', 'CONNECTOR_SECRETS_PATH'):
        env[key] = str(tmp_path / key.lower())
    probe = ('import config; '
             'assert config.MAX_AGENT_ROUNDS == 40, config.MAX_AGENT_ROUNDS; '
             'print(config.QUICK_SEARCH_TIMEOUT, config.PDF_OCR, config.CODEBOX_URL)')
    result = subprocess.run([sys.executable, '-c', probe], cwd=ROOT / 'backend', env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    timeout, ocr, codebox = result.stdout.split()
    assert float(timeout) == 12.0 and ocr == 'on' and codebox == 'http://127.0.0.1:8585'


def test_every_documented_setting_is_read_somewhere():
    keys = set(re.findall(r'^#?\s?([A-Z][A-Z0-9_]+)=', EXAMPLE.read_text(), re.M))
    source = '\n'.join(p.read_text(errors='ignore') for p in (ROOT / 'backend').rglob('*.py')
                       if 'tests' not in p.parts and 'evals' not in p.parts)
    unread = sorted(k for k in keys if f'"{k}"' not in source and f"'{k}'" not in source)
    assert unread == []
