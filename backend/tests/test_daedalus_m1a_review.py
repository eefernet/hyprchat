"""Regressions for the 2026-10-26 review of the M1a change (offline; the worker check uses recorded rows).

Each test names the confirmed finding it pins. Review is off by default, so the router, the rollback path and
the deliverable evidence must all work without a model-written audit or reviewer.
"""
import asyncio
from types import SimpleNamespace


def failing_test(**extra):
    return {'id': 'test:.', 'cwd': '.', 'phase': 'test', 'is_test': True, 'origin': 'project', 'passed': False,
            'exit_code': 1, 'classification': 'application_defect', 'revision_id': 'r', 'execution_id': 'e', **extra}


class FakeOllama:
    """/api/ps + /api/generate over a fixed VRAM budget: a model that does not fit beside the others spills."""
    def __init__(self, vram, sizes, resident=(), busy=(), visible=None, load_error=None):
        # ``visible`` = the names /api/ps shows (the supervisor bridge filters it to the active model);
        # ``load_error`` = a 400 {'error': ...} answered to every load, as the bridge answers a rejected warm request.
        self.vram, self.sizes, self.loaded, self.calls, self.busy = vram, sizes, {}, [], set(busy)
        self.visible, self.load_error = visible, load_error
        for name, ctx in resident:
            self.place(name, ctx)

    def place(self, name, ctx):
        free = self.vram - sum(m['size_vram'] for n, m in self.loaded.items() if n != name)
        size = self.sizes[name]
        self.loaded[name] = {'name': name, 'model': name, 'context_length': ctx, 'size': size, 'size_vram': min(size, max(0, free))}

    def get(self, url, timeout=None):
        self.calls.append(('ps',))
        models = [m for n, m in self.loaded.items() if self.visible is None or n in self.visible]
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {'models': models})

    def post(self, url, json=None, timeout=None):
        if json.get('keep_alive') == 0:
            # Ollama 0.35 accepts the unload and ignores it while the runner is answering a request.
            self.calls.append(('unload', json['model']))
            if json['model'] not in self.busy:
                self.loaded.pop(json['model'], None)
        elif self.load_error:
            self.calls.append(('rejected-load', json['model']))
            def refuse(): raise RuntimeError('400 Client Error')
            return SimpleNamespace(status_code=400, raise_for_status=refuse, json=lambda: {'error': self.load_error})
        elif json['model'] in self.loaded and self.loaded[json['model']]['context_length'] == json['options']['num_ctx']:
            self.calls.append(('noop-load', json['model']))   # already loaded at that context: Ollama changes nothing
        else:
            self.calls.append(('load', json['model'])); self.place(json['model'], json['options']['num_ctx'])
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {})


def test_a_model_squeezed_by_another_resident_model_evicts_it_and_loads_alone():
    from coder_inference import ensure_context
    policy = SimpleNamespace(num_ctx=65536)
    # Live 2026-10-27: planning model resident, coder at 64K context spilled 1.6 GiB to the CPU (0.5 tok/s).
    ollama = FakeOllama(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)])
    ensure_context('http://ollama', 'coder', policy, 60, requests_client=ollama, settle=0, confirm=0)
    assert ollama.loaded['coder']['size_vram'] == 21 and 'planner' not in ollama.loaded
    assert ('unload', 'planner') in ollama.calls
    # Already resident at the right context: nothing is reloaded.
    before = len(ollama.calls)
    ensure_context('http://ollama', 'coder', policy, 60, requests_client=ollama, settle=0, confirm=0)
    assert all(call == ('ps',) for call in ollama.calls[before:])
    # A model that fits beside the other is left alone with it.
    roomy = FakeOllama(vram=48, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)])
    ensure_context('http://ollama', 'coder', policy, 60, requests_client=roomy, settle=0, confirm=0)
    assert 'planner' in roomy.loaded and ('unload', 'planner') not in roomy.calls


def test_a_freshly_loaded_model_is_read_again_before_it_counts_as_resident():
    from coder_inference import ensure_context
    # Live 2026-10-28: /api/ps said size_vram == size for a second after the load, then settled to the spill.
    class Settling(FakeOllama):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self.reads = 0
        def get(self, url, timeout=None):
            self.reads += 1
            response = super().get(url, timeout)
            if self.reads == 2:   # the first read after the load still shows the whole model in VRAM
                entry = dict(self.loaded['coder']); entry['size_vram'] = entry['size']
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'models': [entry, *[m for n, m in self.loaded.items() if n != 'coder']]})
            return response
    ollama = Settling(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)])
    ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 60, requests_client=ollama, settle=0, confirm=0)
    assert 'planner' not in ollama.loaded and ollama.loaded['coder']['size_vram'] == 21


def test_a_busy_runner_that_ignores_unloads_is_reported_as_busy_not_unfit():
    import pytest
    from coder_inference import ensure_context
    # Live 2026-10-28: the baseline kept the spilled coder answering; every keep_alive=0 returned in 2 ms, nothing unloaded.
    ollama = FakeOllama(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536), ('coder', 65536)], busy={'coder'})
    with pytest.raises(ValueError, match='still answering a request'):
        ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 61, requests_client=ollama, settle=0, confirm=0)
    assert ('noop-load', 'coder') not in ollama.calls   # never reloads on top of a runner that was not freed
    # Once the runner is idle the same call evicts and reloads alone.
    ollama.busy.clear()
    ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 61, requests_client=ollama, settle=0, confirm=0)
    assert ollama.loaded['coder']['size_vram'] == 21 and 'planner' not in ollama.loaded


def test_a_model_that_cannot_fit_even_alone_fails_visibly():
    import pytest
    from coder_inference import ensure_context
    ollama = FakeOllama(vram=16, sizes={'coder': 21})
    with pytest.raises(ValueError, match='does not fit in GPU memory'):
        ensure_context('http://ollama', 'coder', SimpleNamespace(num_ctx=65536), 60, requests_client=ollama, settle=0, attempts=2, confirm=0)


def test_live_m1a_follow_up_a_child_behind_the_bridge_cannot_evict_what_it_cannot_see():
    import pytest
    from coder_inference import ensure_context
    policy = SimpleNamespace(num_ctx=65536)
    # Live 2026-10-28 (cw3-13fb755c): the bridge filters /api/ps to the active model and refuses POSTs naming another,
    # so the child's loop unloaded itself six times beside the resident planner and raised a false "does not fit".
    # This is WHY eviction lives in the supervisor bridge (test_daedalus_sandbox.py); the child's loop is left intact.
    bridged = FakeOllama(vram=38, sizes={'coder': 21, 'planner': 19}, resident=[('planner', 65536)], visible={'coder'})
    with pytest.raises(ValueError, match='does not fit in GPU memory'):
        ensure_context('http://ollama', 'coder', policy, 60, requests_client=bridged, settle=0, confirm=0)
    assert ('unload', 'planner') not in bridged.calls and 'planner' in bridged.loaded
    assert bridged.calls.count(('unload', 'coder')) >= 2 and bridged.loaded['coder']['size_vram'] < 21
    # The bridge answers a refused warm load with 400 {'error': reason}; the child surfaces that reason, not "400 Client Error".
    refused = FakeOllama(vram=38, sizes={'coder': 21}, load_error='coder is spilled to the CPU and Ollama will not unload a runner that is still answering a request')
    with pytest.raises(ValueError, match='still answering a request'):
        ensure_context('http://ollama', 'coder', policy, 60, requests_client=refused, settle=0, confirm=0)
    assert ('rejected-load', 'coder') in refused.calls
