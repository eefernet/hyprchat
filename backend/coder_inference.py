"""Local inference boundary shared by coding, inspection, and compaction."""
from __future__ import annotations

import json
import time

from context_policy import estimate_tokens, resolve, operation_settings, thinking_options


class InputBudgetError(ValueError):
    failure_category = "input_budget"


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
    return details


def resident(entry):
    """Whether a loaded model sits wholly in GPU memory (Ollama /api/ps: size_vram == size)."""
    return bool(entry) and (entry.get('size_vram') or 0) >= (entry.get('size') or 0) > 0


SETTLE_SECONDS, CONFIRM_SECONDS = 10, 3   # ensure_context pacing; module-level so tests can shrink the real path


def ensure_context(url, model, policy, timeout, requests_client=None, settle=None, attempts=6, confirm=None):
    """Load ``model`` at the policy's context, wholly on the GPUs.

    Live 2026-10-27: the planning model stayed resident (keep_alive 10m) when the coder loaded at 65,536 context,
    so 1.6 GiB of the coder landed in host memory and every token waited on 64 CPU threads in an 8-core container:
    0.5 tok/s instead of ~140, and a whole job allowance spent on timed-out calls. A model that does not fit beside
    the others now evicts them and loads alone; a model that cannot fit even alone fails visibly.

    Live 2026-10-28: ``/api/ps`` reported size_vram == size for the first second after the load and only then
    settled to the spilled figure, so one read passed a spilled model. The figure is read again ``confirm``
    seconds later and must agree before it counts. The same day: Ollama 0.35 accepts ``keep_alive: 0`` in 2 ms
    and ignores it while that runner has a request in flight (it only bumps expires_at), so an unload is only
    trusted once the entry is gone from ``/api/ps``; a runner that stays busy past ``timeout`` is reported as
    busy, not as a model that cannot fit.

    Live 2026-10-28 (cw3-13fb755c): a child behind the supervisor's inference bridge (coder_sandbox_agent.model_bridge)
    sees ``/api/ps`` filtered to its own runner and cannot unload any other model, so its eviction loop unloaded ITSELF
    six times beside the still-resident planning model and raised a false "does not fit". Eviction is therefore the
    supervisor's job: the bridge runs this function against upstream Ollama before it forwards a warm load, and the
    child's own call only confirms residency (its loop still protects the direct, non-bridged callers). An upstream
    ``{'error': ...}`` on the load is surfaced verbatim so the bridge's reason reaches the child.
    """
    import requests
    requests = requests_client or requests
    settle = SETTLE_SECONDS if settle is None else settle
    confirm = CONFIRM_SECONDS if confirm is None else confirm
    names = {model, model + ':latest'}
    def models():
        response = requests.get(url + '/api/ps', timeout=min(15, timeout))
        response.raise_for_status()
        return response.json().get('models', [])
    def mine(entry):
        return entry.get('name') in names or entry.get('model') in names
    def loaded(settled=False):
        """The model's /api/ps entry; ``settled`` re-reads after the runner has placed its buffers."""
        entry = next((m for m in models() if mine(m)), None)
        if settled and entry and resident(entry):
            time.sleep(confirm)
            entry = next((m for m in models() if mine(m)), None)
        return entry
    def unload(name):
        response = requests.post(url + '/api/generate', json={'model':name,'keep_alive':0}, timeout=timeout)
        response.raise_for_status()
    def gone(names_to_free, deadline):
        """Ask every named runner to unload and wait until /api/ps no longer lists them; False if one stays busy."""
        while True:
            for name in names_to_free:
                unload(name)
            time.sleep(min(settle, max(0, deadline - time.monotonic())))
            still = [m for m in models() if (m.get('name') or m.get('model')) in names_to_free]
            if not still:
                return True
            if time.monotonic() >= deadline:
                return False
    def load():
        response = requests.post(url + '/api/generate', json={'model':model,'prompt':'','stream':False,
            'options':{'num_ctx':policy.num_ctx},'keep_alive':'10m'}, timeout=timeout)
        if getattr(response, 'status_code', 200) >= 400:
            try: error = response.json().get('error')
            except ValueError: error = None
            if error:
                raise ValueError(error)   # the bridge's reason, not a bare "400 Client Error"
        response.raise_for_status()
        if response.json().get('error'):
            raise ValueError(response.json()['error'])
    current = loaded(settled=True)
    if current and current.get('context_length') == policy.num_ctx and resident(current):
        return
    deadline = time.monotonic() + max(settle, timeout - 60)
    busy = False
    if current and not gone([model], deadline):
        busy = True   # a reload would be a no-op on the runner that could not be freed
    else:
        load()
        current = loaded(settled=True)
    for _ in range(attempts):
        if busy:
            break
        if not current or current.get('context_length') != policy.num_ctx or resident(current):
            break
        # Free the GPUs for this model: every loaded runner, this one included, must really be gone before the
        # reload, or Ollama answers the reload as a no-op on the spilled runner.
        occupants = [m.get('name') or m.get('model') for m in models()]
        if not gone(occupants, deadline):
            busy = True
            break
        load()
        current = loaded(settled=True)
    if busy:
        raise ValueError(f'{model} is spilled to the CPU and Ollama will not unload a runner that is still answering a request; '
                         'waited for the GPUs to free up until this stage ran out of time. Try again once the other jobs finish.')
    if not current or current.get('context_length') != policy.num_ctx:
        raise ValueError(f'Ollama did not allocate the configured {policy.num_ctx} context. Adjust Settings or runtime memory.')
    if not resident(current):
        raise ValueError(f'{model} does not fit in GPU memory at {policy.num_ctx} context even after freeing the other loaded '
                         'models; part of it would run on the CPU at a fraction of the speed. Lower the Daedalus context in Settings.')


def local_chat(store, operation_id, role, messages, *, temperature=0.2, model=None, schema=None):
    """Stream internally so cancellation closes inference, and count real calls."""
    import requests
    from coder_worker_runtime import _boundary
    operation = _boundary(store, operation_id)
    payload = operation['payload']
    policy = resolve(role, operation_settings(payload))
    model = model or payload['model']
    images = sum(len(message.get('images',[])) for message in messages)
    details = require_local_model(payload['ollama_url'],model) if payload.get('policy_version',1)>=2 or images else {}
    estimate = estimate_tokens({'messages':[{k:v for k,v in message.items() if k!='images'} for message in messages]})
    if images:
        if 'vision' not in details.get('capabilities',[]):
            raise ValueError('Selected local model does not support images')
        estimate += images * payload['settings'].get('daedalus_image_tokens',2048)
    if estimate > policy.input_budget:
        raise InputBudgetError(f'{role} input exceeds the configured context. Narrow the source range or increase context in Settings.')
    url = payload['ollama_url'].rstrip('/')
    remaining = payload['seconds_remaining'] - (time.time() - operation['started'])
    ensure_context(url, model, policy, remaining)
    _boundary(store, operation_id, model_call=True)
    store.event(operation_id, 'model_call', context=policy.as_dict(), prompt_tokens_estimate=estimate, image_count=images, model=model)
    content, final = [], None
    thinking, thinking_mode = thinking_options(role,{**payload,'model':model},details)
    store.event(operation_id,'inference_policy',role=role,thinking=thinking_mode,structured=bool(schema))
    mode=payload.get('structured_mode','schema')
    formatting = {'format':'json' if mode=='json' else schema} if schema and mode!='text' and not payload.get('schema_unsupported') else {}
    with requests.post(url + '/api/chat', json={'model':model,'messages':messages,'stream':True,**thinking,**formatting,
            'options':{'num_ctx':policy.num_ctx,'num_predict':policy.num_predict,'temperature':temperature}},
            headers={'X-Daedalus-Role': role},
            timeout=remaining, stream=True) as response:
        if formatting and response.status_code in (400,422) and any(word in response.text.lower() for word in ('format','schema','grammar')):
            if payload.get('policy_version',1)>=5:
                from coder_response_recovery import ResponseFormatError
                raise ResponseFormatError('Runtime rejected response format: '+response.text[:1000])
            response.close()
            current=store.get(operation_id)
            store.update(operation_id,payload={**current['payload'],'schema_unsupported':True})
            store.event(operation_id,'structured_output_fallback',reason='Runtime rejected response schema; semantic validation remains required',detail=response.text[:1000])
            return local_chat(store,operation_id,role,messages,temperature=temperature,model=model)
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
        if payload.get('policy_version', 1) >= 7:
            from coder_response_recovery import ResponseFormatError
            store.event(operation_id, 'output_limit', response=''.join(content),
                        usage={k: final.get(k) for k in ('prompt_eval_count', 'eval_count')})
            raise ResponseFormatError('Output limit reached; split this component into file-specific edits within Settings limits',
                                      response=''.join(content), failure_category='output_limit')
        if schema and payload.get('policy_version',1)>=5:
            from coder_response_recovery import ResponseFormatError
            raise ResponseFormatError('Response reached the configured completion allowance; return a concise complete response within the same limit',
                response=''.join(content), failure_category='output_limit')
        raise ValueError('Response reached the configured completion allowance; increase it in Settings or narrow the task')
    return ''.join(content)


def summarize_history(store, operation_id, history):
    """Fold complete persisted history into a checkpoint without losing source."""
    serialized = json.dumps(history, ensure_ascii=False)
    summary, offset = '', 0
    while offset < len(serialized):
        policy = resolve('compaction', operation_settings(store.get(operation_id)['payload']))
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
