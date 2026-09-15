# Coder Reference Docs audit — September 15, 2026

The existing shared KB contained 84 documents and 1,588 indexed chunks. All 84
lacked source-URL metadata; the audit also flagged nine resource/course lists,
six legacy-version references, and one duplicate Swift/iOS document. The refreshed
catalog contains 124 documents: 95 official, seven maintained references and 22
community references, indexed into 2,185 chunks. The KB ID and all 84 existing
file IDs are preserved.

## Corrections and coverage

- Replaced Swift 5 and SwiftUI 2.0 cheatsheets with released Swift language-guide chapters and official Apple SwiftUI documentation covering state, bindings, Bindable, Observation, navigation, async tasks, layouts, lists and accessibility. UIKit and Swift Testing have separate references.
- Replaced C material that described C++ with a C reference, and separated OAuth guidance from JWT guidance.
- Replaced resource lists and thin landing pages with explanatory language/framework documentation where available; retained useful community material with explicit attribution.
- Expanded Python, JavaScript/TypeScript, React, Rust, Java, Go, C/C++, .NET, mobile, backend, database and tooling coverage. Quick Search supplies current references beyond this curated library.
- Preserved code indentation during HTML extraction and Markdown chunking. Added source, authority, version, fetch date and hash metadata.

## Existing-document review

Each managed file was inventoried and its replacement URL fetched and extracted.
Legacy flags identify material requiring version review; they do not mean every
use of the older API is incorrect. Official documentation is preferred but this
audit does not compile every upstream example or certify every claim in retained
community documents. The persona checks the user's target runtime and version.

| Existing file | Audit findings | Refresh source |
|---|---|---|
| `python_stdlib.md` | Missing provenance | [official](https://docs.python.org/3/tutorial/datastructures.html) |
| `rust_reference.md` | Missing provenance | [official](https://doc.rust-lang.org/stable/book/ch04-02-references-and-borrowing.html) |
| `rust_by_example.md` | resource list/course material; verify substantive reference coverage | [official](https://doc.rust-lang.org/stable/rust-by-example/error.html) |
| `c_reference.md` | Missing provenance | [maintained reference](https://en.cppreference.com/w/c/language/pointer.html) |
| `java_reference.md` | Missing provenance | [official](https://dev.java/learn/api/collections-framework/lists/) |
| `javascript_reference.md` | Missing provenance | [maintained reference](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Using_promises) |
| `typescript_reference.md` | Missing provenance | [official](https://www.typescriptlang.org/docs/handbook/2/everyday-types.html) |
| `html_reference.md` | Missing provenance | [maintained reference](https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/Structuring_content/Basic_HTML_syntax) |
| `css_reference.md` | Missing provenance | [maintained reference](https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_grid_layout/Basic_concepts_of_grid_layout) |
| `react_reference.md` | legacy API/version material; review against target version | [official](https://react.dev/learn/managing-state) |
| `bash_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/languages/bash.sh) |
| `linux_commands.md` | resource list/course material; verify substantive reference coverage | [community](https://raw.githubusercontent.com/jlevy/the-art-of-command-line/master/README.md) |
| `git_reference.md` | Missing provenance | [official](https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging) |
| `docker_reference.md` | Missing provenance | [official](https://docs.docker.com/get-started/docker-concepts/running-containers/persisting-container-data/) |
| `sql_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/enochtangg/quick-SQL-cheatsheet/master/README.md) |
| `nodejs_reference.md` | Missing provenance | [official](https://nodejs.org/docs/latest-v22.x/api/fs.html) |
| `express_reference.md` | Missing provenance | [official](https://expressjs.com/en/guide/error-handling.html) |
| `django_reference.md` | Missing provenance | [official](https://docs.djangoproject.com/en/5.2/topics/db/queries/) |
| `go_reference.md` | Missing provenance | [official](https://go.dev/doc/effective_go) |
| `php_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/languages/php.php) |
| `vim_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/tools/vim.txt) |
| `csharp_reference.md` | Missing provenance | [official](https://learn.microsoft.com/en-us/dotnet/csharp/fundamentals/types/) |
| `csharp_design_patterns.md` | Missing provenance | [community](https://raw.githubusercontent.com/nemanjarogic/DesignPatternsLibrary/master/README.md) |
| `ruby_reference.md` | Missing provenance | [official](https://docs.ruby-lang.org/en/master/syntax/methods_rdoc.html) |
| `cpp_modern_reference.md` | Missing provenance | [maintained reference](https://en.cppreference.com/w/cpp/memory/unique_ptr.html) |
| `swift_reference.md` | legacy API/version material; review against target version | [official](https://raw.githubusercontent.com/swiftlang/swift-book/swift-6.3-fcs/TSPL.docc/LanguageGuide/TheBasics.md) |
| `swiftui_reference.md` | legacy API/version material; review against target version | [official](https://developer.apple.com/tutorials/data/documentation/swiftui/managing-model-data-in-your-app.md) |
| `swift_design_patterns.md` | Missing provenance | [community](https://raw.githubusercontent.com/ochococo/Design-Patterns-In-Swift/master/README.md) |
| `kotlin_reference.md` | Missing provenance | [official](https://kotlinlang.org/docs/null-safety.html) |
| `lua_reference.md` | Missing provenance | [official](https://www.lua.org/manual/5.4/manual.html) |
| `elixir_reference.md` | Missing provenance | [official](https://hexdocs.pm/elixir/pattern-matching.html) |
| `haskell_reference.md` | Missing provenance | [official](https://www.haskell.org/onlinereport/haskell2010/haskellch3.html) |
| `perl_reference.md` | Missing provenance | [official](https://perldoc.perl.org/perlintro) |
| `scala_reference.md` | Missing provenance | [official](https://docs.scala-lang.org/scala3/book/types-introduction.html) |
| `dart_reference.md` | resource list/course material; verify substantive reference coverage | [official](https://dart.dev/language/functions) |
| `regex_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/lyudaio/cheatsheets/main/programming_languages/regex.md) |
| `react_patterns.md` | Missing provenance | [official](https://react.dev/learn/you-might-not-need-an-effect) |
| `vue_reference.md` | legacy API/version material; review against target version | [official](https://vuejs.org/guide/essentials/reactivity-fundamentals.html) |
| `angular_reference.md` | Missing provenance | [official](https://angular.dev/guide/components) |
| `nextjs_reference.md` | legacy API/version material; review against target version | [official](https://nextjs.org/docs/app/getting-started/fetching-data) |
| `svelte_reference.md` | Missing provenance | [official](https://svelte.dev/docs/svelte/what-are-runes) |
| `tailwind_reference.md` | Missing provenance | [official](https://tailwindcss.com/docs/styling-with-utility-classes) |
| `bootstrap_reference.md` | Missing provenance | [official](https://getbootstrap.com/docs/5.3/layout/grid/) |
| `jquery_reference.md` | resource list/course material; verify substantive reference coverage | [community](https://raw.githubusercontent.com/AllThingsSmitty/jquery-tips-everyone-should-know/master/README.md) |
| `flask_reference.md` | Missing provenance | [official](https://flask.palletsprojects.com/en/stable/quickstart/) |
| `fastapi_reference.md` | resource list/course material; verify substantive reference coverage | [official](https://fastapi.tiangolo.com/tutorial/body/) |
| `rails_reference.md` | Missing provenance | [official](https://guides.rubyonrails.org/active_record_basics.html) |
| `spring_reference.md` | resource list/course material; verify substantive reference coverage | [official](https://spring.io/guides/gs/rest-service) |
| `laravel_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/backend/laravel.php) |
| `aspnet_reference.md` | Missing provenance | [official](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/minimal-apis/overview?view=aspnetcore-10.0) |
| `gin_reference.md` | Missing provenance | [official](https://raw.githubusercontent.com/gin-gonic/gin/master/README.md) |
| `rust_web_reference.md` | Missing provenance | [official](https://docs.rs/axum/latest/axum/) |
| `flutter_reference.md` | Missing provenance | [official](https://docs.flutter.dev/data-and-backend/state-mgmt/simple) |
| `react_native_reference.md` | Missing provenance | [official](https://reactnative.dev/docs/typescript) |
| `android_reference.md` | Missing provenance | [official](https://developer.android.com/develop/ui/compose/state) |
| `ios_reference.md` | duplicate content: swift_reference.md; legacy API/version material; review against target version | [official](https://developer.apple.com/tutorials/data/documentation/uikit/managing-your-app-s-life-cycle.md) |
| `unity_reference.md` | Missing provenance | [official](https://docs.unity3d.com/6000.0/Documentation/Manual/execution-order.html) |
| `unreal_reference.md` | resource list/course material; verify substantive reference coverage | [official](https://dev.epicgames.com/documentation/en-us/unreal-engine/programming-with-cplusplus-in-unreal-engine) |
| `godot_reference.md` | resource list/course material; verify substantive reference coverage | [official](https://docs.godotengine.org/en/stable/tutorials/scripting/gdscript/gdscript_basics.html) |
| `sql_advanced.md` | Missing provenance | [community](https://raw.githubusercontent.com/crescentpartha/CheatSheets-for-Developers/main/CheatSheets/sql-cheatsheets.md) |
| `postgres_reference.md` | Missing provenance | [official](https://www.postgresql.org/docs/current/tutorial-join.html) |
| `mysql_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/databases/mysql.sh) |
| `mongodb_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/databases/mongodb.sh) |
| `redis_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/databases/redis.sh) |
| `sqlalchemy_reference.md` | Missing provenance | [official](https://docs.sqlalchemy.org/en/20/orm/session_basics.html) |
| `graphql_reference.md` | Missing provenance | [official](https://graphql.org/learn/schema/) |
| `rest_api_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/RestCheatSheet/api-cheat-sheet/master/README.md) |
| `websocket_reference.md` | resource list/course material; verify substantive reference coverage | [maintained reference](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API/Writing_WebSocket_client_applications) |
| `oauth_reference.md` | Missing provenance | [official](https://www.rfc-editor.org/rfc/rfc9700.html) |
| `kubernetes_reference.md` | Missing provenance | [official](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) |
| `nginx_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/LeCoupa/awesome-cheatsheets/master/tools/nginx.sh) |
| `terraform_reference.md` | Missing provenance | [official](https://developer.hashicorp.com/terraform/language/resources/syntax) |
| `ansible_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/germainlefebvre4/ansible-cheatsheet/master/README.md) |
| `github_actions_reference.md` | Missing provenance | [official](https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions) |
| `cmake_reference.md` | Missing provenance | [official](https://raw.githubusercontent.com/Kitware/CMake/v4.1.0/Help/manual/cmake-buildsystem.7.rst) |
| `powershell_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/ab14jain/PowerShell/master/README.md) |
| `markdown_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/adam-p/markdown-here/master/README.md) |
| `npm_reference.md` | Missing provenance | [official](https://docs.npmjs.com/cli/v11/configuring-npm/package-json) |
| `pandas_reference.md` | Missing provenance | [official](https://pandas.pydata.org/docs/user_guide/10min.html) |
| `numpy_reference.md` | Missing provenance | [official](https://numpy.org/doc/stable/user/absolute_beginners.html) |
| `pytorch_reference.md` | Missing provenance | [official](https://docs.pytorch.org/tutorials/beginner/basics/autogradqs_tutorial.html) |
| `design_patterns_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/mutasim77/design-patterns/main/README.md) |
| `system_design_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/donnemartin/system-design-primer/master/README.md) |
| `clean_code_reference.md` | Missing provenance | [community](https://raw.githubusercontent.com/ryanmcdermott/clean-code-javascript/master/README.md) |

The full current source catalog and version notes live in
[`coder_sources.py`](../backend/seed_kb/coder_sources.py). Maintenance commands,
failure handling and recovery guidance are in [coder-docs.md](coder-docs.md).

## Deployment validation

- Deployed the persona, shared RAG changes, maintenance CLI and changelog.
- Verified source URLs and file hashes for all 124 documents; vector/keyword
  indexing completed without errors or warnings.
- Passed 109 backend regression checks; seven optional tests skipped.
- Live retrieval returned relevant Python, TypeScript, Rust, Java, SQL, Swift
  and SwiftUI references with provenance.
- Browser verification found Master Developer under Agents → Personas, with
  no console errors, page errors or failed assets.
- A minimal SwiftUI reference-usage example with Observation, State and Bindable
  passed `swiftc -typecheck` using Swift 6.3.3 and the macOS 14 target. This is
  a type check of shared SwiftUI APIs, not an iOS application build. The final
  persona-generated SwiftUI alternatives also passed independently; citations
  matched supplied KB/web sources.
- A saved persona chat ran Python assertions successfully through the existing
  Codebox tool; an unfamiliar-language question received Quick Search results.
- Reseeding retained the persona ID and model; Daedalus kept the same shared KB.

A private pre-refresh backup under `/root/hyprchat-rollback/` includes a verified
SQLite backup, original KB files, replaced code, and a logical Chroma collection
export. Recovery should target the affected KB/persona records, preserving any
subsequent user activity. Temporary validation conversations were removed.

Answer-quality limit: the Zig smoke test used community sources and produced an
incorrect cleanup comment (errdefer applies to errors after registration). It was
not compiler-verified. Tool/retrieval checks and verified SwiftUI/Python examples
do not establish correctness for every generated answer or language; this remains
dependent on the selected model. No autonomous build/validation pipeline was added.
