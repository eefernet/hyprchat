"""Server-owned delivery for interactive and headless chat consumers."""
import asyncio
import json
import time

import chat_context
import chat_turns
import database as db


_SAVES: set = set()     # final saves still running after their request was cancelled


def sse(payload):
    return 'data: ' + json.dumps(payload) + '\n\n'


async def deliver(stream, req):
    message_id = None
    visible = ''
    candidate = ''
    reviewing = False
    terminal = False
    revisions = []
    calls = []
    effort = None
    started = time.monotonic()
    ephemeral = bool(getattr(req, 'ephemeral', False))
    meter = chat_context.TurnMeter()     # what the chat holds in the model's window (chat_context.py)
    # Where the current model call's text starts. A `clear` retracts that call's text only: it used to
    # empty the whole reply, which lost earlier rounds and, in continue mode, the original half for good.
    round_start = candidate_start = 0
    # Saved with the reply, so a chat whose model was changed later still says who wrote what, and so
    # "cut off at the output limit" is the server's fact (the browser's copy came back after a Continue).
    model = getattr(req, 'model', '') or ''
    truncated = False
    round_limit = 0      # the reply used every tool round it had (chat_rounds.py)

    async def save(status):
        if message_id is not None and not ephemeral:
            await db.save_chat_delivery(message_id, visible, {
                'in_progress': status == 'running', 'delivery_status': status,
                'model': model, 'truncated': truncated and status == 'completed',
                'round_limit': round_limit if status == 'completed' else 0,
                'review_history': revisions, 'effort': effort,
                'usage_calls': calls, 'elapsed_seconds': round(time.monotonic() - started, 3),
                'total_prompt_tokens': sum(c.get('prompt_tokens', 0) for c in calls),
                'total_gen_tokens': sum(c.get('gen_tokens', 0) for c in calls),
                **({'context': reading} if (reading := meter.reading(visible)) else {}),
            })

    try:
        async for chunk in stream:
            if not chunk.startswith('data:'):
                yield chunk
                continue
            try:
                event = json.loads(chunk[5:].strip())
            except (ValueError, TypeError):
                yield chunk
                continue
            kind = event.get('type')
            if kind == 'init':
                message_id = event.get('message_id')
                visible = event.get('prior_content') or ''
                revisions = event.pop('prior_reviews', [])
                event['server_persists'] = True
                meter.begin(visible)
                round_start = len(visible)
                await save('running')
            elif kind == 'effort':
                effort = event
            elif kind == 'context_info':
                meter.info(event)
                if event.get('prompt_estimate'):          # sent once before every model call
                    round_start, candidate_start = len(visible), len(candidate)
                continue
            elif kind == 'ctx_update':
                event = meter.live(event, visible)
            elif kind == 'context_compacted':
                meter.compacted(event)
            elif kind == 'context_kept':                      # this reply's tool record grew (chat_tool_memory.py)
                meter.keep(event)
                continue
            elif kind == 'usage_round':
                calls.append({k: v for k, v in event.items() if k != 'type'})
                meter.usage(event)
                if not ephemeral:
                    await db.record_token_usage(req.conversation_id, req.model,
                        getattr(req, 'persona_id', '') or '', event['prompt_tokens'], event['gen_tokens'])
                continue
            elif kind == 'refinement_start':
                if reviewing and candidate.strip():
                    visible = candidate
                    yield sse({'type': 'replace', 'content': visible})
                if visible.strip() and (not revisions or revisions[-1]['content'] != visible):
                    revisions.append({'round': len(revisions), 'content': visible})
                candidate = ''
                reviewing = True
                await save('running')
            elif kind == 'token':
                if reviewing:
                    candidate += event.get('content', '')
                    continue
                visible += event.get('content', '')
            elif kind == 'clear':
                if reviewing:
                    candidate = '' if event.get('all') else candidate[:candidate_start]
                    continue
                keep = 0 if event.get('all') else min(round_start, len(visible))
                visible = visible[:keep]
                event = {'type': 'clear', 'keep': keep}    # the browser trims its copy to the same length
            elif kind == 'review_incomplete':
                candidate = ''
                event = {'type': 'review_status', 'notice': 'Review incomplete; previous answer retained.'}
            elif kind == 'model_routed':
                model = event.get('model') or model
            elif kind == 'error':
                # Keep the reason on the persisted row: the live ⚠ block is client-side only
                # and a reload used to show just "generation failed".
                if not visible.strip() or not reviewing:
                    visible, fresh = chat_turns.failure_text(visible, event.get('error'))
                    if fresh:
                        yield sse({'type': 'token', 'content': visible})
                    event['saved_note'] = True     # the saved reply carries the reason: no second block needed
                if reviewing:
                    candidate = ''
                    yield sse({'type': 'replace', 'content': visible})
                await save('failed')
                terminal = True
            elif kind == 'done':
                model = event.get('model') or model
                truncated = event.get('done_reason') == 'length'
                round_limit = int(event.get('round_limit') or 0)
                if not reviewing and not visible.strip():
                    visible = '_[The model produced no text response.]_'
                    yield sse({'type': 'token', 'content': visible})
                if reviewing and candidate.strip() and event.get('done_reason') != 'length':
                    visible = candidate
                    if not revisions or revisions[-1]['content'] != visible:
                        revisions.append({'round': len(revisions), 'content': visible})
                    yield sse({'type': 'replace', 'content': visible})
                if reviewing and (not candidate.strip() or event.get('done_reason') == 'length'):
                    event['review_notice'] = 'Previous completed answer retained.'
                event['total_prompt_tokens'] = sum(c.get('prompt_tokens', 0) for c in calls)
                event['total_gen_tokens'] = sum(c.get('gen_tokens', 0) for c in calls)
                if (reading := meter.reading(visible)):
                    event['context'] = reading
                await save('completed')
                terminal = True
            yield sse(event)
    except Exception as error:
        # Anything the chat loop did not turn into an error event itself (a malformed request, a bug). The
        # connection used to just close: the browser took that for a finished reply and showed nothing.
        import traceback
        traceback.print_exc()
        if not terminal:
            reason = ' '.join(f'{type(error).__name__}: {error}'.split())[:160]
            if not (reviewing and visible.strip()):      # a reviewed answer that was kept stays as it is
                visible, fresh = chat_turns.failure_text(visible, reason)
                if fresh:
                    yield sse({'type': 'token', 'content': visible})
            await save('failed')
            terminal = True
            yield sse({'type': 'error', 'error': reason, 'saved_note': True})
    finally:
        try:
            await stream.aclose()
        finally:
            if not terminal and message_id is not None and not ephemeral:
                # Shield the bounded local commit from an HTTP disconnect. The web server cancels this
                # task again at every await once the browser is gone, and awaiting the save a second time
                # (unshielded) passed that cancellation on to the save itself: the reply stayed "running"
                # with nothing saved. Keep a reference, let it finish on its own, and stay cancelled.
                task = asyncio.create_task(save('interrupted'))
                _SAVES.add(task)
                task.add_done_callback(_SAVES.discard)
                await asyncio.shield(task)
