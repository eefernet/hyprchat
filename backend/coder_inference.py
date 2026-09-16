"""Local inference boundary shared by coding, inspection, and compaction."""
from __future__ import annotations

import json
import time

from context_policy import estimate_tokens, resolve


def require_local_model(url, model, timeout=15):
    """Reject cloud aliases before a tool-support probe or inference request."""
    import requests
    if not model or model.startswith(("openai:", "anthropic:", "custom:")) or model.endswith(("-cloud", ":cloud")):
        raise ValueError("Select an installed local coding model in Settings")
    response = requests.post(url.rstrip("/") + "/api/show", json={"model":model}, timeout=timeout)
    response.raise_for_status()
    details = response.json()
    if details.get("remote_host") or details.get("remote_model"):
        raise ValueError("This Ollama model uses remote inference; select a local model in Settings")
    if details.get("error"):
        raise ValueError(details["error"])


def ensure_context(url, model, policy, timeout, requests_client=None):
    import requests
    requests = requests_client or requests
    names = {model, model + ':latest'}
    def loaded():
        response = requests.get(url + '/api/ps', timeout=min(15, timeout))
        response.raise_for_status()
        return next((m for m in response.json().get('models', []) if m.get('name') in names or m.get('model') in names), None)
    current = loaded()
    if current and current.get('context_length') == policy.num_ctx:
        return
    if current:
        response = requests.post(url + '/api/generate', json={'model':model,'keep_alive':0}, timeout=timeout)
        response.raise_for_status()
    response = requests.post(url + '/api/generate', json={'model':model,'prompt':'','stream':False,
        'options':{'num_ctx':policy.num_ctx},'keep_alive':'10m'}, timeout=timeout)
    response.raise_for_status()
    if response.json().get('error'):
        raise ValueError(response.json()['error'])
    current = loaded()
    if not current or current.get('context_length') != policy.num_ctx:
        raise ValueError(f'Ollama did not allocate the configured {policy.num_ctx} context. Adjust Settings or runtime memory.')


def local_chat(store, operation_id, role, messages, *, temperature=0.2):
    """Stream internally so cancellation closes inference, and count real calls."""
    import requests
    from coder_worker_runtime import _boundary
    operation = _boundary(store, operation_id)
    payload = operation['payload']
    policy = resolve(role, payload['settings'])
    estimate = estimate_tokens({'messages':messages})
    if estimate > policy.input_budget:
        raise ValueError(f'{role} input exceeds the configured context. Narrow the source range or increase context in Settings.')
    url = payload['ollama_url'].rstrip('/')
    remaining = payload['seconds_remaining'] - (time.time() - operation['started'])
    ensure_context(url, payload['model'], policy, remaining)
    _boundary(store, operation_id, model_call=True)
    store.event(operation_id, 'model_call', context=policy.as_dict(), prompt_tokens_estimate=estimate, model=payload['model'])
    content, final = [], None
    with requests.post(url + '/api/chat', json={'model':payload['model'],'messages':messages,'stream':True,
            'options':{'num_ctx':policy.num_ctx,'num_predict':policy.num_predict,'temperature':temperature}},
            timeout=remaining, stream=True) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            _boundary(store, operation_id)
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get('error'):
                raise ValueError(chunk['error'])
            content.append(chunk.get('message', {}).get('content', ''))
            if chunk.get('done'):
                final = chunk
    if final is None:
        raise RuntimeError('Inference stream ended without a completion; response was not accepted')
    store.event(operation_id, 'usage', usage={k:final.get(k) for k in ('prompt_eval_count','eval_count')}, context=policy.as_dict())
    if (final.get('prompt_eval_count') or 0) > policy.input_budget:
        raise ValueError('Actual prompt usage exceeded the configured input budget; narrow evidence or increase context in Settings')
    if final.get('done_reason') == 'length':
        raise ValueError('Response reached the configured completion allowance; increase it in Settings or narrow the task')
    return ''.join(content)


def summarize_history(store, operation_id, history):
    """Fold complete persisted history into a checkpoint without losing source."""
    serialized = json.dumps(history, ensure_ascii=False)
    summary, offset = '', 0
    while offset < len(serialized):
        policy = resolve('compaction', store.get(operation_id)['payload']['settings'])
        instruction = ('Summarize execution evidence: requirements, decisions, source paths and hashes, checks, failed approaches, remaining work. '
                       'Preserve uncertainties. Do not invent completed work.\n')
        available = policy.input_budget - estimate_tokens(instruction + summary) - estimate_tokens({'messages':[{'role':'user','content':''}]})
        if available <= 0:
            raise ValueError('Compaction checkpoint exceeds configured context')
        # Fill the configured budget, validating the complete UTF-8 envelope.
        end = min(len(serialized), offset + available * 3)
        messages = [{'role':'user','content':instruction + summary + '\n' + serialized[offset:end]}]
        while end > offset and estimate_tokens({'messages':messages}) > policy.input_budget:
            end = offset + (end-offset)//2
            messages[0]['content'] = instruction + summary + '\n' + serialized[offset:end]
        if end <= offset:
            raise ValueError('Compaction input cannot fit the configured context')
        summary = local_chat(store,operation_id,'compaction',messages,temperature=0.1).strip()
        if not summary:
            raise ValueError('Compaction returned no checkpoint; history was preserved')
        offset = end
    store.event(operation_id,'compaction',context=policy.as_dict(),history_bytes=len(serialized.encode()),summary=summary)
    return summary
