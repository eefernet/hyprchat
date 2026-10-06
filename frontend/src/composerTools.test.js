import test from 'node:test';
import assert from 'node:assert/strict';
import {splitComposerTools,filterConnectorTools,connectorLabel,toolsTitle} from './composerTools.js';

const TOOLS=[
  {id:'codeagent',name:'⚡ CodeAgent',description:'Code tools'},
  {id:'deep_research',name:'🔬 Agent Research'},
  {id:'c1',name:'GitHub: list issues',description:'List repository issues',connector:true},
  {id:'quick_search',name:'⚡ Quick Search'},
  {id:'c2',name:'Jira: create ticket',description:'Open a ticket',connector:true},
];

test('splits tools from connector operations and keeps list order',()=>{
  const out=splitComposerTools(TOOLS,['quick_search','c2','codeagent']);
  assert.deepEqual(out.tools.map(t=>t.id),['codeagent','deep_research','quick_search']);
  assert.deepEqual(out.connectors.map(t=>t.id),['c1','c2']);
  assert.equal(out.activeCount,3);
  assert.deepEqual(out.activeNames,['⚡ CodeAgent','⚡ Quick Search','Jira: create ticket']);
});

test('counts only enabled ids that still name a tool, once each',()=>{
  const out=splitComposerTools(TOOLS,['quick_search','quick_search','deleted-tool']);
  assert.equal(out.activeCount,1);
  assert.deepEqual(out.activeNames,['⚡ Quick Search']);
});

test('tolerates missing inputs and entries without an id',()=>{
  assert.deepEqual(splitComposerTools(null,null),{tools:[],connectors:[],activeCount:0,activeNames:[]});
  const out=splitComposerTools([null,{name:'no id'},{id:'x'}],['x']);
  assert.deepEqual(out.tools.map(t=>t.id),['x']);
  assert.deepEqual(out.activeNames,['x']);
});

test('filters connector operations by name, description or id',()=>{
  const connectors=TOOLS.filter(t=>t.connector);
  assert.equal(filterConnectorTools(connectors,'').length,2);
  assert.equal(filterConnectorTools(connectors,'   ').length,2);
  assert.deepEqual(filterConnectorTools(connectors,'JIRA').map(t=>t.id),['c2']);
  assert.deepEqual(filterConnectorTools(connectors,'repository').map(t=>t.id),['c1']);
  assert.deepEqual(filterConnectorTools(connectors,'c1').map(t=>t.id),['c1']);
  assert.deepEqual(filterConnectorTools(connectors,'nothing'),[]);
  assert.deepEqual(filterConnectorTools(undefined,'x'),[]);
});

test('drops a short connector prefix from the row label only',()=>{
  assert.equal(connectorLabel('GitHub: list issues'),'list issues');
  assert.equal(connectorLabel('no prefix here'),'no prefix here');
  const long='A connector name well over twenty-eight characters: run';
  assert.equal(connectorLabel(long),long);
  assert.equal(connectorLabel(null),'');
});

test('title lists enabled tools and caps the list',()=>{
  assert.equal(toolsTitle([]),'Tools (none on)');
  assert.equal(toolsTitle(null),'Tools (none on)');
  assert.equal(toolsTitle(['a','b']),'Tools: a, b');
  assert.equal(toolsTitle(['a','b','c'],2),'Tools: a, b +1 more');
});
