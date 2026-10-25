# thrift (Claude Code plugin)

thrift builds a local search index of your project (BM25 over tree-sitter
symbol chunks, stored in `.thrift/index.db`) so Claude can find the right code
without reading whole files. It adds three MCP tools (`search_code`,
`get_symbol`, `related_files`), a prompt hook that injects a short list of
likely-relevant file and line pointers, and the `/thrift:ctx-index` command.
Everything runs locally; it needs `uv` to install its Python dependencies on
first use.

Install from a clone of the repository:

```
/plugin marketplace add /path/to/thrift
/plugin install thrift@thrift-local
```

See the repository README for usage, configuration and the benchmark.
