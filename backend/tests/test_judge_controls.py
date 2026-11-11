"""Judge controls are tracked, deterministic and each pair differs by exactly one defect."""
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evals'))
import judge_controls
from engine_cases import DEVELOPMENT

RETAINED = Path(__file__).resolve().parents[2] / 'agent-output/qualification-20261104T2315Z/fixture-archives'


def test_controls_are_deterministic_pairs(tmp_path):
    first, second = judge_controls.write(tmp_path / 'a'), judge_controls.write(tmp_path / 'b')
    assert [r['sha256'] for r in first] == [r['sha256'] for r in second]
    assert len({r['sha256'] for r in first}) == len(first) == 10
    by_kind = {}
    for row in judge_controls.controls(): by_kind.setdefault(row['kind'], []).append(row)
    for kind, (good, broken) in by_kind.items():
        assert good['expected'] is True and broken['expected'] is False and good['case'] == broken['case']
        assert set(good['files']) == set(broken['files'])
        assert sum(good['files'][n] != broken['files'][n] for n in good['files']) == 1, kind
    development = {c['id'] for c in DEVELOPMENT}
    assert {r['case'] for r in first if isinstance(r['case'], str)} <= development


def test_controls_reproduce_the_archives_used_for_the_november_calibration(tmp_path):
    if not RETAINED.is_dir():
        import pytest; pytest.skip('retained calibration archives are not in this checkout')
    for row in judge_controls.write(tmp_path):
        with tarfile.open(row['storage_path']) as new, tarfile.open(RETAINED / (row['key'] + '.tar.gz')) as old:
            assert sorted(new.getnames()) == sorted(old.getnames()), row['key']
            for name in new.getnames():
                assert new.extractfile(name).read() == old.extractfile(name).read(), (row['key'], name)
