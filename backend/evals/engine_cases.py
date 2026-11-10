"""Qualification specifications and held-out assertions. Never mounted in agent sandboxes."""
from copy import deepcopy

PROMPT_BANK_VERSION = 2
TASK = ('Create a Python standard-library command-line program calc.py. '
        'python3 calc.py add 2 3 prints 5 followed by a newline; sub 9 4 prints 5. '
        'Accept negative integers. Successful operations exit 0. Invalid arguments print an error '
        'to stderr and exit 2. Include unittest tests in test_calc.py and a README with usage. '
        'No third-party dependencies.')


def cli(identity, task, command, probes, followup='Add a --version option that prints 1.0 and exits successfully. Preserve all existing behavior and tests.'):
    return {'id':identity,'family':'core','task':task+' Include automated tests and usage documentation.','command':command,
            'probes':probes,'followup':followup}


def ui(identity,family,task,command,steps,followup,followup_steps,toolkit=''):
    return {'id':identity,'family':family,'task':task+' Include automated tests and a README with launch instructions.',
            'command':command,'kind':'web' if family=='web' else 'desktop','toolkit':toolkit,
            'steps':steps,'followup':followup,'followup_steps':followup_steps}


def click(name):return {'action':'click','name':name,'role':'button'}
def fill(selector,value):return {'action':'fill','selector':selector,'value':value}
def text(selector,value):return {'action':'text','selector':selector,'equals':value}
def value(selector, expected): return {'action':'value','selector':selector,'equals':expected}
def field(name, expected): return {'action':'value','name':name,'equals':expected}
def label(expected): return {'action':'text','name':expected,'role':'label','equals':expected}
def observe(value):return {'action':'observe','contains':value}

def private_probes(case, followup):
    probes = list(case['probes'])
    if followup:
        defaults = {'dev-python': [{'args':['mul','-7','8'],'stdout':'-56\n'}],
                    'dev-java': [{'args':['--loud','Ada'],'stdout':'HELLO, ADA!\n'}]}
        probes.extend(case.get('followup_probes', defaults.get(case['id'], [{'args':['--version'],'stdout':'1.0\n'}])))
    return probes


DEVELOPMENT = [
 cli('dev-python',TASK,['python3','calc.py'],[
     {'args':['add','-17','42'],'stdout':'25\n'}, {'args':['sub','-9','-14'],'stdout':'5\n'},
     {'args':['bad','1','2'],'code':2,'stderr_nonempty':True}],
     'Add a mul command for integer multiplication. Preserve existing add, sub, error and negative-number behavior. Add tests and update the README.'),
 cli('dev-java','Create a Maven Java project that builds target/app.jar with a Main-Class. java -jar target/app.jar Ada prints Hello, Ada! followed by newline. With no name print Hello, world!. Include JUnit tests.',['java','-jar','target/app.jar'],[
     {'args':['Ada'],'stdout':'Hello, Ada!\n'}, {'args':[],'stdout':'Hello, world!\n'}],
     'Add --loud: java -jar target/app.jar --loud Ada prints HELLO, ADA!. Preserve the default greeting, package the executable JAR and add tests.'),
 ui('dev-web','web','Create a plain HTML/JavaScript tip calculator in index.html. Inputs with ids amount and percent, button Calculate, output id total. For amount 20 and percent 10, total displays exactly 22.00 without a currency symbol. Reject negative values with a visible error. No build dependencies.',
    'python3 -m http.server 8000',[fill('#amount','-3'),fill('#percent','10'),click('Calculate'),
     {'action':'observe','feedback_pattern':r'(?i)error|invalid|negative|positive|at least|greater|non.?negative|zero|below'},
     fill('#amount','20'),fill('#percent','10'),click('Calculate'),text('#total','22.00')],
    'Add a Reset button that clears the inputs and result. Preserve calculation and validation.',[value('#amount','20'),value('#percent','10'),click('Reset'),value('#amount',''),value('#percent',''),text('#total',''),
     fill('#amount','9'),fill('#percent','20'),click('Calculate'),text('#total','10.80')]),
 ui('dev-qt','desktop','Create a PyQt6 Celsius-to-Fahrenheit desktop application in app.py, runnable with /usr/bin/python3 app.py. A QLineEdit named Celsius, a Convert button, and a result QLabel that displays the numeric Fahrenheit value to one decimal place. Give the input accessibleName Celsius. Keep the result label accessible name equal to its visible text. Show Error for invalid numbers.',
    '/usr/bin/python3 app.py',[{'action':'fill','name':'Celsius','value':'bad'},click('Convert'),label('Error'),
     {'action':'fill','name':'Celsius','value':'25'},click('Convert'),label('77.0')],
    'Add a Reset button that clears the input and sets the result label to Ready. Preserve conversion and error behavior.',[field('Celsius','25'),click('Reset'),field('Celsius',''),label('Ready'),
     {'action':'fill','name':'Celsius','value':'-40'},click('Convert'),label('-40.0')],toolkit='qt'),
]

# Preserve the original wording for historical reports. Assertions are unchanged.
DEVELOPMENT_V1 = deepcopy(DEVELOPMENT)
for case in DEVELOPMENT:
    if case['id'] == 'dev-qt':
        case['task'] = case['task'].replace('the numeric Fahrenheit value to one decimal place.',
            'only the numeric Fahrenheit value to one decimal place, without units or a prefix.')

CORE = [
 cli('csv-totals','Build totals.py using Python stdlib. It accepts one CSV filepath with header category,amount and prints a JSON object mapping each category to the sum as a two-decimal string. Amounts may be negative decimal numbers. Empty CSV with just a header produces {}. Invalid amounts exit 2 with an error on stderr.', ['python3','totals.py'],[
     {'files':{'rows.csv':'category,amount\nA,1.10\nB,-2.00\nA,2.35\n'},'args':['rows.csv'],'json':{'A':'3.45','B':'-2.00'}},
     {'files':{'rows.csv':'category,amount\n'},'args':['rows.csv'],'json':{}},
     {'files':{'rows.csv':'category,amount\nA,nope\n'},'args':['rows.csv'],'code':2}]),
 cli('json-merge','Build merge.py using Python stdlib. Two JSON file arguments are recursively merged: object keys merge, scalar/null/array values in the second replace the first; absent keys remain. Print the resulting JSON. Invalid JSON exits 2.', ['python3','merge.py'],[
     {'files':{'a.json':'{"a":{"x":1,"y":2},"b":[1],"c":7}', 'b.json':'{"a":{"x":9},"b":[2,3],"c":null}'},'args':['a.json','b.json'],'json':{'a':{'x':9,'y':2},'b':[2,3],'c':None}},
     {'files':{'a.json':'{}','b.json':'bad'},'args':['a.json','b.json'],'code':2}]),
 cli('dedupe-lines','Build dedupe.py using Python stdlib. Read stdin and print each distinct line once, preserving first occurrence and order. Include blank lines. --ignore-case compares lines case-insensitively while retaining the original first spelling. Empty input prints nothing.', ['python3','dedupe.py'],[
     {'stdin':'A\na\nA\n\n\nB\n','args':[],'stdout':'A\na\n\nB\n'},
     {'stdin':'A\na\nB\nb\n','args':['--ignore-case'],'stdout':'A\nB\n'}, {'stdin':'','args':[],'stdout':''}]),
 cli('distance-cli','Build convert.py using Python stdlib. One numeric argument is meters; output feet using exactly 3.28084 feet per meter. Print only the converted number with exactly three digits after the decimal point and a trailing newline, without labels or units. Accept negative values. Invalid input exits 2 with an error on stderr.', ['python3','convert.py'],[
     {'args':['1'],'stdout':'3.281\n'}, {'args':['-2'],'stdout':'-6.562\n'}, {'args':['0'],'stdout':'0.000\n'}, {'args':['bad'],'code':2}]),
 cli('java-range','Create a Maven Java CLI packaged as executable target/app.jar. Two integer arguments low and high: print the inclusive sum from low through high using a long accumulator. If low>high print 0. Invalid arguments exit 2. Include JUnit tests.', ['java','-jar','target/app.jar'],[
     {'args':['-3','3'],'stdout':'0\n'}, {'args':['4','7'],'stdout':'22\n'}, {'args':['9','2'],'stdout':'0\n'}, {'args':['x','2'],'code':2}]),
 cli('java-normalize','Create a Maven Java CLI packaged as executable target/app.jar. It reads all stdin, trims leading/trailing whitespace and collapses each internal whitespace run to one space, printing the normalized text followed by newline. Empty input prints just newline. Include JUnit tests.', ['java','-jar','target/app.jar'],[
     {'stdin':'  one\t two\nthree  ','args':[],'stdout':'one two three\n'}, {'stdin':'','args':[],'stdout':'\n'}]),
 cli('go-histogram','Create a Go module with a CLI. Read whitespace-separated integers from stdin. Print one value:count line per unique integer sorted numerically ascending. Empty input prints nothing; invalid tokens exit 2. No external dependencies. Include go test tests.', ['go','run','.'],[
     {'stdin':'4 -2 4 7 -2 -2','args':[],'stdout':'-2:3\n4:2\n7:1\n'}, {'stdin':'','args':[],'stdout':''}]),
 cli('rust-lines','Create a Rust Cargo CLI with no external dependencies. Read stdin and print total=<N> nonblank=<M> followed by newline. Count lines like str.lines(); whitespace-only lines are blank. Empty input has both counts zero. Include cargo tests.', ['cargo','run','--quiet','--'],[
     {'stdin':'alpha\n \nbeta\n','args':[],'stdout':'total=3 nonblank=2\n'}, {'stdin':'','args':[],'stdout':'total=0 nonblank=0\n'}]),
]

WEB = [
 ui('web-counter','web','Create index.html with a counter starting at 0, output id count, Increment and Reset buttons. Persist the count with localStorage across reloads. Use plain HTML/JS.', 'python3 -m http.server 8000',
    [click('Increment'),click('Increment'),text('#count','2'),{'action':'reload'},text('#count','2'),click('Reset'),text('#count','0')],
    'Add a Decrement button. Negative counts are allowed and persisted. Preserve Increment and Reset.',[click('Decrement'),text('#count','-1'),{'action':'reload'},text('#count','-1')]),
 ui('web-todos','web','Create index.html with input id task, Add button, and list id tasks. Ignore blank input. Added task text must appear in the list and survive reload. Each task has a Delete button. Use plain HTML/JS and localStorage.', 'python3 -m http.server 8000',
    [fill('#task','buy oats'),click('Add'),{'action':'text','selector':'#tasks','contains':'buy oats'},{'action':'reload'},{'action':'text','selector':'#tasks','contains':'buy oats'},click('Delete'),text('#tasks','')],
    'Add a Clear all button that removes all tasks and persists the empty list. Preserve Add and Delete.',[fill('#task','walk'),click('Add'),click('Clear all'),text('#tasks',''),{'action':'reload'},text('#tasks','')]),
 ui('web-distance','web','Create index.html with input id meters, Convert button, output id feet. Use 3.28084 feet per meter and display three decimals, including negative values. Invalid input displays Error. Use plain HTML/JS.', 'python3 -m http.server 8000',
    [fill('#meters','-2'),click('Convert'),text('#feet','-6.562'),fill('#meters','0'),click('Convert'),text('#feet','0.000')],
    'Add Reset that clears meters and feet. Preserve conversion behavior.',[value('#meters','0'),click('Reset'),value('#meters',''),text('#feet','')]),
 ui('web-notes','web','Create index.html with textarea id note, Save and Clear buttons, status output id status. Save persists the exact note to localStorage and shows Saved. Restore the note on reload. Clear empties the note, removes saved content and shows Cleared. Use plain HTML/JS.', 'python3 -m http.server 8000',
    [fill('#note','line one\nline two'),click('Save'),text('#status','Saved'),{'action':'reload'},value('#note','line one\nline two'),click('Clear'),text('#status','Cleared'),value('#note',''),{'action':'reload'},value('#note','')],
    'Add output id length showing only the current note length as an integer, without labels or units, updating on input, restore and clear. Preserve Save and Clear.',[fill('#note','hello'),text('#length','5'),click('Save'),{'action':'reload'},text('#length','5'),click('Clear'),text('#length','0')]),
]

DESKTOP = [
 ui('qt-counter','desktop','Create app.py using PyQt6. Show a count label starting 0 and Increment and Reset buttons. Each Increment adds one. Keep the label accessible name equal to its visible value. Run with /usr/bin/python3 app.py.', '/usr/bin/python3 app.py',
    [click('Increment'),click('Increment'),observe('"name": "2"'),click('Reset'),observe('"name": "0"')],
    'Add Decrement, allowing negative values. Preserve Increment and Reset.',[click('Decrement'),observe('"name": "-1"')],toolkit='qt'),
 ui('qt-list','desktop','Create app.py using PyQt6. A QLineEdit with accessibleName Item, Add button, QListWidget, and Clear button. Add appends nonblank input then clears the input. Clear empties the list. Run with /usr/bin/python3 app.py.', '/usr/bin/python3 app.py',
    [{'action':'fill','name':'Item','value':'apples'},click('Add'),field('Item',''),observe('apples'),click('Clear'),{'action':'observe','absent_name':'apples'}],
    'Add a Count button and label that show the number of list items when Count is clicked, as Count: N. Preserve Add and Clear.',[click('Count'),observe('Count: 0')],toolkit='qt'),
 ui('gtk-counter','desktop','Create app.py using system Python and GTK4 via gi. A visible numeric count label starts 0. Increment adds one, Reset sets zero. Run with /usr/bin/python3 app.py. Keep controls accessible through AT-SPI.', '/usr/bin/python3 app.py',
    [click('Increment'),click('Increment'),observe('"name": "2"'),click('Reset'),observe('"name": "0"')],
    'Add Decrement allowing negative values. Preserve Increment and Reset.',[click('Decrement'),observe('"name": "-1"')],toolkit='gtk'),
 ui('swing-counter','desktop','Create Counter.java using Java Swing. A numeric JLabel starts at 0. Increment button adds one and Reset resets to zero. Use the visible label value as its accessible name. Compile with javac Counter.java and run java Counter.', 'javac Counter.java && java Counter',
    [click('Increment'),click('Increment'),observe('"name": "2"'),click('Reset'),observe('"name": "0"')],
    'Add a Decrement button allowing negative values. Preserve Increment and Reset.',[click('Decrement'),observe('"name": "-1"')],toolkit='swing'),
]
CASES=CORE+WEB+DESKTOP
CASES_V1 = deepcopy(CASES)
for case in CASES:
    if case['id'] == 'web-distance':
        case['task'] = case['task'].replace('display three decimals, including negative values.',
            'display only the converted number with three decimals, without labels or units, including negative values.')
    if case['id'] in {'qt-counter', 'gtk-counter', 'swing-counter'}:
        case['task'] += ' The count label must contain only the integer, without a prefix or units.'
