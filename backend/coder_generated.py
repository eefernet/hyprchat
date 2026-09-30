"""Recognize generated .NET directories without excluding authored bin trees."""
import os
from pathlib import Path


def directories(root):
    root = Path(root)
    found = set()
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {'.git', '.venv', 'venv', 'node_modules', 'target', 'build', '.cache'}]
        here = Path(directory)
        if not any(name.endswith(('.csproj', '.fsproj', '.vbproj')) for name in files):
            continue
        for name, patterns in [('obj', ('project.assets.json', '**/*.AssemblyInfo.cs')), ('bin', ('**/*.deps.json', '**/*.runtimeconfig.json'))]:
            path = here / name
            if path.is_dir() and any(next(path.glob(pattern), None) for pattern in patterns):
                found.add(path.relative_to(root).as_posix())
                if name in dirs: dirs.remove(name)
    return found
