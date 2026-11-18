import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeDraft } from './assistantDraft.js';

const saved = { name: 'Jarvis', personality: 'Dry.', quiet_hours: { enabled: false } };

test('an untouched form follows the server', () => {
  const fresh = { ...saved, name: 'Friday' };
  assert.equal(mergeDraft(saved, saved, fresh), fresh);
  assert.equal(mergeDraft({ ...saved }, { ...saved }, fresh), fresh);
  assert.equal(mergeDraft(null, null, fresh), fresh);
});

test('unsaved edits survive a reload on window focus', () => {
  const typing = { ...saved, personality: 'Dry, and a little long-winded about ZFS.' };
  assert.equal(mergeDraft(typing, saved, { ...saved }), typing);
  assert.equal(mergeDraft({ ...saved, quiet_hours: { enabled: true } }, saved, saved).quiet_hours.enabled, true);
});
