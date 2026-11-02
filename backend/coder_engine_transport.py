"""Bounded pre-dispatch inference recovery with authoritative usage events."""
import hashlib
from copy import deepcopy
import http.client
import json
import socket
import time
from urllib.parse import urlsplit
import uuid


class TransportFailure(RuntimeError):
    def __init__(self, status, category, message, retryable=False, usage=None, upstream_status=None):
        super().__init__(message)
        self.status, self.category, self.retryable = status, category, retryable
        self.usage = usage
        self.upstream_status = upstream_status


def upstream_error(status, detail):
    malformed = any(word in detail.lower() for word in ('invalid tool call', 'tool call arguments', 'unexpected end of json'))
    overflow = any(word in detail.lower() for word in ('context length', 'context window', 'input too long'))
    category = 'context_overflow' if overflow else 'malformed_tool_output' if malformed else 'upstream_service'
    return TransportFailure(status, category, f'Local inference returned HTTP {status}: {detail}',
                            retryable=not overflow and (malformed or status in {429, 500, 502, 503, 504}), upstream_status=status)


def request_json(upstream, method, path, body, timeout, connections, network_lock, closed):
    target = urlsplit(upstream + path)
    constructor = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
    connection = constructor(target.hostname, target.port, timeout=timeout)
    with network_lock:
        if closed.is_set(): raise InterruptedError('Inference bridge closed')
        connections.add(connection)
    try:
        connection.connect()
        if closed.is_set(): raise InterruptedError('Inference bridge closed')
        connection.request(method, target.path + ('?' + target.query if target.query else ''),
            body=json.dumps(body).encode() if body is not None else None, headers={'Content-Type': 'application/json'})
        with connection.getresponse() as response:
            if response.status >= 400:
                detail = response.read(8192).decode(errors='replace')
                try: detail = json.loads(detail).get('error', detail)
                except (ValueError, AttributeError): pass
                raise upstream_error(response.status, str(detail)[:1000])
            try: return json.load(response)
            except ValueError as error:
                raise TransportFailure(502, 'malformed_response', str(error), True, upstream_status=response.status) from error
    finally:
        with network_lock: connections.discard(connection)
        connection.close()


def infer(store, operation_id, body, role, policy, send):
    from coder_worker_runtime import _boundary
    from coder_engine_tokens import count_request
    operation = store.get(operation_id)
    payload = operation['payload']
    guarded = payload.get('engine_behavior_version', 0) >= 1
    requirement = (payload.get('response_requirements', {}).get(role) or
                   ('action_required' if role == 'builder' and operation['kind'] == 'code' else 'text_allowed')) if guarded else 'text_allowed'
    if requirement not in {'action_required', 'text_allowed'}:
        raise TransportFailure(400, 'response_configuration', 'Unknown controller response requirement')
    declared = {tool.get('function', {}).get('name') for tool in body.get('tools', [])}
    if requirement == 'action_required' and not declared:
        raise TransportFailure(400, 'response_configuration', 'Action-required inference needs declared tools')
    request = deepcopy(body)
    identity = uuid.uuid4().hex
    for attempt in range(2):
        estimate = count_request(request)
        if estimate > policy.input_budget:
            store.event(operation_id, 'model_failure', request_id=identity, attempt=attempt+1, role=role,
                        category='context_overflow', upstream_status=None, bridge_status=413,
                        response_requirement=requirement, retry=False, usage_available=False, usage=None,
                        prompt_tokens_estimate=estimate, dispatched=False, authority='supervisor',
                        message='Counted request exceeds the configured input budget')
            raise TransportFailure(413, 'context_overflow',
                f'{role} input has {estimate} counted tokens; its configured input budget is {policy.input_budget}')
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        current = _boundary(store, operation_id, model_call=True)
        started = time.time()
        store.event(operation_id, 'model_call', request_id=identity, attempt=attempt+1, role=role,
                    context=policy.as_dict(), prompt_tokens_estimate=estimate, counter='qwen-template-v1',
                    request_sha256=request_hash, inference_options=request['options'], thinking=request.get('think'),
                    response_requirement=requirement, authority='supervisor')
        try:
            result = send(current, request) if guarded else send(current)
            if not isinstance(result, dict) or not isinstance(result.get('message'), dict):
                raise TransportFailure(502, 'malformed_response', 'Inference response must contain a message object', True, upstream_status=200)
            usage = {key: result.get(key) for key in ('prompt_eval_count','eval_count','prompt_eval_duration','eval_duration','load_duration','done_reason')}
            if result.get('done_reason') == 'length':
                raise TransportFailure(502, 'output_limit',
                    'Inference exhausted its configured output allowance before natural termination; no tools were dispatched.',
                    True, usage=usage, upstream_status=200)
            calls = result['message'].get('tool_calls', [])
            if calls is None: calls = []
            if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
                raise TransportFailure(502, 'malformed_tool_output', 'Native tool calls must be a list of objects', True, usage=usage, upstream_status=200)
            for call in calls:
                function = call.get('function') or {}
                if not isinstance(function, dict) or not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), dict):
                    raise TransportFailure(502, 'malformed_tool_output', 'Incomplete native tool-call name or arguments', True, usage=usage, upstream_status=200)
                if guarded and function['name'] not in declared:
                    raise TransportFailure(502, 'malformed_tool_output', 'The response names an undeclared tool', True, usage=usage, upstream_status=200)
            if result.get('done') is not True:
                raise TransportFailure(502, 'incomplete_response', 'Inference did not produce a complete response', True, usage=usage, upstream_status=200)
            if requirement == 'action_required' and not calls:
                raise TransportFailure(502, 'missing_required_tool',
                    'This coding turn requires a declared tool call; prose is not execution or completion.',
                    True, usage=usage, upstream_status=200)
            store.event(operation_id, 'model_response', request_id=identity, attempt=attempt+1, role=role,
                        seconds=time.time()-started, usage=usage, tool_calls=len(calls), upstream_status=200,
                        bridge_status=200, response_requirement=requirement, first_attempt_passed=attempt == 0,
                        recovered=attempt > 0, authority='supervisor')
            return result
        except InterruptedError:
            raise
        except (TransportFailure, OSError, http.client.HTTPException) as error:
            _boundary(store, operation_id)  # Cancellation/exhaustion never becomes a retry.
            if not isinstance(error, TransportFailure):
                error = TransportFailure(504 if isinstance(error, (socket.timeout, TimeoutError)) else 502,
                                         'timeout' if isinstance(error, (socket.timeout, TimeoutError)) else 'network', str(error), True)
            store.event(operation_id, 'model_failure', request_id=identity, attempt=attempt+1, role=role,
                        category=error.category, upstream_status=error.upstream_status if guarded else error.status,
                        bridge_status=error.status, response_requirement=requirement, message=str(error),
                        retry=bool(attempt == 0 and error.retryable), seconds=time.time()-started,
                        usage_available=error.usage is not None, usage=error.usage, authority='supervisor')
            if attempt or not error.retryable: raise error
            if guarded:
                instruction = ('The previous response reached the output limit before completion. Keep this response short.'
                    if error.category == 'output_limit' else
                    'The previous response did not contain a valid declared tool call.'
                    if error.category in {'missing_required_tool', 'malformed_tool_output'} else
                    'The previous response could not be used. Retry this turn.')
                instruction += (' Make one small valid tool call now, or call finish if the work and checks are done.'
                    if requirement == 'action_required' else ' Return a concise complete response in the requested format.')
                request.setdefault('messages', []).append({'role': 'user', 'content': '[Controller response recovery] ' + instruction})
                store.event(operation_id, 'response_recovery', request_id=identity, category=error.category,
                            response_requirement=requirement, instruction=instruction, authority='supervisor')
    raise AssertionError('Unreachable inference retry state')
