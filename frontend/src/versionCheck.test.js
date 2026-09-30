import test from 'node:test';
import assert from 'node:assert/strict';
import {entryScript,isStale,checkForNewVersion} from './versionCheck.js';

test('entry script name is read from index.html and compared with the loaded one',async()=>{
  const html='<script type="module" crossorigin src="/assets/index-DEpl22fZ.js"></script>';
  assert.equal(entryScript(html),'assets/index-DEpl22fZ.js');
  assert.equal(entryScript('<html></html>'),'');
  assert.equal(isStale('assets/index-OLD.js','assets/index-NEW.js'),true);
  assert.equal(isStale('assets/index-A.js','assets/index-A.js'),false);
  assert.equal(isStale('','assets/index-A.js'),false);
  // No document (node) and a failing fetch never report a stale tab.
  assert.equal(await checkForNewVersion(async()=>({ok:true,text:async()=>html})),false);
  assert.equal(await checkForNewVersion(async()=>{throw new Error('offline');}),false);
});
