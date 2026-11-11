"""Good and broken archives that calibrate the private judge before any model run.

Each pair is the same small program with one behavioral defect. The judge must pass the good one and fail
the broken one; a judge that passes both, or fails both, cannot grade real workflows. These are controls,
never workflow results. `python judge_controls.py DIRECTORY` writes the archives and prints their manifest.
"""
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

_C = ('#include <stdio.h>\n'
      "int main(void){int ch=0,last='\\n',lines=0;while((ch=getchar())!=EOF){if(ch=='\\n')++lines;last=ch;} "
      'printf("%d\\n",lines+EXTRA);return 0;}')
_CPP = ('#include <algorithm>\n#include <iostream>\n'
        'int main(){int a,b; if(!(std::cin>>a>>b)) return 2; std::cout<<OP(a,b)<<"\\n";}')
_POM = ('<project><modelVersion>4.0.0</modelVersion><groupId>test.daedalus</groupId><artifactId>gate1</artifactId><version>1.0</version>\n'
        '<properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target></properties>\n'
        '<build><finalName>JARNAME</finalName><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-jar-plugin</artifactId><version>3.3.0</version>\n'
        '<configuration><archive><manifest><mainClass>Main</mainClass></manifest></archive></configuration></plugin></plugins></build></project>')
_JAVA = 'public class Main { public static void main(String[] args) { System.out.println("Hello, " + args[0] + "!"); } }'
_WEB = ('<!doctype html><label>Amount<input id="amount"></label><label>Percent<input id="percent"></label>\n'
        '<button id="calc">Calculate</button><button id="reset">Reset</button><output id="total"></output>\n<script>\n'
        "const a=document.querySelector('#amount'),p=document.querySelector('#percent'),t=document.querySelector('#total');\n"
        "document.querySelector('#calc').onclick=()=>{t.textContent=(+a.value<0 || +p.value<0) ? 'Error' : (+a.value*(1+Number(p.value)/100)).toFixed(2)};\n"
        "document.querySelector('#reset').onclick=()=>{t.textContent=''; RESET};\n</script>")
_QT = ("from PyQt6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLineEdit,QPushButton,QLabel\n"
       "app=QApplication([]);w=QWidget();l=QVBoxLayout(w);label=QLabel('Celsius');i=QLineEdit();i.setAccessibleName('Celsius')\n"
       "r=QLabel('Ready');c=QPushButton('Convert');b=QPushButton('Reset')\n"
       "def result(value): r.setText(value);r.setAccessibleName(value)\n"
       "def convert():\n    try: result(f'{float(i.text())*9/5+32:.1f}')\n    except ValueError: result('Error')\n"
       "def reset():\n    CLEAR\n    result('Ready')\n"
       "result('Ready');c.clicked.connect(convert);b.clicked.connect(reset)\n"
       "for x in [label,i,r,c,b]:l.addWidget(x)\nw.show();app.exec()\n")
_CMAKE = 'cmake_minimum_required(VERSION 3.16)\nproject(compare LANGUAGES CXX)\nset(CMAKE_CXX_STANDARD 17)\nadd_executable(compare main.cpp)\n'


def controls():
    """Every control: its files, the case the judge is given, and the verdict it must reach."""
    rows = []
    def pair(kind, good, broken, case, labels=('good', 'broken'), followup=False):
        for label, files, expected in ((labels[0], good, True), (labels[1], broken, False)):
            rows.append({'key': f'{kind}-{label}', 'kind': kind, 'variant': label, 'expected': expected,
                         'files': files, 'case': case, 'followup': followup})
    pair('c', {'main.c': _C.replace('EXTRA', "(last!='\\n')"), 'Makefile': 'count: main.c\n\tcc -Wall -o $@ $<\n'},
         {'main.c': _C.replace('EXTRA', '0'), 'Makefile': 'count: main.c\n\tcc -Wall -o $@ $<\n'},
         {'id': 'c', 'family': 'core', 'command': ['./count'], 'probes': [{'stdin': 'one\ntwo', 'stdout': '2\n'}]})
    pair('cpp', {'main.cpp': _CPP.replace('OP', 'std::max'), 'CMakeLists.txt': _CMAKE},
         {'main.cpp': _CPP.replace('OP', 'std::min'), 'CMakeLists.txt': _CMAKE},
         {'id': 'cpp', 'family': 'core', 'command': ['./build/compare'], 'probes': [{'stdin': '-4 8', 'stdout': '8\n'}]})
    pair('java', {'pom.xml': _POM.replace('JARNAME', 'app'), 'src/main/java/Main.java': _JAVA},
         {'pom.xml': _POM.replace('JARNAME', 'other'), 'src/main/java/Main.java': _JAVA},
         {'id': 'java', 'family': 'core', 'command': ['java', '-jar', 'target/app.jar'],
          'probes': [{'args': ['Ada'], 'stdout': 'Hello, Ada!\n'}]})
    # The UI pairs are judged with the frozen development case and its follow-up probes.
    pair('web', {'index.html': _WEB.replace('RESET', "a.value='';p.value=''")}, {'index.html': _WEB.replace('RESET', '')},
         'dev-web', labels=('working', 'broken'), followup=True)
    pair('qt', {'app.py': _QT.replace('CLEAR', 'i.clear()')}, {'app.py': _QT.replace('CLEAR', 'pass')},
         'dev-qt', labels=('working', 'broken'), followup=True)
    return rows


def pack(files):
    """A byte-identical archive for identical files, so a control's hash identifies it across runs."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w') as bundle:
        for name in sorted(files):
            data = files[name].encode()
            info = tarfile.TarInfo(name); info.size, info.mode, info.mtime = len(data), 0o644, 0
            bundle.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode='wb', mtime=0) as stream: stream.write(raw.getvalue())
    return packed.getvalue()


def write(directory):
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    for row in controls():
        data = pack(row['files']); path = directory / (row['key'] + '.tar.gz'); path.write_bytes(data)
        manifest.append({**{k: row[k] for k in ('key', 'kind', 'variant', 'expected', 'case', 'followup')},
                         'storage_path': str(path), 'sha256': hashlib.sha256(data).hexdigest()})
    return manifest


if __name__ == '__main__':
    print(json.dumps(write(sys.argv[1]), indent=2))
