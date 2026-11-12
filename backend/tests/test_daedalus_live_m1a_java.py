"""Regressions from live M1a job cw3-59371f48 (2026-10-28, Java unitconv build; offline, no model).

Stop at 45 s into coding, then Continue: the controller checked the near-empty resumed revision (checks: []),
decide() routed a repair and the EMPTY revision became the repair checkpoint (repair_checks=[]) with
repair_documentation=['.'] (behaviour outcomes had marked '.' documented). The builder then produced a real
project (build passed, 12 JUnit tests) without a README; 'documentation:.' counted as a suite regression,
restore_repair() restored the empty baseline, twice, and the card offered Accept on an archive holding only
DAEDALUS_REVIEW_<rev>.json.
"""
from coder_presentation import presentation

EMPTY, BUILT = 'e5aa323' + 'a' * 33, 'd18605d5' + 'b' * 32
PASSED = {'id': 'test:.', 'origin': 'project', 'is_test': True, 'passed': True, 'test_count': 12, 'revision_id': BUILT}
FAILED = {**PASSED, 'passed': False, 'classification': 'application_defect', 'reason': 'one assertion fails'}


def test_accept_is_not_offered_on_the_empty_start_of_a_build_or_an_unchecked_revision():
    base = {'engine':'openhands159', 'state': 'ready_for_review', 'policy_version': 7, 'mode': 'build_from_prompt', 'baseline_revision': EMPTY,
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
