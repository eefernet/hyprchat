"""Public failure categories; detail remains in the original evidence."""

def malformed_native_call(error):
    text=str(error).lower()
    return 'ollama' in text and any(marker in text for marker in ('xml syntax error','failed to parse tool call','error parsing tool call'))


def classify_failure(error, stage, *, recovery_attempted=False):
    text=str(error).lower()
    category,action='operation_failed','Inspect the recorded evidence before continuing.'
    if malformed_native_call(error) or 'multiple tool calls' in text:
        category,action='tool_protocol','The model returned an invalid tool call. Continue with another local model or inspect tool-protocol evidence.'
    elif 'completion allowance' in text or 'output limit' in text:
        category,action='output_limit','Increase this stage’s output allowance in Settings or narrow the request, then continue.'
    elif 'model-call allowance' in text:
        category,action='call_limit','Review progress and continue from the checkpoint with a new allowance.'
    elif 'execution allowance' in text or 'time allowance' in text:
        category,action='time_limit','Review progress and continue from the checkpoint with a new allowance.'
    elif 'preview' in text:
        category,action='preview_startup','Inspect the preview command and server log; correct startup before retrying browser checks.'
    elif any(s in text for s in ('environment','name resolution','enotfound','missing dependencies',"executable doesn't exist")):
        category,action='environment','Restore dependencies or the unavailable service, then continue; changing application code will not fix this failure.'
    elif 'response-format recovery exhausted' in text:
        category,action='response_format','Inspect the rejected responses and format attempts. Select a different local model for a new job; Continue retains exhausted step limits.'
    elif 'invalid raw probe' in text or 'probe contains unsupported' in text:
        category,action='invalid_probe','Inspect the probe and its source bindings before retrying verification.'
    elif stage in {'plan','planning','verify','verifying','accept','accepting'} and any(s in text for s in ('invalid','validation','requirement','probe','schema')):
        category,action='verification_response','The model could not produce valid verification evidence. Inspect the failed requirement and try another local model.'
    elif 'fails after' in text or 'test failed' in text:
        category,action='application_check','Inspect the failing application checks and repair the implementation.'
    return {'category':category,'stage':stage,'recovery_attempted':recovery_attempted,'next_action':action}
