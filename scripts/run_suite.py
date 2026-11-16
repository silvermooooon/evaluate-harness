"""Run the predeclared paired subset serially because ownCloud is shared."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="Override the run label from reports/task-selection.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "reports/task-selection.json").read_text())
    label = args.label or manifest["formal_run_label"]
    for repeat in range(1, manifest["planned_repeats_per_harness"] + 1):
        for index, task in enumerate(manifest["primary_tasks"]):
            harnesses = ("duet", "opencode") if (repeat + index) % 2 else ("opencode", "duet")
            for harness in harnesses:
                run_id = f"{label}-{task}-{harness}-r{repeat}"
                output = root / "runs" / run_id
                if output.exists():
                    result = json.loads((output / "result.json").read_text())
                    if result.get("finished_unix") and result.get("grader_exit_code") == 0:
                        print(f"Existing graded run: {run_id}", flush=True)
                        continue
                    raise RuntimeError(f"Inspect incomplete run before resuming: {run_id}")
                print(f"START {run_id}", flush=True)
                result = subprocess.run([sys.executable, str(root / "scripts/run_trial.py"),
                                         "--task", task, "--harness", harness, "--repeat", str(repeat),
                                         "--label", label],
                                        cwd=root, check=False)
                if result.returncode:
                    raise RuntimeError(f"Runner failed: {run_id}")
                row = json.loads((output / "result.json").read_text())
                if row.get("infrastructure_error") or row.get("evaluation_error"):
                    raise RuntimeError(f"Infrastructure or grading requires inspection: {run_id}")
                print(f"DONE {run_id}: full={row['full_completion']} score={row['official_score']:.3f}", flush=True)


if __name__ == "__main__":
    main()
