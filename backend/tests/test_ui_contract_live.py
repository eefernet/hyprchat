"""Fault fixtures use real GUI sessions, including a false project-test verdict."""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.getenv('DAEDALUS_LIVE_GUI') != '1' or sys.platform != 'linux',
                                reason='Requires the isolated Linux GUI worker')

WEB = '''<label for="item">Item</label><input id="item"><output id="result"></output>
<button id="set">Set</button><button id="reset">Reset</button>
<script>document.querySelector('#set').onclick=()=>{result.textContent='filled'};
document.querySelector('#reset').onclick=()=>{item.value='';result.textContent=EXPECTED};</script>'''
QT = '''from PyQt6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLineEdit,QLabel,QPushButton
app=QApplication([]);w=QWidget();b=QVBoxLayout(w);i=QLineEdit();i.setAccessibleName('Item');r=QLabel('')
s=QPushButton('Set');s.clicked.connect(lambda:r.setText('filled'))
z=QPushButton('Reset');z.clicked.connect(lambda:(i.clear(),r.setText(EXPECTED)))
for c in [i,r,s,z]:b.addWidget(c)
w.show();app.exec()
'''
GTK = '''import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gtk
app=Gtk.Application(application_id='test.daedalus.Reset')
def activate(app):
 w=Gtk.ApplicationWindow(application=app);b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL);i=Gtk.Entry();i.update_property([Gtk.AccessibleProperty.LABEL],['Item']);r=Gtk.Label(label='')
 s=Gtk.Button(label='Set');s.connect('clicked',lambda _:r.set_label('filled'))
 z=Gtk.Button(label='Reset');z.connect('clicked',lambda _:(i.set_text(''),r.set_label(EXPECTED)))
 for c in [i,r,s,z]:b.append(c)
 w.set_child(b);w.present()
app.connect('activate',activate);app.run([])
'''
SWING = '''import javax.swing.*;public class ResetApp{public static void main(String[] args){SwingUtilities.invokeLater(()->{
JFrame w=new JFrame("Reset");JPanel p=new JPanel();JTextField i=new JTextField(10);i.getAccessibleContext().setAccessibleName("Item");JLabel r=new JLabel("");
JButton s=new JButton("Set");s.addActionListener(e->r.setText("filled"));JButton z=new JButton("Reset");z.addActionListener(e->{i.setText("");r.setText(EXPECTED);});
p.add(i);p.add(r);p.add(s);p.add(z);w.add(p);w.pack();w.setVisible(true);w.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
});}}'''


@pytest.mark.parametrize('kind', ['file', 'served', 'qt', 'gtk', 'swing'])
def test_real_contract_rejects_zero_then_accepts_empty(tmp_path, kind):
    import json
    from context_policy import DEFAULTS
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore
    from coder_ui_checks import run
    from coder_ui_contract import freeze, recipes, bindings, run as run_contract, apply_summary
    from coder_engine_ops import summarize
    root = tmp_path/'source'; root.mkdir()
    web = kind in {'file', 'served'}
    filename = 'index.html' if web else 'ResetApp.java' if kind == 'swing' else 'app.py'
    source = WEB if web else {'qt': QT, 'gtk': GTK, 'swing': SWING}[kind]
    launch = {'kind': 'web', 'file_path': 'index.html'} if kind == 'file' else {
        'kind': 'web' if web else 'desktop', 'toolkit': kind,
        'command': 'python3 -m http.server 8000' if web else 'javac ResetApp.java && java ResetApp' if kind == 'swing' else '/usr/bin/python3 app.py'}
    steps = [{'action': 'fill', **({'selector': '#item'} if web else {'name': 'Item'}), 'value': 'seed'},
             {'action': 'value', **({'selector': '#item'} if web else {'name': 'Item'}), 'equals': 'seed'},
             {'action': 'click', 'name': 'Set'},
             {'action': 'text', **({'selector': '#result'} if web else {'name': 'filled', 'role': 'label'}), 'equals': 'filled'}]
    repo = Repository(root, tmp_path/'repository', DEFAULTS['daedalus_exclude_dirs'])
    store = WorkerStore(tmp_path/'worker')
    specification = freeze('Add a Reset button that clears the input and result.', mode='build_from_prompt')
    for phase, expected in [('broken', '0.00'), ('fixed', ''), ('literal', 'Ready')]:
        if phase == 'literal':
            specification = freeze('Make Reset set the result to "Ready".')
        (root/filename).write_text(source.replace('EXPECTED', json.dumps(expected)))
        # The generated test declares the wrong value correct. This is real
        # executed evidence, but it cannot replace the user's expected value.
        tampered = {**launch, 'steps': steps + [
            {'action': 'click', 'name': 'Reset'},
            {'action': 'text', **({'selector': '#result'} if web else {'name': expected, 'role': 'label'}), 'equals': expected}]}
        (root/'.daedalus-ui.json').write_text(json.dumps([tampered]))
        repo.refresh(); revision = repo.snapshot(phase)['revision']
        store.create(phase, 'job', 'check', {'revision_id': revision, 'settings': DEFAULTS, 'engine_behavior_version':1,
            'seconds_remaining': 300, 'calls_remaining': 0, 'task': 'Create '+filename})
        store.update(phase, started=time.time())
        flows = run(store, phase, repo, [tampered], origin='project_interaction')
        assert flows[0]['passed'], flows
        criterion_rows = run_contract(store, phase, repo, specification, recipes(flows), bindings(specification, flows))
        assert [c['status'] for c in criterion_rows] == (['passed'] if phase == 'literal' else ['passed', 'failed' if phase == 'broken' else 'passed']), criterion_rows
        summary = summarize(flows, revision, [launch['kind']]); apply_summary(summary, criterion_rows)
        assert summary['checks_ok']  # The actual project test did pass.
        assert summary['checks_passed'] == (phase != 'broken')
        assert all(e['revision_id'] == revision for c in criterion_rows for e in c['evidence'])


def test_qt_failed_launch_and_locator_repair_in_same_session(tmp_path):
    from context_policy import DEFAULTS
    from coder_repository import Repository
    from coder_worker_runtime import WorkerStore
    from coder_ui_checks import session, UIObservationFailure
    root=tmp_path/'source';root.mkdir()
    (root/'app.py').write_text(QT.replace('EXPECTED',"''"))
    repo=Repository(root,tmp_path/'repository',DEFAULTS['daedalus_exclude_dirs']);repo.refresh()
    revision=repo.snapshot('qt recovery')['revision'];store=WorkerStore(tmp_path/'worker')
    store.create('qt-recovery','job','check',{'revision_id':revision,'settings':DEFAULTS,'engine_behavior_version':1,
        'seconds_remaining':300,'calls_remaining':0,'task':'Create app.py'})
    store.update('qt-recovery',started=time.time())
    with session(store,'qt-recovery',repo) as action:
        with pytest.raises(UIObservationFailure) as caught:
            action({'action':'launch','kind':'desktop','toolkit':'qt','command':'/usr/bin/python3 missing.py'})
        assert caught.value.details['category']=='application_startup' and 'missing.py' in caught.value.details['log_tail']
        observed=action({'action':'launch','kind':'desktop','toolkit':'qt','command':'/usr/bin/python3 app.py'})
        with pytest.raises(UIObservationFailure) as caught:
            action({'action':'fill','name':'celsius_input','role':'lineedit','value':'21'})
        d=caught.value.details
        assert d['category']=='target_not_found' and any(c['name']=='Item' and c['editable'] for c in d['candidates'])
        target=next(n for n in observed['nodes'] if n['name']=='Item' and n['editable'])
        with pytest.raises(UIObservationFailure) as caught:
            action({'action':'value','selector':target['id'],'session_id':'previous-session'})
        assert caught.value.details['category']=='stale_session'
        action({'action':'fill','name':target['name'],'role':target['role'],'value':'21'})
        assert action({'action':'value','name':target['name'],'role':target['role']})['value']=='21'
        action({'action':'click','name':'Reset'})
        assert action({'action':'value','name':target['name'],'role':target['role']})['value']==''
