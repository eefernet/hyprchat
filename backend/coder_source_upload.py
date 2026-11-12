"""Bounded, durable source admission before any inspection/model dispatch."""
import asyncio
from contextlib import suppress
import time

import httpx

MAX_ATTEMPTS = 3
RETRY_SECONDS = 2


async def ensure_source(store, client, job_id, url, path, checksum, settings):
    job = await store.get(job_id)
    ledger = dict(job.get('source_preparation') or {})
    if ledger and ledger.get('sha256') != checksum:
        raise ValueError('Source preparation identity changed')
    if not ledger:
        started = time.time()
        ledger = {'sha256': checksum, 'started': started,
                  'deadline': started + settings.get('daedalus_preparation_seconds', 600),
                  'attempts': 0, 'status': 'pending'}
        await store.save(job_id, source_preparation=ledger)

    async def boundary():
        current = await store.get(job_id)
        if not current or current.get('cancel_requested'):
            raise InterruptedError('Cancellation requested')
        if time.time() >= ledger['deadline']:
            raise TimeoutError('Source preparation allowance exhausted')

    async def wait(factory):
        await boundary()
        task = asyncio.create_task(factory())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=min(.2, max(.001, ledger['deadline'] - time.time())))
                await boundary()
                if done: return task.result()
        finally:
            if not task.done(): task.cancel()
            with suppress(asyncio.CancelledError, Exception): await task

    def receipt(response):
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get('sha256') != checksum:
            raise ValueError('Worker source checksum receipt mismatch')

    async def complete(response):
        receipt(response)
        ledger.update(status='complete', finished=time.time())
        await store.save(job_id, source_preparation=ledger, blocker='')

    def transient(error):
        if isinstance(error, httpx.TransportError): return True
        if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code != 503: return False
        try: return error.response.json().get('detail') == 'VM maintenance is in progress; retry shortly'
        except (ValueError, AttributeError): return False

    try:
        # A committed receipt is durable, just like the worker's immutable source.
        # Resume cannot reset attempts/time or change the exact accepted parent.
        if ledger['status'] == 'complete':
            current = await store.get(job_id)
            if not current or current.get('cancel_requested'): raise InterruptedError('Cancellation requested')
            return
        if ledger['status'] == 'blocked':
            raise TimeoutError('Source preparation is blocked; its consumed allowance is retained')
        while True:
            # Probe first, including after an uncertain final PUT response. This
            # never replays a successful upload or dispatches a second operation.
            await boundary()
            if ledger['attempts'] >= MAX_ATTEMPTS:
                raise TimeoutError('Source admission attempts exhausted')
            ledger['attempts'] += 1
            await store.save(job_id, source_preparation=ledger, event='source_upload_started')
            uploading = False
            try:
                response = await wait(lambda: client.get(url, timeout=15))
                if response.status_code != 404:
                    await complete(response)
                    return
                async def chunks():
                    with path.open('rb') as source:
                        while chunk := source.read(1024 * 1024):
                            await boundary()
                            yield chunk
                uploading = True
                response = await wait(lambda: client.put(url, content=chunks(), timeout=120, params={
                    'upload_mb': settings['daedalus_upload_mb'],
                    'extracted_mb': settings['daedalus_extracted_mb'],
                    'free_mb': settings['daedalus_min_free_mb']}))
                await complete(response)
                return
            except httpx.HTTPError as error:
                if not transient(error): raise
                await store.save(job_id, source_preparation=ledger, event='source_upload_waiting',
                                 blocker='Waiting for source admission; the accepted revision is saved.')
                if ledger['attempts'] >= MAX_ATTEMPTS:
                    # One bounded receipt probe resolves a lost successful PUT.
                    if uploading and not ledger.get('final_probe'):
                        ledger['final_probe'] = True
                        await store.save(job_id, source_preparation=ledger)
                        probe = await wait(lambda: client.get(url, timeout=15))
                        if probe.status_code == 200:
                            await complete(probe)
                            return
                    raise TimeoutError('Source admission attempts exhausted') from error
                await wait(lambda: asyncio.sleep(RETRY_SECONDS))
    except InterruptedError:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, OSError) as error:
        category = 'source_preparation_limit' if isinstance(error, TimeoutError) else 'source_upload'
        ledger.update(status='blocked', last_error=type(error).__name__)
        await store.save(job_id, source_preparation=ledger,
            failure={'category': 'environment', 'stage': 'source_preparation',
                     'preparation': [{'category': category, 'phase': 'source', 'retryable': False,
                                      'attempt': ledger['attempts'], 'detail': str(error)[:1000]}],
                     'next_action': 'Restore source admission before starting a new job with the retained revision.'})
        raise
