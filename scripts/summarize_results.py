"""Summarize captured TAC runs and proxy usage without making API requests."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics

from pricing import PRICING, estimate_cost


ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("duet", "opencode")
PARTITION_GROUPS = ("primary", "pilot", "smoke", "setup", "other")
PROXY_ADDITIVE = (
    "api_requests", "successful_responses", "requests_with_usage", "requests_with_estimated_cost", "requests_with_proxy_estimated_cost",
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "cache_write_30m_input_tokens", "cache_write_12h_input_tokens", "output_tokens", "reasoning_output_tokens",
    "total_tokens", "estimated_cost_usd", "proxy_estimated_cost_usd", "budget_charge_usd", "api_error_requests",
    "http_error_requests", "transport_error_requests", "response_error_requests",
    "client_disconnected_requests",
)
EFFICIENCY = (
    "api_requests", "successful_responses", "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "estimated_cost_usd",
    "budget_charge_usd", "agent_wall_seconds",
)
NATIVE = ("native_turns", "native_tool_calls", "native_tool_errors", "native_compactions")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest, results, ledger, warnings = read_inputs(root)
    expected = expected_runs(manifest)
    comparison = read_comparison(root, expected, results)
    comparison_slots = {slot["run_id"]: slot for slot in comparison["slots"]} if comparison else {}
    records = defaultdict(list)
    for entry in ledger:
        records[entry["run_id"]].append(entry)
    rows = []
    for run_id in sorted(set(results) | set(records)):
        result = results.get(run_id, {})
        summary_path = root / "runs" / run_id / "agent" / "summary.json"
        native = read_json(summary_path, warnings) if summary_path.exists() else {}
        row = run_row(run_id, result, native, records[run_id], manifest, expected)
        slot = comparison_slots.get(run_id)
        row.update(included_in_comparison=slot is not None,
                   comparison_logical_run_id=slot["logical_run_id"] if slot else None,
                   comparison_original_run_id=slot["original_run_id"] if slot else None,
                   comparison_replacement=slot["replacement"] if slot else None)
        rows.append(row)
    groups = {}
    for group in PARTITION_GROUPS:
        selected = [row for row in rows if row["group"] == group]
        groups[group] = group_summary(selected, expected if group == "primary" else None)
    if comparison:
        groups["comparison"] = group_summary(
            [row for row in rows if row["included_in_comparison"]], comparison_slots)
        groups["comparison"]["selection"] = comparison
        groups["comparison"]["overlapping_derived_group"] = True
    ledger_totals = usage_totals(ledger)
    conservation = ledger_conservation(groups, ledger_totals)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark": manifest["benchmark"],
        "formal_run_label": manifest["formal_run_label"],
        "selection": manifest["selection"],
        "primary_complete": groups["primary"]["completeness"]["complete"],
        "comparison_complete": groups["comparison"]["completeness"]["complete"] if comparison else None,
        "pricing": {**PRICING,
                    "observed_max_input_tokens": max((entry.get("usage", {}).get("input_tokens", 0) for entry in ledger), default=0),
                    "observed_long_context_requests": sum(entry.get("usage", {}).get("input_tokens", 0) > 272000 for entry in ledger),
                    "observed_12h_write_tokens": ledger_totals["cache_write_12h_input_tokens"],
                    "requests_where_budget_charge_below_recomputed_cost": sum(
                        entry["report_estimated_cost_usd"] is not None and
                        entry["budget_charge_usd"] < entry["report_estimated_cost_usd"] for entry in ledger)},
        "definitions": {
            "primary": "Only exact predeclared formal task/harness/repeat run IDs; pilots and smoke are separate.",
            "comparison": "Overlapping derived group selected exclusively by reports/comparison-selection.json. Missing recovery runs stay missing; originals are never used as a fallback or selected by score. Both harnesses of affected task/repeat pairs are replaced together. Raw primary remains unchanged.",
            "ledger_partition": "Only primary, pilot, smoke, setup, and other are mutually exclusive. Comparison reuses selected rows and is excluded from ledger conservation and overall spend totals.",
            "finished": "The runner wrote finished_unix, including failed or timed-out runs.",
            "graded": "Successful grader exit, positive possible points, and recorded official score.",
            "official_score": "Recorded upstream TAC score, including its completion bonus; no custom score is introduced.",
            "points_fraction": "Official final_score.result / final_score.total. final_score already includes any task-strategy bonus_for_completing_final; this ratio precedes the normalized score's additional 50% full-completion term. It is not a raw checkpoint sum.",
            "execution_status": "Combines runner state, agent exit code, and native adapter status. A runner status of agent_finished alone does not imply successful agent execution or task completion.",
            "proxy_requests": "Upstream request attempts recorded when finished, including connection failures before a model response and auxiliary requests. api_requests is this attempt count, not a count of successful model turns.",
            "successful_responses": "Ledger attempts with HTTP 200, usage_complete=true, and response_status=completed; includes completed auxiliary model requests.",
            "partial": "In-flight requests and proxy rejections before forwarding are absent from the current ledger snapshot.",
            "tokens": "Known provider usage only. Cache reads (cached_input_tokens) and cache writes are disjoint subsets of input; the 30m and 12h write components sum to cache_write_input_tokens. Fresh input = input - read - write and is validated nonnegative. Reasoning is a subset of output. Total is input + output.",
            "usage_coverage": "Requests with complete usage / ledger requests; null when no requests exist.",
            "estimated_cost_usd": "Recomputed from original usage using pricing.sources, including separate cache read/write rates and request-level long-context multipliers. Coverage counts successfully repriced requests, not historical proxy cost fields. With coverage below 1, this is only the known portion and must not be presented as complete spend; retained budget reservations cover missing usage.",
            "proxy_estimated_cost_usd": "Unmodified sum of the ledger's historical estimated_cost_usd, recorded under the proxy implementation active for that request. The original proxy used in this suite priced cache writes as ordinary fresh input. proxy_estimated_cost_coverage has its own numerator. The ledger itself is never rewritten.",
            "budget_charge_usd": "Proxy's conservative budget accounting, including reservations retained for requests without usage.",
            "api_error_requests": "Union of HTTP errors, transport errors, explicit API errors, and failed/cancelled/incomplete response status; category counts may overlap.",
            "efficiency_finished": "Statistics across finished runs, including task failures; partial runs are excluded.",
            "paired_efficiency": "Computed only for task/repeat pairs where both harnesses achieved official full completion. Token/cost differences and ratios require complete corresponding usage/cost coverage on both sides.",
            "native_diagnostics": "Harness-reported status, errors, turns, tool calls/errors, and compactions; definitions differ and are not normalized comparison metrics. Uncollected compactions remain null.",
            "pair_direction": "All differences are duet minus opencode; ratios are duet / opencode.",
            "statistics": "Descriptive mean, median, and total only; no statistical-significance claim is made.",
        },
        "groups": groups,
        "ledger": {"path": "runs/api-usage.jsonl", "totals": ledger_totals,
                   "partition_groups": PARTITION_GROUPS,
                   "run_ids_without_result": sorted(set(records) - set(results)),
                   "conservation": conservation},
        "warnings": warnings,
        "runs": rows,
    }
    destination = root / "reports"
    destination.mkdir(exist_ok=True)
    (destination / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with (destination / "results.csv").open("w", encoding="utf-8", newline="") as output:
        fields = list(rows[0]) if rows else ["run_id", "group"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                          for key, value in row.items()} for row in rows)
    print(json.dumps({"primary": groups["primary"]["completeness"],
                      "comparison": groups["comparison"]["completeness"] if comparison else None,
                      "ledger_requests": ledger_totals["api_requests"],
                      "successful_responses": ledger_totals["successful_responses"],
                      "estimated_cost_usd": ledger_totals["estimated_cost_usd"],
                      "estimated_cost_coverage": ledger_totals["estimated_cost_coverage"],
                      "proxy_estimated_cost_usd": ledger_totals["proxy_estimated_cost_usd"],
                      "budget_charge_usd": ledger_totals["budget_charge_usd"],
                      "ledger_conserved": conservation["passed"],
                      "warnings": warnings}, ensure_ascii=False))


def read_inputs(root):
    warnings = []
    manifest = json.loads((root / "reports/task-selection.json").read_text())
    results = {}
    for path in sorted((root / "runs").glob("*/result.json")):
        value = read_json(path, warnings)
        if value:
            if value["run_id"] != path.parent.name:
                raise ValueError(f"Run ID does not match directory: {path}")
            results[value["run_id"]] = value
    ledger_path = root / "runs/api-usage.jsonl"
    lines = ledger_path.read_text().splitlines() if ledger_path.exists() else []
    ledger = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise
            warnings.append("Skipped an incomplete final ledger line; rerun after the request completes.")
            continue
        if not isinstance(entry.get("run_id"), str) or "budget_charge_usd" not in entry:
            raise ValueError(f"Invalid proxy record at line {index + 1}")
        entry["report_estimated_cost_usd"] = estimate_cost(entry)
        ledger.append(entry)
    return manifest, results, ledger, warnings


def read_json(path, warnings):
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        warnings.append(f"Skipped a JSON file being written or invalid: {path}")
        return {}


def read_comparison(root, expected, results):
    path = root / "reports/comparison-selection.json"
    if not path.exists():
        return None
    selection = json.loads(path.read_text())
    slots = selection["slots"]
    logical_ids = [slot["logical_run_id"] for slot in slots]
    if len(slots) != len(expected) or set(logical_ids) != set(expected):
        raise ValueError("Comparison mapping must cover each declared logical run exactly once")
    if len({slot["run_id"] for slot in slots}) != len(slots):
        raise ValueError("Comparison mapping selects a physical run more than once")
    replacements = Counter()
    for slot in slots:
        identity = {key: slot[key] for key in ("task", "harness", "repeat")}
        if identity != expected[slot["logical_run_id"]] or slot["original_run_id"] != slot["logical_run_id"]:
            raise ValueError(f"Comparison slot identity mismatch: {slot['logical_run_id']}")
        if slot["replacement"] != (slot["run_id"] != slot["original_run_id"]):
            raise ValueError(f"Comparison replacement flag mismatch: {slot['logical_run_id']}")
        result = results.get(slot["run_id"])
        if result and any(result[key] != value for key, value in identity.items()):
            raise ValueError(f"Comparison selected run identity mismatch: {slot['run_id']}")
        if slot["replacement"]:
            replacements[(slot["task"], slot["repeat"])] += 1
    if any(count != len(HARNESSES) for count in replacements.values()):
        raise ValueError("Infrastructure recovery must replace both harnesses in an affected pair")
    return selection


def expected_runs(manifest):
    return {
        f"{manifest['formal_run_label']}-{task}-{harness}-r{repeat}":
        {"task": task, "harness": harness, "repeat": repeat}
        for task in manifest["primary_tasks"]
        for harness in HARNESSES
        for repeat in range(1, manifest["planned_repeats_per_harness"] + 1)
    }


def run_row(run_id, result, native, entries, manifest, expected):
    if run_id in expected:
        group = "primary"
    elif run_id in manifest.get("excluded_pilot_runs", []) or result.get("label") == "pilot":
        group = "pilot"
    elif result.get("label") == "smoke":
        group = "smoke"
    elif not result:
        group = "setup"
    else:
        group = "other"
    identity = result or expected.get(run_id, {})
    final_score = result.get("grade", {}).get("final_score", {})
    points, total = final_score.get("result"), final_score.get("total")
    graded = result.get("grader_exit_code") == 0 and bool(total and total > 0) and result.get("official_score") is not None
    return {
        "run_id": run_id, "group": group, "task": identity.get("task"),
        "harness": identity.get("harness"), "repeat": identity.get("repeat"),
        "label": result.get("label"), "observed_result": bool(result),
        "finished": result.get("finished_unix") is not None, "graded": graded,
        "status": result.get("status", "ledger_only"),
        "execution_status": execution_status(result, native),
        "infrastructure_error": result.get("infrastructure_error"),
        "evaluation_error": result.get("evaluation_error"),
        "agent_exit_code": result.get("agent_exit_code"),
        "grader_exit_code": result.get("grader_exit_code"),
        "official_score": result.get("official_score") if graded else None,
        "full_completion": result.get("full_completion") if graded else None,
        "points_earned": points, "points_possible": total,
        "points_fraction": points / total if graded else None,
        "checkpoints": result.get("grade", {}).get("checkpoints"),
        "agent_wall_seconds": result.get("agent_wall_seconds"),
        **usage_totals(entries),
        "native_status": native.get("status"),
        "native_error": native.get("error", native.get("errors")),
        "native_turns": native.get("turns"),
        "native_tool_calls": native.get("tool_calls"),
        "native_tool_errors": native.get("tool_errors"),
        "native_compactions": native.get("compactions"),
    }


def execution_status(result, native):
    if not result:
        return "ledger_only"
    if result.get("infrastructure_error"):
        return "infrastructure_error"
    if result.get("status") == "timeout":
        return "timeout"
    if "agent_exit_code" not in result:
        return result.get("status", "unknown")
    if native.get("status") in ("max_turns", "interrupted"):
        return native["status"]
    if result["agent_exit_code"] != 0 or native.get("status") == "agent_error":
        return "agent_error"
    if native.get("status") == "completed":
        return "completed"
    return "exited_without_completed_summary"


def usage_totals(entries):
    known = [entry for entry in entries if entry.get("usage_complete") and entry.get("usage")]
    costs = [entry["report_estimated_cost_usd"] for entry in entries if entry["report_estimated_cost_usd"] is not None]
    proxy_costs = [entry["estimated_cost_usd"] for entry in entries if entry.get("estimated_cost_usd") is not None]
    http_errors = sum(entry.get("http_status", 0) >= 400 for entry in entries)
    transport_errors = sum(bool(entry.get("transport_error")) for entry in entries)
    response_errors = sum(bool(entry.get("api_error_type") or entry.get("api_error_code")) or
                          entry.get("response_status") in ("failed", "cancelled", "incomplete") for entry in entries)
    errors = sum(entry.get("http_status", 0) >= 400 or bool(entry.get("transport_error")) or
                 bool(entry.get("api_error_type") or entry.get("api_error_code")) or
                 entry.get("response_status") in ("failed", "cancelled", "incomplete") for entry in entries)
    tokens_in = sum(entry["usage"]["input_tokens"] for entry in known)
    tokens_out = sum(entry["usage"]["output_tokens"] for entry in known)
    return {
        "api_requests": len(entries), "requests_with_usage": len(known),
        "successful_responses": sum(entry.get("http_status") == 200 and entry.get("usage_complete") is True and
                                    entry.get("response_status") == "completed" for entry in entries),
        "usage_coverage": len(known) / len(entries) if entries else None,
        "requests_with_estimated_cost": len(costs),
        "estimated_cost_coverage": len(costs) / len(entries) if entries else None,
        "requests_with_proxy_estimated_cost": len(proxy_costs),
        "proxy_estimated_cost_coverage": len(proxy_costs) / len(entries) if entries else None,
        "input_tokens": tokens_in,
        "cached_input_tokens": sum(entry["usage"].get("input_tokens_details", {}).get("cached_tokens", 0) for entry in known),
        "cache_write_input_tokens": sum(entry["usage"].get("input_tokens_details", {}).get("cache_write_tokens", 0) for entry in known),
        "cache_write_30m_input_tokens": sum(entry["usage"].get("input_tokens_details", {}).get("cache_write_tokens_details", {}).get("ttl_30m_tokens", 0) for entry in known),
        "cache_write_12h_input_tokens": sum(entry["usage"].get("input_tokens_details", {}).get("cache_write_tokens_details", {}).get("ttl_12h_tokens", 0) for entry in known),
        "output_tokens": tokens_out,
        "reasoning_output_tokens": sum(entry["usage"].get("output_tokens_details", {}).get("reasoning_tokens", 0) for entry in known),
        "total_tokens": tokens_in + tokens_out,
        "estimated_cost_usd": math.fsum(costs),
        "proxy_estimated_cost_usd": math.fsum(proxy_costs),
        "budget_charge_usd": math.fsum(entry["budget_charge_usd"] for entry in entries),
        "api_error_requests": errors, "http_error_requests": http_errors,
        "transport_error_requests": transport_errors, "response_error_requests": response_errors,
        "client_disconnected_requests": sum(bool(entry.get("client_disconnected")) for entry in entries),
    }


def statistics_for(values):
    values = [value for value in values if value is not None]
    return {"n": len(values), "mean": statistics.mean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "total": math.fsum(values) if values else 0}


def sum_proxy(rows):
    totals = {name: (math.fsum(row[name] for row in rows) if name.endswith("_usd")
                     else sum(row[name] for row in rows)) for name in PROXY_ADDITIVE}
    count = totals["api_requests"]
    totals["usage_coverage"] = totals["requests_with_usage"] / count if count else None
    totals["estimated_cost_coverage"] = totals["requests_with_estimated_cost"] / count if count else None
    totals["proxy_estimated_cost_coverage"] = totals["requests_with_proxy_estimated_cost"] / count if count else None
    return totals


def group_summary(rows, expected):
    finished = [row for row in rows if row["finished"]]
    graded = [row for row in finished if row["graded"]]
    observed_ids = {row["run_id"] for row in rows if row["observed_result"]}
    complete = len(graded) == len(expected) if expected is not None else None
    by_harness = {}
    for harness in HARNESSES:
        observed = [row for row in rows if row["harness"] == harness]
        ended = [row for row in finished if row["harness"] == harness]
        scored = [row for row in graded if row["harness"] == harness]
        by_harness[harness] = {
            "expected": sum(item["harness"] == harness for item in expected.values()) if expected is not None else None,
            "observed": sum(row["observed_result"] for row in observed),
            "finished": len(ended), "graded": len(scored),
            "execution_status_counts": dict(Counter(row["execution_status"] for row in observed)),
            "outcomes": {name: statistics_for(row[name] for row in scored)
                         for name in ("official_score", "points_fraction", "full_completion")},
            "efficiency_finished": {name: statistics_for(row[name] for row in ended) for name in EFFICIENCY},
            "proxy_totals_observed": sum_proxy(observed),
            "diagnostic_native_finished": {name: statistics_for(row[name] for row in ended) for name in NATIVE},
        }
    return {
        "completeness": {
            "expected": len(expected) if expected is not None else None,
            "observed": len(observed_ids), "ledger_only_runs": len(rows) - len(observed_ids),
            "finished": len(finished), "graded": len(graded), "complete": complete,
            "missing_run_ids": sorted(set(expected) - observed_ids) if expected is not None else [],
            "unfinished_run_ids": [row["run_id"] for row in rows if row["observed_result"] and not row["finished"]],
        },
        "by_harness": by_harness, "proxy_totals": sum_proxy(rows),
        "paired": paired_summary(rows, expected),
    }


def paired_summary(rows, expected):
    indexed = {(row["task"], row["repeat"], row["harness"]): row for row in rows if row["harness"] in HARNESSES}
    identities = {(row["task"], row["repeat"]) for row in rows if row["harness"] in HARNESSES}
    if expected is not None:
        identities.update((item["task"], item["repeat"]) for item in expected.values())
    pairs = []
    for task, repeat in sorted(identities):
        duet, opencode = (indexed.get((task, repeat, harness)) for harness in HARNESSES)
        scored = all(row and row["finished"] and row["graded"] for row in (duet, opencode))
        both_full = bool(scored and duet["full_completion"] and opencode["full_completion"])
        pair = {
            "task": task, "repeat": repeat, "both_graded": scored,
            "duet_run_id": duet["run_id"] if duet else None,
            "opencode_run_id": opencode["run_id"] if opencode else None,
            "official_score_diff": duet["official_score"] - opencode["official_score"] if scored else None,
            "points_fraction_diff": duet["points_fraction"] - opencode["points_fraction"] if scored else None,
            "full_completion_diff": int(duet["full_completion"]) - int(opencode["full_completion"]) if scored else None,
            "both_full_completion": both_full,
            "efficiency": {},
        }
        if both_full:
            for name in EFFICIENCY:
                left, right = duet[name], opencode[name]
                comparable = left is not None and right is not None
                if name.endswith("tokens"):
                    comparable = comparable and duet["usage_coverage"] == opencode["usage_coverage"] == 1
                elif name == "estimated_cost_usd":
                    comparable = comparable and duet["estimated_cost_coverage"] == opencode["estimated_cost_coverage"] == 1
                pair["efficiency"][name] = {
                    "duet": left, "opencode": right,
                    "comparable": comparable,
                    "difference": left - right if comparable else None,
                    "ratio": left / right if comparable and right != 0 else None,
                }
        pairs.append(pair)
    comparable = [pair for pair in pairs if pair["both_full_completion"]]
    return {
        "expected_pairs": len(identities) if expected is not None else None,
        "observed_pair_keys": len({(row["task"], row["repeat"]) for row in rows if row["harness"] in HARNESSES}),
        "both_graded_pairs": sum(pair["both_graded"] for pair in pairs),
        "both_full_completion_pairs": len(comparable),
        "score_differences": {name: statistics_for(pair[name] for pair in pairs)
                              for name in ("official_score_diff", "points_fraction_diff", "full_completion_diff")},
        "efficiency_both_full": {
            name: {kind: statistics_for(pair["efficiency"][name][kind] for pair in comparable)
                   for kind in ("difference", "ratio")}
            for name in EFFICIENCY
        },
        "pairs": pairs,
    }


def ledger_conservation(groups, ledger_totals):
    checks = {}
    for name in PROXY_ADDITIVE:
        grouped = math.fsum(groups[group]["proxy_totals"][name] for group in PARTITION_GROUPS)
        checks[name] = {"ledger": ledger_totals[name], "grouped": grouped,
                        "equal": math.isclose(grouped, ledger_totals[name], rel_tol=1e-12, abs_tol=1e-12)}
    if not all(item["equal"] for item in checks.values()):
        raise AssertionError("Grouped usage totals do not conserve the proxy ledger")
    return {"passed": True,
            "cost_bases": {"estimated_cost_usd": "Original usage repriced with the report's documented rates.",
                           "proxy_estimated_cost_usd": "Original ledger estimated_cost_usd values.",
                           "budget_charge_usd": "Original ledger budget_charge_usd values."},
            "checks": checks}


if __name__ == "__main__":
    main()
