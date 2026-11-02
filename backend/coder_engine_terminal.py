"""Public SDK terminal adapter with a deadline independent of SDK polling.

The persistent terminal lives in its own process. A watchdog owns that process
and all descendants; SDK yield/poll calls cannot renew the command allowance.
"""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import threading
import time

from openhands.sdk.tool import ToolExecutor


def _terminal_child(pipe, working_dir, output_directory):
    from openhands.tools.terminal import TerminalExecutor
    from openhands.tools.terminal.definition import TerminalAction
    executor = TerminalExecutor(working_dir=working_dir, terminal_type='subprocess',
                               full_output_save_dir=output_directory)
    try:
        pipe.send({'ready': True})
        while True:
            data = pipe.recv()
            if data is None:
                return
            try:
                result = executor(TerminalAction.model_validate(data))
                pipe.send({'observation': result.model_dump(mode='json')})
            except Exception as error:
                pipe.send({'error': str(error)})
    except EOFError:
        pass
    finally:
        executor.close(); pipe.close()


def terminate_tree(process):
    """Only the dedicated terminal child and its descendants, never sibling UI."""
    import psutil
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try: child.kill()
        except psutil.NoSuchProcess: pass
    if process.is_alive(): process.kill()
    process.join(timeout=3)
    if children: psutil.wait_procs(children, timeout=3)


class TerminalLease(ToolExecutor):
    def __init__(self, working_dir, output_directory, seconds):
        self.working_dir, self.directory, self.seconds = str(working_dir), str(output_directory), seconds
        self.process = self.pipe = None
        self.deadline = None
        self.expired = False
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.watchdog = threading.Thread(target=self._watch, daemon=True)
        self.watchdog.start()

    def _save(self, state):
        path = Path(self.directory); path.mkdir(parents=True, exist_ok=True)
        (path / 'terminal-lease.json').write_text(json.dumps({'state': state, 'deadline': self.deadline,
            'command_seconds': self.seconds, 'updated': time.time()}))

    def _watch(self):
        while not self.closed.wait(.05):
            with self.lock:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.expired = True
                    self.deadline = None
                    if self.process: terminate_tree(self.process)
                    self._save('timed_out')

    def _timeout(self, command):
        from openhands.tools.terminal.definition import TerminalObservation
        self.expired = False
        return TerminalObservation.from_text(
            f'Command exceeded {self.seconds} seconds. Its terminal and descendants were stopped. '
            'The terminal session was reset; any newly submitted command was not executed. '
            'Use the managed UI tool for application sessions.',
            command=command, exit_code=124, is_error=True)

    def __call__(self, action, conversation=None):
        from openhands.tools.terminal.definition import TerminalObservation
        with self.lock:
            if self.expired:
                return self._timeout(action.command)
            if self.process is None or not self.process.is_alive():
                if self.pipe: self.pipe.close()
                context = multiprocessing.get_context('spawn')
                self.pipe, child = context.Pipe()
                self.process = context.Process(target=_terminal_child,
                    args=(child, self.working_dir, self.directory), daemon=True)
                self.process.start(); child.close()
                if not self.pipe.poll(30) or self.pipe.recv() != {'ready': True}:
                    terminate_tree(self.process)
                    raise RuntimeError('Terminal initialization failed before command dispatch')
            if self.deadline is None:
                self.deadline = time.monotonic() + self.seconds
                self._save('running')
            self.pipe.send(action.model_dump(mode='json'))
        while not self.closed.is_set():
            if self.pipe.poll(.05):
                try: result = self.pipe.recv()
                except (EOFError, OSError): result = None
                with self.lock:
                    if self.expired: return self._timeout(action.command)
                    if result is None:
                        self.deadline = None
                        raise RuntimeError('Terminal process exited without a result; command was not replayed')
                    if 'error' in result:
                        self.deadline = None
                        raise RuntimeError(result['error'])
                    observation = TerminalObservation.model_validate(result['observation'])
                    if observation.exit_code not in (None, -1):
                        self.deadline = None
                        self._save('finished')
                    return observation
            with self.lock:
                if self.expired: return self._timeout(action.command)
        raise InterruptedError('Terminal closed')

    def close(self):
        self.closed.set()
        with self.lock:
            if self.process: terminate_tree(self.process)
            if self.pipe: self.pipe.close()
            self.deadline = None
        self.watchdog.join(timeout=4)
