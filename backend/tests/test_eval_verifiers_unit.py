"""Pure helpers of the independent verifiers (sweep3 robustness round, 2026-09-26).

The Kanban and medium verifiers assumed layouts the requests never state (column ids, $PORT, exact-text
title elements, an empty database). These helpers carry the robustness fixes; the behavior assertions
they serve are unchanged and are exercised only on Codebox with a real browser.
"""
from pathlib import Path

import pytest

from evals.verify_webapp import column_selectors, port_from_log
from evals.policy7_golden import requirements_file, title_pattern


def test_port_from_log_reads_the_latest_listening_line_and_ignores_address_in_use():
    assert port_from_log('Server listening on port 3000\n') == 3000
    assert port_from_log('Local Task Board running at http://localhost:4173/\n') == 4173
    assert port_from_log('listening on :3000\nnow listening on http://127.0.0.1:5055\n') == 5055
    assert port_from_log('> app@1.0.0 start\n> node server.js\n\nDatabase ready\n') is None
    assert port_from_log('Error: listen EADDRINUSE: address already in use :::3000\n') is None
    assert port_from_log('') is None


def test_column_selectors_try_the_id_conventions_first_then_the_heading_text():
    kinds = [kind for kind, _ in column_selectors('in_progress')]
    assert kinds[0] == 'css' and set(kinds[1:]) == {'xpath'}
    css = column_selectors('in_progress')[0][1]
    assert '[data-status="in_progress"]' in css and '#column-in_progress' in css
    xpaths = [expr for kind, expr in column_selectors('in_progress') if kind == 'xpath']
    assert any("='in progress'" in x for x in xpaths) and any("='in-progress'" in x for x in xpaths)
    assert all(x.endswith('/ancestor::*[self::section or self::div or self::article][1]') for x in xpaths)
    assert all('self::h1' in x and 'self::h6' in x and 'translate(' in x for x in xpaths)
    assert [expr for kind, expr in column_selectors('done') if kind == 'xpath'][0].count("='done'") == 1


def test_title_pattern_matches_the_title_inside_a_row_but_not_a_longer_title():
    pattern = title_pattern('Browser task 1440')
    assert pattern.search('Browser task 1440 · todo · high')
    assert pattern.search('Browser task 1440')
    assert not pattern.search('Browser task 14400')
    assert not pattern.search('Edited browser task 1440')   # a different title (case differs) is not the original
    assert title_pattern('a.b(c)').search('x a.b(c) y') and not title_pattern('a.b(c)').search('aXb(c)')


def test_requirements_file_prefers_the_root_requirements_and_never_node_modules(tmp_path: Path):
    assert requirements_file(tmp_path) is None
    (tmp_path / 'node_modules' / 'pkg').mkdir(parents=True)
    (tmp_path / 'node_modules' / 'pkg' / 'requirements.txt').write_text('evil\n')
    assert requirements_file(tmp_path) is None
    (tmp_path / 'backend').mkdir()
    (tmp_path / 'backend' / 'requirements.txt').write_text('fastapi\n')
    assert requirements_file(tmp_path) == tmp_path / 'backend' / 'requirements.txt'
    (tmp_path / 'requirements-dev.txt').write_text('pytest\n')
    assert requirements_file(tmp_path) == tmp_path / 'requirements-dev.txt'
    (tmp_path / 'requirements.txt').write_text('fastapi\nuvicorn\n')
    assert requirements_file(tmp_path) == tmp_path / 'requirements.txt'
