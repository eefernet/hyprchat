import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('vpn_candidates', ROOT / 'scripts/searxng/vpn_candidates.py')
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)


def profiles(tmp_path):
    paths = []
    for name, address in [('bad-a', '192.0.2.1'), ('good', '192.0.2.2'), ('bad-alias', '192.0.2.1')]:
        path = tmp_path / (name + '.ovpn')
        path.write_text('client\nremote ' + address + ' 1194\n')
        paths.append(str(path))
    return paths


def test_failure_cooldown_applies_to_all_profiles_for_same_entry(tmp_path):
    paths = profiles(tmp_path)
    state = tmp_path / 'state.json'
    vpn.update_failure(state, '192.0.2.1', True, 100)
    failures = vpn.load_failures(state)
    assert vpn.order_candidates(paths, failures, 101) == [paths[1]]
    assert vpn.order_candidates(paths, failures, 100 + vpn.COOLDOWN_SECONDS) == paths
    assert state.stat().st_mode & 0o777 == 0o600


def test_all_cooling_pool_retries_oldest_and_success_clears_failure(tmp_path):
    paths = profiles(tmp_path)
    state = tmp_path / 'state.json'
    vpn.update_failure(state, '192.0.2.2', True, 100)
    vpn.update_failure(state, '192.0.2.1', True, 200)
    assert vpn.order_candidates(paths, vpn.load_failures(state), 201)[0] == paths[1]
    vpn.update_failure(state, '192.0.2.1', False, 202)
    assert vpn.order_candidates(paths, vpn.load_failures(state), 203) == [paths[0], paths[2]]


def test_missing_or_damaged_bookkeeping_preserves_original_candidates(tmp_path):
    paths = profiles(tmp_path)
    state = tmp_path / 'state.json'
    assert vpn.order_candidates(paths, vpn.load_failures(state), 100) == paths
    state.write_text('damaged')
    assert vpn.order_candidates(paths, vpn.load_failures(state), 100) == paths
