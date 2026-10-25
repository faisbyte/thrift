# reprouter offline scoreboard

Generated 2026-10-25 23:14; token counts from count_tokens (claude-haiku-5-5); Claude Code system+tools prefix = 27,599 tokens.

- `api_5m_haiku`: API key, 5m cache, Haiku/other read 0.1x
- `sub_1h_sonnet`: subscription, 1h cache, Sonnet/Opus 5.5 read 0.05x

Corpus: 9 sessions, 1145 tool results, 777,650 tool-result tokens (count_tokens).

| transform | lossless | blocks | tool-result tokens before → after | saved | share of session cost (api_5m_haiku) | share of session cost (sub_1h_sonnet) | fidelity retained (paired n) | verdict |
|---|---|---|---|---|---|---|---|---|
| log_filter | no | 65 | 272,833 → 21,565 | 251,268 | 14.54% | 13.78% | – | off: untested fidelity (no probe results) |
| image | no | 15 | 86,551 → 48,476 | 38,075 | 1.79% | 1.76% | – | off: cannot be delivered live: rewriting a block the model already saw breaks preserved-thinking binding, and new blocks must stay text (recent turns) |
| alias | yes | 294 | 140,740 → 121,553 | 19,187 | 0.63% | 0.66% | – | off: live delivery needs exact replay of model-written aliases; experimental, off by default |
| linenum_sparse | no | 3 | 13,978 → 12,772 | 1,206 | 0.08% | 0.07% | – | off: savings too small (0.078% of session cost < 0.10%) |
| linenum_compact | yes | 3 | 18,909 → 17,996 | 913 | 0.06% | 0.06% | – | off: savings too small (0.061% of session cost < 0.10%) |
| whitespace | yes | 58 | 85,539 → 84,558 | 981 | 0.04% | 0.04% | – | off: savings too small (0.040% of session cost < 0.10%) |
| dedup | yes | 1 | 194 → 60 | 134 | 0.01% | 0.01% | – | off: savings too small (0.005% of session cost < 0.10%) |
| json_minify | yes | 1 | 206 → 176 | 30 | 0.00% | 0.00% | – | off: savings too small (0.001% of session cost < 0.10%) |
| csv_table | no | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
| diff_reread | yes | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
| skeleton | no | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
| toon | yes | 0 | 0 → 0 | 0 | 0.00% | 0.00% | – | off: no savings on the corpus (never applied) |
