"""Independent verdict for a delivered from-scratch build: build it, run its delivered
tests with the native runner, and compare exact program output on fixed fixtures.
Runs as its own process and never imports controller evidence."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tarfile
from pathlib import Path


def run(command, cwd, timeout=600, env=None):
    result = subprocess.run(command, cwd=cwd, shell=isinstance(command, str), capture_output=True, text=True,
                            timeout=timeout, env={**os.environ, **(env or {})})
    return result.returncode, result.stdout, result.stderr


def find(root, *names):
    for name in names:
        hits = sorted(p for p in root.rglob(name) if not {'node_modules', 'target', 'build', 'bin', 'obj', '.venv'} & set(p.relative_to(root).parts))
        if hits:
            return hits[0]
    return None


def expect(checks, name, command, cwd, stdout=None, code=0, contains=None, env=None):
    try:
        actual_code, out, err = run(command, cwd, env=env)
        ok = actual_code == code and (stdout is None or out.strip() == stdout.strip()) and (contains is None or contains in out)
        checks.append({'name': name, 'passed': ok, 'detail': None if ok else {'command': command if isinstance(command, str) else ' '.join(map(str, command)),
                       'exit': actual_code, 'stdout': out[-600:], 'stderr': err[-600:], 'expected': stdout if stdout is not None else contains, 'expected_exit': code}})
    except Exception as error:  # toolchain/timeout problems are failures of the delivery, reported verbatim
        checks.append({'name': name, 'passed': False, 'detail': type(error).__name__ + ': ' + str(error)[:400]})
    return checks[-1]['passed']


def verify(language, root, work):
    checks = []
    readme = find(root, 'README.md', 'README*')
    checks.append({'name': 'README', 'passed': bool(readme and len(readme.read_text(errors='replace').strip()) > 40), 'detail': None})
    text = work / 'sample.txt'; text.write_text('The cat. the CAT! A dog, the end?\n')
    if language == 'python':
        script = find(root, 'wordfreq.py'); base = script.parent if script else root
        tests = [p for p in root.rglob('test*.py')]
        expect(checks, 'delivered tests', [sys.executable, '-m', 'pytest', '-q'] if tests else 'false', base)
        expect(checks, 'top 2', [sys.executable, 'wordfreq.py', str(text), '--top', '2'], base, stdout='the 3\ncat 2')
        code, out, _ = run([sys.executable, 'wordfreq.py', str(text), '--json'], base)
        try: ok = code == 0 and json.loads(out).get('the') == 3 and json.loads(out).get('dog') == 1
        except ValueError: ok = False
        checks.append({'name': 'json', 'passed': ok, 'detail': None if ok else out[-300:]})
        expect(checks, 'missing file', [sys.executable, 'wordfreq.py', str(work / 'nope.txt')], base, code=2, stdout='')
    elif language == 'java':
        pom = find(root, 'pom.xml'); base = pom.parent if pom else root
        expect(checks, 'mvn test', 'mvn -q test', base)
        expect(checks, 'package', 'mvn -q -DskipTests package', base)
        java = ['java', '-cp', 'target/classes', 'app.UnitConv']
        expect(checks, 'km to m', [*java, '1.5', 'km', 'm'], base, stdout='1500.00')
        expect(checks, 'c to f', [*java, '100', 'c', 'f'], base, stdout='212.00')
        expect(checks, 'incompatible', [*java, '1', 'km', 'c'], base, code=2, stdout='')
    elif language in {'c', 'cpp'}:
        cmake = find(root, 'CMakeLists.txt'); base = cmake.parent if cmake else root
        expect(checks, 'configure+build', 'cmake -S . -B vbuild >/dev/null && cmake --build vbuild', base)
        expect(checks, 'ctest', 'ctest --test-dir vbuild --output-on-failure', base, contains='tests passed')
        exe = 'rpncalc' if language == 'c' else 'matrixtool'
        binary = next((str(p) for p in (base / 'vbuild').rglob(exe) if p.is_file() and os.access(p, os.X_OK)), 'vbuild/' + exe)
        if language == 'c':
            expect(checks, 'expression', [binary, '3 4 + 2 *'], base, stdout='14.00')
            expect(checks, 'division', [binary, '7 2 /'], base, stdout='3.50')
            expect(checks, 'divide by zero', [binary, '1 0 /'], base, code=2, stdout='')
        else:
            expect(checks, 'det', [binary, 'det', '1,2;3,4'], base, stdout='-2.00')
            expect(checks, 'transpose', [binary, 'transpose', '1,2;3,4'], base, stdout='1,3;2,4')
            expect(checks, 'non-square det', [binary, 'det', '1,2,3;4,5,6'], base, code=2, stdout='')
    elif language == 'csharp':
        sln = find(root, '*.sln'); base = sln.parent if sln else root
        env = {'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_NOLOGO': '1'}
        expect(checks, 'dotnet test', 'dotnet test', base, contains='Passed!', env=env)
        csv = work / 'data.csv'; csv.write_text('name,score\na,1.5\nb,4\nc,2\n')
        app = find(root, 'CsvStats.csproj')
        # Build first and run with --no-build: `dotnet run` otherwise prints compiler warnings on stdout.
        expect(checks, 'dotnet build', 'dotnet build -v q', base, env=env)
        command = ['dotnet', 'run', '--no-build', '--project', str(app) if app else 'CsvStats', '--']
        expect(checks, 'stats', [*command, str(csv), '--column', 'score'], base, stdout='min 1.50\nmax 4.00\nmean 2.50', env=env)
        expect(checks, 'unknown column', [*command, str(csv), '--column', 'nope'], base, code=2, stdout='', env=env)
    elif language == 'go':
        mod = find(root, 'go.mod'); base = mod.parent if mod else root
        env = {'GOCACHE': str(work / 'gocache'), 'GOFLAGS': '-mod=mod'}
        expect(checks, 'go vet+test', 'go vet ./... && go test ./...', base, env=env)
        log = work / 'app.log'; log.write_text('INFO started\nERROR boom\nINFO done\nWARN slow\nINFO bye\n')
        # `go run` always exits 1 for a failing child, so exit codes are checked on the built binary.
        binary = str(work / 'logsummary-bin')
        expect(checks, 'go build', ['go', 'build', '-o', binary, '.'], base, env=env)
        expect(checks, 'summary', [binary, str(log)], base, stdout='ERROR 1\nINFO 3\nWARN 1')
        expect(checks, 'level flag', [binary, '--level', 'INFO', str(log)], base, contains='3')
        expect(checks, 'missing file', [binary, str(work / 'nope.log')], base, code=2, stdout='')
    elif language == 'rust':
        cargo = find(root, 'Cargo.toml'); base = cargo.parent if cargo else root
        expect(checks, 'cargo test', 'cargo test --quiet', base)
        expect(checks, 'encode', 'cargo run --quiet -- encode "hello world"', base, stdout='aGVsbG8gd29ybGQ=')
        expect(checks, 'decode', 'cargo run --quiet -- decode aGVsbG8gd29ybGQ=', base, stdout='hello world')
        expect(checks, 'invalid', 'cargo run --quiet -- decode "%%%"', base, code=2, stdout='')
    elif language == 'node':
        script = find(root, 'mdtoc.js'); base = script.parent if script else root
        expect(checks, 'npm test', 'npm test --silent', base)
        md = work / 'doc.md'; md.write_text('# Intro\n\n## Getting Started!\n\ntext\n\n### Deep Dive\n\n## API v2\n')
        expect(checks, 'toc', ['node', 'mdtoc.js', str(md)], base,
               stdout='- [Intro](#intro)\n  - [Getting Started!](#getting-started)\n    - [Deep Dive](#deep-dive)\n  - [API v2](#api-v2)')
        expect(checks, 'max depth', ['node', 'mdtoc.js', str(md), '--max-depth', '2'], base,
               stdout='- [Intro](#intro)\n  - [Getting Started!](#getting-started)\n  - [API v2](#api-v2)')
        expect(checks, 'missing file', ['node', 'mdtoc.js', str(work / 'nope.md')], base, code=2, stdout='')
    elif language == 'kanban':
        from verify_webapp import kanban
        checks.extend(kanban(root))
    else:
        checks.append({'name': 'language', 'passed': False, 'detail': 'Unknown language ' + language})
    return [c for c in checks if c]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--folder', required=True); parser.add_argument('--archive', required=True)
    parser.add_argument('--language', required=True); args = parser.parse_args()
    folder = Path(args.folder); source = folder / 'verify-source'; work = folder / 'verify-work'
    source.mkdir(exist_ok=True); work.mkdir(exist_ok=True)
    with tarfile.open(args.archive) as bundle:
        bundle.extractall(source, filter='data')
    checks = verify(args.language, source, work)
    (folder / 'golden.json').write_text(json.dumps({'passed': bool(checks) and all(c['passed'] for c in checks), 'checks': checks}, indent=2))
