# thrift

**Project-specific code retrieval for Claude Code, built to save tokens.**

> **New: [reprouter](router/README.md)** is a representation router. It picks the cheapest
> faithful representation for each block of Claude Code context, measured offline with
> exact token counts and a cache model, and probed for fidelity on Haiku. First-checkpoint
> results: [router/results/CHECKPOINT.md](router/results/CHECKPOINT.md). Ideas borrowed from
> [pxpipe](https://github.com/teamchong/pxpipe), [TOON](https://github.com/toon-format/toon),
> [RTK](https://github.com/rtk-ai/rtk) and
> [Aider's repo map](https://aider.chat/docs/repomap.html); credits in the reprouter README.

Without help, Claude finds code by grepping and reading whole files. thrift
indexes your project locally and gives Claude compact pointers and snippets
instead, so it reads less to find what matters. The repo also includes a
benchmark harness that measures whether that actually happens.

- **Indexer**: walks the project and respects `.gitignore`. It skips binaries, lockfiles and minified files. It chunks code by symbol with tree-sitter (functions, classes, methods, types) and falls back to line windows for other files. Re-indexing is incremental (mtime/size, confirmed by sha1).
- **Ranking**: SQLite FTS5 BM25 over identifier-split terms, re-scored with code-specific signals: exact symbol match, IDF-weighted query coverage, path match, and demotion of test and non-core files. Results are diversified. It is fully local, needs no API keys, and has a `Ranker` interface so embeddings can be added later.
- **MCP server**: `search_code`, `get_symbol` and `related_files`. Output is compact text with capped snippets and never dumps whole files.
- **Prompt hook**: on each prompt, adds a short "likely relevant" pointer line (paths, line ranges and symbols; at most ~150 tokens; no code). It takes about 50 ms, and it stays silent when there is no index or no confident match, or on any error.
- **Benchmark**: headless A/B runs of Claude Code on a pinned repo, with and without the plugin, plus an offline retrieval evaluation.

Languages with symbol chunking: Python, JavaScript/TypeScript (incl. JSX/TSX),
Rust, Java, C and C++. Markdown is split by heading. Everything else is chunked
in line windows.

```
repo/
├── .claude-plugin/marketplace.json     local marketplace ("thrift-local")
├── plugins/thrift/                     the plugin
│   ├── .claude-plugin/plugin.json
│   ├── .mcp.json                       MCP server "thrift" → scripts/run-mcp.sh
│   ├── hooks/hooks.json                UserPromptSubmit + SessionStart
│   ├── skills/ctx-index/SKILL.md       /thrift:ctx-index (also /ctx-index)
│   ├── scripts/                        launchers (bash) + stdlib hook entry point
│   ├── pyproject.toml, uv.lock         runtime deps
│   └── src/thriftctx/                  the Python package
├── bench/                              benchmark harness + tasks
└── tests/                              pytest suite
```

## Requirements

- Claude Code (tested with 2.1.310)
- [`uv`](https://docs.astral.sh/uv/). The plugin uses it to create its own
  dependency environment on first use, in `~/.claude/plugins/data/<plugin>/`.
  uv downloads a suitable Python if needed.
- `python3` 3.9+ on `PATH`. The prompt hook is stdlib-only and falls back to it
  before the plugin venv exists.
- macOS or Linux. Windows is untested: the launchers are bash scripts, and the
  writer lock uses `fcntl`.

## Install

From a clone of this repo:

```bash
claude plugin marketplace add /path/to/thrift
claude plugin install thrift@thrift-local
```

Or inside a session: `/plugin marketplace add /path/to/thrift`, then
`/plugin install thrift@thrift-local`. Because the marketplace is a local
directory, Claude Code reads the plugin in place. Edits take effect after
`/reload-plugins` or in a new session.

To try it without installing: `claude --plugin-dir /path/to/thrift/plugins/thrift`.

Check it loaded: `/mcp` should list `plugin:thrift:thrift` as connected. The
first connection can take a few seconds while dependencies install.

## Index a project

In a Claude Code session in your project:

```
/thrift:ctx-index          # or /ctx-index; incremental after the first run
/thrift:ctx-index --full   # rebuild from scratch
```

You often don't need to run it at all:

- The **first MCP tool call** builds the index if it's missing, capped at about
  60s. A repo of ~350 files and 50k lines takes about 0.5s.
- Before each MCP call, the server re-checks the tree for changed files, at most
  once every 5s.
- A **SessionStart** hook refreshes an existing index in the background.

The index lives in `<project>/.thrift/`, which contains a `.gitignore` of `*`,
so it never gets committed. Delete the directory to drop the index.

### CLI

The same operations are available outside Claude Code:

```bash
S=/path/to/thrift/plugins/thrift/scripts/thriftctx.sh
$S index /path/to/project           # build/refresh (--full to rebuild)
$S --root /path/to/project search "retry backoff for http client" -k 5
$S --root /path/to/project symbol Console.export_html   # or path.py:name, ns::Type::fn
$S --root /path/to/project related src/app/session.py
$S --root /path/to/project stats
```

## What Claude sees

**Prompt hook.** For a prompt like *"why does the progress bar show the wrong
time remaining estimate"*, Claude receives this as context:

```
thrift index matches for this prompt (file, line range, symbol): rich/progress.py L1040-1052 Task.time_remaining,
L772-817 TimeRemainingColumn; rich/progress_bar.py L18-207 ProgressBar, ...
```

The hook stays silent in these cases:
- slash commands
- conversational prompts ("thanks, looks good", "run the tests again")
- no term in the prompt matches a symbol, path or signature
- the index is missing, locked or corrupt
- the 250 ms internal deadline is exceeded

**MCP tools** (they appear as `mcp__plugin_thrift_thrift__*`):

| tool | returns |
|---|---|
| `search_code(query, k=8)` | Up to 20 hits. Each is a `path:Lstart-end  kind  Qualified.name  signature` line plus a numbered snippet of at most 6 lines / 400 chars. |
| `get_symbol(name)` | The full definition, capped at 120 lines with an `offset`/`limit` hint for the rest. Big classes return their header plus a member outline. Ambiguous names return a candidate list. |
| `related_files(path)` | What the file imports and what imports it. Resolved for Python, relative JS/TS, C/C++ `#include "…"`, Java imports and Rust `mod`/`use crate::`. |

### Configuration

| env var | effect |
|---|---|
| `THRIFT_HOOK=0` | disable the prompt hook |
| `THRIFT_HOOK_LOG=/path/file.jsonl` | log every hook invocation (latency, fired, errors) |
| `THRIFT_PROJECT` | override the project root the MCP server and CLI use |

Ranking weights are in `plugins/thrift/src/thriftctx/rank.py` (`Weights`).

## Benchmark

The harness answers one question: with the plugin, does Claude Code finish the
same tasks while spending fewer tokens and less money?

### Setup

- **Repo.** [Textualize/rich](https://github.com/Textualize/rich), pinned at
  `9d8f9a37`: about 52k lines of Python, with a fast test suite.
- **Tasks.** 16 tasks plus a no-op overhead probe, all in `bench/tasks.yaml`:
  - **8 Q&A / locate tasks.** Judged by regexes on the answer, such as the
    right file and function names.
  - **8 edit tasks.** Each injects a bug, and the check is the repo's own tests
    (sometimes with one targeted assertion). `--validate` proves each check
    fails with the bug and passes with the fix.
- **Splits.** Tasks are split into `dev` and `holdout`. Tune the ranker only on
  `dev`.
- **Clean work directories.** Every run starts in a fresh copy of the repo,
  re-initialised as a single-commit git repo, so there's no history to mine.
- **Isolation.** The default mode runs with an empty `CLAUDE_CONFIG_DIR`, so
  none of your settings, plugins, hooks, MCP servers or CLAUDE.md leak into
  either arm.
- **Auth.** Credentials come from your environment or from `.env` at the repo
  root (gitignored; `.env.example` is the template). They are never printed or
  put on the command line. `--auth auto` (the default) uses an API key if one
  is set, otherwise your subscription:
  - `api-key`: `ANTHROPIC_API_KEY`. The report uses Claude Code's own cost
    figure.
  - `subscription`: `CLAUDE_CODE_OAUTH_TOKEN`. Create it once with
    `claude setup-token` (Pro, Max, Team or Enterprise plan), then paste it
    into `.env`. Runs bill to your plan, so the report shows an **estimated
    API cost** from token counts and published prices (`bench/pricing.py`).
  - `--mode bare` is available, but bare mode limits the built-in tools (no
    Grep/Glob) and may skip plugin hooks, so the comparison is less realistic.
  - `--mode user` uses your normal login but is **not** isolated.
- **Arms.** `baseline` and `thrift`. Optional ablations: `hook-only` (MCP tools
  disallowed) and `mcp-only` (`THRIFT_HOOK=0`). Every arm gets the same model,
  allowed tools, permission mode, turn limit and budget. Subagents are
  disallowed so token totals stay comparable.
- **Order.** Arms are interleaved and shuffled per task to spread prompt-cache
  and time-of-day effects. Index build time is recorded separately and not
  counted.

### Run it

```bash
uv sync                                         # dev env: pytest, pyyaml, thriftctx
# put ANTHROPIC_API_KEY=... or CLAUDE_CODE_OAUTH_TOKEN=... in .env at the repo root

uv run python bench/run.py --validate           # tasks are sound (no API calls)
uv run python bench/retrieval_eval.py           # offline recall@k (no API calls)
uv run python bench/run.py --smoke              # 1 probe per arm: plugin, MCP, hook all load
uv run python bench/run.py --tasks edit --repeats 2 --max-cost 2   # minimal run, haiku
uv run python bench/run.py --quick              # 6 tasks x 2 arms x 2 repeats ≈ 24 runs
uv run python bench/run.py --repeats 5          # full: 17 tasks x 2 arms x 5 ≈ 170 runs
uv run python bench/run.py --arms baseline,thrift,hook-only,mcp-only --repeats 3   # ablation
uv run python bench/report.py bench/results/<timestamp>
```

Useful flags:
- `--model`: default `haiku`.
- `--tasks`: `edit`, `qa`, `all`, task ids, or a comma list.
- `--max-cost USD`: stops starting new runs once the total for the results
  directory reaches the limit. Finished runs are kept, and the overshoot is at
  most one run.
- `--auth auto|api-key|subscription`.
- `--split dev|holdout|overhead`, `--max-budget-usd` (default 2 per run),
`--max-turns` (default 40), `--claude-bin` (auto-detects `claude` on PATH or
the VS Code extension's bundled binary), `--keep-workdirs`. Re-running with the
same `--out` resumes and skips finished runs.

**Rough cost:** Haiku 5.5 is about 20× cheaper per token than Sonnet 5.5. The
full run on Sonnet costs about $15–100. Use `--max-cost` to cap any run.

### What it records

These are recorded per run, in `runs.jsonl`, with the raw stream-json kept
under `runs/<key>/`:

- input tokens, split into uncached, cache-write and cache-read
- output tokens
- `total_cost_usd` (cross-checked against `modelUsage`)
- wall time and turns
- Read calls, unique files read, and the share of Reads that used `offset`/`limit`
- Grep, Glob and Bash calls
- thrift tool calls
- total characters of tool output
- whether the hook fired
- success, with budget, turn or time limits counted as failures

`report.py` prints and writes `summary.md` and `runs.csv`:

- **Per arm:** success rate, cost per success, and medians of every metric.
- **Paired, arm vs baseline:** for each task, the ratio of medians across
  repeats, summarised by their geometric mean with a bootstrap 95% CI over
  tasks.
- **Per task:** a breakdown of the above.
- **Fixed overhead:** the cost of tool schemas plus the hook, from the no-op
  task.

### Reading the results

- **The idea works if** the paired cost and input-token change is negative,
  the CI excludes 0, and the success rate doesn't drop.
- **Files read** and **tool output chars** show *where* any saving comes from.
- **thrift calls/run** and **hook fired** show whether Claude actually used the
  plugin. If those are low, retrieval quality doesn't matter.
- **Cost figures** are Claude Code's client-side estimates.
- **Known confound:** rich is a popular library, so some Q&A answers may come
  from the model's prior knowledge. The edit tasks are less exposed to this.

### Offline retrieval evaluation

`bench/retrieval_eval.py` checks whether each task's gold file ranks near the
top for the task prompt, and whether the hook's pointer line includes it. It
makes no LLM calls and runs in seconds, which makes it the tuning loop.
Override weights with `--set`, e.g. `--set test_penalty=0.5`.

Results at the current defaults:

| split | recall@1 | recall@3 | recall@5 | MRR | hook includes gold file |
|---|---|---|---|---|---|
| dev (tuned on) | 0.50 | 1.00 | 1.00 | 0.69 | 7/8 |
| holdout | 0.75 | 0.88 | 0.88 | 0.81 | 6/8 |

Before tuning (plain BM25 plus the first scoring heuristics), recall@5 over all
16 tasks was 0.81 and MRR 0.65. It is now 0.94 and 0.75.

## Development

```bash
uv sync
uv run pytest            # 99 tests: tokenizer, chunker (6 languages), ranking, incremental indexing,
                         # hook (latency, silence, failure modes), MCP formatting, bench plumbing
claude plugin validate ./plugins/thrift && claude plugin validate .
```

Design notes:

- **Stdlib-only hot path.** `tokenize`, `store`, `rank`, `format` and
  `hook_prompt` import nothing outside the stdlib, so the hook runs under any
  `python3 -S` in about 50 ms. Tree-sitter, `pathspec` and `mcp` are used only
  by the indexer and the MCP server.
- **Dependencies, not code, in the venv.** The venv is keyed by the hash of
  `uv.lock` and installed with `--no-install-project`. The launchers put
  `plugins/thrift/src` on `PYTHONPATH`, so code changes never need a reinstall.
- **Concurrency.** SQLite runs in WAL mode with a single writer, guarded by
  `.thrift/index.lock`. Readers (hook, MCP) open read-only with a short busy
  timeout.
- **Schema.** `meta` stores schema and tokenizer versions, and a mismatch
  triggers a full rebuild.
- **Adding embeddings.** Implement the `Ranker` protocol in `rank.py`: take
  `BM25Ranker.candidates()` and re-order them by vector similarity. Store
  vectors in a new table keyed by `chunks.id`.

### Known limitations

- `tree-sitter` is pinned below 0.26, because 0.26.0 segfaulted while chunking
  rich. `mcp` is pinned below 2 (v1 FastMCP API).
- Import resolution is heuristic (see the table above). There's no
  "co-referenced files" signal yet.
- The hook reads the index as of the last refresh, so line ranges can lag
  behind edits made in the current session until the next MCP call or session
  start.
- On macOS, recent CPython ignores `.pth` files carrying the `hidden` flag,
  which can break editable installs in `.venv`. The test config and launchers
  set the import path explicitly, so they're unaffected.
