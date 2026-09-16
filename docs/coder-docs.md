# Master Developer and Coder Reference Docs

Master Developer is a conversational persona for coding questions, debugging,
reviews and focused examples across languages. It uses the existing chat loop,
Quick Search, KB citations and the ordinary `execute_code`/`download_file` tools.
The persona leaves its physical-appearance field empty so ordinary chat does not
inject roleplay/photo instructions. Its coding avatar uses the normal avatar field.
Daedalus remains the autonomous project workflow. No additional agent pipeline
or editor is involved.

`POST /api/seed/master-developer` creates or refreshes the current user's persona.
It also participates in Restore Defaults. Seeds preserve its ID and chosen model;
the initial model is `qwen3.5:27b`. A missing owned Coder Reference Docs KB is
reported as `kb_missing`; another user's KB is never attached. Normal saved chats
retrieve the KB; Ghost mode retains its existing KB/memory exclusion.

## Maintaining the shared reference library

Run with the same Python interpreter, service environment and user as HyprChat.
Check the systemd unit rather than assuming the optional `/opt/hyprchat/venv` is
what the service runs. The verified service uses `/usr/bin/python3` and user
`hyprchat`, with `/opt/hyprchat/backend` as its working directory.

```bash
python3 -m seed_kb.seed_coder_kb --audit --report /tmp/coder-docs-audit.json
python3 -m seed_kb.seed_coder_kb --refresh --report /tmp/coder-docs-refresh.json
```

Use `--user-id` for a different profile, `--kb-id` to select an owned KB, and
`--only swiftui_state.md swiftui_navigation.md` for a limited refresh. Without
`--refresh`, the command only audits inventory and fetches source material;
only an explicitly requested report is written. It never initializes the DB or
loads an embedding model in audit mode.

The catalog covers languages, frontend/backend frameworks, mobile/game tooling,
databases and developer tools. Swift/SwiftUI is one part of that library. Prefer
substantive official documentation; keep useful community references clearly
labelled. Avoid landing pages, screenshot-only cheatsheets and resource lists
that don't actually explain the API. Released Swift language-guide sources are
pinned; Apple Markdown retains platform-availability notes. Check target versions
when combining references from different releases.

Refreshes fetch through `research.fetch_bytes_safely`, normalize official
Markdown or use the existing HTML extractor, and call `rag.index_file`. Every
managed document records its source, authority, version, fetch date and content
hash. Swift-book compiler-test comments are excluded from reference examples.
HTML extraction and Markdown chunking preserve code indentation and repeat short
source/version notes. Oversized code blocks are split at line boundaries with
reopened fences so embedding limits do not silently discard their later sections.

The `.coder-docs-manifest.json` inside the KB directory records successful
updates. Unchanged documents are skipped; user-added files outside the catalog
are untouched. Modified managed files and filename collisions with another source
are reported for review. Original replaced files and metadata are copied under
`.coder-docs-backups/`. Back up the relevant database and Chroma collection before
a bulk maintenance run as well.

Indexing prepares all embeddings before changing either index. Partial embedding
failure keeps the previous index. Chroma write failures restore previous vectors;
obsolete chunks are pruned after successful upserts. FTS updates remain transactional
and report a warning if unavailable. Reports return nonzero on source/index errors
or indexing warnings; successful files remain usable and a rerun retries failures.

## Validation

Offline tests cover persona seeding/scoping, Markdown/code preservation, duplicate
and stale-source flags, unchanged refreshes, incomplete embeddings and failed
Chroma batches. Live checks should query several language/framework areas through
`POST /api/knowledge-bases/query`, then exercise a saved Master Developer chat with
KB and Quick Search events. Verify only runtime-supported examples with Codebox;
a retrieved language manual is not evidence that its compiler or SDK is installed.
