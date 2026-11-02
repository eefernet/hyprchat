"""Regressions from live M1a job cw3-59371f48 (2026-10-28, Java unitconv build; offline, no model).

Stop at 45 s into coding, then Continue: the controller checked the near-empty resumed revision (checks: []),
decide() routed a repair and the EMPTY revision became the repair checkpoint (repair_checks=[]) with
repair_documentation=['.'] (behaviour outcomes had marked '.' documented). The builder then produced a real
project (build passed, 12 JUnit tests) without a README; 'documentation:.' counted as a suite regression,
restore_repair() restored the empty baseline, twice, and the card offered Accept on an archive holding only
DAEDALUS_REVIEW_<rev>.json.
"""
from coder_policy7_ops import retained_deliverables, verified_checkpoint, verified_rows
from coder_presentation import presentation
from tests.test_daedalus_policy7_lifecycle import BRIEF, _drive
from tests.test_daedalus_required_tests import check_operation, row

EMPTY, BUILT = 'e5aa323' + 'a' * 33, 'd18605d5' + 'b' * 32
PASSED = {'id': 'test:.', 'origin': 'project', 'is_test': True, 'passed': True, 'test_count': 12, 'revision_id': BUILT}
FAILED = {**PASSED, 'passed': False, 'classification': 'application_defect', 'reason': 'one assertion fails'}


def test_only_documentation_outcomes_mark_a_component_documented():
    outcomes = [{'id': 'o1', 'text': 'Converts units', 'evidence_types': ['behavior'], 'component': '.'},
                {'id': 'o4', 'text': 'README documents usage', 'evidence_types': ['documentation'], 'component': '.'}]
    job = {'task': 'Build unitconv', 'brief': {'outcomes': outcomes}, 'last_patch': {'source_hashes': {}},
           'verification_summary': {'outcomes': [{**outcomes[0], 'missing_evidence': []},
                                                 {**outcomes[1], 'missing_evidence': ['documentation']}]}}
    assert retained_deliverables(job) == {'repair_deliverables': [], 'repair_documentation': []}
    present = {**job, 'verification_summary': {'outcomes': [{**o, 'missing_evidence': []} for o in outcomes]}}
    assert retained_deliverables(present)['repair_documentation'] == ['.']


def test_verified_checkpoint_needs_a_passed_project_row_and_never_the_empty_start_of_a_build():
    assert not verified_checkpoint({'repair_checkpoint': EMPTY, 'repair_checks': []}, EMPTY)
    assert not verified_checkpoint({'repair_checkpoint': EMPTY, 'repair_checks': [FAILED]}, EMPTY)
    assert not verified_checkpoint({'repair_checkpoint': EMPTY, 'repair_checks': [{**PASSED, 'origin': 'controller'}]}, EMPTY)
    assert verified_checkpoint({'repair_checkpoint': BUILT, 'repair_checks': [PASSED]}, BUILT)
    assert not verified_checkpoint({'mode': 'build_from_prompt', 'baseline_revision': EMPTY, 'repair_checkpoint': EMPTY,
                                    'repair_checks': [PASSED]}, EMPTY)
    # An edit's baseline is a real project: it stays a target when its rows say so.
    assert verified_checkpoint({'mode': 'edit_project', 'baseline_revision': BUILT, 'eligible_revision': BUILT,
                                'eligible_checks': [PASSED]}, BUILT)
    assert not verified_checkpoint({'eligible_revision': EMPTY, 'eligible_checks': []}, EMPTY)
    # The worker payload carries the required set rather than the eligible rows; the set grows only from passing tests.
    assert verified_checkpoint({'eligible_revision': BUILT, 'required_tests': ['.::T.test_add']}, BUILT)
    assert not verified_checkpoint({'eligible_revision': BUILT}, 'f' * 40) and not verified_checkpoint({}, '')
    assert verified_rows([PASSED]) and not verified_rows([FAILED]) and not verified_rows(None)


def test_the_check_operation_skips_a_rollback_onto_an_unverified_checkpoint(tmp_path, monkeypatch):
    # The fixture tree has no README, so 'documentation:.' is missing, as in the live job; the checkpoint has no rows.
    current = [row(passed=True, found={'a': 'passed'})]
    result, calls, events, eligible, repository = check_operation(
        tmp_path, monkeypatch, {'repair_checkpoint': 'OLDER', 'repair_checks': [], 'repair_documentation': ['.']}, {'': current})
    assert 'restored_snapshot' not in result and 'repair_regression' not in result
    assert result['eligible'] and (repository.root / 'app.py').read_text() == 'VALUE = 2\n'
    skipped = [data for kind, data in events if kind == 'rollback_skipped']
    assert skipped and skipped[0]['regressed'] == ['documentation:.'] and skipped[0]['target'] != eligible


def test_the_check_operation_still_restores_a_verified_checkpoint(tmp_path, monkeypatch):
    retained = {'id': '.:test:0', 'origin': 'project', 'is_test': True, 'passed': True, 'test_count': 2}
    result, calls, events, eligible, repository = check_operation(
        tmp_path, monkeypatch, {'repair_checkpoint': 'OLDER', 'repair_checks': [retained]},
        {'': [row(passed=False, found={'a': 'failed'}, id='.:test:0')]})
    assert result['repair_regression']['regressed'] == ['.:test:0']
    assert result['restored_snapshot']['revision'] != eligible and (repository.root / 'app.py').read_text() == 'VALUE = 0\n'
    assert not any(kind == 'rollback_skipped' for kind, _ in events)


def test_the_controller_sets_a_repair_checkpoint_only_on_a_verified_revision(monkeypatch, tmp_path):
    async def operate(job, kind, **extra):
        raise AssertionError('no model operation should be dispatched: ' + kind)
    # The resumed, near-empty revision: checks [] -> "tests are missing" -> repair, but it is no rollback target.
    resumed = dict(state='accepting', revision_id=EMPTY, baseline_revision=EMPTY, brief=BRIEF, checks=[],
                   repair_checkpoint=BUILT, repair_checks=[PASSED], repair_documentation=['.'])
    job = _drive(monkeypatch, tmp_path, resumed, operate)
    assert job['state'] == 'coding' and job['repair_round'] == 1
    assert job['repair_checkpoint'] == BUILT and job['repair_checks'] == [PASSED] and job['repair_documentation'] == ['.']
    (tmp_path / 'built').mkdir()
    built = dict(state='accepting', revision_id=BUILT, baseline_revision=EMPTY, brief=BRIEF, checks=[PASSED, FAILED],
                 execution_profile={'checks': [{'id': 'test:.', 'is_test': True, 'cwd': '.'}]})
    job = _drive(monkeypatch, tmp_path / 'built', built, operate)
    assert job['state'] == 'coding' and job['repair_checkpoint'] == BUILT and job['repair_checks'] == [PASSED, FAILED]


def test_accept_is_not_offered_on_the_empty_start_of_a_build_or_an_unchecked_revision():
    base = {'state': 'ready_for_review', 'policy_version': 7, 'mode': 'build_from_prompt', 'baseline_revision': EMPTY,
            'candidate_artifact': {'id': 'x', 'metadata': {'revision_id': EMPTY}}, 'stop_limit': 'application_repairs',
            'blocker': 'Repair regressed a retained passing test. The earlier checkpoint was restored for review.',
            'verification_summary': {'revision_id': EMPTY, 'checks': [], 'checks_passed': False, 'checks_ok': False, 'rolled_back': True}}
    accept = presentation(base)['actions']['accept']
    assert not accept['enabled'] and accept['revision_id'] == ''
    assert accept['disabled_reason'] == 'Nothing to accept yet: this version has no verified project files'
    unchecked = {**base, 'baseline_revision': 'other', 'candidate_artifact': {'id': 'x', 'metadata': {'revision_id': BUILT}},
                 'verification_summary': {'revision_id': BUILT, 'checks': []}}
    assert presentation(unchecked)['actions']['accept']['disabled_reason'] == 'Nothing to accept yet: this version has no verified project files'
    ready = {**base, 'candidate_artifact': {'id': 'x', 'metadata': {'revision_id': BUILT}},
             'verification_summary': {'revision_id': BUILT, 'checks': [PASSED], 'checks_passed': True, 'checks_ok': True, 'exercised': True}}
    accept = presentation(ready)['actions']['accept']
    assert accept['enabled'] and accept['revision_id'] == BUILT and accept['disabled_reason'] == ''


def test_cw3_59371f48_accept_refuses_the_empty_starting_tree_and_review_only_archives(tmp_path):
    import io, tarfile
    from db.coder_promotion import empty_candidate, REVIEW_PREFIX
    def archive(name, members):
        path = tmp_path / name
        with tarfile.open(path, 'w:gz') as out:
            for member in members:
                info = tarfile.TarInfo(member); data = b'x'; info.size = len(data); out.addfile(info, io.BytesIO(data))
        return path
    review_only = archive('empty.tar.gz', [REVIEW_PREFIX + 'e' * 40 + '.json'])
    real = archive('real.tar.gz', [REVIEW_PREFIX + 'd' * 40 + '.json', 'pom.xml', 'src/main/java/app/UnitConv.java'])
    build = {'mode': 'build_from_prompt', 'baseline_revision': 'e' * 40}
    assert empty_candidate(build, 'e' * 40, real)            # the starting tree of a new build, whatever the archive holds
    assert empty_candidate(build, 'd' * 40, review_only)     # nothing but the review report
    assert not empty_candidate(build, 'd' * 40, real)
    edit = {'mode': 'edit_project', 'baseline_revision': 'e' * 40}
    assert not empty_candidate(edit, 'e' * 40, real)         # an edit's baseline is the uploaded project itself
    assert not empty_candidate(edit, 'e' * 40, tmp_path / 'missing.tar.gz')
