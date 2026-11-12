"""Controller-only filesystem grants shared by Docker and bubblewrap."""
from pathlib import Path
import json


class MountPolicyError(ValueError):
    pass


def private_paths(environment):
    value = json.loads(environment.get('DAEDALUS_PRIVATE_DEPENDENCIES', '[]'))
    base = Path(environment.get('DAEDALUS_ENVIRONMENT_ROOT', '/nonexistent')).resolve()
    result = []
    for name in value:
        path = Path(name).absolute()
        if not path.resolve().is_relative_to(base) or path == base:
            raise MountPolicyError('Private dependency grant escapes its project environment')
        result.append(path)
    return result


def mounts(readonly, writable, *, published=()):
    """Exact writable grants override source defaults, never published entries.

    A read-only child beneath an intended writable target is rejected rather
    than silently producing a different policy in Docker and bubblewrap.
    """
    writes = {Path(p).absolute() for p in writable}
    protected = {Path(p).resolve() for p in published}
    reads = {Path(p).absolute() for p in readonly} - writes
    for write in writes:
        if any(write.resolve() == p or write.resolve().is_relative_to(p) for p in protected):
            raise MountPolicyError('A published dependency cannot receive a writable grant')
        for read in reads:
            if read.is_relative_to(write):
                raise MountPolicyError('Read-only child conflicts with writable target: ' + str(read))
    grants = [{'source': str(p), 'target': str(p), 'writable': p in writes}
              for p in reads | writes]
    return sorted(grants, key=lambda row: (len(Path(row['target']).parts), row['target']))


def runtime_environment(root):
    """Writable per-operation tool caches, never dependencies or source evidence."""
    root = Path(root)
    paths = {name: root / name for name in ('pip', 'npm', 'yarn', 'pnpm', 'composer', 'bundle',
                                           'maven', 'gradle', 'go', 'gocache', 'cargo', 'nuget', 'dotnet')}
    for path in paths.values(): path.mkdir(parents=True, exist_ok=True)
    return {'PIP_CACHE_DIR': str(paths['pip']), 'npm_config_cache': str(paths['npm']),
            'YARN_CACHE_FOLDER': str(paths['yarn']), 'PNPM_HOME': str(paths['pnpm']), 'npm_config_store_dir': str(paths['pnpm'] / 'store'),
            'COMPOSER_CACHE_DIR': str(paths['composer']), 'BUNDLE_USER_CACHE': str(paths['bundle']),
            'BUNDLE_PATH': str(root.parent / 'bundle'),
            'MAVEN_OPTS': '-Dmaven.repo.local=' + str(paths['maven']),
            'GRADLE_USER_HOME': str(paths['gradle']), 'GOPATH': str(paths['go']),
            'GOMODCACHE': str(paths['go'] / 'pkg/mod'), 'GOCACHE': str(paths['gocache']),
            'CARGO_HOME': str(paths['cargo']), 'NUGET_PACKAGES': str(paths['nuget']),
            'DOTNET_CLI_HOME': str(paths['dotnet'])}
