"""AT-SPI adapter, executed by system Python inside a private desktop session."""
import json
import sys
import time


class AccessibleFailure(ValueError):
    def __init__(self, category, message, request, nodes):
        super().__init__(message)
        visible = [row for row, _ in nodes if row.get('showing')]
        visible.sort(key=lambda row: (row.get('name') != request.get('name'),
                                     not (row.get('editable') or row.get('activatable'))))
        candidates = []
        for row in visible[:12]:
            item = {key: row.get(key) for key in ('id', 'name', 'role', 'editable', 'activatable')}
            item['readable'] = 'text' in row or row.get('role') == 'label'
            if row.get('name') and sum(other.get('name') == row['name'] and other.get('role') == row['role'] for other in visible) == 1:
                item['locator'] = {'name': row['name'], 'role': row['role']}
            candidates.append(item)
        self.details = {'error': message, 'category': category,
                        'requested': {key: request[key] for key in ('action', 'selector', 'name', 'role') if key in request},
                        'candidates': candidates, 'controls_truncated': len(visible) > 12}


def resolve_target(request, nodes):
    guarded = request.get('_behavior_version', 0) >= 1
    action = request.get('action')
    if guarded and action not in {'click', 'fill', 'value', 'text'}:
        raise AccessibleFailure('unsupported_action', 'Unsupported desktop action: ' + str(action), request, nodes)
    named = [(row, node) for row, node in nodes
             if (not guarded or row.get('showing')) and
             (row['id'] == request['selector'] if request.get('selector') else row['name'] == request.get('name')) and
             (not request.get('role') or row['role'] == request['role']) and
             (not guarded or not request.get('selector') or not request.get('name') or row['name'] == request['name'])]
    matches = [(row, node) for row, node in named
               if (action not in {'fill', 'value'} or row.get('editable')) and
                  (action != 'click' or row.get('activatable'))]
    if not guarded:
        matches = [(row, node) for row, node in nodes
                   if (action not in {'fill', 'value'} or row.get('editable')) and
                   (action != 'click' or row.get('activatable')) and
                   ((request.get('selector') and row['id'] == request['selector']) or
                    (not request.get('selector') and row['name'] == request.get('name') and
                     (not request.get('role') or row['role'] == request['role'])))]
    if len(matches) != 1:
        message = f'Expected one accessible target; found {len(matches)}.'
        if guarded:
            category = 'ambiguous_target' if len(matches) > 1 else 'unsupported_action' if named else 'target_not_found'
            raise AccessibleFailure(category, message + ' Use an observed accessible name/role; Qt object names and class names are not locators.', request, nodes)
        raise ValueError(message + ' Observe and select its exact id.')
    return matches[0]


def perform(request):
    import pyatspi
    desktop = pyatspi.Registry.getDesktop(0)
    nodes = []
    def visit(node, path, depth=0, interactive_ancestor=False):
        if len(nodes) >= 300 or depth > 20:
            return
        try:
            row = {'id': path, 'name': node.name or '', 'role': node.getRoleName()}
            if request.get('_behavior_version', 0) >= 2:
                try: row['pid'] = node.get_process_id()
                except (AttributeError, NotImplementedError): row['pid'] = None
            row['interactive_ancestor'] = interactive_ancestor
            row['showing'] = node.getState().contains(pyatspi.STATE_SHOWING)
            row['editable'] = node.getState().contains(pyatspi.STATE_EDITABLE)
            try:
                actions = node.queryAction()
                row['activatable'] = any(actions.getName(i).lower() in {'click', 'press', 'activate', 'toggle'}
                                         for i in range(actions.nActions))
            except (NotImplementedError, AttributeError):
                row['activatable'] = False
            try:
                text = node.queryText()
                row['text'] = text.getText(0, min(text.characterCount, 1500))
            except NotImplementedError:
                pass
            nodes.append((row, node))
            for index in range(min(node.childCount, 100)):
                visit(node.getChildAtIndex(index), f'{path}/{index}', depth+1,
                      interactive_ancestor or row.get('activatable', False) or row.get('editable', False))
        except Exception:
            return
    visit(desktop, 'desktop')
    if request.get('_behavior_version', 0) >= 2:
        # AT-SPI shares a bus with every display in the private session. Only
        # controls from this launch's mapped windows may prove visible behavior.
        allowed = set(request.get('_display_pids', []))
        nodes = [(row, node) for row, node in nodes if row.get('pid') in allowed]
    action = request.get('action', 'observe')
    if action == 'observe':
        return {'nodes': [r for r, _ in nodes if (not request.get('role') or r['role'] == request['role'])
                          and (not request.get('name') or r['name'] == request['name'])
                          and (not request.get('selector') or r['id'] == request['selector'])]}
    row, node = resolve_target(request, nodes)
    if action == 'click':
        interface = node.queryAction()
        actions = [(i, interface.getName(i)) for i in range(interface.nActions)]
        index = next((i for i, name in actions if name.lower() in {'click', 'press', 'activate', 'toggle'}), None)
        if index is None or not interface.doAction(index):
            raise ValueError('The accessible control has no successful activation action')
        # Accessible activation is asynchronous: Qt animateClick releases after
        # 100 ms; GTK4's ACTIVATE_TIMEOUT is 250 ms. Earlier repeat activations
        # coalesce, and GTK does not expose that entire interval as PRESSED.
        # Allow both toolkit timers to finish before accepting another action.
        started = time.monotonic()
        while time.monotonic() - started < .35 or node.getState().contains(pyatspi.STATE_PRESSED):
            if time.monotonic() - started >= 2:
                raise TimeoutError('The accessible control did not finish its activation')
            time.sleep(.01)
    elif action == 'fill':
        if not node.queryEditableText().setTextContents(str(request.get('value', ''))):
            raise ValueError('The accessible control refused text input')
    elif action == 'value':
        if 'text' not in row:
            raise ValueError('The editable control does not expose its current value')
        return {**row, 'value': row['text']}
    elif action == 'text':
        # Qt QLabel exposes its displayed label through Name without necessarily
        # implementing the AT-SPI Text interface. Keep text actions consistent
        # across toolkits instead of returning an opaque node to the verifier.
        if 'text' not in row and row['role'] == 'label':
            row['text'] = row['name']
        if 'text' not in row:
            raise ValueError('The selected control does not expose readable text; use observe to inspect its accessible properties')
        return row
    else:
        raise ValueError('Unsupported accessible action')
    return {'performed': action, 'target': row}


if __name__ == '__main__':
    try:
        print(json.dumps(perform(json.loads(sys.stdin.read()))))
    except Exception as error:
        print(json.dumps(getattr(error, 'details', {'error': str(error)})))
        raise SystemExit(1)
