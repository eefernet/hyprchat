"""Behavioral regressions for report evidence, bounded writing and durable progress."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database as db
import research
import research_evidence as evidence
import research_writer as writer
from .optional_deps import install_rag_stub


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'reports.db'))
    run(db.init_db())
    rag = install_rag_stub(monkeypatch)
    rag.embed_single = AsyncMock(return_value=None)
    run(db.create_research_report('r1', query='storage', status='running'))
    return rag


def test_durable_keyword_retrieval_tail_and_exact_names(isolated):
    text = '# Background\n' + ('General storage information. ' * 500) + '\n# Compatibility\nThe XZ-9750 requires DDR5 ECC memory and socket SP5.\n'
    run(evidence.store_documents('r1', [dict(source_id='S1', title='Manual', url='https://example.org/manual', content=text)]))
    # No cache or embeddings required after process restart.
    hits = run(evidence.retrieve('r1', ['XZ-9750 DDR5 SP5'], top_k=3))
    assert hits and 'XZ-9750' in hits[0]['text']
    assert hits[0]['source_id'] == 'S1'
    assert text[hits[0]['start_char']:hits[0]['end_char']] == hits[0]['text']
    assert run(evidence.evidence_stats('r1'))['chunks'] > 5


def test_semantic_queries_are_independent_and_ignore_foreign_ids(isolated, monkeypatch):
    run(evidence.store_documents('r1', [dict(source_id='S1', content='First document.'), dict(source_id='S2', content='Second document.')]))
    async def mark_indexed():
        conn = await db.get_db()
        try:
            await conn.execute('UPDATE research_chunks SET embedded=1 WHERE report_id=?', ('r1',))
            await conn.commit()
            return [r['id'] for r in await conn.execute_fetchall('SELECT id FROM research_chunks ORDER BY source_id')]
        finally:
            await conn.close()
    ids = run(mark_indexed())
    class Collection:
        def count(self): return 3
        def query(self, query_embeddings, **kwargs):
            assert kwargs['where'] == {'report_id': 'r1'}
            return {'ids': [['foreign-report-chunk', ids[0 if query_embeddings[0][0] else 1]]]}
    class Chroma:
        def get_collection(self, name):
            assert name == evidence.collection_name('r1')
            return Collection()
    isolated.embed_single = AsyncMock(side_effect=[[1., 0.], [0., 1.]])
    monkeypatch.setattr(isolated, 'get_chroma', lambda: Chroma(), raising=False)
    hits = run(evidence.retrieve('r1', ['semantic alpha', 'semantic beta'], top_k=2))
    assert {r['source_id'] for r in hits} == {'S1', 'S2'}
    assert [call.args[0] for call in isolated.embed_single.call_args_list] == ['semantic alpha', 'semantic beta']


def test_user_and_report_isolation(isolated):
    run(evidence.store_documents('r1', [dict(source_id='S1', title='Private', content='Private XZ-9750 details. ' * 40)]))
    run(db.create_research_report('r2', query='other', status='running'))
    assert run(evidence.retrieve('r2', ['XZ-9750'])) == []
    token = db.set_current_user_id('someone-else')
    try:
        assert run(evidence.retrieve('r1', ['XZ-9750'])) == []
        assert run(evidence.inspect_source('r1', 'S1')) is None
        assert not run(evidence.store_documents('r1', [dict(source_id='S2', content='attack')]))
        run(db.delete_research_report('r1'))
    finally:
        db.reset_current_user_id(token)
    assert run(evidence.inspect_source('r1', 'S1'))['available']


def test_delete_removes_text_fts_and_queues_index_cleanup(isolated, monkeypatch):
    run(evidence.store_documents('r1', [dict(source_id='S1', content='XZ-9750 specification. ' * 20)]))
    monkeypatch.setattr(evidence, 'retry_cleanup', AsyncMock())
    run(db.delete_research_report('r1'))
    async def counts():
        conn = await db.get_db()
        try:
            return {t: (await conn.execute_fetchall(f'SELECT COUNT(*) AS n FROM {t}'))[0]['n'] for t in
                    ('research_documents', 'research_chunks', 'research_chunks_fts', 'research_index_cleanup')}
        finally:
            await conn.close()
    assert run(counts()) == {'research_documents': 0, 'research_chunks': 0, 'research_chunks_fts': 0, 'research_index_cleanup': 2}
    assert not run(evidence.store_documents('r1', [dict(source_id='S2', content='late writer')]))


def test_cancel_cannot_recreate_or_replace_evidence(isolated):
    run(db.update_research_report('r1', status='cancelled'))
    assert not run(evidence.store_documents('r1', [dict(source_id='S1', content='late data')]))
    assert run(evidence.evidence_stats('r1'))['documents'] == 0


def test_source_ids_survive_reranking():
    registry = {}
    first = {'title': 'Old source', 'url': 'https://example.org/old', 'content': 'original content', 'score': 1}
    old = research._normalize_report_sources([], [], [first], {'target_sources': 1}, registry)
    newer = research._normalize_report_sources([], [], [first, {'title': 'New source', 'url': 'https://nasa.gov/new', 'content': 'new', 'score': 100}], {'target_sources': 1}, registry)
    assert next(s for s in newer if s['url'] == first['url'])['index'] == old[0]['index']
    assert len(newer) == 2
    assert len({s['index'] for s in newer}) == 2


def test_page_cannot_inherit_another_sources_reused_id():
    records = research._build_report_evidence_records('r', 'topic', '',
        [{'index': 1, 'title': 'New', 'url': 'https://nasa.gov/new'}],
        [{'source_index': 1, 'url': 'https://example.org/old', 'content': 'Old evidence ' * 50}])
    page = next(r for r in records if r['kind'] == 'full_page')
    assert page['source_id'] == 'S?'
    assert page['title'] != 'New'


class Stream:
    def __init__(self, frames, status=200):
        self.frames, self.status_code = frames, status
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def aiter_lines(self):
        for frame in self.frames: yield json.dumps(frame)


class HTTP:
    def __init__(self, frames, status=200): self.frames, self.status = frames, status
    def stream(self, *_args, **_kwargs): return Stream(self.frames, self.status)


@pytest.mark.parametrize('frames,status,reason', [
    ([{'error': 'unavailable'}], 500, 'error'),
    ([{'response': '# Report\nPartial draft'}], 200, 'unexpected_eof'),
    ([{'response': '# Report\nCut off', 'done': True, 'done_reason': 'length'}], 200, 'length'),
    ([{'done': True}], 200, 'stop'),
])
def test_bad_streams_never_count_as_complete(frames, status, reason, monkeypatch):
    monkeypatch.setattr(research, '_emit_report_event', AsyncMock())
    result = run(research._ask_report_streamed(HTTP(frames, status), 'test', None, 'r', 'prompt', structured=True))
    assert not result.complete
    assert result.finish_reason == reason
    if frames[0].get('response'): assert result.text.startswith('# Report')


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'custom'])
@pytest.mark.parametrize('ending', ['stop', 'length', 'eof'])
def test_cloud_provider_terminals_reach_report_completion(provider, ending, monkeypatch):
    import model_providers
    monkeypatch.setattr(model_providers, 'get_api_key', AsyncMock(return_value='test-only'))
    monkeypatch.setattr(model_providers, 'get_custom_config', AsyncMock(return_value={'base_url': 'http://test', 'api_key': 'test-only'}))
    monkeypatch.setattr(research, '_emit_report_event', AsyncMock())
    if provider == 'openai':
        frames = [{'type': 'response.output_text.delta', 'delta': '# Report\nBody'}]
        if ending != 'eof': frames.append({'type': 'response.completed' if ending == 'stop' else 'response.incomplete', 'response': {'incomplete_details': {'reason': 'max_output_tokens'}}})
    elif provider == 'anthropic':
        frames = [{'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '# Report\nBody'}}]
        if ending != 'eof': frames += [{'type': 'message_delta', 'delta': {'stop_reason': 'end_turn' if ending == 'stop' else 'max_tokens'}}, {'type': 'message_stop'}]
    else:
        frames = [{'choices': [{'delta': {'content': '# Report\nBody'}, 'finish_reason': None}]}]
        if ending != 'eof': frames.append({'choices': [{'delta': {}, 'finish_reason': ending}]})
    class SSEStream(Stream):
        async def aiter_lines(self):
            for frame in self.frames: yield 'data: ' + json.dumps(frame)
    class SSEHTTP(HTTP):
        def stream(self, *_args, **_kwargs): return SSEStream(self.frames, self.status)
    result = run(research._ask_report_streamed(SSEHTTP(frames), '', None, 'r', 'prompt', model=provider + ':test', structured=True))
    assert result.complete == (ending == 'stop'), result
    assert result.text == '# Report\nBody'


def test_grouped_citations_and_fenced_examples():
    result = research._validate_report_citations('Fact [S1, S999].\n```text\n[S123]\n```', [{'index': 1}])
    assert result['invalid'] == ['S999']
    assert writer.citation_ids('Fact [S1; 2] and [S3].') == ['S1', 'S2', 'S3']
    assert writer.citation_ids('Use `[S99]` as an example. Fact [S1].') == ['S1']


def test_citation_must_have_been_supplied_to_its_section():
    text = '## Architecture\n\n' + 'A concrete claim [S2]. ' * 25
    checks = writer.report_checks(text, ['Architecture'], [{'index': 1}, {'index': 2}], [50, 200],
        [writer.SynthesisResult(text, 'stop')], {'Architecture': {'source_ids': ['S1']}})
    assert checks['invalid_citations'] == []
    assert checks['unavailable_evidence_sections'] == ['Architecture']


def test_off_topic_search_results_are_rejected():
    assert not research._report_result_relevant({'title': 'Intensive English Language Immersion LSI'}, 'LSI HBA firmware compatibility')
    assert not research._report_result_relevant({'title': 'Microsoft Account Sign In'}, 'Arch Linux CachyOS benchmarks')
    assert research._report_result_relevant({'title': 'LSI 9300-8i HBA firmware guide'}, 'LSI HBA firmware compatibility')


def test_cleanup_retries_survive_a_chroma_outage(isolated, monkeypatch):
    async def queue():
        conn = await db.get_db()
        try:
            await conn.execute('INSERT INTO research_index_cleanup(collection_name) VALUES(?)', ('orphan-index',))
            await conn.commit()
        finally:
            await conn.close()
    async def pending():
        conn = await db.get_db()
        try:
            return len(await conn.execute_fetchall('SELECT * FROM research_index_cleanup'))
        finally:
            await conn.close()
    class Chroma:
        failed = True
        deleted = []
        def list_collections(self):
            if self.failed: raise RuntimeError('unavailable')
            return ['orphan-index']
        def delete_collection(self, name): self.deleted.append(name)
    chroma = Chroma()
    monkeypatch.setattr(isolated, 'get_chroma', lambda: chroma, raising=False)
    run(queue())
    run(evidence.retry_cleanup())
    assert run(pending()) == 1
    chroma.failed = False
    run(evidence.retry_cleanup())
    assert run(pending()) == 0
    assert chroma.deleted == ['orphan-index']


def test_delete_waits_for_cancelled_embedding_publication(isolated, monkeypatch):
    import threading
    import httpx
    started, release = threading.Event(), threading.Event()
    collections = set()
    class Collection:
        def upsert(self, **kwargs):
            started.set()
            assert release.wait(3)
            collections.add(evidence.collection_name('r1'))
    class Chroma:
        def get_or_create_collection(self, *_args, **_kwargs): return Collection()
        def list_collections(self): return list(collections)
        def delete_collection(self, name): collections.discard(name)
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'embeddings': [[1., 0.]]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def post(self, *_args, **_kwargs): return Response()
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    monkeypatch.setattr(isolated, 'EMBED_MODEL', 'local-embed', raising=False)
    monkeypatch.setattr(isolated, 'get_chroma', lambda: Chroma(), raising=False)
    run(evidence.store_documents('r1', [dict(source_id='S1', content='One specification.')]))
    async def exercise():
        indexing = asyncio.create_task(evidence.index_pending('r1'))
        assert await asyncio.to_thread(started.wait, 3)
        indexing.cancel()
        deletion = asyncio.create_task(db.delete_research_report('r1'))
        await asyncio.sleep(.03)
        assert not deletion.done()
        indexing.cancel()  # Stop followed by Delete must still wait for the worker.
        await asyncio.sleep(.01)
        assert not deletion.done()
        release.set()
        await asyncio.gather(indexing, return_exceptions=True)
        await deletion
        assert not collections
        assert await db.get_research_report('r1') is None
    run(exercise())


def test_evidence_route_is_user_scoped(isolated, monkeypatch):
    from .test_research_reports_durable import _import_main_for_research_routes
    main = _import_main_for_research_routes(monkeypatch)
    run(evidence.store_documents('r1', [dict(source_id='S1', content='Private specification. ' * 40)]))
    assert run(main.get_research_evidence('r1', 'S1', offset=0, limit=1))['available']
    token = db.set_current_user_id('another-user')
    try:
        with pytest.raises(main.HTTPException) as error:
            run(main.get_research_evidence('r1', 'S1', offset=0, limit=1))
        assert error.value.status_code == 404
        with pytest.raises(main.HTTPException): run(main.cancel_research_report('r1'))
        with pytest.raises(main.HTTPException): run(main.delete_research_report('r1'))
    finally:
        db.reset_current_user_id(token)
    assert run(db.get_research_report('r1'))['status'] == 'running'


def test_context_budget_keeps_whole_excerpts(monkeypatch):
    monkeypatch.setattr(writer, 'research_num_ctx', lambda: 2048)
    rows = [dict(id=str(i), source_id='S1', text='word ' * 500) for i in range(8)]
    prompt, ids = writer.fit_prompt('Instructions', rows, 600)
    assert len(ids) < 8
    assert writer.context_policy.estimate_tokens(prompt) + 600 + 256 <= 2048
    assert prompt.count('<source-text>') == prompt.count('</source-text>')
    with pytest.raises(ValueError): writer.fit_prompt('X' * 10000, rows, 1000)


def test_word_targets_and_completion():
    assert writer.writing_target(5, 'about 900 words') == [810, 900]
    assert writer.writing_target(4, 'in 3,000–4,000 words') == [3000, 4000]
    checked = writer.report_checks('# Report\n## Overview\nBrief.', ['Overview', 'Risks'], [{'index': 1}], [3000, 5000], [writer.SynthesisResult('Brief.', 'length')])
    assert checked['missing_sections'] == ['Risks']
    assert not checked['generation_complete']
    assert any('No source citations' in x for x in checked['issues'])
    review = writer.normalize_review({'unsupported_claims': 'not an array', 'section_revisions': 123, 'missing_questions': [None, 'What is measured?']})
    assert review['unsupported_claims'] == []
    assert review['section_revisions'] == []
    assert review['missing_questions'] == ['What is measured?']


@pytest.mark.parametrize('failed_repair', [False, 'empty', 'error', 'length', 'invalid'])
def test_section_writer_snapshots_and_failure_preserve_draft(isolated, monkeypatch, failed_repair):
    run(evidence.store_documents('r1', [dict(source_id='S1', title='Manual', content='Socket SP5 supports DDR5. ' * 100)]))
    calls, emitted = [], []
    async def generate(*args, **kwargs):
        prompt = args[4]
        import re
        heading = re.search(r'Write only the section "([^"]+)"', prompt).group(1)
        calls.append(heading)
        if failed_repair and len(calls) > 3:
            text = '' if failed_repair == 'empty' else '## Risks\nINTERRUPTED_REPLACEMENT [S999].'
            await kwargs['on_update'](text)
            return writer.SynthesisResult(text, 'length' if failed_repair == 'length' else 'stop' if failed_repair == 'invalid' else 'error', error='HTTP 500 during repair' if failed_repair in ('empty', 'error') else '')
        text = f'## {heading}\n\n' + ('Evidence supports this conclusion [S1]. ' * 90)
        await kwargs['on_update'](text)
        return writer.SynthesisResult(text, 'stop')
    monkeypatch.setattr(research, '_ask_report_streamed', generate)
    monkeypatch.setattr(research, '_ask_ollama_json', AsyncMock(return_value={'revision_advice': 'Verify Risks', 'section_revisions': [{'heading': 'Risks', 'reason': 'Verify claims'}]} if failed_repair else {}))
    async def emit(*args): emitted.append((args[2], args[3]))
    monkeypatch.setattr(research, '_emit_report_event', emit)
    metrics = {'pages_read': 1, 'searches': 1}
    report, status = run(writer.compose_report(http=None, ollama_url='', events=None, report_id='r1', query='Socket SP5, 1500 words', focus='', title='Storage', template={'sections': ['Executive Summary', 'Architecture', 'Risks', 'References']}, depth=5, model='local', default_model='local', auditor_model='local', plan={}, sources=[{'index': 1, 'title': 'Manual', 'url': 'https://example.org'}], findings=[], audit={}, metrics=metrics))
    assert calls[:3] == ['Architecture', 'Risks', 'Executive Summary']
    assert status == 'complete'
    assert report.endswith('https://example.org')
    assert len(calls) == (4 if failed_repair else 3)
    if failed_repair:
        assert calls[-1] == 'Risks'
        assert metrics['retained_drafts'][-1]['section'] == 'Risks'
        assert 'INTERRUPTED_REPLACEMENT' not in report
        assert 'earlier draft was retained' in report
        assert '## Architecture' in report and '## Risks' in report and '## Executive Summary' in report
    snapshots = [d for t, d in emitted if t == 'research_snapshot']
    assert all('INTERRUPTED_REPLACEMENT' not in s['report_markdown'] for s in snapshots)
    assert [s['revision'] for s in snapshots] == sorted({s['revision'] for s in snapshots})
    saved = run(db.get_research_report('r1'))
    assert saved['report_markdown'] == report
    assert saved['metrics']['retrieval']['Architecture']['chunk_ids']


def test_sparse_evidence_uses_one_concise_draft_without_padding_repairs(isolated, monkeypatch):
    run(evidence.store_documents('r1', [dict(source_id='S1', title='Prototype note', content='Prototype storage box exists. No specifications or measurements have been published.')]))
    calls = []
    async def generate(*args, **kwargs):
        calls.append(args[4])
        assert 'Write the complete report' in args[4]
        text = '# Prototype\n\n## Overview\nPrototype exists [S1].\n\n## Evidence\nOnly a project note is available [S1].\n\n## Limitations\nNo measurements support performance claims [S1].'
        return writer.SynthesisResult(text, 'stop')
    monkeypatch.setattr(research, '_ask_report_streamed', generate)
    monkeypatch.setattr(research, '_ask_ollama_json', AsyncMock(return_value={}))
    monkeypatch.setattr(research, '_emit_report_event', AsyncMock())
    metrics = {'pages_read': 0}
    text, status = run(writer.compose_report(http=None, ollama_url='', events=None, report_id='r1', query='Prototype storage', focus='', title='Prototype', template={'sections': ['Overview', 'Evidence', 'Limitations']}, depth=5, model='local', default_model='local', auditor_model='local', plan={}, sources=[{'index': 1, 'title': 'Note'}], findings=[], audit={}, metrics=metrics))
    assert len(calls) == 1
    assert status == 'complete' and metrics['quality'] == 'limited'
    assert metrics['requested_word_target'] == [3000, 5000]
    assert metrics['word_target'] == [300, 600]
    assert 'Too little source text' in text
    assert metrics['writing_sections'] == 1


def test_report_fetch_preserves_tail_and_does_not_block_phoronix(monkeypatch):
    body = ('Readable benchmark results. ' * 400 + 'TAIL_TOKEN').encode()
    monkeypatch.setattr(research, 'fetch_bytes_safely', AsyncMock(return_value=(200, {'content-type': 'text/plain'}, 'https://phoronix.com/test', body)))
    diagnostics = []
    page = run(research._fetch_report_page(None, 'https://phoronix.com/test', diagnostics))
    assert page['content'].endswith('TAIL_TOKEN')
    assert diagnostics[0]['status'] == 'read'


@pytest.mark.parametrize('depth,expected_reads', [(1, 5), (3, 14), (5, 28)])
def test_entire_pipeline_obeys_depth_and_archives_original_text(isolated, monkeypatch, depth, expected_reads):
    searches, reads = [], []
    async def search(http, url, q, **kwargs):
        searches.append(q)
        return [{'url': f'https://example.org/source-{i}', 'title': f'SP5 storage source {i}',
                 'content': 'Socket SP5 memory and storage evidence ' * 12, 'score': 100-i} for i in range(80)]
    async def fetch(http, url, diagnostics):
        reads.append(url)
        diagnostics.append({'url': url, 'status': 'read'})
        return {'url': url, 'requested_url': url, 'content': '# Compatibility\n' + 'Socket SP5 supports DDR5 ECC storage servers. ' * 250}
    async def ask_json(http, url, prompt, **kwargs):
        if 'adaptive middle' in prompt:
            return {'learnings': ['SP5 supports DDR5 ECC'], 'gaps': ['Check compatibility'],
                    'follow_up_queries': ['SP5 storage compatibility ' + str(len(searches))]}
        if 'Extract the strongest findings' in prompt:
            return [{'claim': 'SP5 uses DDR5', 'evidence': 'The manual describes DDR5', 'source_ids': ['S1'], 'confidence': 'high'}]
        if 'citation auditor' in prompt: return {'coverage_score': 75, 'weaknesses': []}
        return {}
    captured = {}
    async def compose(**kwargs):
        captured.update(kwargs)
        return '# Report\n## Overview\nSupported [S1].', 'complete'
    monkeypatch.setattr(research, '_search_searxng', search)
    monkeypatch.setattr(research, '_fetch_report_page', fetch)
    monkeypatch.setattr(research, '_ask_ollama', AsyncMock(return_value=json.dumps({'title': 'Storage', 'research_questions': ['SP5 DDR5 compatibility']})))
    monkeypatch.setattr(research, '_ask_ollama_json', ask_json)
    monkeypatch.setattr(research, '_SEARCH_BATCH_DELAY_DEEP', 0)
    monkeypatch.setattr(evidence, 'index_pending', AsyncMock(return_value={'fallback': True}))
    monkeypatch.setattr(writer, 'compose_report', compose)
    class Events:
        async def emit(self, *_): pass
    result = run(research.run_research_report(None, '', 'local', Events(), 'r1', 'SP5 storage', depth=depth))
    assert result['status'] == 'complete', result
    assert len(reads) == expected_reads
    assert len(set(reads)) == expected_reads
    assert len(searches) <= research._research_depth_budget(depth)['queries']
    saved = run(evidence.inspect_source('r1', 'S1'))
    assert saved['available'] and saved['total'] > 4
    assert captured['metrics']['pages_read'] == expected_reads
    assert captured['metrics']['evidence_index']['chunks'] > expected_reads


@pytest.mark.parametrize('heading', ['## **Architecture**', '### ARCHITECTURE', '# _Architecture_', '###### Architecture ###'])
def test_section_evidence_checks_normalized_headings_and_duplicates(heading):
    text = '## Architecture\nValid [S1].\n' + heading + '\n' + ('Unsupported claim [S2]. ' * 30)
    checks = writer.report_checks(text, ['Architecture'], [{'index': 1}, {'index': 2}], [0, 1000],
        [writer.SynthesisResult(text, 'stop')], {'Architecture': {'source_ids': ['S1']}})
    assert checks['missing_sections'] == []
    assert checks['unavailable_evidence_sections'] == ['Architecture']
    uncited = heading + '\n' + 'Unsupported claim. ' * 50
    assert writer.report_checks(uncited, ['Architecture'], [{'index': 1}], [0, 1000], [])['uncited_sections'] == ['Architecture']


@pytest.mark.parametrize('fence', ['```', '~~~~'])
def test_fenced_headings_and_citations_are_not_evidence(fence):
    text = fence + '\n## Architecture\nClaim [S1].\n' + fence
    checks = writer.report_checks(text, ['Architecture'], [{'index': 1}], [0, 1000], [])
    assert checks['missing_sections'] == ['Architecture']
    assert checks['cited_sources'] == 0


@pytest.mark.parametrize('saved_status', ['running', 'complete', 'partial'])
def test_shutdown_retains_research_snapshot(isolated, monkeypatch, saved_status):
    async def scenario():
        entered = asyncio.Event()
        async def paused(*args, **kwargs):
            await db.update_research_report('r1', status=saved_status, report_markdown='## Retained draft\nVerified evidence [S1].')
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(research, '_ask_ollama', paused)
        monkeypatch.setattr(research, '_emit_report_event', AsyncMock())
        task = asyncio.create_task(research.run_research_report(None, '', 'local', None, 'r1', query='Storage', depth=1))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = await db.get_research_report('r1')
        assert row['status'] == ('failed' if saved_status == 'running' else saved_status)
        if saved_status == 'running':
            assert 'interrupted' in row['error']
        assert 'Retained draft' in row['report_markdown']
    run(scenario())


@pytest.mark.parametrize('bulk', [False, True])
def test_profile_deletion_queues_owned_indexes_and_removes_evidence(isolated, bulk):
    async def scenario():
        user = await db.create_user('Disposable evidence owner')
        token = db.set_current_user_id(user['id'])
        try:
            await db.create_research_report('r-owned', query='Owned research', status='running')
            await evidence.store_documents('r-owned', [{'source_id': 'S1', 'content': 'Private source evidence.'}])
        finally:
            db.reset_current_user_id(token)
        if bulk:
            await db.delete_users_except(db.DEFAULT_USER_ID)
        else:
            assert await db.delete_user(user['id'])
        conn = await db.get_db()
        try:
            for table in ('research_reports', 'research_documents', 'research_chunks', 'research_chunks_fts'):
                column = 'id' if table == 'research_reports' else 'report_id'
                assert not await conn.execute_fetchall(f'SELECT 1 FROM {table} WHERE {column}=?', ('r-owned',))
            queued = {r['collection_name'] for r in await conn.execute_fetchall('SELECT collection_name FROM research_index_cleanup')}
            assert evidence.collection_name('r-owned') in queued
            assert research._evidence_collection_name('r-owned') in queued
            assert evidence.collection_name('r1') not in queued
        finally:
            await conn.close()
        assert await db.get_research_report('r1')
    run(scenario())
