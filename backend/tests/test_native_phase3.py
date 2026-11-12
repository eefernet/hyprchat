"""Phase 3: native-language discovery, the builder's view of the user's words, and the native cohort."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evals'))
from coder_profiles import discover
from coder_request_view import LIMIT, original, render
from coder_repository import Repository
from context_policy import DEFAULTS


def repository(tmp_path, files):
    root = tmp_path / 'project'; root.mkdir(parents=True)
    for name, contents in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(contents)
    repo = Repository(root, tmp_path / 'repo', DEFAULTS['daedalus_exclude_dirs'])
    repo.refresh()
    return repo


def commands(result, phase):
    return [c['command'] for c in result['checks'] if c.get('phase') == phase]


def test_makefile_beside_cmake_is_built_but_never_a_test_run(tmp_path):
    files = {'CMakeLists.txt': 'project(x C)\nadd_executable(lines main.c)\n', 'main.c': 'int main(void){return 0;}\n',
             'Makefile': 'lines: main.c\n\tcc -o $@ $<\ntest:\n\t@echo "1 passed"\n'}
    result = discover(repository(tmp_path, files))
    assert commands(result, 'build') == ['cmake --build build', 'make']
    assert commands(result, 'test') == ['ctest --test-dir build --output-on-failure']
    assert not any('make test' in c['command'] for c in result['checks'])


def test_cmake_alone_and_makefile_alone_are_unchanged(tmp_path):
    cmake = discover(repository(tmp_path / 'a', {'CMakeLists.txt': 'project(x C)\n', 'main.c': 'int main(void){return 0;}\n'}))
    assert commands(cmake, 'build') == ['cmake --build build']
    make = discover(repository(tmp_path / 'b', {'Makefile': 'all:\n\ttrue\ntest:\n\ttrue\n', 'main.c': 'int main(void){return 0;}\n'}))
    assert commands(make, 'build') == ['make'] and commands(make, 'test') == ['make test']


def test_request_view_is_gated_bounded_and_silent_when_it_adds_nothing():
    base = {'engine_behavior_version': 3, 'task': 'Build a tip calculator.', 'original_user_request': 'make me a tip calc, nothing fancy'}
    text = render(base)
    assert 'make me a tip calc, nothing fancy' in text and 'may not add requirements' in text
    assert render({**base, 'engine_behavior_version': 2}) == ''
    assert render({**base, 'original_user_request': '  build a TIP   calculator. '}) == ''
    assert render({**base, 'original_user_request': ''}) == ''
    long = render({**base, 'original_user_request': 'x' * (LIMIT + 500)})
    assert 'x' * LIMIT in long and 'x' * (LIMIT + 1) not in long and 'not shown' in long
    job = {'engine_behavior_version': 3, 'user_task': 'Build a tip calculator.', 'original_user_request': 'tip calc pls'}
    assert original(job) == 'tip calc pls' and original({**job, 'engine_behavior_version': 2}) == ''
    assert original({**job, 'original_user_request': 'Build a tip calculator.'}) == ''


def test_native_cohort_is_one_definition_with_a_full_floor():
    import engine_cohorts
    from engine_cases import private_probes
    cases, repetitions = engine_cohorts.roster('native')
    assert [c['id'] for c in cases] == ['native-c', 'native-cpp', 'native-rust', 'native-csharp'] and repetitions == 2
    assert engine_cohorts.floor('native') == 1.0 and 'native' in engine_cohorts.IDENTITY and 'native' in engine_cohorts.COMPOSER
    every = [c['id'] for bank in engine_cohorts.COMPOSER.values() for c in bank]
    assert len(every) == len(set(every))
    for case in cases:
        assert case['family'] == 'core' and len(private_probes(case, True)) > len(private_probes(case, False)) >= 3
        # A probe never appears in the prompt as a worked example beyond what a user would say.
        assert 'probe' not in case['task'].lower()


def test_followup_cannot_silently_become_a_new_project():
    from coder_followup_guard import refusal
    project = {'id': 'cp-1', 'name': 'roman'}
    assert 'edit_project' in refusal({'mode': 'build_from_prompt'}, {}, project) and 'roman' in refusal({'mode': 'build_from_prompt'}, {}, project)
    assert refusal({'mode': 'build_from_prompt', 'new_project': True}, {}, project) == ''
    assert refusal({'mode': 'build_from_prompt', 'new_project': 'true'}, {}, project) != ''      # only a real boolean confirms
    assert refusal({'mode': 'edit_project'}, {}, project) == '' and refusal({}, {}, project) == ''
    assert refusal({'mode': 'build_from_prompt'}, {}, None) == ''                                  # first build in a conversation
    assert refusal({'mode': 'build_from_prompt'}, {'new_project': True}, project) == ''            # composer choice wins
    assert refusal({'mode': 'build_from_prompt'}, {'project_id': 'cp-1'}, project) == ''           # existing selected-project error applies


def test_tool_schema_names_the_modes_the_controller_accepts():
    import tools
    schema = next(v for v in vars(tools).values() if isinstance(v, dict) and 'start_coder_workflow' in v)['start_coder_workflow']['function']
    assert 'edit_project' in schema['parameters']['properties']['mode']['description'] and 'fix_uploaded_project' not in str(schema)
    assert schema['parameters']['properties']['new_project']['type'] == 'boolean'
