"""Register the pinned SDK browser tool only inside a v3 UI operation."""
import json
import time
import copy
from openhands.sdk import Action,Observation,ToolDefinition,register_tool,Tool
from openhands.sdk.tool import ToolExecutor
from pydantic import Field, field_validator
from coder_browser_schema import STEPS_SCHEMA


class BrowserAction(Action):
    server_command: str = Field(default="",description="Optional preview command with {port}. Omit to use the discovered project preview.")
    cwd: str = Field(default=".",description="Project-relative package directory")
    path: str = Field(default="/",description="Project-relative URL path")
    steps: list[dict] = Field(default_factory=list,description="Ordered click/fill/select/press/check/scroll/reload/exists/visible/text actions, with selector (CSS or {role,name} or {label}) and optional value. exists checks DOM presence; value:false asserts absence. visible with value:false asserts hidden. text with value:'' asserts empty text; exact:true requires a full match, otherwise nonempty text matches a substring.")


class BrowserObservation(Observation):
    diagnostic: dict = Field(description="DOM, action timeline, errors, screenshot/trace references. Diagnostic evidence, not final Acceptance.")


class BrowserActionV4(BrowserAction):
    steps: list[dict] = Field(default_factory=list,json_schema_extra=STEPS_SCHEMA,
        description="Ordered actions. Exact text includes adjacent control labels; prefer substring or a text-only selector. Normal actionability is required.")

    @field_validator('steps')
    @classmethod
    def validate_actions(cls,value):
        from coder_browser_schema import validate_steps
        validate_steps(value)
        return value

    @classmethod
    def to_mcp_schema(cls):
        # SDK 1.11 builds MCP fields from annotations and discards Field's
        # json_schema_extra. Preserve the same schema at that export boundary.
        schema=super().to_mcp_schema()
        schema['properties']['steps'].update(copy.deepcopy(STEPS_SCHEMA))
        return schema


class DaedalusBrowserTool(ToolDefinition):
    """Module-level type so the SDK can reload persisted conversation events."""
    @classmethod
    def create(cls,conv_state,executor):
        return [cls(description="Test the current web app in headless Chromium. Runs fresh desktop and mobile sessions, executes interactions, and returns accessible DOM, errors and evidence references. Supply the full action sequence on each call. Diagnostic only; final checks run on an immutable revision.",
            action_type=BrowserAction,observation_type=BrowserObservation,executor=executor)]


class DaedalusBrowserToolV4(ToolDefinition):
    """A new SDK type preserves policy-3 serialized actions and conversations."""
    @classmethod
    def create(cls,conv_state,executor):
        from coder_browser_schema import INSTRUCTION
        return [cls(description="Inspect the web app in headless Chromium. Supply a complete action sequence. Returns DOM and evidence references. Diagnostic only. "+INSTRUCTION,
            action_type=BrowserActionV4,observation_type=BrowserObservation,executor=executor)]


def install(agent,store,operation_id,repository):
    from coder_browser import browser_check
    from coder_repository import safe_relative
    from coder_evidence import attach
    from coder_worker_runtime import _boundary

    class BrowserExecutor(ToolExecutor):
        def __call__(self,action,conversation=None):
            operation=_boundary(store,operation_id); payload=operation["payload"]
            directory=store.root/"diagnostics"/operation["job_id"]/str(time.time_ns())
            remaining=payload["seconds_remaining"]-(time.time()-operation["started"])
            try:
                check=action.model_dump()
                if not check['server_command']:
                    repository.refresh()
                    if payload.get('policy_version', 1) >= 6:
                        from coder_profiles import discover
                        discovery = discover(repository, payload.get('execution_commands'), payload.get('proposed_commands'))
                    else:
                        from coder_checks import discover
                        discovery = discover(repository)
                    preview=next((c for c in discovery['checks'] if c.get('kind')=='browser' and c.get('cwd','.')==action.cwd),None)
                    if not preview: raise ValueError('No preview discovered. Supply a project preview command with {port}.')
                    check['server_command']=preview['server_command']
                result=browser_check(safe_relative(repository.root,action.cwd),check,directory,
                    min(remaining,payload["settings"]["daedalus_command_seconds"]),
                    policy_version=payload.get('policy_version',1),
                    startup_timeout=payload['settings'].get('daedalus_browser_startup_seconds'),
                    step_timeout=payload["settings"]["daedalus_browser_step_seconds"],viewports=payload["settings"]["daedalus_browser_viewports"],
                    emit=lambda item:store.event(operation_id,"browser_action",action=item,diagnostic=True))
            except (ValueError,FileNotFoundError,NotADirectoryError) as error:
                result={"passed":False,"error":str(error),"instruction":"Correct the browser arguments and retry; no browser verification was completed."}
            attach(store,operation["job_id"],result)
            store.event(operation_id,"browser_diagnostic",result=result)
            # Text-only by construction: never insert images into SDK context.
            from context_policy import estimate_tokens,resolve
            budget=resolve("builder",payload["settings"]).input_budget//3
            preview=json.loads(json.dumps(result))
            for observation in preview.get("observations",[]):
                if estimate_tokens(preview)>budget:
                    observation["dom"]=observation["dom"][:max(0,budget//max(1,len(preview["observations"])))]
                    observation["dom_truncated"]=True
            return BrowserObservation(diagnostic=preview)

    tool_type=DaedalusBrowserToolV4 if store.get(operation_id)['payload'].get('policy_version',1)>=4 else DaedalusBrowserTool
    def factory(conv_state):
        return tool_type.create(conv_state,BrowserExecutor())
    register_tool(tool_type.__name__,factory)
    agent.tools.append(Tool(name=tool_type.__name__))
