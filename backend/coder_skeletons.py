"""Verified minimal starting structures for the new-build stacks that fail from scratch.

Uploaded repairs pass the independent checks 6/6 while greenfield web and .NET builds went 0/4 in
frozen-7 (2026-09-24): an empty .NET solution, a `database.js` whose exports the server never called,
`sqlite3` declared as a pip dependency, a missing route behind the Edit button. A skeleton turns a
new build into the mode that works: the builder starts from files that already install, build, launch
and run their (placeholder) tests, and extends them. Infrastructure only: no request logic, no benchmark
solutions. Explicitly requested file names are preserved because the skeleton is applied only when the
request matches its stack and the builder is told to keep the layout; unsupported stacks keep the general
builder. Embedded as Python data because the worker bundle ships flat modules only.

A skeleton's placeholder files carry NO evidence: the `skeleton:placeholder` check row blocks while any
replace-marked file is byte-identical to the template, so a smoke test can never stand in for delivered
tests, and the builder is told in its guidance that the skeleton implements none of the requested behavior.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

_STATIC = {
    'index.html': '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>App</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<main id="app">
  <h1>App</h1>
</main>
<script src="app.js"></script>
</body>
</html>
''',
    'app.js': '''// Application logic. Wire the controls the request names to the DOM here.
document.addEventListener('DOMContentLoaded', () => {
  // Implement the requested behavior.
});
''',
    'style.css': ''':root { font-family: system-ui, sans-serif; color-scheme: light dark; }
body { max-width: 48rem; margin: 2rem auto; padding: 0 1rem; }
''',
    'tests/test_browser.py': '''"""Browser regression checks. Extend with the requested behavior; keep plain assert statements."""
import os

from playwright.sync_api import sync_playwright


def main():
    url = os.environ['DAEDALUS_APP_URL']
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/', wait_until='load')
        assert page.locator('main').count() == 1, 'main element missing'
        assert not errors, 'page errors: ' + ' | '.join(errors)
        browser.close()


if __name__ == '__main__':
    main()
''',
    '.daedalus.json': '{"packages": {".": {"test": "\\"$DAEDALUS_AUDIT_PYTHON\\" tests/test_browser.py"}}}\n',
    'README.md': '''# App

A dependency-free browser application. Open `index.html`, or serve the folder:

    python3 -m http.server 8000

## Checks

    python3 tests/test_browser.py

The browser checks use Python Playwright and read the served address from `DAEDALUS_APP_URL`.
''',
}

_EXPRESS = {
    'package.json': '''{
  "name": "app",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "start": "node server.js",
    "test": "node --test tests/*.test.js"
  },
  "dependencies": {
    "better-sqlite3": "^11.7.0",
    "express": "^4.21.2"
  }
}
''',
    'database.js': ''''use strict';
// SQLite access for the application. server.js imports ONLY the names exported at the bottom.
const path = require('path');
const Database = require('better-sqlite3');

const DB_PATH = process.env.APP_DB_PATH || path.join(__dirname, 'data.sqlite');
let db;

function connection() {
  if (!db) db = new Database(DB_PATH);
  return db;
}

// Create every table the application needs. Idempotent: runs on every start.
function init() {
  connection().exec('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)');
  return db;
}

function run(sql, params = []) { return connection().prepare(sql).run(params); }
function get(sql, params = []) { return connection().prepare(sql).get(params); }
function all(sql, params = []) { return connection().prepare(sql).all(params); }

module.exports = { init, run, get, all, DB_PATH };
''',
    'server.js': ''''use strict';
const path = require('path');
const express = require('express');
const { init, run, get, all } = require('./database');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

app.get('/api/health', (req, res) => res.json({ ok: true }));

// Add the requested API routes here, using run/get/all from ./database.

const PORT = Number(process.env.PORT) || 3000;
const HOST = process.env.HOST || '127.0.0.1';
if (require.main === module) {
  init();
  app.listen(PORT, HOST, () => console.log(`listening on http://${HOST}:${PORT}`));
}
module.exports = app;
''',
    'public/index.html': '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>App</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<main id="app">
  <h1>App</h1>
  <p id="status">loading</p>
</main>
<script src="app.js"></script>
</body>
</html>
''',
    'public/style.css': ''':root { font-family: system-ui, sans-serif; color-scheme: dark; }
body { max-width: 64rem; margin: 2rem auto; padding: 0 1rem; }
''',
    'public/app.js': '''// PLACEHOLDER: replace this whole file with the requested front-end.
// Front-end logic. Talk to the server through /api routes; never assume unrequested endpoints exist.
document.addEventListener('DOMContentLoaded', async () => {
  const status = document.getElementById('status');
  try {
    const response = await fetch('/api/health');
    status.textContent = response.ok ? 'ready' : 'server error ' + response.status;
  } catch (error) {
    status.textContent = 'server unreachable';
  }
});
''',
    'tests/api.test.js': ''''use strict';
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

// Uses the controller-managed server when DAEDALUS_APP_URL is set; otherwise starts the app in-process.
// Inside the verification sandbox the managed URL is reachable only through DAEDALUS_HTTP_PROXY
// (fetch() ignores proxy variables), so requests go through node:http with the proxy when present.
let url = process.env.DAEDALUS_APP_URL || '';
let server;

function request(path, method = 'GET', body) {
  return new Promise((resolve, reject) => {
    const target = new URL(path, url);
    const direct = ['127.0.0.1', 'localhost'].includes(target.hostname) || !process.env.DAEDALUS_HTTP_PROXY;
    const proxy = direct ? null : new URL(process.env.DAEDALUS_HTTP_PROXY);
    const options = proxy
      ? { host: proxy.hostname, port: proxy.port, path: target.href, method, headers: { Host: target.host, 'Content-Type': 'application/json' } }
      : { host: target.hostname, port: target.port, path: target.pathname + target.search, method, headers: { 'Content-Type': 'application/json' } };
    const req = http.request(options, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, json: data ? JSON.parse(data) : null }));
    });
    req.on('error', reject);
    if (body !== undefined) req.write(JSON.stringify(body));
    req.end();
  });
}

before(async () => {
  if (url) return;
  process.env.APP_DB_PATH = require('path').join(require('os').tmpdir(), `app-test-${process.pid}.sqlite`);
  const app = require('../server');
  require('../database').init();
  server = await new Promise(resolve => { const s = app.listen(0, '127.0.0.1', () => resolve(s)); });
  url = `http://127.0.0.1:${server.address().port}`;
});

after(() => { if (server) server.close(); });

test('PLACEHOLDER — replace with the requested API tests', async () => {
  const response = await request('/api/health');
  assert.equal(response.status, 200);
  assert.deepEqual(response.json, { ok: true });
});
''',
    '.daedalus-run.json': '{"dependencies": {}, "services": [{"id": "app", "cwd": ".", "command": "PORT={port} HOST=127.0.0.1 node server.js", "ready_path": "/api/health"}]}\n',
    '.gitignore': 'node_modules/\n*.sqlite\n*.sqlite-journal\n',
    'README.md': '''# App

Express + SQLite (better-sqlite3) with a static front end served from `public/`.

## Install and run

    npm install
    npm start

The server listens on `PORT` (default 3000). `APP_DB_PATH` selects the SQLite file (default `data.sqlite`).

## Test

    npm test
''',
}

_REACT_FASTAPI = {
    'app.py': '''"""FastAPI application: JSON API under /api, built React frontend (frontend/dist) served at /."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db

DIST = Path(__file__).resolve().parent / 'frontend' / 'dist'


@asynccontextmanager
async def lifespan(application):
    db.init()
    # Mounted at startup so it stays behind every /api route registered below.
    if DIST.is_dir():
        application.mount('/', StaticFiles(directory=DIST, html=True), name='frontend')
    yield


app = FastAPI(lifespan=lifespan)


@app.get('/api/health')
def health():
    return {'ok': True}

# Add the requested /api routes here.
''',
    'db.py': '''"""SQLite storage. APP_DB_PATH selects the database file."""
import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get('APP_DB_PATH') or str(Path(__file__).resolve().parent / 'data.sqlite')


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init():
    """Create every table the application needs. Idempotent."""
    with connect() as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
''',
    'requirements.txt': 'fastapi>=0.115,<1.0\nuvicorn>=0.30,<1.0\n',
    'frontend/package.json': '''{
  "name": "frontend",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "build": "vite build"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "vite": "^5.4.11"
  }
}
''',
    'frontend/vite.config.js': '''import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({ plugins: [react()], build: { outDir: 'dist', emptyOutDir: true } });
''',
    'frontend/index.html': '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>App</title>
</head>
<body>
<div id="root"></div>
<script type="module" src="/src/main.jsx"></script>
</body>
</html>
''',
    'frontend/src/main.jsx': '''import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';

createRoot(document.getElementById('root')).render(<App />);
''',
    'frontend/src/App.jsx': '''import React, { useEffect, useState } from 'react';

// Application UI. Talk to the backend through /api routes.
export default function App() {
  const [status, setStatus] = useState('loading');
  useEffect(() => {
    fetch('/api/health').then(r => setStatus(r.ok ? 'ready' : 'server error ' + r.status)).catch(() => setStatus('server unreachable'));
  }, []);
  return (
    <main id="app">
      <h1>App</h1>
      <p id="status">{status}</p>
    </main>
  );
}
''',
    '.daedalus-run.json': '{"dependencies": {".": ["frontend"]}, "services": [{"id": "app", "cwd": ".", "command": "python3 -m uvicorn app:app --host 127.0.0.1 --port {port}", "ready_path": "/api/health"}]}\n',
    'tests/test_api.py': '''"""API regression checks against the running backend (DAEDALUS_APP_URL, or a local uvicorn)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def request(base, path, method='GET', data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


def main():
    base = os.environ.get('DAEDALUS_APP_URL')
    process = None
    if not base:
        port = 8765
        env = {**os.environ, 'APP_DB_PATH': os.path.join(tempfile.mkdtemp(), 'test.sqlite')}
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env)
        base = f'http://127.0.0.1:{port}'
        for _ in range(50):
            try:
                urllib.request.urlopen(base + '/api/health', timeout=1); break
            except OSError:
                time.sleep(0.2)
    try:
        status, payload = request(base, '/api/health')
        assert status == 200 and payload == {'ok': True}, (status, payload)
    finally:
        if process:
            process.terminate(); process.wait(timeout=10)


if __name__ == '__main__':
    main()
''',
    'tests/test_browser.py': '''# PLACEHOLDER: replace this whole file with the requested browser regression checks.
"""Browser regression checks against the served frontend. Extend with the requested user journeys."""
import os

from playwright.sync_api import sync_playwright


def main():
    url = os.environ['DAEDALUS_APP_URL']
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/', wait_until='load')
        page.wait_for_selector('#app')
        assert not errors, 'page errors: ' + ' | '.join(errors)
        browser.close()


if __name__ == '__main__':
    main()
''',
    '.gitignore': '.venv/\n__pycache__/\nfrontend/node_modules/\nfrontend/dist/\n*.sqlite\n',
    'README.md': '''# App

React (Vite) frontend, FastAPI backend, SQLite storage.

## Install

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cd frontend && npm install && cd ..

## Build the frontend

    cd frontend && npm run build && cd ..

## Run

    python3 -m uvicorn app:app --host 127.0.0.1 --port 8000

`APP_DB_PATH` selects the SQLite file (default `data.sqlite`). The built frontend is served at `/`, the API under `/api`.

## Test

    python3 tests/test_api.py
    python3 tests/test_browser.py
''',
}

_DOTNET = {
    '{App}.sln': '''Microsoft Visual Studio Solution File, Format Version 12.00
# Visual Studio Version 17
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "{App}", "{App}\\{App}.csproj", "{1B0F3B1C-6C6C-4D6A-9E4A-0000000000A1}"
EndProject
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "{Tests}", "{Tests}\\{Tests}.csproj", "{1B0F3B1C-6C6C-4D6A-9E4A-0000000000B2}"
EndProject
Global
\tGlobalSection(SolutionConfigurationPlatforms) = preSolution
\t\tDebug|Any CPU = Debug|Any CPU
\t\tRelease|Any CPU = Release|Any CPU
\tEndGlobalSection
\tGlobalSection(ProjectConfigurationPlatforms) = postSolution
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000A1}.Debug|Any CPU.ActiveCfg = Debug|Any CPU
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000A1}.Debug|Any CPU.Build.0 = Debug|Any CPU
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000A1}.Release|Any CPU.ActiveCfg = Release|Any CPU
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000A1}.Release|Any CPU.Build.0 = Release|Any CPU
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000B2}.Debug|Any CPU.ActiveCfg = Debug|Any CPU
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000B2}.Debug|Any CPU.Build.0 = Debug|Any CPU
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000B2}.Release|Any CPU.ActiveCfg = Release|Any CPU
\t\t{1B0F3B1C-6C6C-4D6A-9E4A-0000000000B2}.Release|Any CPU.Build.0 = Release|Any CPU
\tEndGlobalSection
EndGlobal
''',
    '{App}/{App}.csproj': '''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <RootNamespace>{App}</RootNamespace>
  </PropertyGroup>
</Project>
''',
    '{App}/Program.cs': '''using {App};

return App.Run(args);
''',
    '{App}/App.cs': '''namespace {App};

/// <summary>Application entry logic. Program.cs calls Run(args) and exits with its result.</summary>
public static class App
{
    public static int Run(string[] args)
    {
        // Implement the requested behavior here: return 0 on success, 2 on usage or input errors.
        Console.Error.WriteLine("not implemented");
        return 2;
    }
}
''',
    '{Tests}/{Tests}.csproj': '''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <IsPackable>false</IsPackable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" />
    <PackageReference Include="xunit" Version="2.9.2" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="..\\{App}\\{App}.csproj" />
  </ItemGroup>
</Project>
''',
    '{Tests}/AppTests.cs': '''using Xunit;
using {App};

public class AppTests
{
    [Fact]
    public void RunRejectsMissingArguments()
    {
        Assert.Equal(2, App.Run(Array.Empty<string>()));
    }
}
''',
    '.gitignore': 'bin/\nobj/\n',
    'README.md': '''# {App}

.NET 8 solution: console application `{App}` and xUnit tests `{Tests}`.

## Build and test

    dotnet build {App}.sln
    dotnet test {App}.sln

## Run

    dotnet run --project {App} -- <arguments>
''',
}

SKELETONS = {
    'static-web': {'version': 1, 'files': _STATIC, 'replace': ['app.js', 'tests/test_browser.py']},
    'express-sqlite': {'version': 2, 'files': _EXPRESS, 'replace': ['public/app.js', 'tests/api.test.js']},
    'react-fastapi-sqlite': {'version': 2, 'files': _REACT_FASTAPI, 'replace': ['frontend/src/App.jsx', 'tests/test_api.py', 'tests/test_browser.py']},
    'dotnet-xunit': {'version': 1, 'files': _DOTNET, 'replace': ['{App}/App.cs', '{Tests}/AppTests.cs']},
}
SERVER_WORDS = re.compile(r'\bExpress\b|\bFastAPI\b|\bFlask\b|\bDjango\b|\bNode(?:\.js)?\b|\bPython\b|\bReact\b|\bVue\b|\bSvelte\b|'
                          r'\bbackend\b|\bserver\b|\bREST\b|\bAPI\b|\bSQLite\b|\bdatabase\b', re.I)
STATIC_WORDS = re.compile(r'\bindex\.html\b|\bdependency-free\b|\bstatic\b|\bvanilla\b|\bbrowser\b|\bweb page\b|\bwebpage\b|'
                          r'\bHTML\b|\bJavaScript\b|localStorage', re.I)


def manifest_hash(skeleton_id):
    spec = SKELETONS[skeleton_id]
    digest = hashlib.sha256(json.dumps({'version': spec['version'], 'files': spec['files'], 'replace': spec['replace']}, sort_keys=True).encode())
    return digest.hexdigest()


def _pascal(text):
    words = re.findall(r'[A-Za-z0-9]+', text or '')
    return ''.join(w[:1].upper() + w[1:] for w in words) or 'App'


def names_for(skeleton_id, task, project_name=''):
    """Template names taken from the request (`solution named X`, backticked X / X.Tests), else the project name."""
    if skeleton_id != 'dotnet-xunit':
        return {}
    text = task or ''
    app = None
    for pattern in (r'solution named\s+`?([A-Za-z_][A-Za-z0-9_]*)`?', r'(?:console )?app(?:lication)? project\s+`([A-Za-z_][A-Za-z0-9_.]*)`',
                    r'project (?:named|called)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?'):
        match = re.search(pattern, text, re.I)
        if match and not match.group(1).endswith('.Tests'):
            app = match.group(1); break
    app = app or _pascal(project_name)
    tests = re.search(r'`([A-Za-z_][A-Za-z0-9_]*\.Tests)`', text)
    return {'App': app, 'Tests': tests.group(1) if tests else app + '.Tests'}


def _render(text, names):
    for key, value in names.items():
        text = text.replace('{' + key + '}', value)
    return text


def files_for(skeleton_id, names):
    spec = SKELETONS[skeleton_id]
    return {_render(path, names): _render(content, names) for path, content in spec['files'].items()}


def select_skeleton(task, brief=None, job=None):
    """A skeleton id for a fresh build_from_prompt job whose request names a supported stack, else None."""
    job = job or {}
    if os.environ.get('DAEDALUS_SKELETONS', '').lower() in {'0', 'off', 'false', 'no'}:
        return None  # attribution pilots run the same tree with and without skeletons
    if job.get('mode', 'build_from_prompt') != 'build_from_prompt' or job.get('inherited'):
        return None
    if (job.get('inventory') or {}).get('files'):
        return None
    text = task or ''
    manifests = {Path(f).name for b in (brief or {}).get('batches', []) for f in b.get('files', []) if isinstance(f, str)}
    if re.search(r'\.NET\b|\bC#|\bcsharp\b|\bdotnet\b|\bxunit\b|\.sln\b|\.csproj\b', text, re.I) or any(m.endswith(('.sln', '.csproj')) for m in manifests):
        return 'dotnet-xunit'
    if re.search(r'\bReact\b|\bVite\b', text, re.I) and re.search(r'\bFastAPI\b|\buvicorn\b', text, re.I):
        return 'react-fastapi-sqlite'
    if re.search(r'\bExpress\b', text, re.I) and re.search(r'\bSQLite\b|\bsqlite3\b|better-sqlite3', text, re.I):
        return 'express-sqlite'
    if STATIC_WORDS.search(text) and not SERVER_WORDS.search(text) and (re.search(r'#[a-z][\w-]*', text) or 'index.html' in manifests):
        return 'static-web'
    return None


def describe(skeleton_id, task, project_name=''):
    """The job_json record for a chosen skeleton: identity, rendered file list, replace-marked files."""
    names = names_for(skeleton_id, task, project_name)
    return {'id': skeleton_id, 'version': SKELETONS[skeleton_id]['version'], 'hash': manifest_hash(skeleton_id), 'names': names,
            'files': sorted(files_for(skeleton_id, names)), 'replace': [_render(p, names) for p in SKELETONS[skeleton_id]['replace']],
            'applied': False}


def apply(root, spec):
    """Write the skeleton into an EMPTY workspace; returns the written paths (never overwrites, never on hash drift)."""
    root = Path(root)
    if spec['id'] not in SKELETONS or spec.get('hash') != manifest_hash(spec['id']):
        return []
    written = []
    for path, content in files_for(spec['id'], spec.get('names') or {}).items():
        target = root / path
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(path)
    return written


def unchanged_placeholders(root, spec):
    """Replace-marked files still byte-identical to the template."""
    root = Path(root)
    rendered = files_for(spec['id'], spec.get('names') or {})
    return [p for p in spec.get('replace', []) if (root / p).is_file() and (root / p).read_text(errors='replace') == rendered.get(p)]


def placeholder_row(root, spec, revision_id, execution_id):
    """skeleton:placeholder: fails while the skeleton's placeholder files were never replaced."""
    if not spec or spec['id'] not in SKELETONS:
        return None
    unchanged = unchanged_placeholders(root, spec)
    return {'id': 'skeleton:placeholder', 'origin': 'controller', 'passed': not unchanged, 'cwd': '.', 'paths': unchanged,
            'evidence_types': [], 'outcomes': [], 'classification': 'passed' if not unchanged else 'application_defect',
            'revision_id': revision_id, 'execution_id': execution_id + ':skeleton:placeholder', 'execution_succeeded': not unchanged,
            'command': 'compare skeleton placeholder files', 'log_tail': '' if not unchanged else ', '.join(unchanged),
            'reason': '' if not unchanged else ('The starting skeleton\'s placeholder files were never replaced: ' + ', '.join(unchanged) +
                '. They implement none of the requested behavior. Replace them with the real application code and real tests.')}


def guidance(spec):
    """The SKELETON block for the builder: this job's files as facts, never as examples."""
    if not spec or spec['id'] not in SKELETONS:
        return ''
    return ('\nSKELETON: the project already contains these files, verified to install, build, launch and run: ' + ', '.join(spec['files']) +
            '. Extend them and keep this layout and these file names; do not re-scaffold, rename or delete them. The skeleton implements '
            'NONE of the requested behavior: every requested outcome is still unwritten, and these placeholder files must be replaced '
            'with real code and real tests: ' + ', '.join(spec['replace']) + '.\n')
