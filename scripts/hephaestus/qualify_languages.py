"""Serial, controller-owned language smoke checks bound to the installed image."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

CASES = {
 'python':'print("HEPHAESTUS42")',
 'javascript':'console.log("HEPHAESTUS42")',
 'typescript':'const value: string = "HEPHAESTUS42"; console.log(value);',
 'bash':'printf "HEPHAESTUS42\\n"',
 'ruby':'puts "HEPHAESTUS42"',
 'php':'<?php echo "HEPHAESTUS42\\n"; ?>',
 'perl':'print "HEPHAESTUS42\\n";',
 'lua':'print("HEPHAESTUS42")',
 'r':'cat("HEPHAESTUS42\\n")',
 'groovy':'println "HEPHAESTUS42"',
 'elixir':'IO.puts("HEPHAESTUS42")',
 'dart':'void main() { print("HEPHAESTUS42"); }',
 'scala':'object Main extends App { println("HEPHAESTUS42") }',
 'ocaml':'print_endline "HEPHAESTUS42";;',
 'c':'#include <stdio.h>\nint main(void) { puts("HEPHAESTUS42"); return 0; }',
 'cpp':'#include <iostream>\nint main() { std::cout << "HEPHAESTUS42\\n"; }',
 'java':'public class Main { public static void main(String[] args) { System.out.println("HEPHAESTUS42"); } }',
 'go':'package main\nimport "fmt"\nfunc main() { fmt.Println("HEPHAESTUS42") }',
 'rust':'fn main() { println!("HEPHAESTUS42"); }',
 'csharp':'class MainClass { static void Main() { System.Console.WriteLine("HEPHAESTUS42"); } }',
 'kotlin':'fun main() { println("HEPHAESTUS42") }',
 'haskell':'main = putStrLn "HEPHAESTUS42"',
 'fortran':'program main\n print *, "HEPHAESTUS42"\nend program main\n',
 'cobol':'       IDENTIFICATION DIVISION.\n       PROGRAM-ID. HELLO.\n       PROCEDURE DIVISION.\n           DISPLAY "HEPHAESTUS42"\n           STOP RUN.\n',
 'ada':'with Ada.Text_IO;\nprocedure Main is begin Ada.Text_IO.Put_Line("HEPHAESTUS42"); end Main;\n',
 'pascal':"program main; begin writeln('HEPHAESTUS42'); end.",
 'nim':'echo "HEPHAESTUS42"',
 'zig':'const std = @import("std"); pub fn main() void { std.debug.print("HEPHAESTUS42\\n", .{}); }',
 'v':"fn main() { println('HEPHAESTUS42') }",
}


def main():
    release=Path('/etc/hephaestus/current-release').read_text().strip()
    sys.path.insert(0,release+'/backend')
    from coder_sandbox import Sandbox
    from hephaestus_languages import LANGUAGES
    from hephaestus_scope import scope
    os.environ['HEPHAESTUS_EXECUTOR']='docker'
    os.environ['TMPDIR']='/srv/hephaestus/runs/generic'
    tempfile.tempdir=None
    config_path=Path('/etc/hephaestus/runtime.json')
    config=json.loads(config_path.read_text()); image=config['profiles']['languages']['image']
    directory=Path('/srv/hephaestus/projects')/('language-check-'+uuid.uuid4().hex);directory.mkdir()
    rows={}
    for language,source in CASES.items():
        cfg=LANGUAGES[language]; work=directory/language;work.mkdir()
        name='Main.java' if language=='java' else 'main'+cfg['ext']
        (work/name).write_text(source)
        (work/'tsconfig.json').write_text('{"compilerOptions":{"module":"CommonJS"}}')
        values={'file':shlex.quote(name),'filename':shlex.quote(name),'dir':str(work),
                'output':str(work/'program'),'stem':'main','classname':'Main' if language=='java' else 'main'}
        command=((cfg['compile'].format(**values)+' && ') if cfg.get('compile') else '')+cfg['run'].format(**values)
        started=time.monotonic()
        with scope(owner='controller-language-qualification',project=language):
            with Sandbox(['bash','-c',command],cwd=work,writable=[work],runtime_profile='languages',timeout=60,
                         environment={'PATH':'/usr/local/bin:/usr/bin:/bin','JAVA_HOME':'/usr/lib/jvm/default-java',
                                      'GOCACHE':str(work/'go-cache')}) as box:
                result=subprocess.run(box.args,env=box.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=70)
        passed=result.returncode==0 and 'HEPHAESTUS42' in result.stdout
        rows[language]={'passed':passed,'exit_code':result.returncode,'image':image,'command':command,
                        'seconds':round(time.monotonic()-started,2),'output':result.stdout[-4000:],
                        'fixture_sha256':hashlib.sha256(source.encode()).hexdigest()}
        print(json.dumps({'language':language,**rows[language]}),flush=True)
        (directory/'results.json').write_text(json.dumps(rows,indent=2))
    # Aliases share the exact compile/run implementation; each canonical case ran.
    for name,cfg in LANGUAGES.items():
        if name not in rows:
            alias=next((k for k in CASES if LANGUAGES[k]==cfg),None)
            rows[name]={**rows[alias],'alias':alias} if alias else {'passed':False,'reason':'Not advertised as available on the original Codebox','image':image}
    current=json.loads(config_path.read_text())
    if current['profiles']['languages']['image']!=image: raise RuntimeError('Runtime changed during qualification')
    current['languages']=rows
    temporary=config_path.with_suffix('.tmp');temporary.write_text(json.dumps(current,indent=2)+'\n');temporary.chmod(0o600);temporary.replace(config_path)
    print(json.dumps({'evidence':str(directory/'results.json'),'passed':sum(rows[k]['passed'] for k in CASES),'total':len(CASES)}))
    if any(not rows[k]['passed'] for k in CASES): sys.exit(1)


if __name__=='__main__': main()
