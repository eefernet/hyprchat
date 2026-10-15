import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeResearchReport,researchTerminal,researchHeadings,linkResearchCitations,researchExportMarkdown} from './researchState.js';
test('stale polling cannot erase a newer snapshot or restore running state',()=>{
  const current={id:'r1',status:'running',report_markdown:'new',metrics:{snapshot_revision:4}};
  assert.equal(mergeResearchReport(current,{id:'r1',report_markdown:'old',metrics:{snapshot_revision:3}}),current);
  const done=mergeResearchReport(current,{...current,status:'partial'});
  assert.equal(mergeResearchReport(done,current),done);
  assert.ok(researchTerminal('partial'));
  assert.equal(mergeResearchReport(current,{id:'r2',report_markdown:'other'}).id,'r2');
});
test('citation groups link only known sources and preserve code',()=>{
  const source=[{index:1},{index:2}];
  assert.equal(linkResearchCitations('Fact [S1, S2] and [S999].',source),'Fact [S1](#research-source-S1) [S2](#research-source-S2) and [S999].');
  assert.equal(linkResearchCitations('`[S1]`\n```\n[S2]\n```',source),'`[S1]`\n```\n[S2]\n```');
});
test('navigation follows actual headings excluding code',()=>{
  assert.deepEqual(researchHeadings('# Title\n## Evidence\n```md\n## Fake\n```\n## Risks'),['Evidence','Risks']);
});
test('exports retain failure status and evidence limitations',()=>{
  const body='# Draft\n\nSaved text';
  assert.equal(researchExportMarkdown({status:'complete'},body),body);
  const exported=researchExportMarkdown({status:'partial',metrics:{quality:'limited',completion_checks:{issues:['Unknown citations: S99']}}},body);
  assert.ok(exported.includes(body));
  assert.ok(exported.includes('Report status: partial'));
  assert.ok(exported.includes('Unknown citations: S99'));
});
