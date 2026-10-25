---
name: ctx-index
description: Build or refresh the thrift code index for this project (local search used by the thrift MCP tools and prompt hook). Pass --full to rebuild from scratch.
argument-hint: "[--full]"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/thriftctx.sh" *)
---

Indexing result:

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/thriftctx.sh" index "${CLAUDE_PROJECT_DIR}" $ARGUMENTS 2>&1 | tail -3`

Report the result above to the user in one short sentence. Do not run any other commands.
