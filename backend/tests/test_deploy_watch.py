"""Watch registration must not silently omit newly added frontend sources."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / 'deploy_monitor.py'


def _monitor():
    if not SOURCE.is_file():  # the Codebox dev base ships backend/ alone
        pytest.skip('deploy_monitor.py is not in this checkout')
    spec = importlib.util.spec_from_file_location('deploy_monitor_watch_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frontend_inputs():
    files = set()
    for base in ('frontend/src', 'frontend/public'):
        for p in (REPO / base).rglob('*'):
            if not p.is_file() or p.name.startswith('.'):
                continue
            if '.test.' in p.name or '.fixtures.' in p.name:
                continue
            files.add(p.relative_to(REPO).as_posix())
    files |= {f for f in ('frontend/index.html', 'frontend/vite.config.js',
                          'frontend/package.json', 'frontend/package-lock.json')
              if (REPO / f).is_file()}
    return files


def test_all_frontend_build_inputs_are_watched():
    monitor = _monitor()
    expected = _frontend_inputs()
    assert expected, 'no frontend sources found'
    assert expected == monitor.FRONTEND_SRC_FILES
    for path in expected:
        assert monitor.WATCHED[path] == ('Frontend (build)', monitor.REMOTE_FRONTEND, False)


def test_frontend_tests_and_fixtures_are_not_watched():
    monitor = _monitor()
    watched = [p for p in monitor.WATCHED if p.startswith('frontend/')]
    assert not [p for p in watched if '.test.' in p or '.fixtures.' in p]
