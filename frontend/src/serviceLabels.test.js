import test from 'node:test';
import assert from 'node:assert/strict';
import {serviceLabel} from './serviceLabels.js';
test('historical health identity displays current runtime name',()=>{
  assert.equal(serviceLabel('codebox'),'Hephaestus');
  assert.equal(serviceLabel('hephaestus'),'Hephaestus');
  assert.equal(serviceLabel('ollama'),'Ollama');
  assert.equal(serviceLabel('custom-service'),'custom-service');
});
