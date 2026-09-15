"""Maintained Coder Reference Docs sources; consumed by the existing KB seeder."""

LEGACY_SOURCES = [
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

# Official replacements fix misleading resource-list references and provide
# substantive language/framework documentation. Explicit editions are retained.
OFFICIAL_SOURCES = [
    ('cmake_reference.md', 'https://raw.githubusercontent.com/Kitware/CMake/v4.1.0/Help/manual/cmake-buildsystem.7.rst', 'CMake build system and targets', 'CMake 4.1 manual; reStructuredText source'),
    ('terraform_reference.md', 'https://developer.hashicorp.com/terraform/language/resources/syntax', 'Terraform resource syntax', 'Terraform current stable docs'),
    ('graphql_reference.md', 'https://graphql.org/learn/schema/', 'GraphQL schemas and types', 'GraphQL official learning guide'),
    ('oauth_reference.md', 'https://www.rfc-editor.org/rfc/rfc9700.html', 'OAuth 2.0 security best current practice', 'RFC 9700; OAuth 2.0'),
    ('jwt_reference.md', 'https://www.rfc-editor.org/rfc/rfc8725.html', 'JSON Web Token best current practices', 'RFC 8725; JWT'),
    ('gin_reference.md', 'https://raw.githubusercontent.com/gin-gonic/gin/master/README.md', 'Gin HTTP framework', 'Gin upstream README; check installed release'),
    ('python_stdlib.md', 'https://docs.python.org/3/tutorial/datastructures.html', 'Python data structures', 'Python 3 stable'),
    ('python_errors.md', 'https://docs.python.org/3/tutorial/errors.html', 'Python exceptions and error handling', 'Python 3 stable'),
    ('python_asyncio.md', 'https://docs.python.org/3/library/asyncio-task.html', 'Python asyncio tasks and cancellation', 'Python 3 stable'),
    ('python_testing.md', 'https://docs.python.org/3/library/unittest.html', 'Python unittest', 'Python 3 stable'),
    ('python_packaging.md', 'https://packaging.python.org/en/latest/tutorials/packaging-projects/', 'Python packaging projects', 'PyPA current guide'),
    ('rust_reference.md', 'https://doc.rust-lang.org/stable/book/ch04-02-references-and-borrowing.html', 'Rust references and borrowing', 'Rust stable'),
    ('rust_by_example.md', 'https://doc.rust-lang.org/stable/rust-by-example/error.html', 'Rust by Example: error handling', 'Rust stable'),
    ('rust_traits.md', 'https://doc.rust-lang.org/stable/book/ch10-02-traits.html', 'Rust traits', 'Rust stable'),
    ('java_reference.md', 'https://dev.java/learn/api/collections-framework/lists/', 'Java List collections', 'Java official learning guide'),
    ('bootstrap_reference.md', 'https://getbootstrap.com/docs/5.3/layout/grid/', 'Bootstrap grid layout', 'Bootstrap 5.3'),
    ('swift_packages.md', 'https://www.swift.org/getting-started/library-swiftpm/', 'Swift Package Manager libraries and tests', 'Swift official getting-started guide'),
    ('java_streams.md', 'https://dev.java/learn/api/streams/map-filter-reduce/', 'Java streams: map, filter and reduce', 'Java official learning guide'),
    ('typescript_reference.md', 'https://www.typescriptlang.org/docs/handbook/2/everyday-types.html', 'TypeScript everyday types', 'TypeScript current handbook'),
    ('typescript_generics.md', 'https://www.typescriptlang.org/docs/handbook/2/generics.html', 'TypeScript generics', 'TypeScript current handbook'),
    ('typescript_narrowing.md', 'https://www.typescriptlang.org/docs/handbook/2/narrowing.html', 'TypeScript narrowing', 'TypeScript current handbook'),
    ('react_reference.md', 'https://react.dev/learn/managing-state', 'React managing state', 'React current stable docs'),
    ('react_hooks_reference.md', 'https://react.dev/reference/react/useEffect', 'React useEffect and cleanup', 'React current stable docs'),
    ('react_patterns.md', 'https://react.dev/learn/you-might-not-need-an-effect', 'React effects and derived state', 'React current stable docs'),
    ('vue_reference.md', 'https://vuejs.org/guide/essentials/reactivity-fundamentals.html', 'Vue reactivity fundamentals', 'Vue 3'),
    ('angular_reference.md', 'https://angular.dev/guide/components', 'Angular components', 'Angular current stable docs'),
    ('nextjs_reference.md', 'https://nextjs.org/docs/app/getting-started/fetching-data', 'Next.js App Router data fetching', 'Next.js current stable App Router'),
    ('svelte_reference.md', 'https://svelte.dev/docs/svelte/what-are-runes', 'Svelte runes', 'Svelte 5'),
    ('tailwind_reference.md', 'https://tailwindcss.com/docs/styling-with-utility-classes', 'Tailwind CSS utility classes', 'Tailwind current stable docs'),
    ('nodejs_reference.md', 'https://nodejs.org/docs/latest-v22.x/api/fs.html', 'Node.js filesystem API', 'Node.js 22 LTS'),
    ('express_reference.md', 'https://expressjs.com/en/guide/error-handling.html', 'Express error handling', 'Express guide; distinguish v4 and v5'),
    ('django_reference.md', 'https://docs.djangoproject.com/en/5.2/topics/db/queries/', 'Django ORM queries', 'Django 5.2 LTS'),
    ('go_reference.md', 'https://go.dev/doc/effective_go', 'Effective Go', 'Go official language guide'),
    ('csharp_reference.md', 'https://learn.microsoft.com/en-us/dotnet/csharp/fundamentals/types/', 'C# type system', 'C# current stable docs; heed version notes'),
    ('csharp_async.md', 'https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/', 'C# asynchronous programming', '.NET current stable docs'),
    ('ruby_reference.md', 'https://docs.ruby-lang.org/en/master/syntax/methods_rdoc.html', 'Ruby methods and arguments', 'Ruby upstream docs; check target Ruby version'),
    ('kotlin_reference.md', 'https://kotlinlang.org/docs/null-safety.html', 'Kotlin null safety', 'Kotlin current stable docs'),
    ('kotlin_coroutines.md', 'https://kotlinlang.org/docs/coroutines-basics.html', 'Kotlin coroutines', 'Kotlin current stable docs'),
    ('lua_reference.md', 'https://www.lua.org/manual/5.4/manual.html', 'Lua reference manual', 'Lua 5.4'),
    ('elixir_reference.md', 'https://hexdocs.pm/elixir/pattern-matching.html', 'Elixir pattern matching', 'Elixir current stable docs'),
    ('haskell_reference.md', 'https://www.haskell.org/onlinereport/haskell2010/haskellch3.html', 'Haskell expressions', 'Haskell 2010 language report'),
    ('perl_reference.md', 'https://perldoc.perl.org/perlintro', 'Perl introduction', 'Perl current stable docs'),
    ('scala_reference.md', 'https://docs.scala-lang.org/scala3/book/types-introduction.html', 'Scala type system', 'Scala 3'),
    ('dart_reference.md', 'https://dart.dev/language/functions', 'Dart functions', 'Dart current stable docs'),
    ('flask_reference.md', 'https://flask.palletsprojects.com/en/stable/quickstart/', 'Flask quickstart', 'Flask stable'),
    ('fastapi_reference.md', 'https://fastapi.tiangolo.com/tutorial/body/', 'FastAPI request bodies', 'FastAPI current docs; Pydantic v2'),
    ('fastapi_dependencies.md', 'https://fastapi.tiangolo.com/tutorial/dependencies/', 'FastAPI dependency injection', 'FastAPI current docs'),
    ('fastapi_testing.md', 'https://fastapi.tiangolo.com/tutorial/testing/', 'FastAPI testing', 'FastAPI current docs'),
    ('rails_reference.md', 'https://guides.rubyonrails.org/active_record_basics.html', 'Rails Active Record basics', 'Rails current stable guide'),
    ('spring_reference.md', 'https://spring.io/guides/gs/rest-service', 'Spring REST service', 'Spring official getting-started guide'),
    ('aspnet_reference.md', 'https://learn.microsoft.com/en-us/aspnet/core/fundamentals/minimal-apis/overview?view=aspnetcore-10.0', 'ASP.NET Core minimal APIs', 'ASP.NET Core 10'),
    ('rust_web_reference.md', 'https://docs.rs/axum/latest/axum/', 'Axum routing and extractors', 'Axum latest released crate'),
    ('flutter_reference.md', 'https://docs.flutter.dev/data-and-backend/state-mgmt/simple', 'Flutter simple state management', 'Flutter stable'),
    ('react_native_reference.md', 'https://reactnative.dev/docs/typescript', 'React Native with TypeScript', 'React Native current stable docs'),
    ('android_reference.md', 'https://developer.android.com/develop/ui/compose/state', 'Jetpack Compose state', 'Android Compose; heed API availability'),
    ('unity_reference.md', 'https://docs.unity3d.com/6000.0/Documentation/Manual/execution-order.html', 'Unity script execution order', 'Unity 6.0'),
    ('unreal_reference.md', 'https://dev.epicgames.com/documentation/en-us/unreal-engine/programming-with-cplusplus-in-unreal-engine', 'Unreal C++ programming', 'Unreal Engine current docs'),
    ('godot_reference.md', 'https://docs.godotengine.org/en/stable/tutorials/scripting/gdscript/gdscript_basics.html', 'GDScript basics', 'Godot stable'),
    ('postgres_reference.md', 'https://www.postgresql.org/docs/current/tutorial-join.html', 'PostgreSQL joins', 'PostgreSQL current stable'),
    ('postgres_transactions.md', 'https://www.postgresql.org/docs/current/tutorial-transactions.html', 'PostgreSQL transactions', 'PostgreSQL current stable'),
    ('sqlite_reference.md', 'https://www.sqlite.org/lang_select.html', 'SQLite SELECT and joins', 'SQLite current stable'),
    ('sqlalchemy_reference.md', 'https://docs.sqlalchemy.org/en/20/orm/session_basics.html', 'SQLAlchemy session basics', 'SQLAlchemy 2.0'),
    ('docker_reference.md', 'https://docs.docker.com/get-started/docker-concepts/running-containers/persisting-container-data/', 'Docker persistent container data', 'Docker current stable docs'),
    ('kubernetes_reference.md', 'https://kubernetes.io/docs/concepts/workloads/controllers/deployment/', 'Kubernetes Deployments', 'Kubernetes current stable docs'),
    ('git_reference.md', 'https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging', 'Git branching and merging', 'Pro Git second edition'),
    ('github_actions_reference.md', 'https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions', 'GitHub Actions workflow syntax', 'GitHub Actions current docs'),
    ('npm_reference.md', 'https://docs.npmjs.com/cli/v11/configuring-npm/package-json', 'npm package.json', 'npm CLI 11'),
    ('pandas_reference.md', 'https://pandas.pydata.org/docs/user_guide/10min.html', 'pandas introduction', 'pandas current stable docs'),
    ('numpy_reference.md', 'https://numpy.org/doc/stable/user/absolute_beginners.html', 'NumPy array fundamentals', 'NumPy stable'),
    ('pytorch_reference.md', 'https://docs.pytorch.org/tutorials/beginner/basics/autogradqs_tutorial.html', 'PyTorch automatic differentiation', 'PyTorch current stable docs'),
]

# MDN is maintained platform reference material, not the ECMAScript/C standards.
REFERENCE_SOURCES = [
    ('websocket_reference.md', 'https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API/Writing_WebSocket_client_applications', 'WebSocket clients and connection lifecycle', 'MDN WebSockets guide'),
    ('javascript_reference.md', 'https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Using_promises', 'JavaScript promises and async error handling', 'MDN JavaScript guide'),
    ('javascript_collections.md', 'https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Indexed_collections', 'JavaScript indexed collections', 'MDN JavaScript guide'),
    ('html_reference.md', 'https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/Structuring_content/Basic_HTML_syntax', 'HTML semantic structure', 'MDN HTML guide'),
    ('css_reference.md', 'https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_grid_layout/Basic_concepts_of_grid_layout', 'CSS Grid layout', 'MDN CSS guide'),
    ('c_reference.md', 'https://en.cppreference.com/w/c/language/pointer.html', 'C pointers and pointer arithmetic', 'cppreference; check C standard version'),
    ('cpp_modern_reference.md', 'https://en.cppreference.com/w/cpp/memory/unique_ptr.html', 'C++ unique_ptr and ownership', 'cppreference; C++11 and later'),
]

SWIFT_RELEASE = 'swift-6.3-fcs'
SWIFT_CHAPTERS = {
    'swift_reference.md': 'TheBasics', 'swift_strings.md': 'StringsAndCharacters',
    'swift_collections.md': 'CollectionTypes', 'swift_control_flow.md': 'ControlFlow',
    'swift_functions.md': 'Functions', 'swift_optionals.md': 'OptionalChaining',
    'swift_protocols.md': 'Protocols', 'swift_generics.md': 'Generics',
    'swift_concurrency.md': 'Concurrency', 'swift_errors.md': 'ErrorHandling',
}
for filename, chapter in SWIFT_CHAPTERS.items():
    OFFICIAL_SOURCES.append((filename,
        f'https://raw.githubusercontent.com/swiftlang/swift-book/{SWIFT_RELEASE}/TSPL.docc/LanguageGuide/{chapter}.md',
        f'Swift {chapter}', 'Swift 6.3 released language guide'))

APPLE_PAGES = {
    'swiftui_reference.md': 'swiftui/managing-model-data-in-your-app',
    'swiftui_state.md': 'swiftui/state', 'swiftui_binding.md': 'swiftui/binding',
    'swiftui_bindable.md': 'swiftui/bindable',
    'swiftui_observation.md': 'swiftui/migrating-from-the-observable-object-protocol-to-the-observable-macro',
    'swiftui_navigation.md': 'swiftui/navigationstack',
    'swiftui_navigation_migration.md': 'swiftui/migrating-to-new-navigation-types',
    'swiftui_layout.md': 'swiftui/building-layouts-with-stack-views',
    'swiftui_lists.md': 'swiftui/list', 'swiftui_app.md': 'swiftui/app',
    'swiftui_uikit.md': 'swiftui/uiviewrepresentable',
    'swiftui_accessibility.md': 'swiftui/accessibility-fundamentals',
    'swiftui_async.md': 'swiftui/view/task(name:priority:file:line:_:)',
    'ios_reference.md': 'uikit/managing-your-app-s-life-cycle',
    'swift_testing.md': 'testing/definingtests',
}
for filename, page in APPLE_PAGES.items():
    OFFICIAL_SOURCES.append((filename,
        f'https://developer.apple.com/tutorials/data/documentation/{page}.md',
        page.rsplit('/', 1)[-1].replace('-', ' '), 'Apple documentation; preserve platform availability'))


def source_catalog():
    """Stable filenames preserve existing KB file IDs and saved citations."""
    sources = {name.replace('django_reference.py', 'django_reference.md'): {
        'filename': name.replace('django_reference.py', 'django_reference.md'),
        'url': url, 'title': description, 'authority': 'community',
        'version': 'Community reference; verify target language/framework version',
    } for name, url, description in LEGACY_SOURCES}
    for rows, authority in [(OFFICIAL_SOURCES, 'official'), (REFERENCE_SOURCES, 'maintained reference')]:
        for name, url, title, version in rows:
            sources[name] = dict(filename=name, url=url, title=title, authority=authority, version=version)
    return list(sources.values())
