# OpenCode adapter

Pinned upstream release: [v2.2.7](https://github.com/anomalyco/opencode/releases/tag/v2.2.7), published 2026-11-10. The GitHub `/releases/latest` endpoint still returned v1.19.5 during setup; v1 and v2 configuration formats must not be mixed.

Official release archives and extracted executables live under `.tools/opencode/v2.2.7/`. `manifest.json` records download URLs, byte sizes, and locally computed SHA-256 hashes. This is a project-local install; no global OpenCode installation or user configuration was changed.

## Invocation

In an isolated benchmark task container, pass a dummy proxy credential as `OPENAI_API_KEY` and the budget proxy URL as `OPENAI_BASE_URL`. Keep the actual provider credential exclusively in the host proxy.

```sh
python adapters/opencode_agent.py \
  --binary /opt/harness/opencode \
  --workspace /workspace \
  --instruction-file /opt/harness/instruction.txt \
  --output-dir /opt/results/opencode \
  --model gpt-6-luna \
  --max-turns 60
```

The wrapper invokes `opencode run --standalone --auto --format json --agent build --model tac/gpt-6-luna`, with the task on standard input. Each run gets separate XDG configuration/data/cache/state directories. Project configuration, Claude skills, model catalog downloads, file watching, warming, sharing, snapshots, LSP, and formatting are disabled. The CLI environment removes `PYTHONPATH`, so task shell Python uses the original container packages instead of inheriting the Duet SDK dependency directory. `--prepare-only` writes configuration without starting OpenCode.

The custom `tac` provider uses `@opencode/ai/providers/openai`, whose default route is OpenAI Responses. It uses the supplied proxy URL, `reasoningEffort: medium`, `store: false`, and `max_output_tokens: 8192`. OpenCode retains its native `build` system prompt. Permissions allow read, edit/write, glob, grep, and shell; native GPT handling replaces edit/write with patch. The actual tool schema observed by the budget proxy contains `glob`, `grep`, `opencode_worktree_remove`, `patch`, `read`, `shell`, and `skill`. A deny policy does not necessarily hide a tool's schema: in particular, `skill` remains advertised even though `skill.use` is denied. Tool exposure must therefore be reported from captured requests, rather than inferred from configuration. Shell networking is not separately sandboxed by this wrapper; use the same container/network policy for both harnesses.

## Artifacts and metrics

- `config.json`: run metadata and command, without credentials.
- `opencode.json`: the effective adapter configuration, without credentials.
- `events.jsonl`: unmodified native JSON output, including `step_start`, `step_finish`, `tool_use`, `text`, `reasoning`, `retry`, and `error` records when emitted.
- `stderr.log`: CLI diagnostics.
- `summary.json`: observed model steps, tool calls/errors, elapsed time, and raw step records.
- `final.txt`: emitted assistant text. A clean CLI exit means the agent stopped without a CLI error; it does not mean the benchmark task passed.

Native usage lives in `step_finish.part.tokens`: `input`, `output`, `reasoning`, `cache.read`, `cache.write`; native cost is `step_finish.part.cost`. `tool_use.part` includes `tool`, `state.status`, inputs, outputs/errors, and timing. `retry` includes attempt/next retry/error. Use the proxy's provider usage ledger for authoritative billing and aggregate request counts, because native step output may omit internal auxiliary requests and native cost depends on its model catalog. The proxy must reject any model other than the selected model and enforce the shared budget.

## Validation performed

The macOS arm64 binary returned `opencode v2.2.7`; `run --help` matched the wrapper flags. Python compilation passed. `debug config --standalone` successfully parsed every generated configuration field with a fake credential and an unreachable local model endpoint. No real provider/model request was made during those configuration checks. The Linux x64 binary subsequently ran inside official task containers during the [recorded local experiment](../docs/experiment-2026-11.md). Detailed setup fixtures remain in ignored local artifacts and are not distributed with this repository.

After removing `PYTHONPATH`, a fresh offline check injected a dependency-directory marker into the adapter's parent environment, called the real `environment()` implementation, and launched child Python. The child had neither `PYTHONPATH` nor the marker in `sys.path`; the parent environment remained unchanged. A fresh `debug config --standalone` invocation also exited successfully with the generated configuration and an unreachable local model endpoint. Results are retained in `.tools/opencode/verification/environment-isolation/check.json`; no model API was called by this check.

## Upstream evidence

- [CLI run implementation and JSON event handling](https://github.com/anomalyco/opencode/blob/v2.2.7/packages/cli/src/run/noninteractive.ts)
- [Responses provider routing](https://github.com/anomalyco/opencode/blob/v2.2.7/packages/ai/src/providers/openai.ts)
- [V2 provider configuration](https://github.com/anomalyco/opencode/blob/v2.2.7/packages/schema/src/config/provider.ts)
- [CLI environment/configuration controls](https://github.com/anomalyco/opencode/blob/v2.2.7/packages/cli/src/server-process.ts)
- [Whole-tool permission filtering](https://github.com/anomalyco/opencode/blob/v2.2.7/packages/core/src/tool.ts)
- [GPT patch tool selection](https://github.com/anomalyco/opencode/blob/v2.2.7/packages/core/src/tool/plugin/patch.ts)
- [Token usage schema](https://github.com/anomalyco/opencode/blob/v2.2.7/packages/schema/src/token-usage.ts)
