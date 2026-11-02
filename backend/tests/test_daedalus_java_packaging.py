"""M0 baseline findings 2026-10-28: Java builds fail on a missing <finalName>, usage templates are probed literally,
and stated exit codes never reach the builder. Offline; command_rows uses a fake runner."""
from pathlib import Path

from coder_request_probes import requested_commands, launcher, missing_program_row, built_jars, command_rows
from coder_policy7_builder import stated_behaviour, guidance

TASK = ('Build a Java 17 Maven project, ISBN checker. Running `java -jar target/isbn.jar validate 9780306406157` prints `valid`. '
        'Usage: `java -jar target/isbn.jar COMMAND ISBN`. An invalid ISBN prints an error to stderr and exits with code 2. '
        'A missing argument exits with code 1. Include JUnit 5 tests run by `mvn test` and a README.')


def test_usage_templates_with_bare_placeholders_are_not_probed():
    specs = requested_commands(TASK)
    assert [s['excerpt'] for s in specs] == ['java -jar target/isbn.jar validate 9780306406157']
    # `<file>` placeholders still render from fixtures; bracketed options and SHOUTING words do not.
    assert len(requested_commands('Run `python3 wordfreq.py <file> --top N`.')) == 1
    assert requested_commands('Run `python3 wordfreq.py [--top N] FILE`.') == []
    assert requested_commands('Run `solve FILE` or `solve FILE | grep x`.') == []


def project(tmp_path, jar='target/isbn-1.0-SNAPSHOT.jar'):
    root = tmp_path / 'project'; (root / 'target').mkdir(parents=True)
    (root / 'pom.xml').write_text('<project><artifactId>isbn</artifactId></project>')
    (root / jar).write_bytes(b'PK')
    return root


def test_a_jar_built_under_the_wrong_name_fails_naming_the_real_one_and_owning_pom(tmp_path):
    root = project(tmp_path)
    spec = requested_commands(TASK)[0]
    profile = {'profiles': [{'cwd': '.', 'toolchains': ['mvn', 'java']}]}
    assert launcher(profile, root, spec) is None
    assert built_jars(root) == ['target/isbn-1.0-SNAPSHOT.jar']
    row = missing_program_row('op', root, profile, TASK, spec, 1, tmp_path / 'fx', 'rev')
    assert row and not row['passed'] and row['id'] == 'probe:command:1'
    assert 'target/isbn-1.0-SNAPSHOT.jar' in row['reason'] and '<finalName>isbn</finalName>' in row['reason']
    assert row['owning_files'] == ['pom.xml']
    # The jar under the requested name runs as before.
    (root / 'target/isbn.jar').write_bytes(b'PK')
    assert launcher(profile, root, spec) == spec['tokens']


def test_the_jar_defect_reaches_the_repair_through_the_build_manifest(tmp_path):
    from coder_repair_packet import repair_targets
    root = project(tmp_path)
    spec = requested_commands(TASK)[0]
    row = missing_program_row('op', root, {'profiles': [{'cwd': '.', 'toolchains': ['mvn', 'java']}]}, TASK, spec, 1, tmp_path / 'fx', 'rev')
    job = {'brief': {'outcomes': [], 'batches': []},
           'last_patch': {'source_hashes': {'pom.xml': 'a', 'src/main/java/app/Isbn.java': 'b', 'README.md': 'c'}}}
    assert 'pom.xml' in repair_targets(job, [row])


def test_stated_exit_codes_and_invocations_reach_the_builder_task():
    lines = stated_behaviour({'task': TASK, 'user_task': TASK})
    text = '\n'.join(lines)
    assert 'invalid isbn: exit with code 2 with a message on stderr' in text
    assert 'missing argument: exit with code 1' in text
    assert '`java -jar target/isbn.jar validate 9780306406157` must run exactly as written' in text and '<finalName>' in text
    assert 'STATED BEHAVIOUR' in guidance({'task': TASK, 'user_task': TASK, 'brief': {'outcomes': []}})
    # Repair rounds carry the packet instead; nothing stated is repeated there.
    assert 'STATED BEHAVIOUR' not in guidance({'task': TASK, 'user_task': TASK, 'repair_round': 1, 'repair_feedback': [],
                                              'brief': {'outcomes': [], 'batches': []}, 'last_patch': {'source_hashes': {}}})
    assert stated_behaviour({'task': 'Build a quiet library.', 'user_task': 'Build a quiet library.'}) == []
