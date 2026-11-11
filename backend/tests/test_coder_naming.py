"""Readable download names: the browser-facing name changes, the stored file, URL and filename column do not."""
import asyncio
import io
import json
import tarfile

import pytest

import coder_jobs as controller
import database as db
from coder_naming import NAME, base_for, display_name_for, download_name, manifest_title, slug, title_of
from coder_presentation import presentation
from db import coder_jobs as store
from db import coder_promotion
from .test_daedalus_versions import setup


@pytest.mark.parametrize('text,expected', [
    ('Create a plain HTML/JavaScript tip calculator in index.html. Inputs with ids amount', 'html-javascript-tip-calculator'),
    ('Create a Maven Java project that builds target/app.jar with a Main-Class.', 'maven-java-project'),
    ('Make me a C tool called lines that counts lines from stdin.', 'lines'),
    ('please can you build me a simple todo app', 'todo-app'),
    ('Tip_Calculator', 'tip-calculator'), ('@scope/My.Pkg', 'scope-my-pkg'),
    ('Crème brûlée timer', 'creme-brulee-timer'), ('', ''), ('!!! ???', ''), ('../../etc/passwd', 'etc-passwd'),
])
def test_slug_table(text, expected):
    assert slug(text) == expected
    assert len(slug('word ' * 50)) <= 40


def test_download_names_are_short_safe_and_carry_kind_and_version():
    assert download_name('tip-calculator') == 'tip-calculator.tar.gz'
    assert download_name('tip-calculator', 'review') == 'tip-calculator-review.tar.gz'
    assert download_name('tip-calculator', 'accepted', 2) == 'tip-calculator-v2.tar.gz'
    assert download_name('tip-calculator', 'unverified') == 'tip-calculator-unverified.tar.gz'
    assert download_name('"; rm -rf /\r\nX: y', 'review', 3) == 'rm-rf-v3-review.tar.gz'   # header-safe whatever the title holds
    assert download_name('', 'review') == 'project-review.tar.gz'
    long = download_name('a' * 300, 'unverified', 12)
    assert NAME.fullmatch(long) and len(long) <= 87
    assert title_of('tip-calculator') == 'Tip calculator'


def archive(path, members):
    with tarfile.open(path, 'w:gz') as bundle:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
                bundle.addfile(info)
            else:
                info.size = len(data); bundle.addfile(info, io.BytesIO(data))
    return path


def test_manifest_title_reads_declared_names_in_place(tmp_path):
    assert manifest_title(archive(tmp_path / 'a.tar.gz', {'package.json': b'{"name":"@me/tip-calculator"}'})) == 'tip-calculator'
    assert manifest_title(archive(tmp_path / 'b.tar.gz', {'pyproject.toml': b'[project]\nname = "Temp_Converter"\n'})) == 'temp-converter'
    assert manifest_title(archive(tmp_path / 'c.tar.gz', {'Cargo.toml': b'[package]\nname = "prefixer"\n'})) == 'prefixer'
    pom = b'<project><parent><artifactId>parent-pom</artifactId></parent><artifactId>greeter</artifactId></project>'
    assert manifest_title(archive(tmp_path / 'd.tar.gz', {'pom.xml': pom})) == 'greeter'
    assert manifest_title(archive(tmp_path / 'e.tar.gz', {'CMakeLists.txt': b'cmake_minimum_required(VERSION 3.20)\nproject(span LANGUAGES CXX)\n'})) == 'span'
    assert manifest_title(archive(tmp_path / 'f.tar.gz', {'Ledger.sln': b'', 'src/Ledger/Ledger.csproj': b'<Project/>'})) == 'ledger'
    # A wrapper directory is fine; a generic name falls through to the next manifest or to nothing.
    assert manifest_title(archive(tmp_path / 'g.tar.gz', {'./pkg/package.json': b'{"name":"app"}', 'pkg/pyproject.toml': b'[project]\nname="real-name"\n'})) == 'real-name'
    assert manifest_title(archive(tmp_path / 'h.tar.gz', {'package.json': b'{"name":"app"}'})) is None


def test_manifest_title_survives_hostile_and_broken_archives(tmp_path):
    hostile = archive(tmp_path / 'x.tar.gz', {'../package.json': b'{"name":"escaped"}', 'package.json': None,
        'a/b/c/package.json': b'{"name":"too-deep"}', 'pyproject.toml': b'not [toml', 'Cargo.toml': b'x' * (300 * 1024)})
    assert manifest_title(hostile) is None
    (tmp_path / 'broken.tar.gz').write_bytes(b'not a tarball')
    assert manifest_title(tmp_path / 'broken.tar.gz') is None and manifest_title(tmp_path / 'missing.tar.gz') is None
    assert base_for({'original_user_request': 'Make me a C tool called lines.'}, tmp_path / 'broken.tar.gz') == 'lines'
    assert base_for({}, None) == 'project'


def test_only_a_validated_name_is_ever_sent_to_a_browser():
    assert display_name_for({'metadata': {'download_name': 'tip-calculator-v2.tar.gz'}}) == 'tip-calculator-v2.tar.gz'
    assert display_name_for({'metadata_json': json.dumps({'download_name': 'x.tar.gz'})}) == 'x.tar.gz'
    for bad in ('../x.tar.gz', 'x"\r\nSet-Cookie: a=b.tar.gz', 'X.tar.gz', 'x.zip', '', None, 7, 'a' * 100 + '.tar.gz'):
        assert display_name_for({'metadata': {'download_name': bad}}) is None
    assert display_name_for({'metadata_json': 'not json'}) is None and display_name_for({}) is None and display_name_for(None) is None


async def deliver(tmp_path, job, revision, members, candidate=True):
    """What _deliver does after the archive is on disk, with the worker's internal storage name."""
    (tmp_path / 'outputs').mkdir(exist_ok=True)
    path = archive(tmp_path / 'outputs' / f"candidate-pub-{job['id']}-{revision}.tar.gz", members)
    readable = await controller._readable_name(job, path, candidate)
    summary = {'revision_id': revision, 'accepted': False, 'checks_passed': True, 'checks_ok': True, 'exercised': True, 'evidence_version': 4}
    await store.save(job['id'], state='candidate_packaging', revision_id=revision, verification_summary=summary)
    return await store.finish_candidate(job['id'], revision, artifact_id='artifact-' + revision[:8], filename=path.name,
        url='/api/downloads/' + path.name, kind='archive', storage_path=str(path), status='review_candidate',
        **({'title': readable['download_name']} if readable else {}),
        metadata={'revision_id': revision, 'checks_passed': True, 'runnable': True, **readable})


def test_download_routes_send_the_readable_name_and_keep_storage_identity(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    from routes.artifacts import download_artifact_ep
    from routes.downloads import archive_contents, download_file_endpoint
    async def scenario():
        build = await controller.create('conversation', 'Create a plain HTML/JavaScript tip calculator in index.html.', model='coder:local', key='build')
        assert build['presentation']['project_name'] == 'Html javascript tip calculator'
        files = {'package.json': b'{"name":"tip-calculator"}', 'index.html': b'<html></html>'}
        first = await deliver(tmp_path, build, 'a' * 40, files)
        stored = first['filename']
        assert stored.startswith('candidate-pub-') and first['url'] == '/api/downloads/' + stored      # unchanged
        assert first['title'] == first['metadata']['download_name'] == 'tip-calculator-review.tar.gz'
        assert (await download_artifact_ep(first['id'])).filename == 'tip-calculator-review.tar.gz'
        assert (await download_file_endpoint(stored)).filename == 'tip-calculator-review.tar.gz'
        listing = await archive_contents(stored)                                                       # preview keys on the stored name
        assert 'index.html' in json.dumps(listing if isinstance(listing, dict) else json.loads(listing.body))
        assert (await store.get(build['id']))['presentation']['project_name'] == 'Tip calculator'
        accepted = (await coder_promotion.accept(build['project_id'], 'a' * 40))['artifact']
        assert accepted['metadata']['download_name'] == accepted['title'] == 'tip-calculator.tar.gz'
        assert accepted['filename'].startswith('accepted-') and (await download_artifact_ep(accepted['id'])).filename == 'tip-calculator.tar.gz'
        # A follow-up keeps the project's name whatever it asks for or renames, and gets a version.
        edit = await controller.create('conversation', 'Add a Reset button that clears the inputs and result.', mode='edit_project', model='coder:local', key='edit')
        second = await deliver(tmp_path, edit, 'b' * 40, {**files, 'package.json': b'{"name":"renamed-by-the-model"}'})
        assert second['metadata']['download_name'] == 'tip-calculator-v2-review.tar.gz'
        assert second['metadata']['download_base'] == 'tip-calculator' and second['metadata']['download_version'] == 2
        assert (await db.get_coding_project(build['project_id']))['download_slug'] == 'tip-calculator'
    asyncio.run(scenario())


def test_two_projects_with_one_title_keep_distinct_stored_files(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    async def scenario():
        await db.create_conversation('other', model='coder:local')
        files = {'package.json': b'{"name":"tip-calculator"}'}
        one = await controller.create('conversation', 'Create a tip calculator', model='coder:local', key='one')
        two = await controller.create('other', 'Create a tip calculator', model='coder:local', key='two')
        a = await deliver(tmp_path, one, 'a' * 40, files); b = await deliver(tmp_path, two, 'c' * 40, files)
        assert a['metadata']['download_name'] == b['metadata']['download_name'] == 'tip-calculator-review.tar.gz'
        assert a['id'] != b['id'] and a['url'] != b['url'] and a['storage_path'] != b['storage_path'] and a['filename'] != b['filename']
    asyncio.run(scenario())


def test_a_naming_failure_never_costs_a_delivery(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    import db.coder_download_names as names
    async def broken(*args): raise RuntimeError('database is locked')
    monkeypatch.setattr(names, 'assign', broken)
    async def scenario():
        build = await controller.create('conversation', 'Create a tip calculator', model='coder:local', key='build')
        artifact = await deliver(tmp_path, build, 'a' * 40, {'app.py': b'print(1)\n'})
        assert 'download_name' not in artifact['metadata'] and artifact['title'] == artifact['filename']
        from routes.artifacts import download_artifact_ep
        assert (await download_artifact_ep(artifact['id'])).filename == artifact['filename']
    asyncio.run(scenario())


def test_card_title_before_any_delivery_and_for_old_jobs():
    assert presentation({'state': 'coding', 'engine': 'openhands159', 'user_task': 'Make me a C tool called lines.'})['project_name'] == 'Lines'
    assert presentation({'state': 'coding', 'engine': 'openhands159'})['project_name'] == 'Daedalus project'
