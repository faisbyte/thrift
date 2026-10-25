# reprouter: a representation router for Claude Code

reprouter tries to cut Claude Code's token bill by choosing the **cheapest faithful
representation for each block of context**: TOON or CSV for record-like JSON, filtered test and
build logs, signature skeletons, path aliases, diffs for re-reads, dedup references, whitespace
cleanup, and pxpipe-style images. Byte-exact material stays byte-exact.

It is built to be measured honestly. An offline scoreboard uses exact token counts and a
prompt-cache cost model. Fidelity probes on Haiku measure what each transform loses. An
end-to-end benchmark arm runs Claude Code through the router. Any transform that doesn't pay off,
or loses accuracy, is off by default.

> Status (first checkpoint): core, 12 transforms, proxy, hook adapter, scoreboard and probes
> are built and tested. The end-to-end benchmark has **not** been run yet. See
> [`results/CHECKPOINT.md`](results/CHECKPOINT.md).

## How it plugs into Claude Code

The full reasoning is in [docs/adr/0001-integration.md](../docs/adr/0001-integration.md). In short:

- **Hook adapter.** This is the primary path for compressing new tool output. A PostToolUse hook
  returns `updatedToolOutput` in the tool's own output shape. Claude Code stores the
  replacement itself, so prompt caching, resume and preserved thinking stay consistent with no
  extra work. It handles Bash today: Read results get their line numbers added after the hook
  runs, so Read transforms go through the proxy instead.
- **Local proxy (`ANTHROPIC_BASE_URL`).** It records requests for the corpus, verifies that
  nothing edited history (with the canary), and acts as the kill switch. It also delivers what
  hooks can't reach. It streams SSE unbuffered, forwards credentials untouched (API key or
  subscription OAuth), never writes credentials, and passes compaction and auxiliary calls
  through untouched.
- **Determinism is the core invariant.** Claude Code resends the whole history every turn. On
  the 5.5 models, thinking blocks are bound to a byte-identical copy of the earlier context;
  edited history is dropped or rejected. So every decision is a pure function of the request
  prefix, made when a block first appears and reproduced byte-for-byte on later turns. Sessions
  the proxy first sees mid-way are pinned so pre-existing history is never touched, and that
  pin survives restarts.

```
reprouter proxy --mode on           # then: export ANTHROPIC_BASE_URL=http://127.0.0.1:47900
reprouter run -- claude             # proxy + Claude Code in one command
reprouter off | on | status         # kill switch (flag file ~/.reprouter/off, checked per request)
REPROUTER_MODE=passthrough          # or off / record / on
x-reprouter-bypass: 1               # per-request bypass header
REPROUTER_TRANSFORMS=whitespace,log_filter   # override the enabled set
```

Hook install, in `settings.json`:

```json
{"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "reprouter hook"}]}]}}
```

## Pipeline

1. **`request.py`** parses a `/v1/messages` body into addressable blocks and applies
   replacements without touching any other bytes. `cache_control` markers are kept as they are.
2. **`classify.py` and `exactness.py`** label each block. Kinds: system prompt, tool
   definitions, user prompt, system reminder, assistant text, thinking, and tool results by
   kind (file read, JSON, tabular, log, test output, command output, diff, search results,
   edit confirmation, error). Recency: static, entry, recent or old. Byte-exact risks: hash,
   UUID, path, ID, and `code_edit_target` (a read of a file inside the project).
3. **`router.py`** runs cleanup transforms, then picks the cheapest main transform that saves
   at least 10%, under fidelity rules:
   - lossy transforms skip edit targets
   - non-text output needs opt-in and no exact-string risk
   - decisions never depend on recency, so routed history is byte-stable across turns
     (tested)
4. **`proxy/server.py` and `hook.py`** deliver the result. **`simulate.py`** replays a session
   turn by turn through the prompt cache (breakpoints, lookback, TTL, 512-token minimum,
   per-model read price).

## Transforms

Each transform is its own module with its own tests, under `src/reprouter/transforms/`.

| transform | kind | what it does |
|---|---|---|
| `json_minify` | lossless | compact JSON; number literals and key order preserved |
| `toon` | lossless | TOON for record-like JSON, only on an exact round trip and only if smaller than minified JSON |
| `csv_table` | lossless only for all-string data | CSV for flat uniform records |
| `log_filter` | lossy | test/build/log output reduced to failures, errors and summaries with context. Generic command output only when the command is a known runner (RTK-style) |
| `skeleton` | lossy | Aider-style signature skeleton for reads of files outside the project |
| `alias` | lossless with legend | long repeated path prefixes become `§N/`; legend at first use; `expand_aliases` reverse-maps tool inputs |
| `diff_reread` | lossless by reference | a re-read becomes a diff against the earlier read of the same file |
| `dedup` | lossless by reference | an identical repeated tool result becomes a reference |
| `whitespace` | lossless (presentation) | ANSI codes, carriage-return overwrites and trailing spaces; search results keep trailing spaces |
| `linenum_compact` | lossless | padded Read line numbers made unpadded |
| `linenum_sparse` | lossy | line numbers only every 10th line |
| `image` | lossy | pxpipe-style PNG pages (vendored Spleen font) plus a verbatim factsheet of exact strings |

## Measuring it

```
uv run python router/scripts_run_scoreboard.py [router/results/fidelity_summary.json]   # free (count_tokens)
uv run python router/scripts_run_probes.py                                               # paid, ledger-gated
uv run python bench/run.py --arms base-proxy,router --tasks edit --repeats 2 --max-cost 1   # paid, ask first
```

**Spend.** Every paid call goes through `spend/ledger.jsonl`. It refuses any call that would
take total billed API spend past the $3 project cap. Subscription runs are reported but not
counted against the cap.

**Results:** [`results/scoreboard.md`](results/scoreboard.md) (offline savings),
[`results/fidelity_summary.json`](results/fidelity_summary.json) (probes) and
[`results/CHECKPOINT.md`](results/CHECKPOINT.md) (the write-up).

## Credits

Ideas borrowed, with thanks:

- [**pxpipe**](https://github.com/teamchong/pxpipe) (MIT): rendering dense context as images,
  the profitability gate with a burn term for warm caches, using the free `count_tokens`
  endpoint as a counterfactual, and the factsheet mitigation for silent confabulation of exact
  strings.
- [**TOON**](https://github.com/toon-format/toon): compact tabular encoding of JSON for LLM
  input (we use the official `toon-format` Python package).
- [**RTK**](https://github.com/rtk-ai/rtk): filtering command output through a Claude Code hook,
  keyed on the command that produced it.
- [**Aider's repo map**](https://aider.chat/docs/repomap.html): tree-sitter signature skeletons
  with elision.
- The vendored [**Spleen**](https://github.com/fcambus/spleen) bitmap font (BSD-2-Clause).
