import test from 'node:test';
import assert from 'node:assert/strict';
import {isOfficeFile,isSpreadsheetFile,documentAttachmentContext} from './documentFiles.js';

test('Office binary detection includes legacy and macro variants, not arbitrary archives',()=>{
  for(const name of ['report.DOCX','deck.pptx','old.doc','data.xls','macro.xlsm'])assert.equal(isOfficeFile(name),true);
  for(const name of ['text.txt','archive.zip','book.xlsx.exe'])assert.equal(isOfficeFile(name),false);
  assert.equal(isSpreadsheetFile('book.xlsx'),true);
  assert.equal(isSpreadsheetFile('report.docx'),false);
});
test('Office context retains identity and analysis path rather than decoded bytes',()=>{
  const text=documentAttachmentContext({name:'book.xlsx',artifactId:'art-123',sha256:'abcd',sandboxPath:'/root/chat_files/book.xlsx'});
  assert.match(text,/art-123/);assert.match(text,/abcd/);assert.match(text,/document_read/);assert.match(text,/execute_code/);
  assert.doesNotMatch(text,/undefined/);
});

import {documentArtifactId,documentRunIds,nextDocumentRevision} from './documentFiles.js';
test('document identity survives authenticated and absolute URLs',()=>{
  assert.equal(documentArtifactId('/api/documents/files/art-a?user_id=example'),'art-a');
  assert.equal(documentArtifactId('https://hyprchat.test/api/documents/files/art-a'),'art-a');
  assert.equal(documentArtifactId('/api/downloads/example.pdf'),null);
});
test('documents recover from historical run IDs and keep coding IDs separate',()=>{
  assert.deepEqual(documentRunIds({run_ids:['doc-a','run-b']}),['doc-a']);
  assert.deepEqual(documentRunIds({run_types:{'custom-id':'documents'},run_ids:['custom-id']}),['custom-id']);
});
test('preview follows only the selected lineage, including a PDF of its source',()=>{
  const events=[{type:'file_ready',data:{artifact_id:'other',supersedes_artifact_id:'unrelated'}},{type:'file_ready',data:{artifact_id:'b',supersedes_artifact_id:'a'}},{type:'file_ready',data:{artifact_id:'c',supersedes_artifact_id:'b'}}];
  assert.equal(nextDocumentRevision({id:'a'},events).artifact_id,'c');
  assert.equal(nextDocumentRevision({id:'pdf-a',parent_artifact_id:'a',metadata:{document_role:'preview'}},events).artifact_id,'c');
  assert.equal(nextDocumentRevision({id:'keep'},events),null);
});
