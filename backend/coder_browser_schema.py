"""Policy-4 browser contract shared by plans, probes, SDK tools and execution."""
from urllib.parse import urlsplit


SELECTOR = {"anyOf": [
    {"type":"string", "minLength":1},
    {"type":"object", "properties":{"role":{"type":"string","minLength":1}, "name":{"type":"string"}}, "required":["role"], "additionalProperties":False},
    {"type":"object", "properties":{"label":{"type":"string","minLength":1}}, "required":["label"], "additionalProperties":False},
]}
ACTION_VALUES = {"click":None, "fill":"string", "select":"string", "press":"string",
    "check":"boolean", "scroll":None, "reload":None, "exists":"boolean", "visible":"boolean", "text":"string"}


def _step_schema(action, value_type):
    properties = {"action":{"const":action}}
    required = ["action"]
    if action != "reload":
        properties["selector"] = SELECTOR
        required.append("selector")
    if value_type:
        properties["value"] = {"type":value_type}
        if value_type == "string": required.append("value")
    if action == "text": properties["exact"] = {"type":"boolean"}
    return {"type":"object", "properties":properties, "required":required, "additionalProperties":False}


STEPS_SCHEMA = {"type":"array", "items":{"oneOf":[_step_schema(a,t) for a,t in ACTION_VALUES.items()]}}


def validate_steps(steps):
    if not isinstance(steps,list): raise ValueError("steps: expected an array")
    for index,step in enumerate(steps):
        prefix = f"steps[{index}]"
        if not isinstance(step,dict): raise ValueError(f"{prefix}: expected an object")
        action = step.get("action")
        if not isinstance(action,str) or action not in ACTION_VALUES:
            raise ValueError(f"{prefix}.action: unsupported {action!r}; choose {', '.join(ACTION_VALUES)}")
        schema = _step_schema(action,ACTION_VALUES[action])
        extra = set(step)-set(schema["properties"])
        missing = set(schema["required"])-set(step)
        if extra: raise ValueError(f"{prefix}.{sorted(extra)[0]}: unsupported field for {action}")
        if missing: raise ValueError(f"{prefix}.{sorted(missing)[0]}: required for {action}")
        if action != "reload":
            selector = step["selector"]
            valid = isinstance(selector,str) and bool(selector.strip())
            if isinstance(selector,dict):
                valid = (set(selector) <= {"role","name"} and isinstance(selector.get("role"),str) and bool(selector["role"].strip())
                    and ("name" not in selector or isinstance(selector["name"],str))) or (
                    set(selector)=={"label"} and isinstance(selector["label"],str) and bool(selector["label"].strip()))
            if not valid: raise ValueError(f"{prefix}.selector: use nonempty CSS, {{role,name?}}, or {{label}}")
        for field in ("value","exact"):
            if field not in step: continue
            kind = schema["properties"][field]["type"]
            if type(step[field]) is not (bool if kind=="boolean" else str):
                raise ValueError(f"{prefix}.{field}: expected {kind} for {action}")


def validate_flow(flow):
    validate_steps(flow.get("steps",[]))
    path = flow.get("path","/")
    if not isinstance(path,str) or "\\" in path or urlsplit(path).scheme or urlsplit(path).netloc:
        raise ValueError("path: expected a project-relative URL")


INSTRUCTION = """Browser actions must follow the supplied strict schema. No evaluate, force, or arbitrary page scripts.
visible/exists/check take optional BOOLEAN value; text/fill/select/press require STRING value.
text defaults to substring matching. When a row contains adjacent controls (such as Delete),
assert the entered text with a substring or target its text-only child; an exact match on the whole row includes control labels.
Keep ordinary clicks and visible, usable controls. Do not hide controls to satisfy a text assertion."""
