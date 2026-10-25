# reprouter: first checkpoint (2026-10-25)

**Spend so far: $0.096 of the $3 cap** (`spend/ledger.jsonl`: fidelity probes $0.0961, live proxy
check $0.0001). Token counting used the free `count_tokens` endpoint. **The end-to-end benchmark
has not been run.** The plan and cost estimate are at the end, for approval.

## Recommendation

On this corpus, **no transform clears every bar yet**, so `defaults.json` enables nothing.

There is one large, unproven lever: **`tool_input_elide`**. It would save about **29% of
cache-weighted session cost** offline. It needs the end-to-end run to show it doesn't make
Claude re-read or fail more, and that the preserved-thinking canary stays green.

Everything the brief listed is small on real Claude Code traffic: TOON, CSV, images, skeletons,
aliasing, diffs and dedup. Most of it is near zero, or loses accuracy on Haiku.

## What was built

All of it is test-first. There are 402 router tests, and 518 tests in the repo in total.

- **[ADR-0001](../../docs/adr/0001-integration.md): hybrid integration.**
  - A PostToolUse hook compresses new tool output. Claude Code persists the replacement
    itself, so caching, resume and preserved thinking stay consistent.
  - A streaming proxy (`ANTHROPIC_BASE_URL`) does four jobs:
    - records requests
    - verifies with a canary that history wasn't edited
    - acts as the kill switch and passthrough
    - delivers what hooks can't reach
  - Two facts drive the design. On 5.5 models, preserved thinking rejects or drops edited
    history. And the prompt cache only pays when blocks are compressed as they arrive, never
    retroactively. So every decision is a pure function of the request prefix, made when a
    block first appears and replayed byte-identically after that.
- **Classifier and exactness.**
  - Block kinds: system prompt, tool definitions, user prompt, system reminder, assistant
    text, thinking and tool use.
  - Tool-result kinds: file read, JSON, tabular, log, test output, command output, diff,
    search results, edit confirmation and error.
  - Recency: static, entry, recent or old.
  - Byte-exact risks: hash, UUID, path, ID and code-edit target.
- **Router.**
  - Runs cleanup transforms, then picks the cheapest main transform that clears a 10%
    margin, under fidelity rules.
  - Decisions are byte-stable across turns (tested).
  - The session pin is persisted, so history from before the router is never touched.
- **13 transforms**, each its own module with its own tests: `json_minify`, `toon`,
  `csv_table`, `log_filter`, `skeleton`, `alias`, `diff_reread`, `dedup`, `whitespace`,
  `linenum_compact`, `linenum_sparse`, `image` (pxpipe-style, with a factsheet) and
  `tool_input_elide` (new).
- **Proxy.**
  - Passthrough, record and on modes; kill file; bypass header; fail-open; compaction and
    auxiliary calls passed through.
  - Canary mode adds `thinking-binding-controls` with `prefix_mismatch_behavior: error`.
  - Validated live: `count_tokens` and a streamed message through the proxy, with the full
    SSE sequence including `ping`, usage recorded and no credentials written.
- **Measurement.**
  - Corpus reconstruction from Claude Code transcripts.
  - Cache simulator: breakpoints, 20-block lookback, TTL, 512-token minimum, per-model
    read price.
  - Offline scoreboard, Haiku fidelity probes, spend ledger with a hard cap, and benchmark
    arms `base-proxy`, `router` and `router-hook`.

## Offline scoreboard

- **Corpus:** 9 sessions and 1,196 tool results from your `~/.claude/projects`. That's
  793,600 tool-result tokens; Claude Code's fixed system+tools prefix is 27,599 tokens.
- **How "share" is computed:** cache-weighted savings as a share of total session input cost.
  A saved token is worth one cache write plus one cache read per later turn. Two regimes:
  - **API:** 5m TTL, 0.1x read
  - **Subscription:** 1h TTL, 0.05x read on Sonnet/Opus 5.5
- **Full table:** [`scoreboard.md`](scoreboard.md).

| transform | blocks | routed tokens before → after | share (API / sub) | Haiku fidelity retained | verdict |
|---|---|---|---|---|---|
| tool_input_elide | 432 | 844,162 → 98,932 | **28.7% / 29.0%** | not probed (behavioural) | **candidate for e2e** |
| image | 22 | 134,162 → 84,691 | 2.5% / 2.4% | 81% (n=59, p=0.006) | off: not deliverable live (thinking binding), and confabulates exact strings |
| alias | 301 | 142,251 → 122,878 | 0.6% / 0.7% | 100% (n=12) | off: live delivery needs exact replay of model-written aliases |
| log_filter | 2 | 10,429 → 1,203 | 0.3% / 0.3% | 94% of what it keeps / 70% overall (n=47) | off: 94% < 95% bar; small savings |
| linenum_sparse | 3 | 13,978 → 12,772 | 0.07% | 56% | off |
| linenum_compact | 3 | 18,909 → 17,996 | 0.05% | (lossless) | off: too small |
| whitespace | 61 | 86,719 → 85,728 | 0.04% | 100% | off: too small (harmless) |
| dedup | 1 | 194 → 60 | 0.01% | 81% | off |
| json_minify | 1 | 206 → 176 | 0.00% | 100% | off: no JSON in this corpus |
| csv_table, toon | 0 | – | 0% | 100% | off: never applied (no record-like JSON) |
| diff_reread | 0 | – | 0% | 58% | off: never applied, and confuses Haiku |
| skeleton | 0 | – | 0% | 92% of signatures / 75% overall | off: never applied (no reads outside the project) |

**Where the input cost goes** (cache-weighted, [`attribution.json`](attribution.json)):

| source | share |
|---|---|
| assistant tool-use inputs | 39.5% |
| tool results | 34.3% |
| user prompts | 11.2% |
| fixed system+tools prefix | 11.1% |
| assistant text | 3.2% |

## What the numbers say

1. **The brief's transforms target content this workload barely has.** Your sessions contain
   almost no JSON tool results, so TOON and CSV have nothing to work on. There are no reads
   outside the project, so skeleton never applies. There are no repeated `Read` calls, so
   diff_reread never applies. File viewing goes through `cat` and `sed` in Bash, and must
   stay byte-exact for later edits.
2. **A safety bug was caught by measurement.** The first scoreboard credited log_filter with
   **14.5%**. That number came from filtering `cat`/`sed`/`ls` listings down to their "error"
   lines, which is silent data loss. It now only filters output from known test, build and
   log commands, RTK-style. The true figure is 0.3%.
3. **Images confabulate on Haiku, as pxpipe found for non-Fable models.** Exact-string
   retention is 79%, and the misses are silent wrong answers. Images also can't be delivered
   live: imaging old history breaks the thinking binding, and new blocks must stay text.
4. **"Lossless by reference" isn't lossless for the model.**
   - dedup's misses (16 probes) are all one off-by-one count (60 → 59) when Haiku had to
     look back at the earlier copy.
   - diff_reread's misses (12 probes) are cases where Haiku couldn't reconstruct the new
     file to answer line or count questions.
5. **log_filter misses for a cautious reason, not a false one.** Its two promised-focus
   misses are refusals ("the output is filtered, so the count can't be determined"), not
   wrong answers. A clearer footer moved it from 90% to 94%. Detail it removes by design
   (passing tests, INFO lines) is gone, at 25% retention.
6. **The real money is in Claude's own past tool calls.** Large Write and Edit bodies and
   Bash heredocs are resent every turn. A caveat: most of this corpus is this repository's
   own build sessions, which use unusually large heredocs. Your typical coding sessions may
   differ.

## Proposed end-to-end run (needs your approval)

The goal is to test the one candidate lever and the borderline lossless set on real tasks,
measuring both cost and success.

**Before spending:**
- Add 4 heavier tasks to `bench/tasks.yaml`: write a new module plus tests (large Write), a
  multi-file refactor (large Edits), run the full suite and fix 2 failures (long pytest
  output), and analyse a JSON results file. Each gets an automatic check, validated with
  `--validate`.
- The existing 16 tasks are short, about 5 turns, so on their own they would show little
  either way.

**Arms (all through the proxy, Haiku):**
- `base-proxy`: records exact bodies and usage, no transforms.
- `router-elide`: `tool_input_elide` only, canary on.
- `router-all`: `tool_input_elide`, `log_filter`, `whitespace`, `json_minify`, `toon`,
  `csv_table`; canary on.

**Steps:**
1. **Smoke:** 1 heavy task × 3 arms × 1 repeat. That checks routing happens, the canary
   stays green, and the proxy records. Estimated **$0.05**.
2. **Main:** 20 tasks × 3 arms × 2 repeats = 120 runs. Haiku runs cost about $0.002–0.005
   each on the short tasks, and an estimated $0.01–0.03 on the heavy ones, so **about
   $0.6–1.0 total**. Hard cap: `--max-cost 1.25`.
3. **Report:** cost and success by task type, the hook_hit/miss-style breakdown per arm,
   canary errors, re-read counts, and tokens saved per transform from the proxy
   decisions.

**Totals:** about $1.1 worst case, which would leave about $1.8 of the cap for a confirming
Sonnet spot check on the winners. To spend $0, I can run it on your subscription instead:
set `CLAUDE_CODE_OAUTH_TOKEN`, and the costs become estimates outside the cap.
