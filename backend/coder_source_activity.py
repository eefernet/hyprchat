"""Observe actual workspace changes around SDK tools, excluding dependencies."""
import hashlib
import json
import os
from pathlib import Path
from coder_progress import EXCLUDED, relative


def inventory(root, excludes=()):
    root = Path(root)
    from coder_generated import directories, excluded
    generated = directories(root)
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED | set(excludes) and not excluded((Path(directory)/name).relative_to(root), generated) and not (Path(directory)/name).is_symlink() and not name.startswith('.daedalus-'))
        for name in sorted(files):
            path = Path(directory)/name
            label = relative(str(path.relative_to(root)))
            if not label or name.startswith('.daedalus-publish-') or path.is_symlink() or not path.is_file(): continue
            try:
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
                result[label] = digest.hexdigest()
            except (FileNotFoundError, IsADirectoryError): continue
    return result


class Observer:
    def __init__(self, root, state, emit, excludes=()):
        self.root, self.state, self.emit, self.excludes = root, Path(state), emit, excludes
        self.before = inventory(root, excludes)
        self.identity = ''

    def event(self, event):
        tool = event.get('tool_name', '')
        if tool not in {'terminal', 'file_editor'}: return
        if event.get('kind') == 'ActionEvent':
            if not (tool == 'terminal' and not (event.get('action') or {}).get('command') and self.identity):
                self.before = inventory(self.root, self.excludes)
                self.identity = event['id']
            self.state.write_text(json.dumps({'identity': self.identity, 'before': self.before}))
        elif event.get('kind') in {'ObservationEvent','AgentErrorEvent','UserRejectObservation'}:
            observation = event.get('observation') or {}
            after = inventory(self.root, self.excludes)
            outcome = 'failed' if observation.get('is_error') or event['kind'] != 'ObservationEvent' else 'succeeded'
            self.record(after, outcome, event['id'])
            if not (tool == 'terminal' and observation.get('exit_code') in {None,-1} and not observation.get('is_error')):
                self.identity = ''
            self.state.write_text(json.dumps({'identity': self.identity, 'before': self.before}))

    def record(self, after, outcome, identity):
        for path in sorted(set(self.before) | set(after)):
            if self.before.get(path) == after.get(path): continue
            action = 'create' if path not in self.before else 'delete' if path not in after else 'edit'
            self.emit(id=hashlib.sha256((identity + ':' + path).encode()).hexdigest(), path=path, action=action, outcome=outcome)
        self.before = after


def finish_interrupted(root, state, emit, excludes=()):
    state = Path(state)
    if not state.is_file(): return
    saved = json.loads(state.read_text())
    if not saved.get('identity'): return
    observer = Observer(root, state, emit, excludes)
    observer.before = saved['before']
    observer.record(inventory(root, excludes), 'interrupted', saved['identity'] + ':interrupted')
    state.write_text(json.dumps({'identity': '', 'before': observer.before}))
