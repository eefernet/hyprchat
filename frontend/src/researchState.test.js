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

test('a source count past its target is not shown as a fraction', async () => {
  const { targetTile } = await import('./researchState.js');
  assert.deepEqual(targetTile('Sources', 12, 48), ['Sources', '12/48']);
  assert.deepEqual(targetTile('Sources', 48, 48), ['Sources', '48/48']);
  assert.deepEqual(targetTile('Sources', 59, 48), ['Sources · target 48', '59']);
  assert.deepEqual(targetTile('Pages', 7, 0), ['Pages', '7']);
  assert.deepEqual(targetTile('Searches', undefined, 10), ['Searches', '0/10']);
});

test('a report card counts sources from a list row and from a full record', async () => {
  const { reportSourceCount } = await import('./researchState.js');
  assert.equal(reportSourceCount({ source_count: 59 }), 59);
  assert.equal(reportSourceCount({ metrics: { source_count: 59 }, sources: [] }), 59);
  assert.equal(reportSourceCount({ sources: [{}, {}, {}] }), 3);
  assert.equal(reportSourceCount({}), 0);
});

test('a report card line skips the Method section of older summaries', async () => {
  const { reportCardSummary } = await import('./researchState.js');
  assert.equal(reportCardSummary({ summary: '## Method Collected 59 source records using 12 searches. ## Key Findings ZFS **snapshots** are cheap.' }),
    'Key Findings ZFS snapshots are cheap.');
  assert.equal(reportCardSummary({ summary: '## Method Collected 59 source records', query: 'zfs snapshots' }), 'zfs snapshots');
  assert.equal(reportCardSummary({ summary: 'Plain summary.' }), 'Plain summary.');
  assert.equal(reportCardSummary({ query: 'q' }), 'q');
});

test('opening a report keeps its place in the list; a new report goes on top', async () => {
  const { putReport } = await import('./researchState.js');
  const list = [{ id: 'a', title: 'A' }, { id: 'b', title: 'B' }, { id: 'c', title: 'C' }];
  assert.deepEqual(putReport(list, { id: 'b', title: 'B2' }).map(r => r.title), ['A', 'B2', 'C']);
  assert.deepEqual(putReport(list, { id: 'n', title: 'New' }).map(r => r.id), ['n', 'a', 'b', 'c']);
  assert.deepEqual(putReport(null, { id: 'n' }), [{ id: 'n' }]);
});

test('an interrupted report is finished, not running', () => {
  assert.equal(researchTerminal('interrupted'), true);
  assert.equal(researchTerminal('running'), false);
});

