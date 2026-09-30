"""Run delivered project goldens inside the shared project-code boundary."""
import os
from pathlib import Path
import subprocess
import sys
from coder_sandbox import Sandbox


def run(script, arguments, folder, archive, timeout=1500):
    root = Path(__file__).resolve().parents[1]
    modules = [*root.glob('*.py'), *Path(__file__).parent.glob('*.py')]
    command = [sys.executable, str(script), *arguments]
    with Sandbox(command, cwd=folder, writable=[folder], readonly=[archive, *modules],
                 environment={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(root)}) as box:
        return subprocess.run(box.args, env=box.env, capture_output=True, text=True, timeout=timeout, start_new_session=True)
