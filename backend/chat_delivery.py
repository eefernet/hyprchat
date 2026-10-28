"""Server-owned delivery for interactive and headless chat consumers."""
import asyncio
import json
import time

import database as db


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

    async def save(status):
        if message_id is not None and not ephemeral:
            await db.save_chat_delivery(message_id, visible, {
                'in_progress': status == 'running', 'delivery_status': status,
                'review_history': revisions, 'effort': effort,
                'usage_calls': calls, 'elapsed_seconds': round(time.monotonic() - started, 3),
                'total_prompt_tokens': sum(c.get('prompt_tokens', 0) for c in calls),
                'total_gen_tokens': sum(c.get('gen_tokens', 0) for c in calls),
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
                await save('running')
            elif kind == 'effort':
                effort = event
            elif kind == 'usage_round':
                calls.append({k: v for k, v in event.items() if k != 'type'})
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
                    candidate = ''
                    continue
                visible = ''
            elif kind == 'review_incomplete':
                candidate = ''
                event = {'type': 'review_status', 'notice': 'Review incomplete; previous answer retained.'}
            elif kind == 'error':
                if not visible.strip():
                    visible = '_[No response; generation failed.]_'
                    yield sse({'type': 'token', 'content': visible})
                if reviewing:
                    candidate = ''
                    yield sse({'type': 'replace', 'content': visible})
                await save('failed')
                terminal = True
            elif kind == 'done':
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
                await save('completed')
                terminal = True
            yield sse(event)
    finally:
        try:
            await stream.aclose()
        finally:
            if not terminal and message_id is not None and not ephemeral:
                # Shield the bounded local commit from an HTTP disconnect.
                task = asyncio.create_task(save('interrupted'))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
