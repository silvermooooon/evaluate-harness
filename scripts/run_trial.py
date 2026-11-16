"""Run one fresh official TAC task, then invoke its unchanged evaluator."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--harness", choices=["duet", "opencode"], required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--label", default="main")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    run_id = f"{args.label}-{args.task}-{args.harness}-r{args.repeat}"
    directory = ROOT / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    container = "tac-eval-" + run_id
    image = f"ghcr.io/theagentcompany/{args.task}-image:1.0.0"
    result = {"run_id": run_id, "task": args.task, "harness": args.harness,
              "repeat": args.repeat, "label": args.label, "image": image,
              "model": "gpt-6-luna", "reasoning_effort": "medium", "max_output_tokens": 8192,
              "max_model_requests": 60, "wall_time_limit_seconds": args.timeout,
              "cpus": 2, "memory": "3g", "platform": "linux/amd64",
              "protocol_version": 1,
              "started_unix": time.time(), "status": "initializing"}
    save(directory, result)
    try:
        command = ["docker", "create", "--name", container, "--platform", "linux/amd64",
                   "--cpus", "2", "--memory", "3g", "--workdir", "/workspace",
                   "-v", f"{ROOT / 'adapters'}:/harness/adapters:ro",
                   "-v", f"{ROOT / 'vendor/agent-runtime-snapshot/src'}:/harness/sdk:ro",
                   "-v", f"{ROOT / '.tools/linux-python-locked'}:/harness/python:ro",
                   "-v", f"{ROOT / '.tools/opencode/v2.2.7/linux-x64/opencode'}:/usr/local/bin/opencode:ro",
                   "-v", f"{directory}:/harness-output", image, "sleep", "infinity"]
        run(command, directory / "setup.log")
        run(["docker", "start", container], directory / "setup.log")
        details = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
        result["image_id"] = details["Id"]
        result["image_digests"] = details.get("RepoDigests", [])
        run(["docker", "exec", "-e", "SERVER_HOSTNAME=host.docker.internal", container,
             "bash", "/utils/init.sh"], directory / "init.log", timeout=1000)
        original = subprocess.check_output(["docker", "exec", container, "cat", "/instruction/task.md"], text=True)
        prompt = (
            "Complete the task below in this environment without asking a human for help. "
            "The task's files and services are authoritative. "
            "Do not access /utils, /npc, evaluator code, grading rubrics, or reference answers. "
            "Use /workspace for your working files. If a task needs ownCloud login, "
            "the benchmark username and password are both theagentcompany.\n\n" + original
        )
        (directory / "instruction.txt").write_text(prompt)
        result["task_instruction_sha256"] = hashlib.sha256(original.encode()).hexdigest()
        result["input_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        adapter = "duet_agent.py" if args.harness == "duet" else "opencode_agent.py"
        result["adapter_sha256"] = hashlib.sha256((ROOT / "adapters" / adapter).read_bytes()).hexdigest()
        command = ["docker", "exec", "-e", "OPENAI_API_KEY=tac-proxy-placeholder",
                   "-e", f"OPENAI_BASE_URL=http://host.docker.internal:18765/{run_id}/v1",
                   "-e", "PYTHONPATH=/harness/sdk:/harness/python", container,
                   "python_default", f"/harness/adapters/{adapter}",
                   "--workspace", "/workspace", "--instruction-file", "/harness-output/instruction.txt",
                   "--output-dir", "/harness-output/agent", "--model", "gpt-6-luna", "--max-turns", "60"]
        if args.harness == "opencode":
            command.extend(["--binary", "/usr/local/bin/opencode"])
        started = time.monotonic()
        result["status"] = "running"
        save(directory, result)
        try:
            result["agent_exit_code"] = run(command, directory / "agent.log", timeout=args.timeout, check=False)
            result["status"] = "agent_finished"
        except subprocess.TimeoutExpired:
            result["status"] = "timeout"
            result["agent_exit_code"] = None
            # The task container has its own PID namespace. Stop all agent/tool
            # processes, including detached shells, before grading their files.
            run(["docker", "exec", container, "python_default", "-c",
                 "import os, signal; os.kill(-1, signal.SIGKILL)"], directory / "agent.log")
        result["agent_wall_seconds"] = time.monotonic() - started
        save(directory, result)
        events_path = directory / "agent/events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        # Some upstream checkpoints search trajectory text. Both harnesses must
        # expose the same scope: the real initial instruction plus their events.
        (directory / "evaluation-trajectory.json").write_text(json.dumps({
            "instruction": prompt, "events": events,
        }, ensure_ascii=False))
        trajectory = "/harness-output/evaluation-trajectory.json"
        grader = ["docker", "exec", "-e", "DECRYPTION_KEY=theagentcompany is all you need", container,
                  "python_default", "/utils/eval.py", "--trajectory_path", trajectory,
                  "--result_path", "/harness-output/grade.json"]
        result["grader_exit_code"] = run(grader, directory / "grade.log", timeout=300, check=False)
        if (directory / "grade.json").exists():
            grade = json.loads((directory / "grade.json").read_text())
            result["grade"] = grade
            total, points = grade["final_score"]["total"], grade["final_score"]["result"]
            result["full_completion"] = total > 0 and points == total
            result["official_score"] = (points / total) * 0.5 + (points // total) * 0.5
        else:
            result["evaluation_error"] = True
        run(["docker", "cp", f"{container}:/workspace", str(directory / "workspace")], directory / "setup.log", check=False)
    except Exception as exc:
        result["infrastructure_error"] = type(exc).__name__
        result["status"] = "infrastructure_error"
        print(f"{run_id}: {type(exc).__name__}", flush=True)
    finally:
        result["finished_unix"] = time.time()
        save(directory, result)
        run(["docker", "rm", "-f", "-v", container], directory / "setup.log", check=False)
    print(json.dumps(result), flush=True)


def run(command, logfile, timeout=None, check=True):
    with logfile.open("a") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    if check:
        result.check_returncode()
    return result.returncode


def save(directory, result):
    (directory / "result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
