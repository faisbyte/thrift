# ADR 0001: How reprouter integrates with Claude Code

Status: accepted (2026-10-25). Revisit after the first end-to-end benchmark.

## Context

reprouter rewrites context blocks into cheaper representations: TOON, CSV, filtered logs,
skeletons, aliases, diffs, dedup references and images. It can only rewrite what its integration
point can reach. We checked current Claude Code and API docs for each option.

| Mechanism | Can reach | Cannot reach | Notes |
|---|---|---|---|
| PreToolUse hook (`updatedInput`) | Bash commands before they run (RTK's approach) | Outputs, history | Known bug: ignored in `claude -p` in 2.1.71 (fix unconfirmed) |
| PostToolUse hook (`updatedToolOutput`) | Each new tool result, any tool | System prompt, tool defs, earlier history, images | Must match the tool's output shape or it is silently ignored. Claude Code stores the replaced output in its own transcript. |
| Local proxy (`ANTHROPIC_BASE_URL`) | The full request (system, tools, all history, images) and the streamed response | Nothing in the request | Works with subscription OAuth when `anthropic-beta` is forwarded. Must stream SSE unbuffered, keep the system block order (attribution block) and cache markers. `x-claude-code-request-class` identifies main, subagent, compaction and auxiliary calls. |
| Plugin | Bundles hooks, MCP and skills | Cannot set `ANTHROPIC_BASE_URL` | Packaging only |

Two constraints decide the design.

1. **Preserved thinking.** On the 5.5 models, every thinking block is bound to a byte-identical
   copy of `system`, `tools` and all earlier messages. Changing an earlier message is a mismatch,
   and accounts created on or after 2026-08-31 get a 400. So a rewriter may never change a block
   after the model has seen it. Every rewrite must be decided when the block first arrives, then
   reproduced byte-identically on every later turn.
2. **Prompt caching.** Claude Code caches the whole prefix. A cache write costs 1.25x on the 5m TTL
   or 2x on the 1h TTL used on subscriptions; a read costs 0.1x, or 0.05x on Opus/Sonnet 5.5.
   Nearly all the value of compression comes when a block is first written. Retroactively
   compressing cached history costs a rewrite of everything after it and saves only read-priced
   tokens, which almost never pays.

## Decision

**One library of pure, deterministic transforms, with two delivery adapters.**

1. **Hook adapter (primary for compressing new tool results).** A PostToolUse hook returns
   `updatedToolOutput` with the transformed result. Because Claude Code persists the
   replacement itself, caching, resume and preserved thinking stay consistent without any
   replay. The hook gets `transcript_path`, so dedup and diff-on-reread can look at earlier
   results.
2. **Proxy (aiohttp), with four jobs:**
   - **Recorder:** builds the corpus.
   - **Verifier:** confirms a hook's substitution actually reached the API, and in test runs
     sets the binding canary (`prefix_mismatch_behavior: "error"`) so any accidental history
     edit fails loudly.
   - **Kill switch / passthrough.**
   - **Delivery for what hooks can't reach:** images, alias reverse-mapping in the streamed
     response, and tool-definition changes. Proxy rewrites follow the same rule: a block's
     representation is a pure function of the request prefix plus a config version pinned per
     session. Older blocks are recomputed to identical bytes, never re-decided.

**Invariants**
- Never add, remove or move `cache_control` markers.
- Never reorder `system`.
- Fail open: a transform error passes that new block through unchanged.
- Compaction and auxiliary request classes pass through untouched.

**Kill switches**
- `REPROUTER_MODE=off|passthrough|record|on`
- the flag file `~/.reprouter/off`
- the `x-reprouter-bypass` header
- per-transform enable flags, whose defaults come from the scoreboard

Switching off mid-session only affects new blocks. A hard `off` is honest passthrough and may
cost one cache rebuild.

## Consequences

- Transforms that need to rewrite old history are out on thinking-enabled 5.5 models: pxpipe-style
  history imaging and "aging" old reads into skeletons. They are still measured offline and with
  probes, for the record and for models or accounts without the binding.
- Skeletons only apply to reads of files outside the project root, which cannot be edit targets.
- Aliasing needs the exact bytes the model wrote replayed on later turns. It stays experimental
  and off by default unless the binding canary passes.
- The measured cost driver turned out to be the model's own tool-call inputs: 39.5% of
  cache-weighted input cost. `tool_input_elide` rewrites a tool_use input when it first
  appears in history. The edited bytes come after that message's thinking block, so the
  thinking that precedes them still sees an unchanged prefix, and every later turn sees the
  same elided bytes. It is proxy-only. The end-to-end run with the canary on must confirm
  this before it is enabled.
- Benchmarks run the router arm through the proxy (`ANTHROPIC_BASE_URL`) and/or with the hook
  installed, so the delivery path itself is measured.

## Credits

Ideas borrowed from pxpipe (profitability gate, burn term, count_tokens as counterfactual,
factsheet against confabulation), TOON (compact tabular JSON), RTK (filtering command output
through a hook) and Aider's repo map (signature skeletons).
