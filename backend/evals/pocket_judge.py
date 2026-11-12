"""Browser-side observer for the recorded Pocket Tasks prompts. Runs in the judge container."""
import json
import re
import sys
from playwright.sync_api import sync_playwright


def exercise(page, url, followup):
    page.set_viewport_size({'width':390,'height':844})
    page.goto(url,wait_until='networkidle')
    field=page.get_by_role('textbox',name='Task',exact=True)
    add=page.get_by_role('button',name='Add',exact=True)
    clear=page.get_by_role('button',name='Clear',exact=True)
    result=page.get_by_label('Result',exact=True)
    checks=[]
    def check(name, actual, expected):
        checks.append({'id':name,'expected':expected,'actual':actual,'passed':actual==expected})
        if actual!=expected:
            raise AssertionError(name)
    def completed():
        checkbox=page.get_by_role('checkbox')
        if checkbox.count()==1:
            return checkbox.is_checked()
        return page.get_by_text('pack lunch',exact=True).evaluate("""e=>{
          for(let n=e;n&&n!==document.body;n=n.parentElement){
            if(getComputedStyle(n).textDecorationLine.includes('line-through') ||
               n.getAttribute('aria-pressed')==='true' || n.getAttribute('aria-checked')==='true')return true;
          }
          const b=[...document.querySelectorAll('button')].filter(x=>x.getAttribute('aria-pressed')==='true' ||
             /^(undo|uncheck|mark.*incomplete)/i.test(x.getAttribute('aria-label')||x.innerText));
          return b.length===1;
        }""")
    try:
        check('mobile viewport',page.evaluate('document.documentElement.scrollWidth <= innerWidth'),True)
        field.fill('pack lunch');add.click()
        check('add feedback',result.inner_text(),'Task added')
        check('saved task',page.get_by_text('pack lunch',exact=True).count(),1)
        toggle=page.get_by_role('checkbox')
        if toggle.count()!=1:
            toggle=page.get_by_role('button',name=re.compile('complet|toggle|mark.*done',re.I))
        toggle.click()
        check('completed',completed(),True)
        page.reload(wait_until='networkidle')
        check('completion persisted',completed(),True)
        field.fill('unsaved draft');clear.click()
        check('cleared field',field.input_value(),'')
        check('clear feedback',result.inner_text(),'Ready' if followup else '')
        check('clearing kept completed task',completed(),True)
        # Check blank validation separately, and require the error to disappear.
        field.fill('  ')
        baseline=page.locator('body').inner_text().splitlines()
        add.click()
        appeared=[s.strip() for s in page.locator('body').inner_text().splitlines() if s.strip() and s not in baseline]
        check('visible blank error',bool(appeared),True)
        clear.click()
        check('validation clears',any(t in page.locator('body').inner_text().splitlines() for t in appeared),False)
        # A distinct Task added -> Ready/empty transition is required on both builds.
        field.fill('bring water');add.click();check('distinct start',result.inner_text(),'Task added')
        clear.click();check('exact result transition',result.inner_text(),'Ready' if followup else '')
        deletes=page.get_by_role('button',name=re.compile('delete|remove',re.I))
        check('two delete controls',deletes.count(),2)
        deletes.first.click();deletes.first.click();page.reload(wait_until='networkidle')
        check('deletion persisted',sum(page.get_by_text(s,exact=True).count() for s in ('bring water','pack lunch')),0)
    except Exception as exc:
        if not checks or checks[-1].get('passed'):
            checks.append({'id':'interaction','passed':False,'actual':str(exc)[:900]})
    return checks


if __name__=='__main__':
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page();page.set_default_timeout(10000)
        print(json.dumps(exercise(page,sys.argv[1],sys.argv[2]=='edit')))
        browser.close()
