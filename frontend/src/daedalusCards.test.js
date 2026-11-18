import assert from 'node:assert/strict';
import test from 'node:test';
import { hasJobCard, loadingJob, messageJobIds, ownEvents, ownsJob, startsJob } from './daedalusCards.js';
import { jobStatusLabel, terminalJobStates } from './daedalusJobs.js';

const job = (id, origin, extra = {}) => ({ id, workflow_version: 3, state: 'coding', origin_message_id: origin, ...extra });
const delivered = id => ({ type: 'file_ready', data: { filename: 'app.zip', workflow_id: id } });
const output = stdout => ({ type: 'code_output', data: { stdout } });

test("a background job's delivery does not put its card on a later reply", () => {
  // Found 2026-11-13: job cw3-a (started by message 10) delivered while message 14 was the newest reply.
  // Message 14 showed a second copy of the job card and lost its own code output.
  const workflows = [job('cw3-a', 10)];
  const live = [output('2 + 2 = 4'), delivered('cw3-a')];
  assert.deepEqual(messageJobIds({ messageId: 14, metadata: {}, liveEvents: live, workflows }), []);
  assert.deepEqual(ownEvents(live, workflows, 14), [live[0]]);
  // The reply that started the job keeps both, whichever way the id reaches it.
  assert.deepEqual(messageJobIds({ messageId: 10, metadata: {}, liveEvents: live, workflows }), ['cw3-a']);
  assert.deepEqual(messageJobIds({ messageId: '10', metadata: {}, liveEvents: [], workflows }), ['cw3-a']);
  assert.equal(ownEvents(live, workflows, 10), live);
});

test('a copy that was already saved into the wrong reply is ignored after a reload', () => {
  const workflows = [job('cw3-a', 10)];
  const metadata = { workflow_ids: ['cw3-a'], saved_events: [output('mine'), delivered('cw3-a')] };
  assert.deepEqual(messageJobIds({ messageId: 14, metadata, workflows }), []);
  assert.deepEqual(ownEvents(metadata.saved_events, workflows, 14).map(event => event.type), ['code_output']);
});

test('a job that just started, or whose origin is not recorded, stays with the reply that saw it', () => {
  const live = [{ type: 'tool_start', data: { tool: 'start_coder_workflow', workflow_id: 'cw3-new' } }];
  assert.deepEqual(messageJobIds({ messageId: 14, liveEvents: live, workflows: [] }), ['cw3-new']);            // list not refreshed yet
  assert.deepEqual(messageJobIds({ messageId: 14, liveEvents: live, workflows: [job('cw3-new', null)] }), ['cw3-new']);
  assert.deepEqual(messageJobIds({ messageId: null, liveEvents: live, workflows: [job('cw3-new', 9)] }), ['cw3-new']); // own id unknown
  assert.equal(ownsJob(undefined, 14), true);
  assert.equal(ownsJob(job('x', 9), 14), false);
  assert.equal(ownEvents(live, [], 14), live);
});

test('a job the list has not loaded is a placeholder, not a running job', () => {
  // The placeholder used to be {state:'queued'}: "Planning" with a Stop button, and it armed the workbench auto-open.
  const placeholder = loadingJob('cw3-a');
  assert.equal(terminalJobStates.has(placeholder.state), true);
  assert.equal(jobStatusLabel(placeholder), 'Loading');
});

test('a running project question is not labelled as a problem or as planning', () => {
  assert.equal(jobStatusLabel({ state: 'answering' }), 'Answering');           // was "Needs attention"
  assert.equal(jobStatusLabel({ state: 'queued', mode: 'ask_uploaded_project' }), 'Answering');
  assert.equal(jobStatusLabel({ state: 'queued', mode: 'edit_project' }), 'Planning');
  assert.equal(jobStatusLabel({ state: 'verifying' }), 'Checking');
});

test('a reply is drawn as Daedalus output only when there is a card to draw', () => {
  // A project question asked while a job runs is refused: tool events, no job, no run. The reply was hidden.
  assert.equal(hasJobCard({ workflows: [], runIds: [] }), false);
  assert.equal(hasJobCard({ workflows: [job('cw3-a', 10)] }), true);
  assert.equal(hasJobCard({ runIds: ['run-1'] }), true);
});

test('Regenerate and Edit are withheld from the turn that started a job', () => {
  const messages = [
    { id: 1, role: 'user', content: 'What is a closure?' }, { id: 2, role: 'assistant', content: 'A function…' },
    { id: 3, role: 'user', content: 'Build the board' }, { id: 4, role: 'assistant', content: '' },
    { id: 5, role: 'user', content: 'thanks' }, { id: 6, role: 'assistant', content: 'Welcome', metadata: '{"workflow_ids":["cw3-b"]}' },
  ];
  const workflows = [job('cw3-a', 4), job('cw3-b', 6, { state: 'completed' }), { id: 'legacy', workflow_version: 2, origin_message_id: 2 }];
  assert.deepEqual(messages.map((_, index) => startsJob(messages, index, workflows)), [false, false, true, true, true, true]);
  assert.equal(startsJob(messages, 9, workflows), false);
  assert.equal(startsJob([], 0, workflows), false);
});
