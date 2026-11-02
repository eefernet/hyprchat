"""SDK capability for scoped controller preparation, never privileged shell access."""
import json
from pydantic import Field
import requests
from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import ToolExecutor, register_tool


class PrepareAction(Action):
    manifests: list[str] = Field(default_factory=list, description='Existing project-relative dependency manifests or lockfiles; empty discovers all packages.')


class PrepareObservation(Observation):
    pass


class PrepareExecutor(ToolExecutor):
    def __init__(self, url, timeout): self.url, self.timeout = url, timeout
    def __call__(self, action, conversation=None):
        try:
            with requests.Session() as client:
                client.trust_env = True  # the sandbox capability proxy is the only route to the controller
                response = client.post(self.url + '/daedalus/environment', json={'manifests': action.manifests}, timeout=self.timeout)
            result = response.json()
            return PrepareObservation(content=[TextContent(text=json.dumps(result))], is_error=response.status_code != 200 or not result.get('passed'))
        except Exception as error:
            return PrepareObservation(content=[TextContent(text='Environment preparation unavailable: '+str(error))], is_error=True)


class PrepareEnvironmentTool(ToolDefinition[PrepareAction, PrepareObservation]):
    @classmethod
    def create(cls, conv_state, url, timeout=610):
        return [cls(action_type=PrepareAction, observation_type=PrepareObservation, executor=PrepareExecutor(url,timeout),
            description='Prepare declared project dependencies and matching browser binaries after changing a dependency manifest. '
            'Returns reproducible commands and environment paths. Never install system packages or write into managed cache targets.')]


register_tool(PrepareEnvironmentTool.name, PrepareEnvironmentTool)
