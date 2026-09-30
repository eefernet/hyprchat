"""Controller-owned browser workflows the request asks for, run after the form probe passed. Diagnostic only.

The frozen-6/7 Kanban builds (2026-09-24) passed page load and form submission, then failed the user's real
workflow: Edit called a route the server never implemented (404), drag sent a status-only PUT the API
rejected (400), and a second build threw at appendChild while dragging. The expense tracker rendered
`<b>travel</b>` as bold. None of that is visible to `page:*` (load) or `form:*` (one submit). This guard
runs the workflows the request names, in the disposable probe service, and FAILS a step on exactly three
grounds: an uncaught page error, a write the server rejected (4xx/5xx), or an explicit contradiction of the
requested behavior (text gone after reload, a deleted item still shown, literal markup rendered as an
element, an edit that added a duplicate). A control it cannot locate is advisory. Direct URL only, never
the traffic proxy, so it can never manufacture browser evidence; a browser that cannot start is an
environment fault, never a pass.

When the request names accessible labels or buttons ("Use accessible labels Project name, Task title ...
and buttons Create project and Add task"), the item is created through exactly those controls
(get_by_label / get_by_role(button, name)) and `interface:labels:<service>` FAILS when any named label or
button is never found: the medium board (2026-09-26) failed every browser verdict at
get_by_label('Project name') while its own tests and audits never used a label, and the workflow rows
stayed advisory because no item could be created through the form.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from coder_frontend_guard import _fill as fill_form, PROBE_TEXT, PROBE_VALUES

MARKER = 'daedalus workflow item'
EDITED = 'daedalus edited item'
LITERAL = '<b>daedalus-literal</b>'
WORDING = [
    ('persist', r'\bpersist|\breload|localStorage|\bsurviv|\brestart|\bSQLite\b|\bdatabase\b|\bsaved?\b'),
    ('edit', r'\bedit(?:ing|ed|s|able)?\b|\bupdate (?:a|an|the|existing|tasks?|items?)\b'),
    ('drag', r'\bdrag(?:-| )?(?:and|&|n)?(?:-| )?drop\b|\bdraggable\b|\bdrag\b'),
    ('delete', r'\bdelet(?:e|ing|ed|es)\b|\bremov(?:e|ing|ed|es)\b'),
    ('literal', r'\bliteral(?:ly)?\b|\bescap(?:e|ed|ing)\b|\bsanitiz'),
]
DROP_ZONE = '[data-status], [ondrop], .task-list, .tasks, .task-container, .cards, .card-list, ul, ol'


def creation_failure(created, create_errors, create_writes=()):
    """(status, detail) when no item could be created AND the attempt threw an uncaught page error or sent a write the
    server rejected — the two hard grounds the per-step loop already uses. fix5 Kanban p1 (2026-09-27): the submit
    handler threw `taskIdInput is not defined`; fix5 medium p1: the task POST was rejected once a project existed but was
    not selected. Both left every workflow row advisory because creation ran before the steps."""
    if created:
        return None
    rejected = [w for w in create_writes or () if w.get('status', 0) >= 400]
    if create_errors:
        return 'failed', ('creating an item through the page form threw an uncaught page error: ' + ' | '.join(e[:200] for e in create_errors[:2])
                          + '. The create handler must run without errors and persist the item; until it does, no requested workflow can be exercised.')
    if rejected:
        return 'failed', ('creating an item through the page form sent a request the server rejected: '
                          + ', '.join(f"{w.get('method')} {w.get('path')} -> {w.get('status')}" for w in rejected[:3])
                          + '. Read the server\'s error: the form must send what the API validates (a selected project, required fields), '
                          'and a newly created parent must be selected for the next form when the request says so.')
    return None


SCOPED_CONTROL_JS = """(buttons, [marker, edited]) => buttons.findIndex(b => {
    let el = b;
    for (let i = 0; i < 8 && el; i++) {
        el = el.parentElement;
        if (!el) break;
        const text = el.textContent || '';
        if (text.includes(marker) || text.includes(edited)) {
            return el.querySelectorAll('button, a, [role=button]').length <= 6;   // a card, not the whole board
        }
    }
    return false;
})"""


def _scoped(controls):
    """The candidate control inside the element holding this workflow's item (marker or edited text) when one exists,
    else the first candidate. The page evaluation never raises into a step: any failure keeps today's first-match."""
    try:
        index = controls.evaluate_all(SCOPED_CONTROL_JS, [MARKER, EDITED])
    except Exception:
        index = -1
    return controls.nth(index) if isinstance(index, int) and index >= 0 else controls.first


def missing_item_controls(probe, conditional):
    """Per-item buttons the request names ("Show Edit and Delete buttons for each task") that a step could not find on
    the item the guard created. fix6 medium p1 (2026-09-27): "no visible Edit control was found" stayed advisory while
    the request says every task shows Edit and Delete. Only counted when an item exists and the step actually looked."""
    if not probe.get('created') or not conditional:
        return []
    missing = []
    for step in (probe.get('steps') or {}).values():
        match = re.match(r'no visible (\w[\w ]*?) control was found', str(step.get('detail') or ''), re.I)
        if not match:
            continue
        word = match.group(1).strip().lower()
        for name in conditional:
            if name.lower().split()[0] == word and name not in missing:
                missing.append(name)
    return missing


def _holds_card(container):
    """Whether the workflow's own card is now inside this column."""
    return container.locator('[draggable=true]').filter(has_text=re.compile('daedalus (workflow|edited) item')).count() == 1
CONTAINER = ('[data-status], [data-column], [data-status-id], [id*="progress" i], [id*="done" i], [id*="todo" i], '
             '[class*="column" i], [class*="col-" i], [class*="lane" i], [class*="board" i] > *')
SKIP_LABEL = re.compile(r'\bsearch\b|\bfilter\b|\bquery\b', re.I)
EDIT_NAME = re.compile(r'\bedit\b', re.I)
SAVE_NAME = re.compile(r'\b(save|update|ok|done|submit)\b', re.I)
DELETE_NAME = re.compile(r'\b(delete|remove)\b', re.I)
COLUMNS = re.compile(r'\bcolumns?\b[^:.\n]{0,60}:\s*(?P<list>[^.\n]+)', re.I)
NO_VALUE_INPUTS = {'checkbox', 'radio', 'hidden', 'submit', 'button', 'reset', 'file', 'color', 'image', 'range'}


def workflow_steps(task):
    """The workflows the request wording asks for, in execution order."""
    text = task or ''
    return [name for name, pattern in WORDING if re.search(pattern, text, re.I)]


def column_names(task):
    """Column names the request lists ("Three-column Kanban board layout: Todo, In Progress, Done")."""
    match = COLUMNS.search(task or '')
    names = []
    for item in re.split(r'\s*,\s*|\s+and\s+', match.group('list') if match else ''):
        item = item.strip().strip('`"\'()')
        if item and len(item.split()) <= 3 and item not in names:
            names.append(item)
    return names


def run_workflows(url, steps, *, startup_seconds=60, step_seconds=15, interfaces=None):
    """Drive the requested workflows in headless Chromium; {'steps': {name: {'status', 'detail'}}, 'writes', 'errors', 'fault', 'interfaces'}."""
    result = {'steps': {}, 'writes': [], 'errors': [], 'fault': '', 'interfaces': {}}
    interfaces = interfaces or {}
    from coder_sandbox_call import invoke, browser_options
    if os.environ.get('DAEDALUS_SANDBOXED') != '1':
        return invoke('coder_workflow_guard', 'run_workflows', [url, list(steps)],
                      {'startup_seconds': startup_seconds, 'step_seconds': step_seconds, 'interfaces': interfaces},
                      timeout=startup_seconds + step_seconds * 40)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        result['fault'] = 'Playwright is not installed in the worker: ' + str(error)
        return result
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, timeout=startup_seconds * 1000, **browser_options())
            try:
                page = browser.new_page()
                page.set_default_timeout(step_seconds * 1000)
                page.on('pageerror', lambda error: result['errors'].append(str(error)[:300]))
                page.on('dialog', lambda dialog: dialog.accept())
                page.on('response', lambda response: result['writes'].append({
                    'method': response.request.method, 'path': urlsplit(response.url).path or '/', 'status': response.status})
                    if response.request.method in ('POST', 'PUT', 'PATCH', 'DELETE') else None)
                page.goto(url, wait_until='load', timeout=step_seconds * 1000)
                page.wait_for_timeout(500)
                session = _Session(page, url, result, step_seconds, interfaces)
                errors_before_create, writes_before_create = len(result['errors']), len(result['writes'])
                created = session.create(MARKER)
                result['create_errors'] = result['errors'][errors_before_create:]
                result['create_writes'] = result['writes'][writes_before_create:]
                result['created'] = bool(created)
                for step in steps:
                    before_errors, before_writes = len(result['errors']), len(result['writes'])
                    try:
                        status, detail = getattr(session, step)(created)
                    except Exception as error:  # a locator timeout is "could not locate", never a defect
                        status, detail = 'advisory', type(error).__name__ + ': ' + str(error)[:200]
                    verdict = creation_failure(created, result['create_errors'], result['create_writes']) if step == steps[0] else None
                    if verdict:
                        status, detail = verdict
                    new_errors = result['errors'][before_errors:]
                    rejected = [w for w in result['writes'][before_writes:] if w['status'] >= 400]
                    if new_errors:
                        status, detail = 'failed', 'uncaught page error during the ' + step + ' workflow: ' + ' | '.join(new_errors[:2])
                    elif rejected:
                        status, detail = 'failed', ('the ' + step + ' workflow sent a request the server rejected: ' +
                                                    ', '.join(f"{w['method']} {w['path']} -> {w['status']}" for w in rejected[:3]))
                    result['steps'][step] = {'status': status, 'detail': detail}
                session.finish()
            finally:
                browser.close()
    except Exception as error:
        result['fault'] = type(error).__name__ + ': ' + str(error)[:300]
    return result


class _Session:
    def __init__(self, page, url, result, step_seconds, interfaces=None):
        self.page, self.url, self.result, self.step_seconds = page, url, result, step_seconds
        interfaces = interfaces or {}
        self.labels = list(interfaces.get('labels') or [])
        self.buttons = list(interfaces.get('buttons') or [])
        self.conditional = list(interfaces.get('conditional_buttons') or [])
        self.columns = list(interfaces.get('columns') or [])
        self.seen_labels, self.seen_buttons = set(), set()

    def _visible(self, text):
        return self.page.get_by_text(text, exact=False).count()

    # --- controls the request names -----------------------------------------------------------

    def _position(self, locator):
        try:
            return locator.evaluate("e => Array.from(document.querySelectorAll('input,select,textarea,button')).indexOf(e)")
        except Exception:
            return 10 ** 6

    def _resolve(self):
        """Requested labels and buttons visible right now, in document order: [(position, kind, name, locator)]."""
        page, found = self.page, []
        for kind, names in (('label', self.labels), ('button', self.buttons)):
            for name in names:
                locator = page.get_by_label(name, exact=True) if kind == 'label' else page.get_by_role('button', name=name, exact=True)
                try:
                    visible = next((locator.nth(i) for i in range(min(locator.count(), 4)) if locator.nth(i).is_visible()), None)
                except Exception:
                    visible = None
                if visible is None:
                    continue
                (self.seen_labels if kind == 'label' else self.seen_buttons).add(name)
                found.append((self._position(visible), kind, name, visible))
        return sorted(found, key=lambda entry: entry[0])

    def _enter(self, control, name, text):
        """Fill one labelled control the way a user would: text where asked, the first real option for selects."""
        tag = control.evaluate('e => e.tagName.toLowerCase()')
        if tag == 'select':
            options = control.locator('option').all()
            choice = next((i for i, option in enumerate(options) if (option.get_attribute('value') or option.inner_text() or '').strip()), None)
            if choice is not None:
                control.select_option(index=choice)
            return
        if SKIP_LABEL.search(name):
            return  # a search or filter box narrows the list; typing into it hides the item
        kind = (control.get_attribute('type') or '').lower() if tag == 'input' else ''
        if kind in NO_VALUE_INPUTS:
            if kind == 'checkbox' and not control.is_checked():
                control.check()
            return
        value = text if tag == 'textarea' or kind in ('', 'text', 'search') else PROBE_VALUES.get(kind)
        if value is not None:
            control.fill(value)

    def _settle(self):
        self.page.wait_for_timeout(1500)
        if self.page.url.rstrip('/') != self.url.rstrip('/'):
            self.page.goto(self.url, wait_until='load'); self.page.wait_for_timeout(300)

    def _create_by_labels(self, text):
        """Create an item through the controls the request names; None when no requested label resolves.

        Labels are grouped with the next requested button after them in document order (Project name ->
        Create project; Task title, Priority, Status -> Add task). Earlier groups get probe text, the last
        group gets `text`; labels that only appear once an earlier group was submitted are found on a later pass.
        """
        done, filled = set(), False
        for _ in range(3):
            controls = self._resolve()
            if not any(kind == 'label' for _, kind, _, _ in controls):
                break
            groups, fields = [], []
            for _, kind, name, locator in controls:
                if kind == 'label':
                    fields.append((name, locator))
                else:
                    groups.append((name, locator, fields)); fields = []
            if fields:
                groups.append(('', None, fields))
            pending = [g for g in groups if g[0] not in done and g[2]]
            if not pending:
                break
            unseen = [n for n in self.labels if n not in self.seen_labels]
            for index, (name, button, group_fields) in enumerate(pending):
                last = index == len(pending) - 1 and not unseen
                for label, control in group_fields:
                    self._enter(control, label, text if last else PROBE_TEXT); filled = True
                done.add(name)
                if button is not None:
                    button.click()
                else:
                    group_fields[-1][1].press('Enter')
                self._settle()
        if not filled:
            return None
        if not self._visible(text):
            # Every group was treated as "earlier" because a requested label never appeared: enter the item
            # text through the last group that exists so the workflows still have something to act on.
            controls = self._resolve()
            fields = [(name, locator) for _, kind, name, locator in controls if kind == 'label']
            buttons = [locator for _, kind, _, locator in controls if kind == 'button']
            if fields:
                for label, control in fields:
                    self._enter(control, label, text)
                if buttons:
                    buttons[-1].click()
                else:
                    fields[-1][1].press('Enter')
                self._settle()
        return self._visible(text) > 0

    def _named_button(self, pattern):
        """A visible button the request names for this role (exact text), else any button/link matching `pattern` —
        preferring the control inside the card that holds THIS workflow's item. fix5 Kanban p2 (2026-09-27): the first
        Delete on the page belonged to another task, so the guard deleted that one, reported "still shown after Delete"
        and spent both repair rounds on a defect the independent verifier (scoped to the created card) did not see."""
        page = self.page
        for name in [*self.conditional, *self.buttons]:
            if pattern.search(name):
                control = page.get_by_role('button', name=name, exact=True)
                if control.count() and control.first.is_visible():
                    return _scoped(control)
        control = page.locator('button:visible, a:visible, [role=button]:visible', has_text=pattern)
        return _scoped(control) if control.count() else None

    def finish(self):
        """Record which requested labels/buttons were never found during the whole flow."""
        if self.labels or self.buttons:
            self._resolve()
        self.result['interfaces'] = {
            'requested': {'labels': self.labels, 'buttons': self.buttons, 'conditional_buttons': self.conditional},
            'missing_labels': [n for n in self.labels if n not in self.seen_labels],
            'missing_buttons': [n for n in self.buttons if n not in self.seen_buttons]}

    def _form(self):
        page = self.page
        if not page.locator('form:visible').count():
            opener = page.locator('button:visible, a:visible', has_text=re.compile(r'\b(add|new|create)\b', re.I))
            if opener.count():
                opener.first.click(); page.wait_for_timeout(400)
        forms = page.locator('form:visible')
        return forms.first if forms.count() else None

    def _submit(self, form):
        submit = form.locator('button[type=submit]:visible, input[type=submit]:visible, button:not([type]):visible').first
        if submit.count():
            submit.click()
        else:
            form.evaluate('f => f.requestSubmit ? f.requestSubmit() : f.submit()')
        self.page.wait_for_timeout(1500)
        if self.page.url.rstrip('/') != self.url.rstrip('/'):
            self.page.goto(self.url, wait_until='load'); self.page.wait_for_timeout(300)

    def create(self, text):
        """Create one item whose text is `text`; True when it is visible afterwards."""
        if self.labels or self.buttons:
            try:
                created = self._create_by_labels(text)
            except Exception:  # a locator timeout is "could not create", never an environment fault
                created = True if self._visible(text) else None
            if created is not None:
                return created
        form = self._form()
        if form is None or not fill_form(self.page, form):
            return False
        field = form.locator('input[type=text]:visible, input:not([type]):visible, textarea:visible').first
        if field.count():
            field.fill(text)
        self._submit(form)
        return self._visible(text) > 0

    def persist(self, created):
        if not created:
            return 'advisory', 'no item could be created through the page form'
        self.page.reload(wait_until='load'); self.page.wait_for_timeout(500)
        if self._visible(MARKER) or self._visible(EDITED):
            return 'passed', 'the created item is still shown after reload'
        return 'failed', 'the request asks for persistence, but the created item is gone after a reload'

    def edit(self, created):
        if not created:
            return 'advisory', 'no item could be created through the page form'
        page = self.page
        before = self._visible(MARKER) + self._visible(EDITED)
        control = self._named_button(EDIT_NAME)
        if control is None:
            return 'advisory', 'no visible Edit control was found'
        control.click(); page.wait_for_timeout(500)
        field = page.locator('input[type=text]:visible, input:not([type]):visible, textarea:visible')
        target = next((field.nth(i) for i in range(field.count()) if MARKER in (field.nth(i).input_value() or '')), field.first if field.count() else None)
        if target is None:
            return 'advisory', 'Edit opened no editable text field'
        target.fill(EDITED)
        form = target.locator('xpath=ancestor::form[1]')
        if form.count():
            self._submit(form.first)
        else:
            save = self._named_button(SAVE_NAME)
            if save is not None:
                save.click(); page.wait_for_timeout(1500)
        after = self._visible(MARKER) + self._visible(EDITED)
        if self._visible(EDITED) and after > before:
            return 'failed', 'editing the item added a duplicate instead of changing it in place'
        if self._visible(EDITED):
            return 'passed', 'the edited text replaced the original in place'
        return 'advisory', 'the edited text was not shown after saving (the Edit control may not be the item\'s)'

    def drag(self, created):
        if not created:
            return 'advisory', 'no item could be created through the page form'
        page = self.page
        card = page.locator('[draggable=true]').filter(has_text=re.compile('daedalus (workflow|edited) item')).first
        if not card.count():
            return 'advisory', 'the created item is not inside a draggable element'
        containers = page.locator(CONTAINER)
        target = next((containers.nth(i) for i in range(containers.count())
                       if containers.nth(i).is_visible() and not containers.nth(i).locator('[draggable=true]').filter(
                           has_text=re.compile('daedalus (workflow|edited) item')).count()), None)
        if target is None and self.columns:
            # No structural column marker: the request lists the column names, so a heading with exactly
            # that text marks the column (its nearest section/div/article that does not hold the card).
            for name in self.columns:
                heading = page.get_by_text(re.compile(r'^\s*' + re.escape(name) + r'\s*$', re.I))
                for i in range(min(heading.count(), 4)):
                    box = heading.nth(i).locator('xpath=ancestor::*[self::section or self::div or self::article][1]')
                    if box.count() and box.first.is_visible() and not box.first.locator('[draggable=true]').filter(
                            has_text=re.compile('daedalus (workflow|edited) item')).count():
                        target = box.first; break
                if target is not None:
                    break
        if target is None:
            return 'advisory', 'no drop target column could be located'
        # fix4 Kanban p1 (2026-09-26): the outer `#in-progress-column` matched before the inner `.task-list` that
        # holds the drop listener, so the synthetic drop reached nothing and the row said "no request was sent"
        # while the app's real fault was a rejected partial PUT. A real drop lands on the innermost list under the
        # pointer: try a mouse drag first, then synthetic events on the inner zone, then on the column itself.
        zone = target.locator(DROP_ZONE).first
        if not zone.count():
            zone = target
        writes_before = len(self.result['writes'])
        try:
            card.drag_to(zone, timeout=min(5000, self.step_seconds * 1000))
            page.wait_for_timeout(600)
        except Exception:
            pass
        for drop_target in ([zone, target] if not (_holds_card(target) or len(self.result['writes']) > writes_before) else []):
            transfer = page.evaluate_handle('new DataTransfer()')
            card.dispatch_event('dragstart', {'dataTransfer': transfer})
            drop_target.dispatch_event('dragenter', {'dataTransfer': transfer})
            drop_target.dispatch_event('dragover', {'dataTransfer': transfer})
            drop_target.dispatch_event('drop', {'dataTransfer': transfer})
            card.dispatch_event('dragend', {'dataTransfer': transfer})
            page.wait_for_timeout(600)
            if _holds_card(target) or len(self.result['writes']) > writes_before:
                break
        page.wait_for_timeout(600)
        if _holds_card(target):
            return 'passed', 'drag and drop moved the card into the target column'
        if len(self.result['writes']) > writes_before:
            return 'passed', 'drag and drop sent a write; the card was not seen in the target column (advisory)'
        # The request asks to move items by drag-and-drop; a drop onto a status column that moves nothing and
        # sends nothing is the silent-drag class the independent verifier fails (Pilot A kanban, 2026-09-25).
        return 'failed', ('dropping the card onto another column changed nothing: no request was sent and the card stayed where it was. '
                          'The drop handler must move the task to the target column and persist its new status.')

    def delete(self, created):
        if not created:
            return 'advisory', 'no item could be created through the page form'
        page = self.page
        control = self._named_button(DELETE_NAME)
        if control is None:
            return 'advisory', 'no visible Delete control was found'
        control.click(); page.wait_for_timeout(1200)
        page.reload(wait_until='load'); page.wait_for_timeout(500)
        if self._visible(MARKER) or self._visible(EDITED):
            return 'failed', 'the item is still shown after Delete and a reload'
        return 'passed', 'the item is gone after Delete and a reload'

    def literal(self, created):
        form = self._form()
        if form is None or not fill_form(self.page, form):
            return 'advisory', 'no form to enter text into'
        field = form.locator('input[type=text]:visible, input:not([type]):visible, textarea:visible').first
        if not field.count():
            return 'advisory', 'no text field to enter markup into'
        field.fill(LITERAL)
        self._submit(form)
        if self.page.locator('b', has_text='daedalus-literal').count():
            return 'failed', 'entered text is rendered as HTML: <b>daedalus-literal</b> became a bold element'
        if self._visible(LITERAL):
            return 'passed', 'entered markup is shown literally'
        return 'advisory', 'the entered text was not found on the page after submit'


def workflow_rows(service, url, settings, task, prober=run_workflows):
    """Launch-phase rows: `interface:labels:<service>` when the request names labels/buttons, then one per
    requested workflow; [] when the request names neither."""
    from coder_policy7_evidence import requested_interfaces
    steps = workflow_steps(task)
    wanted = requested_interfaces(task)
    names = {'labels': list(wanted.get('labels') or []), 'buttons': list(wanted.get('buttons') or []),
             'conditional_buttons': list(wanted.get('conditional_buttons') or []), 'columns': column_names(task)}
    named = bool(names['labels'] or names['buttons'])
    if not steps and not named:
        return []
    probe = prober(url + '/', steps, startup_seconds=settings.get('daedalus_browser_startup_seconds', 60),
                   step_seconds=settings.get('daedalus_browser_step_seconds', 15), interfaces=names)
    rows = []
    if named:
        fault = probe.get('fault', '')
        info = probe.get('interfaces') or {}
        missing_labels = [n for n in info.get('missing_labels', []) if n in names['labels']]
        missing_buttons = [n for n in info.get('missing_buttons', []) if n in names['buttons']]
        missing_per_item = missing_item_controls(probe, names.get('conditional_buttons') or [])
        missing = [*missing_labels, *missing_buttons, *missing_per_item]
        passed = not fault and not missing
        if fault:
            reason = 'The page could not be exercised in the verification browser: ' + fault
        elif missing:
            reason = ('Diagnostic probe (never acceptance evidence): the request names accessible labels ' +
                      ', '.join(names['labels']) + (' and buttons ' + ', '.join(names['buttons']) if names['buttons'] else '') +
                      '; on the loaded page get_by_label / get_by_role(button, name) found nothing for: ' + ', '.join(missing) +
                      '. Associate each control with exactly that label text (<label>Text<input>, htmlFor/for + id, or aria-label) '
                      'and give each button exactly that visible text; do not substitute other names.' +
                      (' The request shows ' + ', '.join(missing_per_item) + ' for each item: after creating one, no such control '
                       'was visible on it.' if missing_per_item else ''))
        else:
            reason = ''
        rows.append({'id': f"interface:labels:{service['id']}", 'phase': 'launch', 'origin': 'controller', 'passed': passed,
            'cwd': service.get('cwd', '.'), 'command': 'resolve the requested labels and buttons in headless Chromium',
            'reason': reason, 'log_tail': reason or 'every requested label and button was found', 'environment_fault': bool(fault),
            'execution_succeeded': passed, 'requested_labels': names['labels'], 'requested_buttons': names['buttons'],
            'missing_labels': missing_labels, 'missing_buttons': missing_buttons, 'missing_per_item': missing_per_item, 'evidence_types': [], 'outcomes': [],
            'classification': 'environment' if fault else 'application_defect' if missing else 'passed'})
    for step in steps:
        outcome = probe['steps'].get(step) or {'status': 'advisory', 'detail': 'not attempted'}
        fault = probe.get('fault', '')
        failed = outcome['status'] == 'failed'
        passed = not fault and not failed
        reason = ('The page could not be exercised in the verification browser: ' + fault if fault else
                  'Diagnostic probe (never acceptance evidence): ' + outcome['detail'] if failed else '')
        rows.append({'id': f"workflow:{service['id']}:{step}", 'phase': 'launch', 'origin': 'project', 'passed': passed,
            'cwd': service.get('cwd', '.'), 'command': f'{step} workflow in headless Chromium', 'reason': reason,
            'log_tail': reason or outcome['detail'], 'environment_fault': bool(fault), 'execution_succeeded': passed,
            'workflow_step': step, 'workflow_status': outcome['status'], 'evidence_types': [], 'outcomes': [],
            'classification': 'environment' if fault else 'application_defect' if failed else 'passed'})
    return rows
