"""Run the frozen Duet Agent Runtime SDK on one benchmark task."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

import agent_runtime
from agent_runtime import Agent, CompactionSettings, LocalSession, Models, RuntimeConfig
from agent_runtime.tools import create_coding_tools


async def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(args.output_dir, os.environ["OPENAI_API_KEY"], started)
    config = RuntimeConfig.from_env(
        {name: os.environ[name] for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL") if name in os.environ}
    )
    compaction = CompactionSettings()
    session = LocalSession(directory=None)
    turns = 0
    stop_reason = None

    def finish_turn(turn, abort_signal):
        nonlocal turns, stop_reason
        turns += 1
        if turns >= args.max_turns:
            if any(block.get("type") == "toolCall" for block in turn["message"]["content"]):
                stop_reason = "max_turns"
            return {"action": "end"}
        return None

    agent = Agent(
        model=args.model,
        provider="openai",
        api="openai-responses",
        models=Models(config=config),
        session=session,
        thinking_level="medium",
        parameters={
            "max_tokens": 8192,
            "on_payload": recorder.request,
            "on_provider_stream_event": recorder.provider_event,
        },
        system_prompt=(
            "You are an autonomous agent completing a workplace task. "
            "Read relevant files before editing and verify the requested result. "
            "Work in the supplied environment and complete the task without human help. "
            "Do not inspect benchmark evaluation code, scoring rubrics, or reference answers."
        ),
        tools=create_coding_tools(
            args.workspace,
            bash_options={
                "env": {
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": str(args.workspace),
                    "LANG": "C.UTF-8",
                },
                "output_dir": args.output_dir / "tool-output",
            },
        ),
        compaction=compaction,
        finish_turn=finish_turn,
    )
    agent.subscribe(recorder.event)
    instruction = args.instruction_file.read_text(encoding="utf-8")
    recorder.save(
        "config.json",
        {
            "harness": "duet-agent-runtime",
            "sdk_module_path": agent_runtime.__file__,
            "model": args.model,
            "provider": "openai",
            "api": "openai-responses",
            "base_url": agent.state.model.base_url,
            "reasoning_effort": "medium",
            "max_output_tokens": 8192,
            "max_turns": args.max_turns,
            "compaction": asdict(compaction),
            "tools": [tool.name for tool in agent.state.tools],
            "tool_execution": agent.tool_execution,
            "system_prompt": agent.state.system_prompt,
            "workspace": str(args.workspace),
            "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
        },
    )

    def stop():
        nonlocal stop_reason
        stop_reason = "interrupted"
        agent.abort()

    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop)
    error = None
    try:
        await agent.prompt(instruction)
        error = agent.state.error_message
    except Exception as caught:
        error = f"{type(caught).__name__}: {caught}"
    finally:
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(name)
        status = stop_reason or ("agent_error" if error else "completed")
        summary = recorder.finish(agent, session, status, turns, error)
        recorder.close()
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return {"completed": 0, "max_turns": 2, "interrupted": 130}.get(status, 1)


class Recorder:
    def __init__(self, directory: Path, credential: str, started: float):
        self.directory = directory
        self.credential = credential
        self.started = started
        self.events = (directory / "events.jsonl").open("x", encoding="utf-8")
        self.provider_events = (directory / "provider.jsonl").open("x", encoding="utf-8")
        self.messages = []
        self.requests = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.compactions = 0
        self.usage_by_response = {}

    def encode(self, value) -> str:
        # Provider errors and tool output are archived too; remove the injected credential.
        return json.dumps(value, ensure_ascii=False).replace(self.credential, "[REDACTED]")

    def append(self, file, value):
        file.write(self.encode({"elapsed_seconds": time.monotonic() - self.started, **value}) + "\n")
        file.flush()

    def save(self, name, value):
        (self.directory / name).write_text(self.encode(value) + "\n", encoding="utf-8")

    def request(self, payload, model):
        self.requests += 1
        self.append(
            self.provider_events,
            {"type": "request", "request_number": self.requests, "model": model.id, "payload": payload},
        )

    def provider_event(self, event, model):
        if event["type"] not in ("response.completed", "response.incomplete", "response.failed", "error"):
            return
        self.append(self.provider_events, {"type": "response", "request_number": self.requests, "event": event})
        response = event.get("response", {})
        if response.get("usage"):
            self.usage_by_response[response["id"]] = response["usage"]

    def event(self, event, abort_signal):
        self.append(self.events, event)
        if event["type"] == "message_end":
            self.messages.append(event["message"])
        elif event["type"] == "tool_execution_start":
            self.tool_calls += 1
        elif event["type"] == "tool_execution_end" and event.get("isError"):
            self.tool_errors += 1
        elif event["type"] == "compaction_end" and not event.get("errorMessage"):
            self.compactions += 1

    def finish(self, agent, session, status, turns, error):
        usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }
        for item in self.usage_by_response.values():
            usage["input_tokens"] += item.get("input_tokens", 0)
            usage["cached_input_tokens"] += (item.get("input_tokens_details") or {}).get("cached_tokens", 0)
            usage["output_tokens"] += item.get("output_tokens", 0)
            usage["reasoning_output_tokens"] += (item.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
            usage["total_tokens"] += item.get("total_tokens", 0)
        last = next((message for message in reversed(self.messages) if message["role"] == "assistant"), None)
        summary = {
            "status": status,
            "model": agent.state.model.id,
            "turns": turns,
            "model_requests": self.requests,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "compactions": self.compactions,
            "wall_seconds": time.monotonic() - self.started,
            "usage": usage,
            "requests_with_usage": len(self.usage_by_response),
            "usage_coverage": len(self.usage_by_response) / self.requests if self.requests else None,
            "error": error,
        }
        summary = json.loads(self.encode(summary))
        self.save("summary.json", summary)
        self.save("trajectory.json", self.messages)
        self.save("session-journal.json", session.read_records())
        self.save("final-response.json", last)
        text = "\n".join(block["text"] for block in (last or {}).get("content", []) if block.get("type") == "text")
        (self.directory / "final-response.txt").write_text(text.replace(self.credential, "[REDACTED]"), encoding="utf-8")
        return summary

    def close(self):
        self.events.close()
        self.provider_events.close()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--instruction-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--max-turns", type=int, default=60)
    args = parser.parse_args()
    if not args.workspace.is_dir():
        parser.error("--workspace must be an existing directory")
    if not args.instruction_file.is_file():
        parser.error("--instruction-file must be an existing file")
    if args.max_turns < 1:
        parser.error("--max-turns must be positive")
    if not os.environ.get("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be injected by the runner")
    for name in ("workspace", "instruction_file", "output_dir"):
        setattr(args, name, getattr(args, name).resolve())
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(run(arguments())))
