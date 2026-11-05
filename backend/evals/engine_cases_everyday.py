"""User-facing prompts and private probes for the separately scored everyday cohort."""
from engine_cases import click, fill, field, value, text, label


def program(identity, task, command, probes, followup, new_probes, **extra):
    return dict(id=identity, family='core', task=task, command=command, probes=probes,
                followup=followup, followup_probes=new_probes, **extra)


def pipe(data, expected, *args):
    return {'args':list(args),'stdin':data,'stdout':expected}


def window(identity, toolkit, command, task, steps, followup, after):
    return dict(id=identity, family='desktop', kind='desktop', toolkit=toolkit, command=command,
                task=task, steps=steps, followup=followup, followup_steps=after)


def typing(name, data):
    return {'action':'fill','name':name,'value':data}


def webinput(name, data):
    return {**typing(name,data),'labelled_field':True}


def shown(s):
    return {'action':'text','selector':'body','contains':s}


CORE = [
    program('c-line-count', 'Make me a C tool called lines that counts lines from stdin. I want to build it with make and run ./lines. Count a final line even without a newline. Print just the count.',
            ['./lines'], [pipe('first\n\nlast','3\n'),pipe('','0\n')],
            'Add --nonblank so I can leave empty lines out. Keep the old default.',[pipe('one\n\ntwo','2\n','--nonblank')]),
    program('cpp-interval', 'Make me a C++17 CMake tool I can run as ./build/span LOW HIGH. Print the number of integers between those endpoints, including both. Print zero if the range is reversed.',
            ['./build/span'],[{'args':['-4','3'],'stdout':'8\n'},{'args':['8','2'],'stdout':'0\n'}],
            'Add --exclusive before the numbers to leave out both endpoints.',[{'args':['--exclusive','-4','3'],'stdout':'6\n'},{'args':['--exclusive','2','2'],'stdout':'0\n'}]),
    program('java-ticket', 'Build me a Java ticket-price calculator as an executable Maven JAR at target/tickets.jar. I pass it the number of tickets. Each costs $7.50. Print just the total to two decimal places. Reject negative ticket counts.',
            ['java','-jar','target/tickets.jar'],[{'args':['3'],'stdout':'22.50\n'},{'args':['0'],'stdout':'0.00\n'},{'args':['-1'],'nonzero':True}],
            'Add a --student option before the count that makes each ticket $5.',[{'args':['--student','4'],'stdout':'20.00\n'}]),
    program('python-spending','Write spending.py so I can pipe in a CSV with category and amount columns and get a JSON object of totals by category. Keep cents exact and format each total as a string with two decimal places.',
            ['python3','spending.py'],[{'stdin':'category,amount\na,0.10\na,0.20\nb,-2.50\n','json':{'a':'0.30','b':'-2.50'}},{'stdin':'category,amount\n','json':{}}],
            'Add --only CATEGORY so I can request just that category.',[{'args':['--only','b'],'stdin':'category,amount\na,1\nb,2\n','json':{'b':'2.00'}}]),
    program('node-dedupe','Fix the attached Node tool. When I pipe lines into node lines.mjs it should keep the first copy of each line in input order, including one empty line.',
            ['node','lines.mjs'],[pipe('red\n\nred\nblue\n\n','red\n\nblue\n'),pipe('','')],
            'Add --ignore-case, keeping the first spelling.',[pipe('Red\nred\nRED\nB\n','Red\nB\n','--ignore-case')],
            upload={'lines.mjs':"import fs from 'node:fs';\nprocess.stdout.write(fs.readFileSync(0,'utf8'));\n",
                    'package.json':'{"name":"lines","version":"1.0.0","type":"module"}'}),
    program('ts-minutes','Make a TypeScript CLI that converts a whole number of minutes into hours and leftover minutes. After npm run build, node dist/time.js 75 should print 1h 15m. Reject negative input.',
            ['node','dist/time.js'],[{'args':['122'],'stdout':'2h 2m\n'},{'args':['-1'],'nonzero':True}],
            'Add --pad before the minutes to show two digits for the leftover minutes.',[{'args':['--pad','61'],'stdout':'1h 01m\n'}]),
    program('go-words','Make a Go command I can run with go run . that counts words piped into it. Ignore case, split on whitespace, and print a JSON object with the counts.',
            ['go','run','.'],[{'stdin':'A b\na  c c','json':{'a':2,'b':1,'c':2}},{'stdin':'','json':{}}],
            'Add --min N to hide words used fewer than N times.',[{'args':['--min','2'],'stdin':'a b A','json':{'a':2}}]),
    program('rust-prefix','Build me a Rust Cargo command that puts its first argument at the start of each line I pipe in. Preserve empty lines. With no input it should print nothing.',
            ['cargo','run','--quiet','--'],[pipe('a\n\nb','>a\n>\n>b\n','>'),pipe('','','X')],
            'Add --skip-empty before the prefix to leave blank lines out.',[pipe('a\n\nb\n','+a\n+b\n','--skip-empty','+')]),
]

WEB = [
    dict(id='local-shopping',family='web',kind='web',file_path='index.html',
         task='Make a shopping list I can use by opening index.html directly. Give me an Item field and an Add button. Remember the list when I reopen the page.',
         steps=[webinput('Item','rolled oats'),click('Add'),shown('rolled oats'),{'action':'reload'},shown('rolled oats')],
         followup='Add a Clear list button. Once I clear the list it should still be empty after reload.',
         followup_steps=[click('Clear list'),{'action':'text','selector':'body','excludes':'rolled oats'},{'action':'reload'},{'action':'text','selector':'body','excludes':'rolled oats'}]),
    dict(id='served-mileage',family='web',kind='web',command='python3 -m http.server 8000',
         task='Build a small HTML and JavaScript mileage calculator in index.html. I enter Miles and Gallons, click Calculate and see miles per gallon to one decimal place. Show Error for zero gallons.',
         steps=[webinput('Miles','99'),webinput('Gallons','0'),click('Calculate'),shown('Error'),webinput('Gallons','4'),click('Calculate'),shown('24.8')],
         followup='Add Reset. It should empty both fields and remove the previous answer.',
         followup_steps=[click('Reset'),{'action':'value','labelled_field':True,'name':'Miles','equals':''},{'action':'value','labelled_field':True,'name':'Gallons','equals':''},{'action':'text','selector':'body','excludes':'24.8'}]),
    dict(id='node-price-api',family='web',kind='http',command='PORT={port} node server.js',ready_path='/health',
         task='Build a small Node price API in server.js. It should listen on PORT, expose /health, and return {"cents":750} from GET /price?quantity=3 at 250 cents per item. Reject negative quantities with HTTP 400.',
         probes=[{'path':'/price?quantity=5','json':{'cents':1250}},{'path':'/price?quantity=-1','code':400}],
         followup='Add an optional student=true query parameter that makes each item 175 cents.',
         followup_probes=[{'path':'/price?quantity=2&student=true','json':{'cents':350}}]),
    dict(id='sqlite-notebook',family='web',kind='web',command='python3 app.py',
         task='Build a small web notebook with a Python server, a browser UI and SQLite. Start it with python3 app.py on port 8000. I want to type in a Note field, click Save, and see all saved notes even after restarting the server. Include a View JSON link to /api/notes so I can see the saved texts as a JSON list.',
         steps=[webinput('Note','pack a lunch'),click('Save'),shown('pack a lunch'),{'action':'restart'},shown('pack a lunch'),{'action':'click','role':'link','name':'View JSON'},{'action':'text','selector':'pre','json':['pack a lunch']},{'action':'restart'}],
         followup='Add a Clear notes button that removes saved notes from the database and the screen.',
         followup_steps=[click('Clear notes'),{'action':'restart'},{'action':'text','selector':'body','excludes':'pack a lunch'},{'action':'click','role':'link','name':'View JSON'},{'action':'text','selector':'pre','json':[]}]),
]

DESKTOP = [
    window('qt-length','qt','/usr/bin/python3 app.py','Make a PyQt6 desktop app in app.py. I type in a Text field and click Count. Display the character count, including spaces, as Length: N.',
           [typing('Text','a b'),click('Count'),label('Length: 3')],
           'Add Clear, empty the field and change the result to Ready.',[field('Text','a b'),click('Clear'),field('Text',''),label('Ready')]),
    window('gtk-stock','gtk','/usr/bin/python3 app.py','Make a GTK desktop stock counter in app.py. Start at zero and show Stock: N. Add one with Receive and subtract one with Sell, stopping at zero.',
           [click('Sell'),label('Stock: 0'),click('Receive'),label('Stock: 1'),click('Receive'),label('Stock: 2')],
           'Add a Reset button that sets the stock to zero.',[click('Reset'),label('Stock: 0')]),
    window('swing-quantity','swing','javac Cart.java && java Cart','Build a Java Swing app in Cart.java. I enter a quantity in a field labeled Quantity and click Total. Show Total: N where N is the quantity times four. Show Error for invalid text.',
           [typing('Quantity','bad'),click('Total'),label('Error'),typing('Quantity','7'),click('Total'),label('Total: 28')],
           'Add a Double button that doubles the quantity in the field and recalculates the total.',[click('Double'),field('Quantity','14'),label('Total: 56')]),
    window('qt-saved-note','qt','/usr/bin/python3 app.py','Make a PyQt6 desktop notepad in app.py. Give me a Note field and a Save button. Restore the last saved text when I quit and reopen the app.',
           [typing('Note','water seedlings'),click('Save'),{'action':'restart'},field('Note','water seedlings')],
           'Add Clear to empty the saved note too. I want a blank field when I reopen it.',[click('Clear'),field('Note',''),{'action':'restart'},field('Note','')]),
]

CASES = CORE + WEB + DESKTOP

# Verbatim user messages from the November Pocket Tasks release rehearsal.
POCKET = [dict(id='pocket-tasks', family='web', kind='pocket', command='PORT={port} npm start', ready_path='/',
    task='Build “Pocket Tasks,” a small, mobile-friendly web app using plain HTML, CSS, JavaScript, and a Node.js server.\n\nFeatures:\n- A text input labeled “Task,” an “Add” button, and a “Clear” button.\n- Add nonblank tasks to a list. Reject blank input with a visible error.\n- Each task can be marked complete or deleted.\n- Save tasks and their completion state in localStorage so they survive a page reload.\n- Show feedback in a separate display with the stable accessible name “Result.”\n- After adding a task, show “Task added” in Result.\n- Clicking Clear must empty the Task input, Result display, and validation error without removing or changing saved tasks.\n\nProvide npm start, npm test, and a short README. Use Playwright tests that exercise the real interface, including validation, persistence, completion, deletion, and Clear.\n\nWhen DAEDALUS_APP_URL is provided, use it consistently for every test navigation and request. Start a fallback test server only when that variable is absent.\n\nPublish a short implementation plan, keep it updated, and continue building automatically. Run the checks, deliver a preview and downloadable source, and clearly report anything unverified.',
    followup='Change the Clear button so that clicking “Clear” sets the “Result” display to exactly “Ready” instead of leaving it empty.\n\nClear must still empty the Task input and validation error while preserving all saved tasks and completion states. Keep adding, completing, deleting, and reload persistence working.\n\nUpdate the tests for this changed requirement without removing unrelated checks. Verify a real transition: add a task so Result says “Task added,” then click Clear and confirm Result says exactly “Ready.”\n\nRun the regression checks and deliver the updated candidate and preview. Clearly report anything unverified.')]


def breadth_cases():
    import runpy
    from pathlib import Path
    fixtures = runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/hephaestus/qualify_languages.py'))['CASES']
    cases = []
    for language in fixtures:
        name = {'cpp':'C++','csharp':'C#'}.get(language,language)
        cases.append(program('language-'+language,
            f'Write a small command-line program in {name} that adds up signed whole numbers, one per line from stdin. '
            'I want to run make, then ./total. Print the total as a single number. An empty file should give zero. Include tests and a README.',
            ['./total'],[pipe('5\n-7\n4','2\n'),pipe('','0\n')],
            'Add --positive-only to leave out negative numbers. Keep the old default.',
            [pipe('5\n-7\n4','9\n','--positive-only')]))
    return cases

# Start this rehearsal from the accepted dev-python multiplication revision.
REHEARSAL = [program('working-pointer',
    'Add a div command to calc.py for exact integer division. For 84 divided by 7 print 12. If the result is not a whole number, exit 2. Keep add, sub and mul working.',
    ['python3','calc.py'],[{'args':['div','84','7'],'stdout':'12\n'},{'args':['div','5','2'],'code':2},
                         {'args':['mul','-3','7'],'stdout':'-21\n'},{'args':['add','3','4'],'stdout':'7\n'},{'args':['sub','7','9'],'stdout':'-2\n'}],
    'Add a rem command for the remainder when dividing two positive integers. Keep the division command you just added and the older commands.',
    [{'args':['rem','23','7'],'stdout':'2\n'}])]
