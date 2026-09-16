"""Document refresh failure handling, provenance and actual shared index behavior."""
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from seed_kb import seed_coder_kb as seed
from seed_kb.coder_sources import source_catalog
import rag

SPEC={'filename':'swift_reference.md','url':'https://example.com/guide.md','title':'Swift',
      'authority':'official','version':'Swift 6.3'}


def test_catalog_breadth_unique_urls_and_swiftui():
    sources=source_catalog();names={s['filename'] for s in sources}
    assert len(names)==len(sources)
    assert len({s['url'] for s in sources})==len(sources)
    for prefix in ['python','rust','java','javascript','typescript','c_','cpp_','csharp','go_',
                   'ruby','php','lua','elixir','haskell','perl','scala','dart','swift','swiftui','sql']:
        assert any(n.startswith(prefix) for n in names),prefix
    for name in ['swiftui_state.md','swiftui_bindable.md','swiftui_observation.md','swiftui_navigation.md','swiftui_async.md','swiftui_accessibility.md']:
        assert next(s for s in sources if s['filename']==name)['authority']=='official'


def test_markdown_preserves_availability_code_and_removes_compiler_negative_tests():
    raw='<!-- {"availability":["iOS: 17.0.0 -"]} -->\n# Example\n'+('Reference documentation. '*20)+'\n```swift\nfunc value() {\n    print("hello")\n}\n```\n<!-- ```swifttest\nINVALID COMPILER INPUT\n``` -->'
    text=seed.normalize_document(raw,'text/markdown',SPEC,SPEC['url'])
    assert 'iOS: 17.0.0 -' in text and '    print("hello")' in text
    assert 'INVALID COMPILER INPUT' not in text
    rendered=seed.render_document(SPEC,text,SPEC['url'])
    assert 'Content-SHA256:' in rendered and 'Source: https://example.com/guide.md' in rendered


@pytest.mark.parametrize('declaration',['', '<?xml version="1.0" encoding="utf-8"?>'])
def test_html_extraction_rejects_empty_page_and_preserves_reference_code(declaration):
    with pytest.raises(ValueError):seed.normalize_document('<html><body></body></html>','text/html',SPEC,SPEC['url'])
    html='<html><body><article><h1>Example</h1><p>'+('Useful reference information. '*30)+'</p><pre><code>def example():\n    return 42</code></pre></article></body></html>'
    text=seed.normalize_document(declaration+html,'text/html',SPEC,SPEC['url'])
    assert 'def example():\n    return 42' in text and 'Useful reference information' in text


def test_audit_is_readonly_and_detects_duplicate_and_legacy_content(tmp_path,monkeypatch):
    path=tmp_path/'app.db';conn=sqlite3.connect(path)
    conn.executescript("CREATE TABLE knowledge_bases(id TEXT,name TEXT,user_id TEXT); CREATE TABLE kb_files(id INT,kb_id TEXT,filename TEXT,filepath TEXT,source_url TEXT);")
    conn.execute('INSERT INTO knowledge_bases VALUES(?,?,?)',('kb-test',seed.KB_NAME,'alice'))
    for i,name in enumerate(['swift_reference.md','ios_reference.md']):
        file=tmp_path/name;file.write_text('# description\n\n# Swift 5 Cheatsheet\nSame content')
        conn.execute('INSERT INTO kb_files VALUES(?,?,?,?,?)',(i,'kb-test',name,str(file),''))
    conn.commit();conn.close();before=path.read_bytes()
    monkeypatch.setattr(seed.config,'DATABASE_PATH',str(path))
    assert seed.read_inventory('bob','kb-test') is None
    report=seed.audit_inventory(seed.read_inventory('alice','kb-test'))
    assert any('duplicate' in issue for r in report for issue in r['issues'])
    assert path.read_bytes()==before


class Collection:
    def __init__(self, rows=None):self.rows=rows or {};self.fail_at=None;self.calls=0
    def get(self, **kwargs):
        return {'ids':list(self.rows),'documents':[r[0] for r in self.rows.values()],
                'metadatas':[r[1] for r in self.rows.values()],'embeddings':[r[2] for r in self.rows.values()]}
    def upsert(self,ids,documents,metadatas,embeddings):
        self.calls+=1
        if self.calls==self.fail_at:raise RuntimeError('simulated Chroma failure')
        self.rows.update({id:(t,m,e) for id,t,m,e in zip(ids,documents,metadatas,embeddings)})
    def delete(self,ids):
        for id in ids:self.rows.pop(id,None)


def setup_index(monkeypatch,tmp_path):
    file=tmp_path/'doc.md';file.write_text('Some reference documentation.')
    old_id=hashlib.md5(b'kb-test:doc.md:0').hexdigest()
    collection=Collection({old_id:('old reference',{'filename':'doc.md','chunk_index':0},[1.,0.]),'obsolete':('old tail',{'filename':'doc.md','chunk_index':7},[1.,0.])})
    monkeypatch.setattr(rag,'_get_collection',lambda _:collection)
    monkeypatch.setattr(rag,'chunk_document',lambda *_:[{'text':'new first','chunk_index':0},{'text':'new second','chunk_index':1}])
    mirror=AsyncMock();monkeypatch.setattr(rag.db,'kb_fts_replace_file',mirror)
    return file,collection,mirror


@pytest.mark.parametrize('embeddings',[[None,None],[[0.,1.],None],[]])
def test_failed_embeddings_preserve_both_indexes(monkeypatch,tmp_path,embeddings):
    file,collection,mirror=setup_index(monkeypatch,tmp_path);before=collection.rows.copy()
    monkeypatch.setattr(rag,'embed_texts',AsyncMock(return_value=embeddings))
    result=asyncio.run(rag.index_file('kb-test','doc.md',str(file)))
    assert result['error'] and result['chunks']==0
    assert collection.rows==before and collection.calls==0
    mirror.assert_not_called()


def test_shortened_index_prunes_only_after_success(monkeypatch,tmp_path):
    file,collection,mirror=setup_index(monkeypatch,tmp_path)
    monkeypatch.setattr(rag,'embed_texts',AsyncMock(return_value=[[0.,1.],[0.,1.]]))
    result=asyncio.run(rag.index_file('kb-test','doc.md',str(file)))
    assert result['chunks']==2 and 'obsolete' not in collection.rows
    assert {r[0] for r in collection.rows.values()}=={'new first','new second'}
    mirror.assert_awaited_once()


def test_mid_batch_failure_restores_previous_index(monkeypatch,tmp_path):
    file,collection,mirror=setup_index(monkeypatch,tmp_path);before=collection.rows.copy()
    monkeypatch.setattr(rag,'embed_texts',AsyncMock(return_value=[[0.,1.],[0.,1.]]))
    monkeypatch.setattr(rag,'CHROMA_UPSERT_BATCH',1);collection.fail_at=2
    with pytest.raises(RuntimeError):asyncio.run(rag.index_file('kb-test','doc.md',str(file)))
    assert collection.rows==before
    mirror.assert_not_called()


def test_markdown_chunks_preserve_complete_code_blocks_and_version_context(monkeypatch):
    monkeypatch.setattr(rag,'CHUNK_SIZE',110)
    code='```python\ndef example():\n    if True:\n        return 42\n```'
    doc=seed.render_document(SPEC,('Overview. '*40)+'\n\n'+code+'\n\n'+('Next section. '*50),SPEC['url'])
    chunks=rag.chunk_document(doc,'guide.md')
    assert len(chunks)>1
    assert any(code in c['text'] for c in chunks)
    assert all('Version: Swift 6.3' in c['text'] for c in chunks)
    assert all(c['text'].count('```')%2==0 for c in chunks)


def test_refresh_failure_retains_file_and_user_documents(monkeypatch,tmp_path):
    monkeypatch.setattr(seed.config,'KB_DIR',str(tmp_path))
    directory=tmp_path/'kb-test';directory.mkdir();target=directory/SPEC['filename'];target.write_text('old')
    user_file=directory/'my-notes.md';user_file.write_text('private notes')
    kb={'id':'kb-test','files':[{'id':1,'filename':SPEC['filename'],'filepath':str(target)}]}
    monkeypatch.setattr(rag,'index_file',AsyncMock(return_value={'chunks':0,'error':'embedding failed'}))
    with pytest.raises(ValueError):asyncio.run(seed.replace_document(kb,SPEC,'new'*100,SPEC['url'],{},tmp_path/'backup'))
    assert target.read_text()=='old' and user_file.read_text()=='private notes'


def test_unchanged_refresh_skips_indexing(monkeypatch,tmp_path):
    monkeypatch.setattr(seed.config,'KB_DIR',str(tmp_path))
    directory=tmp_path/'kb-test';directory.mkdir();target=directory/SPEC['filename']
    text='Reference content. '*30;body=seed.render_document(SPEC,text,SPEC['url']);target.write_text(body)
    kb={'id':'kb-test','files':[{'id':1,'filename':SPEC['filename'],'filepath':str(target)}]}
    manifest={SPEC['filename']:{'content_hash':seed.digest(text),'source':SPEC,'source_url':SPEC['url'],'file_hash':seed.digest(body),'chunks':2}}
    index=AsyncMock();monkeypatch.setattr(rag,'index_file',index)
    result=asyncio.run(seed.replace_document(kb,SPEC,text,SPEC['url'],manifest,tmp_path/'backup'))
    assert result['status']=='unchanged';index.assert_not_called()
    assert not (tmp_path/'backup').exists()


@pytest.mark.parametrize('protection',['edited','foreign_source'])
def test_refresh_preserves_user_owned_content(monkeypatch,tmp_path,protection):
    monkeypatch.setattr(seed.config,'KB_DIR',str(tmp_path))
    directory=tmp_path/'kb-test';directory.mkdir();target=directory/SPEC['filename']
    target.write_text('User edited reference notes')
    row={'id':1,'filename':SPEC['filename'],'filepath':str(target)}
    manifest={}
    if protection=='edited':manifest[SPEC['filename']]={'file_hash':seed.digest('previous managed text')}
    else:row['source_url']='https://example.net/user-guide'
    index=AsyncMock();monkeypatch.setattr(rag,'index_file',index)
    with pytest.raises(ValueError,match='preserved'):
        asyncio.run(seed.replace_document({'id':'kb-test','files':[row]},SPEC,'New text '*80,SPEC['url'],manifest,tmp_path/'backup'))
    assert target.read_text()=='User edited reference notes'
    index.assert_not_called()


def test_html_comment_examples_inside_markdown_fences_survive():
    code='```html\n<!-- This comment is part of the example -->\n<div>Hello</div>\n```'
    raw=('Reference explanation. '*30)+'\n'+code+'\n<!-- hidden editorial content -->'
    text=seed.normalize_document(raw,'text/markdown',SPEC,SPEC['url'])
    assert code in text and 'hidden editorial content' not in text


def test_large_code_reference_is_split_without_losing_lines(monkeypatch):
    monkeypatch.setattr(rag,'CHUNK_SIZE',110)
    lines=[f'    print("example {i}")' for i in range(150)]
    chunks=rag.chunk_markdown('```python\n'+'\n'.join(lines)+'\n```','guide.md')
    assert len(chunks)>2 and max(len(c['text']) for c in chunks)<600
    for chunk in chunks:
        assert chunk['text'].startswith('```python\n') and chunk['text'].endswith('```')
    restored=[line for chunk in chunks for line in chunk['text'].splitlines()[1:-1]]
    assert restored==lines
