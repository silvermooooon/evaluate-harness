"""Audit formal-run tool inputs and frozen-input/adapter identity without API calls."""

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {"duet": "adapters/duet_agent.py", "opencode": "adapters/opencode_agent.py"}
PATTERNS = {
    "protected_task_directory": re.compile(
        r"(?<![\w./-])(?:/|(?:\.\./)+)(?:utils|npc)(?=[/\s\"'();,]|$)", re.I
    ),
    "evaluator_or_grading_material": re.compile(
        r"\b(?:evaluator(?:\.py)?|eval\.py|grade\.(?:json|py|log)|grading|"
        r"scoring[ _-]+rubrics?|grading[ _-]+rubrics?)\b", re.I
    ),
    "reference_answer_material": re.compile(
        r"\b(?:reference[ _-]+answers?|ground[ _-]*truth|(?:gold|golden)[ _-]+answers?)\b"
        r"|参考答案|评分标准|评分规则", re.I
    ),
    "harness_scoring_artifact": re.compile(
        r"/harness-output/(?:[^\s\"'`;]*?/)?"
        r"(?:grade\.(?:json|log)|result\.json|evaluation-trajectory\.json)\b", re.I
    ),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="formal")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/integrity-audit.json")
    args = parser.parse_args()
    manifest_path = ROOT / "reports/experiment-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    adapters = {}
    for harness, relative in ADAPTERS.items():
        current = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        expected = manifest["hashes"][relative]
        adapters[harness] = {
            "path": relative, "manifest_sha256": expected, "current_sha256": current,
            "current_matches_manifest": current == expected,
        }
    runs, pending = [], []
    for directory in sorted((ROOT / "runs").glob(f"{args.label}-*")):
        path = directory / "result.json"
        if not path.is_file():
            pending.append({"run_id": directory.name, "reason": "result.json not present"})
            continue
        result = json.loads(path.read_text())
        if not result.get("finished_unix"):
            pending.append({"run_id": directory.name, "reason": "run not finished"})
            continue
        runs.append(audit_run(directory, result, adapters))
    comparisons = compare_inputs(runs)
    suspicious = sum(len(run["suspicious_tool_inputs"]) for run in runs)
    problems = sum(len(run["integrity_issues"]) for run in runs)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "label": args.label,
        "manifest": str(manifest_path.relative_to(ROOT)),
        "scope": {
            "duet_input_source": "tool_execution_start.args",
            "opencode_input_source": "tool_use.part.state.input",
            "excluded": "Initial prompts, model prose, tool outputs, runner initialization and grader logs are not scanned as agent actions.",
            "patterns": {name: pattern.pattern for name, pattern in PATTERNS.items()},
        },
        "limitations": [
            "Literal references in recorded tool arguments are review candidates, not proof that access succeeded or that an answer was used.",
            "No match means no listed literal indicator was found, not a guarantee that protected material was never accessed.",
            "This scan does not execute commands or resolve constructed/encoded paths, symlinks, imported scripts or network redirects.",
            "Actions absent from emitted events, including interrupted tool calls whose inputs were not emitted, cannot be recovered by this scan.",
            "Original-task hashes are compared as recorded by the runner; actual saved instruction bytes are independently hashed.",
            "Only finished runs present at audit time are included; rerun after all planned trials finish.",
        ],
        "adapters": adapters,
        "runs": runs,
        "input_comparisons": comparisons,
        "pending_runs": pending,
        "summary": {
            "completed_runs_audited": len(runs),
            "tool_input_event_records": sum(run["tool_input_event_records"] for run in runs),
            "suspicious_tool_input_event_records": suspicious,
            "run_integrity_issues": problems,
            "adapter_manifest_mismatches": sum(not item["current_matches_manifest"] for item in adapters.values()),
            "task_input_mismatches": sum(not item["all_saved_and_recorded_inputs_match"] for item in comparisons),
            "incomplete_repeat_pairs": sum(len(item["incomplete_repeats"]) for item in comparisons),
            "pending_runs": len(pending),
            "review_status": "review_candidates_present" if suspicious else "no_listed_literal_indicator_found",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=False))


def audit_run(directory, result, adapters):
    harness = result.get("harness")
    if harness not in ADAPTERS or not result.get("task") or not isinstance(result.get("repeat"), int):
        raise ValueError(f"Invalid run identity: {directory.name}")
    issues = []
    instruction = directory / "instruction.txt"
    actual_hash = hashlib.sha256(instruction.read_bytes()).hexdigest() if instruction.is_file() else None
    if actual_hash is None or actual_hash != result.get("input_sha256"):
        issues.append("saved_instruction_missing_or_hash_mismatch")
    expected_adapter = adapters[harness]["manifest_sha256"]
    if result.get("adapter_sha256") != expected_adapter:
        issues.append("recorded_adapter_does_not_match_manifest")
    if not result.get("task_instruction_sha256"):
        issues.append("missing_recorded_original_task_hash")
    events = directory / "agent/events.jsonl"
    count, suspicious, trace_issues = scan_tool_inputs(events, harness)
    issues.extend(trace_issues)
    return {
        "run_id": result["run_id"], "task": result["task"], "harness": harness,
        "repeat": result["repeat"], "run_status": result["status"],
        "events_path": str(events.relative_to(ROOT)),
        "recorded_task_instruction_sha256": result.get("task_instruction_sha256"),
        "recorded_input_sha256": result.get("input_sha256"),
        "saved_instruction_sha256": actual_hash,
        "saved_instruction_matches_recorded": actual_hash is not None and actual_hash == result.get("input_sha256"),
        "recorded_adapter_sha256": result.get("adapter_sha256"),
        "adapter_matches_manifest": result.get("adapter_sha256") == expected_adapter,
        "tool_input_event_records": count,
        "suspicious_tool_inputs": suspicious,
        "integrity_issues": issues,
    }


def scan_tool_inputs(path, harness):
    if not path.is_file():
        return 0, [], ["missing_tool_event_trace"]
    count, suspicious, issues = 0, [], []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            issues.append(f"malformed_event_json_at_line_{number}")
            continue
        if harness == "duet" and event.get("type") == "tool_execution_start":
            arguments = event.get("args")
            name, call_id, status = event.get("toolName"), event.get("toolCallId"), "started"
        elif harness == "opencode" and event.get("type") == "tool_use":
            part = event.get("part", {})
            state = part.get("state", {})
            arguments = state.get("input")
            name, call_id, status = part.get("tool"), part.get("id"), state.get("status")
        else:
            continue
        if not isinstance(arguments, dict):
            issues.append(f"missing_or_invalid_tool_arguments_at_line_{number}")
            continue
        count += 1
        matches = []
        for field, value in string_arguments(arguments):
            for category, pattern in PATTERNS.items():
                for match in pattern.finditer(value):
                    matches.append({
                        "category": category, "argument_field": field,
                        "literal": match.group(),
                        "context": value[max(0, match.start() - 80):match.end() + 100],
                    })
        if matches:
            suspicious.append({
                "event_line": number, "event_type": event["type"],
                "tool_name": name, "call_id": call_id, "recorded_state": status,
                "arguments": arguments, "matches": matches,
                "assessment": "manual_review_required; access_success_not_inferred",
            })
    return count, suspicious, issues


def string_arguments(value, field="input"):
    if isinstance(value, str):
        yield field, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from string_arguments(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from string_arguments(child, f"{field}[{index}]")


def compare_inputs(runs):
    tasks = defaultdict(list)
    for run in runs:
        tasks[run["task"]].append(run)
    comparisons = []
    for task, members in sorted(tasks.items()):
        repeats = defaultdict(list)
        for run in members:
            repeats[run["repeat"]].append(run)
        original = {run["recorded_task_instruction_sha256"] for run in members}
        recorded = {run["recorded_input_sha256"] for run in members}
        actual = {run["saved_instruction_sha256"] for run in members}
        equal = None not in original | recorded | actual and len(original) == len(recorded) == len(actual) == 1 and recorded == actual
        comparisons.append({
            "task": task,
            "runs": [run["run_id"] for run in members],
            "recorded_original_task_hashes": sorted(original, key=str),
            "recorded_input_hashes": sorted(recorded, key=str),
            "actual_instruction_hashes": sorted(actual, key=str),
            "all_saved_and_recorded_inputs_match": equal,
            "complete_repeats": [repeat for repeat, pair in sorted(repeats.items()) if {run["harness"] for run in pair} == set(ADAPTERS) and len(pair) == 2],
            "incomplete_repeats": [repeat for repeat, pair in sorted(repeats.items()) if {run["harness"] for run in pair} != set(ADAPTERS) or len(pair) != 2],
        })
    return comparisons


if __name__ == "__main__":
    main()
