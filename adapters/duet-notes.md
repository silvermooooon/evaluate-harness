# Duet Agent Runtime benchmark adapter

`duet_agent.py` imports the actual Python `agent_runtime` package selected by
`PYTHONPATH`. The runner must point it to the frozen SDK snapshot, not the live
Duet product checkout. No product worker, PostgreSQL state, product platform tools,
subagent manager, or user conversations are loaded.

The adapter uses the SDK's `Agent`, `create_coding_tools`, in-memory `LocalSession`,
and native `CompactionSettings`. It does not implement a replacement agent loop.
The enabled tools are `read`, `bash`, `edit`, and `write`. SDK tool scheduling is
left at its native default (`parallel`). Compaction uses the native 16,384-token
reserve and 20,000 recent-token retention settings, as used by Duet. The system
prompt and effective settings are saved in each run's `config.json`.

## Invocation

Inject `OPENAI_API_KEY` and `OPENAI_BASE_URL` into the process. Do not write them
to a task file. For the benchmark, use the budget proxy's per-run URL and a dummy credential;
the real provider credential should remain in the host proxy process. The SDK
posts to `${OPENAI_BASE_URL}/responses` and does not search for `.env` files.

```sh
PYTHONPATH=/opt/agent-runtime/src python /opt/adapters/duet_agent.py \
  --workspace /workspace \
  --instruction-file /instruction/task.md \
  --output-dir /outputs/duet-run-001 \
  --model gpt-6-luna \
  --max-turns 60
```

The output directory must be new for this attempt. Existing event files are not
overwritten. The model is fixed to OpenAI Responses with `medium` reasoning and
8,192 maximum output tokens per request. The SDK's native `finish_turn` hook stops
after the configured number of main-loop turns; the host proxy separately limits
all model requests, including compaction requests. There is no automatic retry.

The shell tool receives only `PATH`, workspace `HOME`, and `LANG`, following the
SDK's explicit tool-environment mechanism. Model credentials are not passed to
shell subprocesses. The injected API credential is also redacted when serializing
adapter evidence. Run this adapter inside the isolated benchmark container: the
SDK's coding tools themselves are not a filesystem sandbox.

## Evidence

| File | Contents |
| --- | --- |
| `config.json` | Model, endpoint, settings, tools, SDK module path, instruction hash |
| `events.jsonl` | Complete SDK events, including streamed updates and tool results |
| `provider.jsonl` | Prepared request bodies and terminal provider responses; no authorization headers |
| `trajectory.json` | All completed messages from the event stream, including history later compacted out of context |
| `session-journal.json` | Native SDK logical journal, exported after the attempt |
| `final-response.json`, `final-response.txt` | Last assistant message and its text |
| `summary.json` | Execution status, requests, turns, tool calls, duration, token usage coverage |
| `tool-output/` | Native SDK spool files for tool output exceeding the inline limit |

`completed` means that the agent stopped normally, not that the benchmark task
passed. Official task evaluation is a separate step. Exit codes are 0 for normal
completion, 2 for the turn limit, 130 for interruption, and 1 for an agent error.
The runner should still grade artifacts after a limit or agent error when the
task environment remains valid.

Usage totals come from raw Responses terminal events, including compaction calls.
`input_tokens` includes cached input; `cached_input_tokens` is a subset, not an
additional quantity. Reasoning tokens are a subset of output tokens. Missing
usage is represented by coverage, not estimated. `model_requests` counts prepared
SDK requests; the budget proxy's transaction ledger is authoritative for requests
forwarded upstream and their cost. No dollar prices are embedded in this adapter.

## Verification

The adapter passed Python compilation and three local HTTP/SSE fixture checks
using the real SDK and real file-write tool: normal two-request completion, a
one-turn stop after tool execution, and a provider-error response. These checks
verified model settings, request/token accounting, output artifacts, exit codes,
and credential redaction. They made no external model call and are not benchmark
performance evidence.
