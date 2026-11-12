"""Public SDK tool for browser and Linux accessibility interactions."""
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Literal

import requests
from pydantic import Field
from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import ToolExecutor, register_tool


class UIAction(Action):
    action: Literal['launch', 'observe', 'click', 'fill', 'text', 'value', 'press', 'reload', 'screenshot', 'close', 'check_flows']
    application_type: Literal['web', 'desktop'] = 'web'
    command: str = Field(default='', description='Launch command in the project; use the application\'s real entrypoint.')
    url: str = 'http://127.0.0.1:8000'
    selector: str = Field(default='', description='CSS selector for web; desktop node id is valid only with its observed session_id. Never persist desktop ids in UI flows.')
    session_id: str = Field(default='', description='Current desktop session identity from launch/observe; required with a raw desktop selector.')
    name: str = Field(default='', description='Exact observed accessible name, not a Qt objectName or Python variable name.')
    role: str = Field(default='', description='Exact observed accessibility role, not a widget class such as lineedit. Omit when name plus action capability is unique.')
    value: str = ''
    toolkit: str = Field(default='', description='For Java Swing use swing to enable its AT-SPI bridge.')


class UIObservation(Observation):
    """Concrete SDK discriminator required when persisted conversations reload."""


class UIExecutor(ToolExecutor):
    def __init__(self, directory, preflight=None):
        self.directory = Path(directory) / 'ui'
        self.process = None
        self.url = ''
        self.preflight = preflight or {}

    def close(self):
        if self.process:
            from coder_ui_session import stop_session
            stop_session(self.process)
            self.process = None

    def __call__(self, action, conversation=None):
        try:
            if action.action == 'check_flows':
                if self.preflight.get('version') != 1:
                    raise ValueError('UI flow preflight is available for new modern jobs only.')
                self.close()
                from coder_ui_preflight import run
                result = run(self.preflight['root'], self.directory.parent, self.preflight['operation_id'],
                    commands=self.preflight.get('commands'), timeout=self.preflight['timeout'])
                return UIObservation(content=[TextContent(text=json.dumps(result))], is_error=not result['passed'])
            if action.action == 'close':
                self.close(); result = {'closed': True}
            else:
                if self.process is None or self.process.poll() is not None:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    ready = self.directory / 'ready.json'; ready.unlink(missing_ok=True)
                    with (self.directory/'session.log').open('ab') as log:
                        self.process = subprocess.Popen(['dbus-run-session', '--', sys.executable,
                            str(Path(__file__).with_name('coder_ui_session.py')), '--directory', str(self.directory)],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    deadline = time.monotonic()+15
                    while not ready.exists():
                        if time.monotonic() > deadline or self.process.poll() is not None:
                            raise RuntimeError('Desktop session unavailable: '+(self.directory/'session.log').read_text()[-2500:])
                        time.sleep(.1)
                    self.url = 'http://127.0.0.1:'+str(json.loads(ready.read_text())['port'])
                with requests.Session() as client:
                    client.trust_env = False
                    response = client.post(self.url, json={**action.model_dump(), 'kind': action.application_type}, timeout=45)
                result = response.json()
            return UIObservation(content=[TextContent(text=json.dumps(result))], is_error=bool(result.get('error')))
        except Exception as error:
            return UIObservation(content=[TextContent(text=str(error))], is_error=True)


class UITool(ToolDefinition[UIAction, UIObservation]):
    @classmethod
    def create(cls, conv_state, directory, preflight=None):
        return [cls(action_type=UIAction, observation_type=UIObservation, executor=UIExecutor(directory, preflight),
            description='Launch and test the actual application in an isolated Linux GUI. Web uses Playwright; '
            'desktop uses AT-SPI accessible controls. Call launch with a command, then observe, click, fill, text (displayed text), and value (editable input value) '
            'to verify requested behavior. Use close before relaunching. GUI output is diagnostic; independent '
            'verification decides the delivered status. Use check_flows after writing or editing .daedalus-ui.json: '
            'it validates every flow and replays them with disposable application state. It returns precise failures, '
            'not acceptance. Use value for input assertions and text for result displays. '
            'Headless-only tests do not establish visible UI behavior.')]


register_tool(UITool.name, UITool)
