# reprouter offline scoreboard

Generated 2026-10-25 23:16; token counts from count_tokens (claude-haiku-5-5); Claude Code system+tools prefix = 27,599 tokens.

- `api_5m_haiku`: API key, 5m cache, Haiku/other read 0.1x
- `sub_1h_sonnet`: subscription, 1h cache, Sonnet/Opus 5.5 read 0.05x

Corpus: 9 sessions, 1152 tool results, 780,980 tool-result tokens (count_tokens).

| transform | lossless | blocks | tool-result tokens before → after | saved | share of session cost (api_5m_haiku) | share of session cost (sub_1h_sonnet) | fidelity retained (paired n) | verdict |
|---|---|---|---|---|---|---|---|---|
| image | no | 22 | 134,162 → 84,691 | 49,471 | 2.56% | 2.47% | – | off: cannot be delivered live: rewriting a block the model already saw breaks preserved-thinking binding, and new blocks must stay text (recent turns) |
| alias | yes | 295 | 141,751 → 122,528 | 19,223 | 0.63% | 0.66% | – | off: live delivery needs exact replay of model-written aliases; experimental, off by default |
| log_filter | no | 3 | 11,507 → 1,675 | 9,832 | 0.32% | 0.33% | – | off: untested fidelity (no probe results) |
| linenum_sparse | no | 3 | 13,978 → 12,772 | 1,206 | 0.08% | 0.07% | – | off: savings too small (0.077% of session cost < 0.10%) |
| linenum_compact | yes | 3 | 18,909 → 17,996 | 913 | 0.06% | 0.06% | – | off: savings too small (0.060% of session cost < 0.10%) |
| whitespace | yes | 59 | 86,550 → 85,561 | 989 | 0.04% | 0.04% | – | off: savings too small (0.039% of session cost < 0.10%) |
| dedup | yes | 1 | 194 → 60 | 134 | 0.01% | 0.01% | – | off: savings too small (0.006% of session cost < 0.10%) |
| json_minify | yes | 1 | 206 → 176 | 30 | 0.00% | 0.00% | – | off: savings too small (0.001% of session cost < 0.10%) |
| csv_table | no | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
| diff_reread | yes | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
| skeleton | no | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
| toon | yes | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
