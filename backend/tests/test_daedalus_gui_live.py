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


@pytest.mark.parametrize('kind', ['web', 'qt', 'gtk', 'swing'])
def test_real_control_changes_visible_state(tmp_path, kind):
    from coder_sandbox import Sandbox
    root = tmp_path/'project'; root.mkdir()
    (root/'qt.py').write_text(QT); (root/'gtk.py').write_text(GTK); (root/'Counter.java').write_text(SWING)
    (root/'index.html').write_text('<button id="inc" onclick="count.textContent=Number(count.textContent)+1">Increment</button><output id="count">0</output>')
    command = {'web': 'python3 -m http.server 8000', 'qt': '/usr/bin/python3 qt.py',
               'gtk': '/usr/bin/python3 gtk.py', 'swing': 'javac Counter.java && java Counter'}[kind]
    modules = Path(__file__).resolve().parents[1]
    driver = tmp_path/'driver.py'
    driver.write_text('''import json,time,sys
from pathlib import Path
from coder_ui_session import UISession
session=UISession(sys.argv[1])
try:
 result=session.perform(json.loads(sys.argv[2])); print(json.dumps(result),flush=True)
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
finally:session.close()
''')
    launch = {'action': 'launch', 'kind': 'web' if kind == 'web' else 'desktop', 'command': command, 'toolkit': kind}
    with Sandbox(['dbus-run-session','--',sys.executable,str(driver),str(tmp_path/'ui'),json.dumps(launch)],
                 cwd=root, writable=[tmp_path], readonly=[modules/'coder_ui_session.py', modules/'coder_accessibility.py'],
                 environment={'PATH': os.environ['PATH'], 'PYTHONPATH':str(modules)}) as box:
        result = subprocess.run(box.args, env=box.env, capture_output=True, text=True, timeout=70)
    assert result.returncode == 0, result.stdout[-6000:]+result.stderr[-3000:]


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
