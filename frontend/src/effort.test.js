import test from 'node:test';
import assert from 'node:assert/strict';
import {effortChoices,effortRequest,nativeChoice,reviewLevel} from './effort.js';

test('four native values preserve false and use each branded level exactly once',()=>{
  const cap={mode:'native',values:[false,'low','medium','xhigh'],default:'medium'};
  const choices=effortChoices(cap);
  assert.deepEqual(choices.map(c=>c.name),['Blurt','Ponder','Forge','Galaxy Brain']);
  assert.equal(choices[2].isDefault,true);
  assert.equal(effortRequest('qwen',cap,false,3).reasoning_level,false);
});
test('three native levels omit Forge; unknown and boolean models retain reviews',()=>{
  assert.deepEqual(effortChoices({mode:'native',values:['low','medium','high']}).map(c=>c.name),['Blurt','Ponder','Galaxy Brain']);
  assert.deepEqual(effortChoices({mode:'review',values:[false,true]}).map(c=>c.value),[0,1,2,3]);
  assert.equal(effortRequest('local',null,null,2).effort_rounds,2);
});
test('additional native levels are all reachable and stale preferences do not crash',()=>{
  const cap={mode:'native',values:['low','medium','high','xhigh','max','ultra']};
  assert.equal(effortChoices(cap).length,6);
  assert.equal(nativeChoice(cap,'removed').value,'low');
  assert.equal(reviewLevel(99),0);
});
