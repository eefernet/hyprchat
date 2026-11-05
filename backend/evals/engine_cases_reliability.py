"""Fresh qualification cohort. Assertions are never mounted in the coding sandbox."""
from engine_cases import cli,ui,click,fill,text,value,field


def label(value):return {'action':'observe','role':'label','contains':'"name": '+__import__('json').dumps(value)}
def entry(name,value):return {'action':'fill','name':name,'value':value}

CORE=[
 cli('invoice-summary','Create invoice.py using Python stdlib. Read a CSV path with columns item,quantity,unit_price. Print a JSON object mapping each item to an object with keys quantity (total integer quantity) and cost (total decimal string with two places). Repeated items combine; quantities are nonnegative integers and prices are nonnegative decimals. Invalid rows exit 2 with stderr. Header-only input produces {}. Use exact decimal arithmetic.', ['python3','invoice.py'],[
  {'files':{'in.csv':'item,quantity,unit_price\npen,3,0.10\nbook,2,7.25\npen,2,0.20\n'},'args':['in.csv'],'json':{'pen':{'quantity':5,'cost':'0.70'},'book':{'quantity':2,'cost':'14.50'}}},
  {'files':{'in.csv':'item,quantity,unit_price\n'},'args':['in.csv'],'json':{}},
  {'files':{'in.csv':'item,quantity,unit_price\nx,-1,2.00\n'},'args':['in.csv'],'code':2,'stderr_nonempty':True}]),
 cli('json-path','Create select.py using Python stdlib. Arguments are a JSON filepath and a dot-separated object-key path. Print the selected value as JSON. An empty path selects the whole document. Numeric path segments are object keys, never array indices. Missing keys, non-object intermediates, invalid JSON, or incorrect argument count exit 2 with stderr.', ['python3','select.py'],[
  {'files':{'in.json':'{"a":{"b":[1,null]},"0":"zero"}'},'args':['in.json','a.b'],'json':[1,None]},
  {'files':{'in.json':'{"0":"zero"}'},'args':['in.json','0'],'json':'zero'},
  {'files':{'in.json':'{"x":1}'},'args':['in.json',''],'json':{'x':1}},
  {'files':{'in.json':'{"x":1}'},'args':['in.json','x.y'],'code':2,'stderr_nonempty':True}]),
 cli('line-rotate','Create rotate.py using Python stdlib. One integer argument N rotates stdin lines left by N modulo line count; negative N rotates right. Preserve blank lines and each line content. Output each resulting line with a newline. Empty input produces nothing. Invalid N exits 2.', ['python3','rotate.py'],[
  {'args':['1'],'stdin':'red\n\nblue\n','stdout':'\nblue\nred\n'},
  {'args':['-1'],'stdin':'a\nb\nc\n','stdout':'c\na\nb\n'},
  {'args':['7'],'stdin':'x\ny\n','stdout':'y\nx\n'}, {'args':['0'],'stdin':'','stdout':''}, {'args':['bad'],'code':2}]),
 cli('date-gap','Create days.py using Python stdlib. Two ISO YYYY-MM-DD arguments produce the signed number of days from the first date to the second, with newline and no label. Validate real calendar dates and reject other date formats. Invalid input exits 2 with stderr.', ['python3','days.py'],[
  {'args':['2024-02-28','2024-03-01'],'stdout':'2\n'}, {'args':['2023-03-01','2023-02-28'],'stdout':'-1\n'},
  {'args':['2026-10-30','2026-10-30'],'stdout':'0\n'}, {'args':['2023-02-29','2023-03-01'],'code':2,'stderr_nonempty':True},
  {'args':['20240101','2024-01-02'],'code':2,'stderr_nonempty':True}]),
 cli('java-clamp','Create a Maven Java CLI packaged as executable target/app.jar. Arguments are integer value, low, high. Print value clamped to the inclusive range [low,high], followed by newline. Use signed long values. Reject low>high, invalid numbers, and incorrect argument count with exit 2. Include JUnit tests.', ['java','-jar','target/app.jar'],[
  {'args':['20','-5','8'],'stdout':'8\n'}, {'args':['-9','-5','8'],'stdout':'-5\n'}, {'args':['4','-5','8'],'stdout':'4\n'},
  {'args':['3000000000','0','4000000000'],'stdout':'3000000000\n'}, {'args':['1','8','-5'],'code':2}]),
 cli('java-palindrome','Create a Maven Java CLI packaged as executable target/app.jar. Read all stdin. Keep only ASCII letters and digits, compare case-insensitively, and print true if the resulting text is a palindrome or false otherwise, followed by newline. Empty normalized text is a palindrome. Include JUnit tests.', ['java','-jar','target/app.jar'],[
  {'stdin':'A man, a plan, a canal: Panama!\n','stdout':'true\n'}, {'stdin':'ab12ba','stdout':'false\n'},
  {'stdin':'12--21','stdout':'true\n'}, {'stdin':'  !!!\n','stdout':'true\n'}]),
 cli('go-word-count','Create a Go module CLI with no external dependencies. Read stdin whitespace-separated words. Lowercase words, retain punctuation as part of a word, then print word=count lines sorted lexicographically by word. Empty input prints nothing. Include Go tests.', ['go','run','.'],[
  {'stdin':'Zed alpha ZED\nBeta alpha','stdout':'alpha=2\nbeta=1\nzed=2\n'},
  {'stdin':'Hi! hi hi!','stdout':'hi=1\nhi!=2\n'}, {'stdin':'','stdout':''}]),
 cli('rust-tsv-shape','Create a Rust Cargo CLI without external dependencies. Read stdin as TSV rows using str.lines(). Print rows=<N> columns=<M> and newline, where M is the largest field count of any row, splitting each row on tab and preserving empty fields. Empty input yields rows=0 columns=0; a blank row has one field. Include Cargo tests.', ['cargo','run','--quiet','--'],[
  {'stdin':'a\tb\n\tx\t\n','stdout':'rows=2 columns=3\n'}, {'stdin':'\n','stdout':'rows=1 columns=1\n'}, {'stdin':'','stdout':'rows=0 columns=0\n'}]),
]

WEB=[
 ui('web-basket','web','Create index.html in plain HTML/JS. Inputs id price and quantity, button Calculate, output id total. Price is a nonnegative decimal and quantity a nonnegative integer. Total is price times quantity with exactly two decimals. Invalid inputs display Error in total. No build dependencies.', 'python3 -m http.server 8000',
  [fill('#price','2.50'),fill('#quantity','3'),click('Calculate'),text('#total','7.50'),fill('#quantity','1.5'),click('Calculate'),text('#total','Error')],
  'Add Reset clearing both inputs and total. Preserve calculation and validation.',[value('#price','2.50'),value('#quantity','1.5'),click('Reset'),value('#price',''),value('#quantity',''),text('#total','')]),
 ui('web-filter','web','Create index.html in plain HTML/JS with text input id query and list id results. The source items are Amber, Blue, Coral, Indigo in that order. Update results on input to show case-insensitive substring matches, preserving order. Empty query shows all four. Show output id count with the number of visible items as a bare integer.', 'python3 -m http.server 8000',
  [fill('#query','BL'),text('#results','Blue'),text('#count','1'),fill('#query','zzz'),text('#count','0')],
  'Add Clear button that empties query and restores all four results. Preserve filtering.',[value('#query','zzz'),click('Clear'),value('#query',''),text('#count','4')]),
 ui('web-temperature','web','Create index.html in plain HTML/JS. Input id celsius, button Convert, output id fahrenheit. Convert C to F using C*9/5+32, exactly one decimal, no unit text. Reject blank or nonnumeric input with Error in fahrenheit.', 'python3 -m http.server 8000',
  [fill('#celsius','-40'),click('Convert'),text('#fahrenheit','-40.0'),fill('#celsius','100'),click('Convert'),text('#fahrenheit','212.0'),fill('#celsius',''),click('Convert'),text('#fahrenheit','Error')],
  'Add Swap button that takes the currently valid Fahrenheit result as a new Celsius input and recalculates. Preserve conversion and errors.',[fill('#celsius','0'),click('Convert'),click('Swap'),text('#fahrenheit','89.6')]),
 ui('web-text-metrics','web','Create index.html in plain HTML/JS with textarea id draft, outputs id characters and words. Update both on input: characters counts string length, words counts nonempty whitespace-separated tokens. Persist the exact draft in localStorage and restore both metrics after reload. Outputs contain bare integers.', 'python3 -m http.server 8000',
  [fill('#draft','one  two\nthree'),text('#characters','14'),text('#words','3'),{'action':'reload'},text('#characters','14'),text('#words','3')],
  'Add Clear that clears and persists an empty draft and resets both metrics to 0. Preserve editing and restoration.',[click('Clear'),value('#draft',''),text('#words','0'),{'action':'reload'},value('#draft',''),text('#characters','0')]),
]

DESKTOP=[
 ui('qt-invoice','desktop','Create app.py using PyQt6, run with /usr/bin/python3 app.py. Two QLineEdits with accessible names Price and Quantity, Calculate button, QLabel result. Price is nonnegative decimal and Quantity nonnegative integer; show Total: <amount> with two decimals or Error for invalid input. Keep result accessible name equal to visible text.', '/usr/bin/python3 app.py',
  [entry('Price','2.50'),entry('Quantity','3'),click('Calculate'),label('Total: 7.50'),entry('Quantity','bad'),click('Calculate'),label('Error')],
  'Add Reset clearing both fields and setting result to Ready. Preserve calculation and validation.',[field('Price','2.50'),field('Quantity','bad'),click('Reset'),field('Price',''),field('Quantity',''),label('Ready')],toolkit='qt'),
 ui('qt-range','desktop','Create app.py using PyQt6, run with /usr/bin/python3 app.py. A QLabel displays Value: 5 initially, buttons Increase, Decrease, Reset. Increase and Decrease adjust by one within inclusive 0..10, clamping at bounds. Reset returns to 5. Keep result accessible name equal to visible text.', '/usr/bin/python3 app.py',
  [*[click('Decrease') for _ in range(7)],label('Value: 0'),click('Increase'),label('Value: 1'),click('Reset'),label('Value: 5')],
  'Add Maximum button that sets value to 10. Preserve Increase, Decrease, bounds and Reset.',[click('Maximum'),click('Increase'),label('Value: 10')],toolkit='qt'),
 ui('gtk-temperature','desktop','Create app.py using system Python with GTK4 via gi, run /usr/bin/python3 app.py. Entry with accessible label Celsius, Convert button and result label. Convert C to F as C*9/5+32. Display Fahrenheit: <value> with one decimal or Error for invalid input. Expose controls through AT-SPI.', '/usr/bin/python3 app.py',
  [entry('Celsius','-40'),click('Convert'),label('Fahrenheit: -40.0'),entry('Celsius','bad'),click('Convert'),label('Error')],
  'Add Reset clearing Celsius and setting result label to Ready. Preserve conversion and error handling.',[field('Celsius','bad'),click('Reset'),field('Celsius',''),label('Ready')],toolkit='gtk'),
 ui('swing-length','desktop','Create Length.java using Java Swing. Compile javac Length.java and run java Length. JTextField with accessible name Text, Count button, result JLabel. Count displays Length: N for the exact input string length including spaces. Initial result is Ready. Keep result accessible name equal to visible text.', 'javac Length.java && java Length',
  [entry('Text','a b'),click('Count'),label('Length: 3'),entry('Text',''),click('Count'),label('Length: 0')],
  'Add Clear that empties the input and sets result to Ready. Preserve Count.',[entry('Text','hello'),field('Text','hello'),click('Clear'),field('Text',''),label('Ready')],toolkit='swing'),
]
CASES=CORE+WEB+DESKTOP
