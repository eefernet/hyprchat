"""Corroborated generated directories shared by inventory, snapshots and activity."""
import os
from pathlib import Path


class ClassificationConflict(ValueError):
    pass


def pip_target(path):
    """A pip --target tree needs installed-distribution metadata, not a prefix."""
    records = list(path.glob('*.dist-info/RECORD'))
    return bool(records) and any((record.parent / 'METADATA').is_file() and
                                (record.parent / 'INSTALLER').is_file() for record in records)


def directories(root, protected=()):
    root = Path(root)
    found = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = Path(directory)
        relative = here.relative_to(root).as_posix()
        environment = ('pyvenv.cfg' in files and (here / 'bin').is_dir()) or pip_target(here)
        if here != root and environment:
            found.add(relative); dirs[:] = []; continue
        for name in list(dirs):
            path = here / name
            if path.is_symlink() or name == '.git':
                dirs.remove(name); continue
            generated = name in {'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.pnpm-store', '.yarn-cache', '.daedalus-original-venv'}
            generated |= name == '.gradle' and any((here / p).exists() for p in ('build.gradle', 'build.gradle.kts', 'settings.gradle'))
            generated |= name == 'target' and any((here / p).exists() for p in ('Cargo.toml', 'pom.xml'))
            generated |= name == 'vendor' and any((here / p).exists() for p in ('composer.json', 'Gemfile'))
            generated |= name in {'build', 'CMakeFiles', 'Testing'} and ((here / 'CMakeLists.txt').exists() or (path / 'CMakeCache.txt').exists())
            generated |= name.endswith('.egg-info') and (path / 'PKG-INFO').is_file() and (path / 'SOURCES.txt').is_file() and any((here / p).is_file() for p in ('pyproject.toml', 'setup.py'))
            generated |= name == 'build' and any((here / p).is_file() for p in ('build.gradle', 'build.gradle.kts'))
            if name in {'obj', 'bin'} and any(f.endswith(('.csproj', '.fsproj', '.vbproj')) for f in files):
                patterns = ('project.assets.json', '**/*.AssemblyInfo.cs') if name == 'obj' else ('**/*.deps.json', '**/*.runtimeconfig.json')
                generated |= any(next(path.glob(pattern), None) for pattern in patterns)
            if generated:
                found.add(path.relative_to(root).as_posix()); dirs.remove(name)
    for name in protected:
        if any(Path(name).is_relative_to(Path(folder)) for folder in found):
            raise ClassificationConflict('Generated-directory classification conflicts with authored or protected source: ' + name)
    return found


def excluded(path, generated):
    return any(Path(path) == Path(folder) or Path(path).is_relative_to(Path(folder)) for folder in generated)
