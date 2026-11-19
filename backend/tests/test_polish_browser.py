"""Opt-in regressions against a disposable, locally served production build.

HYPRCHAT_POLISH_URL=http://127.0.0.1:8091 pytest backend/tests/test_polish_browser.py
The candidate must contain synthetic agent/persona cards and an HTML artifact.
"""
import json
import os
import re
from urllib.parse import urlparse

import pytest

URL = os.environ.get('HYPRCHAT_POLISH_URL', '')
pytestmark = pytest.mark.skipif(not URL, reason='Requires disposable local polish candidate')


@pytest.fixture(params=['chromium', 'webkit'])
def page(request, tmp_path):
    from playwright.sync_api import sync_playwright
    assert urlparse(URL).hostname in {'localhost', '127.0.0.1', '::1'}
    with sync_playwright() as runtime:
        browser = getattr(runtime, request.param).launch(headless=True)
        # Only the fault-injection case intercepts API traffic. A controlling service
        # worker can bypass Playwright routes; all real-backend cases keep it enabled.
        intercepting = request.node.name.startswith('test_analytics_errors')
        context = browser.new_context(viewport={'width': 1440, 'height': 900},
                                      service_workers='block' if intercepting else 'allow')
        # The disposable candidate's built-in, passwordless profile (no real sessions).
        context.add_init_script("localStorage.setItem('hc-current-user','default')")
        tab = context.new_page()
        tab.set_default_timeout(10000)
        try:
            yield tab
        finally:
            tab.screenshot(path=str(tmp_path / 'final.png'))
            (tmp_path / 'final.html').write_text(tab.content())
            context.close()
            browser.close()


def navigate(page, name):
    menu = page.get_by_role('button', name='Show menu', exact=True)
    if menu.is_visible():
        menu.click()
    target = page.get_by_role('button', name=name, exact=True)
    if not target.is_visible():
        page.get_by_role('button', name='More', exact=True).click()
    target.click()


@pytest.mark.parametrize('width,scale', [(320,14), (390,14), (768,14), (1440,14), (320,24), (390,24)])
def test_profile_cards_keep_identity_and_actions_readable(page, width, scale, tmp_path):
    from playwright.sync_api import expect
    page.set_viewport_size({'width': width, 'height': 900})
    page.add_init_script(f"localStorage.setItem('hc-ui-font-size','{scale}')")
    page.goto(URL)
    navigate(page, 'Agents')
    for kind in ['Agents', 'Personas']:
        page.get_by_role('radio', name=re.compile('^' + kind + ' ·')).click()
        card = page.locator('.hc-profile-card').first
        expect(card).to_be_visible()
        metrics = card.evaluate('''card => {
          const head=card.firstElementChild,identity=head.firstElementChild,actions=head.lastElementChild;
          const a=identity.getBoundingClientRect(),b=actions.getBoundingClientRect();
          return {textWidth:identity.lastElementChild.getBoundingClientRect().width,
            separate:a.right<=b.left+1 || a.bottom<=b.top+1,
            overflow:card.scrollWidth>card.clientWidth+1};
        }''')
        assert metrics['textWidth'] >= 120 and metrics['separate'] and not metrics['overflow'], metrics
        card.get_by_role('button', name='Edit', exact=True).click()
        name = card.get_by_role('textbox').first
        name.fill('A long profile name ' * 6)
        expect(name).to_be_visible()
        assert name.evaluate('(el)=>el.getBoundingClientRect().width') >= 120
        editor_overflow=card.evaluate('''el=>({width:el.clientWidth,scroll:el.scrollWidth,
          wide:[...el.querySelectorAll('*')].filter(n=>n.getClientRects().length&&n.getBoundingClientRect().right>el.getBoundingClientRect().right+1)
            .slice(0,12).map(n=>({tag:n.tagName,text:n.textContent.slice(0,60),style:n.getAttribute('style')}))})''')
        assert editor_overflow['scroll']<=editor_overflow['width']+1, editor_overflow
        # Return to persisted data without saving the edit.
        page.reload()
        navigate(page, 'Agents')
    page.screenshot(path=str(tmp_path / f'profiles-{width}.png'))


@pytest.mark.parametrize('width', [320, 390])
def test_artifact_filters_scroll_and_modal_restores_focus(page, width, tmp_path):
    from playwright.sync_api import expect
    page.set_viewport_size({'width': width, 'height': 844})
    page.goto(URL)
    navigate(page, 'Artifacts')
    tray = page.get_by_role('radiogroup', name='Artifact kinds')
    expect(tray.get_by_role('radio')).to_have_count(11)
    assert tray.evaluate('(el)=>el.scrollWidth>el.clientWidth')
    assert tray.evaluate('(el)=>[...el.children].every(n=>n.scrollWidth<=n.clientWidth+1)')
    tray.get_by_role('radio', name='All', exact=True).focus()
    for _ in range(10):
        page.keyboard.press('ArrowRight')
    expect(tray.get_by_role('radio', name='Archived', exact=True)).to_be_focused()
    expect(tray.get_by_role('radio', name='Archived', exact=True)).to_have_attribute('aria-checked','true')
    assert tray.evaluate('(el)=>el.scrollLeft>0')
    page.keyboard.press('ArrowRight')
    expect(tray.get_by_role('radio', name='All', exact=True)).to_be_focused()
    page.keyboard.press('End')
    expect(tray.get_by_role('radio', name='Archived', exact=True)).to_be_focused()
    page.keyboard.press('Home')
    expect(tray.get_by_role('radio', name='All', exact=True)).to_be_focused()
    page.get_by_role('button', name='Details', exact=True).first.press('Enter')
    dialog = page.get_by_role('dialog', name='Artifact details', exact=True)
    expect(dialog).to_be_visible()
    close = dialog.get_by_role('button', name='Close', exact=True)
    expect(close).to_be_focused()
    assert tray.evaluate('(el)=>!!el.closest("[inert]")')
    close.press('Shift+Tab')
    expect(dialog.get_by_role('button', name='Delete metadata', exact=True)).to_be_focused()
    page.keyboard.press('Tab')
    expect(close).to_be_focused()
    dialog.get_by_role('button', name='✏️ Edit', exact=True).press('Enter')
    editor = page.get_by_role('dialog', name='Artifact editor', exact=True)
    expect(editor).to_be_visible()
    editor.get_by_role('button', name='Close', exact=True).press('Escape')
    expect(editor).to_have_count(0)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('button', name='✏️ Edit', exact=True)).to_be_focused()
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)
    expect(page.get_by_role('button', name='Details', exact=True).first).to_be_focused()
    assert not tray.evaluate('(el)=>!!el.closest("[inert]")')
    # A responsive transition removes modality, then reinstates it on a phone.
    page.get_by_role('button', name='Details', exact=True).first.click()
    expect(dialog).to_be_visible()
    page.set_viewport_size({'width': 1440, 'height': 900})
    expect(page.get_by_role('complementary', name='Artifact details')).to_be_visible()
    assert not tray.evaluate('(el)=>!!el.closest("[inert]")')
    page.set_viewport_size({'width': width, 'height': 844})
    expect(dialog).to_be_visible()
    expect(close).to_be_focused()
    page.screenshot(path=str(tmp_path / f'artifact-details-{width}.png'))
    close.click()


def test_real_backend_refresh_has_no_page_errors(page):
    from playwright.sync_api import expect
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    # No API interception: refresh a loaded screen against the actual backend.
    page.goto(URL)
    navigate(page, 'Analytics')
    expect(page.get_by_text('Total Tokens', exact=True)).to_be_visible()
    page.reload()
    navigate(page, 'Artifacts')
    expect(page.get_by_role('button', name='Details', exact=True).first).to_be_visible()
    assert not errors


def test_early_refresh_recovers_without_uncaught_exceptions(page, tmp_path):
    from playwright.sync_api import expect
    diagnostics, uncaught = [], []
    page.expose_function('recordUncaught', lambda message: uncaught.append(message))
    page.add_init_script('''
      addEventListener('error',e=>recordUncaught({type:'error',message:e.message}));
      addEventListener('unhandledrejection',e=>recordUncaught({type:'rejection',message:String(e.reason)}));
    ''')
    page.on('pageerror',lambda e:diagnostics.append({'name':e.name,'message':str(e),'stack':e.stack}))
    page.goto(URL)
    page.reload()
    navigate(page,'Agents')
    expect(page.locator('.hc-profile-card').first).to_be_visible()
    (tmp_path/'early-reload-diagnostics.json').write_text(json.dumps({'console':diagnostics,'uncaught':uncaught},indent=2))
    assert not uncaught
    # WebKit reports caught fetches started by the departing document as console
    # diagnostics; Playwright exposes these through pageerror. Retain them explicitly.
    assert all(e['name']=='Fetch API cannot load http' and e['message'].endswith(' due to access control checks.') for e in diagnostics)


def test_background_follows_browser_motion_preference(page):
    from playwright.sync_api import expect
    page.add_init_script("localStorage.setItem('hc-bg-effect','snowfall')")
    page.emulate_media(reduced_motion='no-preference')
    page.goto(URL)
    canvas = page.locator('.hc-app > canvas[aria-hidden="true"]')
    expect(canvas).to_be_visible()
    sample = '''async canvas => {
      // Allow media-query handlers to settle, then compare across eight real frames.
      const frame=()=>new Promise(resolve=>requestAnimationFrame(resolve));
      await frame(); await frame();
      const before=canvas.toDataURL();
      for(let i=0;i<8;i++)await frame();
      return before!==canvas.toDataURL();
    }'''
    assert canvas.evaluate(sample), 'The running background should draw changing pixels'
    page.emulate_media(reduced_motion='reduce')
    assert not canvas.evaluate(sample), 'Reduced motion must stop changing pixels without reloading'
    page.set_viewport_size({'width': 390, 'height': 844})
    assert not canvas.evaluate(sample), 'The resized still must remain still'
    assert canvas.evaluate('(c)=>c.getContext("2d").getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3&&v>0)')
    page.emulate_media(reduced_motion='no-preference')
    assert canvas.evaluate(sample), 'Animation must resume when motion is allowed again'


def test_analytics_errors_retry_and_latest_query_wins(page):
    from playwright.sync_api import expect
    summary = {k: [] for k in ['all_time','today','daily_30d','by_model_all','by_persona_all']}
    summary['all_time'] = [{'total_tokens': 42, 'request_count': 1}]
    state = {'mode': 'error', 'pending': []}
    failed_requests = []
    page.on('requestfailed', lambda request: failed_requests.append(request.url))

    def telemetry(route):
        path = urlparse(route.request.url)
        if state['mode'] == 'error':
            return route.fulfill(status=500, json={'detail': 'Injected statistics outage'})
        if path.path.endswith('/summary'):
            return route.fulfill(json=summary)
        if state['mode'] == 'malformed':
            return route.fulfill(json={'detail': 'Invalid grouped data'})
        if state['mode'] == 'race' and 'group_by=model' in path.query:
            state['pending'].append(route)
            return
        return route.fulfill(json=[{'persona_name': 'Current profile', 'total_tokens': 42}])

    page.route('**/api/analytics/tokens**', telemetry)
    page.goto(URL)
    navigate(page, 'Analytics')
    expect(page.get_by_role('alert')).to_contain_text('Statistics unavailable')
    expect(page.get_by_text('Total Tokens', exact=True)).to_have_count(0)
    state['mode'] = 'ok'
    page.get_by_role('button', name='Retry', exact=True).click()
    expect(page.get_by_text('Total Tokens', exact=True)).to_be_visible()
    expect(page.get_by_role('alert')).to_have_count(0)
    state['mode'] = 'malformed'
    page.get_by_role('button', name='Refresh', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('last successfully loaded')
    expect(page.get_by_text('Total Tokens', exact=True)).to_be_visible()
    state['mode'] = 'race'
    page.get_by_role('button', name='model', exact=True).click()
    expect(page.get_by_text('Loading statistics…', exact=True)).to_be_visible()
    page.get_by_role('button', name='profile', exact=True).click()
    expect(page.get_by_text('Tokens by profile (30d)', exact=True)).to_be_visible()
    assert any('group_by=model' in url for url in failed_requests), 'The superseded request must be cancelled'
    for route in state['pending']:
        route.fulfill(json=[{'model': 'Stale model', 'total_tokens': 999}])
    expect(page.get_by_text('Tokens by profile (30d)', exact=True)).to_be_visible()
    expect(page.get_by_role('alert')).to_have_count(0)
    assert state['pending'], 'The deliberately delayed older request must have started'
