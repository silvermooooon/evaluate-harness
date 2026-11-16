"""Run the pinned OpenCode CLI on one benchmark task through the budget proxy."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


VERSION = "2.2.7"


def main() -> int:
    args = arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    instruction = args.instruction_file.read_text(encoding="utf-8")
    config = configuration(args)
    config_path = args.output_dir / "opencode.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    env = environment(args, config_path)
    command = [
        str(args.binary), "run", "--standalone", "--auto", "--format", "json",
        "--agent", "build", "--model", f"tac/{args.model}",
        "--title", "TheAgentCompany evaluation", "--log-level", "error",
    ]
    metadata = {
        "harness": "opencode", "version": VERSION, "model": args.model,
        "provider": "tac", "api": "openai-responses", "base_url": args.base_url,
        "reasoning_effort": "medium", "max_output_tokens": 8192,
        "max_turns": args.max_turns, "workspace": str(args.workspace),
        "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
        "command": command,
        "notes": "Native build system prompt; GPT models use patch instead of edit/write.",
    }
    (args.output_dir / "config.json").write_text(json.dumps(metadata, indent=2) + "\n")
    if args.prepare_only:
        print(json.dumps(metadata))
        return 0
    started = time.monotonic()
    with (args.output_dir / "events.jsonl").open("x", encoding="utf-8") as events:
        with (args.output_dir / "stderr.log").open("x", encoding="utf-8") as stderr:
            result = subprocess.run(
                command, input=instruction, text=True, stdout=events, stderr=stderr,
                cwd=args.workspace, env=env,
            )
    summary = summarize(args.output_dir, result.returncode, time.monotonic() - started)
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return result.returncode


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--instruction-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    for name in ("workspace", "instruction_file", "output_dir"):
        setattr(args, name, getattr(args, name).resolve())
    if not args.base_url:
        parser.error("--base-url or OPENAI_BASE_URL must point to the local budget proxy")
    if args.max_turns < 1:
        parser.error("--max-turns must be positive")
    if args.binary is None:
        system = platform.system().lower()
        arch = {"aarch64": "arm64", "x86_64": "x64"}.get(platform.machine(), platform.machine())
        args.binary = Path(__file__).resolve().parents[1] / ".tools" / "opencode" / f"v{VERSION}" / f"{system}-{arch}" / "opencode"
    args.binary = args.binary.resolve()
    if not args.binary.is_file():
        parser.error(f"OpenCode binary not found: {args.binary}")
    if not args.workspace.is_dir() or not args.instruction_file.is_file():
        parser.error("workspace and instruction-file must exist")
    return args


def configuration(args: argparse.Namespace) -> dict:
    permissions = [{"action": "*", "resource": "*", "effect": "deny"}]
    permissions.extend(
        {"action": action, "resource": "*", "effect": "allow"}
        for action in ("read", "edit", "write", "glob", "grep", "shell", "external_directory")
    )
    return {
        "$schema": "https://opencode.ai/v2/config.json",
        "model": f"tac/{args.model}", "default_agent": "build",
        "update": "disable", "share": "disabled", "snapshots": False,
        "warming": False, "websearch": False, "formatter": False, "lsp": False,
        "permissions": permissions,
        "policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": "tac", "effect": "allow"},
            {"action": "mcp.use", "resource": "*", "effect": "deny"},
            {"action": "skill.use", "resource": "*", "effect": "deny"},
            {"action": "websearch.use", "resource": "*", "effect": "deny"},
        ],
        "agents": {"build": {"steps": args.max_turns}},
        "providers": {
            "tac": {
                "name": "TheAgentCompany budget proxy", "canonical": "openai",
                "package": "@opencode/ai/providers/openai", "env": ["OPENAI_API_KEY"],
                "settings": {"baseURL": args.base_url},
                "models": {
                    args.model: {
                        "modelID": args.model,
                        "settings": {"reasoningEffort": "medium", "store": False},
                        "body": {"max_output_tokens": 8192, "store": False},
                        "capabilities": {
                            "tools": True, "input": ["text"], "output": ["text"],
                            "reasoning": {"supported": True, "effort": ["medium"]},
                        },
                    }
                },
            }
        },
    }


def environment(args: argparse.Namespace, config_path: Path) -> dict:
    env = os.environ.copy()
    # SDK dependencies must not change Python packages available to task shell commands.
    env.pop("PYTHONPATH", None)
    state = args.output_dir / "state"
    for name in ("CONFIG", "DATA", "CACHE", "STATE"):
        env[f"XDG_{name}_HOME"] = str(state / name.lower())
    env.update({
        "OPENCODE_CONFIG_DIR": str(state / "config" / "opencode"),
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_FILEWATCHER_DISABLE": "1",
        "PWD": str(args.workspace),
    })
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env.pop("OPENCODE_CLI_CONFIG_CONTENT", None)
    return env


def summarize(directory: Path, returncode: int, elapsed: float) -> dict:
    counts = Counter()
    tools = Counter()
    steps = []
    texts = []
    errors = []
    for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            counts["non_json_lines"] += 1
            continue
        kind = event.get("type", "unknown")
        counts[kind] += 1
        part = event.get("part", {})
        if kind == "step_finish":
            steps.append(part)
        elif kind == "tool_use":
            tools[part.get("tool", "unknown")] += 1
            if part.get("state", {}).get("status") == "error":
                counts["tool_errors"] += 1
        elif kind == "text":
            texts.append(part.get("text", ""))
        elif kind == "error":
            errors.append(event)
    (directory / "final.txt").write_text("\n".join(texts), encoding="utf-8")
    return {
        "status": "completed" if returncode == 0 and not errors else "agent_error",
        "exit_code": returncode, "elapsed_seconds": elapsed,
        "turns": counts["step_finish"], "tool_calls": sum(tools.values()),
        "tool_errors": counts["tool_errors"], "tools": dict(tools),
        "event_counts": dict(counts), "steps": steps, "errors": errors,
        "usage_source": "step_finish tokens are diagnostic; budget-proxy ledger is authoritative",
    }


if __name__ == "__main__":
    sys.exit(main())
