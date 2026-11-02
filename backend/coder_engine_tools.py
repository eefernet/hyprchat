"""Supported SDK tool factories with bounded observations for local models."""
import json
from pathlib import Path
import time

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import ToolExecutor, register_tool
from openhands.tools.terminal import TerminalTool, TerminalExecutor
from openhands.tools.file_editor import FileEditorTool


class BoundedExecutor(ToolExecutor):
    def __init__(self, delegate, directory, max_chars):
        self.delegate, self.directory, self.max_chars = delegate, Path(directory), max_chars

    def __call__(self, action, conversation=None):
        observation = self.delegate(action, conversation=conversation)
        content = observation.content
        encoded = json.dumps([c.model_dump(mode='json') for c in content], ensure_ascii=False)
        if len(encoded) <= self.max_chars:
            return observation
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f'output-{time.time_ns()}.json'
        path.write_text(encoded)
        # JSON-safe character bounds also handle binary/control-character output.
        excerpt = encoded[:self.max_chars // 2] + '\n…\n' + encoded[-self.max_chars // 2:]
        text = f'Output shortened. Full output: {path}. Read only the needed range.\n{excerpt}'
        return observation.model_copy(update={'content': [TextContent(text=text)]})

    def close(self):
        close = getattr(self.delegate, 'close', None)
        if close:
            close()


class EngineToolSet(ToolDefinition[Action, Observation]):
    @classmethod
    def create(cls, conv_state, output_directory, max_chars=16000, command_seconds=600):
        from coder_engine_terminal import TerminalLease
        terminal = TerminalLease(conv_state.workspace.working_dir, output_directory, command_seconds)
        tools = [*TerminalTool.create(conv_state, executor=terminal), *FileEditorTool.create(conv_state)]
        return [tool.model_copy(update={'executor': BoundedExecutor(tool.executor, output_directory, max_chars)})
                for tool in tools]


register_tool(EngineToolSet.name, EngineToolSet)
