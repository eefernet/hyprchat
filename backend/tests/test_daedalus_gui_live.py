"""Explicit live GUI qualification of the isolated session boundary (no inference)."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.getenv('DAEDALUS_LIVE_GUI') != '1' or sys.platform != 'linux',
                                reason='Requires the dedicated Linux GUI candidate worker')

QT = '''from PyQt6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLabel,QPushButton
app=QApplication([])
w=QWidget(); box=QVBoxLayout(w); label=QLabel('0')
button=QPushButton('Increment'); button.clicked.connect(lambda:label.setText(str(int(label.text())+1)))
box.addWidget(label); box.addWidget(button); w.show(); app.exec()
'''
GTK = '''import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gtk
app=Gtk.Application(application_id='test.daedalus.Counter')
def activate(app):
 w=Gtk.ApplicationWindow(application=app); box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
 label=Gtk.Label(label='0'); button=Gtk.Button(label='Increment')
 button.connect('clicked',lambda b:label.set_label(str(int(label.get_label())+1)))
 box.append(label); box.append(button); w.set_child(box); w.present()
app.connect('activate',activate); app.run([])
'''
SWING = '''import javax.swing.*; public class Counter { public static void main(String[] a) { SwingUtilities.invokeLater(()->{ JFrame w=new JFrame("Counter"); JPanel p=new JPanel(); JLabel l=new JLabel("0"); JButton b=new JButton("Increment"); b.addActionListener(e->l.setText(""+(Integer.parseInt(l.getText())+1))); p.add(l);p.add(b);w.add(p);w.pack();w.setVisible(true);w.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE); }); } }
'''


@pytest.mark.parametrize('persistent', [False, True])
def test_x_server_survives_last_readiness_client_disconnect(tmp_path, persistent):
    from coder_sandbox import Sandbox
    modules = Path(__file__).resolve().parents[1]
    driver = tmp_path/'display.py'
    driver.write_text('''import subprocess,sys,time
from pathlib import Path
from coder_ui_session import UISession
persistent=sys.argv[2]=='True'
original=UISession.spawn
class LegacyResetObserved(Exception):pass
def spawn(self,command,name):
 if name=='xvfb' and not persistent:command=[v for v in command if v!='-noreset']
 if name=='window-manager':
  # No manager or application is connected yet. The probe is the last client;
  # its disconnect must not reset the server between readiness and Openbox.
  subprocess.run(['xprop','-root','-f','DAEDALUS_READY_PROBE','8s','-set','DAEDALUS_READY_PROBE','retained'],env=self.env,check=True,capture_output=True)
  time.sleep(.2)
  probe=subprocess.check_output(['xprop','-root','DAEDALUS_READY_PROBE'],env=self.env,text=True)
  assert ('retained' in probe)==persistent,probe
  if not persistent:
   self.close()
   raise LegacyResetObserved()
 return original(self,command,name)
UISession.spawn=spawn
for index in range(12):
 try:session=UISession(Path(sys.argv[1])/str(index))
 except LegacyResetObserved:continue
 try:
  result=subprocess.check_output(['xprop','-root','_NET_SUPPORTING_WM_CHECK'],env=session.env,text=True)
  assert 'window id #' in result,result
 finally:session.close()
print('Twelve last-client cases; property retained='+str(persistent))
''')
    with Sandbox(['dbus-run-session','--',sys.executable,str(driver),str(tmp_path/'ui'),str(persistent)],
                 cwd=tmp_path, writable=[tmp_path], readonly=[modules/'coder_ui_session.py'],
                 environment={'PATH':os.environ['PATH'],'PYTHONPATH':str(modules)}) as box:
        result=subprocess.run(box.args,env=box.env,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout[-3000:]+result.stderr[-3000:]


@pytest.mark.parametrize('kind', ['web', 'qt', 'gtk', 'swing'])
@pytest.mark.parametrize('behavior', [1, 2])
def test_real_control_changes_visible_state(tmp_path, kind, behavior):
    from coder_sandbox import Sandbox
    root = tmp_path/'project'; root.mkdir()
    (root/'qt.py').write_text(QT); (root/'gtk.py').write_text(GTK); (root/'Counter.java').write_text(SWING)
    (root/'index.html').write_text('<button id="inc" onclick="count.textContent=Number(count.textContent)+1">Increment</button><output id="count">0</output>')
    command = {'web': 'python3 -m http.server 8000', 'qt': '/usr/bin/python3 qt.py',
               'gtk': '/usr/bin/python3 gtk.py', 'swing': 'javac Counter.java && java Counter'}[kind]
    modules = Path(__file__).resolve().parents[1]
    driver = tmp_path/'driver.py'
    driver.write_text('''import json,time,sys,subprocess,hashlib
from pathlib import Path
from coder_ui_session import UISession
session=UISession(sys.argv[1])
def pixels(window):
 # Capture the mapped X window using an already pinned toolkit; xwd is not
 # installed in the runtime. This reads pixels independently of AT-SPI text.
 code="from PyQt6.QtGui import QGuiApplication;import sys;app=QGuiApplication([]);image=app.primaryScreen().grabWindow(int(sys.argv[1],16)).toImage();assert not image.isNull();bits=image.bits();bits.setsize(image.sizeInBytes());sys.stdout.buffer.write(bytes(bits))"
 return subprocess.check_output(['/usr/bin/python3','-c',code,window],env=session.env)
try:
 result=session.perform(json.loads(sys.argv[2])); print(json.dumps(result),flush=True)
 if session.kind=='desktop' and session.behavior_version>=2:
  binding=result['display_binding']; assert binding['display']==session.env['DISPLAY'] and binding['windows'],result
  allowed={w['pid'] for w in binding['windows']}
  assert all(n['pid'] in allowed for n in result['nodes']),result
  window=binding['windows'][0]['window']
  before=pixels(window)
 # No delay or assertion between clicks: queued Qt animateClick activations
 # used to collapse into one. Never retry a mutating action in the test driver.
 for _ in range(5):
  session.perform({'action':'click','name':'Increment','role':'button'})
 deadline=time.monotonic()+5
 while True:
  result=session.perform({'action':'text','selector':'#count'}) if session.kind=='web' else session.perform({'action':'observe'})
  if result.get('text')=='5' or any(n.get('text')=='5' or n.get('name')=='5' for n in result.get('nodes',[])):break
  if time.monotonic()>deadline:raise AssertionError(result)
  time.sleep(.1)
 print(json.dumps(result),flush=True)
 if session.kind=='desktop':
  label=session.perform({'action':'text','name':'5','role':'label'})
  assert label['text']=='5',label
  if session.behavior_version>=2:
   after=pixels(window)
   assert hashlib.sha256(before).digest()!=hashlib.sha256(after).digest(),'Window pixels did not change'
finally:session.close()
''')
    launch = {'action': 'launch', 'kind': 'web' if kind == 'web' else 'desktop', 'command': command, 'toolkit': kind}
    with Sandbox(['dbus-run-session','--',sys.executable,str(driver),str(tmp_path/'ui'),json.dumps(launch)],
                 cwd=root, writable=[tmp_path], readonly=[modules/'coder_ui_session.py', modules/'coder_accessibility.py'],
                 environment={'PATH': os.environ['PATH'], 'PYTHONPATH':str(modules), 'DAEDALUS_ENGINE_BEHAVIOR_VERSION': str(behavior)}) as box:
        result = subprocess.run(box.args, env=box.env, capture_output=True, text=True, timeout=70)
    assert result.returncode == 0, result.stdout[-6000:]+result.stderr[-3000:]


@pytest.mark.parametrize('variant', ['good', 'wrong_total', 'ignored_percent', 'ignored_amount', 'hidden_error', 'hidden_total', 'native_error', 'broken_reset', 'missing_init'])
def test_literal_multi_input_contract_rejects_real_browser_defects(tmp_path, variant):
    import time
    import coder_ui_contract as contract
    from coder_ui_checks import run
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore
    from context_policy import DEFAULTS
    root = tmp_path/'source'; root.mkdir()
    calculate = 'a*(1+p/100)'
    if variant == 'wrong_total': calculate = 'a+p'
    if variant == 'ignored_percent': calculate = 'a'
    if variant == 'ignored_amount': calculate = 'p'
    reset = "document.querySelector('#amount').value=''; document.querySelector('#percent').value=''; total.textContent=''; error.textContent='';"
    if variant == 'broken_reset': reset = "total.textContent='';"
    html = '''<input id="amount"><input id="percent"><button id="calculate">Calculate</button>
<button id="reset">Reset</button><output id="total"></output><div id="error" STYLE></div>
<script type="module">export function init(){
const total=document.querySelector('#total'), error=document.querySelector('#error');
document.querySelector('#calculate').onclick=()=>{const a=Number(document.querySelector('#amount').value),p=Number(document.querySelector('#percent').value);
if(a<0||p<0){error.textContent='Error: negative values';return;}error.textContent='';total.textContent=(FORMULA).toFixed(2);};
document.querySelector('#reset').onclick=()=>{RESET};} INIT</script>'''
    html = html.replace('STYLE', 'style="display:none"' if variant == 'hidden_error' else '').replace('FORMULA', calculate).replace('RESET', reset).replace('INIT', '' if variant == 'missing_init' else 'init();')
    if variant == 'hidden_total': html = html.replace('id="total"', 'id="total" style="visibility:hidden"')
    if variant == 'native_error':
        html = '<form>' + html.replace('<input ', '<input type="number" min="0" ').replace('<button id="reset"', '<button type="button" id="reset"').replace('<script type="module">', '</form><script type="module">')
        html = html.replace("document.querySelector('#calculate').onclick=()=>{", "document.querySelector('form').onsubmit=(event)=>{event.preventDefault();")
    (root/'index.html').write_text(html)
    repo = Repository(root, tmp_path/'repository', DEFAULTS['daedalus_exclude_dirs']); repo.refresh()
    revision = repo.snapshot(variant)['revision']
    store = WorkerStore(tmp_path/'worker')
    task = ('Create a web app in index.html. Inputs with ids amount and percent, button Calculate, output id total. '
            'For amount 20 and percent 10, total displays exactly 22.00 without a currency symbol. '
            'Reject negative values with a visible error.')
    base = contract.freeze(task, mode='build_from_prompt')
    frozen = contract.freeze('Add a Reset button that clears the inputs and result.', {'ui_contract': base})
    store.create('op', 'job', 'check', {'revision_id': revision, 'settings': DEFAULTS, 'engine_behavior_version': 2,
        'seconds_remaining': 500, 'calls_remaining': 0, 'task': task})
    store.update('op', started=time.time())
    # A real but insufficient project test passes even with a missing init().
    # Controller expectations must still catch the broken behavior.
    flow = {'kind': 'web', 'file_path': 'index.html', 'steps': [
        {'action': 'fill', 'selector': '#amount', 'value': '20'},
        {'action': 'fill', 'selector': '#percent', 'value': '10'},
        {'action': 'click', 'name': 'Calculate'}, {'action': 'value', 'selector': '#amount', 'equals': '20'}]}
    checks = run(store, 'op', repo, [flow], origin='project_interaction')
    assert all(c['passed'] for c in checks), checks
    rows = contract.run(store, 'op', repo, frozen, contract.recipes(checks))
    supported = [r for r in rows if r['supported']]
    assert len(supported) == 6, rows
    if variant == 'good': assert all(r['status'] == 'passed' for r in supported), rows
    elif variant == 'native_error':
        assert all(r['status'] == ('unverified' if r.get('probe_kind') == 'negative_error' else 'passed') for r in supported), rows
    elif variant == 'hidden_total': assert any(r['status'] != 'passed' for r in supported), rows
    else: assert any(r['status'] == 'failed' for r in supported), rows
    if variant == 'hidden_error':
        assert all(r['status'] == 'failed' for r in supported if r.get('probe_kind') == 'negative_error'), rows


def test_controller_file_url_interaction_is_revision_bound(tmp_path):
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from context_policy import DEFAULTS
    from coder_ui_checks import run
    from coder_engine_ops import summarize
    import time
    root=tmp_path/'source';root.mkdir()
    (root/'index.html').write_text('<button onclick="count.textContent=Number(count.textContent)+1">Increment</button><output id="count">0</output>')
    repo=Repository(root,tmp_path/'repository',DEFAULTS['daedalus_exclude_dirs']);repo.refresh()
    revision=repo.snapshot('File browser fixture')['revision']
    store=WorkerStore(tmp_path/'worker')
    store.create('op','job','check',{'revision_id':revision,'settings':DEFAULTS,'seconds_remaining':120,'calls_remaining':0})
    store.update('op',started=time.time())
    rows=run(store,'op',repo,[{'kind':'web','file_path':'index.html','steps':[
        {'action':'click','name':'Increment'}, {'action':'text','selector':'#count','equals':'1'}]}],origin='project_interaction')
    assert summarize(rows,revision,['web'])['checks_passed'],rows
    assert rows[0]['service_bindings'][0]['file_path']=='index.html'
    rows=run(store,'op',repo,[{'kind':'web','file_path':'index.html','steps':[
        {'action':'click','name':'Increment'}, {'action':'text','selector':'#count','equals':'2'}]}],origin='project_interaction')
    assert not summarize(rows,revision,['web'])['checks_passed']


def test_controller_qt_label_text_accepts_only_the_observed_value(tmp_path):
    import time
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from context_policy import DEFAULTS
    from coder_ui_checks import run
    from coder_engine_ops import summarize
    root=tmp_path/'source';root.mkdir();(root/'app.py').write_text(QT)
    repo=Repository(root,tmp_path/'repository',DEFAULTS['daedalus_exclude_dirs']);repo.refresh()
    revision=repo.snapshot('Qt label text fixture')['revision']
    store=WorkerStore(tmp_path/'worker')
    store.create('op','job','check',{'revision_id':revision,'settings':DEFAULTS,'seconds_remaining':180,'calls_remaining':0})
    store.update('op',started=time.time())
    for expected,passed in [('1',True),('2',False)]:
        flow={'kind':'desktop','command':'/usr/bin/python3 app.py','toolkit':'qt','steps':[
            {'action':'click','name':'Increment'},
            {'action':'text','name':'1','role':'label','equals':expected}]}
        rows=run(store,'op',repo,[flow],origin='project_interaction')
        assert summarize(rows,revision,['desktop'])['checks_passed']==passed, rows
        if not passed:
            assert "Expected '2'; observed '1'" in rows[0]['reason']


def test_working_fixture_page_cannot_verify_broken_application(tmp_path):
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from context_policy import DEFAULTS
    from coder_ui_checks import run
    from coder_engine_ops import summarize
    import time
    root=tmp_path/'source';root.mkdir()
    (root/'index.html').write_text('<button>Increment</button><output id="count">0</output>')
    (root/'test-fixture.html').write_text('<button onclick="count.textContent=1">Increment</button><output id="count">0</output>')
    repo=Repository(root,tmp_path/'repository',DEFAULTS['daedalus_exclude_dirs']);repo.refresh()
    revision=repo.snapshot('Substitute browser fixture')['revision']
    store=WorkerStore(tmp_path/'worker')
    store.create('op','job','check',{'revision_id':revision,'settings':DEFAULTS,'seconds_remaining':120,'calls_remaining':0,
        'task':'Create index.html with a working Increment counter.'})
    store.update('op',started=time.time())
    flow={'kind':'web','file_path':'test-fixture.html','steps':[
        {'action':'click','name':'Increment'},{'action':'text','selector':'#count','equals':'1'}]}
    # Prove the substitute genuinely runs; it still cannot establish the requested entrypoint.
    assert run(store,'op',repo,[flow])[0]['passed']
    rejected=run(store,'op',repo,[flow],origin='project_interaction')
    assert not summarize(rejected,revision,['web'])['checks_passed']
    assert 'application entrypoint index.html' in rejected[0]['reason']
    actual=run(store,'op',repo,[{**flow,'file_path':'index.html'}],origin='project_interaction')
    assert not summarize(actual,revision,['web'])['checks_passed']


def test_browser_script_controller_url_binds_real_assertions(tmp_path):
    """The documented URL makes browser assertions observable, unlike a private server."""
    import time
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from coder_project_runtime import contract, run_revision
    from context_policy import DEFAULTS
    root=tmp_path/'source';root.mkdir()
    (root/'index.html').write_text('<button onclick="count.textContent=Number(count.textContent)+1">Increment</button><output id="count">0</output>')
    (root/'requirements.txt').write_text('playwright==1.62.0\n')
    script='''import functools,http.server,os,threading,unittest
from playwright.sync_api import sync_playwright
class BrowserTest(unittest.TestCase):
 def test_counter(self):
  url=USE_CONTROLLER
  server=None
  if not url:
   server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(http.server.SimpleHTTPRequestHandler,directory='.'))
   threading.Thread(target=server.serve_forever,daemon=True).start()
   url='http://127.0.0.1:'+str(server.server_port)
  try:
   with sync_playwright() as p:
    browser=p.chromium.launch()
    try:
     page=browser.new_page();page.goto(url);page.get_by_role('button',name='Increment').click()
     self.assertEqual(page.locator('#count').inner_text(),'1')
    finally:browser.close()
  finally:
   if server:server.shutdown();server.server_close()
'''
    repo=Repository(root,tmp_path/'repository',DEFAULTS['daedalus_exclude_dirs'])
    store=WorkerStore(tmp_path/'worker')
    for name,expression in [('private',"''"),('controller',"os.environ.get('DAEDALUS_APP_URL')")]:
        (root/'test_browser.py').write_text(script.replace('USE_CONTROLLER',expression))
        repo.refresh();revision=repo.snapshot(name+' browser endpoint')['revision']
        store.create(name,'job','check',{'engine':'openhands159','revision_id':revision,'settings':DEFAULTS,
            'seconds_remaining':300,'calls_remaining':0})
        store.update(name,started=time.time())
        result=run_revision(store,name,repo,contract(repo,managed_dependencies=True),project_id='browser-endpoint')
        tests=[row for row in result['checks'] if row.get('is_test')]
        assert len(tests)==1, result
        row=tests[0]
        assert row['exit_code']==0 and row['assertions_executed']>0, row
        if name=='private':
            assert not row['passed'] and row['classification']=='missing_provenance', row
        else:
            assert row['passed'] and row['execution_succeeded'], row
            assert row['service_bindings'][0]['revision_id']==revision
            assert 'browser' in row['test_interfaces']


@pytest.mark.parametrize('launch', ['export DISPLAY=:188; /usr/bin/python3 qt.py',
    'QT_QPA_PLATFORM=offscreen /usr/bin/python3 qt.py',
    'Xvfb :188 -screen 0 800x600x24 & export DISPLAY=:188; /usr/bin/python3 qt.py'])
def test_script_cannot_move_desktop_to_another_display(tmp_path, launch):
    from coder_sandbox import Sandbox
    root = tmp_path/'project'; root.mkdir()
    (root/'qt.py').write_text(QT); (root/'launch.sh').write_text(launch)
    modules = Path(__file__).resolve().parents[1]
    driver = tmp_path/'driver.py'
    driver.write_text('''import sys,json
from coder_ui_session import UISession,UIFailure
session=UISession(sys.argv[1])
try:
 try:session.perform({'action':'launch','kind':'desktop','command':'bash launch.sh','toolkit':'qt'})
 except UIFailure as error:
  assert error.details['category'] in {'desktop_display_mismatch','application_startup'},error.details
  print(json.dumps(error.details))
 else:raise AssertionError('A different/offscreen display was accepted')
finally:session.close()
''')
    with Sandbox(['dbus-run-session','--',sys.executable,str(driver),str(tmp_path/'ui')],
                 cwd=root,writable=[tmp_path],readonly=[modules/'coder_ui_session.py',modules/'coder_accessibility.py'],
                 environment={'PATH':os.environ['PATH'],'PYTHONPATH':str(modules),'DAEDALUS_ENGINE_BEHAVIOR_VERSION':'2'}) as box:
        result=subprocess.run(box.args,env=box.env,capture_output=True,text=True,timeout=70)
    assert result.returncode==0,result.stdout[-5000:]+result.stderr[-2000:]


def test_legacy_accessibility_can_see_a_window_absent_from_preview_display(tmp_path):
    from coder_sandbox import Sandbox
    root=tmp_path/'project';root.mkdir();(root/'qt.py').write_text(QT)
    modules=Path(__file__).resolve().parents[1];driver=tmp_path/'driver.py'
    driver.write_text('''import sys,json
from coder_ui_session import UISession
session=UISession(sys.argv[1])
try:
 observed=session.perform({'action':'launch','kind':'desktop','toolkit':'qt',
  'command':'Xvfb :188 -screen 0 800x600x24 & export DISPLAY=:188; sleep .3; /usr/bin/python3 qt.py'})
 assert any(n.get('name')=='Increment' and n.get('showing') for n in observed['nodes']),observed
 session.perform({'action':'click','name':'Increment'})
 assert session.perform({'action':'text','name':'1','role':'label'})['text']=='1'
 binding=session.desktop_binding();assert not binding['windows'],binding
 print(json.dumps({'legacy_interaction_passed':True,'preview_display_binding':binding}))
finally:session.close()
''')
    with Sandbox(['dbus-run-session','--',sys.executable,str(driver),str(tmp_path/'ui')],
                 cwd=root,writable=[tmp_path],readonly=[modules/'coder_ui_session.py',modules/'coder_accessibility.py'],
                 environment={'PATH':os.environ['PATH'],'PYTHONPATH':str(modules),'DAEDALUS_ENGINE_BEHAVIOR_VERSION':'1'}) as box:
        result=subprocess.run(box.args,env=box.env,capture_output=True,text=True,timeout=70)
    assert result.returncode==0,result.stdout[-5000:]+result.stderr[-2000:]


def test_jsdom_provenance_and_default_command_selection(tmp_path):
    import time
    from coder_worker_runtime import WorkerStore
    from coder_repository import Repository
    from coder_project_runtime import contract, run_revision
    from context_policy import DEFAULTS
    root=tmp_path/'source';root.mkdir()
    (root/'index.html').write_text('<button onclick="total.textContent=(20*1.1).toFixed(2)">Calculate</button><output id="total"></output>')
    (root/'jsdom.test.cjs').write_text("""const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
test('real HTML',()=>{const dom=new JSDOM(fs.readFileSync('index.html','utf8'),{runScripts:'dangerously'});
dom.window.document.querySelector('button').click();assert.equal(dom.window.document.querySelector('output').textContent,'22.00');dom.window.close();});""")
    (root/'copied.test.cjs').write_text("const {test}=require('node:test'),assert=require('node:assert/strict');test('copied calculation',()=>assert.equal((20*1.1).toFixed(2),'22.00'));")
    browser = """const {test}=require('node:test'),assert=require('node:assert/strict'),{chromium}=require('playwright');
test('real browser',async()=>{const browser=await chromium.launch();try{const page=await browser.newPage();await page.goto(process.env.DAEDALUS_APP_URL);await page.getByRole('button',{name:'Calculate'}).click();assert.equal(await page.locator('#total').innerText(),'22.00');}finally{await browser.close();}});"""
    package={'scripts':{'test':'node --test jsdom.test.cjs'},'devDependencies':{'jsdom':'26.1.0','playwright':'1.62.0'}}
    repo=Repository(root,tmp_path/'repository',DEFAULTS['daedalus_exclude_dirs']);store=WorkerStore(tmp_path/'worker')
    for name,selected in [('jsdom','jsdom'),('added_browser','jsdom'),('copied','copied'),('selected_browser','browser')]:
        if name=='added_browser':(root/'browser.test.cjs').write_text(browser)
        package['scripts']['test']='node --test '+selected+'.test.cjs'
        (root/'package.json').write_text(json.dumps(package))
        repo.refresh();revision=repo.snapshot(name)['revision']
        store.create(name,'job','check',{'engine':'openhands159','revision_id':revision,'settings':DEFAULTS,'seconds_remaining':600,'calls_remaining':0})
        store.update(name,started=time.time())
        result=run_revision(store,name,repo,contract(repo,managed_dependencies=True),project_id='provenance-controls')
        rows=[row for row in result['checks'] if row.get('is_test')]
        assert len(rows)==1,result
        row=rows[0]
        assert row['exit_code']==0 and row['assertions_executed']>0,row
        assert row['selected_test_runner']==package['scripts']['test'],row
        if name=='selected_browser':
            assert row['passed'] and row['execution_succeeded'] and row['service_bindings'],row
        else:
            assert not row['passed'] and row['failure_kind']=='missing_provenance',row
            assert row['provenance_diagnostic']['jsdom_execution_observed'] is (selected=='jsdom'),row


@pytest.mark.parametrize('variant', ['good', 'wrong_format', 'wrong_input_name', 'wrong_error', 'broken_clear'])
def test_named_desktop_input_format_error_and_clear(tmp_path, variant):
    import time
    import coder_ui_contract as contract
    from coder_ui_checks import run
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore
    from context_policy import DEFAULTS
    root=tmp_path/'source';root.mkdir()
    source='''from PyQt6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLineEdit,QLabel,QPushButton
app=QApplication([]);w=QWidget();box=QVBoxLayout(w);field=QLineEdit();field.setAccessibleName(INPUT_NAME)
label=QLabel('Ready');convert=QPushButton('Convert');clear=QPushButton('Clear')
def calculate():
 try:label.setText(FORMAT.format(float(field.text())*9/5+32))
 except ValueError:label.setText(ERROR)
def reset():
 CLEAR
 label.setText('Ready')
convert.clicked.connect(calculate);clear.clicked.connect(reset)
for node in (field,convert,clear,label):box.addWidget(node)
w.show();app.exec()
'''.replace('INPUT_NAME',repr('Wrong' if variant=='wrong_input_name' else 'Celsius')).replace('FORMAT',repr('F: {:.1f}' if variant=='wrong_format' else '{:.1f}')).replace('ERROR',repr('Oops' if variant=='wrong_error' else 'Error')).replace('CLEAR','pass' if variant=='broken_clear' else 'field.clear()')
    (root/'app.py').write_text(source)
    repo=Repository(root,tmp_path/'repository',DEFAULTS['daedalus_exclude_dirs']);repo.refresh();revision=repo.snapshot(variant)['revision']
    task=('Create a PyQt6 Celsius-to-Fahrenheit desktop application in app.py, runnable with /usr/bin/python3 app.py. '
          'A QLineEdit named Celsius, a Convert button, and a result QLabel that displays only the numeric Fahrenheit value to one decimal place, without units or a prefix. '
          'Give the input accessibleName Celsius. Keep the result label accessible name equal to its visible text. Show Error for invalid numbers.')
    frozen=contract.freeze('Add a Clear button that clears the input and sets the result label to Ready.',
                           {'ui_contract':contract.freeze(task,mode='build_from_prompt')})
    store=WorkerStore(tmp_path/'worker');store.create('op','job','check',{'revision_id':revision,'settings':DEFAULTS,
        'engine_behavior_version':2,'seconds_remaining':400,'calls_remaining':0,'task':task});store.update('op',started=time.time())
    flow={'kind':'desktop','command':'/usr/bin/python3 app.py','toolkit':'qt','steps':[
        {'action':'fill','name':'Wrong' if variant=='wrong_input_name' else 'Celsius','value':'25'},
        {'action':'click','name':'Convert'},
        {'action':'value','name':'Wrong' if variant=='wrong_input_name' else 'Celsius','equals':'25'}]}
    checks=run(store,'op',repo,[flow],origin='project_interaction');assert all(c['passed'] for c in checks),checks
    rows=contract.run(store,'op',repo,frozen,contract.recipes(checks))
    supported=[r for r in rows if r['supported']]
    assert len(supported)==5,rows
    assert all(r['status']=='passed' for r in supported) is (variant=='good'),rows
    # The QLabel Name fallback is not independent visible-text evidence.
    assert any('AT-SPI' in r.get('reason','') and r['status']=='unverified' for r in rows),rows
