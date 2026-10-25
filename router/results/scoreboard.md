# reprouter offline scoreboard

Generated 2026-10-25 23:36; token counts from count_tokens (claude-haiku-5-5); Claude Code system+tools prefix = 27,599 tokens.

- `api_5m_haiku`: API key, 5m cache, Haiku/other read 0.1x
- `sub_1h_sonnet`: subscription, 1h cache, Sonnet/Opus 5.5 read 0.05x

Corpus: 9 sessions, 1196 tool results, 793,600 tool-result tokens (count_tokens).

| transform | lossless | blocks | routed tokens before → after | saved | share of session cost (api_5m_haiku) | share of session cost (sub_1h_sonnet) | fidelity retained (paired n) | verdict |
|---|---|---|---|---|---|---|---|---|
| tool_input_elide | no | 432 | 844,162 → 98,932 | 745,230 | 28.71% | 28.98% | – | off: candidate: needs the end-to-end run (does Claude re-read or fail more? does the binding canary stay green?) before it can be enabled |
| image | no | 22 | 134,162 → 84,691 | 49,471 | 2.47% | 2.39% | 81% (59) | off: cannot be delivered live: rewriting a block the model already saw breaks preserved-thinking binding, and new blocks must stay text (recent turns) |
| alias | yes | 301 | 142,251 → 122,878 | 19,373 | 0.64% | 0.66% | 100% (12) | off: live delivery needs exact replay of model-written aliases; experimental, off by default |
| log_filter | no | 2 | 10,429 → 1,203 | 9,226 | 0.32% | 0.33% | 94% promised / 70% all (47) | off: fidelity 94% < 95% on promised probes |
| linenum_sparse | no | 3 | 13,978 → 12,772 | 1,206 | 0.07% | 0.07% | 56% (16) | off: savings too small (0.070% of session cost < 0.10%) |
| linenum_compact | yes | 3 | 18,909 → 17,996 | 913 | 0.05% | 0.05% | – | off: savings too small (0.054% of session cost < 0.10%) |
| whitespace | yes | 61 | 86,719 → 85,728 | 991 | 0.04% | 0.04% | 100% (12) | off: savings too small (0.037% of session cost < 0.10%) |
| dedup | yes | 1 | 194 → 60 | 134 | 0.01% | 0.01% | 81% (16) | off: savings too small (0.007% of session cost < 0.10%) |
| json_minify | yes | 1 | 206 → 176 | 30 | 0.00% | 0.00% | 100% (21) | off: savings too small (0.001% of session cost < 0.10%) |
| csv_table | no | 0 | 0 → 0 | 0 | 0.00% | 0.00% | 100% (21) | off: no savings on the corpus (never applied) |
| diff_reread | yes | 0 | 0 → 0 | 0 | 0.00% | 0.00% | 58% (12) | off: no savings on the corpus (never applied) |
| skeleton | no | 0 | 0 → 0 | 0 | 0.00% | 0.00% | 92% promised / 75% all (16) | off: no savings on the corpus (never applied) |
| toon | yes | 0 | 0 → 0 | 0 | 0.00% | 0.00% | 100% (21) | off: no savings on the corpus (never applied) |
