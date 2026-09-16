import tarfile

import pytest

from coder_repository import Repository


def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root, Repository(root, tmp_path / "state", excludes=["node_modules"])


def test_inventory_pages_all_files_and_preserves_large_sources(tmp_path):
    root, repository = repo(tmp_path)
    for number in range(205):
        (root / f"source{number:04}.py").write_text(f"def function_{number}():\n    return {number}\n")
    large = "# padding\n" * 30000 + "def very_late_symbol():\n    return 'needle-at-end'\n"
    (root / "large.py").write_text(large)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.py").write_text("ignored")
    stats = repository.refresh()
    assert stats["files"] == 206
    paths, cursor = [], ""
    while True:
        page = repository.inventory(cursor=cursor, limit=17)
        paths.extend(row["path"] for row in page["items"])
        if not page["next_cursor"]:
            break
        cursor = page["next_cursor"]
    assert len(paths) == len(set(paths)) == 206
    hit = repository.search("needle-at-end")["items"][0]
    assert hit["line"] == 30002
    assert repository.inventory(symbols=True, query="very_late_symbol")["items"]
    assert "needle-at-end" in repository.read("large.py", start=30002, limit=1)["content"]


def test_changed_and_deleted_sources_invalidate_index(tmp_path):
    root, repository = repo(tmp_path)
    (root / "old.py").write_text("def old_name(): pass\n")
    repository.refresh()
    before = repository.read("old.py")["sha256"]
    (root / "old.py").write_text("def new_name(): pass\n")
    repository.refresh()
    assert not repository.inventory(symbols=True, query="old_name")["items"]
    with pytest.raises(ValueError, match="changed"):
        repository.read("old.py", expected_hash=before)
    (root / "old.py").rename(root / "new.py")
    repository.refresh()
    assert repository.inventory()["items"][0]["path"] == "new.py"


def test_inventory_globs_find_nested_tests_without_losing_pagination(tmp_path):
    root, repository = repo(tmp_path)
    for name in ('api.py', 'test_root.py', 'tests/test_api.py', 'tests/test_api.py.bak', 'tests/testXapi.py', '100%.txt'):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('def test_lookup(): pass\n')
    repository.refresh()
    first = repository.inventory(query='test_*.py', limit=1)
    second = repository.inventory(query='test_*.py', cursor=first['next_cursor'], limit=1)
    assert [first['items'][0]['path'], second['items'][0]['path']] == ['test_root.py', 'tests/test_api.py']
    assert second['next_cursor'] is None
    assert {row['path'] for row in repository.inventory(query='TEST?API.PY')['items']} == {'tests/test_api.py', 'tests/testXapi.py'}
    assert [row['path'] for row in repository.inventory(query='%')['items']] == ['100%.txt']
    assert {row['path'] for row in repository.inventory(query='test_')['items']} == {'test_root.py', 'tests/test_api.py', 'tests/test_api.py.bak'}
    assert len(repository.inventory(query='*')['items']) == 6
    assert repository.inventory(query='test_*', symbols=True)['items']


def test_snapshot_includes_untracked_files_and_packages_exact_revision(tmp_path):
    root, repository = repo(tmp_path)
    (root / "main.py").write_text("print('accepted')\n")
    snapshot = repository.snapshot()
    (root / "main.py").write_text("print('later edit')\n")
    archive = tmp_path / "artifact.tar.gz"
    metadata = repository.archive(snapshot["revision"], archive)
    with tarfile.open(archive) as bundle:
        assert bundle.extractfile("main.py").read() == b"print('accepted')\n"
    assert metadata["size"] == archive.stat().st_size
    assert not (root / ".git").exists()


def test_empty_checkpoint_is_a_readable_archive(tmp_path):
    _, repository = repo(tmp_path)
    revision = repository.snapshot()["revision"]
    archive = tmp_path / "empty.tar.gz"
    repository.archive(revision, archive)
    with tarfile.open(archive) as bundle:
        assert bundle.getmembers() == []


def test_project_export_attributes_cannot_change_accepted_snapshot(tmp_path):
    root, repository = repo(tmp_path)
    (root / ".gitattributes").write_text("tests/** export-ignore\n*.py export-subst\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("# $Format:%H$\n")
    archive = tmp_path / "exact.tar.gz"
    repository.archive(repository.snapshot()["revision"], archive)
    with tarfile.open(archive) as bundle:
        assert bundle.extractfile("tests/test_app.py").read() == b"# $Format:%H$\n"


def test_project_ignore_rules_and_path_ownership(tmp_path):
    root, repository = repo(tmp_path)
    (root / ".gitignore").write_text("generated/\n")
    (root / "generated").mkdir()
    (root / "generated" / "huge.py").write_text("ignored")
    (root / "main.py").write_text("pass\n")
    repository.refresh()
    assert repository.inventory()["stats"]["files"] == 2
    with pytest.raises(ValueError):
        repository.read("../secret")
    (root / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        repository.read("escape/secret")


@pytest.mark.parametrize('lines',[100000,500000,1000000])
def test_large_repository_indexes_end_of_every_package(tmp_path,lines):
    root,repository=repo(tmp_path)
    count=20
    for number in range(count):
        package=root/f'package{number:03}';package.mkdir()
        (package/'pyproject.toml').write_text('[project]\nname="example"\n')
        (package/'source.py').write_text('# source\n'*(lines//count-2)+f'def last_symbol_{number}():\n    return "needle-{number}"\n')
    inventory=repository.refresh()
    assert inventory['lines']==lines+count*2
    assert len(repository.inventory(symbols=True,query='last_symbol',limit=100)['items'])==count
    assert repository.search('needle-19')['items'][0]['line']==lines//count
    unchanged=repository.refresh()
    assert unchanged['changed']==0


def test_single_line_file_can_be_read_without_losing_its_tail(tmp_path):
    root,repository=repo(tmp_path)
    content='x'*200000+'last-byte'
    (root/'generated.txt').write_text(content)
    offset=0;parts=[]
    while True:
        page=repository.read_bytes('generated.txt',offset=offset,length=8191)
        parts.append(page['content'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert ''.join(parts)==content


def test_new_exclusions_remove_previously_checkpointed_files(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'keep.py').write_text('answer = 42\n')
    (root/'generated').mkdir();(root/'generated'/'cache.py').write_text('cache = 1\n')
    repository=Repository(root,tmp_path/'state')
    before=repository.snapshot()
    repository=Repository(root,tmp_path/'state',excludes=['generated'])
    repository.refresh()
    assert [row['path'] for row in repository.inventory()['items']]==['keep.py']
    after=repository.snapshot(parent=before['revision'])
    assert after['revision']!=before['revision']
    assert repository.git('ls-tree','--name-only','-r',after['revision']).decode().splitlines()==['keep.py']
