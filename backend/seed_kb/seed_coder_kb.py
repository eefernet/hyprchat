#!/usr/bin/env python3
"""
Seed the Coder Bot knowledge base by fetching real docs from the internet.
Run: python3 seed_coder_kb.py
Re-run anytime to update with latest docs.
"""
import asyncio
import json
import os
import sys
import uuid
import hashlib

# Add parent dir to path so we can import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import config
import database as db
import rag

# ── ANSI ──
G = "\033[32m"
Y = "\033[33m"
C = "\033[36m"
R = "\033[31m"
B = "\033[1m"
D = "\033[2m"
RST = "\033[0m"

KB_NAME = "Coder Reference Docs"
KB_DESC = "Full-stack developer reference — Python, Rust, C/C++, C#, Java, JS/TS, Ruby, Go, Lua, Swift, Kotlin, Elixir, Haskell, React, Vue, Angular, Next.js, Unity3D, Unreal Engine, Docker, Kubernetes, Git, SQL, Redis, Terraform, Linux, macOS, Windows"

# ── Sources to fetch ──
# Each: (filename, url, description)
# Using raw GitHub docs, official cheatsheets, and plain-text references
SOURCES = [
    # Python
    ("python_stdlib.md",
     "https://raw.githubusercontent.com/gto76/python-cheatsheet/main/README.md",
     "Comprehensive Python cheatsheet — stdlib, data structures, OOP, async, testing"),

    # Rust
    ("rust_reference.md",
     "https://raw.githubusercontent.com/donbright/rust-lang-cheat-sheet/master/README.md",
     "Rust cheatsheet — ownership, borrowing, lifetimes, traits, generics, macros, concurrency"),
    ("rust_by_example.md",
     "https://raw.githubusercontent.com/mre/idiomatic-rust/master/README.md",
     "Idiomatic Rust — patterns, idioms, and clean code examples"),

    # C/C++
    ("c_reference.md",
     "https://raw.githubusercontent.com/mortennobel/cpp-cheatsheet/master/README.md",
     "C/C++ cheatsheet — pointers, memory, structs, templates"),

    # Java
    ("java_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/languages/java.md",
     "Java cheatsheet — OOP, collections, streams, concurrency"),

    # JavaScript/TypeScript
    ("javascript_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/languages/javascript.js",
     "JavaScript cheatsheet — ES6+, async/await, DOM, patterns"),
    ("typescript_reference.md",
     "https://raw.githubusercontent.com/rmolinamir/typescript-cheatsheet/master/README.md",
     "TypeScript cheatsheet — types, interfaces, generics, decorators, React+TS integration"),

    # HTML/CSS
    ("html_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/frontend/html5.html",
     "HTML5 reference — elements, attributes, semantic markup"),
    ("css_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/frontend/css3.css",
     "CSS3 reference — flexbox, grid, animations, selectors"),
    ("react_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/frontend/react.js",
     "React cheatsheet — hooks, components, state, lifecycle"),

    # Shell / Linux
    ("bash_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/languages/bash.sh",
     "Bash scripting cheatsheet — variables, loops, conditionals, builtins"),
    ("linux_commands.md",
     "https://raw.githubusercontent.com/jlevy/the-art-of-command-line/master/README.md",
     "The Art of Command Line — essential Linux/macOS/Windows terminal commands"),

    # Git
    ("git_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/tools/git.sh",
     "Git cheatsheet — staging, commits, branching, merging, rebasing, stashing, tags"),

    # Docker
    ("docker_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/tools/docker.sh",
     "Docker cheatsheet — build, run, compose, volumes, networking"),

    # SQL / Databases
    ("sql_reference.md",
     "https://raw.githubusercontent.com/enochtangg/quick-SQL-cheatsheet/master/README.md",
     "SQL cheatsheet — SELECT, JOIN, GROUP BY, subqueries, indexes, transactions"),

    # Node.js / Backend JS
    ("nodejs_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/backend/node.js",
     "Node.js cheatsheet — fs, http, path, streams, child_process"),
    ("express_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/backend/express.js",
     "Express.js cheatsheet — routing, middleware, error handling"),

    # Django
    ("django_reference.py",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/backend/django.py",
     "Django cheatsheet — models, views, URLs, ORM, admin"),

    # Go
    ("go_reference.md",
     "https://raw.githubusercontent.com/a8m/golang-cheat-sheet/master/README.md",
     "Go cheatsheet — goroutines, channels, interfaces, error handling"),

    # PHP
    ("php_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/languages/php.php",
     "PHP cheatsheet — arrays, strings, OOP, PDO"),

    # Vim
    ("vim_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/tools/vim.txt",
     "Vim cheatsheet — modes, navigation, editing, macros"),

    # ─── LANGUAGES ───

    # C# / .NET
    ("csharp_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/languages/C%23.txt",
     "C# cheatsheet — LINQ, async/await, generics, delegates, properties"),
    ("csharp_design_patterns.md",
     "https://raw.githubusercontent.com/nemanjarogic/DesignPatternsLibrary/master/README.md",
     "C# design patterns — creational, structural, behavioral with examples"),

    # Ruby
    ("ruby_reference.md",
     "https://raw.githubusercontent.com/ThibaultJanBeyer/cheatsheets/master/Ruby-Cheatsheet.md",
     "Ruby cheatsheet — blocks, procs, lambdas, classes, modules, gems"),

    # C++ (modern)
    ("cpp_modern_reference.md",
     "https://raw.githubusercontent.com/AnthonyCalandra/modern-cpp-features/master/README.md",
     "Modern C++ features — C++11/14/17/20/23, smart pointers, move semantics, concepts, ranges"),

    # Swift
    ("swift_reference.md",
     "https://raw.githubusercontent.com/reinder42/SwiftCheatsheet/master/README.md",
     "Swift cheatsheet — optionals, protocols, closures, generics, SwiftUI basics"),
    ("swiftui_reference.md",
     "https://raw.githubusercontent.com/SimpleBoilerplates/SwiftUI-Cheat-Sheet/master/README.md",
     "SwiftUI cheatsheet — views, stacks, lists, navigation, gestures, UIKit bridging"),
    ("swift_design_patterns.md",
     "https://raw.githubusercontent.com/ochococo/Design-Patterns-In-Swift/master/README.md",
     "Swift design patterns — creational, structural, behavioral patterns with examples"),

    # Kotlin
    ("kotlin_reference.md",
     "https://raw.githubusercontent.com/alidehkhodaei/kotlin-cheat-sheet/master/README.md",
     "Kotlin cheatsheet — coroutines, data classes, extensions, null safety, collections, generics"),

    # Lua
    ("lua_reference.md",
     "https://gist.githubusercontent.com/JettIsOnTheNet/b7472ee8b1f5b324c498302b0f61957d/raw",
     "Lua cheatsheet — tables, metatables, closures, coroutines, OOP, string operations"),

    # Elixir
    ("elixir_reference.md",
     "https://raw.githubusercontent.com/vnegrisolo/cheat-sheet-elixir/master/README.md",
     "Elixir cheatsheet — pattern matching, functions, modules, protocols, processes, Enum/Stream"),

    # Haskell
    ("haskell_reference.md",
     "https://raw.githubusercontent.com/i-am-tom/learn-me-a-haskell/master/README.md",
     "Haskell reference — types, pattern matching, higher-order functions, functional fundamentals"),

    # Perl
    ("perl_reference.md",
     "https://raw.githubusercontent.com/lyudaio/cheatsheets/main/programming_languages/perl.md",
     "Perl cheatsheet — variables, data types, operators, regex, subroutines, file handling"),

    # Scala
    ("scala_reference.md",
     "https://raw.githubusercontent.com/lampepfl/dotty/main/docs/_docs/reference/overview.md",
     "Scala reference — case classes, pattern matching, traits, implicits, futures"),

    # Dart
    ("dart_reference.md",
     "https://raw.githubusercontent.com/Temidtech/dart-cheat-sheet/master/README.md",
     "Dart cheatsheet — string interpolation, functions, lists, maps, null-aware operators, async/await"),

    # Regex
    ("regex_reference.md",
     "https://raw.githubusercontent.com/lyudaio/cheatsheets/main/programming_languages/regex.md",
     "Regex cheatsheet — character classes, quantifiers, anchors, groups, lookahead/lookbehind, flags"),

    # ─── FRONTEND FRAMEWORKS ───

    # React (extended)
    ("react_hooks_reference.md",
     "https://raw.githubusercontent.com/ohansemmanuel/react-hooks-cheatsheet/master/README.md",
     "React Hooks cheatsheet — useState, useEffect, useContext, useReducer, useMemo, useCallback, custom hooks"),
    ("react_patterns.md",
     "https://raw.githubusercontent.com/krasimir/react-in-patterns/master/README.md",
     "React patterns — composition, HOC, render props, controlled components, state management"),

    # Vue.js
    ("vue_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/frontend/vue.js",
     "Vue.js cheatsheet — components, directives, reactivity, Vuex, Vue Router"),

    # Angular
    ("angular_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/frontend/angular.js",
     "Angular cheatsheet — modules, directives, forms, decorators, lifecycle hooks, DI, routing"),

    # Next.js
    ("nextjs_reference.md",
     "https://raw.githubusercontent.com/CyberT33N/next.js-cheat-sheet/main/README.md",
     "Next.js 14 reference — routing, layouts, data fetching, API routes, middleware, rendering strategies"),

    # Svelte
    ("svelte_reference.md",
     "https://raw.githubusercontent.com/mark7p/svelte-5-cheatsheet/main/README.md",
     "Svelte 5 cheatsheet — reactivity with runes, props, events, bindings, stores, transitions"),

    # Tailwind CSS
    ("tailwind_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/frontend/tailwind.css",
     "Tailwind CSS cheatsheet — utility classes, responsive, dark mode, customization"),

    # Bootstrap
    ("bootstrap_reference.md",
     "https://raw.githubusercontent.com/matthewlean/Bootstrap-HTML-CSS-Emmet-Cheetsheet/master/cheatSheet.markdown",
     "Bootstrap/HTML/CSS reference — grid system, typography, media queries, responsive utilities"),

    # jQuery
    ("jquery_reference.md",
     "https://raw.githubusercontent.com/AllThingsSmitty/jquery-tips-everyone-should-know/master/README.md",
     "jQuery tips — selectors, DOM manipulation, events, AJAX, animations, performance"),

    # ─── BACKEND FRAMEWORKS ───

    # Flask
    ("flask_reference.md",
     "https://raw.githubusercontent.com/lucrae/flask-cheat-sheet/master/README.md",
     "Flask cheatsheet — app setup, blueprints, Jinja2, SQLAlchemy, migrations, login manager"),

    # FastAPI
    ("fastapi_reference.md",
     "https://raw.githubusercontent.com/mjhea0/awesome-fastapi/master/README.md",
     "FastAPI ecosystem — middleware, auth, databases, testing, deployment, extensions"),

    # Ruby on Rails
    ("rails_reference.md",
     "https://raw.githubusercontent.com/ThibaultJanBeyer/cheatsheets/master/Ruby-on-Rails-Cheatsheet.md",
     "Ruby on Rails cheatsheet — MVC, routing, migrations, models, controllers, views, ERB"),

    # Spring Boot (Java)
    ("spring_reference.md",
     "https://raw.githubusercontent.com/in28minutes/spring-boot-master-class/master/README.md",
     "Spring Boot reference — REST APIs, JPA, security, testing, microservices"),

    # Laravel (PHP)
    ("laravel_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/backend/laravel.php",
     "Laravel cheatsheet — Eloquent ORM, routing, middleware, Blade, Artisan"),

    # ASP.NET
    ("aspnet_reference.md",
     "https://raw.githubusercontent.com/jwill9999/ASP-DOTNET-CORE-Cheatsheet/master/README.md",
     "ASP.NET Core cheatsheet — CLI commands, Tag Helpers, models, Entity Framework, DI"),

    # Gin (Go)
    ("gin_reference.md",
     "https://raw.githubusercontent.com/gin-gonic/gin/master/README.md",
     "Gin framework official docs — routing, middleware, JSON binding, file upload, grouping, rendering"),

    # Actix/Axum (Rust web)
    ("rust_web_reference.md",
     "https://raw.githubusercontent.com/flosse/rust-web-framework-comparison/master/README.md",
     "Rust web framework comparison — actix-web, axum, rocket, warp, templating, WebSocket"),

    # ─── MOBILE ───

    # Flutter / Dart
    ("flutter_reference.md",
     "https://raw.githubusercontent.com/Temidtech/Flutter-Cheat-Sheet/master/README.md",
     "Flutter cheatsheet — UI components, navigation, tabs, drawers, form validation, installation"),

    # React Native
    ("react_native_reference.md",
     "https://raw.githubusercontent.com/typescript-cheatsheets/react-native/master/README.md",
     "React Native + TypeScript cheatsheet — component typing, hooks, navigation, platform-specific code"),

    # Android (Kotlin/Java)
    ("android_reference.md",
     "https://raw.githubusercontent.com/anitaa1990/Android-Cheat-sheet/master/README.md",
     "Android dev cheatsheet — activities, fragments, data structures, Jetpack Compose"),

    # iOS (Swift/UIKit)
    ("ios_reference.md",
     "https://raw.githubusercontent.com/reinder42/SwiftCheatsheet/master/README.md",
     "Swift/iOS cheatsheet — variables, functions, OOP, protocols, closures, generics, error handling"),

    # ─── GAME ENGINES ───

    # Unity3D
    ("unity_reference.md",
     "https://raw.githubusercontent.com/ozankasikci/unity-cheat-sheet/master/README.md",
     "Unity3D cheatsheet — MonoBehaviour lifecycle, physics, UI, input, coroutines, ScriptableObjects"),

    # Unreal Engine
    ("unreal_reference.md",
     "https://raw.githubusercontent.com/mikeroyal/Unreal-Engine-Guide/main/README.md",
     "Unreal Engine 5 guide — Blueprint, Niagara VFX, MetaHuman, Lumen, Nanite, C++"),

    # Godot
    ("godot_reference.md",
     "https://raw.githubusercontent.com/mikeroyal/Godot-Engine-Guide/main/README.md",
     "Godot Engine guide — GDScript, 2D/3D game dev, networking, C#/Python/Lua integration"),

    # ─── DATABASES ───

    # SQL (extended)
    ("sql_advanced.md",
     "https://raw.githubusercontent.com/crescentpartha/CheatSheets-for-Developers/main/CheatSheets/sql-cheatsheets.md",
     "SQL advanced reference — DDL, DML, joins, subqueries, views, indexes, stored procedures, transactions"),

    # PostgreSQL
    ("postgres_reference.md",
     "https://gist.githubusercontent.com/yokawasa/3be9abf32cc86b674e3c50b7fc56fcdc/raw",
     "PostgreSQL cheatsheet — psql commands, data types, table operations, queries, indexes, JSON"),

    # MySQL
    ("mysql_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/databases/mysql.sh",
     "MySQL cheatsheet — queries, joins, indexes, stored procedures, transactions"),

    # MongoDB
    ("mongodb_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/databases/mongodb.sh",
     "MongoDB cheatsheet — CRUD, aggregation, indexes, replica sets, queries"),

    # Redis
    ("redis_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/databases/redis.sh",
     "Redis cheatsheet — strings, lists, sets, hashes, pub/sub, persistence"),

    # SQLAlchemy
    ("sqlalchemy_reference.md",
     "https://raw.githubusercontent.com/Teemu/sqlalchemy-cheat-sheet/master/README.md",
     "SQLAlchemy reference — connection URIs, sessions, raw SQL, ORM automap, subqueries"),

    # ─── APIs & PROTOCOLS ───

    # GraphQL
    ("graphql_reference.md",
     "https://raw.githubusercontent.com/sogko/graphql-schema-language-cheat-sheet/master/README.md",
     "GraphQL schema language cheatsheet — types, queries, mutations, subscriptions"),

    # REST API design
    ("rest_api_reference.md",
     "https://raw.githubusercontent.com/RestCheatSheet/api-cheat-sheet/master/README.md",
     "REST API design cheatsheet — HTTP methods, status codes, versioning, pagination, authentication"),

    # WebSocket
    ("websocket_reference.md",
     "https://raw.githubusercontent.com/facundofarias/awesome-websockets/master/README.md",
     "Awesome WebSockets — libraries for all major languages, protocol specs, tutorials, tools"),

    # OAuth / Auth
    ("oauth_reference.md",
     "https://raw.githubusercontent.com/dwyl/learn-json-web-tokens/main/README.md",
     "JWT/OAuth reference — token structure, claims, security, session management, implementation"),

    # ─── DEVOPS & INFRA ───

    # Kubernetes
    ("kubernetes_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/tools/kubernetes.md",
     "Kubernetes cheatsheet — pods, deployments, services, configmaps, kubectl"),

    # Nginx
    ("nginx_reference.md",
     "https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/tools/nginx.sh",
     "Nginx cheatsheet — server blocks, reverse proxy, SSL, load balancing"),

    # Terraform
    ("terraform_reference.md",
     "https://raw.githubusercontent.com/scraly/terraform-cheat-sheet/master/README.md",
     "Terraform cheatsheet — providers, resources, modules, state, plan, apply"),

    # Ansible
    ("ansible_reference.md",
     "https://raw.githubusercontent.com/germainlefebvre4/ansible-cheatsheet/master/README.md",
     "Ansible cheatsheet — configuration, inventories, tasks, playbooks, variables, roles, vault"),

    # GitHub Actions / CI/CD
    ("github_actions_reference.md",
     "https://gist.githubusercontent.com/JonasWanke/c8bc0f90658fbfeef7da35ffe8feb7f4/raw",
     "GitHub Actions cheatsheet — workflow YAML, triggers, environment variables, inputs/outputs"),

    # CMake
    ("cmake_reference.md",
     "https://raw.githubusercontent.com/mortennobel/CMake-Cheatsheet/master/README.md",
     "CMake cheatsheet — targets, libraries, find_package, install, variables"),

    # ─── TOOLS ───

    # PowerShell / Windows
    ("powershell_reference.md",
     "https://raw.githubusercontent.com/ab14jain/PowerShell/master/README.md",
     "PowerShell guide — cmdlets, variables, strings, collections, control flow, functions, .NET integration"),

    # Markdown
    ("markdown_reference.md",
     "https://raw.githubusercontent.com/adam-p/markdown-here/master/README.md",
     "Markdown cheatsheet — headings, links, images, tables, code blocks"),

    # NPM / Yarn
    ("npm_reference.md",
     "https://raw.githubusercontent.com/Sunil-Pradhan/npm-cheatsheet/master/README.md",
     "NPM cheatsheet — package creation, installation, versioning, scripts, dependencies"),

    # ─── DATA / ML ───

    # Pandas
    ("pandas_reference.md",
     "https://raw.githubusercontent.com/crescentpartha/CheatSheets-for-Developers/main/CheatSheets/pandas-cheatsheet.md",
     "Pandas cheatsheet — import/export, DataFrame inspection, data cleaning, filtering, grouping, joins"),

    # NumPy
    ("numpy_reference.md",
     "https://raw.githubusercontent.com/rougier/numpy-100/master/100_Numpy_exercises.md",
     "NumPy 100 exercises — arrays, broadcasting, slicing, linear algebra, random"),

    # PyTorch
    ("pytorch_reference.md",
     "https://raw.githubusercontent.com/bfortuner/pytorch-cheatsheet/master/README.md",
     "PyTorch cheatsheet — tensors, autograd, nn.Module, DataLoader, training loops, GPU"),

    # ─── ARCHITECTURE & PATTERNS ───

    # Design patterns
    ("design_patterns_reference.md",
     "https://raw.githubusercontent.com/mutasim77/design-patterns/main/README.md",
     "Design patterns — all 23 GoF patterns (creational, structural, behavioral) + SOLID with TypeScript examples"),

    # System design
    ("system_design_reference.md",
     "https://raw.githubusercontent.com/donnemartin/system-design-primer/master/README.md",
     "System design primer — scalability, caching, load balancing, databases, microservices, CAP theorem"),

    # Clean code
    ("clean_code_reference.md",
     "https://raw.githubusercontent.com/ryanmcdermott/clean-code-javascript/master/README.md",
     "Clean code principles — SOLID, naming, functions, error handling, testing, formatting"),
]


async def fetch_source(client: httpx.AsyncClient, filename: str, url: str, desc: str):
    """Fetch a single source URL. Returns (filename, content, desc) or None on failure."""
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code == 200 and len(r.text.strip()) > 100:
            return filename, r.text, desc
        else:
            print(f"  {Y}!{RST} {filename}: HTTP {r.status_code} or empty ({len(r.text)} chars)")
            return None
    except Exception as e:
        print(f"  {R}x{RST} {filename}: {e}")
        return None


async def main():
    print()
    print(f"  {B}{C}Coder KB Seeder{RST}")
    print(f"  {D}Fetching docs from the internet...{RST}")
    print()

    # Ensure DB is initialized
    await db.init()

    # Check if KB already exists
    kbs = await db.get_kbs()
    existing = next((kb for kb in kbs if kb["name"] == KB_NAME), None)

    if existing:
        kb_id = existing["id"]
        print(f"  {Y}!{RST} KB '{KB_NAME}' exists (id={kb_id}), updating...")
        # Delete old files
        for f in existing.get("files", []):
            await db.delete_kb_file(f["id"])
        # Clear RAG index
        await rag.delete_kb_index(kb_id)
    else:
        kb_id = f"kb-{uuid.uuid4().hex[:12]}"
        _db = await db.get_db()
        try:
            await _db.execute(
                "INSERT INTO knowledge_bases (id, name, description) VALUES (?, ?, ?)",
                (kb_id, KB_NAME, KB_DESC)
            )
            await _db.commit()
        finally:
            await _db.close()
        print(f"  {G}+{RST} Created KB '{KB_NAME}' (id={kb_id})")

    # Create KB directory
    kb_dir = os.path.join(config.KB_DIR, kb_id)
    os.makedirs(kb_dir, exist_ok=True)

    # Ensure embedding model is available
    print(f"  {D}Checking embedding model...{RST}")
    await rag.ensure_embed_model()

    # Fetch all sources in parallel
    print(f"  {D}Fetching {len(SOURCES)} sources...{RST}")
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [fetch_source(client, fn, url, desc) for fn, url, desc in SOURCES]
        results = await asyncio.gather(*tasks)

    fetched = [r for r in results if r is not None]
    print(f"  {G}{len(fetched)}/{len(SOURCES)}{RST} sources fetched successfully")
    print()

    # Save and index each file
    total_chunks = 0
    for i, (filename, content, desc) in enumerate(fetched):
        filepath = os.path.join(kb_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            # Prepend description as context for RAG
            f.write(f"# {desc}\n\n{content}")

        file_size = len(content.encode("utf-8"))
        await db.add_kb_file(kb_id, filename, filepath, file_size, "text/plain")

        # Index in RAG
        print(f"  [{i+1}/{len(fetched)}] {B}{filename}{RST} ({file_size // 1024}KB) ", end="", flush=True)
        try:
            result = await rag.index_file(kb_id, filename, filepath)
            chunks = result.get("chunks", 0)
            total_chunks += chunks
            print(f"{G}{chunks} chunks{RST}")
        except Exception as e:
            print(f"{R}indexing failed: {e}{RST}")

    print()
    print(f"  {B}{G}Done!{RST} {total_chunks} total chunks indexed into '{KB_NAME}'")
    print(f"  {D}KB ID: {kb_id}{RST}")
    print()

    # Attach to Coder Bot if it exists
    configs = await db.get_model_configs()
    coder = next((c for c in configs if "Coder" in c.get("name", "")), None)
    if coder:
        current_kbs = coder.get("kb_ids", [])
        if kb_id not in current_kbs:
            current_kbs.append(kb_id)
            await db.update_model_config(coder["id"], kb_ids=current_kbs)
            print(f"  {G}+{RST} Attached KB to '{coder['name']}'")
        else:
            print(f"  {D}KB already attached to '{coder['name']}'{RST}")
    else:
        print(f"  {Y}!{RST} No Coder Bot found — create one and attach KB ID: {kb_id}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
